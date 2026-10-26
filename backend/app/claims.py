"""The claim protocol: Registry check -> live-claim check -> seat ledger -> student (Lock 3).

Every exit is a visible state. Portal, bulk CSV and API all call create_claim() so they share one code path.
"""
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit, config, matching
from .models import (Alert, Claim, Institution, LIVE_STATES, OutboxMessage, Programme, RegistryRecord,
                     SeatException, SeatLedger, SEAT_TYPES, Setting, utcnow)
from .security import decrypt, encrypt, keyed_hash, sha256


class ClaimError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


class RegistryUnavailable(Exception):
    pass


def current_cycle() -> int:
    return date.today().year


def get_setting(db: Session, key: str, default: str = "") -> str:
    s = db.get(Setting, key)
    return s.value if s else default


def set_setting(db: Session, key: str, value: str):
    s = db.get(Setting, key)
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))


def normalize_apaar(raw: Optional[str]) -> Optional[str]:
    digits = re.sub(r"[\s-]", "", raw or "")
    return digits if re.fullmatch(r"\d{12}", digits) else None


def registry_lookup(db: Session, apaar_id: str) -> Optional[RegistryRecord]:
    """Adapter boundary for the national APAAR registry. Contract: returns a record or None, or raises
    RegistryUnavailable. The prototype backs it with a MOCK table; outages are simulated by a setting."""
    if get_setting(db, "registry_outage") == "1":
        raise RegistryUnavailable()
    return db.get(RegistryRecord, apaar_id)


@dataclass
class ClaimInput:
    institution_id: int
    programme_id: int
    seat_type: str
    student_name: str
    dob: date
    phone: str
    apaar_id: Optional[str] = None
    email: Optional[str] = None
    guardian_phone: Optional[str] = None
    gender: Optional[str] = None
    category: Optional[str] = None
    student_ref: Optional[str] = None
    admitted_on: Optional[date] = None
    provisional_reason: Optional[str] = None


def mask(apaar: str) -> str:
    return f"XXXX XXXX {apaar[-4:]}"


def _ledger(db: Session, programme_id: int, seat_type: str, cycle: int) -> Optional[SeatLedger]:
    return db.scalar(select(SeatLedger).where(SeatLedger.programme_id == programme_id,
                                              SeatLedger.seat_type == seat_type, SeatLedger.cycle == cycle))


def age_on(dob: date, on: date) -> int:
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


# ------------------------------------------------------------------ create
def create_claim(db: Session, inp: ClaimInput, actor: str, idem_key: Optional[str] = None,
                 as_of: Optional[datetime] = None, historic: bool = False) -> Claim:
    if idem_key:
        prior = db.scalar(select(Claim).where(Claim.institution_id == inp.institution_id,
                                              Claim.idempotency_key == idem_key))
        if prior:
            return prior
    prog = db.get(Programme, inp.programme_id)
    if not prog or prog.institution_id != inp.institution_id:
        raise ClaimError(404, "Programme not found for this institution")
    if inp.seat_type not in SEAT_TYPES:
        raise ClaimError(422, f"seat_type must be one of {', '.join(SEAT_TYPES)}")
    cycle = current_cycle()
    ledger = _ledger(db, inp.programme_id, inp.seat_type, cycle)
    if not ledger:
        raise ClaimError(422, f"No {inp.seat_type} seats are approved for this programme in {cycle}")
    name = re.sub(r"\s+", " ", (inp.student_name or "").strip())
    if len(name) < 3:
        raise ClaimError(422, "Student name is required")
    if inp.dob >= date.today() or inp.dob.year < 1950:
        raise ClaimError(422, "Date of birth is not plausible")
    phone = re.sub(r"[^\d]", "", inp.phone or "")[-10:]
    if len(phone) != 10:
        raise ClaimError(422, "A 10-digit mobile number is required for Lock 3 confirmation")
    now = as_of or utcnow()
    admitted = inp.admitted_on or now.date()
    minor = age_on(inp.dob, admitted) < 18
    gphone = re.sub(r"[^\d]", "", inp.guardian_phone or "")[-10:] if inp.guardian_phone else None
    if minor and (not gphone or len(gphone) != 10):
        raise ClaimError(422, "Student is under 18: a guardian's mobile number is required for Lock 3")
    inst = db.get(Institution, inp.institution_id)
    seq = db.scalar(select(func.count(Claim.id)).where(Claim.institution_id == inst.id)) + 1
    apaar = normalize_apaar(inp.apaar_id) if inp.apaar_id else None
    c = Claim(
        institution_id=inst.id, programme_id=prog.id, seat_type=inp.seat_type, cycle=cycle,
        student_ref=inp.student_ref or f"{inst.code}/{cycle}/{seq:04d}", student_name=name, dob=inp.dob,
        gender=inp.gender, category=inp.category, phone_enc=encrypt(phone), phone_hash=keyed_hash(phone),
        email_enc=encrypt(inp.email) if inp.email else None,
        email_hash=keyed_hash(inp.email.lower()) if inp.email else None,
        guardian_phone_enc=encrypt(gphone) if gphone else None, state="PENDING_FIX",
        admitted_on=admitted, created_at=now, idempotency_key=idem_key, historic=historic)
    if inp.apaar_id and inp.apaar_id.strip():
        c.apaar_enc = encrypt(inp.apaar_id.strip())  # raw (even if malformed) so it can be corrected
        c.apaar_masked = mask(apaar) if apaar else "INVALID FORMAT"
    else:
        if not (inp.provisional_reason or "").strip():
            raise ClaimError(422, "No APAAR ID: give a reason to request a provisional ID (deadline applies)")
        c.provisional_token = "PRV-" + secrets.token_hex(5).upper()
        c.provisional_deadline = now + timedelta(days=config.PROVISIONAL_DEADLINE_DAYS)
        c.reason = f"Provisional ID requested: {inp.provisional_reason.strip()}"
        c.apaar_masked = c.provisional_token
    db.add(c)
    db.flush()
    audit.log(db, actor, "CLAIM_SUBMITTED", "claim", c.id, {"institution": inst.code, "programme": prog.name,
                                                            "seat_type": c.seat_type, "provisional": bool(c.provisional_token)})
    db.commit()  # row is durable before the protocol runs, so every outcome is visible
    return run_protocol(db, c, actor, as_of)


# ------------------------------------------------------------------ protocol
def _take_seat(db: Session, ledger_id: int) -> bool:
    res = db.execute(update(SeatLedger).where(SeatLedger.id == ledger_id,
                                              SeatLedger.claimed < SeatLedger.intake + SeatLedger.extra)
                     .values(claimed=SeatLedger.claimed + 1))
    return res.rowcount == 1


def release_seat(db: Session, claim: Claim):
    if not claim.holds_seat:
        return
    ledger = _ledger(db, claim.programme_id, claim.seat_type, claim.cycle)
    if ledger:
        db.execute(update(SeatLedger).where(SeatLedger.id == ledger.id, SeatLedger.claimed > 0)
                   .values(claimed=SeatLedger.claimed - 1))
        db.refresh(ledger)
        if ledger.claimed * 100 < config.SEAT_ALERT_PCT * (ledger.intake + ledger.extra):
            ledger.alert_sent = False
    claim.holds_seat = False


def _seat_alerts(db: Session, claim: Claim):
    ledger = _ledger(db, claim.programme_id, claim.seat_type, claim.cycle)
    db.refresh(ledger)
    cap = ledger.intake + ledger.extra
    prog = db.get(Programme, claim.programme_id)
    label = f"{prog.name} / {claim.seat_type}"
    if ledger.claimed >= cap:
        db.add(Alert(institution_id=claim.institution_id, programme_id=prog.id, seat_type=claim.seat_type,
                     kind="STOP_100", message=f"{label}: seat ledger full ({ledger.claimed}/{cap}); further claims return SEAT_LIMIT"))
    elif ledger.claimed * 100 >= config.SEAT_ALERT_PCT * cap and not ledger.alert_sent:
        ledger.alert_sent = True
        db.add(Alert(institution_id=claim.institution_id, programme_id=prog.id, seat_type=claim.seat_type,
                     kind="ALERT_90", message=f"{label}: {ledger.claimed}/{cap} seats claimed (>= {config.SEAT_ALERT_PCT}%)"))


def _set(c: Claim, state: str, reason: str):
    c.state, c.reason = state, reason
    c.active = state in LIVE_STATES


def run_protocol(db: Session, c: Claim, actor: str = "system", as_of: Optional[datetime] = None) -> Claim:
    now = as_of or utcnow()
    c.fixes, c.conflict_with, c.match_detail, c.match_prob = None, None, None, None
    cid = c.id
    try:
        _protocol(db, c, actor, now)
        audit.log(db, actor, "CLAIM_STATE", "claim", c.id, {"state": c.state, "reason": c.reason})
        db.commit()
    except IntegrityError:
        # Lost a race on the unique live-claim index: the DB refused the second live claim.
        db.rollback()
        c = db.get(Claim, cid)
        c.holds_seat, c.apaar_hash = False, None
        _set(c, "CONFLICT", "APAAR ID already holds a live claim (concurrent claim won)")
        audit.log(db, actor, "CLAIM_STATE", "claim", c.id, {"state": c.state, "reason": c.reason})
        db.commit()
    return c


def _protocol(db: Session, c: Claim, actor: str, now: datetime):
    ledger = _ledger(db, c.programme_id, c.seat_type, c.cycle)
    raw = decrypt(c.apaar_enc)
    # ---- provisional path: no ID yet. Seat still counted; resolve before the deadline.
    if not raw:
        if not c.holds_seat:
            if not _take_seat(db, ledger.id):
                _set(c, "SEAT_LIMIT", _seat_msg(db, c, ledger))
                return
            c.holds_seat = True
            _seat_alerts(db, c)
        _set(c, "PROVISIONAL", f"Provisional ID {c.provisional_token}; link a real APAAR ID by "
                                f"{c.provisional_deadline:%Y-%m-%d}")
        return
    # ---- Check 1: registry
    apaar = normalize_apaar(raw)
    if not apaar:
        _set(c, "PENDING_FIX", "APAAR ID must be exactly 12 digits")
        c.apaar_masked = "INVALID FORMAT"
        return
    c.apaar_masked = mask(apaar)
    try:
        rec = registry_lookup(db, apaar)
    except RegistryUnavailable:
        c.retry_count += 1
        _set(c, "PENDING", "APAAR registry unavailable. Not passed silently; it will be retried automatically")
        return
    if rec is None:
        _set(c, "PENDING_FIX", "APAAR ID not found in the registry; check the number or request a provisional ID")
        return
    if not rec.active:
        _set(c, "PENDING_FIX", "APAAR ID is inactive/deactivated in the registry")
        return
    m = matching.match(c.student_name, c.dob, rec.full_name, rec.dob)
    c.match_prob, c.match_detail = m["prob"], {k: v for k, v in m.items() if k != "fixes"}
    if not m["passed"]:
        c.fixes = m["fixes"]
        if m["probable"]:
            _set(c, "PENDING_FIX", f"Probable match ({min(m['prob'], 0.999) * 100:.1f}%) but fields differ: "
                                   + ", ".join(f["field"] for f in m["fixes"]) + " need correction")
        else:
            _set(c, "PENDING_FIX", "Name/date of birth do not match the registry record for this APAAR ID")
        return
    # ---- Check 2: one live claim per APAAR ID
    h = keyed_hash(apaar)
    other = db.scalar(select(Claim).where(Claim.apaar_hash == h, Claim.active.is_(True), Claim.id != c.id))
    if other:
        c.conflict_with = other.id
        same = other.institution_id == c.institution_id
        _set(c, "CONFLICT", "This APAAR ID already holds a live claim " +
             ("at your institution" if same else "at another institution") + "; the student must be released first")
        return
    # ---- Check 3: seat ledger (conditional UPDATE; never over-allocates)
    if not c.holds_seat:
        if not _take_seat(db, ledger.id):
            _set(c, "SEAT_LIMIT", _seat_msg(db, c, ledger))
            return
        c.holds_seat = True
    c.apaar_hash = h
    c.active = True
    db.flush()  # unique partial index on apaar_hash is the final arbiter
    _seat_alerts(db, c)
    # ---- Lock 3: student confirmation
    _issue_otp(db, c, now)
    _set(c, "AWAITING_STUDENT", "Seat held. Waiting for the student to confirm (OTP or DigiLocker)")


def _seat_msg(db: Session, c: Claim, ledger: SeatLedger) -> str:
    db.refresh(ledger)
    return (f"Seat ledger full for {c.seat_type}: {ledger.claimed}/{ledger.intake + ledger.extra}. "
            "Request an exception from AICTE if this is a dual-programme, transfer or supernumerary case")


# ------------------------------------------------------------------ Lock 3
def _issue_otp(db: Session, c: Claim, now: datetime):
    otp = f"{secrets.randbelow(10**6):06d}"
    c.otp_hash = sha256(f"{c.id}:{otp}")
    c.otp_expires = now + timedelta(minutes=config.OTP_TTL_MINUTES)
    c.otp_attempts = 0
    to = decrypt(c.guardian_phone_enc) or decrypt(c.phone_enc) or ""
    inst = db.get(Institution, c.institution_id)
    who = "guardian " if c.guardian_phone_enc else ""
    db.add(OutboxMessage(claim_id=c.id, to_masked=f"XXXXXX{to[-4:]}", created_at=now,
                         body=f"APAAR Sentinel: {who}OTP {otp} confirms admission of {c.student_name} at {inst.name}. "
                              f"Valid {config.OTP_TTL_MINUTES} min. Do not share."))


def resend_otp(db: Session, c: Claim, actor: str):
    if c.state not in ("AWAITING_STUDENT", "UNCONFIRMED"):
        raise ClaimError(409, "No confirmation is pending for this claim")
    _issue_otp(db, c, utcnow())
    audit.log(db, actor, "OTP_RESENT", "claim", c.id)
    db.commit()


def confirm_claim(db: Session, c: Claim, method: str, otp: Optional[str], actor: str, as_of: Optional[datetime] = None):
    from . import credentials  # local import: credentials depends on models only
    now = as_of or utcnow()
    if c.state not in ("AWAITING_STUDENT", "UNCONFIRMED"):
        raise ClaimError(409, f"Claim is {c.state}; nothing to confirm")
    method = method.upper()
    if method == "OTP":
        if c.otp_attempts >= config.OTP_MAX_ATTEMPTS:
            raise ClaimError(429, "Too many wrong OTP attempts; request a new OTP")
        if not c.otp_hash or not c.otp_expires or now > c.otp_expires:
            raise ClaimError(410, "OTP expired; request a new OTP")
        if sha256(f"{c.id}:{(otp or '').strip()}") != c.otp_hash:
            c.otp_attempts += 1
            audit.log(db, actor, "OTP_FAILED", "claim", c.id, {"attempts": c.otp_attempts})
            db.commit()
            raise ClaimError(400, "Incorrect OTP")
    elif method != "DIGILOCKER":  # DigiLocker consent is MOCKED: accepted for the authenticated owner of the APAAR ID
        raise ClaimError(422, "method must be OTP or DIGILOCKER")
    c.state, c.reason, c.confirm_method, c.verified_at = "VERIFIED", f"Confirmed by student via {method}", method, now
    c.otp_hash = None
    credentials.issue(db, c)
    audit.log(db, actor, "LOCK3_CONFIRMED", "claim", c.id, {"method": method})
    db.commit()


def refuse_consent(db: Session, c: Claim, actor: str):
    if c.state not in ("AWAITING_STUDENT", "UNCONFIRMED"):
        raise ClaimError(409, f"Claim is {c.state}; consent cannot be refused now")
    c.state = "CONSENT_REFUSED"
    c.alt_token = "ALT-" + secrets.token_hex(5).upper()
    c.reason = "Student declined APAAR consent. Counted against the seat; alternate token issued; no service denied"
    c.otp_hash = None
    audit.log(db, actor, "CONSENT_REFUSED", "claim", c.id)
    db.commit()


def withdraw_claim(db: Session, c: Claim, actor: str, reason: str):
    from . import credentials
    if c.state in ("WITHDRAWN",):
        raise ClaimError(409, "Already withdrawn")
    if not reason.strip():
        raise ClaimError(422, "A reason is required")
    release_seat(db, c)
    c.active = False
    c.state, c.reason = "WITHDRAWN", f"Withdrawn: {reason.strip()}"
    credentials.revoke_for_claim(db, c)
    audit.log(db, actor, "CLAIM_WITHDRAWN", "claim", c.id, {"reason": reason})
    db.commit()


# ------------------------------------------------------------------ maintenance & fixes
def apply_fixes(db: Session, c: Claim, fields: list[str], actor: str) -> Claim:
    if c.state != "PENDING_FIX" or not c.fixes:
        raise ClaimError(409, "No suggested fixes to apply")
    applied = []
    for f in c.fixes:
        if f["field"] in fields:
            if f["field"] == "dob":
                c.dob = date.fromisoformat(f["suggested"])
            elif f["field"] == "name":
                c.student_name = f["suggested"]
            applied.append(f["field"])
    if not applied:
        raise ClaimError(422, "None of the requested fields has a suggested fix")
    audit.log(db, actor, "FIX_APPLIED", "claim", c.id, {"fields": applied})
    db.commit()
    return run_protocol(db, c, actor)


def update_claim(db: Session, c: Claim, actor: str, apaar_id: Optional[str] = None, student_name: Optional[str] = None,
                 dob: Optional[date] = None) -> Claim:
    if c.state not in ("PENDING_FIX", "PENDING", "PROVISIONAL", "CONFLICT", "SEAT_LIMIT"):
        raise ClaimError(409, f"A claim in state {c.state} cannot be edited")
    changed = []
    if apaar_id is not None and apaar_id.strip():
        c.apaar_enc = encrypt(apaar_id.strip())
        changed.append("apaar_id")
    if student_name:
        c.student_name = student_name.strip()
        changed.append("student_name")
    if dob:
        c.dob = dob
        changed.append("dob")
    audit.log(db, actor, "CLAIM_EDITED", "claim", c.id, {"fields": changed})
    db.commit()
    return run_protocol(db, c, actor)


def retry_pending(db: Session) -> dict:
    ids = db.scalars(select(Claim.id).where(Claim.state.in_(("PENDING", "SEAT_LIMIT")))).all()
    out = {"retried": 0, "resolved": 0}
    for cid in ids:
        c = db.get(Claim, cid)
        before = c.state
        run_protocol(db, c, "system:retry")
        out["retried"] += 1
        out["resolved"] += before != c.state
    return out


def sweep_unconfirmed(db: Session, as_of: Optional[datetime] = None) -> dict:
    now = as_of or utcnow()
    cutoff = now - timedelta(days=config.UNCONFIRMED_AFTER_DAYS)
    rows = db.scalars(select(Claim).where(Claim.state == "AWAITING_STUDENT", Claim.created_at <= cutoff)).all()
    for c in rows:
        c.state, c.reason = "UNCONFIRMED", f"No student response in {config.UNCONFIRMED_AFTER_DAYS} days; lowers Trust Index"
        audit.log(db, "system:sweep", "CLAIM_UNCONFIRMED", "claim", c.id)
    overdue = db.scalars(select(Claim).where(Claim.state == "PROVISIONAL", Claim.provisional_deadline < now)).all()
    db.commit()
    return {"marked_unconfirmed": len(rows), "provisional_overdue": len(overdue)}


# ------------------------------------------------------------------ exceptions
def decide_exception(db: Session, exc: SeatException, approve: bool, reviewer_id: int, reason: str, actor: str):
    if exc.state != "PENDING":
        raise ClaimError(409, "Exception already decided")
    if not reason.strip():
        raise ClaimError(422, "A reviewer reason is required")
    exc.state = "APPROVED" if approve else "REJECTED"
    exc.reviewer_id, exc.reviewer_reason, exc.decided_at = reviewer_id, reason.strip(), utcnow()
    if approve:
        ledger = _ledger(db, exc.programme_id, exc.seat_type, current_cycle())
        ledger.extra += exc.seats
        ledger.alert_sent = False
    audit.log(db, actor, "EXCEPTION_" + exc.state, "exception", exc.id,
              {"seats": exc.seats, "category": exc.category, "reason": reason})
    db.commit()
    if approve:
        retry_pending(db)  # claims parked in SEAT_LIMIT can now proceed
