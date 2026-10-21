/**
 * What the mix card says for each state `deriveFolderMix` can hand it. The
 * card is called directly and its returned tree read without rendering
 * (`environment: "node"`), so the text is the strings in the tree, in order:
 * the header, then each row's folder, target and posted share.
 */

import { describe, expect, it } from "vitest";
import { createElement, type ReactElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { PostingMixCard, SourcePostingMixCard } from "./posting-mix-card";
import { EmptyState } from "./empty-state";
import {
  deriveSourceShares,
  type FolderMix,
  type FolderMixView,
  type StatsResponse,
} from "@/lib/dashboard-payloads";
import { parseSourceMix, type SourceMix } from "@/lib/source-mix";

function text(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(text).join("");
  return text((node as ReactElement<{ children?: ReactNode }>).props?.children);
}

function* walk(node: ReactNode): Generator<ReactElement> {
  if (node === null || node === undefined || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  const el = node as ReactElement<{ children?: ReactNode }>;
  yield el;
  yield* walk(el.props?.children);
}

const row = (over: Partial<FolderMixView> & { name: string }): FolderMixView => ({
  sourceId: `s-${over.name}`,
  mode: "explicit",
  planned: 0,
  posted: null,
  ...over,
});

const card = (mix: FolderMix) => PostingMixCard({ mix });

describe("the posting mix card", () => {
  it("gives each folder one row: its target, then its posted share", () => {
    const shown = text(
      card({
        folders: [
          row({ name: "events", planned: 70, posted: 60 }),
          row({ name: "tips", mode: "automatic", planned: 30, posted: 20 }),
          row({ name: "old", mode: "off", planned: 0, posted: 0 }),
        ],
        total: 10,
        fromRemoved: 2,
      }),
    );
    expect(shown).toContain("FolderTargetPosted");
    expect(shown).toContain("events70%60%");
    expect(shown).toContain("tips30% auto20%");
    expect(shown).toContain("oldOff0%");
    expect(shown).toContain(
      "Target: today's mix. Posted: each folder's share of the 10 posts the mix picked in this window, 2 of them from folders no longer connected. Stories you planned yourself are left out.",
    );
  });

  it("prints no share for a window with no post, and says why", () => {
    const shown = text(
      card({
        folders: [row({ name: "events", planned: 100, posted: null })],
        total: 0,
        fromRemoved: 0,
      }),
    );
    expect(shown).toContain("events100%—");
    expect(shown).toContain("Target: today's mix. Nothing posted in this window yet.");
  });

  it("says the counts are missing when the API sent none, not that nothing posted", () => {
    const shown = text(
      card({
        folders: [row({ name: "events", planned: 100, posted: null })],
        total: null,
        fromRemoved: 0,
      }),
    );
    expect(shown).toContain("events100%—");
    expect(shown).toContain("Posted shares are not available yet.");
    expect(shown).not.toContain("Nothing posted");
  });

  it("names no removed folders when every post is on a row", () => {
    const shown = text(
      card({
        folders: [row({ name: "events", planned: 100, posted: 100 })],
        total: 1,
        fromRemoved: 0,
      }),
    );
    expect(shown).toContain(
      "Posted: each folder's share of the 1 post the mix picked in this window. Stories",
    );
  });

  it("adds the Posted column to 100, where rounding each share alone read 101", () => {
    // The sample workspace's own month: 77, 45 and 30 of 152 posts.
    const share = (posts: number) => (posts / 152) * 100;
    const shown = text(
      card({
        folders: [
          row({ name: "Tips", planned: 50, posted: share(77) }),
          row({ name: "Behind the scenes", planned: 30, posted: share(45) }),
          row({ name: "Events", planned: 20, posted: share(30) }),
        ],
        total: 152,
        fromRemoved: 0,
      }),
    );
    expect(shown).toContain("Tips50%51%");
    expect(shown).toContain("Behind the scenes30%29%");
    expect(shown).toContain("Events20%20%");
  });

  it("adds the Target column to 100 too, when the mix splits three ways", () => {
    const third = 100 / 3;
    const shown = text(
      card({
        folders: [
          row({ name: "a", mode: "automatic", planned: third }),
          row({ name: "b", mode: "automatic", planned: third }),
          row({ name: "c", mode: "automatic", planned: third }),
        ],
        total: 0,
        fromRemoved: 0,
      }),
    );
    expect(shown).toContain("a34% auto—");
    expect(shown).toContain("b33% auto—");
    expect(shown).toContain("c33% auto—");
  });

  it("is the empty state when no folder is connected", () => {
    const tree = card({ folders: [], total: 0, fromRemoved: 0 });
    expect([...walk(tree)].some((el) => el.type === EmptyState)).toBe(true);
  });
});

/**
 * The protocol-2 card (#1644): sources with their providers and
 * workspace targets, derived by `deriveSourceShares` from a converted read and
 * drawn to markup. Every handle is synthetic.
 */
describe("the posting mix card of a converted workspace", () => {
  const IG = "22222222-2222-4222-8222-222222222222";
  const DRIVE = "33333333-3333-4333-8333-333333333333";
  const EVENTS = "44444444-4444-4444-8444-444444444444";
  const THIRD = "0.33333333333333333333333333333333333333333333333333";

  const sourceRow = (source_id: string, provider: string, name: string, share: string, over = {}) => ({
    source_id,
    provider,
    name,
    state: "active",
    origin_handle: provider === "instagram_past_posts" ? name : null,
    enabled: share !== "0",
    ratio: share,
    target_share: share,
    ...over,
  });

  function converted(rows: unknown[], destinations: unknown[] = [{ account_id: "a1", sources: [] }]): SourceMix {
    const mix = parseSourceMix({ protocol_version: 2, revision: 8, mode: "equal", rows, destinations });
    if (!mix) throw new Error("fixture did not parse");
    return mix;
  }

  const stats = (posted?: Record<string, number>) =>
    ({ intents_by_state: {}, ...(posted ? { posted_by_source: posted } : {}) }) as unknown as StatsResponse;

  const draw = (mix: SourceMix | null, posted?: Record<string, number>) =>
    renderToStaticMarkup(
      createElement(SourcePostingMixCard, { mix: mix ? deriveSourceShares(stats(posted), mix) : null }),
    )
      .replace(/<[^>]+>/g, " ")
      .replace(/&#x27;/g, "'")
      .replace(/\s+/g, " ")
      .trim();

  it("gives each source a row: its title and provider, its target, its posted share", () => {
    const text = draw(
      converted([
        sourceRow(IG, "instagram_past_posts", "example_handle", THIRD),
        sourceRow(DRIVE, "gdrive", "Event photos", THIRD),
        sourceRow(EVENTS, "gdrive", "Events", THIRD),
      ]),
      { [IG]: 2, [DRIVE]: 1, [EVENTS]: 1 },
    );
    expect(text).toContain("Source Target Posted");
    expect(text).toContain("Instagram past posts @example_handle · Instagram 33.4% 50%");
    expect(text).toContain("Event photos Google Drive 33.3% 25%");
    expect(text).toContain("Events Google Drive 33.3% 25%");
    expect(text).toContain("Posted: each source's share of the 4 posts the mix picked in this window.");
    expect(text).not.toMatch(/NaN|folder/);
  });

  it("reads an Off source as Off, and every source Off as a choice", () => {
    const text = draw(
      converted([
        sourceRow(IG, "instagram_past_posts", "example_handle", "0"),
        sourceRow(DRIVE, "gdrive", "Event photos", "0"),
      ]),
    );
    expect(text).toContain("Instagram past posts @example_handle · Instagram Off —");
    expect(text).toContain("Every source is Off, so nothing is drawn until one is turned on in Posting mix.");
    expect(text).toContain("Posted shares are not available yet.");
  });

  it("is the no-source empty state when nothing is connected, naming both kinds of source", () => {
    const text = draw(converted([]));
    expect(text).toContain("No content sources yet");
    expect(text).toMatch(/past posts in Posting mix, or connect a Google Drive folder/);
  });

  it("says a mix it could not read is unreadable, never that nothing is connected", () => {
    const text = draw(null);
    expect(text).toContain("The posting mix could not be read.");
    expect(text).not.toContain("No content sources yet");
  });

  it("with accounts that draw on different sources, says a workspace target is no one account's share", () => {
    const rows = [
      sourceRow(IG, "instagram_past_posts", "example_handle", "0.5"),
      sourceRow(DRIVE, "gdrive", "Event photos", "0.5"),
    ];
    const served = (...ids: string[]) => ids.map((source_id) => ({ source_id, assignable: true }));
    const one = [{ account_id: "a1", sources: served(IG, DRIVE) }];
    expect(draw(converted(rows, one))).not.toContain("Each account's own share can differ");
    // The past-posts source serves its own account and not the other: they draw differently.
    const apart = [
      { account_id: "a1", sources: served(IG, DRIVE) },
      { account_id: "a2", sources: [{ source_id: IG, assignable: false }, ...served(DRIVE)] },
    ];
    expect(draw(converted(rows, apart))).toContain("Each account's own share can differ; Posting mix shows it.");
  });

  it("says no such thing of several accounts that draw on the same sources, which have one mix", () => {
    const rows = [sourceRow(DRIVE, "gdrive", "Event photos", "1")];
    const together = [
      { account_id: "a1", sources: [{ source_id: DRIVE, assignable: true }] },
      { account_id: "a2", sources: [{ source_id: DRIVE, assignable: true }] },
    ];
    expect(draw(converted(rows, together))).not.toContain("Each account's own share can differ");
  });
});
