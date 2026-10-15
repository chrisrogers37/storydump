/**
 * Destination readiness on the overview (#1644): the server's
 * `destinations` and `guidance` read and said, never derived. The fixtures are
 * the mix resource's own shape (`versioned_mix.resource` with
 * `category_mix.destination_projection`), one per case:
 * A-only with B's past posts Off, a handle-only B, a shared Drive folder, an
 * inactive B, every source Off, no source at all, partial coverage beside
 * ready, and an admin beside a member. Protocol 1 keeps row existence.
 * Every handle is synthetic.
 */

import { describe, expect, it } from "vitest";
import { RESOLVED_IN, nextSetupStep } from "./conditions";
import type { DestinationSourceView } from "./source-mix";
import {
  exclusionSentence,
  indexedBackLine,
  overviewSetup,
  parseReadiness,
  readinessStateLabel,
  readinessTone,
  readinessView,
  runwayNotes,
  sourceStatusBadges,
  type Readiness,
} from "./readiness";

const A = "55555555-5555-4555-8555-555555555555";
const B = "66666666-6666-4666-8666-666666666666";
const C = "88888888-8888-4888-8888-888888888888";
const IG_A = "22222222-2222-4222-8222-222222222222";
const IG_B = "44444444-4444-4444-8444-444444444444";
const DRIVE = "33333333-3333-4333-8333-333333333333";

function igRow(id: string, handle: string, enabled = true) {
  return {
    source_id: id,
    provider: "instagram_archive",
    state: "active",
    origin_account_id: id === IG_A ? A : B,
    origin_handle: handle,
    name: handle,
    enabled,
    ratio: enabled ? "1" : "0",
    target_share: enabled ? "1" : "0",
    media_count: 700,
    indexed: 680,
  };
}

function driveRow(over: Record<string, unknown> = {}) {
  return {
    source_id: DRIVE,
    provider: "gdrive",
    state: "active",
    origin_account_id: null,
    origin_handle: null,
    name: "Event photos",
    enabled: true,
    ratio: "1",
    target_share: "1",
    media_count: 40,
    indexed: null,
    ...over,
  };
}

function source(id: string, over: Record<string, unknown> = {}) {
  return {
    source_id: id,
    assignable: true,
    candidates: 0,
    eligible: 0,
    effective_share: "0",
    exclusions: {},
    indexing: false,
    partial_coverage: false,
    stalled: false,
    needs_reconnect: false,
    oldest_observed_at: null,
    ...over,
  };
}

function destination(account_id: string, handle: string, over: Record<string, unknown> = {}) {
  return {
    account_id,
    handle,
    display_name: null,
    state: "ready",
    tone: "active",
    partial_coverage: false,
    candidates: 124,
    eligible: 3,
    leading_exclusion: null,
    remedy: null,
    sources: [],
    ...over,
  };
}

function mix(rows: unknown[], destinations: unknown[], guidance: unknown) {
  return {
    protocol_version: 2,
    policy_version: 1,
    revision: 8,
    mode: "equal",
    supported_actions: ["reset_equal", "set_enabled", "save_custom"],
    conversion: { state: "complete", kind: "new_equal" },
    archive_available: true,
    rows,
    destinations,
    guidance,
    reset_explanation: null,
  };
}

function read(data: unknown): Readiness {
  const readiness = parseReadiness(data);
  if (!readiness) throw new Error("fixture did not parse");
  return readiness;
}

/** A's archive On and ready; B's archive Off; no Drive. */
const A_ONLY_B_OFF = mix(
  [igRow(IG_A, "example_a"), igRow(IG_B, "example_b", false)],
  [
    destination(A, "example_a", { sources: [source(IG_A, { candidates: 124 }), source(IG_B, { assignable: false })] }),
    destination(B, "example_b", {
      state: "no_assignable_source",
      tone: "attention",
      remedy: "enable_history",
      candidates: 0,
      sources: [source(IG_A, { assignable: false }), source(IG_B)],
    }),
  ],
  {
    active: 2,
    ready: 1,
    intentional: null,
    incomplete: [{ account_id: B, state: "no_assignable_source", remedy: "enable_history" }],
  },
);

describe("reading the resource", () => {
  it("reads a protocol-2 answer's readiness, and nothing else", () => {
    const readiness = read(A_ONLY_B_OFF);
    expect(readiness.guidance).toEqual({ active: 2, ready: 1, intentional: null, incomplete: [B] });
    expect(readiness.destinations.map((d) => [d.handle, d.state, d.tone, d.remedy])).toEqual([
      ["example_a", "ready", "active", null],
      ["example_b", "no_assignable_source", "attention", "enable_history"],
    ]);
    expect(parseReadiness({ rows: [] })).toBeNull();
    expect(parseReadiness({ protocol_version: 1, rows: [] })).toBeNull();
    // A protocol-2 answer with no guidance keeps the setup step.
    expect(parseReadiness({ ...A_ONLY_B_OFF, guidance: undefined })).toBeNull();
    expect(parseReadiness({ ...A_ONLY_B_OFF, guidance: { active: 2, ready: 1, intentional: "maybe", incomplete: [] } })).toBeNull();
  });

  it("maps the server's tones onto the badge tones through a closed table", () => {
    for (const tone of ["inert", "attention", "progress", "active", "problem"]) {
      expect(readinessTone(tone)).toBe(tone);
    }
    expect(readinessTone("green")).toBe("attention");
    expect(readinessTone(undefined)).toBe("attention");
    expect(readinessStateLabel("indexing")).toBe("Indexing");
    expect(readinessStateLabel("launching")).toBe("Unknown state");
  });
});

describe("the guidance, for an admin and for a member", () => {
  it("A-only with B's past posts Off: B needs its history enabled, and one ready account does not finish setup", () => {
    const view = readinessView(read(A_ONLY_B_OFF), { isAdmin: true })!;
    expect(view.title).toBe("1 of 2 accounts ready");
    expect(view.intentional).toBe(false);
    expect(view.lines).toEqual([
      {
        key: B,
        name: "@example_b",
        badges: [{ label: "No source", tone: "attention" }],
        detail: "@example_b's past posts are Off, and no Google Drive folder is on.",
        coverage: null,
        action: { href: RESOLVED_IN.mix.href, label: "Turn on @example_b's past posts" },
        ask: null,
      },
    ]);
    const member = readinessView(read(A_ONLY_B_OFF), { isAdmin: false })!;
    expect(member.lines[0].action).toBeNull();
    expect(member.lines[0].ask).toBe("Ask an admin to enable @example_b's past posts or add a Drive folder.");
  });

  it("a handle-only B needs a connection, and the remedy opens Accounts", () => {
    const data = mix(
      [igRow(IG_A, "example_a")],
      [
        destination(A, "example_a"),
        destination(B, "example_b", { state: "needs_connection", tone: "attention", remedy: "connect", candidates: 0 }),
      ],
      { active: 2, ready: 1, intentional: null, incomplete: [{ account_id: B, state: "needs_connection", remedy: "connect" }] },
    );
    const [line] = readinessView(read(data), { isAdmin: true })!.lines;
    expect(line.badges).toEqual([{ label: "Not connected", tone: "attention" }]);
    expect(line.action).toEqual({ href: RESOLVED_IN.accounts.href, label: "Connect @example_b" });
    expect(readinessView(read(data), { isAdmin: false })!.lines[0].ask).toBe("Ask an admin to connect @example_b.");
  });

  it("an enabled shared Drive folder with matching posts makes both ready: nothing to say", () => {
    const data = mix(
      [driveRow()],
      [
        destination(A, "example_a", { sources: [source(DRIVE, { candidates: 12 })] }),
        destination(B, "example_b", { sources: [source(DRIVE, { candidates: 12 })] }),
      ],
      { active: 2, ready: 2, intentional: null, incomplete: [] },
    );
    expect(readinessView(read(data), { isAdmin: true })).toBeNull();
    expect(overviewSetup(read(data), { accounts: [{}, {}], sources: [] }, { isAdmin: true })).toEqual({
      setupStep: null,
      readiness: null,
    });
  });

  it("an inactive B is left out of the count and the list", () => {
    const data = mix(
      [igRow(IG_A, "example_a")],
      [
        destination(A, "example_a", { state: "indexing", tone: "progress", candidates: 0 }),
        destination(B, "example_b", { state: "inactive", tone: "inert", candidates: 0 }),
      ],
      { active: 1, ready: 0, intentional: null, incomplete: [{ account_id: A, state: "indexing", remedy: null }] },
    );
    const view = readinessView(read(data), { isAdmin: true })!;
    expect(view.title).toBe("0 of 1 account ready");
    expect(view.lines.map((l) => l.name)).toEqual(["@example_a"]);
    // Indexing is progress, with no remedy to press.
    expect(view.lines[0].badges).toEqual([{ label: "Indexing", tone: "progress" }]);
    expect(view.lines[0].action).toBeNull();
  });

  it("every source Off is an intentional state: a choice with an optional control, no setup pressure", () => {
    const data = mix(
      [igRow(IG_A, "example_a", false), driveRow({ enabled: false, ratio: "0", target_share: "0" })],
      [
        destination(A, "example_a", { state: "sources_off", tone: "inert", candidates: 0 }),
        destination(B, "example_b", { state: "sources_off", tone: "inert", candidates: 0 }),
      ],
      { active: 2, ready: 0, intentional: "all_sources_off", incomplete: [] },
    );
    const admin = readinessView(read(data), { isAdmin: true })!;
    expect(admin).toEqual({
      intentional: true,
      title: "Every source is Off",
      detail: "Nothing is drawn while every source is Off. Turn one on in Posting mix whenever you want posts again.",
      action: { href: RESOLVED_IN.mix.href, label: "Open Posting mix" },
      lines: [],
    });
    expect(readinessView(read(data), { isAdmin: false })!.action).toBeNull();
    expect(JSON.stringify(admin)).not.toMatch(/get started|set up your/i);
  });

  it("every account inactive is intentional too", () => {
    const data = mix(
      [driveRow()],
      [destination(A, "example_a", { state: "inactive", tone: "inert" })],
      { active: 0, ready: 0, intentional: "all_inactive", incomplete: [] },
    );
    const view = readinessView(read(data), { isAdmin: true })!;
    expect(view.title).toBe("Every account is inactive");
    expect(view.action).toEqual({ href: RESOLVED_IN.accounts.href, label: "Open Accounts" });
  });

  it("no source at all is setup, not a choice: add a Drive folder, in the server's order", () => {
    const data = mix(
      [],
      [
        destination(A, "example_a", { state: "no_assignable_source", tone: "attention", remedy: "add_source", candidates: 0 }),
        destination(C, "example_c", { state: "needs_connection", tone: "attention", remedy: "connect", candidates: 0 }),
      ],
      {
        active: 2,
        ready: 0,
        intentional: null,
        incomplete: [
          { account_id: C, state: "needs_connection", remedy: "connect" },
          { account_id: A, state: "no_assignable_source", remedy: "add_source" },
        ],
      },
    );
    const view = readinessView(read(data), { isAdmin: true })!;
    expect(view.intentional).toBe(false);
    expect(view.lines.map((l) => [l.name, l.action])).toEqual([
      ["@example_c", { href: RESOLVED_IN.accounts.href, label: "Connect @example_c" }],
      ["@example_a", { href: RESOLVED_IN.integrations.href, label: "Add a Drive folder for @example_a" }],
    ]);
    expect(view.lines[1].detail).toBe("@example_a has nothing to post from yet.");
  });

  it("partial coverage is its own badge beside ready, with the oldest post as evidence only", () => {
    const data = mix(
      [igRow(IG_A, "example_a")],
      [
        destination(A, "example_a", {
          partial_coverage: true,
          sources: [source(IG_A, { candidates: 124, partial_coverage: true, oldest_observed_at: "2024-03-03T09:00:00+00:00" })],
        }),
      ],
      { active: 1, ready: 1, intentional: null, incomplete: [] },
    );
    const view = readinessView(read(data), { isAdmin: true })!;
    expect(view.title).toBe("1 of 1 account ready");
    expect(view.lines).toEqual([
      {
        key: A,
        name: "@example_a",
        badges: [
          { label: "Ready", tone: "active" },
          { label: "Partial coverage", tone: "attention" },
        ],
        detail: "124 matching posts. Media is checked when used. Not every past post is indexed yet.",
        coverage: "Oldest post indexed so far: March 3, 2024.",
        action: null,
        ask: null,
      },
    ]);
    // No percentage, and no claim that the history is complete.
    expect(JSON.stringify(view)).not.toMatch(/%|all (of )?your past posts|fully indexed|complete/i);
  });

  it("says what is left out so far while indexing, and leads with it when nothing matches", () => {
    const leading = { reason: "too_recent", count: 42 };
    const indexing = mix(
      [igRow(IG_A, "example_a")],
      [destination(A, "example_a", { state: "indexing", tone: "progress", candidates: 0, leading_exclusion: leading })],
      { active: 1, ready: 0, intentional: null, incomplete: [{ account_id: A, state: "indexing", remedy: null }] },
    );
    expect(readinessView(read(indexing), { isAdmin: true })!.lines[0].detail).toBe(
      "Indexing @example_a's past posts. Matching posts appear as indexing continues. So far, 42 posts are left out because they are newer than the minimum age.",
    );
    const none = mix(
      [igRow(IG_A, "example_a")],
      [destination(A, "example_a", { state: "zero_eligible", tone: "attention", remedy: "review_policy", candidates: 0, leading_exclusion: leading })],
      { active: 1, ready: 0, intentional: null, incomplete: [{ account_id: A, state: "zero_eligible", remedy: "review_policy" }] },
    );
    const [line] = readinessView(read(none), { isAdmin: true })!.lines;
    expect(line.detail).toBe("No posts match for @example_a yet. 42 posts are left out because they are newer than the minimum age.");
    expect(line.action).toEqual({ href: RESOLVED_IN.mix.href, label: "Review @example_a's past-posts settings" });
    expect(readinessView(read(none), { isAdmin: false })!.lines[0].ask).toBe(
      "Ask an admin to review @example_a's past-posts settings.",
    );
  });

  it("sends a retry where the stopped source is: Drive to Integrations, past posts to Posting mix", () => {
    const stalled = mix(
      [igRow(IG_A, "example_a")],
      [destination(A, "example_a", { state: "stalled", tone: "problem", remedy: "retry", candidates: 0, sources: [source(IG_A, { stalled: true })] })],
      { active: 1, ready: 0, intentional: null, incomplete: [{ account_id: A, state: "stalled", remedy: "retry" }] },
    );
    expect(readinessView(read(stalled), { isAdmin: true })!.lines[0]).toMatchObject({
      badges: [{ label: "Indexing stalled", tone: "problem" }],
      action: { href: RESOLVED_IN.mix.href, label: "Check @example_a's past posts" },
    });
    const broken = mix(
      [driveRow({ state: "error" })],
      [destination(A, "example_a", { state: "error", tone: "problem", remedy: "retry", candidates: 0, sources: [source(DRIVE)] })],
      { active: 1, ready: 0, intentional: null, incomplete: [{ account_id: A, state: "error", remedy: "retry" }] },
    );
    expect(readinessView(read(broken), { isAdmin: true })!.lines[0].action).toEqual({
      href: RESOLVED_IN.integrations.href,
      label: "Check @example_a's Drive folder",
    });
  });

  it("names an unknown exclusion plainly rather than inventing a cause", () => {
    expect(exclusionSentence({ reason: "something_new", count: 1 })).toBe(
      "1 post is left out because they cannot be used right now.",
    );
  });
});

describe("the switch between the two protocols", () => {
  const progress = { accounts: [{}], sources: [] };

  it("keeps row existence for protocol 1, and for an API that predates the field", () => {
    for (const data of [{ rows: [] }, { protocol_version: 1, rows: [] }, { ...A_ONLY_B_OFF, guidance: null }]) {
      const { setupStep, readiness } = overviewSetup(parseReadiness(data), progress, { isAdmin: true });
      expect(readiness).toBeNull();
      expect(setupStep).toEqual(nextSetupStep(progress));
      expect(setupStep?.title).toBe("Connect Google Drive and pick a folder");
    }
  });

  it("guides a converted workspace by readiness: no Drive step where an account's own past posts serve", () => {
    const { setupStep, readiness } = overviewSetup(read(A_ONLY_B_OFF), progress, { isAdmin: true });
    expect(setupStep).toBeNull();
    expect(readiness).toEqual(readinessView(read(A_ONLY_B_OFF), { isAdmin: true }));
  });

  it("still asks for an account first when a converted workspace has none", () => {
    const empty = mix([driveRow()], [], { active: 0, ready: 0, intentional: null, incomplete: [] });
    expect(overviewSetup(read(empty), { accounts: [], sources: [] }, { isAdmin: false })).toEqual({
      setupStep: nextSetupStep({ accounts: [], sources: [] }, { isAdmin: false }),
      readiness: null,
    });
  });
});

describe("runwayNotes", () => {
  it("explains every account that is not ready, and no other", () => {
    expect(runwayNotes(read(A_ONLY_B_OFF))).toEqual({
      [B]: "@example_b's past posts are Off, and no Google Drive folder is on.",
    });
    expect(runwayNotes(null)).toEqual({});
  });
});

describe("a source's own status on the Posting mix, as the viewed account's entry sent it", () => {
  /** A destination's entry for a source with none of the progress facts sent. */
  const view = (over: Partial<DestinationSourceView> = {}): DestinationSourceView => ({
    sourceId: IG_A,
    assignable: true,
    candidates: 124,
    effectiveShare: "1",
    indexing: null,
    partialCoverage: null,
    stalled: null,
    needsReconnect: null,
    oldestObservedAt: null,
    ...over,
  });

  it("draws one badge per fact, in the destination badges' own words and tones", () => {
    expect(sourceStatusBadges(view({ indexing: true }))).toEqual([{ label: "Indexing", tone: "progress" }]);
    expect(sourceStatusBadges(view({ stalled: true }))).toEqual([{ label: "Indexing stalled", tone: "problem" }]);
    expect(sourceStatusBadges(view({ needsReconnect: true }))).toEqual([
      { label: "Reconnect needed", tone: "attention" },
    ]);
    expect(sourceStatusBadges(view({ partialCoverage: true }))).toEqual([
      { label: "Partial coverage", tone: "attention" },
    ]);
  });

  it("uses the words the overview's badges use for the same states", () => {
    for (const [status, state] of [
      ["indexing", "indexing"],
      ["stalled", "stalled"],
      ["needsReconnect", "needs_reconnect"],
    ] as const) {
      const [badge] = sourceStatusBadges(view({ [status]: true }));
      expect(badge.label, status).toBe(readinessStateLabel(state));
    }
  });

  it("draws partial coverage beside the status, since it is a separate fact", () => {
    expect(sourceStatusBadges(view({ indexing: true, partialCoverage: true }))).toEqual([
      { label: "Indexing", tone: "progress" },
      { label: "Partial coverage", tone: "attention" },
    ]);
  });

  it("draws the one status that says most, never a contradicting pair", () => {
    // Stalled is indexing past its budget: the API sends both, and "Indexing"
    // beside "Indexing stalled" would say two things about one source.
    expect(sourceStatusBadges(view({ indexing: true, stalled: true }))).toEqual([
      { label: "Indexing stalled", tone: "problem" },
    ]);
    // A source that needs reconnecting is not progressing until it does.
    expect(sourceStatusBadges(view({ indexing: true, stalled: true, needsReconnect: true }))).toEqual([
      { label: "Reconnect needed", tone: "attention" },
    ]);
    expect(sourceStatusBadges(view({ stalled: true, needsReconnect: true, partialCoverage: true }))).toEqual([
      { label: "Reconnect needed", tone: "attention" },
      { label: "Partial coverage", tone: "attention" },
    ]);
  });

  it("draws nothing for a fact that is false, was not sent, or has no account to be viewed for", () => {
    expect(
      sourceStatusBadges(
        view({ indexing: false, partialCoverage: false, stalled: false, needsReconnect: false }),
      ),
    ).toEqual([]);
    // Nothing is worked out from what else the entry says: zero matching posts
    // and a share of 0 are not "indexing".
    expect(sourceStatusBadges(view({ candidates: 0, effectiveShare: "0", assignable: false }))).toEqual([]);
    expect(sourceStatusBadges(view())).toEqual([]);
    expect(sourceStatusBadges(null)).toEqual([]);
  });

  it("says how far back the index reaches, on UTC's clock, and nothing more", () => {
    expect(indexedBackLine(view({ oldestObservedAt: "2020-03-15T10:00:00+00:00" }))).toBe(
      "Indexed back to March 15, 2020",
    );
    // The day is UTC's, so a post at 23:30 UTC the night before is that day, wherever it is read.
    expect(indexedBackLine(view({ oldestObservedAt: "2020-03-14T23:30:00+00:00" }))).toBe(
      "Indexed back to March 14, 2020",
    );
    const line = indexedBackLine(view({ oldestObservedAt: "2020-03-15T10:00:00+00:00" }))!;
    expect(line).not.toMatch(/all|complete|full|history|%|progress/i);
  });

  it("says nothing without evidence of how far back it reaches", () => {
    expect(indexedBackLine(view())).toBeNull();
    expect(indexedBackLine(view({ oldestObservedAt: "not a time" }))).toBeNull();
    expect(indexedBackLine(null)).toBeNull();
  });
});
