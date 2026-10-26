"""HTTP-level tests: end-to-end workflow, RBAC/ABAC, privacy, tamper evidence."""
import io

from sqlalchemy import text

from app import analytics, audit, credentials as cr
from app.models import Claim
from tests.conftest import PW, latest_otp, login


def claim_body(world, code, idx, **kw):
    aid, nm, dob, ph = world.people[idx]
    b = {"programme_id": world.progs[code].id, "seat_type": "REGULAR", "student_name": nm, "dob": dob.isoformat(), "phone": ph,
         "apaar_id": aid, "email": "a@b.test", "gender": "F", "category": "GEN"}
    b.update(kw)
    return b


def test_full_workflow_claim_to_signed_verification(client, world):
    inst = login(client, "reg@aaa.t")
    r = client.post("/api/claims", json=claim_body(world, "AAA", 0), headers=inst)
    assert r.status_code == 201 and r.json()["state"] == "AWAITING_STUDENT"
    cid = r.json()["id"]
    # the institution can NOT see the OTP (only the student's own phone gets it)
    assert client.get("/api/dev/outbox", headers=inst).status_code == 403
    # student registers by proving APAAR ownership, reads OTP from their (mock) phone, confirms
    aid, nm, dob, ph = world.people[0]
    reg = client.post("/api/auth/student-register", json={"apaar_id": aid, "dob": dob.isoformat(), "email": "s0@t", "password": PW})
    assert reg.status_code == 200
    stu = {"Authorization": f"Bearer {reg.json()['token']}"}
    w = client.get("/api/student/wallet", headers=stu).json()
    assert w["claims"][0]["id"] == cid and w["claims"][0]["state"] == "AWAITING_STUDENT"
    otp = client.get("/api/dev/outbox", headers=stu).json()[0]["body"].split("OTP ")[1][:6]
    assert client.post(f"/api/student/claims/{cid}/confirm", json={"method": "OTP", "otp": "000000"}, headers=stu).status_code == 400
    assert client.post(f"/api/student/claims/{cid}/confirm", json={"method": "OTP", "otp": otp}, headers=stu).json()["state"] == "VERIFIED"
    cred = client.get("/api/student/wallet", headers=stu).json()["claims"][0]["credential"]
    assert cred["status"] == "VALID" and len(cred["fields"]) >= 10
    # verifier: purpose-bound disclosure + signed answer
    ver = login(client, "ver@t")
    v = client.post("/api/verifier/verify", json={"credential_id": cred["id"], "purpose": "EMPLOYMENT", "reason": "Offer letter check"}, headers=ver).json()
    assert v["payload"]["valid"] and set(v["payload"]["disclosed"]) == set(cr.PURPOSES["EMPLOYMENT"])
    assert "dob" not in v["payload"]["disclosed"] and "apaar_id" not in v["payload"]["disclosed"]
    assert client.post("/api/public/check-signature", json={"payload": v["payload"], "signature": v["signature"]}).json()["valid"]
    tampered = dict(v["payload"], valid=False)
    assert not client.post("/api/public/check-signature", json={"payload": tampered, "signature": v["signature"]}).json()["valid"]
    off = cr.check_disclosure(v["credential"]["envelope"], v["credential"]["signature"], v["credential"]["disclosures"])
    assert off == {"signature_valid": True, "disclosures_valid": True}
    # editing a disclosed value is detected
    bad = {f: dict(d, value="Someone Else") for f, d in v["credential"]["disclosures"].items()}
    assert not cr.check_disclosure(v["credential"]["envelope"], v["credential"]["signature"], bad)["disclosures_valid"]
    # public QR: yes/no only, wrong token rejected
    ok = client.get(f"/api/public/verify/{cred['id']}", params={"t": cred["qr_token"]}).json()
    assert ok["payload"]["valid"] and "disclosed" not in ok["payload"]
    assert not client.get(f"/api/public/verify/{cred['id']}", params={"t": "wrong"}).json()["payload"]["valid"]
    # student sees who looked and can revoke an organisation
    log = client.get("/api/student/access-log", headers=stu).json()
    assert any(e["org"] == "Acme" and e["purpose"] == "EMPLOYMENT" for e in log["events"])
    assert client.post("/api/student/revoke-access", json={"credential_id": cred["id"], "org": "Acme"}, headers=stu).status_code == 200
    again = client.post("/api/verifier/verify", json={"credential_id": cred["id"], "purpose": "EMPLOYMENT", "reason": "Second look"}, headers=ver).json()
    assert again["payload"]["status"] == "ACCESS_REVOKED" and not again["payload"].get("disclosed")
    other = login(client, "ver2@t")
    assert client.post("/api/verifier/verify", json={"credential_id": cred["id"], "purpose": "ENROLMENT_CHECK", "reason": "Different org"}, headers=other).json()["payload"]["valid"]
    # institution withdraws -> credential no longer verifies
    assert client.post(f"/api/claims/{cid}/withdraw", json={"reason": "Student transferred"}, headers=inst).status_code == 200
    assert not client.get(f"/api/public/verify/{cred['id']}", params={"t": cred["qr_token"]}).json()["payload"]["valid"]


def test_authentication_and_rbac(client, world):
    assert client.get("/api/claims").status_code == 401
    assert client.get("/api/claims", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "reg@aaa.t", "password": "wrong"}).status_code == 401
    inst, ver = login(client, "reg@aaa.t"), login(client, "ver@t")
    assert client.get("/api/aicte/dashboard", headers=inst).status_code == 403
    assert client.get("/api/claims", headers=ver).status_code == 403
    assert client.post("/api/claims", json=claim_body(world, "AAA", 0), headers=ver).status_code == 403
    assert client.post("/api/aicte/registry-outage", json={"outage": True}, headers=login(client, "rev@t")).status_code == 403  # reviewer != admin


def test_account_lockout_after_repeated_failures(client, world):
    for _ in range(5):
        client.post("/api/auth/login", json={"email": "reg@bbb.t", "password": "nope"})
    assert client.post("/api/auth/login", json={"email": "reg@bbb.t", "password": PW}).status_code == 423


def test_mfa_required_for_aicte_admin(client, world):
    r = client.post("/api/auth/login", json={"email": "admin@t", "password": PW}).json()
    assert r["mfa_required"] and "token" not in r
    assert client.post("/api/auth/mfa", json={"mfa_token": r["mfa_token"], "code": "000000"}).status_code == 401
    assert login(client, "admin@t")  # full flow with a valid TOTP
    # an MFA-stage token cannot be used as an access token
    assert client.get("/api/aicte/dashboard", headers={"Authorization": f"Bearer {r['mfa_token']}"}).status_code == 401


def test_institution_cannot_see_or_touch_another_institutions_claims(client, world):
    a, b = login(client, "reg@aaa.t"), login(client, "reg@bbb.t")
    cid = client.post("/api/claims", json=claim_body(world, "AAA", 0), headers=a).json()["id"]
    assert client.get(f"/api/claims/{cid}", headers=b).status_code == 404
    assert client.post(f"/api/claims/{cid}/withdraw", json={"reason": "hack attempt"}, headers=b).status_code == 404
    assert client.get("/api/claims", headers=b).json()["total"] == 0
    # cannot claim into another institution's programme
    r = client.post("/api/claims", json=claim_body(world, "AAA", 1), headers=b)
    assert r.status_code == 404
    # cannot read another institution's timeline
    assert client.get("/api/ledger/timeline", params={"programme_id": world.progs["AAA"].id}, headers=b).status_code == 404


def test_student_cannot_confirm_someone_elses_claim(client, world):
    a = login(client, "reg@aaa.t")
    cid = client.post("/api/claims", json=claim_body(world, "AAA", 0), headers=a).json()["id"]
    world.student_user(1)
    other = login(client, "s1@t")
    assert client.post(f"/api/student/claims/{cid}/confirm", json={"method": "DIGILOCKER"}, headers=other).status_code == 404
    assert client.get("/api/student/wallet", headers=other).json()["claims"] == []


def test_student_register_requires_matching_dob(client, world):
    aid = world.people[3][0]
    r = client.post("/api/auth/student-register", json={"apaar_id": aid, "dob": "1990-01-01", "email": "x@y.z", "password": PW})
    assert r.status_code == 400


def test_validation_errors_are_clear(client, world):
    a = login(client, "reg@aaa.t")
    assert client.post("/api/claims", json=claim_body(world, "AAA", 0, phone="123"), headers=a).status_code == 422
    assert client.post("/api/claims", json=claim_body(world, "AAA", 0, dob="31-02-2006"), headers=a).status_code == 422
    assert client.post("/api/claims", json=claim_body(world, "AAA", 0, seat_type="VIP"), headers=a).status_code == 422
    assert client.post("/api/claims", json={"programme_id": 1}, headers=a).status_code == 422


def test_idempotency_header(client, world):
    a = login(client, "reg@aaa.t")
    h = {**a, "Idempotency-Key": "abc-1"}
    r1 = client.post("/api/claims", json=claim_body(world, "AAA", 0), headers=h).json()
    r2 = client.post("/api/claims", json=claim_body(world, "AAA", 0), headers=h).json()
    assert r1["id"] == r2["id"]


def test_bulk_csv_uses_same_protocol_and_reports_each_row(client, world):
    a = login(client, "reg@aaa.t")
    rows = ["student_name,dob,apaar_id,phone,email,programme,seat_type"]
    for i in range(5):  # intake is 3 -> rows 4,5 hit SEAT_LIMIT
        aid, nm, dob, ph = world.people[i]
        rows.append(f"{nm},{dob.isoformat()},{aid},{ph},,B.Tech CSE,REGULAR")
    rows.append("Bad Row,not-a-date,200000000010,9876512345,,B.Tech CSE,REGULAR")
    rows.append("Wrong Programme,2005-01-01,200000000011,9876512345,,Astrology,REGULAR")
    r = client.post("/api/claims/bulk", files={"file": ("x.csv", io.BytesIO("\n".join(rows).encode()), "text/csv")}, headers=a)
    assert r.status_code == 202
    st = client.get(f"/api/claims/bulk/{r.json()['job_id']}", headers=a).json()
    assert st["status"] == "DONE" and st["processed"] == 7
    assert st["summary"]["AWAITING_STUDENT"] == 3 and st["summary"]["SEAT_LIMIT"] == 2 and st["summary"]["REJECTED_ROW"] == 2
    assert client.post("/api/claims/bulk", files={"file": ("x.csv", io.BytesIO(b"a,b\n1,2"), "text/csv")}, headers=a).status_code == 422
    assert client.get(f"/api/claims/bulk/{r.json()['job_id']}", headers=login(client, "reg@bbb.t")).status_code == 404


def test_exception_workflow_over_http(client, world):
    a, rev = login(client, "reg@aaa.t"), login(client, "rev@t")
    for i in range(3):
        client.post("/api/claims", json=claim_body(world, "AAA", i), headers=a)
    blocked = client.post("/api/claims", json=claim_body(world, "AAA", 3), headers=a).json()
    assert blocked["state"] == "SEAT_LIMIT"
    e = client.post("/api/exceptions", json={"programme_id": world.progs["AAA"].id, "seat_type": "REGULAR", "category": "TRANSFER",
                                             "seats": 1, "justification": "Inter-institute transfer approved by university"}, headers=a).json()
    assert client.post(f"/api/exceptions/{e['id']}/decide", json={"approve": True, "reason": "ok"}, headers=a).status_code == 403
    assert client.post(f"/api/exceptions/{e['id']}/decide", json={"approve": True, "reason": ""}, headers=rev).status_code == 422
    assert client.post(f"/api/exceptions/{e['id']}/decide", json={"approve": True, "reason": "Transfer order verified"}, headers=rev).json()["state"] == "APPROVED"
    assert client.get(f"/api/claims/{blocked['id']}", headers=a).json()["state"] == "AWAITING_STUDENT"
    assert client.post(f"/api/exceptions/{e['id']}/decide", json={"approve": False, "reason": "again"}, headers=rev).status_code == 409


def test_aicte_drilldown_requires_logged_reason_and_report_suppresses_small_groups(client, world):
    a, admin = login(client, "reg@aaa.t"), login(client, "admin@t")
    for i in range(3):
        client.post("/api/claims", json=claim_body(world, "AAA", i), headers=a)
    iid = world.insts["AAA"].id
    assert client.get("/api/aicte/claims", params={"institution_id": iid}, headers=admin).status_code == 422
    assert client.get("/api/aicte/claims", params={"institution_id": iid, "reason": "short"}, headers=admin).status_code == 422
    r = client.get("/api/aicte/claims", params={"institution_id": iid, "reason": "Inspection planning for visit 12"}, headers=admin)
    assert r.status_code == 200 and len(r.json()) == 3 and "phone_masked" not in r.json()[0]
    assert any(x["action"] == "DRILL_DOWN" for x in client.get("/api/aicte/audit", headers=admin).json())
    rep = client.get("/api/aicte/report", params={"group_by": "institution"}, headers=admin).json()
    assert all(row["total"] == "<5" for row in rep["rows"]) and rep["suppressed_groups"] >= 1  # only 3 students
    assert "<5" in client.get("/api/aicte/report", params={"group_by": "state", "format": "csv"}, headers=admin).text
    assert client.get("/api/aicte/report", params={"group_by": "nonsense"}, headers=admin).status_code == 422


def test_audit_chain_detects_tampering(client, world):
    a, admin = login(client, "reg@aaa.t"), login(client, "admin@t")
    for i in range(3):
        client.post("/api/claims", json=claim_body(world, "AAA", i), headers=a)
    assert client.get("/api/aicte/audit/verify", headers=admin).json()["ok"]
    from app.db import engine
    with engine.begin() as conn:
        conn.execute(text("UPDATE audit_log SET actor='mallory' WHERE id=(SELECT MIN(id)+2 FROM audit_log)"))
    res = client.get("/api/aicte/audit/verify", headers=admin).json()
    assert not res["ok"] and res["first_bad_id"]


def test_cohort_roots_detect_post_publication_changes_and_inclusion_proofs_verify(client, world):
    a, admin = login(client, "reg@aaa.t"), login(client, "admin@t")
    ids = []
    for i in range(3):
        cid = client.post("/api/claims", json=claim_body(world, "AAA", i), headers=a).json()["id"]
        ids.append(cid)
        c = world.db.get(Claim, cid)
        from app import claims as cl
        cl.confirm_claim(world.db, c, "DIGILOCKER", None, "s")
    pub = client.post("/api/aicte/cohort-roots/publish", headers=admin).json()
    assert next(p for p in pub if p["institution"] == "AAA")["leaf_count"] == 3
    assert all(r["matches"] for r in client.get("/api/aicte/cohort-roots/check", headers=admin).json())
    proof = client.get(f"/api/claims/{ids[1]}/inclusion-proof", headers=a).json()
    assert proof["verifies_against_published_root"]
    from app.db import engine
    with engine.begin() as conn:  # silently change a record after the root was signed
        conn.execute(text(f"UPDATE claims SET seat_type='EWS' WHERE id={ids[0]}"))
    chk = {r["code"]: r for r in client.get("/api/aicte/cohort-roots/check", headers=admin).json()}
    assert chk["AAA"]["status"] == "CHANGED SINCE PUBLICATION"


def test_policy_simulator_and_registry_outage_endpoints(client, world):
    admin = login(client, "admin@t")
    a = login(client, "reg@aaa.t")
    r = client.post("/api/aicte/simulate", json={"intake_multiplier": 1.0, "stop_pct": 100, "enforce_unique": True}, headers=admin)
    assert r.status_code == 200 and r.json()["total_admissions"] == 0
    assert client.post("/api/aicte/simulate", json={"intake_multiplier": 9}, headers=admin).status_code == 422
    assert client.post("/api/aicte/registry-outage", json={"outage": True}, headers=admin).json()["outage"]
    assert client.post("/api/claims", json=claim_body(world, "AAA", 0), headers=a).json()["state"] == "PENDING"
    res = client.post("/api/aicte/registry-outage", json={"outage": False}, headers=admin).json()
    assert res["retry"]["resolved"] == 1
    assert client.get("/api/claims", params={"state": "AWAITING_STUDENT"}, headers=a).json()["total"] == 1


def test_nl_query_is_rule_based_and_returns_rows(client, world):
    admin = login(client, "admin@t")
    r = client.post("/api/aicte/ask", json={"question": "which institutions have the lowest trust index?"}, headers=admin).json()
    assert r["intent"] == "lowest_trust"
    assert client.post("/api/aicte/ask", json={"question": "x"}, headers=admin).status_code == 422
