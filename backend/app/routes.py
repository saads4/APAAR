"""HTTP API. Authorisation is enforced here on the server (RBAC by role, ABAC by institution / APAAR owner)."""
import csv
import io
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import analytics, audit, claims as cl, config, credentials as cr, intelligence, matching
from .db import SessionLocal, get_db
from .models import (AccessRevocation, Alert, AuditLog, BulkJob, Claim, Credential, Institution, Lead, OutboxMessage,
                     Programme, SeatException, SeatLedger, User, VerificationEvent, utcnow)
from .security import (AICTE, current_user, decrypt, hash_password, keyed_hash, make_token, decode_token, new_totp_secret,
                       public_key_b64, rate_limit, require_roles, totp_code, verify_password, verify_signature, verify_totp, sign)

router = APIRouter(prefix="/api")


def _err(e: cl.ClaimError):
    return HTTPException(e.status, e.message)


# =============================================================== auth
class LoginIn(BaseModel):
    email: str
    password: str


class MfaIn(BaseModel):
    mfa_token: str
    code: str


class StudentRegisterIn(BaseModel):
    apaar_id: str
    dob: str
    email: str
    password: str = Field(min_length=8)


def user_dict(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role, "institution_id": u.institution_id, "org": u.org}


@router.post("/auth/login", tags=["auth"])
def login(body: LoginIn, db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if u and u.locked_until and u.locked_until > utcnow():
        raise HTTPException(423, "Account temporarily locked after repeated failures")
    if not u or not verify_password(body.password, u.password_hash):
        if u:
            u.failed_logins += 1
            if u.failed_logins >= 5:
                from datetime import timedelta
                u.locked_until = utcnow() + timedelta(minutes=10)
                u.failed_logins = 0
            db.commit()
        raise HTTPException(401, "Invalid email or password")
    u.failed_logins = 0
    db.commit()
    if u.mfa_secret:
        return {"mfa_required": True, "mfa_token": make_token(u, kind="mfa", minutes=5)}
    audit.log(db, u.email, "LOGIN", "user", u.id)
    db.commit()
    return {"token": make_token(u), "user": user_dict(u)}


@router.post("/auth/mfa", tags=["auth"])
def mfa(body: MfaIn, db: Session = Depends(get_db)):
    try:
        data = decode_token(body.mfa_token)
    except Exception:
        raise HTTPException(401, "MFA session expired")
    if data.get("kind") != "mfa":
        raise HTTPException(401, "Wrong token type")
    u = db.get(User, int(data["sub"]))
    if not verify_totp(u.mfa_secret, body.code):
        raise HTTPException(401, "Invalid authenticator code")
    audit.log(db, u.email, "LOGIN_MFA", "user", u.id)
    db.commit()
    return {"token": make_token(u), "user": user_dict(u)}


@router.get("/auth/demo-mfa", tags=["auth"])
def demo_mfa(email: str, db: Session = Depends(get_db)):
    """DEMO ONLY: stands in for the authenticator app so a live demo needs no phone."""
    if not config.DEMO_MODE:
        raise HTTPException(404, "Not found")
    u = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not u or not u.mfa_secret:
        raise HTTPException(404, "No MFA for that user")
    return {"code": totp_code(u.mfa_secret)}


@router.get("/auth/me", tags=["auth"])
def me(u: User = Depends(current_user)):
    return user_dict(u)


@router.post("/auth/student-register", tags=["auth"])
def student_register(body: StudentRegisterIn, db: Session = Depends(get_db)):
    """A student proves they own an APAAR ID by supplying the registry date of birth."""
    from .models import RegistryRecord
    apaar = cl.normalize_apaar(body.apaar_id)
    dob = matching.parse_dob(body.dob)
    rec = db.get(RegistryRecord, apaar) if apaar else None
    if not rec or not dob or rec.dob != dob:
        raise HTTPException(400, "APAAR ID and date of birth do not match the registry")
    email = body.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Email already registered")
    if db.scalar(select(User).where(User.apaar_hash == keyed_hash(apaar))):
        raise HTTPException(409, "This APAAR ID already has an account")
    u = User(email=email, name=rec.full_name, password_hash=hash_password(body.password), role="STUDENT",
             apaar_hash=keyed_hash(apaar))
    db.add(u)
    db.commit()
    return {"token": make_token(u), "user": user_dict(u)}


# =============================================================== claims (institution)
class ClaimIn(BaseModel):
    programme_id: int
    seat_type: str = "REGULAR"
    student_name: str
    dob: str
    phone: str
    apaar_id: Optional[str] = None
    email: Optional[str] = None
    guardian_phone: Optional[str] = None
    gender: Optional[str] = None
    category: Optional[str] = None
    student_ref: Optional[str] = None
    provisional_reason: Optional[str] = None


class ClaimEdit(BaseModel):
    apaar_id: Optional[str] = None
    student_name: Optional[str] = None
    dob: Optional[str] = None


def claim_dict(c: Claim, db: Session, detail: bool = False, include_pii: bool = True) -> dict:
    prog = db.get(Programme, c.programme_id)
    inst = db.get(Institution, c.institution_id)
    d = {"id": c.id, "institution_id": inst.id, "institution": inst.name, "programme_id": prog.id, "programme": prog.name,
         "seat_type": c.seat_type, "student_ref": c.student_ref, "apaar_masked": c.apaar_masked, "student_name": c.student_name,
         "dob": c.dob.isoformat(), "state": c.state, "reason": c.reason, "fixes": c.fixes, "match_prob": c.match_prob,
         "provisional_token": c.provisional_token, "provisional_deadline": c.provisional_deadline.isoformat() if c.provisional_deadline else None,
         "alt_token": c.alt_token, "admitted_on": c.admitted_on.isoformat(), "created_at": c.created_at.isoformat(),
         "verified_at": c.verified_at.isoformat() if c.verified_at else None, "category": c.category, "gender": c.gender,
         "cycle": c.cycle, "retry_count": c.retry_count, "confirm_method": c.confirm_method}
    if include_pii:
        d["phone_masked"] = "XXXXXX" + (decrypt(c.phone_enc) or "")[-4:]
    if detail:
        d["match_detail"] = c.match_detail
        d["conflict_with"] = c.conflict_with
    return d


def _own_claim(db: Session, user: User, claim_id: int) -> Claim:
    c = db.get(Claim, claim_id)
    if not c or (user.role == "INSTITUTION" and c.institution_id != user.institution_id):
        raise HTTPException(404, "Claim not found")  # 404, not 403: don't reveal other institutions' records
    return c


def _to_input(user: User, body: ClaimIn, admitted: Optional[date] = None) -> cl.ClaimInput:
    dob = matching.parse_dob(body.dob)
    if not dob:
        raise cl.ClaimError(422, "Date of birth must be YYYY-MM-DD or DD-MM-YYYY")
    return cl.ClaimInput(institution_id=user.institution_id, programme_id=body.programme_id, seat_type=body.seat_type.upper(),
                         student_name=body.student_name, dob=dob, phone=body.phone, apaar_id=body.apaar_id, email=body.email,
                         guardian_phone=body.guardian_phone, gender=body.gender, category=body.category,
                         student_ref=body.student_ref, admitted_on=admitted, provisional_reason=body.provisional_reason)


@router.post("/claims", tags=["claims"], status_code=201)
def submit_claim(body: ClaimIn, idempotency_key: Optional[str] = Header(None), user: User = Depends(rate_limit),
                 db: Session = Depends(get_db)):
    if user.role != "INSTITUTION":
        raise HTTPException(403, "Only institution accounts submit claims")
    try:
        c = cl.create_claim(db, _to_input(user, body), user.email, idempotency_key)
    except cl.ClaimError as e:
        raise _err(e)
    return claim_dict(c, db, detail=True)


@router.get("/claims", tags=["claims"])
def list_claims(state: Optional[str] = None, programme_id: Optional[int] = None, q: Optional[str] = None,
                limit: int = Query(100, le=500), offset: int = 0, user: User = Depends(require_roles("INSTITUTION")),
                db: Session = Depends(get_db)):
    stmt = select(Claim).where(Claim.institution_id == user.institution_id, Claim.cycle == cl.current_cycle(),
                               Claim.historic.is_(False))
    if state:
        stmt = stmt.where(Claim.state == state)
    if programme_id:
        stmt = stmt.where(Claim.programme_id == programme_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(Claim.student_name.ilike(like) | Claim.student_ref.ilike(like) | Claim.apaar_masked.ilike(like))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Claim.id.desc()).limit(limit).offset(offset)).all()
    return {"total": total, "items": [claim_dict(c, db) for c in rows]}


@router.get("/claims/{claim_id}", tags=["claims"])
def get_claim(claim_id: int, user: User = Depends(require_roles("INSTITUTION")), db: Session = Depends(get_db)):
    return claim_dict(_own_claim(db, user, claim_id), db, detail=True)


@router.patch("/claims/{claim_id}", tags=["claims"])
def edit_claim(claim_id: int, body: ClaimEdit, user: User = Depends(require_roles("INSTITUTION")), db: Session = Depends(get_db)):
    c = _own_claim(db, user, claim_id)
    dob = matching.parse_dob(body.dob) if body.dob else None
    if body.dob and not dob:
        raise HTTPException(422, "Invalid date of birth")
    try:
        c = cl.update_claim(db, c, user.email, body.apaar_id, body.student_name, dob)
    except cl.ClaimError as e:
        raise _err(e)
    return claim_dict(c, db, detail=True)


class FixIn(BaseModel):
    fields: list[str]


@router.post("/claims/{claim_id}/apply-fixes", tags=["claims"])
def apply_fixes(claim_id: int, body: FixIn, user: User = Depends(require_roles("INSTITUTION")), db: Session = Depends(get_db)):
    c = _own_claim(db, user, claim_id)
    try:
        return claim_dict(cl.apply_fixes(db, c, body.fields, user.email), db, detail=True)
    except cl.ClaimError as e:
        raise _err(e)


class WithdrawIn(BaseModel):
    reason: str


@router.post("/claims/{claim_id}/withdraw", tags=["claims"])
def withdraw(claim_id: int, body: WithdrawIn, user: User = Depends(require_roles("INSTITUTION")), db: Session = Depends(get_db)):
    c = _own_claim(db, user, claim_id)
    try:
        cl.withdraw_claim(db, c, user.email, body.reason)
    except cl.ClaimError as e:
        raise _err(e)
    return claim_dict(c, db)


@router.post("/claims/{claim_id}/retry", tags=["claims"])
def retry(claim_id: int, user: User = Depends(require_roles("INSTITUTION")), db: Session = Depends(get_db)):
    c = _own_claim(db, user, claim_id)
    if c.state not in ("PENDING", "SEAT_LIMIT", "CONFLICT", "PENDING_FIX"):
        raise HTTPException(409, f"Claim in state {c.state} cannot be re-run")
    return claim_dict(cl.run_protocol(db, c, user.email), db, detail=True)


@router.post("/claims/{claim_id}/resend-otp", tags=["claims"])
def inst_resend(claim_id: int, user: User = Depends(require_roles("INSTITUTION")), db: Session = Depends(get_db)):
    try:
        cl.resend_otp(db, _own_claim(db, user, claim_id), user.email)
    except cl.ClaimError as e:
        raise _err(e)
    return {"ok": True}


@router.get("/ledger", tags=["claims"])
def my_ledger(user: User = Depends(require_roles("INSTITUTION")), db: Session = Depends(get_db)):
    return analytics.ledger_rows(db, user.institution_id)


@router.get("/ledger/timeline", tags=["claims"])
def ledger_timeline(programme_id: int, seat_type: str = "REGULAR", user: User = Depends(current_user), db: Session = Depends(get_db)):
    prog = db.get(Programme, programme_id)
    if not prog or (user.role == "INSTITUTION" and prog.institution_id != user.institution_id) or user.role not in ("INSTITUTION",) + AICTE:
        raise HTTPException(404, "Programme not found")
    return analytics.timeline(db, programme_id, seat_type.upper())


@router.get("/programmes", tags=["claims"])
def programmes(user: User = Depends(require_roles("INSTITUTION")), db: Session = Depends(get_db)):
    ps = db.scalars(select(Programme).where(Programme.institution_id == user.institution_id)).all()
    return [{"id": p.id, "name": p.name, "level": p.level} for p in ps]


@router.get("/my/alerts", tags=["claims"])
def my_alerts(user: User = Depends(require_roles("INSTITUTION")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Alert).where(Alert.institution_id == user.institution_id).order_by(Alert.id.desc()).limit(20)).all()
    return [{"id": a.id, "kind": a.kind, "message": a.message, "at": a.created_at.isoformat()} for a in rows]


# ---------------- bulk CSV (same code path as single claims, via a background job)
CSV_COLS = ["student_name", "dob", "apaar_id", "phone", "email", "programme", "seat_type", "gender", "category", "guardian_phone", "provisional_reason"]


def _process_job(job_id: int, inst_id: int, actor: str):
    db = SessionLocal()
    try:
        job = db.get(BulkJob, job_id)
        job.status = "RUNNING"
        db.commit()
        progs = {p.name.lower(): p.id for p in db.scalars(select(Programme).where(Programme.institution_id == inst_id))}
        results = []
        for i, row in enumerate(job.payload, start=1):
            res = {"row": i, "student_name": row.get("student_name", "")}
            try:
                pid = progs.get((row.get("programme") or "").strip().lower())
                if not pid:
                    raise cl.ClaimError(422, f"Unknown programme '{row.get('programme')}'")
                dob = matching.parse_dob(row.get("dob", ""))
                if not dob:
                    raise cl.ClaimError(422, "Invalid dob")
                inp = cl.ClaimInput(institution_id=inst_id, programme_id=pid, seat_type=(row.get("seat_type") or "REGULAR").upper(),
                                    student_name=row.get("student_name", ""), dob=dob, phone=row.get("phone", ""),
                                    apaar_id=row.get("apaar_id") or None, email=row.get("email") or None,
                                    guardian_phone=row.get("guardian_phone") or None, gender=row.get("gender") or None,
                                    category=row.get("category") or None, provisional_reason=row.get("provisional_reason") or None)
                c = cl.create_claim(db, inp, actor, idem_key=f"bulk{job_id}-{i}")
                res.update(claim_id=c.id, state=c.state, reason=c.reason)
            except cl.ClaimError as e:
                db.rollback()
                res.update(claim_id=None, state="REJECTED_ROW", reason=e.message)
            except Exception as e:  # never let one bad row kill the file
                db.rollback()
                res.update(claim_id=None, state="REJECTED_ROW", reason=f"Unexpected error: {type(e).__name__}")
            results.append(res)
            job = db.get(BulkJob, job_id)
            job.processed, job.results = i, list(results)
            db.commit()
        job = db.get(BulkJob, job_id)
        job.status = "DONE"
        db.commit()
    except Exception:
        db.rollback()
        job = db.get(BulkJob, job_id)
        job.status = "FAILED"
        db.commit()
    finally:
        db.close()


@router.post("/claims/bulk", tags=["claims"], status_code=202)
async def bulk_upload(bg: BackgroundTasks, file: UploadFile = File(...), user: User = Depends(rate_limit), db: Session = Depends(get_db)):
    if user.role != "INSTITUTION":
        raise HTTPException(403, "Only institution accounts upload files")
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    if len(raw) > 2_000_000:
        raise HTTPException(413, "File too large (2 MB limit in the prototype)")
    reader = csv.DictReader(io.StringIO(raw))
    missing = [c for c in ("student_name", "dob", "phone", "programme") if c not in (reader.fieldnames or [])]
    if missing:
        raise HTTPException(422, f"CSV is missing required columns: {', '.join(missing)}")
    rows = [{k.strip(): (v or "").strip() for k, v in r.items() if k} for r in reader]
    if not rows:
        raise HTTPException(422, "CSV has no data rows")
    if len(rows) > 5000:
        raise HTTPException(413, "Max 5000 rows per file")
    job = BulkJob(institution_id=user.institution_id, filename=file.filename or "upload.csv", total=len(rows), payload=rows, created_by=user.email)
    db.add(job)
    db.commit()
    bg.add_task(_process_job, job.id, user.institution_id, user.email)
    return {"job_id": job.id, "total": job.total, "status": job.status}


@router.get("/claims/bulk/{job_id}", tags=["claims"])
def bulk_status(job_id: int, user: User = Depends(require_roles("INSTITUTION")), db: Session = Depends(get_db)):
    job = db.get(BulkJob, job_id)
    if not job or job.institution_id != user.institution_id:
        raise HTTPException(404, "Job not found")
    summary = {}
    for r in job.results:
        summary[r["state"]] = summary.get(r["state"], 0) + 1
    return {"id": job.id, "filename": job.filename, "status": job.status, "total": job.total, "processed": job.processed,
            "summary": summary, "results": job.results}


@router.get("/claims-template.csv", tags=["claims"])
def template():
    return Response(",".join(CSV_COLS) + "\nAarav Patil,12-08-2007,,9876501234,aarav@example.com,B.Tech CSE,REGULAR,M,GEN,9876500000,Awaiting APAAR generation\n",
                    media_type="text/csv")


# ---------------- exceptions
class ExcIn(BaseModel):
    programme_id: int
    seat_type: str
    category: str
    seats: int = Field(ge=1, le=50)
    justification: str = Field(min_length=10)


def exc_dict(e: SeatException, db: Session) -> dict:
    p = db.get(Programme, e.programme_id)
    return {"id": e.id, "institution_id": e.institution_id, "institution": db.get(Institution, e.institution_id).name, "programme": p.name,
            "seat_type": e.seat_type, "category": e.category, "seats": e.seats, "justification": e.justification, "state": e.state,
            "reviewer_reason": e.reviewer_reason, "created_at": e.created_at.isoformat()}


@router.post("/exceptions", tags=["exceptions"], status_code=201)
def request_exception(body: ExcIn, user: User = Depends(require_roles("INSTITUTION")), db: Session = Depends(get_db)):
    p = db.get(Programme, body.programme_id)
    if not p or p.institution_id != user.institution_id:
        raise HTTPException(404, "Programme not found")
    if body.category not in ("DUAL_PROGRAMME", "TRANSFER", "SUPERNUMERARY", "OTHER"):
        raise HTTPException(422, "Unknown exception category")
    if not db.scalar(select(SeatLedger).where(SeatLedger.programme_id == p.id, SeatLedger.seat_type == body.seat_type.upper(),
                                              SeatLedger.cycle == cl.current_cycle())):
        raise HTTPException(422, "No such seat ledger")
    e = SeatException(institution_id=user.institution_id, programme_id=p.id, seat_type=body.seat_type.upper(), category=body.category,
                      seats=body.seats, justification=body.justification)
    db.add(e)
    db.flush()
    audit.log(db, user.email, "EXCEPTION_REQUESTED", "exception", e.id, {"seats": e.seats, "category": e.category})
    db.commit()
    return exc_dict(e, db)


@router.get("/exceptions", tags=["exceptions"])
def list_exceptions(user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(SeatException).order_by(SeatException.id.desc())
    if user.role == "INSTITUTION":
        stmt = stmt.where(SeatException.institution_id == user.institution_id)
    elif user.role not in AICTE:
        raise HTTPException(403, "Forbidden")
    return [exc_dict(e, db) for e in db.scalars(stmt.limit(100))]


class DecideIn(BaseModel):
    approve: bool
    reason: str


@router.post("/exceptions/{eid}/decide", tags=["exceptions"])
def decide_exception(eid: int, body: DecideIn, user: User = Depends(require_roles(*AICTE)), db: Session = Depends(get_db)):
    e = db.get(SeatException, eid)
    if not e:
        raise HTTPException(404, "Not found")
    try:
        cl.decide_exception(db, e, body.approve, user.id, body.reason, user.email)
    except cl.ClaimError as ex:
        raise _err(ex)
    return exc_dict(e, db)


# =============================================================== student wallet
def _student_claims(db: Session, user: User) -> list[Claim]:
    if not user.apaar_hash:
        return []
    return db.scalars(select(Claim).where(Claim.apaar_hash == user.apaar_hash).order_by(Claim.id.desc())).all()


def _own_student_claim(db: Session, user: User, claim_id: int) -> Claim:
    c = db.get(Claim, claim_id)
    if not c or not user.apaar_hash or c.apaar_hash != user.apaar_hash:
        raise HTTPException(404, "Claim not found")
    return c


@router.get("/student/wallet", tags=["student"])
def wallet(user: User = Depends(require_roles("STUDENT")), db: Session = Depends(get_db)):
    out = []
    for c in _student_claims(db, user):
        cred = db.scalar(select(Credential).where(Credential.claim_id == c.id))
        d = claim_dict(c, db, include_pii=False)
        d["credential"] = None
        if cred:
            d["credential"] = {"id": cred.id, "status": cl_status(db, cred), "qr_token": cred.qr_token,
                               "verify_url": f"{config.FRONTEND_URL}/#/verify?c={cred.id}&t={cred.qr_token}",
                               "issued_at": cred.issued_at.isoformat(),
                               "fields": {k: v["value"] for k, v in cred.attributes.items()}}
        out.append(d)
    return {"claims": out}


def cl_status(db: Session, cred: Credential) -> str:
    return cr._status(db, cred)


class ConfirmIn(BaseModel):
    method: str = "OTP"
    otp: Optional[str] = None


@router.post("/student/claims/{claim_id}/confirm", tags=["student"])
def student_confirm(claim_id: int, body: ConfirmIn, user: User = Depends(require_roles("STUDENT")), db: Session = Depends(get_db)):
    c = _own_student_claim(db, user, claim_id)
    try:
        cl.confirm_claim(db, c, body.method, body.otp, user.email)
    except cl.ClaimError as e:
        raise _err(e)
    return claim_dict(c, db, include_pii=False)


@router.post("/student/claims/{claim_id}/resend-otp", tags=["student"])
def student_resend(claim_id: int, user: User = Depends(require_roles("STUDENT")), db: Session = Depends(get_db)):
    try:
        cl.resend_otp(db, _own_student_claim(db, user, claim_id), user.email)
    except cl.ClaimError as e:
        raise _err(e)
    return {"ok": True}


@router.post("/student/claims/{claim_id}/refuse-consent", tags=["student"])
def student_refuse(claim_id: int, user: User = Depends(require_roles("STUDENT")), db: Session = Depends(get_db)):
    c = _own_student_claim(db, user, claim_id)
    try:
        cl.refuse_consent(db, c, user.email)
    except cl.ClaimError as e:
        raise _err(e)
    return claim_dict(c, db, include_pii=False)


@router.get("/student/access-log", tags=["student"])
def access_log(user: User = Depends(require_roles("STUDENT")), db: Session = Depends(get_db)):
    ids = [x.id for x in db.scalars(select(Credential).join(Claim, Claim.id == Credential.claim_id).where(Claim.apaar_hash == user.apaar_hash))]
    if not ids:
        return {"events": [], "revoked": []}
    evs = db.scalars(select(VerificationEvent).where(VerificationEvent.credential_id.in_(ids)).order_by(VerificationEvent.id.desc()).limit(100)).all()
    revoked = db.scalars(select(AccessRevocation).where(AccessRevocation.credential_id.in_(ids))).all()
    return {"events": [{"id": e.id, "credential_id": e.credential_id, "org": e.org, "purpose": e.purpose, "reason": e.reason,
                        "fields": e.fields, "result": e.result, "at": e.created_at.isoformat()} for e in evs],
            "revoked": [{"credential_id": r.credential_id, "org": r.org} for r in revoked]}


class RevokeIn(BaseModel):
    credential_id: str
    org: str


@router.post("/student/revoke-access", tags=["student"])
def revoke_access(body: RevokeIn, user: User = Depends(require_roles("STUDENT")), db: Session = Depends(get_db)):
    cred = db.get(Credential, body.credential_id)
    claim = db.get(Claim, cred.claim_id) if cred else None
    if not cred or claim.apaar_hash != user.apaar_hash:
        raise HTTPException(404, "Credential not found")
    if not db.scalar(select(AccessRevocation).where(AccessRevocation.credential_id == cred.id, AccessRevocation.org == body.org)):
        db.add(AccessRevocation(credential_id=cred.id, org=body.org))
        audit.log(db, user.email, "ACCESS_REVOKED", "credential", cred.id, {"org": body.org})
        db.commit()
    return {"ok": True}


@router.delete("/student/revoke-access", tags=["student"])
def restore_access(credential_id: str, org: str, user: User = Depends(require_roles("STUDENT")), db: Session = Depends(get_db)):
    cred = db.get(Credential, credential_id)
    claim = db.get(Claim, cred.claim_id) if cred else None
    if not cred or claim.apaar_hash != user.apaar_hash:
        raise HTTPException(404, "Credential not found")
    r = db.scalar(select(AccessRevocation).where(AccessRevocation.credential_id == cred.id, AccessRevocation.org == org))
    if r:
        db.delete(r)
        db.commit()
    return {"ok": True}


# =============================================================== verifier + public
class VerifyIn(BaseModel):
    credential_id: str
    purpose: str
    reason: str = Field(min_length=5)


@router.get("/verifier/purposes", tags=["verifier"])
def purposes(user: User = Depends(require_roles("VERIFIER"))):
    return cr.PURPOSES


@router.post("/verifier/verify", tags=["verifier"])
def verifier_verify(body: VerifyIn, user: User = Depends(rate_limit), db: Session = Depends(get_db)):
    if user.role != "VERIFIER":
        raise HTTPException(403, "Verifier accounts only")
    if body.purpose not in cr.PURPOSES:
        raise HTTPException(422, f"purpose must be one of {', '.join(cr.PURPOSES)}")
    return cr.verify_for_org(db, body.credential_id, body.purpose, body.reason, user.org or user.name, user.id)


@router.get("/public/verify/{cred_id}", tags=["public"])
def public_verify(cred_id: str, t: str = "", db: Session = Depends(get_db)):
    return cr.public_yes_no(db, cred_id, t)


class CheckSigIn(BaseModel):
    payload: dict
    signature: str


@router.post("/public/check-signature", tags=["public"])
def check_signature(body: CheckSigIn):
    return {"valid": verify_signature(body.payload, body.signature)}


@router.get("/public/key", tags=["public"])
def pubkey():
    return {"algorithm": "Ed25519", "public_key": public_key_b64()}


# =============================================================== AICTE
@router.get("/aicte/dashboard", tags=["aicte"])
def dashboard(user: User = Depends(require_roles(*AICTE)), db: Session = Depends(get_db)):
    d = analytics.national_dashboard(db)
    d["registry_outage"] = cl.get_setting(db, "registry_outage") == "1"
    return d


@router.get("/aicte/ledger", tags=["aicte"])
def aicte_ledger(institution_id: Optional[int] = None, user: User = Depends(require_roles(*AICTE)), db: Session = Depends(get_db)):
    return analytics.ledger_rows(db, institution_id)


@router.get("/aicte/institutions", tags=["aicte"])
def institutions(user: User = Depends(require_roles(*AICTE)), db: Session = Depends(get_db)):
    return [{"id": i.id, "code": i.code, "name": i.name, "state": i.state} for i in db.scalars(select(Institution).order_by(Institution.name))]


@router.get("/aicte/trust-index", tags=["aicte"])
def trust(user: User = Depends(require_roles(*AICTE)), db: Session = Depends(get_db)):
    return intelligence.trust_index(db, cl.current_cycle())


@router.post("/aicte/intelligence/run", tags=["aicte"])
def run_intel(user: User = Depends(require_roles(*AICTE)), db: Session = Depends(get_db)):
    res = intelligence.run(db, cl.current_cycle())
    audit.log(db, user.email, "INTELLIGENCE_RUN", "system", "-", res)
    db.commit()
    return res


def lead_dict(l: Lead, db: Session) -> dict:
    return {"id": l.id, "institution_id": l.institution_id, "institution": db.get(Institution, l.institution_id).name, "layer": l.layer,
            "severity": l.severity, "title": l.title, "baseline": l.baseline, "deviation": l.deviation, "evidence": l.evidence,
            "action": l.action, "status": l.status, "reason_code": l.reason_code, "reviewer_note": l.reviewer_note,
            "created_at": l.created_at.isoformat()}


@router.get("/aicte/leads", tags=["aicte"])
def leads(status: Optional[str] = None, institution_id: Optional[int] = None, user: User = Depends(require_roles(*AICTE)),
          db: Session = Depends(get_db)):
    stmt = select(Lead)
    if status:
        stmt = stmt.where(Lead.status == status)
    if institution_id:
        stmt = stmt.where(Lead.institution_id == institution_id)
    sev = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    rows = sorted(db.scalars(stmt), key=lambda l: (sev[l.severity], -l.id))
    return [lead_dict(l, db) for l in rows]


class LeadDecision(BaseModel):
    status: str
    reason_code: str
    note: str = ""


REASON_CODES = ("EXPLAINED_BY_INSTITUTION", "DATA_ERROR", "NEEDS_INSPECTION", "CONFIRMED_IRREGULARITY", "FALSE_POSITIVE", "DUPLICATE_LEAD")


@router.post("/aicte/leads/{lead_id}/decide", tags=["aicte"])
def decide_lead(lead_id: int, body: LeadDecision, user: User = Depends(require_roles(*AICTE)), db: Session = Depends(get_db)):
    l = db.get(Lead, lead_id)
    if not l:
        raise HTTPException(404, "Lead not found")
    if body.status not in ("CONFIRMED", "DISMISSED", "ESCALATED"):
        raise HTTPException(422, "status must be CONFIRMED, DISMISSED or ESCALATED")
    if body.reason_code not in REASON_CODES:
        raise HTTPException(422, f"reason_code must be one of {', '.join(REASON_CODES)}")
    l.status, l.reason_code, l.reviewer_note, l.reviewer_id, l.decided_at = body.status, body.reason_code, body.note, user.id, utcnow()
    audit.log(db, user.email, "LEAD_DECIDED", "lead", l.id, {"status": body.status, "reason_code": body.reason_code})
    db.commit()
    return lead_dict(l, db)


@router.get("/aicte/claims", tags=["aicte"])
def drill_down(institution_id: int, reason: str = Query(..., min_length=10), state: Optional[str] = None, limit: int = Query(50, le=200),
               user: User = Depends(require_roles(*AICTE)), db: Session = Depends(get_db)):
    """Student-level drill-down needs a logged reason; aggregates are the default view."""
    audit.log(db, user.email, "DRILL_DOWN", "institution", institution_id, {"reason": reason, "state": state})
    stmt = select(Claim).where(Claim.institution_id == institution_id, Claim.cycle == cl.current_cycle(), Claim.historic.is_(False))
    if state:
        stmt = stmt.where(Claim.state == state)
    db.commit()
    return [claim_dict(c, db, include_pii=False) for c in db.scalars(stmt.order_by(Claim.id.desc()).limit(limit))]


class OutageIn(BaseModel):
    outage: bool


@router.post("/aicte/registry-outage", tags=["aicte"])
def registry_outage(body: OutageIn, user: User = Depends(require_roles("AICTE_ADMIN")), db: Session = Depends(get_db)):
    """Simulates the (mock) registry going down so PENDING handling can be demonstrated."""
    cl.set_setting(db, "registry_outage", "1" if body.outage else "0")
    audit.log(db, user.email, "REGISTRY_OUTAGE_SET", "system", "registry", {"outage": body.outage})
    db.commit()
    res = cl.retry_pending(db) if not body.outage else {}
    return {"outage": body.outage, "retry": res}


@router.post("/aicte/retry-pending", tags=["aicte"])
def aicte_retry(user: User = Depends(require_roles("AICTE_ADMIN")), db: Session = Depends(get_db)):
    return cl.retry_pending(db)


class SweepIn(BaseModel):
    simulate_days_ahead: int = Field(0, ge=0, le=400)


@router.post("/aicte/sweep", tags=["aicte"])
def sweep(body: SweepIn, user: User = Depends(require_roles("AICTE_ADMIN")), db: Session = Depends(get_db)):
    """Marks claims with no student response in 30 days UNCONFIRMED. `simulate_days_ahead` moves the clock for demos."""
    from datetime import timedelta
    res = cl.sweep_unconfirmed(db, utcnow() + timedelta(days=body.simulate_days_ahead))
    audit.log(db, user.email, "SWEEP", "system", "unconfirmed", {**res, "days_ahead": body.simulate_days_ahead})
    db.commit()
    return res


class SimIn(BaseModel):
    intake_multiplier: float = Field(1.0, ge=0.5, le=2.0)
    stop_pct: int = Field(100, ge=50, le=120)
    enforce_unique: bool = True


@router.post("/aicte/simulate", tags=["aicte"])
def simulate(body: SimIn, user: User = Depends(require_roles(*AICTE)), db: Session = Depends(get_db)):
    return analytics.simulate(db, body.intake_multiplier, body.stop_pct, body.enforce_unique)


@router.get("/aicte/report", tags=["aicte"])
def report(group_by: str = "state", format: str = "json", user: User = Depends(require_roles(*AICTE)), db: Session = Depends(get_db)):
    try:
        rep = analytics.enrolment_report(db, group_by)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if format == "csv":
        return Response(analytics.report_csv(rep), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="enrolment_by_{group_by}.csv"'})
    return rep


@router.post("/aicte/cohort-roots/publish", tags=["aicte"])
def publish_roots(user: User = Depends(require_roles("AICTE_ADMIN")), db: Session = Depends(get_db)):
    res = analytics.publish_roots(db)
    audit.log(db, user.email, "COHORT_ROOTS_PUBLISHED", "system", "-", {"institutions": len(res)})
    db.commit()
    return res


@router.get("/aicte/cohort-roots/check", tags=["aicte"])
def check_roots(user: User = Depends(require_roles(*AICTE)), db: Session = Depends(get_db)):
    return analytics.check_roots(db)


@router.get("/aicte/audit/verify", tags=["aicte"])
def audit_verify(user: User = Depends(require_roles(*AICTE)), db: Session = Depends(get_db)):
    return audit.verify_chain(db)


@router.get("/aicte/audit", tags=["aicte"])
def audit_tail(limit: int = Query(30, le=200), user: User = Depends(require_roles(*AICTE)), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()
    return [{"id": r.id, "ts": r.ts, "actor": r.actor, "action": r.action, "entity": r.entity, "entity_id": r.entity_id,
             "detail": r.detail, "hash": r.hash[:16]} for r in rows]


class NLIn(BaseModel):
    question: str = Field(min_length=3, max_length=300)


@router.post("/aicte/ask", tags=["aicte"])
def ask(body: NLIn, user: User = Depends(require_roles(*AICTE)), db: Session = Depends(get_db)):
    return analytics.nl_query(db, body.question)


# ---------------- inclusion proof (institution or student can show a record is inside a published cohort root)
@router.get("/claims/{claim_id}/inclusion-proof", tags=["claims"])
def inclusion(claim_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = db.get(Claim, claim_id)
    allowed = c and ((user.role == "INSTITUTION" and c.institution_id == user.institution_id) or user.role in AICTE
                     or (user.role == "STUDENT" and c.apaar_hash == user.apaar_hash))
    if not allowed:
        raise HTTPException(404, "Claim not found")
    try:
        return analytics.inclusion_proof(db, c)
    except LookupError as e:
        raise HTTPException(404, str(e))


# =============================================================== demo helpers
@router.get("/dev/outbox", tags=["demo"])
def outbox(claim_id: Optional[int] = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """DEMO ONLY: the mock SMS gateway. A student sees only their own messages; an institution never sees OTPs."""
    if not config.DEMO_MODE:
        raise HTTPException(404, "Not found")
    if user.role != "STUDENT":
        raise HTTPException(403, "Only the student's own phone receives OTPs")
    mine = [c.id for c in _student_claims(db, user)]
    stmt = select(OutboxMessage).where(OutboxMessage.claim_id.in_(mine or [0]))
    if claim_id:
        stmt = stmt.where(OutboxMessage.claim_id == claim_id)
    return [{"claim_id": m.claim_id, "to": m.to_masked, "body": m.body, "at": m.created_at.isoformat()}
            for m in db.scalars(stmt.order_by(OutboxMessage.id.desc()).limit(10))]


@router.get("/meta", tags=["demo"])
def meta():
    return {"demo_mode": config.DEMO_MODE, "cycle": cl.current_cycle(), "seat_alert_pct": config.SEAT_ALERT_PCT,
            "purposes": cr.PURPOSES, "disclaimer": "Prototype with synthetic data. APAAR registry, SMS, DigiLocker and ABC are MOCKED."}
