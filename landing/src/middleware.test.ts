import { getScriptNonceFromHeader } from "next/dist/server/app-render/get-script-nonce-from-header";
import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
import { SESSION_COOKIE, WORKSPACE_COOKIE } from "@/lib/session";
import { middleware } from "./middleware";

const BASE = "https://app.example.test";
const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const SIGNED_IN = { [SESSION_COOKIE]: "opaque-session-token" };
const IN_A_WORKSPACE = { ...SIGNED_IN, [WORKSPACE_COOKIE]: WORKSPACE };

function visit(path: string, cookies: Record<string, string> = {}) {
  const cookie = Object.entries(cookies)
    .map(([name, value]) => `${name}=${value}`)
    .join("; ");
  return middleware(new NextRequest(`${BASE}${path}`, { headers: cookie ? { cookie } : {} }));
}

/** The policy header the browser gets, and whether it is Report-Only. */
function policyOf(response: Response) {
  const enforced = response.headers.get("content-security-policy");
  const reported = response.headers.get("content-security-policy-report-only");
  return { enforced, reported, sent: enforced ?? reported };
}

describe("the per-request policy", () => {
  it("names the nonce Next renders the page with", () => {
    const response = visit("/join/some-invite");
    const { sent } = policyOf(response);
    // What the page is rendered with travels as an overridden request header.
    const rendered = response.headers.get("x-middleware-request-content-security-policy");
    expect(sent).not.toBeNull();
    expect(rendered).toBe(sent);
    expect(getScriptNonceFromHeader(sent!)).toMatch(/^[A-Za-z0-9+/]{22}==$/);
  });

  it("carries a fresh nonce on every request", () => {
    const first = getScriptNonceFromHeader(policyOf(visit("/auth/error")).sent!);
    const second = getScriptNonceFromHeader(policyOf(visit("/auth/error")).sent!);
    expect(first).not.toBe(second);
  });

  it.each(["/join/some-invite", "/auth/error"])("is enforced on %s", (path) => {
    const { enforced, reported } = policyOf(visit(path));
    expect(enforced).toContain("'nonce-");
    expect(reported).toBeNull();
  });

  it.each<[string, Record<string, string>]>([
    ["/dashboard", IN_A_WORKSPACE],
    ["/dashboard/queue", IN_A_WORKSPACE],
    ["/welcome", SIGNED_IN],
    ["/workspaces", SIGNED_IN],
  ])("is Report-Only on %s", (path, cookies) => {
    const { enforced, reported } = policyOf(visit(path, cookies));
    expect(reported).toContain("'nonce-");
    expect(enforced).toBeNull();
  });
});

describe("the session gate", () => {
  it.each(["/dashboard", "/dashboard/queue", "/workspaces"])(
    "sends a visitor with no session from %s to /login",
    (path) => {
      const response = visit(path);
      expect(response.status).toBe(307);
      expect(response.headers.get("location")).toBe(`${BASE}/login`);
    },
  );

  it("sends a session with no workspace from the dashboard to /welcome", () => {
    const response = visit("/dashboard/queue", SIGNED_IN);
    expect(response.headers.get("location")).toBe(`${BASE}/welcome`);
  });

  it("clears a legacy JWT on its way to /login", () => {
    const response = visit("/workspaces", { [SESSION_COOKIE]: "eyJhbGc.eyJzdWI.c2ln" });
    expect(response.headers.get("location")).toBe(`${BASE}/login`);
    const cleared = response.headers.getSetCookie().filter((c) => c.startsWith(`${SESSION_COOKIE}=;`));
    expect(cleared).toHaveLength(1);
  });

  it.each(["/join/some-invite", "/auth/error", "/welcome"])(
    "leaves %s to the page, which a visitor with no session may reach",
    (path) => {
      expect(visit(path).headers.get("location")).toBeNull();
    },
  );
});
