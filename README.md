# APAAR Sentinel — working prototype

**SIH 2026 · APAAR-based Student Data Management and Verification System · Team Artificial_Unintelligence**

> *Claim · Lock · Record.* Institutions claim a student's APAAR ID before admitting; a live seat ledger and three
> independent confirmations decide what AICTE records; verifiers get a signed yes/no, never the student file.

This is a runnable implementation of the idea in `SIH ppt_final.pdf` / `APAAR_Sentinel_SIH2026.md`:
React + TypeScript frontend, FastAPI backend, SQL database, real authentication, and tests.
**All data is synthetic, and the external systems (APAAR registry, SMS, DigiLocker, ABC) are mocked** — see
[Limitations](#limitations-read-this-before-demoing).

---

## 1. Run it

### Option A — local (no Docker)

Prerequisites: Python 3.11+ and Node 20+.

```powershell
# from this folder
./start-local.ps1
```

The script creates the Python venv, installs dependencies, seeds the database (≈12 s), and starts both servers.
(`start-local.ps1` is a convenience wrapper that was only syntax-checked, not executed; the manual steps below are exactly what was run during development.) By hand:

```bash
# backend  (terminal 1)
cd backend
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt     # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m app.seed --reset                                  # synthetic data
.venv/Scripts/python -m uvicorn app.main:app --port 8000

# frontend (terminal 2)
cd frontend
npm install
npm run dev                                                               # http://localhost:5173
```

Open **http://localhost:5173**. API docs: http://localhost:8000/docs.

### Option B — Docker Compose (PostgreSQL)

```bash
cp .env.example .env
docker compose up --build        # web on :8080, API on :8000
```

> ⚠️ **Not executed in development.** The Docker daemon was not available on the build machine, so the
> Dockerfiles, `docker-compose.yml`, the Alembic migration on PostgreSQL, and the PostgreSQL code paths were
> written and statically checked (the schema compiles to valid PostgreSQL DDL) but **never run**. The SQLite
> path is what was tested. Expect to fix small things on the first `docker compose up`.

### Database migrations and seed

* Schema is managed by Alembic: `cd backend && alembic upgrade head` (the container does this on start).
  The migration was generated from the models and applied to a fresh SQLite database successfully.
* `python -m app.seed [--reset]` loads the demo world. Everything is pushed through the **real** claim protocol
  (so ledgers, audit rows and credentials are genuine product output); only the inputs are synthetic.
* Configuration: [`.env.example`](.env.example). With `DEMO_MODE=false` the app refuses to start on the built-in dev secrets.

---

## 2. Demo accounts (password for all: `Demo@1234`)

| Role | Login | What to look at |
|---|---|---|
| AICTE admin (**MFA**) | `admin@aicte.demo` | Dashboard, Trust Index, leads, simulator, integrity, ops buttons. The login page has a *Demo: fetch current code* button standing in for an authenticator app. |
| AICTE reviewer (MFA) | `reviewer@aicte.demo` | Same, but cannot run admin operations |
| Institution | `registrar@dit.demo` | Demo Institute of Technology (flagship). Every institution has `registrar@<code>.demo` (`rce`, `nfi`, `ltc`, `spi`, `gct`, `kit`, `tne`, `pem`, `bim`, `hpt`, `ose`) |
| Student | `ananya@student.demo` | Admission awaiting OTP (Lock 3) |
| Student | `rohan@student.demo` | Verified; credential + QR |
| Student | `arif@student.demo` | Registered for the "Mohd Arif K." fix-it example |
| Verifier | `hr@acme.demo`, `loans@sbank.demo` | Purpose-bound verification |

Seeded "stories": **Riverbend** (ghost-student pattern: low student confirmation, shared phones, no ABC credits),
**Northfield** (admission burst + past excess intake), **Lakeview** (messy data + double-enrolment attempts),
nine well-behaved peers (no false alarms).

### 8-minute live script

1. **Institution → Seat ledger** (`registrar@dit.demo`): B.Tech CSE regular is **59/60**, the chart is the slide-3 curve with the 90 % alert line and 100 % stop line.
2. **New claim → "Valid student (Pooja Deshpande)"** → *Awaiting student*, ledger now 60/60.
3. **"Another valid student (Imran Shaikh)"** → **SEAT_LIMIT** (claim 61 blocked, live).
4. **"Already enrolled at Lakeview → CONFLICT"** → atomic APAAR claim refuses a second live claim.
5. **Claims → search "Arif"** → *Pending fix*: `Mohd Arif K.` ≈ `Mohammed Arif Khan`, DOB day/month swapped → **Apply this correction** → protocol re-runs → *Awaiting student*.
6. **Exceptions → request** 1 supernumerary seat → sign in as `reviewer@aicte.demo` → approve with a reason → the parked SEAT_LIMIT claim proceeds automatically.
7. **Student** (`ananya@student.demo`): read the OTP in the demo SMS inbox → **Confirm** → credential with QR. Click *Open verify link* (signed yes/no, no personal data).
8. **Verifier** (`hr@acme.demo`): paste the credential ID, purpose *Employment* → only 4 of 14 fields disclosed, signed answer, *Verify the platform signature*.
   Back as the student: *Who has looked at my record?* → **Revoke access** → the verifier now gets `ACCESS_REVOKED`.
9. **AICTE admin**: Dashboard (conflicts/excess blocked), **Trust Index** → expand Riverbend → every penalty links to a lead; **Leads** → decide with a reason code; **Policy simulator** → replay last year at a 0.9× intake; **Integrity** → audit-chain and Merkle checks; **Ask the data**.
   Optional: *Simulate registry outage* → new claims land in PENDING (never silently passed) → *Restore registry* → they resolve.

Other ready-made demo identities: `110000000016` Tanvi Joshi (under 18 → guardian OTP), `110000000020` (inactive APAAR ID),
and the **No APAAR yet → provisional ID** helper. The *Demo helper* buttons on the New-claim page fill these in.

---

## 3. What is implemented (mapped to the idea document)

| Slide / requirement | Implementation | Where |
|---|---|---|
| **Claim protocol** (Registry → live claim → seat ledger → student) with every exit a visible state | `PENDING_FIX · PENDING · CONFLICT · SEAT_LIMIT · AWAITING_STUDENT · VERIFIED · UNCONFIRMED · CONSENT_REFUSED · PROVISIONAL · WITHDRAWN`. Portal, bulk CSV and API share one function. | `backend/app/claims.py` |
| **Atomic claim on keyed APAAR hash** | HMAC-SHA256 of the ID + a **partial unique index** (`active = true`) as the final arbiter. Race tested with 8 parallel threads → exactly one live claim. | `models.py`, `tests/test_protocol.py` |
| **Seat ledger, alert 90 %, stop 100 %** | Conditional `UPDATE … WHERE claimed < intake + extra` (never over-allocates). 10 parallel claims on a 3-seat ledger → exactly 3 winners. | `claims._take_seat` |
| **Exception list** (dual programmes, transfers, supernumerary) | Institution requests, AICTE reviewer approves with a mandatory reason; parked claims auto-retry. | `routes.py`, `claims.decide_exception` |
| **Indic-aware name + DOB matching, field-level fixes** | Fellegi–Sunter log-likelihood weights over name (token alignment, initials, phonetic skeleton, `Mohd→Mohammed`) and DOB (exact / day-month swap / one-field). Weights are shown to the user. | `matching.py` |
| **Anti-enumeration** | Registry values are suggested **only** when the match is probable, so an institution cannot harvest names/DOBs by probing IDs. | `matching.match` |
| **Provisional ID** | Reason + 30-day deadline, seat still counted, resolves without double-counting; overdue tracked. | `claims.py` |
| **Registry outage ⇒ PENDING** | Adapter contract with a mock; outage switch; auto/manual retry. | `claims.registry_lookup` |
| **Triple-Lock** | Lock 1 institution, Lock 2 registry, Lock 3 student: OTP (hashed, 15-min expiry, 5 attempts, resend), DigiLocker (**mock**), guardian OTP for under-18s, **consent refusal** with alternate token (still counted). 30-day sweep → `UNCONFIRMED`. | `claims.py` |
| **Signed credentials, selective disclosure** | Ed25519-signed envelope of **salted field digests**; the wallet discloses a purpose-bound subset; verifier can recompute digests. QR → signed yes/no with no PII. | `credentials.py` |
| **Student control** | Wallet, access log ("who has looked"), per-organisation revoke/restore. | `pages/Student.tsx` |
| **Bulk CSV** | Background job, per-row result, bad rows reported (never dropped), idempotent. | `routes._process_job` |
| **Intelligence: leads, not verdicts** | Rules · peer baselines (robust z) · Isolation Forest · shared-contact graph (NetworkX) · ABC-credit liveness · lead queue with reason-coded decisions (labels). | `intelligence.py` |
| **Trust Index** | Transparent penalty points; each links to the lead that caused it. | `intelligence.trust_index` |
| **Policy simulator** | Replays the previous cycle's imported admissions under a proposed cap/duplicate rule; reports the cost (real students turned away). | `analytics.simulate` |
| **Tamper evidence** | Hash-chained audit log; Merkle cohort roots signed with Ed25519; per-record inclusion proofs. Tests edit rows directly in the DB and prove detection. | `audit.py`, `merkle.py`, `analytics.py` |
| **Privacy** | Phone/email encrypted at rest (Fernet), keyed hashes for matching, reports suppress groups < 5, student-level drill-down requires a logged reason, verifiers get only purpose-bound fields. | `security.py`, `analytics.py` |
| **Auth** | PBKDF2 passwords, JWT, **TOTP MFA** for AICTE roles, lockout after 5 failures, RBAC by role + ABAC by institution/APAAR owner, rate limiting, idempotency keys. | `security.py`, `routes.py` |
| **Natural-language query** | Rule-based intent matcher over the same aggregates (no LLM). | `analytics.nl_query` |

---

## 4. Tests — what was actually run

| Suite | Command | Result at last run |
|---|---|---|
| Backend (pytest) | `cd backend && .venv/Scripts/python -m pytest tests -q` | **46 passed** (~20 s) |
| Frontend (vitest) | `cd frontend && npm test` | **13 passed** |
| Type check + prod build | `cd frontend && npm run build` | passes |

Backend coverage: the full protocol and every state transition, concurrency/atomicity (parallel claims), matching
(Mohd/Mohammed, swaps, transliteration), OTP rules, exceptions, bulk CSV, RBAC/ABAC isolation between institutions,
student isolation, MFA and lockout, signed-answer tampering, selective-disclosure tampering, revoked access, audit-chain
tampering, Merkle tampering + inclusion proofs, drill-down audit, report suppression, and an **integration test that runs the
real seed** and asserts that the planted anomalies are found (Riverbend ≤ 60, no false alarms ≥ 85 on the nine healthy
institutions, slide-3 curve reproduced exactly).
Frontend: API client, login + MFA, verifier, OTP wallet, fix-it dialog, chart.
The UI was also exercised by hand in a browser (sign-in with MFA, dashboards, Trust Index, simulator, claim submission,
SEAT_LIMIT, fix-it, student OTP, QR page, verifier, exception approval, mobile width). Not hand-tested in a browser:
bulk-upload UI (the API is tested), lead-decision dialog, report CSV download.

---

## 5. Architecture

```
React + TS (Vite) ──/api──▶ FastAPI ─┬─ claims / matching / credentials   (domain, no HTTP)
 institution · AICTE ·               ├─ intelligence / analytics / audit / merkle
 student wallet · verifier           └─ SQLAlchemy ─▶ SQLite (dev) | PostgreSQL (compose)
```
`backend/app`: `claims.py` (protocol) · `matching.py` · `credentials.py` · `intelligence.py` · `analytics.py` ·
`audit.py` · `merkle.py` · `security.py` · `routes.py` (HTTP) · `seed.py`.

---

## Limitations (read this before demoing)

**Mocked — clearly not real:**
* **APAAR registry** — a `registry_records` table behind an adapter contract (`claims.registry_lookup`). The real validation API is not public.
* **SMS gateway** — OTPs are written to an `outbox` table and shown in the student's demo inbox (`DEMO_MODE`). Institutions can never see them.
* **DigiLocker consent** and **ABC credits** — mocked (DigiLocker accepts any authenticated owner; ABC is a number on the registry row).
* **Demo helpers** (`/api/auth/demo-mfa`, `/api/dev/outbox`, demo-account list) exist only while `DEMO_MODE=true`.

**Deviations from the deck's tech stack (and why):**
| Deck | Here |
|---|---|
| Keycloak OAuth2 | Built-in JWT + PBKDF2 + TOTP (self-contained demo). Swap for Keycloak at the gateway in production. |
| Redis queue + workers | FastAPI `BackgroundTasks` for bulk jobs. Fine for a demo; a real queue is needed for 1M-row loads. |
| Splink | Own Fellegi–Sunter implementation with fixed expert m/u probabilities (no EM training, no labelled data). |
| SHAP | Isolation Forest drivers are explained by standardised-feature deviation, not SHAP values. |
| W3C VC 2.0 + SD-JWT | A simplified salted-digest scheme with the same idea; not wire-compatible with SD-JWT/VC. The wallet's salts are stored server-side. |
| PostgreSQL row-level security | Authorisation is enforced in the API (RBAC/ABAC), not by RLS. |
| Kubernetes / Grafana | Not built. |

**Other honest caveats**
* Not tested: Docker, PostgreSQL, load at 10K–1M records, multi-process rate limiting (the limiter is per process), browsers other than the built-in one.
* The deck's "3 of ~40 fields shared per verification" is, in this build, **3–5 of 14 fields** (e.g. Employment shares 4 and withholds 10).
* Match probabilities come from hand-set priors, so treat the percentage as a ranking aid, not a calibrated probability. A pass needs name ≥ 0.85 *and* exact DOB regardless.
* Intelligence outputs are leads and a transparent score; nothing blocks an institution automatically except the two deterministic rules (live-claim uniqueness and the seat ledger).
* The Trust Index weights and thresholds are illustrative; they must be agreed with AICTE and checked against labelled outcomes (reviewer decisions are stored as labels for that purpose).
* Security is prototype-grade: tokens live in `localStorage`, there is no CSRF/CSP hardening beyond nginx headers, and secrets come from environment variables.
