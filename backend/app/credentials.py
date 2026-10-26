"""Signed credentials with salted-digest selective disclosure (a simplified SD-JWT / W3C VC 2.0 style).

The envelope signed by the platform carries only digests; the student wallet holds the salts+values and
discloses a purpose-bound subset. The prototype stores the wallet attributes server-side (documented limit).
"""
import secrets
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, config
from .models import AccessRevocation, Claim, Credential, Institution, Programme, VerificationEvent, utcnow
from .security import decrypt, public_key_b64, sha256, sign, verify_signature

ISSUER = "APAAR Sentinel (AICTE trust layer, prototype)"

# Purpose -> the only fields a verifier may receive. Everything else stays with the student.
PURPOSES: dict[str, list[str]] = {
    "EMPLOYMENT": ["full_name", "institution", "programme", "enrolment_status"],
    "BANK_LOAN": ["full_name", "institution", "programme", "admission_year", "enrolment_status"],
    "EMBASSY": ["full_name", "dob", "institution", "programme", "enrolment_status"],
    "SCHOLARSHIP": ["full_name", "institution", "programme", "category", "enrolment_status"],
    "ENROLMENT_CHECK": ["enrolment_status"],
}


def _digest(salt: str, field: str, value) -> str:
    return sha256(f"{salt}|{field}|{value}")


def issue(db: Session, claim: Claim) -> Credential:
    existing = db.scalar(select(Credential).where(Credential.claim_id == claim.id))
    if existing:
        existing.status = "VALID"
        return existing
    inst = db.get(Institution, claim.institution_id)
    prog = db.get(Programme, claim.programme_id)
    apaar = decrypt(claim.apaar_enc) or ""
    values = {
        "full_name": claim.student_name, "dob": claim.dob.isoformat(), "apaar_id": apaar,
        "student_id": claim.student_ref, "institution": inst.name, "institution_code": inst.code,
        "institution_state": inst.state, "programme": prog.name, "level": prog.level,
        "admission_year": claim.cycle, "seat_type": claim.seat_type, "category": claim.category or "NA",
        "gender": claim.gender or "NA", "enrolment_status": "ENROLLED",
    }
    attrs = {k: {"value": v, "salt": secrets.token_hex(8)} for k, v in values.items()}
    cid = "CRD-" + secrets.token_hex(6).upper()
    envelope = {"id": cid, "issuer": ISSUER, "issued_at": (claim.verified_at or utcnow()).isoformat(),
                "subject": sha256(f"subject|{claim.apaar_hash}")[:24],
                "digests": {k: _digest(a["salt"], k, a["value"]) for k, a in attrs.items()}}
    cred = Credential(id=cid, claim_id=claim.id, envelope=envelope, signature=sign(envelope), attributes=attrs,
                      qr_token=secrets.token_hex(12), issued_at=utcnow())
    db.add(cred)
    db.flush()
    audit.log(db, "system", "CREDENTIAL_ISSUED", "credential", cid, {"claim": claim.id})
    return cred


def revoke_for_claim(db: Session, claim: Claim):
    cred = db.scalar(select(Credential).where(Credential.claim_id == claim.id))
    if cred and cred.status != "REVOKED":
        cred.status = "REVOKED"
        audit.log(db, "system", "CREDENTIAL_REVOKED", "credential", cred.id, {"claim": claim.id})


def _status(db: Session, cred: Credential) -> str:
    claim = db.get(Claim, cred.claim_id)
    if cred.status == "REVOKED" or claim.state != "VERIFIED":
        return "REVOKED"
    return "VALID"


def _answer(payload: dict) -> dict:
    payload = dict(payload)
    payload["checked_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    payload["nonce"] = secrets.token_hex(6)
    payload["key"] = public_key_b64()
    return {"payload": payload, "signature": sign(payload)}


def public_yes_no(db: Session, cred_id: str, token: Optional[str]) -> dict:
    """QR scan: a signed yes/no with no personal data at all."""
    cred = db.get(Credential, cred_id)
    ok = bool(cred) and token == cred.qr_token
    status = _status(db, cred) if ok else "NOT_FOUND"
    db.add(VerificationEvent(credential_id=cred_id, org="Anonymous QR scan", purpose="QR_YES_NO", fields=[],
                             result=status if ok else "NOT_FOUND"))
    db.commit()
    return _answer({"credential_id": cred_id, "valid": status == "VALID", "status": status,
                    "issuer": ISSUER if ok else None})


def verify_for_org(db: Session, cred_id: str, purpose: str, reason: str, org: str, user_id: int) -> dict:
    if purpose not in PURPOSES:
        raise ValueError("Unknown purpose")
    cred = db.get(Credential, cred_id.strip())
    fields = PURPOSES[purpose]
    if not cred:
        db.add(VerificationEvent(credential_id=cred_id[:24], verifier_user_id=user_id, org=org, purpose=purpose,
                                 reason=reason, fields=[], result="NOT_FOUND"))
        db.commit()
        return _answer({"credential_id": cred_id, "valid": False, "status": "NOT_FOUND", "purpose": purpose})
    if db.scalar(select(AccessRevocation).where(AccessRevocation.credential_id == cred.id, AccessRevocation.org == org)):
        db.add(VerificationEvent(credential_id=cred.id, verifier_user_id=user_id, org=org, purpose=purpose,
                                 reason=reason, fields=[], result="ACCESS_REVOKED"))
        db.commit()
        return _answer({"credential_id": cred.id, "valid": False, "status": "ACCESS_REVOKED", "purpose": purpose,
                        "note": "The student has revoked this organisation's access"})
    status = _status(db, cred)
    disclosed = {f: {"value": cred.attributes[f]["value"], "salt": cred.attributes[f]["salt"]} for f in fields} \
        if status == "VALID" else {}
    db.add(VerificationEvent(credential_id=cred.id, verifier_user_id=user_id, org=org, purpose=purpose, reason=reason,
                             fields=list(disclosed), result=status))
    audit.log(db, f"verifier:{org}", "CREDENTIAL_VERIFIED", "credential", cred.id,
              {"purpose": purpose, "fields": list(disclosed), "result": status})
    db.commit()
    ans = _answer({"credential_id": cred.id, "valid": status == "VALID", "status": status, "purpose": purpose,
                   "org": org, "disclosed": {f: d["value"] for f, d in disclosed.items()},
                   "fields_withheld": len(cred.attributes) - len(disclosed)})
    # selective-disclosure proof: lets the verifier recompute digests against the signed envelope
    ans["credential"] = {"envelope": cred.envelope, "signature": cred.signature,
                         "disclosures": {f: d for f, d in disclosed.items()}}
    return ans


def check_disclosure(envelope: dict, signature: str, disclosures: dict) -> dict:
    """Offline verification helper (also used by tests): signature + digest check for each disclosed field."""
    sig_ok = verify_signature(envelope, signature)
    fields_ok = all(_digest(d["salt"], f, d["value"]) == envelope["digests"].get(f) for f, d in disclosures.items())
    return {"signature_valid": sig_ok, "disclosures_valid": fields_ok}
