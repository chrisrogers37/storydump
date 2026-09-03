import { describe, expect, it } from "vitest";
import {
  COMMAND_SPECS,
  IDEMPOTENCY_KEY_MAX,
  idempotencyKeyFor,
  isOfferedCommand,
  parseCommand,
} from "./commands";

const UUID = "3f8a1c2e-5b4d-4e6f-9a1b-7c2d3e4f5a6b";
const UUID2 = "9c1b2a3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d";

/**
 * The command schema's pure decisions (#1057/#1063 epic, P2).
 *
 * The load-bearing tests here are the STRUCTURAL ones. F2 locked "generalize the
 * route" over "add a second route", and the way that quietly becomes the thing it
 * replaced is a per-command table with one shape still special-cased beside it.
 * A version that hard-codes the intent body passes every behavioural test below
 * and fails the structural ones.
 */

describe("the intent shape is a row, not a special case", () => {
  it("routes every offered command through the same door", () => {
    // If any command were handled outside the table, deleting its row would not
    // change the answer. Each of these is refused only because the table says so.
    for (const name of Object.keys(COMMAND_SPECS)) {
      expect(isOfferedCommand(name), name).toBe(true);
    }
    expect(isOfferedCommand("cancel")).toBe(false); // in the port's vocabulary, not offered here
    expect(parseCommand("cancel", { intent_id: UUID })).toEqual({
      ok: false,
      error: "unknown_command",
    });
  });

  it("gives the intent commands no privileged handling", () => {
    // The four queue commands and the entity-less ones are the same kind of
    // object. If `approve` were special-cased in the dispatcher this would still
    // pass — which is why the test above, on the table, is the real gate.
    for (const name of ["approve", "mark_posted", "skip", "reject"]) {
      expect(typeof COMMAND_SPECS[name].parse, name).toBe("function");
    }
    expect(typeof COMMAND_SPECS.settings_change.parse).toBe("function");
  });
});

describe("every offered command can produce an idempotency key", () => {
  // The invariant behind "Idempotency-Key on EVERY call". The port refuses a
  // command without one, so a spec that parsed OK with an empty identity would
  // mint `command:` and trade a 404 for a 400 — a different-looking bug.
  const validBodies: Record<string, unknown> = {
    approve: { intent_id: UUID },
    mark_posted: { intent_id: UUID },
    skip: { intent_id: UUID },
    reject: { intent_id: UUID },
    settings_change: { submission_id: UUID, settings: { posts_per_day: 3 } },
    sync_now: { submission_id: UUID, source_id: UUID2 },
    rename_workspace: { submission_id: UUID, name: "Northside Coffee" },
    offboard_workspace: { submission_id: UUID, confirm: true },
    restore_workspace: { submission_id: UUID },
    disconnect_account: { submission_id: UUID, source_id: UUID2 },
    account_settings_change: {
      submission_id: UUID,
      ig_account_id: UUID2,
      settings: { posts_per_day: 3 },
    },
  };

  it("covers the whole table, so a new spec cannot skip this check", () => {
    expect(Object.keys(validBodies).sort()).toEqual(Object.keys(COMMAND_SPECS).sort());
  });

  for (const [name, body] of Object.entries(validBodies)) {
    it(`${name} yields a non-empty identity and a key within the cap`, () => {
      const parsed = parseCommand(name, body);
      expect(parsed.ok, name).toBe(true);
      if (!parsed.ok) return;
      expect(parsed.identity).not.toBe("");
      const key = idempotencyKeyFor(name, parsed.identity);
      expect(key.length).toBeLessThanOrEqual(IDEMPOTENCY_KEY_MAX);
    });
  }
});

describe("intent commands — behaviour preserved exactly", () => {
  it("forwards only the intent id", () => {
    const parsed = parseCommand("approve", { intent_id: UUID, extra: "ignored" });
    expect(parsed).toEqual({ ok: true, body: { intent_id: UUID }, identity: UUID });
  });

  it("refuses a non-uuid intent by name", () => {
    for (const bad of ["", "not-a-uuid", 12, null, undefined, {}]) {
      expect(parseCommand("skip", { intent_id: bad }), String(bad)).toEqual({
        ok: false,
        error: "invalid_intent",
      });
    }
  });

  it("refuses a body that is not an object", () => {
    for (const bad of [null, "x", 3, []]) {
      expect(parseCommand("reject", bad), String(bad)).toEqual({
        ok: false,
        error: "malformed_body",
      });
    }
  });
});

describe("entity-less commands", () => {
  it("require a submission id, refused by its own name", () => {
    expect(parseCommand("sync_now", {})).toEqual({
      ok: false,
      error: "invalid_submission_id",
    });
    expect(
      parseCommand("settings_change", { settings: { posts_per_day: 3 } }),
    ).toEqual({ ok: false, error: "invalid_submission_id" });
  });

  it("key on the submission, so a retry replays and a later attempt does not", () => {
    const first = parseCommand("settings_change", {
      submission_id: UUID,
      settings: { posts_per_day: 3 },
    });
    const retry = parseCommand("settings_change", {
      submission_id: UUID,
      settings: { posts_per_day: 3 },
    });
    const later = parseCommand("settings_change", {
      submission_id: UUID2,
      settings: { posts_per_day: 4 },
    });
    if (!first.ok || !retry.ok || !later.ok) throw new Error("expected all ok");
    expect(idempotencyKeyFor("settings_change", first.identity)).toBe(
      idempotencyKeyFor("settings_change", retry.identity),
    );
    expect(idempotencyKeyFor("settings_change", later.identity)).not.toBe(
      idempotencyKeyFor("settings_change", first.identity),
    );
  });

  /**
   * REWRITTEN, and not to make a change pass — the previous version asserted
   * `body: {}`, which pinned a spec that could never succeed. `sync_now` is
   * per-SOURCE: the executor reads `source_id` and refuses `invalid_args`
   * without it, so an empty body was a guaranteed refusal at the port. P2 added
   * the row deliberately unwired, so nothing exercised it until P4 wired the
   * control.
   */
  it("sync_now forwards the source it is syncing, and nothing else", () => {
    const parsed = parseCommand("sync_now", {
      submission_id: UUID,
      source_id: UUID2,
      junk: 1,
    });
    expect(parsed).toEqual({
      ok: true,
      body: { source_id: UUID2 },
      identity: UUID,
    });
  });

  it("refuses sync_now with no source, by its own name", () => {
    // The regression guard for the defect above. A missing source must be
    // refused HERE, where the reason names the field, rather than at the port
    // as a generic `invalid_args` after a round trip.
    expect(parseCommand("sync_now", { submission_id: UUID })).toEqual({
      ok: false,
      error: "invalid_source_id",
    });
    expect(
      parseCommand("sync_now", { submission_id: UUID, source_id: "not-a-uuid" }),
    ).toEqual({ ok: false, error: "invalid_source_id" });
  });

  it("keys sync_now on the SUBMISSION, not the source", () => {
    // Two deliberate syncs of one source are two submissions and must not
    // collapse onto one key — the same property F3 locked for settings.
    const first = parseCommand("sync_now", { submission_id: UUID, source_id: UUID2 });
    const second = parseCommand("sync_now", { submission_id: UUID2, source_id: UUID2 });
    if (!first.ok || !second.ok) throw new Error("expected both ok");
    expect(idempotencyKeyFor("sync_now", first.identity)).not.toBe(
      idempotencyKeyFor("sync_now", second.identity),
    );
  });
});

describe("settings_change checks shape, and deliberately not the port's allowlist", () => {
  it("passes a key the client has never heard of", () => {
    // A second copy of the server's 13-key allowlist is one that can disagree,
    // and this is the copy that would go stale. The port refuses unknown keys by
    // name; if this client ever starts refusing them first, that refusal is the
    // one that rots.
    const parsed = parseCommand("settings_change", {
      submission_id: UUID,
      settings: { a_key_the_port_will_reject: true },
    });
    expect(parsed).toEqual({
      ok: true,
      body: { settings: { a_key_the_port_will_reject: true } },
      identity: UUID,
    });
  });

  it("refuses a missing or empty settings object", () => {
    for (const bad of [undefined, null, {}, [], "x"]) {
      expect(
        parseCommand("settings_change", { submission_id: UUID, settings: bad }),
        String(bad),
      ).toEqual({ ok: false, error: "invalid_settings" });
    }
  });
});

describe("the key derivation", () => {
  it("is stable, and distinct per command on the same subject", () => {
    expect(idempotencyKeyFor("mark_posted", UUID)).toBe(`mark_posted:${UUID}`);
    expect(idempotencyKeyFor("skip", UUID)).not.toBe(idempotencyKeyFor("mark_posted", UUID));
  });
});

describe("rename_workspace", () => {
  // #1152: the command was BUILT at the port the whole time — `admin` floor,
  // executor wired — and absent from this allowlist, which is the only reason
  // nobody could rename anything. `/welcome` promised "You can rename it later"
  // regardless. The spec is what makes the promise true.
  const spec = COMMAND_SPECS.rename_workspace;

  it("is offered at all — the whole defect was that it was not", () => {
    expect(isOfferedCommand("rename_workspace")).toBe(true);
  });

  it("passes the name through, trimmed, keyed on the submission", () => {
    const parsed = spec.parse({ submission_id: UUID, name: "  Northside  " });
    expect(parsed).toEqual({
      ok: true,
      body: { name: "Northside" },
      identity: UUID,
    });
  });

  it("refuses a blank name here rather than spending a round trip on it", () => {
    for (const name of ["", "   ", 42, null, undefined]) {
      expect(spec.parse({ submission_id: UUID, name })).toEqual({
        ok: false,
        error: "invalid_name",
      });
    }
  });

  it("still requires a submission id, like every other entity-less command", () => {
    expect(spec.parse({ name: "Northside" })).toEqual({
      ok: false,
      error: "invalid_submission_id",
    });
  });
});

describe("disconnect_account", () => {
  // #1155: built at all three layers since #1083 — command, executor, and the
  // background Google revoke — and unreachable because this table did not list
  // it. `storydump.app/privacy` §13 committed to the disconnect publicly the
  // whole time. Third instance of built-unreachable-and-promised.
  const spec = COMMAND_SPECS.disconnect_account;

  it("is offered at all — that was the entire defect", () => {
    expect(isOfferedCommand("disconnect_account")).toBe(true);
  });

  it("carries the source id the executor refuses without", () => {
    expect(spec.parse({ submission_id: UUID, source_id: UUID2 })).toEqual({
      ok: true,
      body: { source_id: UUID2 },
      identity: UUID,
    });
  });

  it("refuses a non-id before it becomes a database lookup", () => {
    for (const bad of ["", "not-a-uuid", 7, null, undefined]) {
      expect(spec.parse({ submission_id: UUID, source_id: bad })).toEqual({
        ok: false,
        error: "invalid_source_id",
      });
    }
  });

  it("shares sync_now's shape, since they take the same argument", () => {
    const a = spec.parse({ submission_id: UUID, source_id: UUID2 });
    const b = COMMAND_SPECS.sync_now.parse({ submission_id: UUID, source_id: UUID2 });
    expect(a).toEqual(b);
  });
});

describe("account_settings_change", () => {
  it("forwards the account id and the settings, keyed on the submission", () => {
    const parsed = parseCommand("account_settings_change", {
      submission_id: UUID,
      ig_account_id: UUID2,
      settings: { posts_per_day: 7, tz: "America/New_York" },
      extra: "ignored",
    });
    expect(parsed).toEqual({
      ok: true,
      body: {
        ig_account_id: UUID2,
        settings: { posts_per_day: 7, tz: "America/New_York" },
      },
      identity: UUID,
    });
  });

  it("refuses a non-uuid account id by its own name", () => {
    for (const bad of ["", "not-a-uuid", 12, null, undefined, {}]) {
      expect(
        parseCommand("account_settings_change", {
          submission_id: UUID,
          ig_account_id: bad,
          settings: { posts_per_day: 3 },
        }),
        String(bad),
      ).toEqual({ ok: false, error: "invalid_ig_account_id" });
    }
  });

  it("refuses an empty or non-object settings map", () => {
    for (const bad of [{}, null, "x", 3, []]) {
      expect(
        parseCommand("account_settings_change", {
          submission_id: UUID,
          ig_account_id: UUID2,
          settings: bad,
        }),
        String(bad),
      ).toEqual({ ok: false, error: "invalid_settings" });
    }
  });

  it("does NOT re-validate what the port owns", () => {
    // `nonsense_key` is not one of the four columns and `posts_per_day: 900`
    // is past `ck_iga_ppd`. Both pass HERE and are refused by the port, which
    // is the same division `settings_change` draws: a second copy of the
    // allowlist is one that can disagree, and this is the copy that goes stale.
    const parsed = parseCommand("account_settings_change", {
      submission_id: UUID,
      ig_account_id: UUID2,
      settings: { nonsense_key: 1, posts_per_day: 900 },
    });
    expect(parsed.ok).toBe(true);
  });
});

describe("offboard_workspace", () => {
  // #1127: the executor has been built and owner-gated at the port since the
  // X.3 work landed, and NO adapter could issue it. This row is the web
  // surface — the "explicit, confirmed" half of `06` §1's owner action.
  const spec = COMMAND_SPECS.offboard_workspace;

  it("is offered at all — the whole gap was that it was not", () => {
    expect(isOfferedCommand("offboard_workspace")).toBe(true);
  });

  it("forwards the stated intent, keyed on the submission", () => {
    expect(spec.parse({ submission_id: UUID, confirm: true })).toEqual({
      ok: true,
      body: { confirm: true },
      identity: UUID,
    });
  });

  it("refuses here unless the intent was STATED — the port requires confirm=true too", () => {
    // The dialog is the front end's; this is the shape check that keeps an
    // accidental empty POST from spending a round trip to be refused.
    expect(spec.parse({ submission_id: UUID })).toEqual({
      ok: false,
      error: "confirm_required",
    });
    expect(spec.parse({ submission_id: UUID, confirm: "yes" })).toEqual({
      ok: false,
      error: "confirm_required",
    });
    expect(spec.parse({ submission_id: UUID, confirm: false })).toEqual({
      ok: false,
      error: "confirm_required",
    });
  });
});

describe("restore_workspace", () => {
  // The way back inside the grace window. Owner-only at the port; no body —
  // the workspace is the URL and the deadline is the port's to enforce.
  const spec = COMMAND_SPECS.restore_workspace;

  it("is offered", () => {
    expect(isOfferedCommand("restore_workspace")).toBe(true);
  });

  it("sends an empty body, keyed on the submission", () => {
    expect(spec.parse({ submission_id: UUID })).toEqual({
      ok: true,
      body: {},
      identity: UUID,
    });
  });
});
