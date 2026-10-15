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
 * The command schema's pure decisions (#1057/#1063).
 *
 * The load-bearing tests here are the STRUCTURAL ones. One route serves every
 * command, and the way that quietly becomes two dialects is a per-command
 * table with one shape special-cased beside it.
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
    expect(isOfferedCommand("autopost_now")).toBe(false); // in the port's vocabulary, not offered here
    expect(parseCommand("autopost_now", { intent_id: UUID })).toEqual({
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
    cancel: { intent_id: UUID },
    resolve_review: { intent_id: UUID, resolution: "retry", episode: "2026-09-12T10:00:00+00:00" },
    settings_change: { submission_id: UUID, settings: { posts_per_day: 3 } },
    sync_now: { submission_id: UUID, source_id: UUID2 },
    rename_workspace: { submission_id: UUID, name: "Northside Coffee" },
    offboard_workspace: { submission_id: UUID, confirm: true },
    restore_workspace: { submission_id: UUID },
    pause_workspace: { submission_id: UUID },
    resume_workspace: { submission_id: UUID },
    disconnect_account: { submission_id: UUID },
    disable_account: { submission_id: UUID, ig_account_id: UUID2 },
    remove_member: { submission_id: UUID, user_id: UUID2 },
    invite_member: { submission_id: UUID, email: "partner@example.com", role: "member" },
    revoke_invitation: { submission_id: UUID, invitation_id: UUID2 },
    account_settings_change: {
      submission_id: UUID,
      ig_account_id: UUID2,
      settings: { posts_per_day: 3 },
    },
    schedule_item: {
      submission_id: UUID,
      ig_account_id: UUID2,
      media_item_id: UUID,
      local_at: "2026-10-03T14:30",
    },
    reschedule_item: { submission_id: UUID, intent_id: UUID2, local_at: "2026-10-09T09:30" },
    set_item_link: { submission_id: UUID, media_item_id: UUID2, link_url: "https://example.com/menu" },
    set_source_approval: { submission_id: UUID, source_id: UUID2, approval_mode: "auto" },
    set_item_auto_approve: { submission_id: UUID, media_item_id: UUID2, auto_approve: true },
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

describe("intent commands — behaviour pinned exactly", () => {
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
   * `sync_now` is per-SOURCE: the executor reads `source_id` and refuses
   * `invalid_args` without it, so an empty body is a guaranteed refusal at the
   * port.
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
    // A missing source must be refused HERE, where the reason names the field,
    // rather than at the port as a generic `invalid_args` after a round trip.
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
    // collapse onto one key — the same property as `settings_change`.
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
  // #1152: the command is built at the port — `admin` floor, executor wired —
  // and this allowlist is what offers it. `/welcome` promises "You can rename
  // it later"; the spec is what makes the promise true.
  const spec = COMMAND_SPECS.rename_workspace;

  it("offers rename_workspace", () => {
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
  // #1155: built at all three layers (#1083) — command, executor, and the
  // background Google revoke — and this table is what offers it.
  // `storydump.app/privacy` §13 commits to the disconnect publicly.
  const spec = COMMAND_SPECS.disconnect_account;

  it("offers disconnect_account", () => {
    expect(isOfferedCommand("disconnect_account")).toBe(true);
  });

  it("takes no arguments — the grant is the workspace's (069), so nothing is named", () => {
    expect(spec.parse({ submission_id: UUID, source_id: UUID2 })).toEqual({
      ok: true,
      body: {},
      identity: UUID,
    });
  });
});

describe("disable_account", () => {
  // The Accounts tab's Remove. The port's edge is
  // `active → disabled`; the row stays for history and for the connect that
  // brings it back.
  const spec = COMMAND_SPECS.disable_account;

  it("is offered", () => {
    expect(isOfferedCommand("disable_account")).toBe(true);
  });

  it("carries the account id the executor refuses without", () => {
    expect(spec.parse({ submission_id: UUID, ig_account_id: UUID2 })).toEqual({
      ok: true,
      body: { ig_account_id: UUID2 },
      identity: UUID,
    });
  });

  it("refuses a non-id before it becomes a database lookup", () => {
    for (const bad of ["", "not-a-uuid", 7, null, undefined]) {
      expect(spec.parse({ submission_id: UUID, ig_account_id: bad })).toEqual({
        ok: false,
        error: "invalid_account_id",
      });
    }
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

describe("schedule_item", () => {
  // #1413: the Media Library's Schedule…. The port reads the wall
  // time in the account's zone and owns every rule about it; this tier
  // checks shape.
  const spec = COMMAND_SPECS.schedule_item;
  const ITEM = "5d6e7f80-91a2-4b3c-8d4e-5f6a7b8c9d0e";
  const plan = {
    submission_id: UUID,
    ig_account_id: UUID2,
    media_item_id: ITEM,
    local_at: "2026-10-03T14:30",
  };

  it("is offered", () => {
    expect(isOfferedCommand("schedule_item")).toBe(true);
  });

  it("carries the account, the item and the wall time as typed, keyed on the submission", () => {
    expect(spec.parse({ ...plan, extra: "ignored" })).toEqual({
      ok: true,
      body: { ig_account_id: UUID2, media_item_id: ITEM, local_at: "2026-10-03T14:30" },
      identity: UUID,
    });
  });

  it("forwards override_locks only when it is true, as the CLI sends it", () => {
    expect(spec.parse({ ...plan, override_locks: true })).toEqual({
      ok: true,
      body: {
        ig_account_id: UUID2,
        media_item_id: ITEM,
        local_at: "2026-10-03T14:30",
        override_locks: true,
      },
      identity: UUID,
    });
    expect(spec.parse({ ...plan, override_locks: false })).toEqual({
      ok: true,
      body: { ig_account_id: UUID2, media_item_id: ITEM, local_at: "2026-10-03T14:30" },
      identity: UUID,
    });
  });

  it("refuses a non-id account or item by its own name", () => {
    for (const bad of ["", "not-a-uuid", 7, null, undefined]) {
      expect(spec.parse({ ...plan, ig_account_id: bad }), String(bad)).toEqual({
        ok: false,
        error: "invalid_ig_account_id",
      });
      expect(spec.parse({ ...plan, media_item_id: bad }), String(bad)).toEqual({
        ok: false,
        error: "invalid_media_item_id",
      });
    }
  });

  it("refuses a missing or blank wall time", () => {
    for (const bad of ["", "   ", 7, null, undefined]) {
      expect(spec.parse({ ...plan, local_at: bad }), String(bad)).toEqual({
        ok: false,
        error: "invalid_local_at",
      });
    }
  });

  it("refuses an override that is not a boolean", () => {
    for (const bad of ["true", 1, null]) {
      expect(spec.parse({ ...plan, override_locks: bad }), String(bad)).toEqual({
        ok: false,
        error: "invalid_override_locks",
      });
    }
  });

  it("does NOT re-validate the time rules the port owns", () => {
    // A past time passes HERE. The port refuses it by name, as it does a time
    // the clocks skip and one past the horizon (`facts.at_rule`); a second copy
    // of those rules is one that can disagree.
    expect(spec.parse({ ...plan, local_at: "2000-01-01 00:00" }).ok).toBe(true);
  });
});

describe("a planned story's two levers (#1413)", () => {
  it("are both offered", () => {
    expect(isOfferedCommand("cancel")).toBe(true);
    expect(isOfferedCommand("reschedule_item")).toBe(true);
  });

  it("key a Cancel on the story, so asking twice is one request", () => {
    expect(parseCommand("cancel", { intent_id: UUID })).toEqual({
      ok: true,
      body: { intent_id: UUID },
      identity: UUID,
    });
  });

  it("key a Reschedule on the submission, so moving it back is a second move", () => {
    expect(
      parseCommand("reschedule_item", {
        submission_id: UUID,
        intent_id: UUID2,
        local_at: "2026-10-09T09:30",
      }),
    ).toEqual({
      ok: true,
      body: { intent_id: UUID2, local_at: "2026-10-09T09:30" },
      identity: UUID,
    });
  });

  it("refuse a Reschedule with no story or no time, by name", () => {
    expect(
      parseCommand("reschedule_item", { submission_id: UUID, intent_id: "x", local_at: "2026-10-09T09:30" }),
    ).toEqual({ ok: false, error: "invalid_intent" });
    expect(
      parseCommand("reschedule_item", { submission_id: UUID, intent_id: UUID2, local_at: " " }),
    ).toEqual({ ok: false, error: "invalid_local_at" });
  });

  it("leave the time rules to the port, which owns them", () => {
    // A past time is the port's to refuse by its rule (`at_rule: past`).
    expect(
      parseCommand("reschedule_item", {
        submission_id: UUID,
        intent_id: UUID2,
        local_at: "2001-01-01 00:00",
      }).ok,
    ).toBe(true);
  });
});

describe("a repeating story (#485)", () => {
  const weekly = { unit: "week", every: 1, days: [1, 4] };

  it("keys a Cancel that stops the series apart from a plain one, as the CLI does", () => {
    expect(parseCommand("cancel", { intent_id: UUID, stop_repeat: true })).toEqual({
      ok: true,
      body: { intent_id: UUID, stop_repeat: true },
      identity: `${UUID}:stop_repeat`,
    });
    expect(idempotencyKeyFor("cancel", `${UUID}:stop_repeat`)).toBe(`cancel:${UUID}:stop_repeat`);
    // `false` is a plain cancel; anything else is refused by name.
    expect(parseCommand("cancel", { intent_id: UUID, stop_repeat: false })).toEqual({
      ok: true,
      body: { intent_id: UUID },
      identity: UUID,
    });
    expect(parseCommand("cancel", { intent_id: UUID, stop_repeat: "yes" })).toEqual({
      ok: false,
      error: "invalid_stop_repeat",
    });
  });

  it("forwards a schedule's rule as chosen, and refuses one that is not an object", () => {
    const body = {
      submission_id: UUID,
      ig_account_id: UUID2,
      media_item_id: UUID2,
      local_at: "2026-10-15T16:07",
    };
    expect(parseCommand("schedule_item", { ...body, repeat: weekly })).toMatchObject({
      ok: true,
      body: { repeat: weekly },
    });
    // No rule rides as none: the body carries no repeat at all.
    expect(parseCommand("schedule_item", { ...body, repeat: null })).toEqual({
      ok: true,
      body: { ig_account_id: UUID2, media_item_id: UUID2, local_at: "2026-10-15T16:07" },
      identity: UUID,
    });
    expect(parseCommand("schedule_item", { ...body, repeat: "weekly" })).toEqual({
      ok: false,
      error: "invalid_repeat",
    });
  });

  it("takes a time, a rule, a clear or both for a reschedule, and refuses neither", () => {
    const base = { submission_id: UUID, intent_id: UUID2 };
    expect(parseCommand("reschedule_item", { ...base, repeat: weekly })).toMatchObject({
      ok: true,
      body: { intent_id: UUID2, repeat: weekly },
    });
    // The clear rides as null: the port reads null as the clear, absence as no change.
    expect(parseCommand("reschedule_item", { ...base, repeat: null })).toMatchObject({
      ok: true,
      body: { intent_id: UUID2, repeat: null },
    });
    expect(
      parseCommand("reschedule_item", { ...base, local_at: "2026-10-15T16:07", repeat: weekly }),
    ).toMatchObject({ ok: true, body: { local_at: "2026-10-15T16:07", repeat: weekly } });
    expect(parseCommand("reschedule_item", base)).toEqual({
      ok: false,
      error: "nothing_to_change",
    });
    expect(parseCommand("reschedule_item", { ...base, repeat: [weekly] })).toEqual({
      ok: false,
      error: "invalid_repeat",
    });
  });
});

describe("an item's link to add by hand (#1413)", () => {
  it("is offered", () => {
    expect(isOfferedCommand("set_item_link")).toBe(true);
  });

  it("keys on the submission, so setting the same link again after a clear is a third act", () => {
    expect(
      parseCommand("set_item_link", {
        submission_id: UUID,
        media_item_id: UUID2,
        link_url: "https://example.com/menu",
      }),
    ).toEqual({
      ok: true,
      body: { media_item_id: UUID2, link_url: "https://example.com/menu" },
      identity: UUID,
    });
  });

  it("forwards a clear as an explicit null, which the port reads as a clear", () => {
    expect(
      parseCommand("set_item_link", { submission_id: UUID, media_item_id: UUID2, link_url: null }),
    ).toEqual({ ok: true, body: { media_item_id: UUID2, link_url: null }, identity: UUID });
  });

  it("refuses a missing or blank link, and a bad item, by name", () => {
    // A missing key is never a clear: the port reads it as a mistake, so it stops here too.
    expect(parseCommand("set_item_link", { submission_id: UUID, media_item_id: UUID2 })).toEqual({
      ok: false,
      error: "invalid_link_url",
    });
    expect(
      parseCommand("set_item_link", { submission_id: UUID, media_item_id: UUID2, link_url: "  " }),
    ).toEqual({ ok: false, error: "invalid_link_url" });
    expect(
      parseCommand("set_item_link", { submission_id: UUID, media_item_id: UUID2, link_url: 7 }),
    ).toEqual({ ok: false, error: "invalid_link_url" });
    expect(
      parseCommand("set_item_link", {
        submission_id: UUID,
        media_item_id: "x",
        link_url: "https://example.com/menu",
      }),
    ).toEqual({ ok: false, error: "invalid_media_item_id" });
  });

  it("leaves the URL rule to the port, which owns it", () => {
    // An http link is the port's to refuse by its rule (`invalid_args`).
    expect(
      parseCommand("set_item_link", {
        submission_id: UUID,
        media_item_id: UUID2,
        link_url: "http://example.com/menu",
      }).ok,
    ).toBe(true);
  });
});

describe("a folder's own approval position — set_source_approval (#1477)", () => {
  it("is offered", () => {
    expect(isOfferedCommand("set_source_approval")).toBe(true);
  });

  it("forwards the folder and its position, keyed on the submission", () => {
    expect(
      parseCommand("set_source_approval", {
        submission_id: UUID,
        source_id: UUID2,
        approval_mode: "returning",
        extra: "ignored",
      }),
    ).toEqual({
      ok: true,
      body: { source_id: UUID2, approval_mode: "returning" },
      identity: UUID,
    });
  });

  it("forwards Workspace default as an explicit null, never a missing key", () => {
    const parsed = parseCommand("set_source_approval", {
      submission_id: UUID,
      source_id: UUID2,
      approval_mode: null,
    });
    expect(parsed).toEqual({
      ok: true,
      body: { source_id: UUID2, approval_mode: null },
      identity: UUID,
    });
    expect(parsed.ok && "approval_mode" in parsed.body).toBe(true);
  });

  it("refuses a missing or blank position, and a bad folder, by name", () => {
    // A missing key is never Workspace default: the port reads it as a mistake.
    for (const bad of [undefined, "", "  ", 3, true]) {
      expect(
        parseCommand("set_source_approval", { submission_id: UUID, source_id: UUID2, approval_mode: bad }),
        String(bad),
      ).toEqual({ ok: false, error: "invalid_approval_mode" });
    }
    expect(
      parseCommand("set_source_approval", { submission_id: UUID, source_id: "x", approval_mode: "auto" }),
    ).toEqual({ ok: false, error: "invalid_source_id" });
    expect(parseCommand("set_source_approval", { source_id: UUID2, approval_mode: "auto" })).toEqual({
      ok: false,
      error: "invalid_submission_id",
    });
  });

  it("leaves the three positions to the port, which refuses any other by name", () => {
    expect(
      parseCommand("set_source_approval", {
        submission_id: UUID,
        source_id: UUID2,
        approval_mode: "sometimes",
      }).ok,
    ).toBe(true);
  });

  it("keys on the submission, so YOLO, then the default, then YOLO again are three acts", () => {
    const first = parseCommand("set_source_approval", { submission_id: UUID, source_id: UUID2, approval_mode: "auto" });
    const third = parseCommand("set_source_approval", { submission_id: UUID2, source_id: UUID2, approval_mode: "auto" });
    if (!first.ok || !third.ok) throw new Error("expected both ok");
    expect(idempotencyKeyFor("set_source_approval", first.identity)).not.toBe(
      idempotencyKeyFor("set_source_approval", third.identity),
    );
  });
});

describe("an item's Auto-approve flag — set_item_auto_approve (#1477)", () => {
  it("is offered", () => {
    expect(isOfferedCommand("set_item_auto_approve")).toBe(true);
  });

  it("forwards the item and the flag, on or off, keyed on the submission", () => {
    for (const on of [true, false]) {
      expect(
        parseCommand("set_item_auto_approve", {
          submission_id: UUID,
          media_item_id: UUID2,
          auto_approve: on,
          extra: "ignored",
        }),
      ).toEqual({ ok: true, body: { media_item_id: UUID2, auto_approve: on }, identity: UUID });
    }
  });

  it("refuses a flag that is not a boolean, and a bad item, by name", () => {
    for (const bad of [undefined, null, "true", 1, 0]) {
      expect(
        parseCommand("set_item_auto_approve", { submission_id: UUID, media_item_id: UUID2, auto_approve: bad }),
        String(bad),
      ).toEqual({ ok: false, error: "invalid_auto_approve" });
    }
    expect(
      parseCommand("set_item_auto_approve", { submission_id: UUID, media_item_id: "x", auto_approve: true }),
    ).toEqual({ ok: false, error: "invalid_media_item_id" });
  });
});

describe("offboard_workspace", () => {
  // #1127: the executor is built and owner-gated at the port, and this row is
  // its web surface — an owner action, explicit and confirmed.
  const spec = COMMAND_SPECS.offboard_workspace;

  it("offers offboard_workspace", () => {
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

describe("resolve_review — the review card's three resolutions", () => {
  it("forwards the intent and the resolution, and nothing else", () => {
    const parsed = parseCommand("resolve_review", {
      intent_id: UUID,
      resolution: "posted",
      episode: "2026-09-12T10:00:00+00:00",
      extra: "ignored",
    });
    expect(parsed).toEqual({
      ok: true,
      body: { intent_id: UUID, resolution: "posted" },
      identity: `${UUID}:posted:2026-09-12T10:00:00+00:00`,
    });
  });

  it("forwards the one verdict the port knows, and drops any other", () => {
    const withVerdict = parseCommand("resolve_review", {
      intent_id: UUID,
      resolution: "retry",
      verdict: "not_posted",
    });
    expect(withVerdict.ok && withVerdict.body).toEqual({
      intent_id: UUID,
      resolution: "retry",
      verdict: "not_posted",
    });
    const other = parseCommand("resolve_review", {
      intent_id: UUID,
      resolution: "retry",
      verdict: "posted",
    });
    expect(other.ok && other.body).toEqual({ intent_id: UUID, resolution: "retry" });
  });

  it("keys on the review episode, so a second review of the same post is a new command and a double-click is not", () => {
    const first = parseCommand("resolve_review", { intent_id: UUID, resolution: "retry", episode: "t1" });
    const again = parseCommand("resolve_review", { intent_id: UUID, resolution: "retry", episode: "t1" });
    const later = parseCommand("resolve_review", { intent_id: UUID, resolution: "retry", episode: "t2" });
    expect(first.ok && again.ok && later.ok).toBe(true);
    if (!first.ok || !again.ok || !later.ok) return;
    expect(idempotencyKeyFor("resolve_review", first.identity)).toBe(
      idempotencyKeyFor("resolve_review", again.identity),
    );
    expect(idempotencyKeyFor("resolve_review", first.identity)).not.toBe(
      idempotencyKeyFor("resolve_review", later.identity),
    );
    // Without an episode the key is still per (intent, resolution).
    const bare = parseCommand("resolve_review", { intent_id: UUID, resolution: "cancel" });
    expect(bare.ok && bare.identity).toBe(`${UUID}:cancel`);
  });

  it("refuses a resolution the port does not know, by name, before any round trip", () => {
    for (const bad of ["failed", "", undefined, 3, "RETRY"]) {
      expect(parseCommand("resolve_review", { intent_id: UUID, resolution: bad }), String(bad)).toEqual({
        ok: false,
        error: "invalid_resolution",
      });
    }
    expect(parseCommand("resolve_review", { intent_id: "nope", resolution: "retry" })).toEqual({
      ok: false,
      error: "invalid_intent",
    });
  });
});

describe("invite_member — the web's invitation is an email invitation, and only that", () => {
  it("passes the address and the role through as typed: the port validates both", () => {
    expect(
      parseCommand("invite_member", {
        submission_id: UUID,
        email: "Partner@Example.com",
        role: "admin",
      }),
    ).toEqual({
      ok: true,
      body: { email: "Partner@Example.com", role: "admin" },
      identity: UUID,
    });
  });

  it("sends nothing else, so the browser cannot pick the Telegram arm or name a Telegram user", () => {
    const parsed = parseCommand("invite_member", {
      submission_id: UUID,
      email: "partner@example.com",
      role: "member",
      delivery_channel: "telegram",
      invited_tg_user_id: 42,
    });
    expect(parsed.ok && Object.keys(parsed.body).sort()).toEqual(["email", "role"]);
  });

  it("refuses an address or a role that is not a string before any round trip", () => {
    for (const bad of [undefined, null, 3, ["a@b.co"]]) {
      expect(
        parseCommand("invite_member", { submission_id: UUID, email: bad, role: "member" }),
        String(bad),
      ).toEqual({ ok: false, error: "invalid_email" });
      expect(
        parseCommand("invite_member", { submission_id: UUID, email: "a@b.co", role: bad }),
        String(bad),
      ).toEqual({ ok: false, error: "invalid_role" });
    }
  });
});

describe("revoke_invitation — an admin withdraws a pending invitation", () => {
  it("forwards the invitation's id and nothing else", () => {
    expect(
      parseCommand("revoke_invitation", {
        submission_id: UUID,
        invitation_id: UUID2,
        email: "partner@example.com",
      }),
    ).toEqual({ ok: true, body: { invitation_id: UUID2 }, identity: UUID });
  });

  it("refuses an id that is not one before any round trip", () => {
    for (const bad of [undefined, null, 7, "not-an-id"]) {
      expect(
        parseCommand("revoke_invitation", { submission_id: UUID, invitation_id: bad }),
        String(bad),
      ).toEqual({ ok: false, error: "invalid_invitation_id" });
    }
  });
});
