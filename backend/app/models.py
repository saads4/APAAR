"""Database schema. SQLite for the demo, PostgreSQL in docker-compose (same models)."""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer,
                        String, Text, UniqueConstraint, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.utcnow()


# Claim states that hold a seat and count as a "live claim" on the APAAR ID.
LIVE_STATES = ("AWAITING_STUDENT", "VERIFIED", "UNCONFIRMED", "CONSENT_REFUSED", "PROVISIONAL")
# Visible exits of the claim protocol.
ALL_STATES = ("PENDING_FIX", "PENDING", "CONFLICT", "SEAT_LIMIT", "WITHDRAWN") + LIVE_STATES
SEAT_TYPES = ("REGULAR", "EWS", "LATERAL", "SUPERNUMERARY")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(24))  # AICTE_ADMIN|AICTE_REVIEWER|INSTITUTION|STUDENT|VERIFIER
    institution_id: Mapped[Optional[int]] = mapped_column(ForeignKey("institutions.id"), nullable=True)
    apaar_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    org: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)  # verifier organisation
    mfa_secret: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Institution(Base):
    __tablename__ = "institutions"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(80))
    district: Mapped[str] = mapped_column(String(80))
    programmes: Mapped[list["Programme"]] = relationship(back_populates="institution")


class Programme(Base):
    __tablename__ = "programmes"
    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    level: Mapped[str] = mapped_column(String(24))  # UG|PG|DIPLOMA
    institution: Mapped[Institution] = relationship(back_populates="programmes")
    seats: Mapped[list["SeatLedger"]] = relationship(back_populates="programme")


class SeatLedger(Base):
    """One row per programme and seat type; `claimed` only changes through a conditional UPDATE."""
    __tablename__ = "seat_ledger"
    __table_args__ = (UniqueConstraint("programme_id", "seat_type", "cycle"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    programme_id: Mapped[int] = mapped_column(ForeignKey("programmes.id"), index=True)
    seat_type: Mapped[str] = mapped_column(String(16))
    cycle: Mapped[int] = mapped_column(Integer)  # admission year
    intake: Mapped[int] = mapped_column(Integer)  # AICTE-approved intake
    extra: Mapped[int] = mapped_column(Integer, default=0)  # reviewer-approved exception seats
    claimed: Mapped[int] = mapped_column(Integer, default=0)
    alert_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    programme: Mapped[Programme] = relationship(back_populates="seats")


class RegistryRecord(Base):
    """MOCK of the national APAAR registry (the real validation API is not public)."""
    __tablename__ = "registry_records"
    apaar_id: Mapped[str] = mapped_column(String(12), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(160))
    dob: Mapped[date] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    abc_credits: Mapped[int] = mapped_column(Integer, default=0)  # mock Academic Bank of Credits balance


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))


class Claim(Base):
    __tablename__ = "claims"
    __table_args__ = (
        # THE atomic gate: one live claim per APAAR ID, enforced by the database itself.
        Index("uq_live_apaar", "apaar_hash", unique=True,
              sqlite_where=text("active = 1"), postgresql_where=text("active")),
        Index("ix_claim_inst_state", "institution_id", "state"),
        UniqueConstraint("institution_id", "idempotency_key", name="uq_idem"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    programme_id: Mapped[int] = mapped_column(ForeignKey("programmes.id"), index=True)
    seat_type: Mapped[str] = mapped_column(String(16))
    cycle: Mapped[int] = mapped_column(Integer)
    student_ref: Mapped[str] = mapped_column(String(40))  # institution's enrolment number
    apaar_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    apaar_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Fernet-encrypted raw ID (for retries)
    apaar_masked: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    provisional_token: Mapped[Optional[str]] = mapped_column(String(24), nullable=True, unique=True)
    provisional_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    student_name: Mapped[str] = mapped_column(String(160))
    dob: Mapped[date] = mapped_column(Date)
    gender: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # GEN|OBC|SC|ST|EWS
    phone_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    email_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    email_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    guardian_phone_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(20), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False)  # live claim flag (partial unique index)
    holds_seat: Mapped[bool] = mapped_column(Boolean, default=False)
    historic: Mapped[bool] = mapped_column(Boolean, default=False)  # imported prior-cycle record
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # human-readable state explanation
    match_prob: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    match_detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    fixes: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    conflict_with: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    otp_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    otp_expires: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    otp_attempts: Mapped[int] = mapped_column(Integer, default=0)
    confirm_method: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    alt_token: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    exception_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    admitted_on: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    institution: Mapped[Institution] = relationship()
    programme: Mapped[Programme] = relationship()


class OutboxMessage(Base):
    """MOCK SMS gateway. In DEMO_MODE the OTP text is visible to the demo UI."""
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"), index=True)
    to_masked: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SeatException(Base):
    __tablename__ = "seat_exceptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    programme_id: Mapped[int] = mapped_column(ForeignKey("programmes.id"))
    seat_type: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(24))  # DUAL_PROGRAMME|TRANSFER|SUPERNUMERARY|OTHER
    seats: Mapped[int] = mapped_column(Integer)
    justification: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(12), default="PENDING")
    reviewer_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reviewer_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    programme_id: Mapped[int] = mapped_column(Integer)
    seat_type: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(12))  # ALERT_90|STOP_100
    message: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"), unique=True)
    envelope: Mapped[dict] = mapped_column(JSON)  # signed credential (salted field digests)
    signature: Mapped[str] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column(JSON)  # {field: {"value":..., "salt":...}} held for the student wallet
    status: Mapped[str] = mapped_column(String(12), default="VALID")  # VALID|REVOKED
    qr_token: Mapped[str] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VerificationEvent(Base):
    __tablename__ = "verification_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    credential_id: Mapped[str] = mapped_column(String(24), index=True)
    verifier_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    org: Mapped[str] = mapped_column(String(160))
    purpose: Mapped[str] = mapped_column(String(24))
    reason: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    fields: Mapped[list] = mapped_column(JSON)
    result: Mapped[str] = mapped_column(String(24))  # VALID|REVOKED|ACCESS_REVOKED|NOT_FOUND
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AccessRevocation(Base):
    """Student revokes a verifier organisation's access to one credential."""
    __tablename__ = "access_revocations"
    __table_args__ = (UniqueConstraint("credential_id", "org"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    credential_id: Mapped[str] = mapped_column(String(24), index=True)
    org: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    """Hash-chained, append-only log: each row commits to the previous row's hash."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(160))
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(40))
    detail: Mapped[dict] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class CohortRoot(Base):
    __tablename__ = "cohort_roots"
    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    cycle: Mapped[int] = mapped_column(Integer)
    root: Mapped[str] = mapped_column(String(64))
    leaf_count: Mapped[int] = mapped_column(Integer)
    signature: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Lead(Base):
    """An anomaly lead: evidence for a human reviewer, never a verdict."""
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("institution_id", "signature"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    layer: Mapped[str] = mapped_column(String(20))  # RULE|PEER|ISOLATION|GRAPH|LIVENESS
    signature: Mapped[str] = mapped_column(String(120))
    severity: Mapped[str] = mapped_column(String(8))  # LOW|MEDIUM|HIGH
    title: Mapped[str] = mapped_column(String(200))
    baseline: Mapped[str] = mapped_column(String(200))
    deviation: Mapped[str] = mapped_column(String(200))
    evidence: Mapped[dict] = mapped_column(JSON)
    action: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), default="OPEN")  # OPEN|CONFIRMED|DISMISSED|ESCALATED
    reason_code: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    reviewer_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewer_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class BulkJob(Base):
    __tablename__ = "bulk_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"))
    filename: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), default="QUEUED")  # QUEUED|RUNNING|DONE|FAILED
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    results: Mapped[list] = mapped_column(JSON, default=list)
    payload: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
