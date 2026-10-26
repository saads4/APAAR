import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, errorMessage, qs, setToken } from "../api";
import { AuthCtx } from "../auth";
import { ClaimDetail } from "../pages/Institution";
import Login from "../pages/Login";
import Verifier from "../pages/Verifier";
import Wallet from "../pages/Student";
import { SeatChart, StateBadge } from "../ui";

type Route = (url: string, init?: RequestInit) => { status?: number; body: unknown } | undefined;

/** Minimal fetch double: routes by method + path substring, records calls. */
function mockFetch(routes: Record<string, Route | { status?: number; body: unknown }>) {
  const calls: { url: string; method: string; body?: any }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ url, method, body: init?.body ? JSON.parse(init.body as string) : undefined });
    const key = Object.keys(routes).find((k) => `${method} ${url}`.includes(k));
    if (!key) return new Response(JSON.stringify({ detail: `no mock for ${method} ${url}` }), { status: 500 });
    const r = routes[key];
    const out = typeof r === "function" ? r(url, init) ?? { body: {} } : r;
    return new Response(JSON.stringify(out.body), { status: out.status ?? 200 });
  }));
  return calls;
}

afterEach(() => { vi.unstubAllGlobals(); setToken(null); });

describe("api helpers", () => {
  it("turns FastAPI errors into readable text", () => {
    expect(errorMessage({ detail: "Seat ledger full" }, "x")).toBe("Seat ledger full");
    expect(errorMessage({ detail: [{ loc: ["body", "phone"], msg: "field required" }] }, "x")).toBe("phone: field required");
    expect(errorMessage(null, "fallback")).toBe("fallback");
  });
  it("builds query strings without empty values", () => {
    expect(qs({ a: 1, b: "", c: undefined, d: "x y" })).toBe("a=1&d=x%20y");
  });
  it("surfaces server errors and network failures as ApiError", async () => {
    mockFetch({ "GET /api/x": { status: 403, body: { detail: "Role STUDENT may not access this resource" } } });
    await expect(api.get("/api/x")).rejects.toMatchObject({ status: 403, message: "Role STUDENT may not access this resource" });
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("offline"); }));
    await expect(api.get("/api/y")).rejects.toBeInstanceOf(ApiError);
  });
  it("sends the bearer token and idempotency header", async () => {
    const calls = mockFetch({ "POST /api/claims": { body: { id: 1 } } });
    setToken("tok");
    await api.post("/api/claims", { a: 1 }, { "Idempotency-Key": "k1" });
    const init = (fetch as any).mock.calls[0][1];
    expect(init.headers.Authorization).toBe("Bearer tok");
    expect(init.headers["Idempotency-Key"]).toBe("k1");
    expect(calls[0].body).toEqual({ a: 1 });
  });
});

describe("components", () => {
  it("StateBadge shows human labels", () => {
    render(<><StateBadge state="SEAT_LIMIT" /><StateBadge state="AWAITING_STUDENT" /></>);
    expect(screen.getByText("Seat limit")).toBeInTheDocument();
    expect(screen.getByText("Awaiting student")).toBeInTheDocument();
  });
  it("SeatChart shows the 90% alert and 100% stop lines and a summary label", () => {
    render(<SeatChart days={[1, 2, 3]} cumulative={[5, 30, 59]} alertAt={54} stopAt={60} />);
    expect(screen.getByRole("img", { name: /59 of 60/ })).toBeInTheDocument();
    expect(screen.getByText(/alert 54/)).toBeInTheDocument();
    expect(screen.getByText(/stop 60/)).toBeInTheDocument();
  });
});

describe("Login", () => {
  const renderLogin = (signIn = vi.fn()) => render(<AuthCtx.Provider value={{ user: null, signIn, signOut: vi.fn() }}><Login /></AuthCtx.Provider>);

  it("signs in and hands the token to the app", async () => {
    mockFetch({ "GET /api/meta": { body: { demo_mode: false } }, "POST /api/auth/login": { body: { token: "T", user: { id: 1, role: "INSTITUTION", name: "R", email: "r@x" } } } });
    const signIn = vi.fn();
    renderLogin(signIn);
    await userEvent.type(screen.getByLabelText("Email"), "r@x");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(signIn).toHaveBeenCalledWith("T", expect.objectContaining({ role: "INSTITUTION" })));
  });

  it("shows the server's error for bad credentials", async () => {
    mockFetch({ "GET /api/meta": { body: { demo_mode: false } }, "POST /api/auth/login": { status: 401, body: { detail: "Invalid email or password" } } });
    renderLogin();
    await userEvent.type(screen.getByLabelText("Email"), "a@b.c");
    await userEvent.type(screen.getByLabelText("Password"), "bad");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid email or password");
  });

  it("asks for the authenticator code when MFA is required", async () => {
    const calls = mockFetch({ "GET /api/meta": { body: { demo_mode: false } }, "POST /api/auth/login": { body: { mfa_required: true, mfa_token: "M" } },
      "POST /api/auth/mfa": { body: { token: "T2", user: { id: 2, role: "AICTE_ADMIN", name: "A", email: "a@x" } } } });
    const signIn = vi.fn();
    renderLogin(signIn);
    await userEvent.type(screen.getByLabelText("Email"), "a@x");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await userEvent.type(await screen.findByLabelText("Authenticator code"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => expect(signIn).toHaveBeenCalledWith("T2", expect.anything()));
    expect(calls.find((c) => c.url.includes("/auth/mfa"))?.body).toEqual({ mfa_token: "M", code: "123456" });
  });
});

describe("Verifier", () => {
  it("shows only purpose-bound fields and the signed answer", async () => {
    const calls = mockFetch({
      "GET /api/verifier/purposes": { body: { EMPLOYMENT: ["full_name", "institution"], EMBASSY: ["full_name", "dob"] } },
      "POST /api/verifier/verify": { body: { payload: { valid: true, status: "VALID", disclosed: { full_name: "Ananya Sharma", institution: "DIT" }, fields_withheld: 12 }, signature: "sig" } },
    });
    render(<MemoryRouter><Verifier /></MemoryRouter>);
    expect(await screen.findByText("full_name", { selector: "span" })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/Credential ID/), "CRD-1");
    await userEvent.type(screen.getByLabelText(/Reason for this check/), "Offer letter check");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByText("Ananya Sharma")).toBeInTheDocument();
    expect(screen.getByText(/12 other fields were withheld/)).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST")?.body).toMatchObject({ credential_id: "CRD-1", purpose: "EMPLOYMENT" });
  });
});

describe("Student wallet", () => {
  const claim = { id: 7, institution: "DIT", programme: "B.Tech CSE", seat_type: "REGULAR", state: "AWAITING_STUDENT", reason: "Seat held", credential: null };
  it("enables OTP confirmation only for 6 digits, then posts it", async () => {
    const calls = mockFetch({
      "GET /api/student/wallet": { body: { claims: [claim] } }, "GET /api/student/access-log": { body: { events: [], revoked: [] } },
      "GET /api/dev/outbox": { body: [{ body: "OTP 111222 confirms" }] }, "POST /api/student/claims/7/confirm": { body: { ...claim, state: "VERIFIED" } },
    });
    render(<MemoryRouter><Wallet /></MemoryRouter>);
    const btn = await screen.findByRole("button", { name: "Confirm with OTP" });
    expect(btn).toBeDisabled();
    await userEvent.type(screen.getByLabelText("OTP"), "111222");
    expect(btn).toBeEnabled();
    await userEvent.click(btn);
    await waitFor(() => expect(calls.find((c) => c.url.includes("/confirm"))?.body).toEqual({ method: "OTP", otp: "111222" }));
  });
  it("shows a wrong-OTP error from the server", async () => {
    mockFetch({
      "GET /api/student/wallet": { body: { claims: [claim] } }, "GET /api/student/access-log": { body: { events: [], revoked: [] } },
      "GET /api/dev/outbox": { body: [] }, "POST /api/student/claims/7/confirm": { status: 400, body: { detail: "Incorrect OTP" } },
    });
    render(<MemoryRouter><Wallet /></MemoryRouter>);
    await userEvent.type(await screen.findByLabelText("OTP"), "000000");
    await userEvent.click(screen.getByRole("button", { name: "Confirm with OTP" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect OTP");
  });
});

describe("ClaimDetail fix-it", () => {
  it("offers the field-level correction and applies it", async () => {
    const pending = { id: 214, student_name: "Mohd Arif K.", state: "PENDING_FIX", reason: "Probable match", programme: "MBA", seat_type: "REGULAR", student_ref: "DIT/1", apaar_masked: "XXXX XXXX 0003",
      dob: "2006-07-04", admitted_on: "2026-10-24", phone_masked: "XXXXXX0003", match_prob: 0.99, match_detail: { name_level: "AGREE", name_score: 0.97, dob_level: "SWAPPED", weights_bits: { name: 7.5, dob: 7.2 } },
      fixes: [{ field: "dob", submitted: "2006-07-04", suggested: "2006-04-07", reason: "day and month appear swapped" }] };
    const calls = mockFetch({ "GET /api/claims/214": { body: pending }, "POST /api/claims/214/apply-fixes": { body: { ...pending, state: "AWAITING_STUDENT", fixes: null } } });
    render(<ClaimDetail id={214} onClose={() => {}} onChanged={() => {}} />);
    expect(await screen.findByText(/day and month appear swapped/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Apply this correction" }));
    await waitFor(() => expect(calls.find((c) => c.url.includes("apply-fixes"))?.body).toEqual({ fields: ["dob"] }));
  });
});
