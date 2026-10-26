"""Claim protocol: every exit is a visible state; seats and APAAR IDs can never be over-allocated."""
import threading
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app import claims as cl
from app.db import SessionLocal
from app.models import Alert, Claim, Credential, SeatException, SeatLedger
from app.security import verify_signature
from tests.conftest import latest_otp


def ledger(db, code_prog, seat="REGULAR"):
    lg = db.scalar(select(SeatLedger).where(SeatLedger.programme_id == code_prog.id, SeatLedger.seat_type == seat))
    db.refresh(lg)
    return lg


def test_happy_path_lock3_and_credential(world):
    c = world.claim("AAA", 0)
    assert c.state == "AWAITING_STUDENT" and c.holds_seat and c.active
    assert ledger(world.db, world.progs["AAA"]).claimed == 1
    otp = latest_otp(world.db, c.id)
    cl.confirm_claim(world.db, c, "OTP", otp, "student")
    assert c.state == "VERIFIED"
    cred = world.db.scalar(select(Credential).where(Credential.claim_id == c.id))
    assert cred and verify_signature(cred.envelope, cred.signature)


def test_second_claim_on_same_apaar_conflicts_and_does_not_take_a_seat(world):
    world.claim("AAA", 1)
    c2 = world.claim("BBB", 1)
    assert c2.state == "CONFLICT" and c2.conflict_with
    assert ledger(world.db, world.progs["BBB"]).claimed == 0
    assert not c2.active


def test_same_institution_duplicate_also_conflicts(world):
    world.claim("AAA", 1)
    assert world.claim("AAA", 1).state == "CONFLICT"


def test_seat_limit_blocks_at_100_percent_and_alerts(world):
    for i in range(3):
        assert world.claim("AAA", i).state == "AWAITING_STUDENT"
    c4 = world.claim("AAA", 3)
    assert c4.state == "SEAT_LIMIT" and "3/3" in c4.reason
    assert ledger(world.db, world.progs["AAA"]).claimed == 3
    kinds = {a.kind for a in world.db.scalars(select(Alert))}
    assert {"ALERT_90", "STOP_100"} <= kinds or "STOP_100" in kinds


def test_alert_fires_at_90_percent(world):
    for i in range(8):
        world.claim("BBB", i)
    assert not world.db.scalars(select(Alert).where(Alert.kind == "ALERT_90")).all()
    world.claim("BBB", 8)  # 9/10 = 90%
    assert world.db.scalars(select(Alert).where(Alert.kind == "ALERT_90")).all()


def test_exception_approval_adds_seat_and_parked_claim_proceeds(world):
    for i in range(3):
        world.claim("AAA", i)
    parked = world.claim("AAA", 3)
    assert parked.state == "SEAT_LIMIT"
    exc = SeatException(institution_id=world.insts["AAA"].id, programme_id=world.progs["AAA"].id, seat_type="REGULAR",
                        category="SUPERNUMERARY", seats=1, justification="Supernumerary seat for scheme students")
    world.db.add(exc)
    world.db.commit()
    with pytest.raises(cl.ClaimError):
        cl.decide_exception(world.db, exc, True, 1, "  ", "rev")  # reason is mandatory
    cl.decide_exception(world.db, exc, True, 1, "Scheme notification attached", "rev")
    world.db.refresh(parked)
    assert parked.state == "AWAITING_STUDENT"
    assert ledger(world.db, world.progs["AAA"]).claimed == 4


def test_swapped_dob_gives_field_level_fix_and_apply_resolves(world):
    from app.models import RegistryRecord
    aid, nm, _, ph = world.people[14]
    dob = date(2005, 3, 9)
    world.db.get(RegistryRecord, aid).dob = dob
    world.db.commit()
    c = world.claim("AAA", 14, dob=date(dob.year, dob.day, dob.month))
    assert c.state == "PENDING_FIX"
    assert [f["field"] for f in c.fixes] == ["dob"] and c.fixes[0]["suggested"] == dob.isoformat()
    assert ledger(world.db, world.progs["AAA"]).claimed == 0
    c = cl.apply_fixes(world.db, c, ["dob"], "reg")
    assert c.state == "AWAITING_STUDENT" and c.dob == dob


def test_no_registry_values_leak_when_match_is_not_probable(world):
    c = world.claim("AAA", 2, student_name="Totally Different Person", dob=date(1999, 1, 1))
    assert c.state == "PENDING_FIX" and not c.fixes  # anti-enumeration


def test_invalid_format_and_unknown_id_are_pending_fix(world):
    assert world.claim("AAA", 0, apaar_id="12345").state == "PENDING_FIX"
    c = world.claim("AAA", 1, apaar_id="999999999999")
    assert c.state == "PENDING_FIX" and "not found" in c.reason


def test_inactive_apaar_id(world):
    from app.models import RegistryRecord
    rec = world.db.get(RegistryRecord, world.people[5][0])
    rec.active = False
    world.db.commit()
    c = world.claim("AAA", 5)
    assert c.state == "PENDING_FIX" and "inactive" in c.reason


def test_registry_outage_is_pending_never_silent_pass_then_retry(world):
    cl.set_setting(world.db, "registry_outage", "1")
    world.db.commit()
    c = world.claim("AAA", 0)
    assert c.state == "PENDING" and not c.holds_seat
    cl.set_setting(world.db, "registry_outage", "0")
    world.db.commit()
    assert cl.retry_pending(world.db)["resolved"] == 1
    world.db.refresh(c)
    assert c.state == "AWAITING_STUDENT"


def test_provisional_id_holds_one_seat_and_resolves_without_double_counting(world):
    aid, nm, dob, ph = world.people[0]
    c = world.claim("AAA", 0, apaar_id=None, provisional_reason="APAAR generation pending")
    assert c.state == "PROVISIONAL" and c.provisional_token.startswith("PRV-") and c.holds_seat
    assert ledger(world.db, world.progs["AAA"]).claimed == 1
    c = cl.update_claim(world.db, c, "reg", apaar_id=aid)
    assert c.state == "AWAITING_STUDENT"
    assert ledger(world.db, world.progs["AAA"]).claimed == 1


def test_missing_apaar_without_reason_is_rejected(world):
    with pytest.raises(cl.ClaimError):
        world.claim("AAA", 0, apaar_id=None)


def test_minor_requires_guardian_number(world):
    kid = date.today().replace(year=date.today().year - 16)
    with pytest.raises(cl.ClaimError, match="guardian"):
        world.claim("AAA", 0, dob=kid)


def test_wrong_otp_attempts_are_limited_and_expiry_enforced(world):
    c = world.claim("AAA", 0)
    for _ in range(5):
        with pytest.raises(cl.ClaimError):
            cl.confirm_claim(world.db, c, "OTP", "000000", "s")
    with pytest.raises(cl.ClaimError, match="Too many"):
        cl.confirm_claim(world.db, c, "OTP", latest_otp(world.db, c.id), "s")
    cl.resend_otp(world.db, c, "s")
    with pytest.raises(cl.ClaimError, match="expired"):
        cl.confirm_claim(world.db, c, "OTP", latest_otp(world.db, c.id), "s", as_of=datetime.utcnow() + timedelta(hours=1))
    cl.confirm_claim(world.db, c, "OTP", latest_otp(world.db, c.id), "s")
    assert c.state == "VERIFIED"


def test_consent_refusal_still_counts_the_seat_and_gets_alt_token(world):
    c = world.claim("AAA", 0)
    cl.refuse_consent(world.db, c, "student")
    assert c.state == "CONSENT_REFUSED" and c.alt_token and c.holds_seat
    assert ledger(world.db, world.progs["AAA"]).claimed == 1


def test_unconfirmed_after_30_days_and_late_confirmation_still_possible(world):
    c = world.claim("AAA", 0)
    assert cl.sweep_unconfirmed(world.db, datetime.utcnow() + timedelta(days=29))["marked_unconfirmed"] == 0
    assert cl.sweep_unconfirmed(world.db, datetime.utcnow() + timedelta(days=31))["marked_unconfirmed"] == 1
    world.db.refresh(c)
    assert c.state == "UNCONFIRMED"
    cl.confirm_claim(world.db, c, "DIGILOCKER", None, "student")
    assert c.state == "VERIFIED"


def test_withdraw_releases_seat_revokes_credential_and_allows_new_claim(world):
    c = world.claim("AAA", 0)
    cl.confirm_claim(world.db, c, "DIGILOCKER", None, "s")
    cl.withdraw_claim(world.db, c, "reg", "Student left")
    assert ledger(world.db, world.progs["AAA"]).claimed == 0
    cred = world.db.scalar(select(Credential).where(Credential.claim_id == c.id))
    assert cred.status == "REVOKED"
    assert world.claim("BBB", 0).state == "AWAITING_STUDENT"  # released student can be admitted elsewhere


def test_idempotency_key_returns_same_claim(world):
    a = cl.create_claim(world.db, world.inp("AAA", 0), "reg", idem_key="k1")
    b = cl.create_claim(world.db, world.inp("AAA", 0), "reg", idem_key="k1")
    assert a.id == b.id
    assert world.db.scalar(select(func.count(Claim.id))) == 1


# ------------------------------------------------------------------ concurrency (atomicity)
def _parallel(fn, n):
    errors, barrier = [], threading.Barrier(n)

    def run(i):
        db = SessionLocal()
        try:
            barrier.wait()
            fn(db, i)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            db.close()

    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return errors


def test_concurrent_claims_on_one_apaar_id_yield_exactly_one_live_claim(world):
    ids = {"AAA": world.insts["AAA"].id, "BBB": world.insts["BBB"].id}

    def go(db, i):
        code = "AAA" if i % 2 == 0 else "BBB"
        cl.create_claim(db, world.inp(code, 7), f"t{i}")

    errs = _parallel(go, 8)
    assert not errs, errs
    db = world.db
    live = db.scalars(select(Claim).where(Claim.active.is_(True))).all()
    assert len(live) == 1
    states = sorted(c.state for c in db.scalars(select(Claim)))
    assert states.count("AWAITING_STUDENT") == 1 and states.count("CONFLICT") == 7
    total_claimed = sum(l.claimed for l in db.scalars(select(SeatLedger).where(SeatLedger.seat_type == "REGULAR")))
    for l in db.scalars(select(SeatLedger)):
        db.refresh(l)
    assert sum(l.claimed for l in db.scalars(select(SeatLedger).where(SeatLedger.seat_type == "REGULAR"))) == 1


def test_concurrent_distinct_students_never_exceed_intake(world):
    def go(db, i):
        cl.create_claim(db, world.inp("AAA", 10 + i), f"t{i}")  # intake is 3

    errs = _parallel(go, 10)
    assert not errs, errs
    db = world.db
    assert len(db.scalars(select(Claim).where(Claim.state == "AWAITING_STUDENT")).all()) == 3
    assert len(db.scalars(select(Claim).where(Claim.state == "SEAT_LIMIT")).all()) == 7
    assert ledger(db, world.progs["AAA"]).claimed == 3
