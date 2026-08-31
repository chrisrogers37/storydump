/**
 * `POST /api/workspaces` must send `Idempotency-Key`.
 *
 * This file exists because omitting it was a live production outage, not a
 * style question. `POST /workspaces` reaches the port through `_dispatch`,
 * whose FIRST statement refuses a keyless command (`v1.py:104-109`) before the
 * body is read. So the create returned 400, wrote nothing, and `messageFor`
 * had no case for it — the first real user saw "That did not work. This one is
 * on us." and no one could tell from the message what had happened.
 *
 * The invariant was already written down one route over
 * (`workspaces/[id]/commands/[command]/route.ts`: "`Idempotency-Key` rides on
 * EVERY call, not on the ones that look like they need it"). The rule existed,
 * the enforcement was named, the neighbours complied, and this route did not.
 * So the assertion is that the header is SENT, not that the code looks a
 * certain way.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const captured: Array<{ path: string; init?: Record<string, unknown> }> = [];

vi.mock("@/lib/session", () => ({
  getSessionToken: async () => "tok-test",
  SESSION_COOKIE: "sd_session",
  WORKSPACE_COOKIE: "storydump_workspace",
}));

vi.mock("@/lib/target-api", () => ({
  targetFetch: async (path: string, _token: string | null, init?: Record<string, unknown>) => {
    captured.push({ path, init });
    return { ok: true, data: { id: "ws-1", name: "Northside", role: "owner", state: "active" } };
  },
}));

const { POST } = await import("./route");

function req(body: unknown) {
  return new Request("https://storydump.app/api/workspaces", {
    method: "POST",
    body: JSON.stringify(body),
  }) as unknown as Parameters<typeof POST>[0];
}

describe("POST /api/workspaces", () => {
  beforeEach(() => {
    captured.length = 0;
  });

  it("sends an Idempotency-Key — the port refuses a keyless command", async () => {
    await POST(req({ name: "Northside" }));
    const headers = (captured[0]?.init?.headers ?? {}) as Record<string, string>;
    const keys = Object.keys(headers).map((k) => k.toLowerCase());
    expect(keys).toContain("idempotency-key");
    expect(headers["Idempotency-Key"]).toBeTruthy();
  });

  it("derives the key from the command and the trimmed name, via the ONE derivation", async () => {
    await POST(req({ name: "  Northside  " }));
    const headers = (captured[0].init!.headers ?? {}) as Record<string, string>;
    // `idempotencyKeyFor` is `${command}:${identity}`; the identity must be the
    // TRIMMED name, or the same submission retried with stray whitespace would
    // key differently and create a second workspace.
    expect(headers["Idempotency-Key"]).toBe("create_workspace:Northside");
  });

  it("keys differently for a different workspace, so distinct creates are not deduped", async () => {
    await POST(req({ name: "Northside" }));
    await POST(req({ name: "Southside" }));
    const k = (i: number) =>
      ((captured[i].init!.headers ?? {}) as Record<string, string>)["Idempotency-Key"];
    expect(k(0)).not.toBe(k(1));
  });

  it("still refuses a blank name locally, without reaching the port", async () => {
    const res = await POST(req({ name: "   " }));
    expect(res.status).toBe(400);
    expect(captured).toHaveLength(0);
  });
});
