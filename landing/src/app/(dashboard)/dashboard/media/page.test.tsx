/**
 * What the media library page reads, and what it tells an empty library
 * (#1644). The page, an async server component, is called directly with
 * its two doors mocked, the session guard and the target fetch, and the returned
 * element tree is read without rendering it (`environment: "node"`).
 *
 * The posting mix is read for one thing: whether the workspace has its past
 * posts as a source, on or Off, so an empty library does not tell it to connect
 * Google Drive. So it is read for an empty library alone, after the reads the
 * page stands on. A workspace whose mix is protocol 1, a mix that cannot be read
 * and one with no past posts all tell the grid nothing, so the library says to
 * connect Google Drive.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement, ReactNode } from "react";

const { workspaceFetch } = vi.hoisted(() => ({ workspaceFetch: vi.fn() }));

vi.mock("@/lib/page-guards", () => ({
  requireWorkspacePage: async () => ({ workspaceId: "ws-1" }),
}));
vi.mock("@/lib/workspaces", () => ({ workspaceFetch }));

import MediaLibraryPage from "./page";
import { MediaGrid } from "@/components/dashboard/media/media-grid";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";

/** Depth-first walk of a returned tree, children flattened. */
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

const ok = (data: unknown) => ({ ok: true, data });
const DOWN = { ok: false, status: 503, error: "http_503" };

type Read = "media" | "stats" | "accounts" | "config" | "mix";

/** The four reads the page stands on, which it makes together. */
const MAIN = ["", "accounts", "media?state=available&limit=100", "stats"];

/** Every read, as `start <path>` when it is asked and `end <path>` when answered. */
const log: string[] = [];

function readOf(path: string): Read {
  if (path.startsWith("media")) return "media";
  if (path === "stats") return "stats";
  if (path === "accounts") return "accounts";
  if (path === "category-mix") return "mix";
  if (path === "") return "config";
  throw new Error(`unexpected read: ${path}`);
}

/** A mix as a converted workspace's read answers it, its sources as given. */
function converted(rows: Array<{ provider: string; enabled: boolean }>) {
  return {
    protocol_version: 2,
    revision: 8,
    mode: "equal",
    supported_actions: ["reset_equal", "set_enabled", "save_custom"],
    conversion: { state: "complete", kind: "new_equal" },
    past_posts_available: true,
    destinations: [],
    rows: rows.map((row, i) => ({
      source_id: `src-${i}`,
      provider: row.provider,
      name: row.provider === "gdrive" ? "Event photos" : "example_handle",
      state: "active",
      enabled: row.enabled,
      ratio: row.enabled ? "0.5" : "0",
      target_share: row.enabled ? "0.5" : "0",
    })),
  };
}

/** The read of a workspace whose mix has not converted: folders, with no protocol named. */
const LEGACY = {
  rows: [{ source_id: "src-0", name: "Event photos", mode: "automatic", effective: 100 }],
  weights: {},
};

function answer(over: Partial<Record<Read, unknown>> = {}) {
  const table: Record<Read, unknown> = {
    media: ok({ media: [], limit: 100 }),
    stats: ok({ intents_by_state: {}, media_by_state: {}, media_by_category: {} }),
    accounts: ok({ accounts: [] }),
    config: ok({ tz: "UTC" }),
    mix: ok(LEGACY),
    ...over,
  };
  workspaceFetch.mockImplementation(async (path: string) => {
    log.push(`start ${path}`);
    await Promise.resolve();
    log.push(`end ${path}`);
    return table[readOf(path)];
  });
}

const paths = () => workspaceFetch.mock.calls.map(([path]) => path as string).sort();

async function page() {
  return [...walk(await MediaLibraryPage())];
}

const gridProps = (elements: ReactElement[]) => {
  const grid = elements.find((el) => el.type === MediaGrid);
  expect(grid, "the page does not render the grid").toBeDefined();
  return grid!.props as Parameters<typeof MediaGrid>[0];
};

beforeEach(() => {
  workspaceFetch.mockReset();
  log.length = 0;
});

describe("the media library page's one read of the posting mix", () => {
  it("asks for it once, of an empty library, after the four reads the page stands on", async () => {
    answer();
    await page();
    expect(paths()).toEqual([...MAIN, "category-mix"].sort());
    const mix = log.indexOf("start category-mix");
    for (const path of MAIN) {
      expect(log.indexOf(`end ${path}`), `${path} was not answered before the mix was asked`).toBeLessThan(mix);
    }
  });

  it("does not ask for it of a library with items: only an empty library's copy uses it", async () => {
    answer({
      media: ok({ media: [{ id: "media-1" }], limit: 100 }),
      mix: ok(converted([{ provider: "instagram_past_posts", enabled: true }])),
    });
    expect(gridProps(await page()).pastPosts).toBeNull();
    expect(paths()).toEqual(MAIN);
    // The positive control: the same mix, asked of an empty library, is told.
    workspaceFetch.mockClear();
    answer({ mix: ok(converted([{ provider: "instagram_past_posts", enabled: true }])) });
    expect(gridProps(await page()).pastPosts).toBe("on");
    expect(paths()).toEqual([...MAIN, "category-mix"].sort());
  });

  it("tells the grid a workspace's past posts are on, or Off, from its converted mix", async () => {
    answer({ mix: ok(converted([{ provider: "instagram_past_posts", enabled: true }])) });
    expect(gridProps(await page()).pastPosts).toBe("on");
    answer({
      mix: ok(
        converted([
          { provider: "gdrive", enabled: true },
          { provider: "instagram_past_posts", enabled: false },
        ]),
      ),
    });
    expect(gridProps(await page()).pastPosts).toBe("off");
  });

  it("tells the grid nothing for protocol 1, named or not, so its empty library keeps its words", async () => {
    answer({ mix: ok(LEGACY) });
    expect(gridProps(await page()).pastPosts).toBeNull();
    answer({ mix: ok({ ...LEGACY, protocol_version: 1 }) });
    expect(gridProps(await page()).pastPosts).toBeNull();
  });

  it("tells it nothing for a converted mix with no past-posts source, or one it cannot read", async () => {
    answer({ mix: ok(converted([{ provider: "gdrive", enabled: true }])) });
    expect(gridProps(await page()).pastPosts).toBeNull();
    answer({ mix: ok({ ...converted([{ provider: "instagram_past_posts", enabled: true }]), revision: "8" }) });
    expect(gridProps(await page()).pastPosts).toBeNull();
    answer({ mix: ok({ protocol_version: 3, rows: [] }) });
    expect(gridProps(await page()).pastPosts).toBeNull();
  });

  it("draws the library whether or not the mix could be read: that read is not one it stands on", async () => {
    answer({ mix: DOWN });
    const elements = await page();
    expect(gridProps(elements).pastPosts).toBeNull();
    expect(elements.some((el) => el.type === RouterUnavailable)).toBe(false);
  });

  it("still stands on the library and its counts: without either it is unavailable, and asks no mix", async () => {
    answer({ media: DOWN, mix: ok(converted([{ provider: "instagram_past_posts", enabled: true }])) });
    expect((await page()).some((el) => el.type === RouterUnavailable)).toBe(true);
    expect(paths()).toEqual(MAIN);
    workspaceFetch.mockClear();
    answer({ stats: DOWN });
    expect((await page()).some((el) => el.type === RouterUnavailable)).toBe(true);
    expect(paths()).toEqual(MAIN);
  });
});
