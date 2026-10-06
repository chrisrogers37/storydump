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

    await targetFetch("/me", "sekrit-token");
    await targetFetch("/google", null, { method: "POST", plane: "auth" });

    const authed = new Headers(calls[0].headers);
    expect(authed.get("Authorization")).toBe("Bearer sekrit-token");

    // The auth plane's pre-auth endpoints are anonymous by construction — there
    // is no principal yet. A header sent there would have to be a token from
    // somewhere else. Paths here are REAL ones on purpose: a fixture naming an
    // endpoint that does not exist teaches the next reader a false contract,
    // which is how this tier ended up calling four paths the API never served.
    const anon = new Headers(calls[1].headers);
    expect(anon.has("Authorization")).toBe(false);
  });
});

describe("the public plane", () => {
  it("sends neither Origin nor Sec-Fetch-Site, which the API refuses as a browser", async () => {
    stubFetch(() => new Response("{}", { status: 202 }));
    await targetFetch("/waitlist", null, { method: "POST", plane: "public", body: "{}" });
    const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/public\/waitlist$/);
    const headers = new Headers(init.headers);
    expect(headers.has("origin")).toBe(false);
    expect(headers.has("sec-fetch-site")).toBe(false);
  });
});

describe("error bodies", () => {
  it("returns a reason code, never the upstream body", async () => {
    // The API's real shape (`src/api/app.py`, `InvitationRefused`): `detail`
    // is a sentence, `reason` is the code. An earlier fixture here carried an
    // `{error}` key the API never sends, which taught a false contract.
    stubFetch(async () => new Response(
      JSON.stringify({ detail: "invitation not acceptable", reason: "not_acceptable", token: "leaked-secret" }),
      { status: 404, headers: { "Content-Type": "application/json" } },
    ));

    const result = await targetFetch("/invitations/abc123/accept", "tok", {
      method: "POST",
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error).toBe("not_acceptable");
    expect(JSON.stringify(result)).not.toContain("leaked-secret");
  });

  it("falls back to the status when the reason is not a plain code", async () => {
    stubFetch(async () => new Response(
      JSON.stringify({ detail: "internal error", reason: { message: "Bearer eyJhbGciOi...", trace: "x" } }),
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

describe("a command refusal", () => {
  it("surfaces the port's `reason` as the reason code — the refusal envelope is {detail, reason}, not {error}", async () => {
    stubFetch(async () => new Response(
      JSON.stringify({ detail: "this workspace publishes manually; use mark_posted after posting by hand", reason: "manual_mode" }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    ));

    const result = await targetFetch("/workspaces/ws/commands/approve", "tok", { method: "POST" });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    // Without this the queue would render every 409 as `http_409` and could
    // not tell "already acted on" from "use Posted myself" — two different
    // sentences with two different next moves.
    expect(result.status).toBe(409);
    expect(result.error).toBe("manual_mode");
  });

  it("still refuses a reason that is not a plain code", async () => {
    stubFetch(async () => new Response(
      JSON.stringify({ detail: "x", reason: "Bearer eyJhbGciOi..." }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    ));
    const odd = await targetFetch("/x", "tok");
    if (odd.ok) throw new Error("unreachable");
    expect(odd.error).toBe("http_409");
    expect(JSON.stringify(odd)).not.toContain("eyJhbGciOi");
  });
});

describe("a command refusal's facts (#1413 phase 6)", () => {
  // The port's real envelope (`src/api/app.py`, `_command_body`): a sentence,
  // a code, and the facts a person needs to act on the refusal.
  const LOCKED = {
    detail: "item 5: recent — override_locks schedules it anyway",
    reason: "locked",
    facts: { in_the_way: ["recent"], overridable: true, note: "Bearer eyJhbGciOi..." },
  };

  function refuse(body: unknown) {
    stubFetch(async () => new Response(JSON.stringify(body), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    }));
  }

  it("cross only when the caller asks, and only through the allow-list", async () => {
    refuse(LOCKED);
    const result = await targetFetch("/workspaces/ws/commands/schedule_item", "tok", {
      method: "POST",
      refusalFacts: true,
    });
    if (result.ok) throw new Error("unreachable");
    expect(result.facts).toEqual({ in_the_way: ["recent"], overridable: true });
    // The sentence stays behind, and so does anything the list does not name.
    expect(JSON.stringify(result)).not.toContain("eyJhbGciOi");
    expect(JSON.stringify(result)).not.toContain("override_locks schedules");
  });

  it("leave every other caller's result exactly as it was", async () => {
    refuse(LOCKED);
    const result = await targetFetch("/workspaces/ws/commands/schedule_item", "tok", {
      method: "POST",
    });
    expect(result).toStrictEqual({ ok: false, status: 409, error: "locked" });
  });

  it("never change the reason readError derives, which is the value that reaches logs", async () => {
    const bodies = [
      LOCKED,
      { ...LOCKED, facts: "free text where facts belong" },
      { detail: "x", reason: "Bearer eyJhbGciOi...", facts: { overridable: true } },
      { detail: "x", facts: { overridable: true } },
    ];
    for (const body of bodies) {
      refuse(body);
      const plain = await targetFetch("/x", "tok");
      refuse(body);
      const asked = await targetFetch("/x", "tok", { refusalFacts: true });
      if (plain.ok || asked.ok) throw new Error("unreachable");
      expect(asked.error, JSON.stringify(body)).toBe(plain.error);
      expect(asked.status).toBe(plain.status);
      expect(JSON.stringify(asked)).not.toContain("eyJhbGciOi");
    }
  });

  it("are none when the body carries nothing the list admits", async () => {
    refuse({ detail: "x", reason: "locked", facts: { note: "free text" } });
    const result = await targetFetch("/x", "tok", { refusalFacts: true });
    expect(result).toStrictEqual({ ok: false, status: 409, error: "locked" });
  });

  it("are none when the body is not JSON", async () => {
    stubFetch(async () => new Response("<html>bad gateway</html>", { status: 502 }));
    const result = await targetFetch("/x", "tok", { refusalFacts: true });
    expect(result).toStrictEqual({ ok: false, status: 502, error: "http_502" });
  });
});
