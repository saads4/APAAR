import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Alert, Card, KV, Spinner, useLoad } from "../ui";

/** QR landing page: a signed yes/no and nothing else — no personal data is returned. */
export default function PublicVerify() {
  const [sp] = useSearchParams();
  const c = sp.get("c") || "", t = sp.get("t") || "";
  const r = useLoad(() => (c ? api.get(`/api/public/verify/${encodeURIComponent(c)}?t=${encodeURIComponent(t)}`) : Promise.resolve(null)), [c, t]);
  if (!c) return <Alert kind="err">This link has no credential ID.</Alert>;
  if (r.loading) return <Spinner />;
  if (r.error) return <Alert kind="err">{r.error}</Alert>;
  const p = r.data.payload;
  return (
    <Card title="APAAR Sentinel · credential check">
      <div style={{ textAlign: "center", padding: "1rem" }}>
        <div style={{ fontSize: "3rem" }} aria-hidden>{p.valid ? "✅" : "❌"}</div>
        <h1>{p.valid ? "Valid enrolment credential" : p.status === "NOT_FOUND" ? "Not recognised" : "Not valid"}</h1>
        <p className="muted">{p.valid ? "Issued by the AICTE trust layer and currently in force. No personal details are shown here; ask the student to share a purpose-bound check." : "This credential could not be confirmed."}</p>
      </div>
      <KV data={{ "Credential ID": p.credential_id, Status: p.status, "Checked at": p.checked_at, Signature: <span className="mono small">{r.data.signature.slice(0, 32)}…</span> }} />
    </Card>
  );
}
