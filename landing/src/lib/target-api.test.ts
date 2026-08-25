import { describe, it, expect, vi, afterEach } from "vitest";
import { targetFetch } from "./target-api";

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch(impl: (...args: unknown[]) => unknown) {
  vi.stubGlobal("fetch", vi.fn(impl));
}

describe("an unreachable router is not an empty answer", () => {
  it("reports a typed 503 rather than succeeding with nothing", async () => {
    stubFetch(() => {
      throw new TypeError("fetch failed");
    });

    const result = await targetFetch("/workspaces", "tok");

    // The whole point. If this ever returned `{ok: true, data: []}` the
    // screens above it would render "you have no workspaces" to someone who
    // has six — the failure direction nobody reports, because it reads as
    // good news.
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.status).toBe(503);
    expect(result.error).toBe("target_router_unreachable");
  });

  it("keeps an empty list distinguishable from that failure", async () => {
    stubFetch(async () => new Response(JSON.stringify({ workspaces: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));

    const result = await targetFetch<{ workspaces: unknown[] }>("/workspaces", "tok");

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok");
    expect(result.data.workspaces).toEqual([]);
  });
});

describe("the credential", () => {
  it("rides the Authorization header, and is absent when there is none", async () => {
    const calls: RequestInit[] = [];
    stubFetch(async (_url: unknown, init: unknown) => {
      calls.push(init as RequestInit);
      return new Response("{}", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    await targetFetch("/auth/session", "sekrit-token");
    await targetFetch("/auth/google/signin", null, { method: "POST", body: "{}" });

    const authed = new Headers(calls[0].headers);
    expect(authed.get("Authorization")).toBe("Bearer sekrit-token");

    // Sign-in is anonymous by construction — there is no session yet. A header
    // sent here would have to be a token from somewhere else.
    const anon = new Headers(calls[1].headers);
    expect(anon.has("Authorization")).toBe(false);
  });
});

describe("error bodies", () => {
  it("returns a reason code, never the upstream body", async () => {
    stubFetch(async () => new Response(
      JSON.stringify({ error: "invitation_expired", token: "leaked-secret" }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    ));

    const result = await targetFetch("/invitations/accept", "tok", { method: "POST" });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error).toBe("invitation_expired");
    expect(JSON.stringify(result)).not.toContain("leaked-secret");
  });

  it("falls back to the status when the reason is not a plain code", async () => {
    stubFetch(async () => new Response(
      JSON.stringify({ error: { message: "Bearer eyJhbGciOi...", trace: "x" } }),
      { status: 500, headers: { "Content-Type": "application/json" } },
    ));

    const result = await targetFetch("/workspaces", "tok");

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    // A structured upstream error must not be flattened into our reason field:
    // that is how a credential fragment reaches a log line.
    expect(result.error).toBe("http_500");
    expect(JSON.stringify(result)).not.toContain("eyJhbGciOi");
  });
});
