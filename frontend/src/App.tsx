import { useCallback, useEffect, useState } from "react";
import { HashRouter, NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, Dict, getToken, setToken, setUnauthorizedHandler } from "./api";
import { Spinner } from "./ui";
import { AuthCtx, User } from "./auth";
import Login from "./pages/Login";
import { InstOverview, ClaimsPage, NewClaim, BulkPage, InstExceptions } from "./pages/Institution";
import { AicteDashboard, LedgerPage, TrustPage, LeadsPage, AicteExceptions, SimulatorPage, IntegrityPage, ReportsPage, AskPage } from "./pages/Aicte";
import Wallet from "./pages/Student";
import Verifier from "./pages/Verifier";
import PublicVerify from "./pages/PublicVerify";


const NAV: Record<string, [string, string][]> = {
  INSTITUTION: [["/", "Seat ledger"], ["/claims", "Claims"], ["/new-claim", "New claim"], ["/bulk", "Bulk upload"], ["/exceptions", "Exceptions"]],
  AICTE: [["/", "Dashboard"], ["/ledger", "Seat ledger"], ["/trust", "Trust Index"], ["/leads", "Leads"], ["/exceptions", "Exceptions"],
    ["/simulator", "Policy simulator"], ["/integrity", "Integrity"], ["/reports", "Reports"], ["/ask", "Ask the data"]],
  STUDENT: [["/", "My wallet"]],
  VERIFIER: [["/", "Verify a credential"]],
};
const ROLE_LABEL: Record<string, string> = { AICTE_ADMIN: "AICTE admin", AICTE_REVIEWER: "AICTE reviewer", INSTITUTION: "Institution", STUDENT: "Student", VERIFIER: "Verifier" };

function Shell({ user, signOut }: { user: User; signOut: () => void }) {
  const group = user.role.startsWith("AICTE") ? "AICTE" : user.role;
  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">
          <svg width="28" height="28" viewBox="0 0 32 32" aria-hidden><rect width="32" height="32" rx="8" fill="#4338ca" /><path d="M16 6l8 3v7c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V9z" fill="#fff" /></svg>
          <div>APAAR Sentinel<small>Claim · Lock · Record</small></div>
        </div>
        <nav className="nav" aria-label="Main">
          {NAV[group].map(([to, label]) => <NavLink key={to} to={to} end={to === "/"}>{label}</NavLink>)}
        </nav>
        <div className="who">
          <b>{user.name}</b>{ROLE_LABEL[user.role]}{user.org ? ` · ${user.org}` : ""}
          <div style={{ marginTop: 8 }}><button className="btn sm" onClick={signOut}>Sign out</button></div>
        </div>
      </aside>
      <main className="main">
        <div className="banner">Prototype · synthetic data · APAAR registry, SMS, DigiLocker and ABC are mocked.</div>
        <Routes>
          {group === "INSTITUTION" && (<>
            <Route path="/" element={<InstOverview />} /><Route path="/claims" element={<ClaimsPage />} /><Route path="/new-claim" element={<NewClaim />} />
            <Route path="/bulk" element={<BulkPage />} /><Route path="/exceptions" element={<InstExceptions />} />
          </>)}
          {group === "AICTE" && (<>
            <Route path="/" element={<AicteDashboard />} /><Route path="/ledger" element={<LedgerPage />} /><Route path="/trust" element={<TrustPage />} />
            <Route path="/leads" element={<LeadsPage />} /><Route path="/exceptions" element={<AicteExceptions />} /><Route path="/simulator" element={<SimulatorPage />} />
            <Route path="/integrity" element={<IntegrityPage />} /><Route path="/reports" element={<ReportsPage />} /><Route path="/ask" element={<AskPage />} />
          </>)}
          {group === "STUDENT" && <Route path="/" element={<Wallet />} />}
          {group === "VERIFIER" && <Route path="/" element={<Verifier />} />}
          <Route path="/verify" element={<PublicVerify />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const signOut = useCallback(() => { setToken(null); setUser(null); }, []);
  const signIn = useCallback((t: string, u: User) => { setToken(t); setUser(u); }, []);
  useEffect(() => {
    setUnauthorizedHandler(signOut);
    if (!getToken()) { setReady(true); return; }
    api.get("/api/auth/me").then((u: Dict) => setUser(u as User)).catch(() => setToken(null)).finally(() => setReady(true));
  }, [signOut]);
  if (!ready) return <Spinner />;
  return (
    <AuthCtx.Provider value={{ user, signIn, signOut }}>
      <HashRouter>
        {user ? <Shell user={user} signOut={signOut} /> : (
          <Routes><Route path="/verify" element={<div className="main" style={{ maxWidth: 640, margin: "0 auto" }}><PublicVerify /></div>} /><Route path="*" element={<Login />} /></Routes>
        )}
      </HashRouter>
    </AuthCtx.Provider>
  );
}
