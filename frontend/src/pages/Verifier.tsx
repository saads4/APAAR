import { FormEvent, useState } from "react";
import { api, Dict } from "../api";
import { Alert, Card, Field, KV, StateBadge, useAction, useLoad } from "../ui";

export default function Verifier() {
  const purposes = useLoad(() => api.get("/api/verifier/purposes"), []);
  const [id, setId] = useState("");
  const [purpose, setPurpose] = useState("EMPLOYMENT");
  const [reason, setReason] = useState("");
  const [res, setRes] = useState<Dict | null>(null);
  const [sigOk, setSigOk] = useState<boolean | null>(null);
  const { busy, error, run } = useAction();
  const fields: string[] = purposes.data?.[purpose] || [];
  const submit = (e: FormEvent) => {
    e.preventDefault();
    run(async () => { setSigOk(null); setRes(await api.post("/api/verifier/verify", { credential_id: id.trim(), purpose, reason })); });
  };
  const checkSig = () => run(async () => setSigOk((await api.post("/api/public/check-signature", { payload: res!.payload, signature: res!.signature })).valid));
  const p = res?.payload;
  return (
    <>
      <h1>Verify a credential</h1>
      <p className="muted">Enter the credential ID printed on the certificate (or scan its QR for a quick yes/no). You state a purpose and receive only the fields that purpose needs. The student can see this check.</p>
      <form onSubmit={submit}><Card>
        <div className="form-grid">
          <Field label="Credential ID"><input value={id} onChange={(e) => setId(e.target.value)} placeholder="CRD-XXXXXXXXXXXX" required className="mono" /></Field>
          <Field label="Purpose"><select value={purpose} onChange={(e) => setPurpose(e.target.value)}>{Object.keys(purposes.data || { EMPLOYMENT: 1 }).map((k) => <option key={k}>{k}</option>)}</select></Field>
        </div>
        <p className="small">Fields you will receive for <b>{purpose}</b>: {fields.map((f) => <span key={f} className="badge b-brand" style={{ marginRight: 4 }}>{f}</span>)}</p>
        <Field label="Reason for this check (recorded, shown to the student)"><input value={reason} onChange={(e) => setReason(e.target.value)} required minLength={5} placeholder="e.g. Pre-employment education check, offer #123" /></Field>
        {error && <Alert kind="err">{error}</Alert>}
        <button className="btn primary" disabled={busy}>Verify</button>
      </Card></form>
      {p && (
        <Card title={<>Signed answer: {p.valid ? <span className="badge b-ok">YES — valid</span> : <span className="badge b-bad">NO — {p.status}</span>}</>}>
          {p.note && <Alert kind="warn">{p.note}</Alert>}
          {p.valid && <KV data={Object.fromEntries(Object.entries(p.disclosed).map(([k, v]) => [k, String(v)]))} />}
          {p.valid && <p className="small muted">{p.fields_withheld} other fields were withheld from you.</p>}
          <div className="row gap">
            <button className="btn" onClick={checkSig} disabled={busy}>Verify the platform signature</button>
            {sigOk !== null && (sigOk ? <StateBadge state="VALID" /> : <StateBadge state="REVOKED" />)}
          </div>
          <details className="gap"><summary>Signed payload (Ed25519)</summary><pre className="json">{JSON.stringify(res, null, 2)}</pre></details>
        </Card>)}
    </>
  );
}
