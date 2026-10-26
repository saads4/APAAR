import { FormEvent, useEffect, useRef, useState } from "react";
import { api, Dict, qs } from "../api";
import { Alert, Bar, Card, Field, fmtDate, fmtDateTime, KV, Modal, SeatChart, Spinner, Stat, StateBadge, useAction, useLoad } from "../ui";

const STATES = ["", "AWAITING_STUDENT", "VERIFIED", "UNCONFIRMED", "PENDING_FIX", "PENDING", "CONFLICT", "SEAT_LIMIT", "PROVISIONAL", "CONSENT_REFUSED", "WITHDRAWN"];

// -------------------------------------------------------------------- overview
export function InstOverview() {
  const ledger = useLoad(() => api.get("/api/ledger"), []);
  const alerts = useLoad(() => api.get("/api/my/alerts"), []);
  const [sel, setSel] = useState<Dict | null>(null);
  const tl = useLoad(() => (sel ? api.get(`/api/ledger/timeline?${qs({ programme_id: sel.programme_id, seat_type: sel.seat_type })}`) : Promise.resolve(null)), [sel?.ledger_id]);
  useEffect(() => { if (!sel && ledger.data?.length) setSel([...ledger.data].sort((a: Dict, b: Dict) => b.claimed - a.claimed)[0]); }, [ledger.data, sel]);
  if (ledger.loading && !ledger.data) return <Spinner />;
  const rows: Dict[] = ledger.data || [];
  return (
    <>
      <h1>Seat ledger</h1>
      <p className="muted">Each seat is a ledger entry. Alert at 90%, hard stop at 100% — decided live at claim time, not found in an annual audit.</p>
      {ledger.error && <Alert kind="err">{ledger.error}</Alert>}
      <div className="grid g4">
        <Stat label="Seats claimed" value={rows.reduce((a, r) => a + r.claimed, 0)} note={`of ${rows.reduce((a, r) => a + r.cap, 0)} approved`} />
        <Stat label="Ledgers at ≥90%" value={rows.filter((r) => r.status === "ALERT").length} tone="warn" />
        <Stat label="Ledgers full" value={rows.filter((r) => r.status === "FULL").length} tone="bad" />
      </div>
      <div className="grid g2 gap">
        <Card title="Programmes and seat types">
          <div className="scroll"><table>
            <thead><tr><th>Programme</th><th>Seat</th><th>Claimed</th><th></th><th>Status</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.ledger_id} className="click" onClick={() => setSel(r)} aria-selected={sel?.ledger_id === r.ledger_id} style={sel?.ledger_id === r.ledger_id ? { background: "var(--brand-soft)" } : undefined}>
                <td>{r.programme}</td><td>{r.seat_type}</td><td>{r.claimed}/{r.cap}{r.extra ? <span className="small muted"> (+{r.extra} exc.)</span> : null}</td>
                <td style={{ minWidth: 100 }}><Bar pct={r.pct} /></td><td><StateBadge state={r.status} /></td>
              </tr>))}</tbody>
          </table></div>
        </Card>
        <Card title={sel ? `Claims by admission day · ${sel.programme} / ${sel.seat_type}` : "Timeline"}>
          {tl.loading ? <Spinner /> : tl.data && <>
            <SeatChart days={tl.data.days} cumulative={tl.data.cumulative} alertAt={tl.data.alert_at} stopAt={tl.data.stop_at} />
            {tl.data.blocked > 0 && <p className="small muted">{tl.data.blocked} claim(s) blocked by SEAT_LIMIT.</p>}
          </>}
        </Card>
      </div>
      <Card title="Alerts">
        {(alerts.data || []).length === 0 ? <span className="muted">No alerts.</span> :
          (alerts.data as Dict[]).map((a) => <div key={a.id} className="row small" style={{ marginBottom: 4 }}><StateBadge state={a.kind === "STOP_100" ? "FULL" : "ALERT"} /><span className="grow">{a.message}</span><span className="muted">{fmtDateTime(a.at)}</span></div>)}
      </Card>
    </>
  );
}

// -------------------------------------------------------------------- claims list + detail
export function ClaimsPage() {
  const [state, setState] = useState("");
  const [q, setQ] = useState("");
  const [qDeb, setQDeb] = useState("");
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const PAGE = 25;
  useEffect(() => { const t = setTimeout(() => { setQDeb(q); setPage(0); }, 250); return () => clearTimeout(t); }, [q]);
  const list = useLoad(() => api.get(`/api/claims?${qs({ state, q: qDeb, limit: PAGE, offset: page * PAGE })}`), [state, qDeb, page]);
  const items: Dict[] = list.data?.items || [];
  return (
    <>
      <h1>Claims</h1>
      <Card>
        <div className="row">
          <div style={{ minWidth: 200 }}><label htmlFor="f-state">State</label>
            <select id="f-state" value={state} onChange={(e) => { setState(e.target.value); setPage(0); }}>{STATES.map((s) => <option key={s} value={s}>{s || "All states"}</option>)}</select></div>
          <div className="grow" style={{ minWidth: 220 }}><label htmlFor="f-q">Search name / enrolment no. / APAAR tail</label>
            <input id="f-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. Arif" /></div>
          <div className="muted small" style={{ alignSelf: "end" }}>{list.data ? `${list.data.total} claims` : ""}</div>
        </div>
      </Card>
      {list.error && <Alert kind="err">{list.error}</Alert>}
      <Card>
        {list.loading && !list.data ? <Spinner /> : items.length === 0 ? <p className="muted">No claims match.</p> : (
          <div className="scroll"><table>
            <thead><tr><th>#</th><th>Student</th><th>APAAR</th><th>Programme</th><th>Seat</th><th>State</th><th>Admitted</th></tr></thead>
            <tbody>{items.map((c) => (
              <tr key={c.id} className="click" onClick={() => setOpen(c.id)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setOpen(c.id)}>
                <td>{c.id}</td><td>{c.student_name}<div className="small muted">{c.student_ref}</div></td><td className="mono small">{c.apaar_masked}</td>
                <td>{c.programme}</td><td>{c.seat_type}</td><td><StateBadge state={c.state} />{c.fixes?.length ? <span className="small muted"> · fix available</span> : null}</td><td>{fmtDate(c.admitted_on)}</td>
              </tr>))}</tbody>
          </table></div>
        )}
        <div className="row gap">
          <button className="btn sm" disabled={page === 0} onClick={() => setPage(page - 1)}>← Prev</button>
          <span className="small muted">Page {page + 1}</span>
          <button className="btn sm" disabled={!list.data || (page + 1) * PAGE >= list.data.total} onClick={() => setPage(page + 1)}>Next →</button>
        </div>
      </Card>
      {open !== null && <ClaimDetail id={open} onClose={() => setOpen(null)} onChanged={list.reload} />}
    </>
  );
}

export function ClaimDetail({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const det = useLoad(() => api.get(`/api/claims/${id}`), [id]);
  const { busy, error, run } = useAction();
  const [edit, setEdit] = useState({ apaar_id: "", student_name: "", dob: "" });
  const [reason, setReason] = useState("");
  const [showWithdraw, setShowWithdraw] = useState(false);
  const [note, setNote] = useState("");
  const c: Dict | null = det.data;
  const act = (fn: () => Promise<any>, msg: string) => run(async () => { const r = await fn(); det.setData(r?.id ? r : det.data); setNote(msg); det.reload(); onChanged(); });
  if (!c) return <Modal title={`Claim #${id}`} onClose={onClose}>{det.error ? <Alert kind="err">{det.error}</Alert> : <Spinner />}</Modal>;
  const md = c.match_detail;
  return (
    <Modal title={`Claim #${c.id} · ${c.student_name}`} onClose={onClose}>
      <div className="row"><StateBadge state={c.state} /><span className="muted small">{c.programme} · {c.seat_type}</span></div>
      <p>{c.reason}</p>
      {error && <Alert kind="err">{error}</Alert>}
      {note && <Alert kind="ok">{note}</Alert>}
      <KV data={{ "Enrolment no.": c.student_ref, APAAR: c.apaar_masked, "Date of birth": fmtDate(c.dob), Phone: c.phone_masked, Admitted: fmtDate(c.admitted_on),
        ...(c.provisional_deadline ? { "Provisional deadline": fmtDate(c.provisional_deadline) } : {}), ...(c.confirm_method ? { "Confirmed via": c.confirm_method } : {}),
        ...(c.alt_token ? { "Alternate token": c.alt_token } : {}), ...(c.conflict_with ? { "Conflicts with claim": `#${c.conflict_with} (another live claim)` } : {}) }} />
      {md && <details className="gap"><summary>Why the matcher decided this</summary>
        <p className="small">Match probability <b>{(Math.min(c.match_prob, 0.999) * 100).toFixed(1)}%</b>. Name: {md.name_level} (similarity {md.name_score}, {md.weights_bits.name} bits). DOB: {md.dob_level} ({md.weights_bits.dob} bits).
          Weights come from Fellegi–Sunter m/u probabilities; they are shown so a person can check the decision.</p></details>}
      {c.state === "PENDING_FIX" && (
        <div className="gap">
          <h3>Fix it</h3>
          {c.fixes?.length ? c.fixes.map((f: Dict) => (
            <div key={f.field} className="alert warn">
              <b>{f.field === "dob" ? "Date of birth" : "Name"}</b>: you entered <span className="mono">{f.submitted}</span>; registry suggests <span className="mono">{f.suggested}</span> ({f.reason}).
              <div className="gap"><button className="btn sm primary" disabled={busy} onClick={() => act(() => api.post(`/api/claims/${c.id}/apply-fixes`, { fields: [f.field] }), "Correction applied and the protocol re-run.")}>Apply this correction</button></div>
            </div>)) : <Alert kind="info">No safe suggestion: the details do not look like the person behind this APAAR ID. Check the number or the student's details below.</Alert>}
        </div>)}
      {["PENDING_FIX", "PENDING", "PROVISIONAL", "CONFLICT", "SEAT_LIMIT"].includes(c.state) && (
        <details className="gap" open={c.state === "PROVISIONAL"}><summary>{c.state === "PROVISIONAL" ? "Link the real APAAR ID" : "Edit details and re-run"}</summary>
          <form className="form-grid gap" onSubmit={(e: FormEvent) => { e.preventDefault(); act(() => api.patch(`/api/claims/${c.id}`, Object.fromEntries(Object.entries(edit).filter(([, v]) => v))), "Saved and protocol re-run."); }}>
            <Field label="APAAR ID"><input value={edit.apaar_id} onChange={(e) => setEdit({ ...edit, apaar_id: e.target.value })} placeholder="12 digits" /></Field>
            <Field label="Name"><input value={edit.student_name} onChange={(e) => setEdit({ ...edit, student_name: e.target.value })} placeholder={c.student_name} /></Field>
            <Field label="Date of birth"><input value={edit.dob} onChange={(e) => setEdit({ ...edit, dob: e.target.value })} placeholder="DD-MM-YYYY" /></Field>
            <div style={{ alignSelf: "end" }}><button className="btn primary" disabled={busy}>Save &amp; re-run</button></div>
          </form></details>)}
      <div className="row gap">
        {["PENDING", "SEAT_LIMIT", "CONFLICT", "PENDING_FIX"].includes(c.state) && <button className="btn" disabled={busy} onClick={() => act(() => api.post(`/api/claims/${c.id}/retry`), "Protocol re-run.")}>Re-run protocol</button>}
        {["AWAITING_STUDENT", "UNCONFIRMED"].includes(c.state) && <button className="btn" disabled={busy} onClick={() => act(() => api.post(`/api/claims/${c.id}/resend-otp`).then(() => null), "A new OTP was sent to the student's phone.")}>Resend OTP</button>}
        {!["WITHDRAWN", "CONFLICT", "SEAT_LIMIT", "PENDING_FIX", "PENDING"].includes(c.state) && <button className="btn danger" onClick={() => setShowWithdraw(!showWithdraw)}>Withdraw…</button>}
        {c.state === "VERIFIED" && <InclusionProof id={c.id} />}
      </div>
      {showWithdraw && (<div className="gap"><Field label="Reason (required, recorded in the audit log)"><input value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
        <button className="btn danger" disabled={busy || reason.trim().length < 3} onClick={() => act(() => api.post(`/api/claims/${c.id}/withdraw`, { reason }), "Claim withdrawn; the seat was released.")}>Confirm withdrawal</button></div>)}
    </Modal>
  );
}

function InclusionProof({ id }: { id: number }) {
  const { busy, error, run } = useAction();
  const [res, setRes] = useState<Dict | null>(null);
  return (<>
    <button className="btn" disabled={busy} onClick={() => run(async () => setRes(await api.get(`/api/claims/${id}/inclusion-proof`)))}>Merkle inclusion proof</button>
    {error && <span className="small" style={{ color: "var(--bad)" }}>{error}</span>}
    {res && <span className="small">{res.verifies_against_published_root ? "✔ record is inside the published, signed cohort root" : "✘ proof failed"}</span>}
  </>);
}

// -------------------------------------------------------------------- new claim
const DEMO_FILL: [string, Dict][] = [
  ["Valid student (Pooja Deshpande)", { student_name: "Pooja Deshpande", dob: "30-01-2007", apaar_id: "110000000012", phone: "9333300012" }],
  ["Another valid student (Imran Shaikh)", { student_name: "Imran Shaikh", dob: "22-08-2006", apaar_id: "110000000011", phone: "9333300011" }],
  ["Under-18 (needs guardian)", { student_name: "Tanvi Joshi", dob: "01-12-2008", apaar_id: "110000000016", phone: "9333300016", guardian_phone: "9444400016" }],
  ["Already enrolled at Lakeview → CONFLICT", { student_name: "Karan Malhotra", dob: "20-01-2006", apaar_id: "110000000004", phone: "9333300004" }],
  ["Inactive APAAR ID", { student_name: "Sunil Kamble", dob: "06-06-2005", apaar_id: "110000000020", phone: "9333300020" }],
  ["Day/month swapped DOB", { student_name: "Neha Kulkarni", dob: "05-09-2006", apaar_id: "110000000010", phone: "9333300010" }],
  ["No APAAR yet → provisional ID", { student_name: "Aarav Patil", dob: "12-08-2007", apaar_id: "", phone: "9333300099", provisional_reason: "APAAR generation pending at school" }],
];

export function NewClaim() {
  const progs = useLoad(() => api.get("/api/programmes"), []);
  const meta = useLoad(() => api.get("/api/meta"), []);
  const blank = { programme_id: "", seat_type: "REGULAR", student_name: "", dob: "", phone: "", apaar_id: "", email: "", guardian_phone: "", gender: "", category: "", provisional_reason: "" };
  const [f, setF] = useState<Dict>(blank);
  const [result, setResult] = useState<Dict | null>(null);
  const { busy, error, run } = useAction();
  const idem = useRef(crypto.randomUUID?.() ?? String(Date.now()));
  useEffect(() => { if (progs.data?.length && !f.programme_id) setF((x: Dict) => ({ ...x, programme_id: String(progs.data[0].id) })); }, [progs.data]);
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    run(async () => {
      const body: Dict = { ...f, programme_id: Number(f.programme_id) };
      Object.keys(body).forEach((k) => body[k] === "" && delete body[k]);
      // Idempotency-Key: a double-click or network retry returns the same claim instead of a second one.
      const r = await api.post("/api/claims", body, { "Idempotency-Key": idem.current });
      setResult(r);
      idem.current = crypto.randomUUID?.() ?? String(Date.now());
    });
  };
  return (
    <>
      <h1>New admission claim</h1>
      <p className="muted">Claim → registry check → live-claim check → seat ledger → student confirmation. Every outcome is a visible state.</p>
      {meta.data?.demo_mode && (
        <Card title="Demo helper"><div className="row">{DEMO_FILL.map(([l, v]) => <button key={l} className="btn sm" type="button" onClick={() => { setF({ ...f, ...blank, programme_id: f.programme_id, ...v }); setResult(null); }}>{l}</button>)}</div></Card>)}
      <form onSubmit={submit} aria-label="New claim">
        <Card>
          <div className="form-grid">
            <Field label="Programme"><select value={f.programme_id} onChange={set("programme_id")} required>{(progs.data || []).map((p: Dict) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
            <Field label="Seat type"><select value={f.seat_type} onChange={set("seat_type")}>{["REGULAR", "EWS", "LATERAL", "SUPERNUMERARY"].map((s) => <option key={s}>{s}</option>)}</select></Field>
            <Field label="Student name"><input value={f.student_name} onChange={set("student_name")} required /></Field>
            <Field label="Date of birth (DD-MM-YYYY)"><input value={f.dob} onChange={set("dob")} required placeholder="15-03-2007" /></Field>
            <Field label="APAAR ID (12 digits)"><input value={f.apaar_id} onChange={set("apaar_id")} placeholder="leave empty for a provisional ID" inputMode="numeric" /></Field>
            <Field label="Student mobile (for OTP)"><input value={f.phone} onChange={set("phone")} required inputMode="tel" /></Field>
            <Field label="Guardian mobile (required if under 18)"><input value={f.guardian_phone} onChange={set("guardian_phone")} inputMode="tel" /></Field>
            <Field label="Email"><input type="email" value={f.email} onChange={set("email")} /></Field>
            <Field label="Gender"><select value={f.gender} onChange={set("gender")}><option value="">—</option><option>F</option><option>M</option><option>O</option></select></Field>
            <Field label="Category"><select value={f.category} onChange={set("category")}><option value="">—</option>{["GEN", "OBC", "SC", "ST", "EWS"].map((s) => <option key={s}>{s}</option>)}</select></Field>
          </div>
          {!f.apaar_id.trim() && <Field label="Why is there no APAAR ID? (provisional ID, 30-day deadline)"><input value={f.provisional_reason} onChange={set("provisional_reason")} required /></Field>}
          {error && <Alert kind="err">{error}</Alert>}
          <button className="btn primary" disabled={busy}>{busy ? "Running protocol…" : "Submit claim"}</button>
        </Card>
      </form>
      {result && (
        <Card title={<>Result: <StateBadge state={result.state} /></>}>
          <p>{result.reason}</p>
          {result.fixes?.length > 0 && <Alert kind="warn">Suggested correction: {result.fixes.map((x: Dict) => `${x.field} ${x.submitted} → ${x.suggested}`).join("; ")}. Open it from the Claims page to apply.</Alert>}
          <KV data={{ "Claim #": result.id, "Enrolment no.": result.student_ref, APAAR: result.apaar_masked, "Match probability": result.match_prob != null ? `${(Math.min(result.match_prob, 0.999) * 100).toFixed(1)}%` : "—" }} />
        </Card>)}
    </>
  );
}

// -------------------------------------------------------------------- bulk upload
export function BulkPage() {
  const [file, setFile] = useState<File | null>(null);
  const [job, setJob] = useState<Dict | null>(null);
  const { busy, error, run } = useAction();
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearInterval(timer.current), []);
  const poll = (id: number) => {
    window.clearInterval(timer.current);
    timer.current = window.setInterval(async () => {
      try { const j = await api.get(`/api/claims/bulk/${id}`); setJob(j); if (j.status === "DONE" || j.status === "FAILED") window.clearInterval(timer.current); } catch { window.clearInterval(timer.current); }
    }, 700);
  };
  const upload = () => run(async () => { const r = await api.upload("/api/claims/bulk", file!); setJob({ status: "QUEUED", total: r.total, processed: 0, summary: {}, results: [] }); poll(r.job_id); });
  return (
    <>
      <h1>Bulk upload (CSV)</h1>
      <p className="muted">Same claim protocol as the form: every row ends in a visible state — nothing is silently dropped or passed.</p>
      <Card>
        <p>Required columns: <span className="mono">student_name, dob, phone, programme</span>. Optional: <span className="mono">apaar_id, email, seat_type, gender, category, guardian_phone, provisional_reason</span>.
          {" "}<a href="/api/claims-template.csv" download="claims_template.csv">Download template</a></p>
        <div className="row"><input type="file" accept=".csv,text/csv" style={{ maxWidth: 360 }} aria-label="CSV file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          <button className="btn primary" disabled={!file || busy} onClick={upload}>Upload &amp; process</button></div>
        {error && <div className="gap"><Alert kind="err">{error}</Alert></div>}
      </Card>
      {job && (<Card title={`Job ${job.status}`}>
        <Bar pct={job.total ? (100 * job.processed) / job.total : 0} />
        <p className="small muted">{job.processed}/{job.total} rows processed</p>
        <div className="row">{Object.entries(job.summary || {}).map(([k, v]) => <span key={k}><StateBadge state={k} /> × {String(v)}</span>)}</div>
        {job.results?.length > 0 && <div className="scroll gap" style={{ maxHeight: 360 }}><table><thead><tr><th>Row</th><th>Student</th><th>Result</th><th>Why</th></tr></thead>
          <tbody>{job.results.map((r: Dict) => <tr key={r.row}><td>{r.row}</td><td>{r.student_name}</td><td><StateBadge state={r.state} /></td><td className="small">{r.reason}</td></tr>)}</tbody></table></div>}
      </Card>)}
    </>
  );
}

// -------------------------------------------------------------------- exceptions
export function InstExceptions() {
  const progs = useLoad(() => api.get("/api/programmes"), []);
  const list = useLoad(() => api.get("/api/exceptions"), []);
  const [f, setF] = useState<Dict>({ programme_id: "", seat_type: "REGULAR", category: "SUPERNUMERARY", seats: 1, justification: "" });
  const { busy, error, run } = useAction();
  useEffect(() => { if (progs.data?.length && !f.programme_id) setF((x: Dict) => ({ ...x, programme_id: String(progs.data[0].id) })); }, [progs.data]);
  const submit = (e: FormEvent) => { e.preventDefault(); run(async () => { await api.post("/api/exceptions", { ...f, programme_id: Number(f.programme_id), seats: Number(f.seats) }); setF({ ...f, justification: "" }); list.reload(); }); };
  return (
    <>
      <h1>Seat exceptions</h1>
      <p className="muted">The 100% stop is a hard stop. Legitimate cases (dual programmes, transfers, supernumerary seats) are approved by an AICTE reviewer with a recorded reason; parked SEAT_LIMIT claims then proceed automatically.</p>
      <form onSubmit={submit}><Card title="Request an exception">
        <div className="form-grid">
          <Field label="Programme"><select value={f.programme_id} onChange={(e) => setF({ ...f, programme_id: e.target.value })}>{(progs.data || []).map((p: Dict) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
          <Field label="Seat type"><select value={f.seat_type} onChange={(e) => setF({ ...f, seat_type: e.target.value })}>{["REGULAR", "EWS", "LATERAL", "SUPERNUMERARY"].map((s) => <option key={s}>{s}</option>)}</select></Field>
          <Field label="Category"><select value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })}>{["DUAL_PROGRAMME", "TRANSFER", "SUPERNUMERARY", "OTHER"].map((s) => <option key={s}>{s}</option>)}</select></Field>
          <Field label="Extra seats"><input type="number" min={1} max={50} value={f.seats} onChange={(e) => setF({ ...f, seats: e.target.value })} /></Field>
        </div>
        <Field label="Justification (min 10 characters)"><textarea rows={2} value={f.justification} onChange={(e) => setF({ ...f, justification: e.target.value })} required minLength={10} /></Field>
        {error && <Alert kind="err">{error}</Alert>}
        <button className="btn primary" disabled={busy}>Submit request</button>
      </Card></form>
      <Card title="My requests"><ExceptionTable rows={list.data || []} /></Card>
    </>
  );
}

export function ExceptionTable({ rows, onDecide }: { rows: Dict[]; onDecide?: (e: Dict) => void }) {
  if (!rows.length) return <p className="muted">None.</p>;
  return (<div className="scroll"><table><thead><tr><th>#</th><th>Institution</th><th>Programme</th><th>Seat</th><th>Type</th><th>Seats</th><th>Justification</th><th>State</th>{onDecide && <th></th>}</tr></thead>
    <tbody>{rows.map((e) => (<tr key={e.id}><td>{e.id}</td><td>{e.institution}</td><td>{e.programme}</td><td>{e.seat_type}</td><td>{e.category}</td><td>{e.seats}</td>
      <td className="small">{e.justification}{e.reviewer_reason && <div className="muted">Reviewer: {e.reviewer_reason}</div>}</td>
      <td><StateBadge state={e.state === "PENDING" ? "PENDING_EXC" : e.state} /></td>{onDecide && <td>{e.state === "PENDING" && <button className="btn sm primary" onClick={() => onDecide(e)}>Decide</button>}</td>}</tr>))}</tbody></table></div>);
}
