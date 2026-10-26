"""Intelligence layers. Output is LEADS (evidence for a human reviewer), never verdicts.

Layers: 1 rules - 2 peer baselines (robust z) - 3 Isolation Forest + shared-contact graph - 4 liveness - 5 lead queue.
The Trust Index is a transparent weighted penalty score: each point links back to the evidence that caused it.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median
from typing import Optional

import networkx as nx
import numpy as np
from sklearn.ensemble import IsolationForest
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Claim, Institution, Lead, Programme, RegistryRecord, SeatLedger, utcnow
from .security import decrypt

# metric -> (label, bad direction: +1 higher is worse, -1 lower is worse)
METRICS = {
    "lock3_rate": ("Student confirmation rate (Lock 3)", -1),
    "fix_rate": ("Claims needing a fix", +1),
    "conflict_rate": ("Double-enrolment attempts", +1),
    "burst": ("Share of claims on the busiest 2 days", +1),
    "shared_contact": ("Students sharing a phone/email", +1),
    "abc_dead": ("Past cohort with no ABC credits", +1),
    "excess": ("Past-cycle admissions above intake", +1),
}


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def current_metrics(db: Session, cycle: int, now: Optional[datetime] = None) -> dict[int, dict]:
    now = now or utcnow()
    insts = db.scalars(select(Institution)).all()
    claims = db.scalars(select(Claim).where(Claim.state != "WITHDRAWN")).all()
    by_inst: dict[int, list[Claim]] = defaultdict(list)
    for c in claims:
        by_inst[c.institution_id].append(c)
    ledgers = db.scalars(select(SeatLedger)).all()
    reg_credit = {r.apaar_id: r.abc_credits for r in db.scalars(select(RegistryRecord))}

    # shared-contact graph: claim <-> contact-hash bipartite graph (only live current-cycle claims)
    G = nx.Graph()
    for c in claims:
        if c.cycle == cycle and c.active:
            for h in (c.phone_hash, c.email_hash):
                if h:
                    G.add_edge(f"c{c.id}", f"h{h}")
    shared_claims: set[int] = set()
    clusters = []
    for comp in nx.connected_components(G):
        members = [int(n[1:]) for n in comp if n.startswith("c")]
        if len(members) >= 3:
            shared_claims.update(members)
            clusters.append(sorted(members))
    cluster_of = {m: i for i, cl in enumerate(clusters) for m in cl}

    out: dict[int, dict] = {}
    for inst in insts:
        cs = by_inst.get(inst.id, [])
        cur = [c for c in cs if c.cycle == cycle and not c.historic]
        past = [c for c in cs if c.historic]
        live = [c for c in cur if c.active]
        n = len(cur)
        responders = [c for c in live if c.state in ("VERIFIED", "UNCONFIRMED", "CONSENT_REFUSED")
                      or (c.state == "AWAITING_STUDENT" and c.created_at <= now - timedelta(days=7))]
        lock3 = (sum(c.state == "VERIFIED" for c in responders) / len(responders)) if len(responders) >= 5 else None
        days = defaultdict(int)
        for c in live:
            days[c.admitted_on] += 1
        top2 = sum(sorted(days.values(), reverse=True)[:2])
        burst = (top2 / len(live)) if len(live) >= 20 else None
        shared = [c for c in live if c.id in shared_claims]
        past_ver = [c for c in past if c.state == "VERIFIED"]
        dead = [c for c in past_ver if reg_credit.get(decrypt(c.apaar_enc) or "", 1) == 0]
        # excess admissions in the previous cycle: historic imports per programme/seat vs approved intake
        excess_n, past_intake = 0, 0
        for lg in ledgers:
            prog = db.get(Programme, lg.programme_id)
            if prog.institution_id == inst.id and lg.cycle == cycle - 1:
                cnt = sum(1 for c in past if c.programme_id == lg.programme_id and c.seat_type == lg.seat_type)
                excess_n += max(0, cnt - lg.intake)
                past_intake += lg.intake
        cur_ledgers = [lg for lg in ledgers if lg.cycle == cycle and db.get(Programme, lg.programme_id).institution_id == inst.id]
        cap = sum(lg.intake + lg.extra for lg in cur_ledgers)
        out[inst.id] = {
            "institution_id": inst.id, "code": inst.code, "name": inst.name, "state": inst.state,
            "attempts": n, "live": len(live),
            "util": (sum(lg.claimed for lg in cur_ledgers) / cap) if cap else None,
            "seat_limit_hits": sum(c.state == "SEAT_LIMIT" for c in cur),
            "lock3_rate": lock3,
            "fix_rate": (sum(c.state == "PENDING_FIX" for c in cur) / n) if n >= 10 else None,
            "conflict_rate": (sum(c.state == "CONFLICT" for c in cur) / n) if n >= 10 else None,
            "burst": burst,
            "shared_contact": (len(shared) / len(live)) if len(live) >= 10 else None,
            "shared_cluster_sizes": sorted({len(clusters[cluster_of[c.id]]) for c in shared}, reverse=True)[:5],
            "shared_claim_ids": [c.id for c in shared][:12],
            "abc_dead": (len(dead) / len(past_ver)) if len(past_ver) >= 10 else None,
            "abc_dead_n": len(dead), "past_verified": len(past_ver),
            "excess": (excess_n / past_intake) if past_intake else None, "excess_n": excess_n,
            "provisional_overdue": sum(c.state == "PROVISIONAL" and c.provisional_deadline and c.provisional_deadline < now for c in cur),
        }
    return out


def robust_z(values: dict[int, Optional[float]]) -> dict[int, Optional[float]]:
    vals = [v for v in values.values() if v is not None]
    if len(vals) < 4:
        return {k: None for k in values}
    med = median(vals)
    mad = median(abs(v - med) for v in vals) * 1.4826
    scale = max(mad, 0.03)  # floor: a flat peer group must not turn tiny noise into huge z-scores
    return {k: (None if v is None else round((v - med) / scale, 2)) for k, v in values.items()}


def _sev(z: float) -> str:
    return "HIGH" if abs(z) >= 6 else "MEDIUM" if abs(z) >= 4 else "LOW"


def _upsert(db: Session, inst_id: int, layer: str, sig: str, sev: str, title: str, baseline: str,
            deviation: str, evidence: dict, action: str) -> Lead:
    lead = db.scalar(select(Lead).where(Lead.institution_id == inst_id, Lead.signature == sig))
    if lead:
        if lead.status == "OPEN":
            lead.severity, lead.title, lead.baseline, lead.deviation = sev, title, baseline, deviation
            lead.evidence, lead.action = evidence, action
        return lead
    lead = Lead(institution_id=inst_id, layer=layer, signature=sig, severity=sev, title=title, baseline=baseline,
                deviation=deviation, evidence=evidence, action=action)
    db.add(lead)
    db.flush()
    return lead


def run(db: Session, cycle: int, now: Optional[datetime] = None) -> dict:
    metrics = current_metrics(db, cycle, now)
    made = 0
    # ---- layer 2: peer baselines (robust z-score)
    zs = {m: robust_z({i: v[m] for i, v in metrics.items()}) for m in METRICS}
    for m, (label, direction) in METRICS.items():
        vals = [v[m] for v in metrics.values() if v[m] is not None]
        base = median(vals) if vals else None
        for iid, z in zs[m].items():
            if z is not None and z * direction >= 3:
                v = metrics[iid]
                _upsert(db, iid, "PEER", f"PEER:{m}", _sev(z), f"{label} is far from peers",
                        f"Peer median {_pct(base)}", f"{v['name']}: {_pct(v[m])} (robust z = {z:+.1f})",
                        {"metric": m, "value": v[m], "peer_median": base, "robust_z": z, "n_peers": len(vals)},
                        "Request institution explanation; sample-check the listed claims")
                made += 1
    # ---- layer 1: rules
    for iid, v in metrics.items():
        if v["seat_limit_hits"] >= 5:
            _upsert(db, iid, "RULE", "RULE:seat_pressure", "MEDIUM", "Repeated attempts beyond approved intake",
                    "Approved intake per programme", f"{v['seat_limit_hits']} claims blocked by the seat ledger",
                    {"seat_limit_hits": v["seat_limit_hits"]}, "Review pending exception requests; check for unapproved sections")
            made += 1
        if v["excess_n"] > 0:
            _upsert(db, iid, "RULE", "RULE:past_excess", "HIGH", "Past-cycle admissions exceeded approved intake",
                    "Approved intake", f"{v['excess_n']} admissions above intake last cycle",
                    {"excess_n": v["excess_n"], "excess_ratio": v["excess"]}, "Include in inspection plan; compare with seat ledger")
            made += 1
        if v["provisional_overdue"] > 0:
            _upsert(db, iid, "RULE", "RULE:provisional_overdue", "LOW", "Provisional APAAR IDs past their deadline",
                    "Deadline: 30 days", f"{v['provisional_overdue']} overdue", {"count": v["provisional_overdue"]},
                    "Remind institution; track as compliance")
            made += 1
    # ---- layer 3a: shared-contact graph
    for iid, v in metrics.items():
        if v["shared_contact"] and v["shared_contact"] >= 0.15:
            _upsert(db, iid, "GRAPH", "GRAPH:shared_contacts", "HIGH" if v["shared_contact"] >= 0.3 else "MEDIUM",
                    "Many students share the same phone/email", "Normally each student has their own contact",
                    f"{_pct(v['shared_contact'])} of live claims sit in shared-contact clusters (sizes {v['shared_cluster_sizes']})",
                    {"share": v["shared_contact"], "cluster_sizes": v["shared_cluster_sizes"], "example_claim_ids": v["shared_claim_ids"]},
                    "Sample-check these claims with the student confirmation channel")
            made += 1
    # ---- layer 4: liveness
    for iid, v in metrics.items():
        if v["abc_dead"] is not None and v["abc_dead"] >= 0.35:
            _upsert(db, iid, "LIVENESS", "LIVENESS:abc_credits", "HIGH", "Admitted students show no academic activity",
                    "Active students earn ABC credits within 2 semesters",
                    f"{v['abc_dead_n']} of {v['past_verified']} verified past-cohort students have 0 ABC credits",
                    {"abc_dead": v["abc_dead"], "n": v["abc_dead_n"], "of": v["past_verified"]},
                    "Request attendance/exam records for a sample; do not conclude without them")
            made += 1
    # ---- layer 3b: Isolation Forest across all institutions (multivariate outliers, no labels needed)
    iso = isolation_scores(metrics)
    for iid, info in iso.items():
        if info["flag"]:
            top = ", ".join(f"{METRICS[k][0]} ({_pct(metrics[iid][k])})" for k in info["drivers"])
            _upsert(db, iid, "ISOLATION", "ISOLATION:profile", "MEDIUM", "Unusual overall profile vs peers",
                    "Typical institution profile", f"Isolation Forest score {info['score']:.2f}; driven by {top}",
                    {"score": info["score"], "drivers": info["drivers"]}, "Treat as a prompt to look, not a finding")
            made += 1
    db.commit()
    return {"leads_total": db.query(Lead).count(), "evaluated_institutions": len(metrics), "touched": made}


def isolation_scores(metrics: dict[int, dict]) -> dict[int, dict]:
    keys = list(METRICS)
    ids = list(metrics)
    if len(ids) < 6:
        return {}
    X = np.array([[(metrics[i][k] if metrics[i][k] is not None else np.nan) for k in keys] for i in ids], dtype=float)
    # impute missing with the column median; orient so that larger = worse
    for j, k in enumerate(keys):
        col = X[:, j]
        med = np.nanmedian(col) if not np.all(np.isnan(col)) else 0.0
        col[np.isnan(col)] = med
        if METRICS[k][1] < 0:
            X[:, j] = -col
    mu, sd = X.mean(0), X.std(0)
    Z = (X - mu) / np.where(sd < 1e-9, 1, sd)
    model = IsolationForest(n_estimators=300, contamination="auto", random_state=7).fit(Z)
    dec = model.decision_function(Z)  # < 0 means outlier
    out = {}
    cutoff = np.percentile(dec, 20)
    for idx, iid in enumerate(ids):
        # simple, honest explanation: which standardised features push this institution away from the pack
        drivers = [keys[j] for j in np.argsort(-Z[idx])[:3] if Z[idx][j] > 1.0]
        out[iid] = {"score": float(-dec[idx]), "flag": bool(dec[idx] < 0 and dec[idx] <= cutoff and drivers),
                    "drivers": drivers}
    return out


# ---------------------------------------------------------------- Trust Index
def trust_index(db: Session, cycle: int, metrics: Optional[dict] = None, now: Optional[datetime] = None) -> list[dict]:
    metrics = metrics or current_metrics(db, cycle, now)
    leads = db.scalars(select(Lead)).all()
    lead_ids: dict[tuple[int, str], list[int]] = defaultdict(list)
    for l in leads:
        if l.status != "DISMISSED":
            lead_ids[(l.institution_id, l.signature.split(":")[-1])].append(l.id)
    rows = []
    for iid, v in metrics.items():
        comps = []

        def add(key, label, penalty, detail, ev_keys):
            comps.append({"key": key, "label": label, "penalty": round(penalty, 1), "detail": detail,
                          "lead_ids": sorted({i for k in ev_keys for i in lead_ids.get((iid, k), [])})})

        r = v["lock3_rate"]
        add("lock3_rate", "Student confirmation (Lock 3)", min(30, max(0, 0.85 - r) * 100 * 0.8) if r is not None else 0,
            f"{_pct(r)} of responding students confirmed (target 85%)", ["lock3_rate"])
        r = v["fix_rate"]
        add("fix_rate", "Data quality", min(10, max(0, r - 0.10) * 50) if r is not None else 0,
            f"{_pct(r)} of claims needed fixes (tolerated 10%)", ["fix_rate"])
        r = v["conflict_rate"]
        add("conflict_rate", "Double-enrolment attempts", min(15, max(0, r - 0.03) * 150) if r is not None else 0,
            f"{_pct(r)} of claims collided with a live claim elsewhere (tolerated 3%)", ["conflict_rate"])
        r = v["burst"]
        add("burst", "Admission burst", min(10, max(0, r - 0.25) * 40) if r is not None else 0,
            f"{_pct(r)} of claims fall on the busiest 2 days (tolerated 25%)", ["burst", "profile"])
        r = v["shared_contact"]
        add("shared_contact", "Shared contact details", min(20, r * 100) if r is not None else 0,
            f"{_pct(r)} of live claims share a phone/email", ["shared_contacts", "shared_contact"])
        r = v["abc_dead"]
        add("abc_dead", "ABC credit liveness", min(20, max(0, r - 0.08) * 40) if r is not None else 0,
            f"{_pct(r)} of last year's verified cohort has 0 ABC credits (tolerated 8%)", ["abc_credits", "abc_dead"])
        r = v["excess"]
        add("excess", "Past excess admissions", min(15, r * 100) if r is not None else 0,
            f"{v['excess_n']} admissions above approved intake last cycle", ["past_excess", "excess"])
        add("provisional", "Overdue provisional IDs", min(5, v["provisional_overdue"]),
            f"{v['provisional_overdue']} provisional IDs past deadline", ["provisional_overdue"])
        total = sum(c["penalty"] for c in comps)
        score = round(max(0.0, 100 - total), 1)
        rows.append({"institution_id": iid, "code": v["code"], "name": v["name"], "state": v["state"], "score": score,
                     "band": "GOOD" if score >= 80 else "WATCH" if score >= 60 else "REVIEW",
                     "components": [c for c in comps], "util": v["util"], "live": v["live"], "attempts": v["attempts"]})
    rows.sort(key=lambda r: r["score"])
    return rows
