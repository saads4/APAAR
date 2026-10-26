import { FormEvent, Fragment, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, Dict, qs } from "../api";
import { useAuth } from "../auth";
import { Alert, Bar, Card, CompareChart, Field, fmtDateTime, HBar, KV, Modal, SeatChart, Spinner, Stat, StateBadge, useAction, useLoad } from "../ui";
import { ExceptionTable } from "./Institution";

// -------------------------------------------------------------------- dashboard
export function AicteDashboard() {
  const { user } = useAuth();
  const d = useLoad(() => api.get("/api/aicte/dashboard"), []);
  const { busy, error, run } = useAction();
  const [msg, setMsg] = useState("");
  const admin = user?.role === "AICTE_ADMIN";
  const act = (fn: () => Promise<any>, f: (r: any) => string) => run(async () => { const r = await fn(); setMsg(f(r)); d.reload(); });
  if (d.loading && !d.data) return <Spinner />;
  const x: Dict = d.data;
  if (!x) return <Alert kind="err">{d.error}</Alert>;
  const s = x.claims_by_state;
  return (
    <>
      <h1>National dashboard <span className="muted small">admission cycle {x.cycle}</span></h1>
      <p className="muted">Aggregates by default. Student-level drill-down needs a logged reason.</p>
      {x.registry_outage && <Alert kind="warn"><b>Registry outage simulated.</b> New claims land in PENDING and are retried — never silently passed.</Alert>}
      <div className="grid g4">
        <Stat label="Students verified (3 locks)" value={s.VERIFIED ?? 0} tone="ok" note={`${s.AWAITING_STUDENT ?? 0} awaiting student · ${s.UNCONFIRMED ?? 0} unconfirmed`} />
        <Stat label="Seats claimed" value={`${x.utilisation_pct}%`} note={`${x.seats_claimed} of ${x.seats_cap} approved`} />
        <Stat label="Double enrolments blocked" value={x.prevented.double_enrolments_blocked} tone="bad" note="CONFLICT at claim time" />
        <Stat label="Excess admissions blocked" value={x.prevented.excess_admissions_blocked} tone="bad" note="SEAT_LIMIT at claim time" />
        <Stat label="Ledgers ≥90% / full" value={`${x.ledger_alerts} / ${x.ledger_full}`} tone="warn" />
        <Stat label="Open leads" value={x.open_leads} note="evidence for reviewers" />
        <Stat label="Exceptions pending" value={x.pending_exceptions} />
        <Stat label="Institutions" value={x.institutions} />
      </div>
      <div className="grid g2 gap">
        <Card title="Claims by state"><HBar rows={Object.entries(s).map(([k, v]) => ({ label: k.replace(/_/g, " ").toLowerCase(), value: v as number })).sort((a, b) => b.value - a.value)} /></Card>
        <Card title="Recent seat alerts">{x.recent_alerts.length === 0 ? <span className="muted">None</span> : x.recent_alerts.map((a: Dict) => (
          <div key={a.id} className="row small" style={{ marginBottom: 4 }}><StateBadge state={a.kind === "STOP_100" ? "FULL" : "ALERT"} /><span className="grow">{a.message}</span><span className="muted">{fmtDateTime(a.at)}</span></div>))}</Card>
      </div>
      <Card title="Operations" right={!admin && <span className="small muted">Admin only</span>}>
        {error && <Alert kind="err">{error}</Alert>}{msg && <Alert kind="ok">{msg}</Alert>}
        <div className="row">
          <button className="btn" disabled={!admin || busy} onClick={() => act(() => api.post("/api/aicte/registry-outage", { outage: !x.registry_outage }), (r) => r.outage ? "Registry outage simulated." : `Registry restored; ${r.retry.resolved ?? 0} pending claims resolved on retry.`)}>{x.registry_outage ? "Restore registry" : "Simulate registry outage"}</button>
          <button className="btn" disabled={!admin || busy} onClick={() => act(() => api.post("/api/aicte/retry-pending"), (r) => `Retried ${r.retried} claims; ${r.resolved} changed state.`)}>Retry pending claims</button>
          <button className="btn" disabled={!admin || busy} onClick={() => act(() => api.post("/api/aicte/sweep", { simulate_days_ahead: 0 }), (r) => `${r.marked_unconfirmed} claims marked UNCONFIRMED (30-day rule); ${r.provisional_overdue} provisional IDs overdue.`)}>Run 30-day sweep</button>
          <button className="btn" disabled={busy} onClick={() => act(() => api.post("/api/aicte/intelligence/run"), (r) => `Intelligence run: ${r.leads_total} leads on file.`)}>Re-run intelligence</button>
        </div>
      </Card>
    </>
  );
}

// -------------------------------------------------------------------- ledger
export function LedgerPage() {
  const rows = useLoad(() => api.get("/api/aicte/ledger"), []);
  const [filter, setFilter] = useState("");
  const [only, setOnly] = useState("");
  const [sel, setSel] = useState<Dict | null>(null);
  const tl = useLoad(() => (sel ? api.get(`/api/ledger/timeline?${qs({ programme_id: sel.programme_id, seat_type: sel.seat_type })}`) : Promise.resolve(null)), [sel?.ledger_id]);
  const list: Dict[] = (rows.data || []).filter((r: Dict) => r.institution.toLowerCase().includes(filter.toLowerCase()) && (!only || r.status === only));
  return (
    <>
      <h1>Seat ledger — all institutions</h1>
      <Card><div className="row">
        <div className="grow"><label htmlFor="lf">Institution</label><input id="lf" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter by name" /></div>
        <div><label htmlFor="ls">Status</label><select id="ls" value={only} onChange={(e) => setOnly(e.target.value)}><option value="">All</option><option value="ALERT">≥90%</option><option value="FULL">Full</option><option value="OK">OK</option></select></div>
      </div></Card>
      {rows.loading && !rows.data ? <Spinner /> : (
        <Card><div className="scroll"><table><thead><tr><th>Institution</th><th>Programme</th><th>Seat</th><th>Claimed / cap</th><th></th><th>Status</th></tr></thead>
          <tbody>{list.map((r) => (<tr key={r.ledger_id} className="click" onClick={() => setSel(r)}><td>{r.institution}</td><td>{r.programme}</td><td>{r.seat_type}</td><td>{r.claimed}/{r.cap}{r.extra ? ` (+${r.extra})` : ""}</td><td style={{ minWidth: 100 }}><Bar pct={r.pct} /></td><td><StateBadge state={r.status} /></td></tr>))}</tbody></table>
          {list.length === 0 && <p className="muted">No ledgers match.</p>}</div></Card>)}
      {sel && <Modal title={`${sel.institution} · ${sel.programme} / ${sel.seat_type}`} onClose={() => setSel(null)}>
        {tl.loading ? <Spinner /> : tl.data && <SeatChart days={tl.data.days} cumulative={tl.data.cumulative} alertAt={tl.data.alert_at} stopAt={tl.data.stop_at} />}
        <p className="small muted">{tl.data?.blocked ?? 0} claims blocked by the ledger.</p></Modal>}
    </>
  );
}

// -------------------------------------------------------------------- trust index
export function TrustPage() {
  const t = useLoad(() => api.get("/api/aicte/trust-index"), []);
  const [open, setOpen] = useState<number | null>(null);
  if (t.loading && !t.data) return <Spinner />;
  return (
    <>
      <h1>Trust Index</h1>
      <p className="muted">100 minus transparent penalty points. It is a prompt to look, not a verdict: every point links to the lead (evidence) that caused it, and reviewers record their decisions as labels.</p>
      {t.error && <Alert kind="err">{t.error}</Alert>}
      <Card><div className="scroll"><table><thead><tr><th>Institution</th><th>State</th><th>Score</th><th></th><th>Band</th><th>Seat use</th></tr></thead>
        <tbody>{(t.data || []).map((r: Dict) => (<Fragment key={r.institution_id}>
          <tr className="click" onClick={() => setOpen(open === r.institution_id ? null : r.institution_id)} aria-expanded={open === r.institution_id}>
            <td><b>{r.name}</b></td><td>{r.state}</td><td><b>{r.score}</b></td><td style={{ minWidth: 120 }}><Bar pct={r.score} good /></td><td><StateBadge state={r.band} /></td><td>{r.util != null ? Math.round(r.util * 100) + "%" : "—"}</td></tr>
          {open === r.institution_id && <tr><td colSpan={6} style={{ background: "var(--brand-soft)" }}>
            <table><thead><tr><th>Signal</th><th>Penalty</th><th>Detail</th><th>Evidence</th></tr></thead><tbody>
              {r.components.map((c: Dict) => (<tr key={c.key}><td>{c.label}</td><td style={{ color: c.penalty > 0 ? "var(--bad)" : "var(--ok)" }}>−{c.penalty}</td><td className="small">{c.detail}</td>
                <td className="small">{c.lead_ids.length ? c.lead_ids.map((i: number) => <a key={i} href={`#/leads?lead=${i}`} style={{ marginRight: 6 }}>lead #{i}</a>) : <span className="muted">—</span>}</td></tr>))}</tbody></table></td></tr>}
        </Fragment>))}</tbody></table></div></Card>
    </>
  );
}

// -------------------------------------------------------------------- leads
const REASONS = ["EXPLAINED_BY_INSTITUTION", "DATA_ERROR", "NEEDS_INSPECTION", "CONFIRMED_IRREGULARITY", "FALSE_POSITIVE", "DUPLICATE_LEAD"];
export function LeadsPage() {
  const [status, setStatus] = useState("OPEN");
  const [sel, setSel] = useState<Dict | null>(null);
  const [drill, setDrill] = useState<Dict | null>(null);
  const [sp, setSp] = useSearchParams();
  const only = sp.get("lead");
  const leads = useLoad(() => api.get(`/api/aicte/leads?${qs({ status: only ? "" : status })}`).then((r: Dict[]) => (only ? r.filter((l) => String(l.id) === only) : r)), [status, only]);
  return (
    <>
      <h1>Anomaly leads</h1>
      <p className="muted">Five layers — rules, peer baselines, Isolation Forest + shared-contact graph, liveness — produce leads with the baseline, the deviation, the evidence and a suggested action. A reviewer decides; the decision is stored as a label.</p>
      <Card><div className="row"><label htmlFor="ls" style={{ margin: 0 }}>Status</label>
        <select id="ls" value={status} onChange={(e) => setStatus(e.target.value)} style={{ maxWidth: 200 }}>{["", "OPEN", "CONFIRMED", "ESCALATED", "DISMISSED"].map((s) => <option key={s} value={s}>{s || "All"}</option>)}</select>
        <span className="muted small">{leads.data?.length ?? 0} leads</span>
        {only && <button className="btn sm" onClick={() => setSp({})}>Showing lead #{only} — show all</button>}</div></Card>
      {leads.error && <Alert kind="err">{leads.error}</Alert>}
      {leads.loading && !leads.data ? <Spinner /> : (leads.data || []).map((l: Dict) => (
        <Card key={l.id}>
          <div className="row"><StateBadge state={l.severity} /><span className="badge">{l.layer}</span><b className="grow">{l.title}</b><StateBadge state={l.status} /></div>
          <p style={{ margin: ".4rem 0" }}><b>{l.institution}</b> — {l.deviation}</p>
          <div className="grid g3 small">
            <div><span className="muted">Baseline</span><br />{l.baseline}</div><div><span className="muted">Suggested action</span><br />{l.action}</div>
            <div><span className="muted">Lead #{l.id}</span><br />{fmtDateTime(l.created_at)}</div>
          </div>
          <details className="gap"><summary>Evidence</summary><pre className="json">{JSON.stringify(l.evidence, null, 2)}</pre></details>
          {l.reason_code && <p className="small">Decision: <b>{l.status}</b> · {l.reason_code}{l.reviewer_note ? ` · ${l.reviewer_note}` : ""}</p>}
          <div className="row gap"><button className="btn sm primary" onClick={() => setSel(l)}>{l.status === "OPEN" ? "Decide…" : "Change decision…"}</button><button className="btn sm" onClick={() => setDrill(l)}>Inspect claims…</button></div>
        </Card>))}
      {!leads.loading && (leads.data || []).length === 0 && <Card><p className="muted">No leads in this view.</p></Card>}
      {sel && <DecideLead lead={sel} onClose={() => setSel(null)} onDone={() => { setSel(null); leads.reload(); }} />}
      {drill && <DrillDown inst={{ id: drill.institution_id, name: drill.institution }} onClose={() => setDrill(null)} />}
    </>
  );
}

function DecideLead({ lead, onClose, onDone }: { lead: Dict; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState({ status: "CONFIRMED", reason_code: "NEEDS_INSPECTION", note: "" });
  const { busy, error, run } = useAction();
  const submit = (e: FormEvent) => { e.preventDefault(); run(async () => { await api.post(`/api/aicte/leads/${lead.id}/decide`, f); onDone(); }); };
  return (<Modal title={`Decide lead #${lead.id}`} onClose={onClose}><form onSubmit={submit}>
    <p className="small">{lead.title} — {lead.institution}</p>
    <Field label="Decision"><select value={f.status} onChange={(e) => setF({ ...f, status: e.target.value })}><option value="CONFIRMED">Confirmed — worth action</option><option value="ESCALATED">Escalate to inspection</option><option value="DISMISSED">Dismiss</option></select></Field>
    <Field label="Reason code (becomes a training label)"><select value={f.reason_code} onChange={(e) => setF({ ...f, reason_code: e.target.value })}>{REASONS.map((r) => <option key={r}>{r}</option>)}</select></Field>
    <Field label="Note"><textarea rows={2} value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} /></Field>
    {error && <Alert kind="err">{error}</Alert>}<button className="btn primary" disabled={busy}>Save decision</button></form></Modal>);
}

function DrillDown({ inst, onClose }: { inst: { id: number; name: string }; onClose: () => void }) {
  const [reason, setReason] = useState("");
  const [rows, setRows] = useState<Dict[] | null>(null);
  const { busy, error, run } = useAction();
  return (<Modal title={`Student-level view · ${inst.name}`} onClose={onClose}>
    <Alert kind="info">Aggregates are the default. Student-level drill-down needs a reason; it is written to the tamper-evident audit log.</Alert>
    <div className="row"><input className="grow" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason (min 10 characters), e.g. Inspection planning visit 12" aria-label="Reason" />
      <button className="btn primary" disabled={busy || reason.trim().length < 10} onClick={() => run(async () => setRows(await api.get(`/api/aicte/claims?${qs({ institution_id: inst.id, reason, limit: 50 })}`)))}>Open</button></div>
    {error && <div className="gap"><Alert kind="err">{error}</Alert></div>}
    {rows && <div className="scroll gap"><table><thead><tr><th>#</th><th>Student</th><th>APAAR</th><th>State</th></tr></thead><tbody>{rows.map((r) => <tr key={r.id}><td>{r.id}</td><td>{r.student_name}</td><td className="mono small">{r.apaar_masked}</td><td><StateBadge state={r.state} /></td></tr>)}</tbody></table></div>}
  </Modal>);
}

// -------------------------------------------------------------------- exceptions
export function AicteExceptions() {
  const list = useLoad(() => api.get("/api/exceptions"), []);
  const [sel, setSel] = useState<Dict | null>(null);
  const [reason, setReason] = useState("");
  const { busy, error, run } = useAction();
  const decide = (approve: boolean) => run(async () => { await api.post(`/api/exceptions/${sel!.id}/decide`, { approve, reason }); setSel(null); setReason(""); list.reload(); });
  return (<>
    <h1>Seat exceptions</h1>
    <p className="muted">Approving adds seats to the ledger and automatically re-runs claims parked in SEAT_LIMIT. A reason is mandatory and audited.</p>
    <Card>{list.loading && !list.data ? <Spinner /> : <ExceptionTable rows={list.data || []} onDecide={setSel} />}</Card>
    {sel && <Modal title={`Exception #${sel.id}`} onClose={() => setSel(null)}>
      <KV data={{ Institution: sel.institution, Programme: `${sel.programme} / ${sel.seat_type}`, Category: sel.category, Seats: sel.seats, Justification: sel.justification }} />
      <Field label="Your reason (required)"><textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
      {error && <Alert kind="err">{error}</Alert>}
      <div className="row"><button className="btn primary" disabled={busy || !reason.trim()} onClick={() => decide(true)}>Approve</button><button className="btn danger" disabled={busy || !reason.trim()} onClick={() => decide(false)}>Reject</button></div></Modal>}
  </>);
}

// -------------------------------------------------------------------- simulator
export function SimulatorPage() {
  const [p, setP] = useState({ intake_multiplier: 1.0, stop_pct: 100, enforce_unique: true });
  const [res, setRes] = useState<Dict | null>(null);
  const { busy, error, run } = useAction();
  const go = () => run(async () => setRes(await api.post("/api/aicte/simulate", p)));
  useEffect(() => { go(); }, []); // eslint-disable-line
  return (<>
    <h1>Policy simulator</h1>
    <p className="muted">Test a rule on a past admission cycle before it goes live. Replays last cycle's imported admissions in date order under the proposed cap.</p>
    <Card><div className="form-grid">
      <Field label={`Intake multiplier: ${p.intake_multiplier.toFixed(2)}×`}><input type="range" min={0.5} max={1.5} step={0.05} value={p.intake_multiplier} onChange={(e) => setP({ ...p, intake_multiplier: Number(e.target.value) })} /></Field>
      <Field label={`Hard stop at ${p.stop_pct}% of intake`}><input type="range" min={80} max={110} step={5} value={p.stop_pct} onChange={(e) => setP({ ...p, stop_pct: Number(e.target.value) })} /></Field>
      <Field label="Atomic APAAR claim (block double enrolment)"><select value={String(p.enforce_unique)} onChange={(e) => setP({ ...p, enforce_unique: e.target.value === "true" })}><option value="true">Enforced</option><option value="false">Off</option></select></Field>
      <div style={{ alignSelf: "end" }}><button className="btn primary" disabled={busy} onClick={go}>Run simulation</button></div></div></Card>
    {error && <Alert kind="err">{error}</Alert>}
    {res && (<>
      <div className="grid g4">
        <Stat label="Past admissions replayed" value={res.total_admissions} /><Stat label="Would be blocked" value={`${res.would_block} (${res.would_block_pct}%)`} tone="bad" note={`${res.blocked_cap} over cap · ${res.blocked_duplicate} duplicates`} />
        <Stat label="Actual excess over intake" value={res.actual_excess_over_intake} /><Stat label="Real students turned away" value={res.likely_genuine_blocked} tone="warn" note="blocked students who are active in ABC: the human cost of the rule" />
      </div>
      <Card title="Admissions over the cycle: actual vs. under the proposed rule"><CompareChart curve={res.curve} /></Card>
      <Card title="Institutions affected">{res.institutions_affected.length === 0 ? <p className="muted">No admission would be blocked under these settings.</p> : (
        <div className="scroll"><table><thead><tr><th>Institution</th><th>Admitted</th><th>Blocked: over cap</th><th>Blocked: duplicate</th></tr></thead><tbody>{res.institutions_affected.map((r: Dict) => <tr key={r.institution}><td>{r.institution}</td><td>{r.admitted}</td><td>{r.blocked_cap}</td><td>{r.blocked_duplicate}</td></tr>)}</tbody></table></div>)}
        <p className="small muted">{res.note}</p></Card>
    </>)}
  </>);
}

// -------------------------------------------------------------------- integrity
export function IntegrityPage() {
  const { user } = useAuth();
  const admin = user?.role === "AICTE_ADMIN";
  const chain = useLoad(() => api.get("/api/aicte/audit/verify"), []);
  const tail = useLoad(() => api.get("/api/aicte/audit?limit=15"), []);
  const roots = useLoad(() => api.get("/api/aicte/cohort-roots/check"), []);
  const { busy, error, run } = useAction();
  return (<>
    <h1>Integrity</h1>
    <p className="muted">Tamper evidence: the audit log is a hash chain, and each institution's verified cohort is summarised in a Merkle root signed with Ed25519.</p>
    {error && <Alert kind="err">{error}</Alert>}
    <Card title="Audit chain" right={<button className="btn sm" onClick={() => { chain.reload(); tail.reload(); }}>Re-verify</button>}>
      {chain.data && (chain.data.ok ? <Alert kind="ok">✔ {chain.data.checked} audit rows verified. Head {chain.data.head.slice(0, 16)}…</Alert> : <Alert kind="err">✘ {chain.data.message}</Alert>)}
      <div className="scroll"><table><thead><tr><th>#</th><th>When</th><th>Actor</th><th>Action</th><th>Entity</th><th>Hash</th></tr></thead>
        <tbody>{(tail.data || []).map((r: Dict) => <tr key={r.id}><td>{r.id}</td><td className="small">{fmtDateTime(r.ts)}</td><td className="small">{r.actor}</td><td>{r.action}</td><td className="small">{r.entity} {r.entity_id}</td><td className="mono small">{r.hash}</td></tr>)}</tbody></table></div>
    </Card>
    <Card title="Signed cohort roots" right={<div className="row"><button className="btn sm primary" disabled={!admin || busy} onClick={() => run(async () => { await api.post("/api/aicte/cohort-roots/publish"); roots.reload(); })}>Publish new roots</button><button className="btn sm" onClick={roots.reload}>Check against live data</button></div>}>
      {!admin && <p className="small muted">Publishing is admin-only.</p>}
      <div className="scroll"><table><thead><tr><th>Institution</th><th>Verified students</th><th>Root</th><th>Signature</th><th>Status</th></tr></thead>
        <tbody>{(roots.data || []).map((r: Dict) => <tr key={r.code}><td>{r.institution}</td><td>{r.leaf_count}{r.current_leaf_count !== r.leaf_count ? ` → ${r.current_leaf_count} now` : ""}</td><td className="mono small">{r.published_root.slice(0, 16)}…</td><td>{r.signature_valid ? "✔ valid" : "✘ invalid"}</td>
          <td>{r.matches ? <StateBadge state="OK" /> : <span className="badge b-warn">{r.status}</span>}</td></tr>)}</tbody></table></div>
      <p className="small muted">"Changed since publication" is expected after new students verify; publish a new root to snapshot them. An unexplained change to a past root is what tampering looks like.</p>
    </Card>
  </>);
}

// -------------------------------------------------------------------- reports
export function ReportsPage() {
  const [g, setG] = useState("state");
  const rep = useLoad(() => api.get(`/api/aicte/report?group_by=${g}`), [g]);
  const { error, run } = useAction();
  return (<>
    <h1>Reports</h1>
    <Card><div className="row"><div><label htmlFor="g">Group by</label><select id="g" value={g} onChange={(e) => setG(e.target.value)}>{["state", "institution", "programme", "category", "seat_type", "level"].map((x) => <option key={x}>{x}</option>)}</select></div>
      <div style={{ alignSelf: "end" }}><button className="btn" onClick={() => run(() => api.download(`/api/aicte/report?group_by=${g}&format=csv`, `enrolment_by_${g}.csv`))}>Download CSV</button></div></div></Card>
    {error && <Alert kind="err">{error}</Alert>}
    <Card>{rep.loading && !rep.data ? <Spinner /> : rep.error ? <Alert kind="err">{rep.error}</Alert> : (<>
      <div className="scroll"><table><thead><tr><th>{g}</th><th>Total</th><th>Verified</th><th>Awaiting student</th><th>Needs attention</th></tr></thead>
        <tbody>{rep.data.rows.map((r: Dict) => <tr key={r.group}><td>{r.group}</td><td>{r.total}</td><td>{r.verified}</td><td>{r.awaiting_student}</td><td>{r.needs_attention}</td></tr>)}</tbody></table></div>
      <p className="small muted">{rep.data.note}</p></>)}</Card>
  </>);
}

// -------------------------------------------------------------------- ask
const EXAMPLES = ["Who exceeded intake?", "Which institutions have the lowest trust?", "How many double enrolments were blocked?", "Where are pending fixes?", "Which have low student confirmation?", "Which seats are almost full?"];
export function AskPage() {
  const [q, setQ] = useState("");
  const [res, setRes] = useState<Dict | null>(null);
  const { busy, error, run } = useAction();
  const ask = (text: string) => { setQ(text); run(async () => setRes(await api.post("/api/aicte/ask", { question: text }))); };
  return (<>
    <h1>Ask the data</h1>
    <p className="muted">Plain-language questions answered by a rule-based intent matcher over the same aggregates (no LLM; it never invents numbers).</p>
    <Card><form className="row" onSubmit={(e) => { e.preventDefault(); ask(q); }}><input className="grow" value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. who exceeded intake?" aria-label="Question" /><button className="btn primary" disabled={busy || q.trim().length < 3}>Ask</button></form>
      <div className="row gap">{EXAMPLES.map((e) => <button key={e} className="btn sm" onClick={() => ask(e)}>{e}</button>)}</div></Card>
    {error && <Alert kind="err">{error}</Alert>}
    {res && <Card title={res.answer}><span className="badge b-brand">intent: {res.intent}</span>{res.rows.length > 0 && <div className="scroll gap"><table><thead><tr>{Object.keys(res.rows[0]).map((k) => <th key={k}>{k.replace(/_/g, " ")}</th>)}</tr></thead>
      <tbody>{res.rows.map((r: Dict, i: number) => <tr key={i}>{Object.values(r).map((v, j) => <td key={j}>{String(v)}</td>)}</tr>)}</tbody></table></div>}</Card>}
  </>);
}
