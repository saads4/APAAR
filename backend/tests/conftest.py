import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="sentinel-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["DATA_DIR"] = _tmp
os.environ["DEMO_MODE"] = "true"
os.environ["RATE_LIMIT_PER_MIN"] = "100000"

from datetime import date  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import claims as cl  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Institution, Programme, RegistryRecord, SeatLedger, User  # noqa: E402
from app.security import hash_password, keyed_hash, new_totp_secret, reset_rate_limits  # noqa: E402

PW = "Test@12345"


class World:
    """Small deterministic world: 2 institutions, 1 programme each (REGULAR intake 3 / 10), 30 registry people."""

    def __init__(self):
        self.db = SessionLocal()
        db = self.db
        self.cycle = cl.current_cycle()
        self.insts, self.progs = {}, {}
        for code, intake in (("AAA", 3), ("BBB", 10)):
            i = Institution(code=code, name=f"Inst {code}", state="Maharashtra", district="Pune")
            db.add(i)
            db.flush()
            p = Programme(institution_id=i.id, name="B.Tech CSE", level="UG")
            db.add(p)
            db.flush()
            db.add(SeatLedger(programme_id=p.id, seat_type="REGULAR", cycle=self.cycle, intake=intake))
            db.add(SeatLedger(programme_id=p.id, seat_type="EWS", cycle=self.cycle, intake=2))
            db.add(User(email=f"reg@{code.lower()}.t", name=f"Reg {code}", role="INSTITUTION", institution_id=i.id,
                        password_hash=hash_password(PW)))
            self.insts[code], self.progs[code] = i, p
        self.people = []
        for n in range(30):
            aid = f"2000000000{n:02d}"
            nm = ["Aarav Kulkarni", "Diya Sharma", "Mohammed Arif Khan", "Priya Nair", "Rohan Mehta"][n % 5] + f" {chr(65 + n // 5)}x"
            nm = nm.replace("x", "").strip()
            dob = date(2005, (n % 12) + 1, (n % 27) + 1)
            db.add(RegistryRecord(apaar_id=aid, full_name=nm, dob=dob, active=True, abc_credits=5))
            self.people.append((aid, nm, dob, f"98765{n:05d}"))
        db.add(User(email="admin@t", name="Admin", role="AICTE_ADMIN", password_hash=hash_password(PW), mfa_secret=new_totp_secret()))
        db.add(User(email="rev@t", name="Rev", role="AICTE_REVIEWER", password_hash=hash_password(PW)))
        db.add(User(email="ver@t", name="Verifier", role="VERIFIER", org="Acme", password_hash=hash_password(PW)))
        db.add(User(email="ver2@t", name="Verifier2", role="VERIFIER", org="OtherCo", password_hash=hash_password(PW)))
        db.commit()

    def student_user(self, idx: int) -> User:
        aid = self.people[idx][0]
        u = User(email=f"s{idx}@t", name=self.people[idx][1], role="STUDENT", apaar_hash=keyed_hash(aid), password_hash=hash_password(PW))
        self.db.add(u)
        self.db.commit()
        return u

    def inp(self, code: str, idx: int, seat="REGULAR", **kw) -> cl.ClaimInput:
        aid, nm, dob, ph = self.people[idx]
        base = dict(institution_id=self.insts[code].id, programme_id=self.progs[code].id, seat_type=seat, student_name=nm, dob=dob,
                    phone=ph, apaar_id=aid, email=f"p{idx}@x.t", gender="F", category="GEN")
        base.update(kw)
        return cl.ClaimInput(**base)

    def claim(self, code: str, idx: int, **kw):
        return cl.create_claim(self.db, self.inp(code, idx, **kw), f"reg@{code.lower()}.t")


@pytest.fixture()
def world():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    reset_rate_limits()
    w = World()
    yield w
    w.db.close()


@pytest.fixture()
def client(world):
    with TestClient(app) as c:
        yield c


def login(client, email, pw=PW) -> dict:
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    body = r.json()
    if body.get("mfa_required"):
        code = client.get("/api/auth/demo-mfa", params={"email": email}).json()["code"]
        body = client.post("/api/auth/mfa", json={"mfa_token": body["mfa_token"], "code": code}).json()
    return {"Authorization": f"Bearer {body['token']}"}


def latest_otp(db, claim_id) -> str:
    import re
    from sqlalchemy import select
    from app.models import OutboxMessage
    m = db.scalar(select(OutboxMessage).where(OutboxMessage.claim_id == claim_id).order_by(OutboxMessage.id.desc()))
    return re.search(r"OTP (\d{6})", m.body).group(1)
