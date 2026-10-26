"""Runs the real seed and checks the intelligence layers find the planted anomalies (and not false alarms)."""
from sqlalchemy import select

from app import analytics, claims as cl, intelligence
from app.db import Base, SessionLocal, engine
from app.models import Claim, Lead, SeatLedger
from app.seed import seed


def test_seed_then_intelligence_surfaces_planted_anomalies():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    seed(verbose=False)
    db = SessionLocal()
    cycle = cl.current_cycle()
    ti = {r["code"]: r for r in intelligence.trust_index(db, cycle)}
    assert ti["RCE"]["band"] == "REVIEW" and ti["RCE"]["score"] < 60  # ghost-student profile
    assert ti["NFI"]["band"] in ("WATCH", "REVIEW")  # burst + past excess
    assert ti["LTC"]["band"] in ("WATCH", "REVIEW")  # messy data + double-enrolment attempts
    healthy = [ti[c]["score"] for c in ("SPI", "GCT", "KIT", "TNE", "PEM", "BIM", "HPT", "OSE", "DIT")]
    assert min(healthy) >= 85, healthy  # no false alarms on well-behaved institutions
    # every point of penalty is explained and linked to evidence
    for comp in ti["RCE"]["components"]:
        if comp["penalty"] >= 10:
            assert comp["lead_ids"], comp
    layers = {(l.institution_id, l.layer) for l in db.scalars(select(Lead))}
    codes = {i.id: i.code for i in db.execute(select(Claim.institution_id, Claim.institution_id)).all()} if False else None
    by_layer = {l.layer for l in db.scalars(select(Lead))}
    assert {"RULE", "PEER", "GRAPH", "LIVENESS", "ISOLATION"} <= by_layer
    # slide-3 prototype view: B.Tech CSE regular, intake 60, 59 claimed after 18 days, alert line 54
    from app.models import Institution, Programme
    dit = db.scalar(select(Institution).where(Institution.code == "DIT"))
    prog = db.scalar(select(Programme).where(Programme.institution_id == dit.id, Programme.name == "B.Tech CSE"))
    tl = analytics.timeline(db, prog.id, "REGULAR")
    assert tl["intake"] == 60 and tl["alert_at"] == 54 and tl["cumulative"][-1] == 59 and tl["cumulative"][:5] == [5, 11, 17, 22, 27]
    # simulator: a stricter rule blocks more than the status quo, and reports false-positive cost
    base = analytics.simulate(db, 1.0, 100, True)
    strict = analytics.simulate(db, 0.9, 100, True)
    assert strict["would_block"] > base["would_block"] > 0 and base["blocked_duplicate"] >= 5
    # re-running intelligence is idempotent (no duplicate leads)
    n = db.query(Lead).count()
    intelligence.run(db, cycle)
    assert db.query(Lead).count() == n
    db.close()


def test_demo_flow_on_seeded_data_conflict_and_seat_limit():
    """The scripted live demo from the README works on the seeded database."""
    db = SessionLocal()
    from app.models import Institution, Programme
    from datetime import date
    dit = db.scalar(select(Institution).where(Institution.code == "DIT"))
    prog = db.scalar(select(Programme).where(Programme.institution_id == dit.id, Programme.name == "B.Tech CSE"))

    def go(aid, name, dob, phone, **kw):
        return cl.create_claim(db, cl.ClaimInput(institution_id=dit.id, programme_id=prog.id, seat_type="REGULAR", student_name=name,
                                                 dob=dob, phone=phone, apaar_id=aid, **kw), "registrar@dit.demo")

    assert go("110000000004", "Karan Malhotra", date(2006, 1, 20), "9222222221").state == "CONFLICT"  # live at Lakeview
    assert go("110000000010", "Neha Kulkarni", date(2006, 5, 9), "9222222222").state == "AWAITING_STUDENT"  # seat 60/60
    assert go("110000000011", "Imran Shaikh", date(2006, 8, 22), "9222222223").state == "SEAT_LIMIT"  # claim 61
    lg = db.scalar(select(SeatLedger).where(SeatLedger.programme_id == prog.id, SeatLedger.seat_type == "REGULAR", SeatLedger.cycle == cl.current_cycle()))
    assert lg.claimed == 60
    db.close()
