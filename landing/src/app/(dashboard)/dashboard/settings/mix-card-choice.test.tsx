/**
 * Which Posting mix card the settings page draws.
 *
 * The category-mix read says which protocol the workspace's mix speaks, and the
 * page picks the card by that and by nothing else: `SourceMixCard` for protocol
 * 2, the protocol-1 `CategoryWeightsCard` for protocol 1 and
 * for an API that predates the field, and NEITHER editor for a protocol this
 * page does not know, since a card that wrote a mix in a shape the API does not
 * take would be refused, or worse, taken for another one.
 *
 * Two properties of the protocol-2 branch are the design, not details. The card
 * is not keyed on the rows: a refresh after a conflict hands it the new read
 * without remounting it, and a key would throw away the draft the conflict
 * kept. And an unreadable protocol-2 read still draws that card (which says the
 * mix could not be loaded) rather than falling back to the protocol-1 card,
 * whose save would write the wrong shape.
 *
 * As in `invite-admin-gate.test.tsx`, the page, an async server component, is
 * called directly with its doors mocked and the returned element tree is read
 * without rendering it.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement, type ReactNode } from "react";

const { workspaceFetch, viewer, mixRead } = vi.hoisted(() => ({
  workspaceFetch: vi.fn(),
  viewer: { role: "admin" },
  mixRead: { current: undefined as unknown },
}));

vi.mock("@/lib/page-guards", () => ({
  requireWorkspacePage: async () => ({
    workspaceId: "ws-1",
    session: {
      userId: "u-1",
      workspaces: [{ id: "ws-1", name: "WS", role: viewer.role, state: "active" }],
    },
  }),
}));
vi.mock("@/lib/workspaces", () => ({ workspaceFetch }));
vi.mock("@/lib/target-api", () => ({ targetFetch: async () => ({ ok: true, data: { tokens: [] } }) }));
vi.mock("@/lib/session", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/session")>()),
  getSessionToken: async () => "tok",
}));

import SettingsPage from "./page";
import { CategoryWeightsCard } from "@/components/dashboard/settings/category-weights-card";
import { ScrollToHash } from "@/components/dashboard/scroll-to-hash";
import { IntegrationsTab } from "@/components/dashboard/settings/integrations-tab";
import { SourceMixCard } from "@/components/dashboard/settings/source-mix-card";
import { RESOLVED_IN } from "@/lib/conditions";
import { POSTING_MIX_ID } from "@/lib/fragment-scroll";
import { parseSourceMix } from "@/lib/source-mix";

/**
 * Every element in a returned tree. It also enters element-valued props: the
 * card reaches the page's tree as `GeneralTab`'s `categoryMix` prop, not as a
 * child.
 */
function* walk(node: ReactNode): Generator<ReactElement> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (!isValidElement(node)) return;
  yield node;
  for (const value of Object.values(node.props as Record<string, unknown>)) {
    if (isValidElement(value) || Array.isArray(value)) yield* walk(value as ReactNode);
  }
}

const ok = (data: unknown) => ({ ok: true, data });

const SOURCE = "22222222-2222-4222-8222-222222222222";

/** The protocol-1 read: folders weighed as explicit, Automatic or Off. */
const LEGACY_ROWS = [
  {
    source_id: SOURCE,
    provider: "gdrive",
    name: "Event photos",
    state: "active",
    media_count: 12,
    ratio: 0.5,
    effective: 50,
  },
];

/** The protocol-2 read, as far as the page needs it to be one the card can read. */
const CONVERTED = {
  protocol_version: 2,
  revision: 8,
  mode: "equal",
  supported_actions: ["reset_equal", "set_enabled", "save_custom"],
  conversion: { state: "complete", kind: "new_equal" },
  rows: [
    {
      source_id: SOURCE,
      provider: "gdrive",
      name: "Event photos",
      state: "active",
      enabled: true,
      ratio: "1",
      target_share: "1",
    },
  ],
  destinations: [],
};

/** Answer each read by its path; the Posting mix's read is the one under test. */
function answer(path: string) {
  switch (path) {
    case "":
      return ok({ id: "ws-1", name: "WS", state: "active", tz: "UTC" });
    case "accounts":
      return ok({ accounts: [] });
    case "sources":
      return ok({ sources: [] });
    case "bindings":
      return ok({ bindings: [] });
    case "members":
      return ok({ members: [] });
    case "stats":
      return ok({ intents_by_state: {}, media_by_state: {}, posts_by_day: [], accounts: 0, sources: 0 });
    case "drive":
      return ok({ drive: null });
    case "category-mix":
      return mixRead.current;
    case "tokens":
      return ok({ tokens: [] });
    case "invitations":
      return ok({ invitations: [] });
  }
  return { ok: false, status: 404, error: "http_404" };
}

async function draw(read: unknown, role = "admin") {
  viewer.role = role;
  mixRead.current = read;
  const tree = await SettingsPage({ searchParams: Promise.resolve({}) });
  const elements = [...walk(tree)];
  return {
    elements,
    source: elements.filter((el) => el.type === SourceMixCard),
    legacy: elements.filter((el) => el.type === CategoryWeightsCard),
    integrations: elements.filter((el) => el.type === IntegrationsTab),
  };
}

beforeEach(() => {
  workspaceFetch.mockReset();
  workspaceFetch.mockImplementation(async (path: string) => answer(path));
});

describe("a converted workspace (protocol 2)", () => {
  it("draws the source card from the parsed read, and not the protocol-1 card", async () => {
    const seen = await draw(ok(CONVERTED));
    expect(seen.source).toHaveLength(1);
    expect(seen.legacy).toHaveLength(0);
    const props = seen.source[0].props as {
      workspaceId: string;
      data: ReturnType<typeof parseSourceMix>;
      editable: boolean;
    };
    expect(props.workspaceId).toBe("ws-1");
    expect(props.data).toEqual(parseSourceMix(CONVERTED));
    expect(props.data?.revision).toBe(8);
    expect(props.data?.rows.map((r) => r.sourceId)).toEqual([SOURCE]);
  });

  it("does not key the card, so a refresh after a conflict cannot remount it and drop the draft", async () => {
    const seen = await draw(ok(CONVERTED));
    expect(seen.source).toHaveLength(1);
    expect(seen.source[0].key).toBeNull();
    // The protocol-1 card IS keyed on its rows: a new read remounts it, which
    // is the rule that card documents. Checked below, so this is a difference
    // the page makes on purpose and not one both cards share.
  });

  it("still draws the source card, with nothing to show, when the read cannot be parsed", async () => {
    for (const broken of [
      { ...CONVERTED, revision: "8" },
      { ...CONVERTED, mode: "automatic" },
      { ...CONVERTED, rows: "none" },
      { ...CONVERTED, rows: [{ source_id: SOURCE, provider: "gdrive", enabled: true, ratio: 1, target_share: 1 }] },
    ]) {
      const seen = await draw(ok(broken));
      expect(seen.legacy, JSON.stringify(broken)).toHaveLength(0);
      expect(seen.source).toHaveLength(1);
      expect((seen.source[0].props as { data: unknown }).data).toBeNull();
    }
  });

  it("is editable for an admin and the owner, and read-only for a member", async () => {
    for (const [role, editable] of [
      ["owner", true],
      ["admin", true],
      ["member", false],
    ] as const) {
      const seen = await draw(ok(CONVERTED), role);
      expect((seen.source[0].props as { editable: boolean }).editable, role).toBe(editable);
    }
  });
});

describe("a workspace that has not converted (protocol 1)", () => {
  it.each([
    ["names protocol 1", { protocol_version: 1, rows: LEGACY_ROWS }],
    ["predates the field", { rows: LEGACY_ROWS }],
  ])("keeps the protocol-1 card, its read untouched, for a read that %s", async (_label, data) => {
    const seen = await draw(ok(data));
    expect(seen.source).toHaveLength(0);
    expect(seen.legacy).toHaveLength(1);
    const card = seen.legacy[0];
    const props = card.props as { workspaceId: string; data: unknown; editable: boolean };
    expect(props.workspaceId).toBe("ws-1");
    // The read itself, untouched: the card parses it.
    expect(props.data).toBe(data);
    expect(props.editable).toBe(true);
    // Keyed on the rows, so a new read remounts it and drops an edit in progress.
    expect(card.key).toBe(JSON.stringify(LEGACY_ROWS));
  });

  it("is editable for an admin and read-only for a member", async () => {
    for (const [role, editable] of [
      ["owner", true],
      ["admin", true],
      ["member", false],
    ] as const) {
      const seen = await draw(ok({ protocol_version: 1, rows: LEGACY_ROWS }), role);
      expect((seen.legacy[0].props as { editable: boolean }).editable, role).toBe(editable);
    }
  });

  it("says the mix could not be loaded when the read failed or its rows are not a list", async () => {
    for (const [read, key] of [
      [{ ok: false, status: 503, error: "target_router_unreachable" }, "unavailable"],
      [ok({ protocol_version: 1, rows: "none" }), JSON.stringify("none")],
      [ok({}), "null"],
    ] as const) {
      const seen = await draw(read);
      expect(seen.source).toHaveLength(0);
      expect(seen.legacy).toHaveLength(1);
      expect((seen.legacy[0].props as { data: unknown }).data).toBeNull();
      expect(seen.legacy[0].key).toBe(key);
    }
  });
});

describe("a protocol this page does not know", () => {
  it.each([
    ["a later version", 3],
    ["version 0", 0],
    ["a string that looks like 2", "2"],
    ["null", null],
    ["a boolean", true],
  ])("draws neither editor for %s: the protocol-1 card with nothing to write", async (_label, protocol_version) => {
    const seen = await draw(ok({ protocol_version, revision: 8, mode: "equal", rows: LEGACY_ROWS }));
    expect(seen.source).toHaveLength(0);
    expect(seen.legacy).toHaveLength(1);
    // No rows are handed over, so there is nothing for it to save.
    expect((seen.legacy[0].props as { data: unknown }).data).toBeNull();
  });
});

describe("the Drive card writes against the mix this page read", () => {
  // Picking or removing a folder moves a converted workspace's mix, and the API
  // refuses the write unless it names the revision it was made against: the
  // Integrations tab is handed the very mix the Posting mix card draws.
  const mixOf = (seen: Awaited<ReturnType<typeof draw>>) =>
    (seen.integrations[0].props as { mix: ReturnType<typeof parseSourceMix> }).mix;

  it("hands the Integrations tab the converted mix, its revision included", async () => {
    const seen = await draw(ok(CONVERTED));
    expect(seen.integrations).toHaveLength(1);
    expect(mixOf(seen)).toEqual(parseSourceMix(CONVERTED));
    expect(mixOf(seen)?.revision).toBe(8);
    // The same read the card was given, not a second one.
    expect(mixOf(seen)).toEqual((seen.source[0].props as { data: unknown }).data);
  });

  it("hands it nothing on protocol 1, which has no revision to write against", async () => {
    for (const read of [
      ok({ protocol_version: 1, rows: LEGACY_ROWS }),
      ok({ rows: LEGACY_ROWS }),
      { ok: false, status: 503, error: "target_router_unreachable" },
    ]) {
      expect(mixOf(await draw(read))).toBeNull();
    }
  });

  it("hands it nothing for a protocol-2 read it cannot parse, as it hands the card", async () => {
    const seen = await draw(ok({ ...CONVERTED, revision: "8" }));
    expect(mixOf(seen)).toBeNull();
    expect((seen.source[0].props as { data: unknown }).data).toBeNull();
  });
});

describe("a link to the Posting mix reaches the card", () => {
  // The dashboard scrolls inside <main>, so the browser's own fragment scroll
  // does not reach the card on a document load: the page carries the anchor the
  // links name and scrolls to it once it has mounted (`fragment-scroll.ts`).
  it("carries the id its links name around the source card, and mounts the scroller", async () => {
    const seen = await draw(ok(CONVERTED));
    const anchor = seen.elements.find((el) => (el.props as { id?: string }).id === POSTING_MIX_ID);
    expect(anchor, "an element with the Posting mix's id").toBeDefined();
    expect([...walk((anchor!.props as { children: ReactNode }).children)].map((el) => el.type)).toContain(
      SourceMixCard,
    );
    // The id the links end in is the one the card carries.
    expect(RESOLVED_IN.mix.href.split("#")[1]).toBe(POSTING_MIX_ID);
    expect(seen.elements.filter((el) => el.type === ScrollToHash)).toHaveLength(1);
  });

  it("mounts the scroller whichever card is drawn, since a document load can name the fragment on either", async () => {
    const seen = await draw(ok({ protocol_version: 1, rows: LEGACY_ROWS }));
    expect(seen.elements.filter((el) => el.type === ScrollToHash)).toHaveLength(1);
  });
});
