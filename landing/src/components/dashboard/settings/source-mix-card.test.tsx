import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  LONG_RUN_COPY,
  MEDIA_CHECKED_COPY,
  MEMBERSHIP_RESET_COPY,
  offCopy,
  parseSourceMix,
  resetEqualAction,
  setEnabledAction,
  type SourceMix,
} from "@/lib/source-mix";
import {
  INITIAL_FLOW,
  mixFlowReducer,
  type MixFlow,
  type MixFlowEvent,
} from "@/lib/source-mix-flow";
import { SourceMixView, type SourceMixHandlers } from "./source-mix-card";

/**
 * The posting-mix card of a converted workspace, drawn from each state
 * (`environment: "node"`: markup, not a browser). `SourceMixView` takes no hooks
 * of its own, so a state is a `current` mix and a `flow`, and what a person
 * would see, hear and be able to press is read back from the markup. What
 * presses and dispatches is `source-mix-flow.test.ts`'s.
 *
 * Every fixture is synthetic: `example_handle` is no one's account.
 */

const IG = "22222222-2222-4222-8222-222222222222";
const DRIVE = "33333333-3333-4333-8333-333333333333";
const EVENTS = "44444444-4444-4444-8444-444444444444";
const ACCOUNT_A = "55555555-5555-4555-8555-555555555555";
const ACCOUNT_B = "66666666-6666-4666-8666-666666666666";

function igRow(over: Record<string, unknown> = {}) {
  return {
    source_id: IG,
    provider: "instagram_past_posts",
    name: "example_handle",
    state: "active",
    origin_handle: "example_handle",
    enabled: true,
    ratio: "0.5",
    target_share: "0.5",
    media_count: 700,
    indexed: 680,
    ...over,
  };
}

function driveRow(over: Record<string, unknown> = {}) {
  return {
    source_id: DRIVE,
    provider: "gdrive",
    name: "Event photos",
    state: "active",
    enabled: true,
    ratio: "0.5",
    target_share: "0.5",
    media_count: 40,
    ...over,
  };
}

const ONE_ACCOUNT = [
  {
    account_id: ACCOUNT_A,
    handle: "example_handle",
    display_name: "Example",
    sources: [
      { source_id: IG, assignable: true, candidates: 124, effective_share: "0.5" },
      { source_id: DRIVE, assignable: true, candidates: 12, effective_share: "0.5" },
    ],
  },
];

/** A second account the past-posts source cannot serve: its whole share is Drive's. */
const TWO_ACCOUNTS = [
  ...ONE_ACCOUNT,
  {
    account_id: ACCOUNT_B,
    handle: "other_handle",
    display_name: "Other",
    sources: [
      { source_id: IG, assignable: false, candidates: 0, effective_share: "0" },
      { source_id: DRIVE, assignable: true, candidates: 9, effective_share: "1" },
    ],
  },
];

function mix(over: Record<string, unknown> = {}): SourceMix {
  const parsed = parseSourceMix({
    protocol_version: 2,
    revision: 8,
    mode: "equal",
    supported_actions: ["reset_equal", "set_enabled", "save_custom"],
    conversion: { state: "complete", kind: "new_equal" },
    rows: [igRow(), driveRow()],
    destinations: ONE_ACCOUNT,
    past_posts_available: true,
    ...over,
  });
  if (!parsed) throw new Error("fixture did not parse");
  return parsed;
}

/** Every source Off: the custom mix a person can end up with. */
const ALL_OFF = {
  mode: "custom",
  rows: [
    igRow({ enabled: false, ratio: "0", target_share: "0" }),
    driveRow({ enabled: false, ratio: "0", target_share: "0" }),
  ],
};

const on = new Proxy({}, { get: () => () => {} }) as SourceMixHandlers;

function draw(
  current: SourceMix | null,
  options: { flow?: MixFlow; editable?: boolean; viewAs?: string | null } = {},
): string {
  return renderToStaticMarkup(
    createElement(SourceMixView, {
      current,
      flow: options.flow ?? INITIAL_FLOW,
      editable: options.editable ?? true,
      viewAs: options.viewAs ?? null,
      ids: "ID",
      workspaceId: "ws-1",
      on,
    }),
  );
}

const flowOf = (events: MixFlowEvent[]): MixFlow => events.reduce(mixFlowReducer, INITIAL_FLOW);

// ── Reading markup ─────────────────────────────────────────────────────────

/**
 * The visible text: inline spans dropped as they are written, every other tag
 * a space, entities read, white space folded.
 */
function textOf(html: string): string {
  return html
    .replace(/<\/?span[^>]*>/g, "")
    .replace(/<[^>]*>/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
}

/** A value that must be there: a markup reader that finds nothing is a failure, not a pass. */
function must<T>(value: T | undefined, what: string): T {
  if (value === undefined) throw new Error(`no ${what} in the markup`);
  return value;
}

function attr(tag: string, name: string): string | undefined {
  return tag.match(new RegExp(`\\s${name}="([^"]*)"`))?.[1];
}

const isDisabled = (tag: string) => /\sdisabled(?:=""|\s|>)/.test(tag);

/** The opening tag of the button that holds exactly this label, or "". */
function button(html: string, label: string): string {
  const end = html.indexOf(`>${label}</button>`);
  return end < 0 ? "" : html.slice(html.lastIndexOf("<button", end), end + 1);
}

/** The opening tag of every switch, in the order they are drawn. */
function switches(html: string): string[] {
  return html.match(/<button[^>]*role="switch"[^>]*>/g) ?? [];
}

function switchNamed(html: string, name: string): string {
  const found = switches(html).find((tag) => attr(tag, "aria-label") === name);
  if (!found) throw new Error(`no switch named "${name}" in ${switches(html).map((t) => attr(t, "aria-label"))}`);
  return found;
}

/** The text of the paragraph with this id. */
function textAt(html: string, id: string): string {
  const found = html.match(new RegExp(`id="${id}"[^>]*>([\\s\\S]*?)</p>`));
  if (!found) throw new Error(`no paragraph with id ${id}`);
  return textOf(found[1]);
}

describe("a row says what a source is and what it holds", () => {
  it("titles Instagram as Instagram past posts with its handle, and Drive by its folder", () => {
    const text = textOf(draw(mix()));
    expect(text).toContain("Instagram past posts");
    expect(text).toContain("@example_handle · Instagram");
    expect(text).toContain("Event photos");
    expect(text).toContain("Google Drive");
    // The handle's `@` is added once, however the API wrote it.
    expect(textOf(draw(mix({ rows: [igRow({ origin_handle: "@example_handle" }), driveRow()] })))).toContain(
      "@example_handle · Instagram",
    );
    expect(text).not.toContain("@@");
  });

  it("counts matching posts for the viewed account beside what is indexed, and files for a folder", () => {
    const text = textOf(draw(mix()));
    expect(text).toContain("124 matching items · 680 posts indexed");
    expect(text).toContain("12 matching posts · 40 files");
  });

  it("shows each source's share of the mix, or Off", () => {
    const text = textOf(draw(mix()));
    expect(text.match(/50%/g)).toHaveLength(2);
    const thirds = textOf(
      draw(
        mix({
          rows: [
            igRow({ ratio: "0.3333", target_share: "0.3333" }),
            driveRow({ ratio: "0.3333", target_share: "0.3333" }),
            driveRow({ source_id: EVENTS, name: "Events", ratio: "0.3334", target_share: "0.3334" }),
          ],
          destinations: [],
        }),
      ),
    );
    // Thirds add up to 100 on screen: the extra tenth goes to the largest remainder.
    expect(thirds).toMatch(/33\.3%/);
    expect(thirds).toMatch(/33\.4%/);
    expect(textOf(draw(mix(ALL_OFF)))).toMatch(/Off.*Off/);
  });

  it("says media is checked when used and shares are long-run targets, and never calls a past post verified or published", () => {
    const text = textOf(draw(mix()));
    expect(text).toContain(MEDIA_CHECKED_COPY);
    expect(text).toContain(LONG_RUN_COPY);
    expect(text).not.toMatch(/verif|publish|already posted|ready to post/i);
  });

  it("shows the mode as the read says it: Equal shares, or Custom percentages", () => {
    expect(textOf(draw(mix({ mode: "equal" })))).toContain("Equal shares");
    const custom = textOf(draw(mix({ mode: "custom" })));
    expect(custom).toContain("Custom percentages");
    expect(custom).not.toContain("Equal shares");
  });
});

describe("one account keeps the simple list; several are told apart", () => {
  it("draws no selector and no Workspace targets heading for one destination", () => {
    const html = draw(mix());
    expect(html).not.toContain("<select");
    expect(textOf(html)).not.toContain("Workspace target");
    expect(textOf(html)).not.toContain("View as");
  });

  it("labels the shares set here Workspace targets, and adds a View as selector with each account", () => {
    const html = draw(mix({ destinations: TWO_ACCOUNTS }));
    const text = textOf(html);
    expect(text).toContain("Workspace targets");
    expect(text).toContain("Workspace target 50%");
    expect(html).toContain('<label for="ID-view-as"');
    expect(html).toMatch(/<select[^>]*id="ID-view-as"/);
    expect(html).toContain(">@example_handle</option>");
    expect(html).toContain(">@other_handle</option>");
    expect(text).toContain(LONG_RUN_COPY);
  });

  it("shows the viewed account's read-only shares and counts, and says when a source cannot serve it", () => {
    const first = textOf(draw(mix({ destinations: TWO_ACCOUNTS }), { viewAs: ACCOUNT_A }));
    expect(first).toContain("@example_handle 50%");
    expect(first).toContain("124 matching items · 680 posts indexed");
    expect(first).not.toContain("Not used for");

    const second = textOf(draw(mix({ destinations: TWO_ACCOUNTS }), { viewAs: ACCOUNT_B }));
    expect(second).toContain("Not used for @other_handle");
    expect(second).toContain("@other_handle 0%");
    expect(second).toContain("@other_handle 100%");
    expect(second).toContain("9 matching posts · 40 files");
    // The workspace's own targets do not move with the account viewed.
    expect(second).toContain("Workspace target 50%");
  });
});

describe("Instagram past posts row says its own status, for the account being viewed", () => {
  /** What the API sends for one source in one destination's entry: its progress facts, as `instagram_sync.status` has them. */
  const progress = (over: Record<string, unknown> = {}) => ({
    indexing: false,
    partial_coverage: false,
    stalled: false,
    needs_reconnect: false,
    oldest_observed_at: null,
    ...over,
  });

  const entry = (accountId: string, handle: string, igProgress: Record<string, unknown>, assignable = true) => ({
    account_id: accountId,
    handle,
    display_name: handle,
    sources: [
      { source_id: IG, assignable, candidates: 124, effective_share: "0.5", ...igProgress },
      // A Drive folder has no progress of its own: the API sends false and null.
      { source_id: DRIVE, assignable: true, candidates: 12, effective_share: "0.5", ...progress() },
    ],
  });

  /** Every badge drawn, as its words and its classes. */
  const badgesOf = (html: string) =>
    [...html.matchAll(/<span[^>]*data-slot="badge"[^>]*class="([^"]*)"[^>]*>([^<]*)<\/span>/g)].map(
      (m) => ({ label: m[2], classes: m[1] }),
    );

  const INDEXING = progress({
    indexing: true,
    partial_coverage: true,
    oldest_observed_at: "2020-03-15T10:00:00+00:00",
  });

  it("draws the status as badges in the closed tones, and how far back the index reaches", () => {
    const html = draw(mix({ destinations: [entry(ACCOUNT_A, "example_handle", INDEXING)] }));
    const badges = badgesOf(html);
    expect(badges.map((b) => b.label)).toEqual(["Indexing", "Partial coverage"]);
    // Progress is the site's tap orange, and partial coverage the amber of attention.
    expect(badges[0].classes).toContain("bg-tap/15");
    expect(badges[1].classes).toContain("bg-amber-100");
    expect(textOf(html)).toContain("Indexed back to March 15, 2020");
  });

  it("draws a stalled source as a problem and one that needs reconnecting as attention", () => {
    const stalled = badgesOf(
      draw(mix({ destinations: [entry(ACCOUNT_A, "example_handle", progress({ indexing: true, stalled: true }))] })),
    );
    expect(stalled.map((b) => b.label)).toEqual(["Indexing stalled"]);
    expect(stalled[0].classes).toContain("bg-alarm/10");
    const reconnect = badgesOf(
      draw(mix({ destinations: [entry(ACCOUNT_A, "example_handle", progress({ needs_reconnect: true }))] })),
    );
    expect(reconnect.map((b) => b.label)).toEqual(["Reconnect needed"]);
    expect(reconnect[0].classes).toContain("bg-amber-100");
  });

  it("is on the Instagram row, and under its counts", () => {
    const html = draw(mix({ destinations: [entry(ACCOUNT_A, "example_handle", INDEXING)] }));
    const igRowHtml = html.slice(html.indexOf("Instagram past posts"), html.indexOf("Event photos"));
    expect(igRowHtml).toContain("Indexing");
    expect(igRowHtml).toContain("Indexed back to");
    // The Drive folder's row has no status of its own to say.
    const driveRowHtml = html.slice(html.indexOf("Event photos"));
    expect(driveRowHtml).not.toMatch(/Indexing|Partial coverage|Reconnect needed|Indexed back/);
    expect(textOf(igRowHtml)).toMatch(/124 matching items · 680 posts indexed Each carousel slide is its own item\. Indexing/);
  });

  it("says the viewed account's status, not another's", () => {
    // A's entry for the past-posts source says it is indexing; B's says nothing is.
    const destinations = [
      entry(ACCOUNT_A, "example_handle", INDEXING),
      entry(ACCOUNT_B, "other_handle", progress(), false),
    ];
    const asA = draw(mix({ destinations }), { viewAs: ACCOUNT_A });
    expect(badgesOf(asA).map((b) => b.label)).toEqual(["Indexing", "Partial coverage"]);
    const asB = draw(mix({ destinations }), { viewAs: ACCOUNT_B });
    expect(badgesOf(asB)).toEqual([]);
    expect(textOf(asB)).not.toContain("Indexed back to");
  });

  it("works nothing out: no status where the API sent none, whatever else the entry says", () => {
    // The default fixture's entries carry candidates and shares and no progress facts.
    const html = draw(mix());
    expect(badgesOf(html)).toEqual([]);
    expect(textOf(html)).not.toContain("Indexed back to");
    // Zero matching posts and a share of 0 are not "indexing".
    const none = draw(
      mix({
        destinations: [
          {
            account_id: ACCOUNT_A,
            handle: "example_handle",
            sources: [{ source_id: IG, assignable: true, candidates: 0, effective_share: "0" }],
          },
        ],
      }),
    );
    expect(badgesOf(none)).toEqual([]);
    // And nothing at all with no account to view.
    expect(badgesOf(draw(mix({ destinations: [] })))).toEqual([]);
  });

  it("never states a coverage it was not given, and never a progress", () => {
    const text = textOf(draw(mix({ destinations: [entry(ACCOUNT_A, "example_handle", INDEXING)] })));
    expect(text).not.toMatch(/all (?:your )?(?:past )?posts|complete|fully|\d+\s*%\s*(?:indexed|done|complete)/i);
  });
});

describe("accounts are told apart only where their scopes differ", () => {
  /** Two accounts with the same Drive folder and nothing else: one mix, whoever is looking. */
  const SAME_SCOPE = {
    rows: [driveRow({ ratio: "1", target_share: "1" })],
    destinations: [
      {
        account_id: ACCOUNT_A,
        handle: "example_handle",
        display_name: "Example",
        sources: [{ source_id: DRIVE, assignable: true, candidates: 12, effective_share: "1" }],
      },
      {
        account_id: ACCOUNT_B,
        handle: "other_handle",
        display_name: "Other",
        sources: [{ source_id: DRIVE, assignable: true, candidates: 9, effective_share: "1" }],
      },
    ],
  };

  it("draws no selector, no Workspace targets labels and no per-account share for accounts served by the same sources", () => {
    const html = draw(mix(SAME_SCOPE));
    const text = textOf(html);
    expect(html).not.toContain("<select");
    expect(text).not.toContain("View as");
    expect(text).not.toContain("Workspace target");
    expect(text).not.toContain("@other_handle");
    expect(text).not.toContain("read-only");
    // The shares are still there, as the simple list's.
    expect(text).toContain("Event photos");
    expect(text).toContain("100%");
    expect(html).toMatch(/<p id="ID-rows" class="sr-only">Sources<\/p>/);
  });

  it("still tells the accounts apart when the same sources serve them in different shares", () => {
    // B has nothing usable in the second folder, so its mix is the first alone:
    // one account's mix must not be shown as the workspace's.
    const skewed = {
      rows: [driveRow({ ratio: "0.5", target_share: "0.5" }), driveRow({ source_id: EVENTS, name: "Events", ratio: "0.5", target_share: "0.5" })],
      destinations: [
        {
          account_id: ACCOUNT_A,
          handle: "example_handle",
          sources: [
            { source_id: DRIVE, assignable: true, candidates: 12, effective_share: "0.5" },
            { source_id: EVENTS, assignable: true, candidates: 5, effective_share: "0.5" },
          ],
        },
        {
          account_id: ACCOUNT_B,
          handle: "other_handle",
          sources: [
            { source_id: DRIVE, assignable: true, candidates: 9, effective_share: "1" },
            { source_id: EVENTS, assignable: true, candidates: 0, effective_share: "0" },
          ],
        },
      ],
    };
    const html = draw(mix(skewed), { viewAs: ACCOUNT_B });
    expect(html).toMatch(/<select[^>]*id="ID-view-as"/);
    expect(textOf(html)).toContain("Workspace targets");
    expect(textOf(html)).toContain("@other_handle 100%");
    expect(textOf(html)).toContain("@other_handle 0%");
  });

  it("names no account's matching posts as the workspace's when none is being viewed", () => {
    const text = textOf(draw(mix(SAME_SCOPE)));
    expect(text).toContain("40 files");
    expect(text).not.toMatch(/matching post/);
    // One account is the one being viewed, so its counts stand: the simple list keeps them.
    expect(textOf(draw(mix()))).toContain("12 matching posts · 40 files");
  });

  it("still draws the selector and the labels the moment the scopes differ", () => {
    const html = draw(mix({ destinations: TWO_ACCOUNTS }));
    expect(html).toMatch(/<select[^>]*id="ID-view-as"/);
    expect(textOf(html)).toContain("Workspace targets");
    expect(textOf(html)).toContain("Workspace target 50%");
    expect(html).toMatch(/<p id="ID-rows" class="text-sm font-medium">Workspace targets<\/p>/);
  });

  it("keeps the heading and the selector together: both or neither", () => {
    for (const destinations of [ONE_ACCOUNT, TWO_ACCOUNTS, SAME_SCOPE.destinations]) {
      const rows = destinations === SAME_SCOPE.destinations ? SAME_SCOPE.rows : [igRow(), driveRow()];
      const html = draw(mix({ rows, destinations }));
      const selector = html.includes("<select");
      expect(textOf(html).includes("Workspace targets"), JSON.stringify(destinations).slice(0, 40)).toBe(selector);
    }
  });

  it("lists what each account would draw in a preview only where they would draw differently", () => {
    const turnDriveOff = setEnabledAction(8, DRIVE, false);
    const previewOf = (over: Record<string, unknown>) =>
      mixFlowReducer(flowOf([{ type: "change_requested", action: turnDriveOff }]), {
        type: "preview_answered",
        action: turnDriveOff,
        result: { ok: true, mix: mix(over) },
      });
    const same = previewOf({ ...SAME_SCOPE, reset_explanation: "enabled_membership_changed" });
    expect(textOf(draw(mix(SAME_SCOPE), { flow: same }))).not.toContain("What each account would draw");
    const differ = previewOf({ destinations: TWO_ACCOUNTS, reset_explanation: "enabled_membership_changed" });
    expect(textOf(draw(mix({ destinations: TWO_ACCOUNTS }), { flow: differ }))).toContain(
      "What each account would draw:",
    );
  });
});

describe("Off is explained, per provider, and never as deleting", () => {
  it("describes each switch with what Off does for that provider", () => {
    const html = draw(mix());
    const expected: Array<[string, string]> = [
      ["Instagram past posts @example_handle", "instagram_past_posts"],
      ["Event photos (Google Drive)", "gdrive"],
    ];
    expect(switches(html)).toHaveLength(2);
    for (const [name, provider] of expected) {
      const described = attr(switchNamed(html, name), "aria-describedby");
      expect(described, name).toBeDefined();
      expect(textAt(html, described!.split(" ")[0]), name).toBe(`Off: ${offCopy(provider)}`);
    }
    expect(textOf(html)).toContain(
      "Off: Stops indexing and selecting past posts; indexed metadata stays until deletion.",
    );
    expect(textOf(html)).toContain("Off: Stops selecting this folder; syncing continues.");
    expect(textOf(html)).not.toMatch(/delete (?:your )?history|deletes/i);
  });

  it("names each switch by its source and its handle or provider", () => {
    const html = draw(mix());
    expect(switches(html).map((tag) => attr(tag, "aria-label"))).toEqual([
      "Instagram past posts @example_handle",
      "Event photos (Google Drive)",
    ]);
    // Each is a real switch: a native, focusable button that says whether it is on.
    for (const tag of switches(html)) {
      expect(tag.startsWith("<button")).toBe(true);
      expect(attr(tag, "aria-checked")).toBe("true");
    }
    const off = draw(mix(ALL_OFF));
    expect(switches(off).map((tag) => attr(tag, "aria-checked"))).toEqual(["false", "false"]);
  });
});

describe("who may change the mix", () => {
  it("shows a member every value and none of the controls that change it", () => {
    const html = draw(mix(), { editable: false });
    const text = textOf(html);
    expect(switches(html)).toEqual([]);
    for (const label of ["Edit percentages", "Reset to equal", "Confirm", "Save percentages"]) {
      expect(button(html, label), label).toBe("");
    }
    expect(text).toContain("124 matching items · 680 posts indexed");
    expect(text).toContain("Equal shares");
    expect(text.match(/50%/g)).toHaveLength(2);
    // The same mix, for an admin, has them: the controls are the role's, not missing.
    const admin = draw(mix());
    expect(switches(admin)).toHaveLength(2);
    expect(button(admin, "Edit percentages")).not.toBe("");
  });

  it("draws only the controls whose action the API lists", () => {
    const none = draw(mix({ supported_actions: [], mode: "custom" }));
    expect(switches(none)).toEqual([]);
    expect(button(none, "Edit percentages")).toBe("");
    expect(button(none, "Reset to equal")).toBe("");

    const switchOnly = draw(mix({ supported_actions: ["set_enabled"], mode: "custom" }));
    expect(switches(switchOnly)).toHaveLength(2);
    expect(button(switchOnly, "Edit percentages")).toBe("");

    const all = draw(mix({ mode: "custom" }));
    expect(button(all, "Edit percentages")).not.toBe("");
    expect(button(all, "Reset to equal")).not.toBe("");
  });

  it("offers Reset to equal for a custom mix, and not for one that is already equal", () => {
    expect(button(draw(mix({ mode: "custom" })), "Reset to equal")).not.toBe("");
    expect(button(draw(mix({ mode: "equal" })), "Reset to equal")).toBe("");
  });

  it("will not switch Instagram past posts On when they are not available, and says why on the switch", () => {
    const off = { rows: [igRow({ enabled: false, ratio: "0", target_share: "0" }), driveRow({ ratio: "1", target_share: "1" })] };
    const html = draw(mix({ past_posts_available: false, ...off }));
    const instagram = switchNamed(html, "Instagram past posts @example_handle");
    expect(isDisabled(instagram)).toBe(true);
    const described = attr(instagram, "aria-describedby")!.split(" ");
    expect(described).toHaveLength(2);
    expect(textAt(html, described[1])).toBe("Using past posts is not switched on for this workspace yet.");
    // Drive is not held by it, and the past-posts settings are not offered.
    expect(isDisabled(switchNamed(html, "Event photos (Google Drive)"))).toBe(false);
    expect(textOf(html)).not.toContain("Past posts settings");
    expect(textOf(draw(mix()))).toContain("Past posts settings");
    // Positive control: available, the same Off switch is free to turn On.
    expect(isDisabled(switchNamed(draw(mix(off)), "Instagram past posts @example_handle"))).toBe(false);
  });

  it("still lets an admin switch a past-posts source that is On Off when past posts are no longer available, and says it cannot come back yet", () => {
    const html = draw(mix({ past_posts_available: false }));
    const instagram = switchNamed(html, "Instagram past posts @example_handle");
    expect(attr(instagram, "aria-checked")).toBe("true");
    expect(isDisabled(instagram)).toBe(false);
    const described = attr(instagram, "aria-describedby")!.split(" ");
    expect(described).toHaveLength(2);
    expect(textAt(html, described[1])).toBe(
      "Past posts are paused for this workspace. You can switch this source off, but not back on yet.",
    );
    expect(textOf(html)).not.toContain("Past posts settings");
    // A person who may not edit still gets no switch and no note.
    const readOnly = draw(mix({ past_posts_available: false }), { editable: false });
    expect(textOf(readOnly)).not.toContain("paused for this workspace");
  });
});

describe("every source Off is a state, never an error", () => {
  it("says No active sources, leaves each switch free to turn one on, and compels nothing", () => {
    const html = draw(mix(ALL_OFF));
    expect(textOf(html)).toContain("No active sources.");
    expect(textOf(html)).toContain("Nothing is picked for posting while every source is off.");
    expect(textOf(html)).toContain("whenever you are ready");
    expect(html).not.toContain('role="alert"');
    for (const tag of switches(html)) expect(isDisabled(tag)).toBe(false);
    expect(button(html, "Reset to equal")).not.toBe("");
    // Nothing to put percentages on.
    expect(button(html, "Edit percentages")).toBe("");
    // The notice is gone as soon as one source is on.
    expect(textOf(draw(mix()))).not.toContain("No active sources");
  });
});

describe("how the mix came to protocol 2 is said once, as what it did", () => {
  it.each([
    ["automatic_to_equal", "equal", /Automatic shares.*became Equal shares/],
    ["explicit_preserved", "custom", /custom percentages were kept as you set them/],
    ["hybrid_to_custom", "custom", /translated once into fixed custom percentages/],
  ])("says %s while the mix is %s", (kind, mode, expected) => {
    const html = draw(mix({ mode, conversion: { state: "complete", kind } }));
    expect(textOf(html)).toMatch(expected);
    expect(html).toContain('role="status"');
  });

  it.each([
    ["automatic_to_equal", "custom"],
    ["explicit_preserved", "equal"],
    ["hybrid_to_custom", "equal"],
  ])("no longer says %s once the mix is %s", (kind, mode) => {
    // The conversion is kept in the read for good; the sentence is not, once the
    // mix it describes is gone: "became Equal shares" over "Custom percentages".
    const text = textOf(draw(mix({ mode, conversion: { state: "complete", kind } })));
    expect(text).not.toMatch(/became Equal shares|were kept as you set|translated once/);
    expect(text).toContain(mode === "custom" ? "Custom percentages" : "Equal shares");
  });

  it("says nothing of a conversion above the mode that contradicts it", () => {
    const afterCustomSave = textOf(
      draw(mix({ mode: "custom", conversion: { state: "complete", kind: "automatic_to_equal" } })),
    );
    expect(afterCustomSave).toContain("Custom percentages");
    expect(afterCustomSave).not.toMatch(/Automatic shares|Equal shares/);
  });

  it("claims no exact match with what Automatic drew when it was translated from counts", () => {
    const text = textOf(
      draw(mix({ mode: "custom", conversion: { state: "complete", kind: "hybrid_to_custom" } })),
    );
    expect(text).toMatch(/using how many items each source held/);
    expect(text).toMatch(/no longer follow file counts/);
    expect(text).not.toMatch(/identical|exactly|same ratios|unchanged/i);
  });

  it.each(["new_equal", "legacy_intent", "something_new"])("says nothing for %s", (kind) => {
    for (const mode of ["equal", "custom"]) {
      const text = textOf(draw(mix({ mode, conversion: { state: "complete", kind } })));
      expect(text).not.toMatch(/became Equal shares|were kept as you set|translated once/);
    }
  });
});

describe("the one polite place outcomes are announced", () => {
  it("is on the page before anything is said, and quiet", () => {
    const html = draw(mix());
    const regions = html.match(/<p[^>]*aria-live="polite"[^>]*>[\s\S]*?<\/p>/g) ?? [];
    expect(regions).toHaveLength(1);
    expect(regions[0]).toContain('role="status"');
    expect(regions[0]).toContain("sr-only");
    expect(textOf(must(regions[0], "live region"))).toBe("");
  });

  it("carries the flow's announcement, and is shown once there is one", () => {
    const flow = { ...INITIAL_FLOW, announcement: "Posting mix saved. Every share is as shown." };
    const html = draw(mix(), { flow });
    const region = must(html.match(/<p[^>]*aria-live="polite"[^>]*>[\s\S]*?<\/p>/g)?.[0], "live region");
    expect(textOf(region)).toBe("Posting mix saved. Every share is as shown.");
    expect(region).not.toContain("sr-only");
  });

  it("opens below the rows, so saying something never moves a control the person just pressed", () => {
    // Above the rows, the sentence turning from `sr-only` to a visible line
    // would push the card's rows down while a change is pending, moving the
    // switch just pressed from under the pointer. Below them nothing it follows
    // can move.
    const turnDriveOff = setEnabledAction(8, DRIVE, false);
    const previewing = flowOf([{ type: "change_requested", action: turnDriveOff }]);
    const ready = mixFlowReducer(previewing, {
      type: "preview_answered",
      action: turnDriveOff,
      result: { ok: true, mix: mix({ rows: [igRow({ ratio: "1", target_share: "1" }), driveRow({ enabled: false, ratio: "0", target_share: "0" })] }) },
    });
    expect(ready.announcement).not.toBe("");
    const regionStart = (html: string) => html.indexOf('<p role="status" aria-live="polite"');
    const afterRegion = (html: string) => html.slice(html.indexOf("</p>", regionStart(html)) + "</p>".length);

    for (const flow of [ready, { ...ready, announcement: "" }, INITIAL_FLOW, { ...INITIAL_FLOW, announcement: "Posting mix saved." }]) {
      const html = draw(mix(), { flow });
      expect(regionStart(html), flow.announcement).toBeGreaterThan(-1);
      // Every row, panel and control is before it; only the card's own end follows.
      expect(html.lastIndexOf("<li"), flow.announcement).toBeLessThan(regionStart(html));
      expect(html.lastIndexOf("<button"), flow.announcement).toBeLessThan(regionStart(html));
      expect(afterRegion(html), flow.announcement).toBe("</div>");
    }
    // Quiet or loud, what comes before it is the same rows in the same place.
    const loud = draw(mix(), { flow: ready });
    const quiet = draw(mix(), { flow: { ...ready, announcement: "" } });
    expect(loud.slice(0, regionStart(loud))).toBe(quiet.slice(0, regionStart(quiet)));
  });

  it("is quiet, and out of the layout, until there is something to say, and fits the card's padding when there is", () => {
    const region = (html: string) =>
      must(html.match(/<p role="status" aria-live="polite"[^>]*>[\s\S]*?<\/p>/)?.[0], "live region");
    expect(attr(region(draw(mix())), "class")).toBe("sr-only");
    const said = attr(region(draw(mix(), { flow: { ...INITIAL_FLOW, announcement: "Posting mix saved." } })), "class")!;
    expect(said.split(" ")).toEqual(expect.arrayContaining(["px-6", "break-words"]));
    expect(said).not.toContain("sr-only");
  });

  it("shows a refusal as an alert, and never as only an announcement", () => {
    const html = draw(mix(), { flow: { ...INITIAL_FLOW, error: "Nothing changed — try again shortly." } });
    expect(html).toMatch(/<div role="alert"[^>]*>Nothing changed — try again shortly\.<\/div>/);
  });
});

describe("a change is previewed in place, then confirmed", () => {
  const turnDriveOff = setEnabledAction(8, DRIVE, false);
  const previewing = flowOf([{ type: "change_requested", action: turnDriveOff }]);
  const afterDriveOff = mix({
    reset_explanation: "enabled_membership_changed",
    rows: [
      igRow({ ratio: "1", target_share: "1" }),
      driveRow({ enabled: false, ratio: "0", target_share: "0" }),
    ],
  });
  const ready = mixFlowReducer(previewing, {
    type: "preview_answered",
    action: turnDriveOff,
    result: { ok: true, mix: afterDriveOff },
  });

  it("says that a membership change resets every share, and waits for the preview before Confirm", () => {
    const html = draw(mix(), { flow: previewing });
    const text = textOf(html);
    expect(text).toContain("Turn off Event photos (Google Drive)?");
    expect(text).toContain(MEMBERSHIP_RESET_COPY);
    expect(text).toContain("Working out the new shares…");
    expect(isDisabled(button(html, "Confirm"))).toBe(true);
    expect(button(html, "Cancel")).not.toBe("");
    expect(isDisabled(button(html, "Cancel"))).toBe(false);
    // Nothing else can start meanwhile, and the switch shows what it is being turned to.
    for (const tag of switches(html)) expect(isDisabled(tag)).toBe(true);
    expect(isDisabled(button(html, "Edit percentages"))).toBe(true);
    expect(attr(switchNamed(html, "Event photos (Google Drive)"), "aria-checked")).toBe("false");
  });

  it("shows the resulting equal shares, and is confirmed only now", () => {
    const html = draw(mix(), { flow: ready });
    const text = textOf(html);
    expect(text).toContain("Instagram past posts (@example_handle · Instagram): 100%");
    expect(text).toContain("Event photos (Google Drive): Off");
    expect(isDisabled(button(html, "Confirm"))).toBe(false);
    // The shares on the rows are still the saved ones: nothing was written.
    expect(text.match(/\b50%/g)?.length).toBeGreaterThanOrEqual(2);
    // And the live region says it, for anyone not looking at the panel.
    expect(text).toMatch(/would make the shares: Instagram past posts @example_handle 100%\. Confirm or cancel\./);
  });

  it("says Reset to equal replaces custom percentages, and keeps what is Off off", () => {
    const html = draw(mix({ mode: "custom" }), {
      flow: flowOf([{ type: "change_requested", action: resetEqualAction(8) }]),
    });
    const text = textOf(html);
    expect(text).toContain("Reset every share to equal?");
    expect(text).toContain("sources that are off stay off");
    expect(text).toContain("Your custom percentages will be replaced.");
  });

  it("says a change went stale, and offers Preview again instead of Confirm", () => {
    const stale = mixFlowReducer(mixFlowReducer(ready, { type: "change_confirmed" }), {
      type: "save_answered",
      action: turnDriveOff,
      result: { ok: false, error: "mix_conflict", status: 409, currentRevision: 9 },
    });
    // The page has not read revision 9 yet.
    const waiting = draw(mix(), { flow: stale });
    expect(textOf(waiting)).toContain("Loading the current mix…");
    expect(isDisabled(button(waiting, "Preview again"))).toBe(true);
    expect(button(waiting, "Confirm")).toBe("");
    // Once it has, the change is previewed again, never replayed.
    const reloaded = draw(mix({ revision: 9 }), { flow: stale });
    expect(textOf(reloaded)).toContain("Preview again to see what the change makes now.");
    expect(isDisabled(button(reloaded, "Preview again"))).toBe(false);
    expect(button(reloaded, "Confirm")).toBe("");
  });
});

describe("a custom draft", () => {
  const custom = mix({ mode: "custom" });
  const typed = flowOf([
    { type: "draft_opened", mix: custom },
    { type: "draft_edited", sourceId: IG, text: "70" },
    { type: "draft_edited", sourceId: DRIVE, text: "20" },
  ]);

  it("gives each source that is on an input named for it, shows the saved share beside it, and the total", () => {
    const html = draw(custom, { flow: typed });
    const inputs = html.match(/<input[^>]*aria-label="[^"]*percentage"[^>]*>/g) ?? [];
    expect(inputs.map((tag) => attr(tag, "aria-label"))).toEqual([
      "Instagram past posts @example_handle percentage",
      "Event photos (Google Drive) percentage",
    ]);
    expect(inputs.map((tag) => attr(tag, "value"))).toEqual(["70", "20"]);
    expect(textOf(html)).toContain("Saved 50%");
    expect(textOf(html)).toContain("The percentages add up to 90%, not 100%.");
    expect(isDisabled(button(html, "Save percentages"))).toBe(true);
    // A mix is changed one way at a time: no switch or reset while a draft is open.
    for (const tag of switches(html)) expect(isDisabled(tag)).toBe(true);
    expect(isDisabled(button(html, "Reset to equal"))).toBe(true);
  });

  it("is saved only when the percentages add up, and an Off source gets no input", () => {
    const fixed = mixFlowReducer(typed, { type: "draft_edited", sourceId: DRIVE, text: "30" });
    const html = draw(custom, { flow: fixed });
    expect(textOf(html)).toContain("Your percentages total 100% · unsaved");
    expect(isDisabled(button(html, "Save percentages"))).toBe(false);
    expect(button(html, "Cancel")).not.toBe("");

    const oneOff = mix({
      mode: "custom",
      rows: [igRow({ ratio: "1", target_share: "1" }), driveRow({ enabled: false, ratio: "0", target_share: "0" })],
    });
    const offHtml = draw(oneOff, { flow: flowOf([{ type: "draft_opened", mix: oneOff }]) });
    const offInputs = offHtml.match(/<input[^>]*aria-label="[^"]*percentage"[^>]*>/g) ?? [];
    expect(offInputs.map((tag) => attr(tag, "aria-label"))).toEqual(["Instagram past posts @example_handle percentage"]);
  });

  it("names the source and the problem for a percentage that cannot be saved", () => {
    const zero = flowOf([
      { type: "draft_opened", mix: custom },
      { type: "draft_edited", sourceId: DRIVE, text: "0" },
    ]);
    const html = draw(custom, { flow: zero });
    expect(textOf(html)).toContain("Event photos (Google Drive) is on, so it needs a share above 0%");
    const input = must(
      html.match(/<input[^>]*aria-label="Event photos \(Google Drive\) percentage"[^>]*>/g)?.[0],
      "percentage field",
    );
    expect(attr(input, "aria-invalid")).toBe("true");
    expect(attr(input, "aria-describedby")).toBe("ID-draft-check");
    expect(isDisabled(button(html, "Save percentages"))).toBe(true);
  });

  describe("that meets a conflict", () => {
    const conflicted = mixFlowReducer(
      mixFlowReducer(typed, { type: "draft_save_started" }),
      {
        type: "save_answered",
        action: { protocol_version: 2, action: "save_custom", expected_revision: 8, rows: [] },
        result: { ok: false, error: "mix_conflict", status: 409, currentRevision: 9 },
      },
    );

    it("keeps what was typed and says it is loading the current mix, until the page has it", () => {
      const html = draw(custom, { flow: conflicted });
      const text = textOf(html);
      expect(text).toContain("The posting mix changed. Loading the current mix… Your percentages are kept.");
      expect(isDisabled(button(html, "Use my percentages on the current mix"))).toBe(true);
      expect(isDisabled(button(html, "Discard my percentages"))).toBe(false);
      // It cannot be saved from here, whatever it adds up to.
      expect(button(html, "Save percentages")).toBe("");
      // The announcement carries it to anyone not looking.
      expect(text).toContain("Your percentages are kept; review them against the current mix before saving.");
    });

    it("marks every row against the mix as it is now: kept, turned off, new, and what changed under it", () => {
      const now = mix({
        revision: 9,
        mode: "equal",
        rows: [
          igRow({ enabled: false, ratio: "0", target_share: "0" }),
          driveRow({ ratio: "1", target_share: "1" }),
          driveRow({ source_id: EVENTS, name: "Events", ratio: "0", target_share: "0", enabled: false }),
        ],
      });
      const html = draw(now, { flow: conflicted });
      const text = textOf(html);
      expect(text).toContain("Your unsaved percentages, beside the mix as it is now");
      expect(text).toContain("Instagram past posts @example_handle: yours 70% · now Off · turned off since you started");
      expect(text).toContain("Event photos (Google Drive): yours 20% · now 100% · unchanged");
      expect(text).toContain("Events (Google Drive): yours — · now Off · new since you started");
      expect(text).toContain("Nothing was saved.");
      expect(isDisabled(button(html, "Use my percentages on the current mix"))).toBe(false);
      expect(button(html, "Discard my percentages")).not.toBe("");
      expect(button(html, "Save percentages")).toBe("");
    });

    it("lists a source that is gone as removed, last, instead of dropping it unseen", () => {
      const now = mix({ revision: 9, rows: [driveRow({ ratio: "1", target_share: "1" })] });
      const text = textOf(draw(now, { flow: conflicted }));
      expect(text).toContain("Instagram past posts @example_handle: yours 70% · now — · removed since you started");
      expect(text.indexOf("Event photos")).toBeLessThan(text.indexOf("removed since you started"));
    });
  });
});

describe("on a narrow screen, long text wraps instead of running past the card", () => {
  // These tests cannot measure a 320px screen, so they pin the classes.
  // `Button` is `shrink-0 whitespace-nowrap`, so without them "Use my
  // percentages on the current mix" runs past the card at 320px, and so does
  // a handle, one unbreakable word of up to 30 characters, in the effective
  // share and in each sentence that names it. These are the classes that
  // stop each of them.
  const classesOf = (tag: string) => (attr(tag, "class") ?? "").split(" ");

  const custom = mix({ mode: "custom" });
  const typed = flowOf([
    { type: "draft_opened", mix: custom },
    { type: "draft_edited", sourceId: IG, text: "70" },
  ]);
  const conflicted = mixFlowReducer(mixFlowReducer(typed, { type: "draft_save_started" }), {
    type: "save_answered",
    action: { protocol_version: 2, action: "save_custom", expected_revision: 8, rows: [] },
    result: { ok: false, error: "mix_conflict", status: 409, currentRevision: 9 },
  });

  it("lets the draft review's buttons wrap and shrink, which Button alone does not", () => {
    const html = draw(mix({ revision: 9 }), { flow: conflicted });
    for (const label of ["Use my percentages on the current mix", "Discard my percentages"]) {
      const classes = classesOf(button(html, label));
      expect(classes, label).toEqual(expect.arrayContaining(["whitespace-normal", "min-w-0", "shrink", "h-auto"]));
      expect(classes, label).not.toContain("whitespace-nowrap");
      expect(classes, label).not.toContain("shrink-0");
    }
    // The short buttons of the same card are left as they were.
    const short = classesOf(button(draw(mix(), { flow: flowOf([{ type: "change_requested", action: resetEqualAction(8) }]) }), "Cancel"));
    expect(short).toContain("whitespace-nowrap");
  });

  it("lets a handle break anywhere in the effective share, which break-words does not", () => {
    const html = draw(mix({ destinations: TWO_ACCOUNTS }), { viewAs: ACCOUNT_B });
    expect(html).toMatch(/<span class="[^"]*\[overflow-wrap:anywhere\][^"]*">@other_handle <\/span>/);
  });

  it("lets every sentence that can name a handle wrap", () => {
    const withAnnouncement = draw(mix(), { flow: { ...INITIAL_FLOW, announcement: "Posting mix saved." } });
    expect(withAnnouncement).toMatch(/<p role="status" aria-live="polite" class="[^"]*\bbreak-words\b/);

    const twoAccounts = draw(mix({ destinations: TWO_ACCOUNTS }));
    expect(twoAccounts).toMatch(/<p class="[^"]*\bbreak-words\b[^"]*">Workspace targets are the shares set here\./);

    const previewing = draw(mix(), {
      flow: flowOf([{ type: "change_requested", action: setEnabledAction(8, IG, false) }]),
    });
    expect(previewing).toMatch(/<p id="ID-change" class="[^"]*\bbreak-words\b/);

    // The draft's problem line is a flex item, so it needs to be allowed to shrink as well.
    const zero = draw(custom, {
      flow: flowOf([
        { type: "draft_opened", mix: custom },
        { type: "draft_edited", sourceId: IG, text: "0" },
      ]),
    });
    const line = zero.match(/<p id="ID-draft-check" class="([^"]*)"/)?.[1] ?? "";
    expect(line.split(" ")).toEqual(expect.arrayContaining(["min-w-0", "break-words"]));
  });
});

describe("what is shown when there is nothing to draw", () => {
  it("says the mix could not be loaded, with no control that would write one", () => {
    const html = draw(null);
    expect(textOf(html)).toContain("The mix could not be loaded just now. Reload to try again.");
    expect(switches(html)).toEqual([]);
    expect(html).not.toContain("<button");
  });

  it("says no sources are connected, and offers no control for none", () => {
    const html = draw(mix({ rows: [], destinations: [] }));
    expect(textOf(html)).toContain("No sources are connected yet.");
    expect(switches(html)).toEqual([]);
    expect(button(html, "Edit percentages")).toBe("");
  });
});
