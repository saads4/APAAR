"""Dashboards, seat timeline, policy simulator, suppressed reports, cohort roots, rule-based NL query."""
import csv
import io
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config, intelligence, merkle
from .claims import current_cycle
from .models import (Alert, Claim, CohortRoot, Institution, Lead, Programme, RegistryRecord, SeatException,
                      SeatLedger, utcnow)
from .security import decrypt, sign, verify_signature


# ---------------------------------------------------------------- ledger
def ledger_rows(db: Session, institution_id: Optional[int] = None, cycle: Optional[int] = None) -> list[dict]:
    cycle = cycle or current_cycle()
    q = (select(SeatLedger, Programme, Institution).join(Programme, Programme.id == SeatLedger.programme_id)
         .join(Institution, Institution.id == Programme.institution_id).where(SeatLedger.cycle == cycle))
    if institution_id:
        q = q.where(Institution.id == institution_id)
    rows = []
    for lg, p, i in db.execute(q.order_by(Institution.name, Programme.name, SeatLedger.seat_type)):
        cap = lg.intake + lg.extra
        rows.append({"ledger_id": lg.id, "institution_id": i.id, "institution": i.name, "programme_id": p.id,
                     "programme": p.name, "seat_type": lg.seat_type, "intake": lg.intake, "extra": lg.extra,
                     "cap": cap, "claimed": lg.claimed, "pct": round(100 * lg.claimed / cap, 1) if cap else 0,
                     "status": "FULL" if lg.claimed >= cap else "ALERT" if lg.claimed * 100 >= config.SEAT_ALERT_PCT * cap else "OK"})
    return rows


def timeline(db: Session, programme_id: int, seat_type: str, cycle: Optional[int] = None) -> dict:
    cycle = cycle or current_cycle()
    lg = db.scalar(select(SeatLedger).where(SeatLedger.programme_id == programme_id, SeatLedger.seat_type == seat_type,
                                            SeatLedger.cycle == cycle))
    claims = db.scalars(select(Claim).where(Claim.programme_id == programme_id, Claim.seat_type == seat_type,
                                            Claim.cycle == cycle, Claim.holds_seat.is_(True)).order_by(Claim.admitted_on)).all()
    blocked = db.scalars(select(Claim).where(Claim.programme_id == programme_id, Claim.seat_type == seat_type,
                                             Claim.cycle == cycle, Claim.state == "SEAT_LIMIT")).all()
    cap = (lg.intake + lg.extra) if lg else 0
    if not claims:
        return {"days": [], "cumulative": [], "intake": cap, "alert_at": -(-cap * config.SEAT_ALERT_PCT // 100),
                "stop_at": cap, "blocked": len(blocked)}
    start = claims[0].admitted_on
    last = max(c.admitted_on for c in claims)
    per = Counter(c.admitted_on for c in claims)
    days, cum, run = [], [], 0
    d = start
    while d <= last:
        run += per.get(d, 0)
        days.append((d - start).days + 1)
        cum.append(run)
        d += timedelta(days=1)
    return {"days": days, "dates": [(start + timedelta(days=i)).isoformat() for i in range(len(days))],
            "cumulative": cum, "intake": cap, "alert_at": -(-cap * config.SEAT_ALERT_PCT // 100), "stop_at": cap,
            "blocked": len(blocked)}


# ---------------------------------------------------------------- dashboards
def national_dashboard(db: Session) -> dict:
    cycle = current_cycle()
    states = dict(db.execute(select(Claim.state, func.count()).where(Claim.cycle == cycle, Claim.historic.is_(False))
                             .group_by(Claim.state)).all())
    rows = ledger_rows(db, cycle=cycle)
    cap = sum(r["cap"] for r in rows)
    claimed = sum(r["claimed"] for r in rows)
    alerts = db.scalars(select(Alert).order_by(Alert.id.desc()).limit(8)).all()
    return {
        "cycle": cycle, "claims_by_state": states, "institutions": db.scalar(select(func.count(Institution.id))),
        "seats_cap": cap, "seats_claimed": claimed, "utilisation_pct": round(100 * claimed / cap, 1) if cap else 0,
        "prevented": {"double_enrolments_blocked": states.get("CONFLICT", 0),
                      "excess_admissions_blocked": states.get("SEAT_LIMIT", 0)},
        "ledger_alerts": sum(r["status"] == "ALERT" for r in rows), "ledger_full": sum(r["status"] == "FULL" for r in rows),
        "open_leads": db.scalar(select(func.count(Lead.id)).where(Lead.status == "OPEN")),
        "pending_exceptions": db.scalar(select(func.count(SeatException.id)).where(SeatException.state == "PENDING")),
        "recent_alerts": [{"id": a.id, "kind": a.kind, "message": a.message, "at": a.created_at.isoformat()} for a in alerts],
        "registry_outage": False,
    }


# ---------------------------------------------------------------- policy simulator
def simulate(db: Session, intake_multiplier: float = 1.0, stop_pct: int = 100, enforce_unique: bool = True,
             cycle: Optional[int] = None) -> dict:
    """Replay a PAST admission cycle (imported historic records) under a proposed rule set."""
    cycle = cycle or current_cycle() - 1
    ledgers = {(lg.programme_id, lg.seat_type): lg for lg in db.scalars(select(SeatLedger).where(SeatLedger.cycle == cycle))}
    claims = db.scalars(select(Claim).where(Claim.cycle == cycle, Claim.historic.is_(True))
                        .order_by(Claim.admitted_on, Claim.id)).all()
    reg = {r.apaar_id: r.abc_credits for r in db.scalars(select(RegistryRecord))}
    inst_names = {i.id: i.name for i in db.scalars(select(Institution))}
    seen_hash: dict[str, int] = {}
    per_group: dict[tuple, int] = defaultdict(int)
    blocked, by_inst = [], defaultdict(lambda: {"admitted": 0, "blocked_cap": 0, "blocked_duplicate": 0})
    sim_by_day: Counter = Counter()
    act_by_day: Counter = Counter()
    genuine_blocked = 0
    for c in claims:
        key = (c.programme_id, c.seat_type)
        lg = ledgers.get(key)
        act_by_day[c.admitted_on] += 1
        by_inst[c.institution_id]["admitted"] += 1
        reason = None
        if enforce_unique and c.apaar_hash:
            if c.apaar_hash in seen_hash:
                reason = "DUPLICATE"
        if reason is None and lg is not None:
            limit = int(lg.intake * intake_multiplier * stop_pct / 100)
            if per_group[key] >= limit:
                reason = "CAP"
        if reason:
            blocked.append(c)
            by_inst[c.institution_id]["blocked_cap" if reason == "CAP" else "blocked_duplicate"] += 1
            # a blocked student with ABC credits is likely a genuine student: the false-positive cost of the rule
            if reg.get(decrypt(c.apaar_enc) or "", 0) > 0:
                genuine_blocked += 1
        else:
            per_group[key] += 1
            sim_by_day[c.admitted_on] += 1
            if c.apaar_hash:
                seen_hash[c.apaar_hash] = c.id
    total = len(claims)
    act_total_excess = sum(max(0, sum(1 for c in claims if (c.programme_id, c.seat_type) == k) - lg.intake)
                           for k, lg in ledgers.items())
    days = sorted(act_by_day)
    ca = cs = 0
    curve = []
    for d in days:
        ca += act_by_day[d]
        cs += sim_by_day.get(d, 0)
        curve.append({"date": d.isoformat(), "actual": ca, "simulated": cs})
    inst_rows = [{"institution": inst_names[i], **v} for i, v in by_inst.items() if v["blocked_cap"] or v["blocked_duplicate"]]
    inst_rows.sort(key=lambda r: -(r["blocked_cap"] + r["blocked_duplicate"]))
    return {"cycle": cycle, "params": {"intake_multiplier": intake_multiplier, "stop_pct": stop_pct, "enforce_unique": enforce_unique},
            "total_admissions": total, "would_block": len(blocked), "would_block_pct": round(100 * len(blocked) / total, 1) if total else 0,
            "actual_excess_over_intake": act_total_excess, "likely_genuine_blocked": genuine_blocked,
            "blocked_duplicate": sum(v["blocked_duplicate"] for v in by_inst.values()),
            "blocked_cap": sum(v["blocked_cap"] for v in by_inst.values()),
            "institutions_affected": inst_rows, "curve": curve,
            "note": "Replay of imported prior-cycle records. 'likely genuine' = blocked students who have ABC credits."}


# ---------------------------------------------------------------- suppressed reports
GROUPS = {"state": lambda c, i, p: i.state, "institution": lambda c, i, p: i.name, "programme": lambda c, i, p: f"{i.name} - {p.name}",
          "category": lambda c, i, p: c.category or "NA", "seat_type": lambda c, i, p: c.seat_type, "level": lambda c, i, p: p.level}


def enrolment_report(db: Session, group_by: str) -> dict:
    if group_by not in GROUPS:
        raise ValueError(f"group_by must be one of {', '.join(GROUPS)}")
    q = (select(Claim, Institution, Programme).join(Institution, Institution.id == Claim.institution_id)
         .join(Programme, Programme.id == Claim.programme_id).where(Claim.cycle == current_cycle(), Claim.historic.is_(False)))
    agg: dict[str, Counter] = defaultdict(Counter)
    for c, i, p in db.execute(q):
        agg[GROUPS[group_by](c, i, p)][c.state] += 1
    rows = []
    suppressed = 0
    for k in sorted(agg):
        total = sum(agg[k].values())
        if total < config.MIN_GROUP_SIZE:
            suppressed += 1
            rows.append({"group": k, "total": "<5", "verified": "<5", "awaiting_student": "<5", "needs_attention": "<5"})
        else:
            s = agg[k]
            rows.append({"group": k, "total": total, "verified": s["VERIFIED"],
                         "awaiting_student": s["AWAITING_STUDENT"] + s["UNCONFIRMED"],
                         "needs_attention": s["PENDING_FIX"] + s["PENDING"] + s["CONFLICT"] + s["SEAT_LIMIT"]})
    return {"group_by": group_by, "rows": rows, "suppressed_groups": suppressed,
            "note": f"Groups with fewer than {config.MIN_GROUP_SIZE} students are suppressed (shown as <5)."}


def report_csv(rep: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([rep["group_by"], "total", "verified", "awaiting_student", "needs_attention"])
    for r in rep["rows"]:
        w.writerow([r["group"], r["total"], r["verified"], r["awaiting_student"], r["needs_attention"]])
    return buf.getvalue()


# ---------------------------------------------------------------- cohort roots
def _leaves(db: Session, inst_id: int, cycle: int):
    cs = db.scalars(select(Claim).where(Claim.institution_id == inst_id, Claim.cycle == cycle, Claim.state == "VERIFIED",
                                        Claim.historic.is_(False)).order_by(Claim.id)).all()
    return cs, [merkle.leaf(c.id, c.apaar_hash or "", c.seat_type, c.state) for c in cs]


def publish_roots(db: Session, cycle: Optional[int] = None) -> list[dict]:
    cycle = cycle or current_cycle()
    out = []
    for inst in db.scalars(select(Institution)):
        cs, leaves = _leaves(db, inst.id, cycle)
        root = merkle.root(leaves)
        body = {"institution": inst.code, "cycle": cycle, "root": root, "leaf_count": len(leaves)}
        db.add(CohortRoot(institution_id=inst.id, cycle=cycle, root=root, leaf_count=len(leaves), signature=sign(body)))
        out.append({**body, "name": inst.name})
    db.commit()
    return out


def check_roots(db: Session) -> list[dict]:
    """Re-derive each institution's latest published root from live data and compare."""
    out = []
    for inst in db.scalars(select(Institution)):
        r = db.scalar(select(CohortRoot).where(CohortRoot.institution_id == inst.id).order_by(CohortRoot.id.desc()).limit(1))
        if not r:
            continue
        _, leaves = _leaves(db, inst.id, r.cycle)
        now_root = merkle.root(leaves)
        body = {"institution": inst.code, "cycle": r.cycle, "root": r.root, "leaf_count": r.leaf_count}
        sig_ok = verify_signature(body, r.signature)
        out.append({"institution": inst.name, "code": inst.code, "cycle": r.cycle, "published_root": r.root,
                    "current_root": now_root, "leaf_count": r.leaf_count, "current_leaf_count": len(leaves),
                    "signature_valid": sig_ok, "matches": sig_ok and now_root == r.root,
                    "published_at": r.created_at.isoformat(), "status": "INTACT" if sig_ok and now_root == r.root else "CHANGED SINCE PUBLICATION"})
    return out


def inclusion_proof(db: Session, claim: Claim) -> dict:
    r = db.scalar(select(CohortRoot).where(CohortRoot.institution_id == claim.institution_id, CohortRoot.cycle == claim.cycle)
                  .order_by(CohortRoot.id.desc()).limit(1))
    if not r:
        raise LookupError("No cohort root published for this institution yet")
    cs, leaves = _leaves(db, claim.institution_id, claim.cycle)
    ids = [c.id for c in cs]
    if claim.id not in ids:
        raise LookupError("Claim is not in the cohort (it must be VERIFIED when the root is built)")
    idx = ids.index(claim.id)
    lf = leaves[idx]
    path = merkle.proof(leaves, idx)
    return {"claim_id": claim.id, "leaf": lf, "path": path, "published_root": r.root,
            "verifies_against_published_root": merkle.verify(lf, path, r.root)}


# ---------------------------------------------------------------- natural-language query (rule-based, no LLM)
def nl_query(db: Session, text: str) -> dict:
    t = text.lower()
    cycle = current_cycle()
    if re.search(r"exceed|excess|over.?intake|seat.?limit|beyond", t):
        m = intelligence.current_metrics(db, cycle)
        rows = [{"institution": v["name"], "blocked_by_ledger": v["seat_limit_hits"], "past_cycle_excess": v["excess_n"]}
                for v in m.values() if v["seat_limit_hits"] or v["excess_n"]]
        rows.sort(key=lambda r: -(r["blocked_by_ledger"] + r["past_cycle_excess"]))
        return {"intent": "excess_admissions", "answer": f"{len(rows)} institutions tried to exceed approved intake or did last cycle.", "rows": rows[:10]}
    if re.search(r"trust|risk|worst|lowest|suspicious", t):
        ti = intelligence.trust_index(db, cycle)[:5]
        return {"intent": "lowest_trust", "answer": "Five lowest Trust Index scores (leads, not verdicts).",
                "rows": [{"institution": r["name"], "score": r["score"], "band": r["band"]} for r in ti]}
    if re.search(r"conflict|duplicate|double", t):
        n = db.scalar(select(func.count(Claim.id)).where(Claim.state == "CONFLICT", Claim.cycle == cycle))
        top = db.execute(select(Institution.name, func.count(Claim.id)).join(Institution, Institution.id == Claim.institution_id)
                         .where(Claim.state == "CONFLICT", Claim.cycle == cycle).group_by(Institution.name)
                         .order_by(func.count(Claim.id).desc()).limit(5)).all()
        return {"intent": "double_enrolment", "answer": f"{n} double-enrolment attempts were blocked this cycle.",
                "rows": [{"institution": a, "conflicts": b} for a, b in top]}
    if re.search(r"fix|pending|invalid|mismatch", t):
        top = db.execute(select(Institution.name, func.count(Claim.id)).join(Institution, Institution.id == Claim.institution_id)
                         .where(Claim.state.in_(("PENDING_FIX", "PENDING")), Claim.cycle == cycle).group_by(Institution.name)
                         .order_by(func.count(Claim.id).desc()).limit(8)).all()
        return {"intent": "pending_fixes", "answer": f"{sum(b for _, b in top)} claims across the top institutions need a fix or retry.",
                "rows": [{"institution": a, "claims": b} for a, b in top]}
    if re.search(r"confirm|ghost|unconfirmed|student response|lock", t):
        m = intelligence.current_metrics(db, cycle)
        rows = sorted(({"institution": v["name"], "confirmation_rate": round(v["lock3_rate"], 2)} for v in m.values() if v["lock3_rate"] is not None),
                      key=lambda r: r["confirmation_rate"])[:6]
        return {"intent": "lock3", "answer": "Lowest student-confirmation (Lock 3) rates.", "rows": rows}
    if re.search(r"full|utili|seat|capacity|90", t):
        rows = [r for r in ledger_rows(db) if r["status"] in ("ALERT", "FULL")]
        return {"intent": "seats_near_full", "answer": f"{len(rows)} programme/seat-type ledgers are at or above 90%.",
                "rows": [{"institution": r["institution"], "programme": r["programme"], "seat": r["seat_type"], "claimed": f"{r['claimed']}/{r['cap']}"} for r in rows[:12]]}
    d = national_dashboard(db)
    return {"intent": "summary", "answer": f"{d['claims_by_state'].get('VERIFIED', 0)} students verified, {d['seats_claimed']} of {d['seats_cap']} seats claimed "
                                          f"({d['utilisation_pct']}%). Try: 'who exceeded intake', 'lowest trust', 'double enrolment', 'pending fixes', 'seats almost full'.",
            "rows": []}
