/**
 * The F3 property, tested at the layer that owns it.
 *
 * P2 built the per-submission mechanism and pinned it structurally, but no real
 * caller existed, so the property that matters could not be exercised: **a
 * genuine second edit BACK TO A PREVIOUS VALUE must not be silently deduped.**
 * That is the failure F3 rejected a content hash to avoid, and it is invisible
 * — the port answers 200, the UI says saved, the setting does not move.
 *
 * These tests run the client's real output through the route's real spec and
 * the real key derivation, rather than asserting that two UUIDs differ. The
 * chain is what can break: a client that reuses an id, a spec that keys on
 * something else, a derivation that collapses two submissions onto one key.
 * Asserting on the last link tests all three.
 *
 * What is NOT proven here: that the port then admits all three. That lives in
 * `command_dedup` and is proven against a real database in
 * `tests/scripts/test_p3_settings_idempotency.py`, which consumes keys of this
 * exact shape.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { idempotencyKeyFor, parseCommand } from "./commands";
import {
  REPLAYED_ERROR,
  offboardingRefusalCopy,
  settingsRefusalCopy,
  submitCommand,
  submitOffboardWorkspace,
  submitRestoreWorkspace,
  submitSettingsChange,
  submitDisableAccount,
  disableAccountRefusalCopy,
  submitRemoveMember,
  removeMemberRefusalCopy,
} from "./command-client";

const WS = "11111111-1111-4111-8111-111111111111";

type Captured = { url: string; init: RequestInit };

let captured: Captured[];

/** Reply with `body` at `status`, recording every call. */
function stubFetch(body: unknown = {}, status = 200) {
  captured = [];
  const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
    captured.push({ url, init });
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
    } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
}

/** The body the browser actually sent, parsed back. */
function sentBody(i: number): Record<string, unknown> {
  return JSON.parse(String(captured[i].init.body));
}

/**
 * The key the PORT would receive for call `i` — derived by running the sent
 * body through the route's own spec and the one key derivation, not by
 * re-implementing either.
 */
function portKey(i: number, command = "settings_change"): string {
  const parsed = parseCommand(command, sentBody(i));
  if (!parsed.ok) throw new Error(`the route would refuse call ${i}: ${parsed.error}`);
  return idempotencyKeyFor(command, parsed.identity);
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe("the F3 property — an edit back to a previous value", () => {
  it("keys three distinct submissions when the value returns to where it started", async () => {
    stubFetch();

    // The exact sequence F3 names. Not two calls: two of ANY key scheme
    // usually differ. It is the THIRD, whose content matches the first, that
    // separates a per-submission key from a content hash.
    await submitSettingsChange(WS, { caption_style: "enhanced" });
    await submitSettingsChange(WS, { caption_style: "simple" });
    await submitSettingsChange(WS, { caption_style: "enhanced" });

    const keys = [portKey(0), portKey(1), portKey(2)];
    expect(new Set(keys).size).toBe(3);

    // Stated as its own assertion because it is the whole point: calls 0 and 2
    // carry IDENTICAL content. Under F3's rejected option (b) these two keys
    // would be equal and the third write would be acknowledged, not executed.
    expect(sentBody(0).settings).toEqual(sentBody(2).settings);
    expect(keys[0]).not.toBe(keys[2]);
  });

  it("keys two identical resubmissions distinctly — a deliberate retry is not a replay", async () => {
    stubFetch();

    // F3's stated cost, asserted rather than assumed: this scheme dedups a
    // double-submit of one attempt but NOT a deliberate second one. Someone
    // pressing Save twice on purpose gets two writes.
    await submitSettingsChange(WS, { posts_per_day: 3 });
    await submitSettingsChange(WS, { posts_per_day: 3 });

    expect(portKey(0)).not.toBe(portKey(1));
  });

  it("mints the identity per CALL, so no caller can hold one across submissions", async () => {
    stubFetch();
    await submitSettingsChange(WS, { posts_per_day: 1 });
    await submitSettingsChange(WS, { posts_per_day: 2 });

    // The structural half of the rule. `submitCommand` takes no id parameter,
    // so a component cannot pass a `useState` one in — which is exactly how a
    // mount-scoped id would arrive.
    expect(sentBody(0).submission_id).not.toBe(sentBody(1).submission_id);
    expect(submitCommand.length).toBeLessThanOrEqual(3);
  });
});

describe("what the browser sends", () => {
  it("posts to the command route and lets the SERVER set Idempotency-Key", async () => {
    stubFetch();
    await submitSettingsChange(WS, { dry_run_mode: true });

    expect(captured[0].url).toBe(`/api/workspaces/${WS}/commands/settings_change`);
    // The browser must not set the header. The route derives it from the
    // identity, so a browser-set one would be a second, unvalidated source.
    const headers = captured[0].init.headers as Record<string, string>;
    expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain(
      "idempotency-key",
    );
  });

  it("wraps the settings map where the port expects it, and adds nothing else", async () => {
    stubFetch();
    await submitSettingsChange(WS, { caption_style: "simple" });

    const body = sentBody(0);
    expect(body.settings).toEqual({ caption_style: "simple" });
    // submission_id and settings — no client-side copy of the allowlist, no
    // re-stated workspace id, nothing the port did not ask for.
    expect(Object.keys(body).sort()).toEqual(["settings", "submission_id"]);
  });

  it("produces a body the route's own spec accepts", async () => {
    stubFetch();
    await submitSettingsChange(WS, { posts_per_day: 4 });

    // The client and the spec are separate files that must agree. This is the
    // seam a rename would break silently.
    expect(parseCommand("settings_change", sentBody(0)).ok).toBe(true);
  });
});

describe("a replay is reported as a failure, not a success", () => {
  it("treats 200 outcome=replayed as a refusal", async () => {
    // The port acknowledges a same-key/same-body call at HTTP 200 WITHOUT
    // executing it (`app.py:247-250`). For a settings write, reporting that as
    // success is precisely the F3 harm.
    stubFetch({ outcome: "replayed" }, 200);

    const result = await submitSettingsChange(WS, { posts_per_day: 5 });
    expect(result.ok).toBe(false);
    expect(result.ok === false && result.error).toBe(REPLAYED_ERROR);
  });

  it("says the change was not saved, in the copy a person reads", () => {
    // The one sentence that must never appear for this reason is a reassuring
    // one. Pinned so a later copy edit cannot soften it into "saved".
    const copy = settingsRefusalCopy(REPLAYED_ERROR);
    expect(copy).toMatch(/not saved/i);

    // Every occurrence of "saved" is negated. Written as a scan rather than
    // one clever regex because the property is about EVERY occurrence, and a
    // lookaround gets the direction wrong: the negation here precedes the
    // word, so `(?!.*not)` passes text that reads "Saved, but ... not".
    const occurrences = [...copy.matchAll(/saved/gi)];
    expect(occurrences.length).toBeGreaterThan(0);
    for (const m of occurrences) {
      expect(copy.slice(0, m.index).toLowerCase()).toMatch(/\bnot\s+$/);
    }
  });

  it("passes a normal success through", async () => {
    stubFetch({ outcome: "executed", settings: { posts_per_day: 5 } }, 200);
    const result = await submitSettingsChange(WS, { posts_per_day: 5 });
    expect(result.ok).toBe(true);
  });
});

describe("refusals", () => {
  it("surfaces the port's reason rather than a status code", async () => {
    stubFetch({ detail: "unknown setting 'is_paused'", reason: "invalid_args" }, 400);
    const result = await submitSettingsChange(WS, { is_paused: true });
    expect(result.ok).toBe(false);
    expect(result.ok === false && result.error).toBe("invalid_args");
  });

  it("reports an unreachable app without claiming anything about the write", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("network");
      }),
    );
    const result = await submitCommand(WS, "settings_change", { settings: { tz: "UTC" } });
    expect(result.ok).toBe(false);
    expect(result.ok === false && result.error).toBe("unreachable");
    expect(settingsRefusalCopy("unreachable")).toMatch(/Nothing changed/i);
  });

  it("does not borrow the queue's sentences", () => {
    // `intents.ts::refusalCopy` answers about posts and the queue. If these
    // two ever get folded together, a settings failure starts talking about a
    // post that moved on.
    expect(settingsRefusalCopy("illegal_transition")).not.toMatch(/queue|post/i);
  });
});

describe("a permission refusal is not a network blip", () => {
  // The port maps `insufficient_role` to 403 and answers `{detail: "forbidden"}`
  // — no `error`, no `reason` — so `submitCommand` synthesises `http_403` and
  // every reason branch misses. Before this, a member refused for their ROLE and
  // a browser that could not reach the app got the SAME sentence, and the two
  // remedies are opposite: "ask an admin" versus "try again shortly".
  //
  // Found in review on #1154 by tracing the real refusal path, not by analogy.

  it("names the permission problem instead of the generic failure", () => {
    const copy = settingsRefusalCopy("http_403", 403);
    expect(copy).toMatch(/permission/i);
    expect(copy).not.toMatch(/try again shortly/i);
  });

  it("says who CAN do it — a refusal with no next step is half a message", () => {
    expect(settingsRefusalCopy("http_403", 403)).toMatch(/admin|owner/i);
  });

  it("keys on the status, not the synthesised string", () => {
    // `http_403` is a stand-in the client invents; the status is the real signal.
    // If the port ever sends a reason alongside the 403, this must still fire.
    expect(settingsRefusalCopy("something_else", 403)).toMatch(/permission/i);
  });

  it("does not swallow the other refusals", () => {
    // A transport failure must keep its own sentence: same screen, opposite remedy.
    expect(settingsRefusalCopy("unreachable", 0)).toMatch(/cannot reach/i);
    expect(settingsRefusalCopy("invalid_name", 400)).toMatch(/give the workspace a name/i);
    expect(settingsRefusalCopy("whatever", 500)).toMatch(/try again shortly/i);
  });
});

describe("offboarding — the way in and the way back (#1127)", () => {
  it("starts offboarding with the intent STATED, under a fresh submission id", async () => {
    stubFetch({ outcome: "enqueued", state: "offboarding" }, 202);
    const result = await submitOffboardWorkspace(WS);
    expect(result.ok).toBe(true);
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/commands/offboard_workspace`);
    expect(sentBody(0).confirm).toBe(true);
    expect(typeof sentBody(0).submission_id).toBe("string");
    expect(portKey(0, "offboard_workspace")).toMatch(/^offboard_workspace:/);
  });

  it("restores with an empty body, under a fresh submission id", async () => {
    stubFetch({ outcome: "executed", state: "active" }, 200);
    const result = await submitRestoreWorkspace(WS);
    expect(result.ok).toBe(true);
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/commands/restore_workspace`);
    expect(Object.keys(sentBody(0))).toEqual(["submission_id"]);
  });

  it("two attempts are two submissions, never one deduped by content", async () => {
    stubFetch({ outcome: "enqueued" }, 202);
    await submitOffboardWorkspace(WS);
    await submitOffboardWorkspace(WS);
    expect(portKey(0, "offboard_workspace")).not.toBe(portKey(1, "offboard_workspace"));
  });
});

describe("offboardingRefusalCopy", () => {
  it("names the owner on a role refusal — not 'an admin', because admins cannot", () => {
    expect(offboardingRefusalCopy("http_403", 403)).toMatch(/owner/i);
    expect(offboardingRefusalCopy("http_403", 403)).not.toMatch(/admin/i);
  });

  it("asks for the typed confirmation when the intent was not stated", () => {
    expect(offboardingRefusalCopy("confirm_required")).toMatch(/type the workspace name/i);
  });

  it("sends a stale screen back to the current state on an illegal transition", () => {
    // Covers all three port refusals that share the reason: already
    // offboarding, not offboarding, and the grace window having closed.
    expect(offboardingRefusalCopy("illegal_transition")).toMatch(/reload/i);
  });

  it("does not smooth a replay into success", () => {
    expect(offboardingRefusalCopy(REPLAYED_ERROR)).toMatch(/not/i);
  });

  it("has a sentence for the unknown case that promises nothing", () => {
    expect(offboardingRefusalCopy("something_new")).toMatch(/nothing changed/i);
  });
});

describe("removing a destination — disable_account (owner decision 2026-09-04)", () => {
  const ACCOUNT = "55555555-5555-4555-8555-555555555555";

  it("sends the account id under a fresh submission id, to the port's door", async () => {
    stubFetch({ outcome: "executed", ig_account_id: ACCOUNT }, 200);
    const result = await submitDisableAccount(WS, ACCOUNT);
    expect(result.ok).toBe(true);
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/commands/disable_account`);
    expect(sentBody(0).ig_account_id).toBe(ACCOUNT);
    expect(typeof sentBody(0).submission_id).toBe("string");
    expect(portKey(0, "disable_account")).toMatch(/^disable_account:/);
  });

  it("two attempts are two submissions", async () => {
    stubFetch({ outcome: "executed" }, 200);
    await submitDisableAccount(WS, ACCOUNT);
    await submitDisableAccount(WS, ACCOUNT);
    expect(portKey(0, "disable_account")).not.toBe(portKey(1, "disable_account"));
  });
});

describe("disableAccountRefusalCopy", () => {
  it("names the admin floor on a role refusal", () => {
    expect(disableAccountRefusalCopy("http_403", 403)).toMatch(/admin/i);
  });

  it("sends a stale screen back when the destination is gone or already removed", () => {
    expect(disableAccountRefusalCopy("not_found")).toMatch(/reload/i);
    expect(disableAccountRefusalCopy("illegal_transition")).toMatch(/already/i);
  });

  it("does not smooth a replay into success", () => {
    expect(disableAccountRefusalCopy(REPLAYED_ERROR)).toMatch(/not/i);
  });

  it("has a sentence for the unknown case that promises nothing", () => {
    expect(disableAccountRefusalCopy("something_new")).toMatch(/nothing changed/i);
  });
});

describe("removing a member — remove_member (the revoke for every join edge)", () => {
  const MEMBER = "66666666-6666-4666-8666-666666666666";

  it("sends the user id under a fresh submission id, to the port's door", async () => {
    stubFetch({ outcome: "executed", user_id: MEMBER, removed_role: "member" }, 200);
    const result = await submitRemoveMember(WS, MEMBER);
    expect(result.ok).toBe(true);
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/commands/remove_member`);
    expect(sentBody(0).user_id).toBe(MEMBER);
    expect(portKey(0, "remove_member")).toMatch(/^remove_member:/);
  });

  it("names the admin floor, the owner rule, and a stale screen", () => {
    expect(removeMemberRefusalCopy("http_403", 403)).toMatch(/admin/i);
    expect(removeMemberRefusalCopy("illegal_transition")).toMatch(/owner/i);
    expect(removeMemberRefusalCopy("not_found")).toMatch(/reload/i);
  });
});
