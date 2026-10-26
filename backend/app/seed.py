"""Synthetic demo data. Deterministic (seeded RNG); dates are relative to today.

Everything goes through the real claim protocol (create_claim / confirm_claim), so ledgers, states, audit rows and
credentials are genuine outputs of the product code. Only the *inputs* (people, registry, behaviour) are synthetic.

Run:  python -m app.seed [--reset]
"""
import random
import re
import sys
from datetime import date, datetime, timedelta

from sqlalchemy import select

from . import analytics, audit, claims as cl, config, intelligence
from .db import Base, SessionLocal, engine
from .models import (Claim, Institution, OutboxMessage, Programme, RegistryRecord, SeatException, SeatLedger, User, utcnow)
from .security import encrypt, hash_password, keyed_hash, new_totp_secret

PASSWORD = "Demo@1234"
FIRST_M = ["Aarav", "Vihaan", "Arjun", "Rohan", "Karan", "Siddharth", "Rahul", "Aditya", "Mohammed", "Imran", "Faisal", "Harpreet",
           "Gurpreet", "Rajesh", "Suresh", "Vikram", "Nikhil", "Pranav", "Yash", "Dev", "Ishaan", "Kunal", "Manish", "Tarun"]
FIRST_F = ["Ananya", "Diya", "Priya", "Kavya", "Neha", "Sneha", "Pooja", "Riya", "Isha", "Meera", "Aisha", "Fatima", "Simran",
           "Harleen", "Lakshmi", "Divya", "Shruti", "Tanvi", "Nandini", "Anjali", "Swati", "Rhea", "Aditi", "Pallavi"]
LAST = ["Sharma", "Verma", "Patil", "Deshmukh", "Kulkarni", "Reddy", "Nair", "Iyer", "Gupta", "Singh", "Khan", "Ansari", "Mehta", "Shah",
        "Joshi", "Bose", "Das", "Chatterjee", "Gill", "Sandhu", "Yadav", "Mishra", "Pandey", "Rao", "Menon", "Pillai", "Thakur", "Chauhan"]

INSTITUTIONS = [  # code, name, state, district, profile
    ("DIT", "Demo Institute of Technology", "Maharashtra", "Pune", "flagship"),
    ("RCE", "Riverbend College of Engineering", "Uttar Pradesh", "Lucknow", "ghost"),
    ("NFI", "Northfield Institute of Technology", "Rajasthan", "Jaipur", "burst"),
    ("LTC", "Lakeview Technical Campus", "Madhya Pradesh", "Bhopal", "messy"),
    ("SPI", "Sahyadri Polytechnic Institute", "Maharashtra", "Nashik", "normal"),
    ("GCT", "Gandhinagar College of Technology", "Gujarat", "Gandhinagar", "normal"),
    ("KIT", "Kaveri Institute of Technology", "Karnataka", "Mysuru", "normal"),
    ("TNE", "Tamirabarani Engineering College", "Tamil Nadu", "Tirunelveli", "normal"),
    ("PEM", "Punjab Engineering & Management College", "Punjab", "Ludhiana", "normal"),
    ("BIM", "Brahmaputra Institute of Management", "Assam", "Guwahati", "normal"),
    ("HPT", "Himalaya Polytechnic", "Himachal Pradesh", "Shimla", "normal"),
    ("OSE", "Odisha School of Engineering", "Odisha", "Bhubaneswar", "normal"),
]
PROGRAMMES = [("B.Tech CSE", "UG", 40), ("B.Tech ECE", "UG", 30), ("B.Tech Mechanical", "UG", 30), ("MBA", "PG", 30),
              ("Diploma Mechanical", "DIPLOMA", 30), ("MCA", "PG", 24)]
PROFILE = {  # fill, confirm prob, fix prob, conflict attempts, burst, shared-contact share, past mult, past abc-zero prob
    "flagship": dict(fill=0.85, confirm=0.9, fix=0.05, conflicts=2, burst=0.0, shared=0.0, past=0.92, abc0=0.03),
    "normal": dict(fill=0.8, confirm=0.88, fix=0.06, conflicts=1, burst=0.0, shared=0.0, past=0.9, abc0=0.04),
    "ghost": dict(fill=0.95, confirm=0.28, fix=0.04, conflicts=1, burst=0.0, shared=0.35, past=1.0, abc0=0.62),
    "burst": dict(fill=1.0, confirm=0.85, fix=0.05, conflicts=1, burst=0.8, shared=0.0, past=1.28, abc0=0.05),
    "messy": dict(fill=0.8, confirm=0.8, fix=0.4, conflicts=14, burst=0.0, shared=0.0, past=0.9, abc0=0.08),
}
DEMO_PEOPLE = {  # fixed identities used in the README demo script
    "ananya": ("110000000001", "Ananya Sharma", date(2007, 3, 15), "9000000001", "F"),
    "rohan": ("110000000002", "Rohan Mehta", date(2006, 11, 2), "9000000002", "M"),
    "arif": ("110000000003", "Mohammed Arif Khan", date(2006, 4, 7), "9000000003", "M"),
    "karan": ("110000000004", "Karan Malhotra", date(2006, 1, 20), "9000000004", "M"),
}
SPARE = [  # unclaimed registry identities for live entry during the demo
    ("110000000010", "Neha Kulkarni", date(2006, 5, 9), True), ("110000000011", "Imran Shaikh", date(2006, 8, 22), True),
    ("110000000012", "Pooja Deshpande", date(2007, 1, 30), True), ("110000000013", "Vikram Rathore", date(2006, 12, 12), True),
    ("110000000014", "Divya Nambiar", date(2006, 9, 3), True), ("110000000015", "Harleen Kaur", date(2006, 2, 17), True),
    ("110000000016", "Tanvi Joshi", date(2008, 12, 1), True),  # under 18: needs a guardian OTP
    ("110000000020", "Sunil Kamble", date(2005, 6, 6), False),  # inactive APAAR ID
]


class Gen:
    def __init__(self, rng):
        self.rng, self.used_ids, self.used_phones = rng, set(), set()

    def apaar(self):
        while True:
            v = str(self.rng.randint(2, 9)) + "".join(str(self.rng.randint(0, 9)) for _ in range(11))
            if v not in self.used_ids:
                self.used_ids.add(v)
                return v

    def phone(self):
        while True:
            v = str(self.rng.randint(6, 9)) + "".join(str(self.rng.randint(0, 9)) for _ in range(9))
            if v not in self.used_phones:
                self.used_phones.add(v)
                return v

    def name(self):
        g = self.rng.choice("MF")
        first = self.rng.choice(FIRST_M if g == "M" else FIRST_F)
        mid = self.rng.choice(FIRST_M) if self.rng.random() < 0.35 else ""
        return " ".join(x for x in (first, mid, self.rng.choice(LAST)) if x), g

    def dob(self, level):
        lo, hi = {"UG": (2004, 2008), "DIPLOMA": (2006, 2009), "PG": (1998, 2004)}[level]
        y = self.rng.randint(lo, hi)
        return date(y, self.rng.randint(1, 12), self.rng.randint(1, 28))


def _wipe(db):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def seed(reset: bool = False, verbose: bool = True):
    Base.metadata.create_all(engine)
    db = SessionLocal()
    if db.scalar(select(Institution.id).limit(1)):
        if not reset:
            if verbose:
                print("Database already seeded (use --reset to rebuild).")
            return
        db.close()
        _wipe(engine)
        db = SessionLocal()
    rng = random.Random(42)
    g = Gen(rng)
    today, cycle = date.today(), cl.current_cycle()
    log = print if verbose else (lambda *a, **k: None)

    # ---------- institutions, programmes, ledgers
    inst_rows, prog_rows = {}, {}
    for code, name, state, district, profile in INSTITUTIONS:
        inst = Institution(code=code, name=name, state=state, district=district)
        db.add(inst)
        db.flush()
        inst_rows[code] = (inst, profile)
        if code == "DIT":
            plist = [("B.Tech CSE", "UG", 60), ("B.Tech ECE", "UG", 40), ("MBA", "PG", 30)]
        else:
            k = rng.choice([2, 3])
            plist = rng.sample(PROGRAMMES, k)
        for pname, level, intake in plist:
            p = Programme(institution_id=inst.id, name=pname, level=level)
            db.add(p)
            db.flush()
            prog_rows[(code, pname)] = p
            for cyc in (cycle, cycle - 1):
                db.add(SeatLedger(programme_id=p.id, seat_type="REGULAR", cycle=cyc, intake=intake))
                db.add(SeatLedger(programme_id=p.id, seat_type="EWS", cycle=cyc, intake=max(2, intake // 10)))
    db.commit()

    # ---------- users
    admin = User(email="admin@aicte.demo", name="AICTE Admin", role="AICTE_ADMIN", password_hash=hash_password(PASSWORD), mfa_secret=new_totp_secret())
    rev = User(email="reviewer@aicte.demo", name="AICTE Reviewer", role="AICTE_REVIEWER", password_hash=hash_password(PASSWORD), mfa_secret=new_totp_secret())
    db.add_all([admin, rev])
    for code, (inst, _) in inst_rows.items():
        db.add(User(email=f"registrar@{code.lower()}.demo", name=f"Registrar, {inst.name}", role="INSTITUTION",
                    institution_id=inst.id, password_hash=hash_password(PASSWORD)))
    db.add(User(email="hr@acme.demo", name="Acme HR Desk", role="VERIFIER", org="Acme Technologies HR", password_hash=hash_password(PASSWORD)))
    db.add(User(email="loans@sbank.demo", name="Loan Officer", role="VERIFIER", org="Sahyadri Bank - Education Loans", password_hash=hash_password(PASSWORD)))
    db.commit()

    # ---------- registry: demo people, spares
    for key, (aid, name, dob, phone, gen) in DEMO_PEOPLE.items():
        db.add(RegistryRecord(apaar_id=aid, full_name=name, dob=dob, active=True, abc_credits=0))
        g.used_ids.add(aid)
        g.used_phones.add(phone)
    for aid, name, dob, active in SPARE:
        db.add(RegistryRecord(apaar_id=aid, full_name=name, dob=dob, active=active, abc_credits=0))
        g.used_ids.add(aid)
    db.commit()

    # ---------- previous cycle (imported records; includes excess admissions and a few duplicates)
    log("Seeding previous-cycle cohort ...")
    hist_people = []
    for (code, pname), p in prog_rows.items():
        inst, profile = inst_rows[code]
        pr = PROFILE[profile]
        for lg in db.scalars(select(SeatLedger).where(SeatLedger.programme_id == p.id, SeatLedger.cycle == cycle - 1)):
            n = round(lg.intake * pr["past"]) + (1 if profile == "burst" and lg.seat_type == "EWS" else 0)
            for k in range(n):
                aid = g.apaar()
                name, gen = g.name()
                dob = g.dob(p.level)
                zero = rng.random() < pr["abc0"]
                db.add(RegistryRecord(apaar_id=aid, full_name=name, dob=dob, active=True,
                                      abc_credits=0 if zero else rng.randint(8, 44)))
                ph = g.phone()
                adm = today - timedelta(days=rng.randint(380, 440))
                db.add(Claim(institution_id=inst.id, programme_id=p.id, seat_type=lg.seat_type, cycle=cycle - 1,
                             student_ref=f"{code}/{cycle - 1}/{len(hist_people) + 1:05d}", apaar_hash=keyed_hash(aid), apaar_enc=encrypt(aid),
                             apaar_masked=cl.mask(aid), student_name=name, dob=dob, gender=gen, category=rng.choice(["GEN", "OBC", "SC", "ST", "EWS"]),
                             phone_enc=encrypt(ph), phone_hash=keyed_hash(ph), state="VERIFIED", active=False, holds_seat=False, historic=True,
                             reason="Imported from previous cycle", admitted_on=adm, created_at=datetime.combine(adm, datetime.min.time()),
                             verified_at=datetime.combine(adm, datetime.min.time())))
                hist_people.append((aid, name, dob, ph, inst.id, p.id, lg.seat_type))
                lg.claimed += 1
    # the same student admitted at two institutions last year (ghost/double enrolment the old process missed)
    for aid, name, dob, ph, iid, pid, st in rng.sample(hist_people, 7):
        other = rng.choice([x for x in inst_rows.values() if x[0].id != iid])[0]
        op = rng.choice([p for (c, _), p in prog_rows.items() if c == other.code])
        adm = today - timedelta(days=rng.randint(380, 440))
        db.add(Claim(institution_id=other.id, programme_id=op.id, seat_type="REGULAR", cycle=cycle - 1,
                     student_ref=f"{other.code}/{cycle - 1}/D{rng.randint(1000, 9999)}", apaar_hash=keyed_hash(aid), apaar_enc=encrypt(aid),
                     apaar_masked=cl.mask(aid), student_name=name, dob=dob, phone_enc=encrypt(ph), phone_hash=keyed_hash(ph), state="VERIFIED",
                     active=False, historic=True, reason="Imported from previous cycle", admitted_on=adm,
                     created_at=datetime.combine(adm, datetime.min.time())))
    db.commit()

    # ---------- current cycle through the REAL protocol
    log("Running current-cycle claims through the protocol ...")
    verified_pool = []

    def run_one(code, p, seat_type, adm_date, person, pr, profile, phone_override=None):
        inst, _ = inst_rows[code]
        aid, name, dob, phone, gen = person
        phone = phone_override or phone
        sub_name, sub_dob, sub_aid = name, dob, aid
        r = rng.random()
        if r < pr["fix"]:
            kind = rng.choice(["swap", "swap", "year", "wrongid", "name"])
            if kind == "swap" and dob.day <= 12 and dob.day != dob.month:
                sub_dob = date(dob.year, dob.day, dob.month)
            elif kind == "year":
                sub_dob = date(dob.year + 1, dob.month, dob.day)
            elif kind == "wrongid":
                sub_aid = aid[:-1] + str((int(aid[-1]) + 1) % 10)
            else:
                sub_name = name.split()[0] + " " + rng.choice(LAST)
        minor = cl.age_on(sub_dob, adm_date) < 18
        inp = cl.ClaimInput(institution_id=inst.id, programme_id=p.id, seat_type=seat_type, student_name=sub_name, dob=sub_dob,
                            phone=phone, apaar_id=sub_aid, email=f"{re.sub(r'[^a-z]', '', name.lower())}{rng.randint(1, 999)}@mail.test",
                            guardian_phone="9111111111" if minor else None, gender=gen,
                            category=rng.choice(["GEN", "GEN", "OBC", "SC", "ST", "EWS"]), admitted_on=adm_date)
        as_of = datetime.combine(adm_date, datetime.min.time()) + timedelta(hours=rng.randint(9, 17), minutes=rng.randint(0, 59))
        c = cl.create_claim(db, inp, f"registrar@{code.lower()}.demo", as_of=as_of)
        if c.state == "PENDING_FIX" and c.fixes and rng.random() < 0.5:
            c = cl.apply_fixes(db, c, [f["field"] for f in c.fixes], f"registrar@{code.lower()}.demo")
        if c.state == "AWAITING_STUDENT" and rng.random() < pr["confirm"]:
            m = db.scalar(select(OutboxMessage).where(OutboxMessage.claim_id == c.id).order_by(OutboxMessage.id.desc()))
            otp = re.search(r"OTP (\d{6})", m.body).group(1)
            when = as_of + timedelta(minutes=rng.randint(1, 10))
            if rng.random() < 0.5:
                cl.confirm_claim(db, c, "OTP", otp, f"student:{c.id}", as_of=min(when, datetime.utcnow()))
            else:
                cl.confirm_claim(db, c, "DIGILOCKER", None, f"student:{c.id}", as_of=min(when, datetime.utcnow()))
        if c.state == "VERIFIED":
            verified_pool.append((c, person))
        return c

    def new_person(level):
        aid = g.apaar()
        name, gen = g.name()
        dob = g.dob(level)
        db.add(RegistryRecord(apaar_id=aid, full_name=name, dob=dob, active=True, abc_credits=0))
        return (aid, name, dob, g.phone(), gen)

    CURVE = [5, 11, 17, 22, 27, 31, 35, 38, 41, 44, 47, 49, 51, 53, 54, 56, 57, 59]  # slide-3 cumulative claims, days 1-18
    for code, (inst, profile) in inst_rows.items():
        pr = PROFILE[profile]
        for (c_, pname), p in [(k, v) for k, v in prog_rows.items() if k[0] == code]:
            for lg in db.scalars(select(SeatLedger).where(SeatLedger.programme_id == p.id, SeatLedger.cycle == cycle)):
                if code == "DIT" and pname == "B.Tech CSE" and lg.seat_type == "REGULAR":
                    # the exact admission curve of the idea document (59 of 60 by day 18); leaves one seat for the live demo
                    dates = []
                    prev = 0
                    for day, cum in enumerate(CURVE, start=1):
                        dates += [today - timedelta(days=25 - day)] * (cum - prev)
                        prev = cum
                    people = [new_person("UG") for _ in dates]
                    for d, person in zip(dates, people):
                        run_one(code, p, "REGULAR", d, person, {**pr, "fix": 0.0}, profile)
                    continue
                cap = lg.intake
                n = round(cap * pr["fill"]) if profile != "burst" else round(cap * 1.3)
                shared_phone = None
                for k in range(n):
                    if pr["burst"] and rng.random() < pr["burst"]:
                        d = today - timedelta(days=rng.choice([14, 13]))
                    else:
                        d = today - timedelta(days=rng.randint(2, 44))
                    person = new_person(p.level)
                    ph = None
                    if pr["shared"] and rng.random() < pr["shared"]:
                        if shared_phone is None or rng.random() < 0.2:
                            shared_phone = g.phone()
                        ph = shared_phone
                    run_one(code, p, lg.seat_type, d, person, pr, profile, ph)
        db.commit()
        log(f"  {code}: done")

    # ---------- double-enrolment attempts: someone verified elsewhere is claimed again
    log("Injecting double-enrolment attempts ...")
    for code, (inst, profile) in inst_rows.items():
        pr = PROFILE[profile]
        progs = [v for k, v in prog_rows.items() if k[0] == code]
        for _ in range(pr["conflicts"]):
            c0, person = rng.choice([x for x in verified_pool if x[0].institution_id != inst.id])
            aid, name, dob, phone, gen = person
            p = rng.choice(progs)
            cl.create_claim(db, cl.ClaimInput(institution_id=inst.id, programme_id=p.id, seat_type="REGULAR", student_name=name, dob=dob,
                                              phone=g.phone(), apaar_id=aid, gender=gen, category="GEN", guardian_phone="9111111111",
                                              admitted_on=today - timedelta(days=rng.randint(1, 20))),
                            f"registrar@{code.lower()}.demo")
    # a few provisional-ID students (one overdue)
    for code in ("OSE", "KIT"):
        inst, _ = inst_rows[code]
        p = next(v for k, v in prog_rows.items() if k[0] == code)
        for days_ago, label in ((12, "ID generation pending"), (41, "ID generation pending")):
            adm = today - timedelta(days=days_ago)
            n, g_ = g.name()
            cl.create_claim(db, cl.ClaimInput(institution_id=inst.id, programme_id=p.id, seat_type="REGULAR", student_name=n, dob=g.dob(p.level),
                                              phone=g.phone(), guardian_phone="9111111111", gender=g_, provisional_reason=label, admitted_on=adm),
                            f"registrar@{code.lower()}.demo", as_of=datetime.combine(adm, datetime.min.time()))

    # ---------- named demo students
    log("Creating named demo students ...")
    dit, _ = inst_rows["DIT"]
    ece, cse, mba = prog_rows[("DIT", "B.Tech ECE")], prog_rows[("DIT", "B.Tech CSE")], prog_rows[("DIT", "MBA")]
    reg = "registrar@dit.demo"
    aid, name, dob, phone, gen = DEMO_PEOPLE["ananya"]
    cl.create_claim(db, cl.ClaimInput(institution_id=dit.id, programme_id=ece.id, seat_type="REGULAR", student_name=name, dob=dob, phone=phone,
                                      apaar_id=aid, email="ananya@mail.test", gender=gen, category="GEN", admitted_on=today - timedelta(days=1)), reg)
    aid, name, dob, phone, gen = DEMO_PEOPLE["rohan"]
    c = cl.create_claim(db, cl.ClaimInput(institution_id=dit.id, programme_id=ece.id, seat_type="REGULAR", student_name=name, dob=dob, phone=phone,
                                          apaar_id=aid, email="rohan@mail.test", gender=gen, category="OBC", admitted_on=today - timedelta(days=9)), reg)
    cl.confirm_claim(db, c, "DIGILOCKER", None, "student:rohan")
    aid, name, dob, phone, gen = DEMO_PEOPLE["arif"]
    # slide-3 example: 'Mohd Arif K.' with day/month swapped in the date of birth
    cl.create_claim(db, cl.ClaimInput(institution_id=dit.id, programme_id=mba.id, seat_type="REGULAR", student_name="Mohd Arif K.", dob=date(2006, 7, 4),
                                      phone=phone, apaar_id=aid, email="arif@mail.test", gender=gen, category="GEN", admitted_on=today - timedelta(days=2)), reg)
    # Karan: live at Lakeview => any other institution claiming him gets CONFLICT (demo)
    aid, name, dob, phone, gen = DEMO_PEOPLE["karan"]
    ltc, ltc_p = inst_rows["LTC"][0], next(v for k, v in prog_rows.items() if k[0] == "LTC")
    c = cl.create_claim(db, cl.ClaimInput(institution_id=ltc.id, programme_id=ltc_p.id, seat_type="REGULAR", student_name=name, dob=dob, phone=phone,
                                          apaar_id=aid, gender=gen, category="GEN", admitted_on=today - timedelta(days=12)), "registrar@ltc.demo")
    if c.state == "AWAITING_STUDENT":
        cl.confirm_claim(db, c, "DIGILOCKER", None, "student:karan")
    for key in ("ananya", "rohan", "arif"):
        aid, name, *_ = DEMO_PEOPLE[key]
        db.add(User(email=f"{key}@student.demo", name=name, role="STUDENT", apaar_hash=keyed_hash(aid), password_hash=hash_password(PASSWORD)))
    db.commit()

    # ---------- exceptions, sweeps, intelligence, cohort roots
    nfi, nfi_p = inst_rows["NFI"][0], next(v for k, v in prog_rows.items() if k[0] == "NFI")
    db.add(SeatException(institution_id=nfi.id, programme_id=nfi_p.id, seat_type="REGULAR", category="SUPERNUMERARY", seats=6,
                         justification="Supernumerary seats for J&K migrants / PMSSS students as per scheme notification."))
    db.commit()
    log("Sweeping unconfirmed claims, running intelligence, publishing cohort roots ...")
    cl.sweep_unconfirmed(db)
    res = intelligence.run(db, cycle)
    analytics.publish_roots(db, cycle)
    audit.log(db, "system", "SEED_COMPLETE", "system", "-", res)
    db.commit()
    total = db.query(Claim).count()
    log(f"Seed complete: {total} claims, {res['leads_total']} leads. Demo password for all accounts: {PASSWORD}")
    db.close()


if __name__ == "__main__":
    seed(reset="--reset" in sys.argv)
