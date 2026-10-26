import { ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { ApiError, Dict } from "./api";

const STATE_STYLE: Record<string, [string, string]> = {
  VERIFIED: ["b-ok", "Verified"], AWAITING_STUDENT: ["b-info", "Awaiting student"], UNCONFIRMED: ["b-warn", "Unconfirmed"],
  CONSENT_REFUSED: ["b-warn", "Consent refused"], PROVISIONAL: ["b-brand", "Provisional ID"], PENDING_FIX: ["b-warn", "Pending fix"],
  PENDING: ["b-warn", "Pending (retry)"], CONFLICT: ["b-bad", "Conflict"], SEAT_LIMIT: ["b-bad", "Seat limit"], WITHDRAWN: ["", "Withdrawn"],
  REJECTED_ROW: ["b-bad", "Rejected row"], OPEN: ["b-info", "Open"], CONFIRMED: ["b-bad", "Confirmed"], DISMISSED: ["", "Dismissed"],
  ESCALATED: ["b-warn", "Escalated"], PENDING_EXC: ["b-warn", "Pending"], APPROVED: ["b-ok", "Approved"], REJECTED: ["b-bad", "Rejected"],
  HIGH: ["b-bad", "High"], MEDIUM: ["b-warn", "Medium"], LOW: ["b-info", "Low"], GOOD: ["b-ok", "Good"], WATCH: ["b-warn", "Watch"],
  REVIEW: ["b-bad", "Review"], OK: ["b-ok", "OK"], ALERT: ["b-warn", "≥90%"], FULL: ["b-bad", "Full"], VALID: ["b-ok", "Valid"], REVOKED: ["b-bad", "Revoked"],
};

export function StateBadge({ state }: { state: string }) {
  const [cls, label] = STATE_STYLE[state] ?? ["", state];
  return <span className={`badge ${cls}`} data-testid="state-badge">{label}</span>;
}

export const Card = ({ title, children, right }: { title?: ReactNode; children: ReactNode; right?: ReactNode }) => (
  <section className="card">
    {(title || right) && (
      <div className="row" style={{ marginBottom: ".5rem" }}>
        <h2 className="grow" style={{ margin: 0 }}>{title}</h2>
        {right}
      </div>
    )}
    {children}
  </section>
);

export const Stat = ({ label, value, note, tone }: { label: string; value: ReactNode; note?: ReactNode; tone?: "ok" | "bad" | "warn" }) => (
  <div className="stat">
    <div className="l">{label}</div>
    <div className="v" style={tone ? { color: `var(--${tone})` } : undefined}>{value}</div>
    {note && <div className="n muted">{note}</div>}
  </div>
);

export const Alert = ({ kind = "info", children }: { kind?: "err" | "ok" | "info" | "warn"; children: ReactNode }) => (
  <div className={`alert ${kind}`} role={kind === "err" ? "alert" : "status"}>{children}</div>
);

export const Spinner = () => <div className="spinner">Loading…</div>;

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <div className="row"><h2 className="grow">{title}</h2><button className="btn sm" onClick={onClose} aria-label="Close">✕</button></div>
        {children}
      </div>
    </div>
  );
}

/** Loads data on mount / when deps change, with loading + error state and a reload handle. */
export function useLoad<T = any>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);
  const reload = useCallback(() => {
    const my = ++seq.current;
    setLoading(true);
    fn()
      .then((d) => my === seq.current && (setData(d), setError("")))
      .catch((e: ApiError) => my === seq.current && setError(e.message))
      .finally(() => my === seq.current && setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(reload, [reload]);
  return { data, error, loading, reload, setData };
}

export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const run = async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    setError("");
    try {
      return await fn();
    } catch (e: any) {
      setError(e.message || "Something went wrong");
      return undefined;
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run, setError };
}

export const fmtDate = (s?: string | null) => (s ? new Date(s).toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" }) : "—");
export const fmtDateTime = (s?: string | null) => (s ? new Date(s.endsWith("Z") ? s : s + "Z").toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—");

/** `good`: higher is better (Trust Index) — colours invert versus seat usage, where high is the risk. */
export function Bar({ pct, good }: { pct: number; good?: boolean }) {
  const cls = good ? (pct >= 80 ? "" : pct >= 60 ? "warn" : "full") : pct >= 100 ? "full" : pct >= 90 ? "warn" : "";
  return <div className={`bar ${cls}`} role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}><i style={{ width: `${Math.min(100, pct)}%` }} /></div>;
}

/** Cumulative-claims line chart with the 90% alert line and 100% stop line (slide-3 prototype view). */
export function SeatChart({ days, cumulative, alertAt, stopAt }: { days: number[]; cumulative: number[]; alertAt: number; stopAt: number }) {
  const W = 640, H = 240, L = 36, R = 12, T = 12, B = 28;
  if (!days.length) return <div className="muted">No claims yet for this ledger.</div>;
  const maxY = Math.max(stopAt, ...cumulative) * 1.08;
  const x = (i: number) => L + (days.length === 1 ? 0 : (i / (days.length - 1)) * (W - L - R));
  const y = (v: number) => H - B - (v / maxY) * (H - T - B);
  const pts = cumulative.map((v, i) => `${x(i)},${y(v)}`).join(" ");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(f * stopAt));
  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={`Cumulative claims: ${cumulative[cumulative.length - 1]} of ${stopAt}`}>
      {ticks.map((t) => (<g key={t}><line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke="var(--line)" /><text x={L - 6} y={y(t) + 4} textAnchor="end">{t}</text></g>))}
      <line x1={L} x2={W - R} y1={y(alertAt)} y2={y(alertAt)} stroke="var(--warn)" strokeDasharray="5 4" />
      <text x={W - R} y={y(alertAt) - 4} textAnchor="end" style={{ fill: "var(--warn)" }}>alert {alertAt} (90%)</text>
      <line x1={L} x2={W - R} y1={y(stopAt)} y2={y(stopAt)} stroke="var(--bad)" strokeDasharray="5 4" />
      <text x={W - R} y={y(stopAt) - 4} textAnchor="end" style={{ fill: "var(--bad)" }}>stop {stopAt} (100%)</text>
      <polyline points={pts} fill="none" stroke="var(--brand)" strokeWidth="2.5" />
      {cumulative.map((v, i) => <circle key={i} cx={x(i)} cy={y(v)} r="3" fill="var(--brand)"><title>{`Day ${days[i]}: ${v} claims`}</title></circle>)}
      {days.map((d, i) => (i % Math.ceil(days.length / 10) === 0 ? <text key={d} x={x(i)} y={H - 8} textAnchor="middle">{d}</text> : null))}
      <text x={(L + W) / 2} y={H} textAnchor="middle" style={{ fontSize: 10 }}>admission day</text>
    </svg>
  );
}

/** Two-line chart for the policy simulator: what happened vs. what the proposed rule would have admitted. */
export function CompareChart({ curve }: { curve: { date: string; actual: number; simulated: number }[] }) {
  const W = 640, H = 220, L = 40, R = 12, T = 12, B = 24;
  if (!curve.length) return <div className="muted">No historical admissions to replay.</div>;
  const maxY = Math.max(...curve.map((c) => c.actual)) * 1.05 || 1;
  const x = (i: number) => L + (curve.length === 1 ? 0 : (i / (curve.length - 1)) * (W - L - R));
  const y = (v: number) => H - B - (v / maxY) * (H - T - B);
  const line = (k: "actual" | "simulated") => curve.map((c, i) => `${x(i)},${y(c[k])}`).join(" ");
  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Admissions: actual versus simulated">
      {[0, 0.5, 1].map((f) => <g key={f}><line x1={L} x2={W - R} y1={y(maxY * f)} y2={y(maxY * f)} stroke="var(--line)" /><text x={L - 6} y={y(maxY * f) + 4} textAnchor="end">{Math.round(maxY * f)}</text></g>)}
      <polyline points={line("actual")} fill="none" stroke="var(--bad)" strokeWidth="2.5" />
      <polyline points={line("simulated")} fill="none" stroke="var(--ok)" strokeWidth="2.5" />
      <text x={L + 6} y={T + 8} style={{ fill: "var(--bad)" }}>● actual admissions</text>
      <text x={L + 150} y={T + 8} style={{ fill: "var(--ok)" }}>● under proposed rule</text>
      <text x={L} y={H - 6}>{curve[0].date}</text><text x={W - R} y={H - 6} textAnchor="end">{curve[curve.length - 1].date}</text>
    </svg>
  );
}

export function HBar({ rows, color = "var(--brand)" }: { rows: { label: string; value: number }[]; color?: string }) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  return (
    <div>
      {rows.map((r) => (
        <div key={r.label} className="row" style={{ marginBottom: 4, flexWrap: "nowrap" }}>
          <div style={{ width: 150 }} className="small">{r.label}</div>
          <div className="grow bar"><i style={{ width: `${(r.value / max) * 100}%`, background: color }} /></div>
          <div style={{ width: 40 }} className="small right">{r.value}</div>
        </div>
      ))}
    </div>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return <div className="field"><label>{label}{children}</label></div>;
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: [T, string][]; value: T; onChange: (t: T) => void }) {
  return <div className="tabs" role="tablist">{tabs.map(([k, l]) => <button key={k} role="tab" aria-selected={value === k} className={value === k ? "on" : ""} onClick={() => onChange(k)}>{l}</button>)}</div>;
}

export function KV({ data }: { data: Dict }) {
  return <dl className="kv">{Object.entries(data).map(([k, v]) => <div key={k} style={{ display: "contents" }}><dt>{k}</dt><dd>{v ?? "—"}</dd></div>)}</dl>;
}
