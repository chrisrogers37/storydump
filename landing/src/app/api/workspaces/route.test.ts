/**
 * `POST /api/workspaces` must send `Idempotency-Key`.
 *
 * This file exists because omitting it was a live production outage, not a
 * style question. `POST /workspaces` reaches the port through `_dispatch`,
 * whose FIRST statement refuses a keyless command (`v1.py:104-109`) before the
 * body is read. So the create returned 400, wrote nothing, and
 * `createWorkspaceRefusalCopy` had no case for it — the first real user saw "That did not work. This one is
 * on us." and no one could tell from the message what had happened.
 *
 * The invariant was already written down one route over
 * (`workspaces/[id]/commands/[command]/route.ts`: "`Idempotency-Key` rides on
 * EVERY call, not on the ones that look like they need it"). The rule existed,
 * the enforcement was named, the neighbours complied, and this route did not.
 * So the assertion is that the header is SENT, not that the code looks a
 * certain way.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { callBff, postJson } from "@/lib/bff";
import { createWorkspaceRefusalCopy } from "@/lib/refusal-copy";

const captured: Array<{ path: string; init?: Record<string, unknown> }> = [];

const CREATED = {
  ok: true,
  data: { id: "ws-1", name: "Northside", role: "owner", state: "active" },
} as const;

/** What the port answers the next create. A refusal is `{ ok: false, status, error }`. */
let portAnswer: unknown = CREATED;

vi.mock("@/lib/session", () => ({
  getSessionToken: async () => "tok-test",
  SESSION_COOKIE: "sd_session",
  WORKSPACE_COOKIE: "storydump_workspace",
}));

vi.mock("@/lib/target-api", () => ({
  targetFetch: async (path: string, _token: string | null, init?: Record<string, unknown>) => {
    captured.push({ path, init });
    return portAnswer;
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

/**
 * The sentence a failed create shows is often the only artifact a report
 * carries, so each refusal this route gives must render a sentence of its own.
 * Driven through the browser's own wrapper (`callBff`) and the form's own table
 * (`createWorkspaceRefusalCopy`), with only the network hop replaced by a call
 * to the handler, so what is compared is what a person would see.
 */
describe("POST /api/workspaces — a refusal names its exit", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", (input: string, init?: RequestInit) =>
      POST(
        new Request(new URL(input, "https://storydump.app"), init) as unknown as Parameters<
          typeof POST
        >[0],
      ),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    portAnswer = CREATED;
  });

  async function refusal(init: RequestInit) {
    const result = await callBff("/api/workspaces", init);
    if (result.ok) throw new Error("expected the create to be refused");
    return {
      error: result.error,
      status: result.status,
      sentence: createWorkspaceRefusalCopy(result.error, result.status),
    };
  }

  it.each([
    ["a body that is not JSON", '{"name":'],
    ["a body of null", "null"],
  ])("renders %s apart from a refusal relayed from the port", async (_case, body) => {
    const unreadable = await refusal({ method: "POST", body });
    // A port refusal whose body carries no `reason`, which `readError` reports
    // as `http_<status>`. Same status as the local refusal, so only the reason
    // can separate the two.
    portAnswer = { ok: false, status: 400, error: "http_400" };
    const relayed = await refusal(postJson({ name: "Northside" }));

    expect(unreadable).toMatchObject({ error: "malformed_body", status: 400 });
    expect(relayed).toMatchObject({ error: "http_400", status: 400 });
    expect(unreadable.sentence).not.toBe(relayed.sentence);
  });
});
