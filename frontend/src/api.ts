export type Dict = Record<string, any>;

const BASE: string = (import.meta as any).env?.VITE_API_URL ?? "";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

let token: string | null = null;
try {
  token = localStorage.getItem("sentinel_token");
} catch {
  /* storage may be unavailable */
}
let onUnauthorized: (() => void) | null = null;

export function setToken(t: string | null) {
  token = t;
  try {
    if (t) localStorage.setItem("sentinel_token", t);
    else localStorage.removeItem("sentinel_token");
  } catch {
    /* ignore */
  }
}
export const getToken = () => token;
export const setUnauthorizedHandler = (fn: () => void) => (onUnauthorized = fn);

/** Turns FastAPI error payloads (string or validation list) into one readable sentence. */
export function errorMessage(body: any, fallback: string): string {
  const d = body?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((e) => `${(e.loc || []).slice(1).join(".") || "input"}: ${e.msg}`).join("; ");
  return fallback;
}

async function request(method: string, path: string, body?: unknown, form?: FormData, extra?: Record<string, string>): Promise<any> {
  const headers: Record<string, string> = { ...(extra || {}) };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(BASE + path, { method, headers, body: form ?? (body !== undefined ? JSON.stringify(body) : undefined) });
  } catch {
    throw new ApiError(0, "Cannot reach the server. Is the backend running?");
  }
  const text = await res.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    if (res.status === 401 && token && onUnauthorized && !path.includes("/auth/")) onUnauthorized();
    throw new ApiError(res.status, errorMessage(data, `Request failed (${res.status})`));
  }
  return data;
}

export const api = {
  get: (p: string) => request("GET", p),
  post: (p: string, b?: unknown, headers?: Record<string, string>) => request("POST", p, b ?? {}, undefined, headers),
  patch: (p: string, b: unknown) => request("PATCH", p, b),
  del: (p: string) => request("DELETE", p),
  upload: (p: string, file: File) => {
    const f = new FormData();
    f.append("file", file);
    return request("POST", p, undefined, f);
  },
  /** Authenticated download (CSV exports). */
  async download(path: string, filename: string) {
    const res = await fetch(BASE + path, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (!res.ok) throw new ApiError(res.status, `Download failed (${res.status})`);
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  },
};

export const qs = (o: Dict) =>
  Object.entries(o)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join("&");
