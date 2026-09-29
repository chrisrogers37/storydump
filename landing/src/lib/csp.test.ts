import { getScriptNonceFromHeader } from "next/dist/server/app-render/get-script-nonce-from-header";
import { describe, expect, it } from "vitest";
import { createNonce, isReportOnly, noncePolicy, staticPagePolicy } from "./csp";

/** A policy as `directive -> sources`, so a test reads one directive. */
function directives(policy: string): Map<string, string[]> {
  return new Map(
    policy
      .split(";")
      .map((part) => part.trim().split(/\s+/))
      .filter((tokens) => tokens[0] !== "")
      .map(([name, ...sources]) => [name, sources]),
  );
}

describe("the nonce", () => {
  it("is read back by Next, which stamps it on every script it renders", () => {
    // Next finds the nonce by parsing this policy (`getScriptNonceFromHeader`).
    // One it cannot parse is dropped without an error, and then every script
    // on the page is blocked.
    const nonce = createNonce();
    expect(getScriptNonceFromHeader(noncePolicy(nonce))).toBe(nonce);
  });

  it("is 128 random bits, fresh on every call", () => {
    const nonces = new Set(Array.from({ length: 50 }, () => createNonce()));
    expect(nonces.size).toBe(50);
    for (const nonce of nonces) {
      expect(atob(nonce)).toHaveLength(16);
    }
  });
});

describe("the nonce policy", () => {
  it("runs an inline script only when it carries the nonce", () => {
    const script = directives(noncePolicy("abc123")).get("script-src");
    expect(script).toContain("'nonce-abc123'");
    expect(script).toContain("'strict-dynamic'");
    expect(script).not.toContain("'unsafe-inline'");
  });
});

describe("the static page policy", () => {
  it("carries no nonce or hash, which would make browsers ignore 'unsafe-inline'", () => {
    // A prerendered page's inline scripts are fixed at build time and carry no
    // nonce. With a nonce or hash source present, a browser disregards
    // 'unsafe-inline' and those scripts stop running.
    const script = directives(staticPagePolicy()).get("script-src") ?? [];
    expect(script).toContain("'unsafe-inline'");
    expect(script.filter((s) => /^'(nonce|sha(256|384|512))-/.test(s))).toEqual([]);
  });
});

describe("both policies", () => {
  it("differ only in which scripts run", () => {
    const loose = directives(staticPagePolicy());
    const strict = directives(noncePolicy("abc123"));
    loose.delete("script-src");
    strict.delete("script-src");
    expect(strict).toEqual(loose);
  });

  it("allow eval only in development, where React needs it", () => {
    for (const policy of [staticPagePolicy(), noncePolicy("abc123")]) {
      expect(directives(policy).get("script-src")).not.toContain("'unsafe-eval'");
    }
    for (const policy of [staticPagePolicy({ dev: true }), noncePolicy("abc123", { dev: true })]) {
      expect(directives(policy).get("script-src")).toContain("'unsafe-eval'");
    }
  });

  it("let the analytics script load and report", () => {
    for (const policy of [staticPagePolicy(), noncePolicy("abc123")]) {
      const parsed = directives(policy);
      expect(parsed.get("script-src")).toContain("https://plausible.io");
      expect(parsed.get("connect-src")).toContain("https://plausible.io");
    }
  });

  it("refuse framing, plugins, a foreign <base> and form posts to other origins", () => {
    const parsed = directives(staticPagePolicy());
    expect(parsed.get("frame-ancestors")).toEqual(["'none'"]);
    expect(parsed.get("object-src")).toEqual(["'none'"]);
    expect(parsed.get("base-uri")).toEqual(["'self'"]);
    expect(parsed.get("form-action")).toEqual(["'self'"]);
  });
});

describe("isReportOnly", () => {
  it.each(["/dashboard", "/dashboard/queue", "/dashboard/media/calendar", "/welcome", "/workspaces"])(
    "reports without blocking on %s",
    (path) => {
      expect(isReportOnly(path)).toBe(true);
    },
  );

  it.each(["/join/abc", "/auth/error", "/dashboards", "/welcomes", "/"])(
    "enforces on %s",
    (path) => {
      expect(isReportOnly(path)).toBe(false);
    },
  );
});
