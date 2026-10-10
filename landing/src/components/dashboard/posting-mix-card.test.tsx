/**
 * What the mix card says for each state `deriveFolderMix` can hand it. The
 * card is called directly and its returned tree read without rendering
 * (`environment: "node"`), so the text is the strings in the tree, in order:
 * the header, then each row's folder, target and posted share.
 */

import { describe, expect, it } from "vitest";
import type { ReactElement, ReactNode } from "react";
import { PostingMixCard } from "./posting-mix-card";
import { EmptyState } from "./empty-state";
import type { FolderMix, FolderMixView } from "@/lib/dashboard-payloads";

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
          row({ name: "memes", planned: 70, posted: 60 }),
          row({ name: "merch", mode: "automatic", planned: 30, posted: 20 }),
          row({ name: "old", mode: "off", planned: 0, posted: 0 }),
        ],
        total: 10,
        fromRemoved: 2,
      }),
    );
    expect(shown).toContain("FolderTargetPosted");
    expect(shown).toContain("memes70%60%");
    expect(shown).toContain("merch30% auto20%");
    expect(shown).toContain("oldOff0%");
    expect(shown).toContain(
      "Target: today's mix. Posted: each folder's share of the 10 posts the mix picked in this window, 2 of them from folders no longer connected. Stories you planned yourself are left out.",
    );
  });

  it("prints no share for a window with no post, and says why", () => {
    const shown = text(
      card({
        folders: [row({ name: "memes", planned: 100, posted: null })],
        total: 0,
        fromRemoved: 0,
      }),
    );
    expect(shown).toContain("memes100%—");
    expect(shown).toContain("Target: today's mix. Nothing posted in this window yet.");
  });

  it("says the counts are missing when the API sent none, not that nothing posted", () => {
    const shown = text(
      card({
        folders: [row({ name: "memes", planned: 100, posted: null })],
        total: null,
        fromRemoved: 0,
      }),
    );
    expect(shown).toContain("memes100%—");
    expect(shown).toContain("Posted shares are not available yet.");
    expect(shown).not.toContain("Nothing posted");
  });

  it("names no removed folders when every post is on a row", () => {
    const shown = text(
      card({
        folders: [row({ name: "memes", planned: 100, posted: 100 })],
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
          row({ name: "Product shots", planned: 50, posted: share(77) }),
          row({ name: "Behind the scenes", planned: 30, posted: share(45) }),
          row({ name: "Memes", planned: 20, posted: share(30) }),
        ],
        total: 152,
        fromRemoved: 0,
      }),
    );
    expect(shown).toContain("Product shots50%51%");
    expect(shown).toContain("Behind the scenes30%29%");
    expect(shown).toContain("Memes20%20%");
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
