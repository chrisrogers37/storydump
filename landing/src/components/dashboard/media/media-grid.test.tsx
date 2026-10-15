/**
 * The media library (#1644): an item from a connected account's past
 * posts goes by its own name and says where it came from, its picture is asked
 * for under the archive's limits, and a library that holds any is listed 24 to a
 * page. A Drive library is listed whole
 * (`environment: "node"`: markup, not a browser). Every fixture is synthetic:
 * `example_handle` is no one's account and `FAKE_ID` is no post's.
 */

import { createElement, isValidElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

const hoisted = vi.hoisted(() => ({
  /** Whether the card's hooks are stubbed: it is called as a function, outside a render. */
  direct: false,
}));

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh() {} }) }));

// The card holds hooks (#1477). Rendered to markup they are React's own; called
// as a function, as the Schedule… test does to read its tree, they are stubbed
// as `media-card.test.tsx` stubs them.
vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return {
    ...actual,
    useState: (initial: unknown) =>
      hoisted.direct ? [initial, () => {}] : actual.useState(initial),
    useOptimistic: (value: unknown) =>
      hoisted.direct ? [value, () => {}] : actual.useOptimistic(value),
    useTransition: () => (hoisted.direct ? [false, () => {}] : actual.useTransition()),
  };
});

import { Button } from "@/components/ui/button";
import type { MediaRow } from "@/lib/dashboard-payloads";
import { ARCHIVE_PROVIDER, withArchiveName } from "@/lib/archive-media";
import { MediaCard } from "./media-card";
import { GridPager, MediaGrid, MediaGridView } from "./media-grid";

const FAKE_ID = "17900000000000001";
const WS = "11111111-1111-4111-8111-111111111111";
const GENERATION = "5b0e1c2e-8c67-4d29-9a3f-0c1d2e3f4a5b";

function drive(n: number, over: Partial<MediaRow> = {}): MediaRow {
  return {
    id: `drive-${n}`,
    source_id: "src-drive",
    provider_file_ref: `file-${n}`,
    file_name: `drive-${n}.jpg`,
    media_kind: "image",
    mime_type: "image/jpeg",
    file_size: 2048,
    category: null,
    title: null,
    caption: null,
    tags: null,
    has_thumbnail: true,
    thumbnail_version: "0123456789abcdef",
    provenance: null,
    link_url: null,
    state: "available",
    times_posted: 0,
    last_posted_at: null,
    created_at: "2026-10-01T12:00:00Z",
    ...over,
  };
}

/**
 * An item as a row that still carries Instagram's id would arrive: named
 * `Instagram <id>`, with the id as its file reference. The API stores
 * `Instagram post` and projects that name and a null `provider_file_ref` on every
 * read, so this is the guard's input (`withArchiveName`), not what a read sends.
 */
function archive(n: number, over: Partial<MediaRow> = {}): MediaRow {
  return {
    ...drive(n),
    id: `archive-${n}`,
    source_id: "src-archive",
    provider_file_ref: FAKE_ID,
    file_name: `Instagram ${FAKE_ID}`,
    thumbnail_version: `${GENERATION}.7`,
    provenance: {
      provider: ARCHIVE_PROVIDER,
      origin_handle: "example_handle",
      original_at: "2020-01-01T12:00:00+00:00",
    },
    ...over,
  };
}

/** Archive items at even places and Drive items at odd ones, `count` in all. */
function library(count: number): MediaRow[] {
  return Array.from({ length: count }, (_, i) => (i % 2 === 0 ? archive(i) : drive(i)));
}

const TARGETS = { accounts: [], workspaceTz: "UTC" };

const noop = () => {};

type ViewProps = Parameters<typeof MediaGridView>[0];

const view = (items: MediaRow[], over: Partial<ViewProps> = {}) =>
  renderToStaticMarkup(
    createElement(MediaGridView, {
      items: items.map(withArchiveName),
      limit: 100,
      workspaceId: WS,
      tz: "UTC",
      pastPosts: null,
      apiPublishing: null,
      category: null,
      page: 0,
      onCategory: noop,
      onPage: noop,
      onSchedule: noop,
      ...over,
    }),
  );

/** The grid through its stateful wrapper, as the page draws it. */
const grid = (items: MediaRow[], limit = 100) =>
  renderToStaticMarkup(
    createElement(MediaGrid, {
      items,
      limit,
      workspaceId: WS,
      targets: TARGETS,
      apiPublishing: null,
    }),
  );

const textOf = (html: string) =>
  html
    .replace(/<[^>]*>/g, " ")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, "&")
    .replace(/&#x27;/g, "'")
    .replace(/\s+/g, " ")
    .trim();

const cards = (html: string) => html.match(/data-slot="card"/g)?.length ?? 0;

/** The names Drive's cards carry, in order; an archive card has its own. */
const driveNames = (html: string) =>
  [...html.matchAll(/title="(drive-\d+\.jpg)"/g)].map((m) => m[1]);

/** Every element in a returned tree, depth-first. */
function* walk(node: unknown): Generator<ReactElement> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (!isValidElement(node)) return;
  yield node;
  yield* walk((node.props as { children?: unknown }).children);
}

describe("a Drive library", () => {
  const items = Array.from({ length: 60 }, (_, i) => drive(i));

  it("is listed whole, with no pages, and its pictures are the browser's to load lazily", () => {
    const html = grid(items);
    expect(cards(html)).toBe(60);
    expect(html).not.toContain('aria-label="Library pages"');
    expect(textOf(html)).toContain("Showing 60 items. That is the whole library.");
    expect(html.match(/<img[^>]*loading="lazy"/g)).toHaveLength(60);
    expect(html).not.toContain("data-preview-phase");
  });

  it("says when the list is a prefix of the library, and filters a category within the loaded items", () => {
    expect(textOf(grid(items.slice(0, 3), 3))).toContain(
      "This is the first 3 in the library — the API serves a bounded list with no page control, so there may be more.",
    );
    const events = items.map((item, i) => (i < 4 ? { ...item, category: "events" } : item));
    const html = view(events, { category: "events" });
    expect(cards(html)).toBe(4);
    expect(textOf(html)).toContain('Showing 4 of 60 loaded items in "events".');
    expect(html).not.toContain('aria-label="Library pages"');
  });
});

describe("an empty library", () => {
  /** The empty state's own text and its button's address. */
  const drawn = (html: string) => ({
    text: textOf(html),
    href: html.match(/<a[^>]*href="([^"]*)"/)?.[1],
  });

  it("tells a workspace with no past posts source to connect Google Drive, with a link to set it up", () => {
    // Protocol 1, a mix that could not be read, and a mix with no past posts all pass null.
    const { text, href } = drawn(view([]));
    expect(text).toContain(
      "No media items found Connect Google Drive and pick the folders to sync your content library. Set up Google Drive",
    );
    expect(href).toBe("/dashboard/settings?tab=integrations");
    expect(view([])).toContain("Set up Google Drive</a>");
  });

  it("tells a workspace whose past posts are on that they arrive as they are indexed, and Drive is optional", () => {
    const { text, href } = drawn(view([], { pastPosts: "on" }));
    expect(text).toContain(
      "No media items found Your past posts are on, so they appear here as they are indexed. Google Drive is optional. Open Posting mix",
    );
    expect(text).not.toContain("Connect Google Drive");
    expect(href).toBe("/dashboard/settings?tab=general#posting-mix");
  });

  it("tells a workspace whose past posts are Off so, and where to turn them on", () => {
    const { text, href } = drawn(view([], { pastPosts: "off" }));
    expect(text).toContain(
      "No media items found Your past posts are Off, so none are indexed. Turn them on in Posting mix. Google Drive is optional. Open Posting mix",
    );
    expect(text).not.toContain("Connect Google Drive");
    expect(href).toBe("/dashboard/settings?tab=general#posting-mix");
  });

  it("says it through the grid the page draws, and nothing when the library is not empty", () => {
    const wrapped = (pastPosts: "on" | "off" | undefined, items: MediaRow[] = []) =>
      textOf(
        renderToStaticMarkup(
          createElement(MediaGrid, {
            items,
            limit: 100,
            workspaceId: WS,
            targets: TARGETS,
            apiPublishing: null,
            pastPosts,
          }),
        ),
      );
    expect(wrapped(undefined)).toContain("Connect Google Drive and pick the folders");
    expect(wrapped("on")).toContain("Your past posts are on");
    expect(wrapped("off")).toContain("Your past posts are Off");
    // The positive control: a library with items shows them, whatever the flag.
    expect(wrapped("on", [drive(0)])).not.toContain("No media items found");
    expect(wrapped("off", [archive(0)])).not.toContain("Your past posts are Off");
  });
});

describe("a library with archive items", () => {
  const items = library(60);

  it("is listed 24 to a page, whichever kind each item is, and says which page it is", () => {
    const html = view(items);
    expect(cards(html)).toBe(24);
    expect(textOf(html)).toContain("Showing 1–24 of 60 loaded items.");
    expect(textOf(html)).toContain("Page 1 of 3");
    // Drive items at odd places: the first page holds those below 24.
    expect(driveNames(html)).toEqual(Array.from({ length: 12 }, (_, i) => `drive-${2 * i + 1}.jpg`));
  });

  it("lists the next page's 24, and the last page's rest", () => {
    const second = view(items, { page: 1 });
    expect(cards(second)).toBe(24);
    expect(textOf(second)).toContain("Showing 25–48 of 60 loaded items.");
    expect(driveNames(second)).toEqual(Array.from({ length: 12 }, (_, i) => `drive-${25 + 2 * i}.jpg`));
    const last = view(items, { page: 2 });
    expect(cards(last)).toBe(12);
    expect(textOf(last)).toContain("Showing 49–60 of 60 loaded items.");
    expect(textOf(last)).toContain("Page 3 of 3");
  });

  it("holds a page it is given to the pages there are", () => {
    expect(textOf(view(items, { page: 99 }))).toContain("Showing 49–60 of 60 loaded items.");
    expect(textOf(view(items, { page: -4 }))).toContain("Showing 1–24 of 60 loaded items.");
  });

  it("draws only a page's archive pictures, each asking for nothing until it is near", () => {
    const html = view(items);
    expect(html.match(/data-preview-phase="idle"/g)).toHaveLength(12);
    // Not one archive picture is on the page yet; the 12 Drive ones are the browser's.
    expect(html.match(/<img/g)).toHaveLength(12);
    expect(html.match(/<img[^>]*loading="lazy"/g)).toHaveLength(12);
  });

  it("is listed whole again when a category leaves no archive item in it, and a page when one does", () => {
    const tagged = items.map((item, i) => ({ ...item, category: i < 10 ? "events" : "shots" }));
    // "events" holds archive items 0, 2, 4, 6, 8: five of ten, one page, but still a paged list.
    const events = view(tagged, { category: "events" });
    expect(cards(events)).toBe(10);
    expect(textOf(events)).toContain('Showing 1–10 of 10 loaded items in "events".');
    expect(textOf(events)).toContain("Page 1 of 1");
    // A category of Drive items only is listed as a Drive library is.
    const driveOnly = tagged.map((item) =>
      item.id.startsWith("drive") ? { ...item, category: "docs" } : item,
    );
    const docs = view(driveOnly, { category: "docs" });
    expect(cards(docs)).toBe(30);
    expect(docs).not.toContain('aria-label="Library pages"');
    expect(textOf(docs)).toContain('Showing 30 of 60 loaded items in "docs".');
  });
});

describe("an archive item's card", () => {
  const html = view([archive(0)]);

  it("goes by its own name and says where it came from, and that its media is checked when used", () => {
    const text = textOf(html);
    expect(text).toContain("Instagram post");
    expect(text).toContain("From @example_handle · Instagram, posted Jan 1, 2020");
    expect(text).toContain("Media is checked when used.");
  });

  it("does not call the item a matching post: the library lists items whatever a source's settings leave out", () => {
    // Too recent, credited and unknown-attribution items are listed too, so a
    // card that said "matching" would claim what the list does not establish.
    expect(textOf(html)).not.toMatch(/match/i);
    expect(textOf(grid([archive(0), archive(1)]))).not.toMatch(/match/i);
  });

  it("never shows the id it is stored under, in its text, its labels or its markup, even through the page's own draw", () => {
    for (const drawn of [html, grid([archive(0)])]) {
      expect(drawn).not.toContain(FAKE_ID);
      expect(drawn).not.toContain("Instagram 179");
      expect(drawn).toContain('aria-label="Schedule Instagram post"');
      expect(drawn).toContain('aria-label="Link Instagram post"');
      expect(drawn).toContain('title="Instagram post"');
    }
  });

  it("says nothing of being verified, ready or posted", () => {
    expect(textOf(html)).not.toMatch(/verified|ready|already posted|published/i);
  });

  it("reads the day it was posted on the workspace's clock", () => {
    const late = archive(0, {
      provenance: {
        provider: ARCHIVE_PROVIDER,
        origin_handle: "example_handle",
        original_at: "2020-01-01T02:30:00+00:00",
      },
    });
    expect(textOf(view([late], { tz: "America/New_York" }))).toContain("posted Dec 31, 2019");
    expect(textOf(view([late], { tz: "UTC" }))).toContain("posted Jan 1, 2020");
    expect(
      textOf(
        renderToStaticMarkup(
          createElement(MediaGrid, {
            items: [late],
            limit: 100,
            workspaceId: WS,
            targets: { accounts: [], workspaceTz: "America/New_York" },
            apiPublishing: null,
          }),
        ),
      ),
    ).toContain("posted Dec 31, 2019");
  });

  it("asks for its picture at the route by the version the API named, and a video's is still its thumbnail", () => {
    const clip = view([archive(0, { media_kind: "video", mime_type: "video/mp4" })]);
    expect(clip).not.toMatch(/<video|<source|\.mp4/i);
    expect(clip).toContain('data-preview-phase="idle"');
  });

  it("waits for its picture without a place for one while it awaits observation again", () => {
    const waiting = view([archive(0, { has_thumbnail: false, thumbnail_version: null })]);
    expect(waiting).not.toContain("data-preview-phase");
    expect(waiting).not.toContain("<img");
    expect(textOf(waiting)).toContain("From @example_handle · Instagram, posted Jan 1, 2020");
  });

  it("leaves out what the read does not have, as the label does", () => {
    const bare = archive(0, {
      provenance: { provider: ARCHIVE_PROVIDER, origin_handle: null, original_at: null },
    });
    expect(textOf(view([bare]))).toContain("From Instagram Media is checked when used.");
  });
});

describe("a Drive item's card beside it", () => {
  const html = view([archive(0), drive(1, { file_name: "Instagram 12.jpg" })]);

  it("keeps its file name, whatever it says, and says nothing of matching, provenance or media being checked", () => {
    const [, driveCard] = html.split('data-slot="card"').slice(1);
    expect(driveCard).toContain('title="Instagram 12.jpg"');
    expect(textOf(driveCard)).not.toMatch(/From|match|checked when used/i);
    expect(driveCard).toContain('aria-label="Schedule Instagram 12.jpg"');
  });
});

describe("MediaCard", () => {
  const props = (item: MediaRow) => ({
    item,
    workspaceId: WS,
    tz: "UTC",
    apiPublishing: null,
    onSchedule: noop,
  });

  it("hands the item and the button that was pressed to Schedule…, so focus can return to it", () => {
    const onSchedule = vi.fn();
    const item = withArchiveName(archive(0));
    hoisted.direct = true;
    let card: ReactElement;
    try {
      card = MediaCard({ ...props(item), onSchedule }) as ReactElement;
    } finally {
      hoisted.direct = false;
    }
    const schedule = [...walk(card)].find(
      (el) => el.type === Button && (el.props as { "aria-label"?: string })["aria-label"] === "Schedule Instagram post",
    )!;
    const button = {} as HTMLButtonElement;
    (schedule.props as { onClick: (e: unknown) => void }).onClick({ currentTarget: button });
    expect(onSchedule).toHaveBeenCalledWith(item, button);
  });
});

describe("GridPager", () => {
  const pager = (page: number, pages: number, onPage = noop) =>
    GridPager({ page, pages, onPage }) as ReactElement;
  const buttons = (tree: ReactElement) =>
    [...walk(tree)].filter((el) => el.type === Button) as ReactElement<{
      disabled?: boolean;
      onClick: () => void;
      children: string;
    }>[];

  it("says where the reader is, and offers Previous and Next to the pages beside it", () => {
    const onPage = vi.fn();
    const [previous, next] = buttons(pager(1, 3, onPage));
    expect([previous.props.children, next.props.children]).toEqual(["Previous", "Next"]);
    expect([previous.props.disabled, next.props.disabled]).toEqual([false, false]);
    previous.props.onClick();
    next.props.onClick();
    expect(onPage.mock.calls).toEqual([[0], [2]]);
    const html = renderToStaticMarkup(pager(1, 3));
    expect(textOf(html)).toBe("Previous Page 2 of 3 Next");
    expect(html).toContain('aria-label="Library pages"');
    expect(html).toContain('aria-live="polite"');
  });

  it("has no Previous on the first page and no Next on the last", () => {
    const [firstPrevious, firstNext] = buttons(pager(0, 3));
    expect([firstPrevious.props.disabled, firstNext.props.disabled]).toEqual([true, false]);
    const [lastPrevious, lastNext] = buttons(pager(2, 3));
    expect([lastPrevious.props.disabled, lastNext.props.disabled]).toEqual([false, true]);
    const [onlyPrevious, onlyNext] = buttons(pager(0, 1));
    expect([onlyPrevious.props.disabled, onlyNext.props.disabled]).toEqual([true, true]);
  });
});

describe("an Instagram post's slides (#1644)", () => {
  const slide = (n: number, postRef: string, source = "src-archive") =>
    archive(n, {
      source_id: source,
      provenance: {
        provider: ARCHIVE_PROVIDER,
        origin_handle: "@example_handle",
        original_at: "2020-01-01T12:00:00+00:00",
        slide: { post_ref: postRef, position: n, count: 3 },
      },
    });

  it("groups slides by source and post while keeping standalone items in read order", () => {
    const html = view([
      slide(1, "post-a"),
      drive(1),
      slide(3, "post-a"),
      slide(2, "post-a", "another-source"),
      archive(9),
    ]);
    expect(html.match(/aria-label="Instagram post slides"/g)).toHaveLength(2);
    expect(html).toContain("Instagram post · @example_handle · Jan 1, 2020 · 3 slides");
    expect(html).toContain("Slide 1 of 3");
    expect(html).toContain("Slide 3 of 3");
    expect(html.indexOf("drive-1.jpg")).toBeLessThan(html.indexOf("Instagram post, slide 2 of 3"));
    expect(html).not.toContain("post-a");
  });

  it("draws each slide as the same card, with the card's own levers", () => {
    const html = view([slide(1, "post-a"), slide(2, "post-a")]);
    expect(html.match(/aria-pressed="/g)).toHaveLength(2);
    expect(html.match(/>Schedule…</g)).toHaveLength(2);
    expect(html.match(/Media is checked when used\./g)).toHaveLength(2);
  });
});
