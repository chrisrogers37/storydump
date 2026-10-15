/**
 * The past-posts choice before an Instagram connect (#1644): the
 * preview read, the body each entry sends, and the two-step flow over the
 * BFF — ask only where past posts are a choice, start with no choice
 * everywhere else (a preview that cannot be had included: the web and the API
 * deploy independently, so a web ahead of its API must still connect), and
 * answer a stale preview's 409 with a fresh preview, never a redirect.
 *
 * The flow runs against a stubbed `fetch`, so what is pinned is what the
 * browser actually sends and where: the preview's path and query, the start's
 * path and body, and the authorize-host guard on the URL it would navigate to.
 * The preview fixtures are the API's own shape (`connect_choice.preview`).
 * Every handle is synthetic.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  UNREADABLE_PREVIEW,
  beginConnect,
  confirmConnect,
  connectRefusalCopy,
  parseConnectPreview,
  startBody,
  type ConnectPreview,
} from "./connect-choice";
import { SOURCE_REFUSAL_COPY } from "./source-refusals";

const WS = "11111111-1111-4111-8111-111111111111";
const ACCOUNT = "55555555-5555-4555-8555-555555555555";
const DRIVE = "33333333-3333-4333-8333-333333333333";
const AUTHORIZE = "https://api.instagram.com/oauth/authorize?state=s";

/** `connect_choice.preview` for the header's Add, archive live, mix at revision 8. */
function addPreview(over: Record<string, unknown> = {}) {
  return {
    archive_available: true,
    entry: "add",
    account_id: null,
    handle: null,
    history: { editable: true, default: true, saved_enabled: null },
    expected_mix_revision: 8,
    preview_version: 1,
    reset: {
      applies: true,
      mode_after: "equal",
      rows_after: [
        { source_id: null, provider: "instagram_archive", name: null, target_share: "0.5" },
        { source_id: DRIVE, provider: "gdrive", name: "Event photos", target_share: "0.5" },
      ],
    },
    ...over,
  };
}

const FIRST_CONNECT = addPreview({
  entry: "first_connect",
  account_id: ACCOUNT,
  handle: "example_handle",
});

const RECONNECT = {
  archive_available: true,
  entry: "reconnect",
  account_id: ACCOUNT,
  handle: "example_handle",
  history: { editable: false, default: null, saved_enabled: false },
  expected_mix_revision: 8,
  preview_version: 1,
  reset: null,
};

/** The default: no capability, so no choice and no revision. */
const UNAVAILABLE = {
  archive_available: false,
  entry: "add",
  account_id: null,
  handle: null,
  history: { editable: false, default: false, saved_enabled: null },
  expected_mix_revision: null,
  preview_version: 1,
  reset: null,
};

function parsed(data: unknown): ConnectPreview {
  const preview = parseConnectPreview(data);
  if (!preview) throw new Error("fixture did not parse");
  return preview;
}

// ── A stubbed BFF: each call takes the next answer and is recorded ─────────

type Answer =
  | { status: number; body: unknown }
  /** An answer whose body is not JSON, as a gateway's error page is. */
  | { status: number; raw: string }
  /** `fetch` itself throwing: the connection dropped. */
  | { network: true };
const calls: Array<{ url: string; method: string; body: unknown }> = [];
let answers: Answer[] = [];

function serve(...queue: Answer[]) {
  answers = queue;
}

beforeEach(() => {
  calls.length = 0;
  answers = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: unknown, init?: RequestInit) => {
      calls.push({
        url: String(url),
        method: init?.method ?? "GET",
        body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      });
      const next = answers.shift();
      if (!next) throw new Error(`unexpected call: ${String(url)}`);
      if ("network" in next) throw new TypeError("fetch failed");
      return new Response("raw" in next ? next.raw : JSON.stringify(next.body), {
        status: next.status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const ok = (body: unknown): Answer => ({ status: 200, body });
const granted = ok({ authorizationUrl: AUTHORIZE });

describe("reading the preview", () => {
  it("reads each entry the API sends", () => {
    expect(parsed(addPreview())).toEqual({
      archiveAvailable: true,
      entry: "add",
      handle: null,
      defaultOn: true,
      savedEnabled: null,
      expectedMixRevision: 8,
      previewVersion: 1,
      reset: [
        { sourceId: null, provider: "instagram_archive", name: null, targetShare: "0.5" },
        { sourceId: DRIVE, provider: "gdrive", name: "Event photos", targetShare: "0.5" },
      ],
    });
    expect(parsed(FIRST_CONNECT)).toMatchObject({ entry: "first_connect", handle: "example_handle" });
    expect(parsed(RECONNECT)).toMatchObject({
      entry: "reconnect",
      savedEnabled: false,
      defaultOn: false,
      expectedMixRevision: null,
      reset: null,
    });
    expect(parsed(UNAVAILABLE)).toMatchObject({ archiveAvailable: false, reset: null });
  });

  it("follows the server's default rather than assuming On", () => {
    const off = addPreview({ history: { editable: true, default: false, saved_enabled: null } });
    expect(parsed(off).defaultOn).toBe(false);
  });

  it("starts nothing from a preview it cannot answer", () => {
    for (const [label, data] of [
      ["no revision", addPreview({ expected_mix_revision: null })],
      ["a negative revision", addPreview({ expected_mix_revision: -1 })],
      ["another preview version", addPreview({ preview_version: 2 })],
      ["a switch the server says is not editable", addPreview({ history: { editable: false, default: true } })],
      ["an unknown entry", addPreview({ entry: "adopt" })],
      ["no archive_available", { ...UNAVAILABLE, archive_available: undefined }],
      ["a reconnect without its saved setting", { ...RECONNECT, history: { editable: false } }],
      [
        "a reset row without a share",
        addPreview({ reset: { applies: true, rows_after: [{ source_id: null, provider: "instagram_archive" }] } }),
      ],
      ["a reset row with a float share", addPreview({ reset: { applies: true, rows_after: [{ source_id: null, provider: "gdrive", target_share: 0.5 }] } })],
    ] as const) {
      expect(parseConnectPreview(data), label).toBeNull();
    }
    // The positive control: the same fixture, whole, is read.
    expect(parseConnectPreview(addPreview())).not.toBeNull();
  });

  it("drops a reset the server says does not apply", () => {
    const none = addPreview({ reset: { applies: false, mode_after: "equal", rows_after: [] } });
    expect(parsed(none).reset).toBeNull();
  });
});

describe("the body each entry sends", () => {
  it("sends the choice with the preview's revision and version for Add and a first connect", () => {
    expect(startBody(parsed(addPreview()), true)).toEqual({
      archive_choice: { use_history: true, expected_mix_revision: 8, preview_version: 1 },
    });
    expect(startBody(parsed(FIRST_CONNECT), false)).toEqual({
      archive_choice: { use_history: false, expected_mix_revision: 8, preview_version: 1 },
    });
  });

  it("sends no choice on a reconnect, whatever the switch would have said", () => {
    expect(startBody(parsed(RECONNECT), true)).toEqual({ archive_choice: null });
  });

  it("sends the empty body where past posts are no choice", () => {
    expect(startBody(parsed(UNAVAILABLE), true)).toEqual({});
  });
});

describe("pressing Connect", () => {
  it("starts at once, with no dialog, where past posts are no choice", async () => {
    serve(ok(UNAVAILABLE), granted);
    const step = await beginConnect({ workspaceId: WS, accountId: null });
    expect(step).toEqual({ kind: "redirect", url: AUTHORIZE });
    expect(calls).toEqual([
      { url: `/api/workspaces/${WS}/accounts/connect-preview`, method: "GET", body: undefined },
      { url: `/api/workspaces/${WS}/accounts/connect`, method: "POST", body: {} },
    ]);
  });

  it("previews a row's connect by its account and starts it on its own path", async () => {
    serve(ok({ ...UNAVAILABLE, entry: "first_connect", account_id: ACCOUNT }), granted);
    await beginConnect({ workspaceId: WS, accountId: ACCOUNT });
    expect(calls.map((c) => [c.method, c.url])).toEqual([
      ["GET", `/api/workspaces/${WS}/accounts/connect-preview?account_id=${ACCOUNT}`],
      ["POST", `/api/workspaces/${WS}/accounts/${ACCOUNT}/connect`],
    ]);
  });

  it.each([
    ["the header's Add", null, addPreview()],
    ["a handle-only row's first connect", ACCOUNT, FIRST_CONNECT],
    ["a row's reconnect", ACCOUNT, RECONNECT],
  ])("asks before %s where past posts are a choice, and starts nothing yet", async (_label, accountId, preview) => {
    serve(ok(preview));
    const step = await beginConnect({ workspaceId: WS, accountId });
    expect(step).toEqual({ kind: "ask", preview: parsed(preview), notice: null });
    expect(calls).toHaveLength(1);
  });

});

describe("a preview that cannot be had does not stop a connect", () => {
  // The web is on Vercel and the API on Railway, and they deploy on their own:
  // a web that is ahead asks for a route that is not there yet, and connecting
  // Instagram must go on with the plain start. With past posts live the
  // account then lands with them Off, and the banner says so.
  const add = { workspaceId: WS, accountId: null };
  const PLAIN_START = [
    { url: `/api/workspaces/${WS}/accounts/connect-preview`, method: "GET", body: undefined },
    { url: `/api/workspaces/${WS}/accounts/connect`, method: "POST", body: {} },
  ];

  it.each<[string, Answer]>([
    ["a 404, the API without the route yet", { status: 404, body: { error: "http_404" } }],
    ["a 404 whose body is a page", { status: 404, raw: "<html>Not Found</html>" }],
    ["a 405", { status: 405, body: { error: "http_405" } }],
    ["a 500", { status: 500, body: { error: "http_500" } }],
    ["a 503", { status: 503, body: { error: "target_router_unreachable" } }],
    ["a 502 whose body is a page", { status: 502, raw: "<html>Bad Gateway</html>" }],
    ["a 200 whose body is not JSON", { status: 200, raw: "<html>ok</html>" }],
    ["a 200 that is not a preview", ok({ archive_available: true, entry: "add" })],
    ["a 200 with no revision to send back", ok(addPreview({ expected_mix_revision: null }))],
    ["a 200 with a malformed reset row", ok(addPreview({ reset: { applies: true, rows_after: [{ source_id: null, provider: "gdrive", target_share: 0.5 }] } }))],
    ["a dropped connection", { network: true }],
  ])("starts at once after %s: no dialog and no choice", async (_label, answer) => {
    serve(answer, granted);
    expect(await beginConnect(add)).toEqual({ kind: "redirect", url: AUTHORIZE });
    expect(calls).toEqual(PLAIN_START);
    // Not `archive_choice: null` either: exactly the plain start's request.
    expect(Object.keys(calls[1].body as object)).toEqual([]);
  });

  it("starts a row's connect on its own path, with the same empty body", async () => {
    serve({ status: 404, body: { error: "http_404" } }, granted);
    expect(await beginConnect({ workspaceId: WS, accountId: ACCOUNT })).toEqual({
      kind: "redirect",
      url: AUTHORIZE,
    });
    expect(calls).toEqual([
      {
        url: `/api/workspaces/${WS}/accounts/connect-preview?account_id=${ACCOUNT}`,
        method: "GET",
        body: undefined,
      },
      { url: `/api/workspaces/${WS}/accounts/${ACCOUNT}/connect`, method: "POST", body: {} },
    ]);
  });

  it("leaves what is wrong with the connect to the start", async () => {
    // The preview and the start are refused alike: the person reads the start's own
    // refusal.
    serve({ status: 403, body: { error: "insufficient_role" } }, { status: 403, body: { error: "insufficient_role" } });
    expect(await beginConnect(add)).toEqual({ kind: "refused", error: "insufficient_role", status: 403 });
    expect(calls.map((c) => c.method)).toEqual(["GET", "POST"]);
    expect(connectRefusalCopy("insufficient_role", 403)).toMatch(/admin/i);
  });

  it("is still a refusal when the start refuses after a failed preview", async () => {
    serve({ status: 404, body: { error: "http_404" } }, { status: 503, body: { error: "http_503" } });
    expect(await beginConnect(add)).toEqual({ kind: "refused", error: "http_503", status: 503 });
  });

  it("still opens the dialog over a preview it can read, and starts nothing yet", async () => {
    // The positive control: the fallback is for a preview that cannot be had, not for a choice.
    serve(ok(addPreview()));
    expect(await beginConnect(add)).toEqual({ kind: "ask", preview: parsed(addPreview()), notice: null });
    expect(calls).toEqual([PLAIN_START[0]]);
  });
});

describe("the dialog's Continue", () => {
  const add = { workspaceId: WS, accountId: null };

  it("sends the choice made and leaves for Instagram", async () => {
    serve(granted);
    expect(await confirmConnect(add, parsed(addPreview()), true)).toEqual({
      kind: "redirect",
      url: AUTHORIZE,
    });
    expect(calls).toEqual([
      {
        url: `/api/workspaces/${WS}/accounts/connect`,
        method: "POST",
        body: { archive_choice: { use_history: true, expected_mix_revision: 8, preview_version: 1 } },
      },
    ]);
  });

  it("connects for publishing only when the switch is off", async () => {
    serve(granted);
    await confirmConnect(add, parsed(addPreview()), false);
    expect(calls[0].body).toEqual({
      archive_choice: { use_history: false, expected_mix_revision: 8, preview_version: 1 },
    });
  });

  it("reconnects with no choice", async () => {
    serve(granted);
    await confirmConnect({ workspaceId: WS, accountId: ACCOUNT }, parsed(RECONNECT), true);
    expect(calls[0]).toEqual({
      url: `/api/workspaces/${WS}/accounts/${ACCOUNT}/connect`,
      method: "POST",
      body: { archive_choice: null },
    });
  });

  it("answers a stale preview with a fresh one and asks again, never redirecting", async () => {
    const fresh = addPreview({ expected_mix_revision: 9 });
    serve({ status: 409, body: { error: "mix_conflict", current_revision: 9 } }, ok(fresh));
    const step = await confirmConnect(add, parsed(addPreview()), true);
    expect(step).toEqual({
      kind: "ask",
      preview: parsed(fresh),
      notice:
        "The posting mix changed after this preview was shown. Look at the new preview, then connect again. Nothing changed.",
    });
    expect(calls.map((c) => [c.method, c.url])).toEqual([
      ["POST", `/api/workspaces/${WS}/accounts/connect`],
      ["GET", `/api/workspaces/${WS}/accounts/connect-preview`],
    ]);

    // Asked again, the start carries the fresh revision, and goes.
    serve(granted);
    expect(await confirmConnect(add, parsed(fresh), true)).toEqual({ kind: "redirect", url: AUTHORIZE });
    expect(calls[2].body).toEqual({
      archive_choice: { use_history: true, expected_mix_revision: 9, preview_version: 1 },
    });
  });

  it("keeps the dialog, and starts nothing, when the refresh cannot be had", async () => {
    // Unlike the first preview, the refresh follows a choice the person made, and the
    // API has just answered a start that carried it: a plain start would drop the choice.
    serve({ status: 409, body: { error: "mix_conflict", current_revision: 9 } }, { status: 503, body: { error: "target_router_unreachable" } });
    expect(await confirmConnect(add, parsed(addPreview()), true)).toEqual({
      kind: "refused",
      error: "target_router_unreachable",
      status: 503,
    });
    serve({ status: 409, body: { error: "mix_conflict", current_revision: 9 } }, ok({ archive_available: true, entry: "add" }));
    expect(await confirmConnect(add, parsed(addPreview()), true)).toEqual({
      kind: "refused",
      error: UNREADABLE_PREVIEW,
      status: 200,
    });
    // Each time: the refused start, then the refresh. No second start.
    expect(calls.map((c) => c.method)).toEqual(["POST", "GET", "POST", "GET"]);
    expect(calls.filter((c) => c.method === "POST").every((c) => "archive_choice" in (c.body as object))).toBe(true);
  });

  it("ends the dialog when the fresh preview no longer offers the choice", async () => {
    serve({ status: 409, body: { error: "mix_conflict", current_revision: 9 } }, ok(UNAVAILABLE));
    expect(await confirmConnect(add, parsed(addPreview()), true)).toEqual({
      kind: "refused",
      error: "archive_not_activated",
      status: 503,
    });
  });

  it("keeps the authorize-host guard: a 200 pointing elsewhere is a refusal, not a navigation", async () => {
    serve(ok({ authorizationUrl: "https://api.instagram.com.evil.example/oauth/authorize" }));
    expect(await confirmConnect(add, parsed(addPreview()), true)).toEqual({
      kind: "refused",
      error: "malformed_authorization_url",
      status: 200,
    });
  });

  it("says any other refusal by name and fetches nothing more", async () => {
    serve({ status: 400, body: { error: "invalid_archive_choice" } });
    expect(await confirmConnect(add, parsed(addPreview()), true)).toEqual({
      kind: "refused",
      error: "invalid_archive_choice",
      status: 400,
    });
    expect(calls).toHaveLength(1);
  });
});

describe("connectRefusalCopy", () => {
  it("says the past-posts reasons in their own words and everything else as the connect leg does", () => {
    expect(connectRefusalCopy("archive_not_activated", 503)).toBe(SOURCE_REFUSAL_COPY.archive_not_activated);
    expect(connectRefusalCopy("invalid_archive_choice", 400)).toBe(SOURCE_REFUSAL_COPY.invalid_archive_choice);
    expect(connectRefusalCopy("mix_conflict", 409)).toMatch(/after this preview was shown/);
    expect(connectRefusalCopy(UNREADABLE_PREVIEW, 200)).toMatch(/nothing was started/i);
    expect(connectRefusalCopy("http_403", 403)).toMatch(/admin/i);
    expect(connectRefusalCopy("malformed_authorization_url", 200)).toMatch(/could not start/i);
  });
});
