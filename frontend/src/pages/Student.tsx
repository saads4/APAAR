import { useState } from "react";
import { QRCodeSVG } from "qrcode.react";
import { api, Dict } from "../api";
import { Alert, Card, fmtDate, fmtDateTime, KV, Spinner, StateBadge, useAction, useLoad } from "../ui";

export default function Wallet() {
  const w = useLoad(() => api.get("/api/student/wallet"), []);
  const log = useLoad(() => api.get("/api/student/access-log"), []);
  const claims: Dict[] = w.data?.claims || [];
  const refresh = () => { w.reload(); log.reload(); };
  if (w.loading && !w.data) return <Spinner />;
  return (
    <>
      <h1>My wallet</h1>
      <p className="muted">You see every admission claimed against your APAAR ID, confirm or refuse it, and control who may check your credential.</p>
      {w.error && <Alert kind="err">{w.error}</Alert>}
      {claims.length === 0 && <Card><p className="muted">No institution has claimed your APAAR ID yet.</p></Card>}
      {claims.map((c) => <ClaimCard key={c.id} c={c} onChanged={refresh} />)}
      <Card title="Who has looked at my record?">
        {(log.data?.events || []).length === 0 ? <p className="muted">Nobody yet.</p> : (
          <div className="scroll"><table><thead><tr><th>When</th><th>Organisation</th><th>Purpose</th><th>Fields shared</th><th>Result</th><th></th></tr></thead>
            <tbody>{(log.data.events as Dict[]).map((e) => {
              const revoked = (log.data.revoked as Dict[]).some((r) => r.org === e.org && r.credential_id === e.credential_id);
              return (<tr key={e.id}><td>{fmtDateTime(e.at)}</td><td>{e.org}</td><td>{e.purpose}</td><td className="small">{e.fields.length ? e.fields.join(", ") : "none"}</td><td><StateBadge state={e.result === "VALID" ? "VALID" : e.result === "REVOKED" ? "REVOKED" : "WITHDRAWN"} /> <span className="small muted">{e.result}</span></td>
                <td>{e.org !== "Anonymous QR scan" && (revoked ? <RevokeBtn restore e={e} onDone={refresh} /> : <RevokeBtn e={e} onDone={refresh} />)}</td></tr>);
            })}</tbody></table></div>)}
      </Card>
    </>
  );
}

function RevokeBtn({ e, restore, onDone }: { e: Dict; restore?: boolean; onDone: () => void }) {
  const { busy, run } = useAction();
  const go = () => run(async () => { restore ? await api.del(`/api/student/revoke-access?credential_id=${e.credential_id}&org=${encodeURIComponent(e.org)}`) : await api.post("/api/student/revoke-access", { credential_id: e.credential_id, org: e.org }); onDone(); });
  return <button className={`btn sm ${restore ? "" : "danger"}`} disabled={busy} onClick={go}>{restore ? "Restore access" : "Revoke access"}</button>;
}

function ClaimCard({ c, onChanged }: { c: Dict; onChanged: () => void }) {
  const { busy, error, run } = useAction();
  const [otp, setOtp] = useState("");
  const [msg, setMsg] = useState("");
  const inbox = useLoad(() => (["AWAITING_STUDENT", "UNCONFIRMED"].includes(c.state) ? api.get(`/api/dev/outbox?claim_id=${c.id}`).catch(() => []) : Promise.resolve([])), [c.state, msg]);
  const cred = c.credential;
  const pending = ["AWAITING_STUDENT", "UNCONFIRMED"].includes(c.state);
  return (
    <Card title={<>{c.institution} <span className="muted small">· {c.programme} · {c.seat_type}</span></>} right={<StateBadge state={c.state} />}>
      <p>{c.reason}</p>
      {error && <Alert kind="err">{error}</Alert>}
      {msg && <Alert kind="ok">{msg}</Alert>}
      {pending && (<div>
        <h3>Lock 3 — confirm this admission is yours</h3>
        {(inbox.data as Dict[] | null)?.length ? <div className="otp-box" aria-label="Demo SMS inbox"><b>📱 Demo SMS inbox</b> (mock gateway — in production this arrives on your phone)<br />{(inbox.data as Dict[])[0].body}</div> : null}
        <div className="row">
          <input style={{ maxWidth: 160 }} inputMode="numeric" maxLength={6} placeholder="6-digit OTP" aria-label="OTP" value={otp} onChange={(e) => setOtp(e.target.value)} />
          <button className="btn primary" disabled={busy || otp.length !== 6} onClick={() => run(async () => { await api.post(`/api/student/claims/${c.id}/confirm`, { method: "OTP", otp }); setMsg("Confirmed. Your credential is ready."); onChanged(); })}>Confirm with OTP</button>
          <button className="btn" disabled={busy} onClick={() => run(async () => { await api.post(`/api/student/claims/${c.id}/confirm`, { method: "DIGILOCKER" }); setMsg("Confirmed via DigiLocker (mock)."); onChanged(); })}>Confirm via DigiLocker (mock)</button>
          <button className="btn" disabled={busy} onClick={() => run(async () => { await api.post(`/api/student/claims/${c.id}/resend-otp`); setMsg("A new OTP was sent."); onChanged(); })}>Resend OTP</button>
          <button className="btn danger" disabled={busy} onClick={() => confirm("Refuse APAAR consent? You are still counted for this seat and no service is denied; you receive an alternate token.") && run(async () => { await api.post(`/api/student/claims/${c.id}/refuse-consent`); onChanged(); })}>I do not consent</button>
        </div>
        <p className="small muted">Not you? Do nothing — an unconfirmed admission lowers the institution's Trust Index after 30 days.</p>
      </div>)}
      {c.state === "CONSENT_REFUSED" && <Alert kind="info">Alternate token: <b className="mono">{c.alt_token}</b>. Keep it as proof of your admission.</Alert>}
      {cred && <Credential cred={cred} c={c} />}
    </Card>
  );
}

function Credential({ cred, c }: { cred: Dict; c: Dict }) {
  const f = cred.fields;
  return (
    <div className="gap">
      <h3>Credential {cred.status !== "VALID" && <StateBadge state="REVOKED" />}</h3>
      <div className="cert">
        <div>
          <KV data={{ Name: f.full_name, "APAAR ID": f.apaar_id, "Student ID": f.student_id, Institution: f.institution, Programme: f.programme, "Admitted": f.admission_year, "Credential ID": <span className="mono">{cred.id}</span>, Issued: fmtDate(cred.issued_at) }} />
          <p className="small muted">Signed with Ed25519. A verifier sees only the fields needed for their stated purpose.</p>
        </div>
        <div style={{ textAlign: "center" }}><QRCodeSVG value={cred.verify_url} size={132} level="M" bgColor="#ffffff" fgColor="#111" includeMargin /><div className="small muted">Scan: signed yes/no</div></div>
      </div>
      <div className="row gap no-print">
        <button className="btn" onClick={() => window.print()}>Print certificate</button>
        <button className="btn" onClick={() => navigator.clipboard?.writeText(cred.id)}>Copy credential ID</button>
        <a className="btn" href={cred.verify_url}>Open verify link</a>
      </div>
      <InclusionLine id={c.id} />
    </div>
  );
}

function InclusionLine({ id }: { id: number }) {
  const { busy, error, run } = useAction();
  const [r, setR] = useState<Dict | null>(null);
  return (<div className="row small no-print"><button className="btn sm" disabled={busy} onClick={() => run(async () => setR(await api.get(`/api/claims/${id}/inclusion-proof`)))}>Prove I am in the signed national cohort root</button>
    {r && <span>{r.verifies_against_published_root ? "✔ Merkle proof verifies against the published root" : "✘ proof failed"}</span>}{error && <span style={{ color: "var(--bad)" }}>{error}</span>}</div>);
}
