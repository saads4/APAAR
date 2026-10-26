"""Tamper-evident audit log (hash chain)."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog
from .security import canonical, sha256

GENESIS = "0" * 64


def _digest(prev: str, ts, actor, action, entity, entity_id, detail) -> str:
    return sha256(prev + canonical([ts, actor, action, entity, entity_id, detail]))


def log(db: Session, actor: str, action: str, entity: str, entity_id, detail: dict | None = None) -> AuditLog:
    last = db.scalar(select(AuditLog).order_by(AuditLog.id.desc()).limit(1))
    prev = last.hash if last else GENESIS
    ts = datetime.utcnow().isoformat(timespec="microseconds")
    detail = detail or {}
    row = AuditLog(ts=ts, actor=actor, action=action, entity=entity, entity_id=str(entity_id), detail=detail,
                   prev_hash=prev, hash=_digest(prev, ts, actor, action, entity, str(entity_id), detail))
    db.add(row)
    db.flush()
    return row


def verify_chain(db: Session) -> dict:
    prev = GENESIS
    count = 0
    for row in db.scalars(select(AuditLog).order_by(AuditLog.id)):
        expect = _digest(prev, row.ts, row.actor, row.action, row.entity, row.entity_id, row.detail)
        if row.prev_hash != prev or row.hash != expect:
            return {"ok": False, "checked": count, "first_bad_id": row.id,
                    "message": f"Audit row {row.id} was altered or the chain was broken"}
        prev = row.hash
        count += 1
    return {"ok": True, "checked": count, "head": prev, "message": "Chain intact"}
