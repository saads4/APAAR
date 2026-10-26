import { FormEvent, useState } from "react";
import { api, Dict } from "../api";
import { useAuth } from "../auth";
import { Alert, Field, useAction, useLoad } from "../ui";

const DEMO = [
  ["AICTE admin (MFA)", "admin@aicte.demo"], ["AICTE reviewer (MFA)", "reviewer@aicte.demo"],
  ["Institution: Demo Institute of Technology", "registrar@dit.demo"], ["Institution: Riverbend College (flagged)", "registrar@rce.demo"],
  ["Student: Ananya (OTP pending)", "ananya@student.demo"], ["Student: Rohan (verified, has QR)", "rohan@student.demo"],
  ["Student: Arif (wrongly entered DOB)", "arif@student.demo"], ["Verifier: Acme HR", "hr@acme.demo"], ["Verifier: Sahyadri Bank", "loans@sbank.demo"],
];

export default function Login() {
  const { signIn } = useAuth();
  const meta = useLoad(() => api.get("/api/meta"), []);
  const demo = meta.data?.demo_mode;
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfaToken, setMfaToken] = useState("");
  const [code, setCode] = useState("");
  const [mode, setMode] = useState<"login" | "register">("login");
  const [reg, setReg] = useState({ apaar_id: "", dob: "", email: "", password: "" });
  const { busy, error, run, setError } = useAction();

  const finish = (r: Dict) => signIn(r.token, r.user);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    run(async () => {
      if (mfaToken) return finish(await api.post("/api/auth/mfa", { mfa_token: mfaToken, code }));
      const r = await api.post("/api/auth/login", { email, password });
      if (r.mfa_required) { setMfaToken(r.mfa_token); setCode(""); } else finish(r);
    });
  };
  const register = (e: FormEvent) => {
    e.preventDefault();
    run(async () => finish(await api.post("/api/auth/student-register", reg)));
  };
  const demoCode = () => run(async () => setCode((await api.get(`/api/auth/demo-mfa?email=${encodeURIComponent(email)}`)).code));

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="login-hero">
          <h1>APAAR Sentinel</h1>
          <p><b style={{ color: "var(--ink)" }}>Claim · Lock · Record</b></p>
          <p>Every seat is a ledger entry, every enrolment is confirmed by three independent parties, and every verification reveals only the minimum.</p>
          <p className="small">SIH 2026 · APAAR-based Student Data Management and Verification System. Prototype with synthetic data.</p>
          {demo && (
            <div className="demo-list gap">
              <p className="small"><b style={{ color: "var(--ink)" }}>Demo accounts</b> (password <span className="mono">Demo@1234</span>)</p>
              {DEMO.map(([l, e]) => (
                <button key={e} className="btn sm" type="button" onClick={() => { setEmail(e); setPassword("Demo@1234"); setMfaToken(""); setMode("login"); setError(""); }}>{l}</button>
              ))}
            </div>
          )}
        </div>
        <div className="login-form">
          {mode === "login" ? (
            <form onSubmit={submit} aria-label="Sign in">
              <h2>{mfaToken ? "Two-factor check" : "Sign in"}</h2>
              {error && <Alert kind="err">{error}</Alert>}
              {!mfaToken ? (<>
                <Field label="Email"><input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="username" /></Field>
                <Field label="Password"><input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="current-password" /></Field>
              </>) : (<>
                <p className="muted">Enter the 6-digit code from your authenticator app.</p>
                <Field label="Authenticator code"><input inputMode="numeric" maxLength={6} value={code} onChange={(e) => setCode(e.target.value)} required autoFocus /></Field>
                {demo && <button type="button" className="btn sm" onClick={demoCode}>Demo: fetch current code</button>}
              </>)}
              <div className="row gap">
                <button className="btn primary" disabled={busy}>{busy ? "Checking…" : mfaToken ? "Verify" : "Sign in"}</button>
                {mfaToken && <button type="button" className="btn" onClick={() => { setMfaToken(""); setError(""); }}>Back</button>}
              </div>
              {!mfaToken && <p className="small muted gap">Student without an account? <a href="#/" onClick={(e) => { e.preventDefault(); setMode("register"); setError(""); }}>Register with your APAAR ID</a></p>}
            </form>
          ) : (
            <form onSubmit={register} aria-label="Student registration">
              <h2>Student registration</h2>
              <p className="muted">Prove you own the APAAR ID with the date of birth on the registry.</p>
              {error && <Alert kind="err">{error}</Alert>}
              <Field label="APAAR ID (12 digits)"><input value={reg.apaar_id} onChange={(e) => setReg({ ...reg, apaar_id: e.target.value })} required /></Field>
              <Field label="Date of birth (DD-MM-YYYY)"><input value={reg.dob} onChange={(e) => setReg({ ...reg, dob: e.target.value })} required placeholder="15-03-2007" /></Field>
              <Field label="Email"><input type="email" value={reg.email} onChange={(e) => setReg({ ...reg, email: e.target.value })} required /></Field>
              <Field label="Password (min 8 characters)"><input type="password" minLength={8} value={reg.password} onChange={(e) => setReg({ ...reg, password: e.target.value })} required /></Field>
              <div className="row"><button className="btn primary" disabled={busy}>Create account</button><button type="button" className="btn" onClick={() => { setMode("login"); setError(""); }}>Back to sign in</button></div>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
