/**
 * The library's Auto-approve (#1477): the toggle sends the flag it moves to,
 * the 🔁 badge follows it, and the tooltip says when it acts.
 *
 * Without a DOM, as `queue-list.test.tsx` does it: `MediaCard` is called as a
 * function and its returned tree read, with Next's `useRouter` and React's
 * state hooks stubbed. `useOptimistic` answers the server's value unless a test
 * says what a toggle in flight shows; `useTransition` runs the action and
 * records whether the re-read ran inside it. The wire is asserted as literals
 * and the body run through the route's own spec.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement } from "react";

const hoisted = vi.hoisted(() => ({
  refresh: vi.fn(),
  /** One setter per `useState` call, in call order (the card's one: the refusal under the actions). */
  setters: [] as ReturnType<typeof vi.fn>[],
  /** Values the next `useState` calls return instead of their initial state. */
  presets: [] as unknown[],
  /** What the next `useOptimistic` shows instead of the server's value: a toggle in flight. */
  optimistic: [] as unknown[],
  /** One setter per `useOptimistic` call, in call order. */
  showers: [] as ReturnType<typeof vi.fn>[],
  /** Whether the next `useTransition` is pending: a toggle's command, or its re-read, in flight. */
  pending: [] as boolean[],
  /** Each transition started: settles once its action has, saying whether the re-read ran inside it. */
  transitions: [] as Promise<{ refreshedInside: boolean }>[],
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: hoisted.refresh }),
}));

vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return {
    ...actual,
    useState: (initial: unknown) => {
      const setter = vi.fn();
      hoisted.setters.push(setter);
      const value =
        hoisted.presets.length > 0
          ? hoisted.presets.shift()
          : typeof initial === "function"
            ? (initial as () => unknown)()
            : initial;
      return [value, setter];
    },
    useOptimistic: (value: unknown) => {
      const show = vi.fn();
      hoisted.showers.push(show);
      return [hoisted.optimistic.length > 0 ? hoisted.optimistic.shift() : value, show];
    },
    useTransition: () => [
      hoisted.pending.length > 0 ? hoisted.pending.shift() : false,
      (action: () => unknown) => {
        const before = hoisted.refresh.mock.calls.length;
        hoisted.transitions.push(
          Promise.resolve(action()).then(() => ({
            refreshedInside: hoisted.refresh.mock.calls.length > before,
          })),
        );
      },
    ],
    // The grid's own hooks, so it can be read as a tree too.
    useMemo: (factory: () => unknown) => factory(),
    useRef: (initial: unknown) => ({ current: initial }),
  };
});

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { parseCommand } from "@/lib/commands";
import type { MediaRow } from "@/lib/dashboard-payloads";
import { MediaCard } from "./media-card";
import { MediaGrid, MediaGridView } from "./media-grid";

const WS = "11111111-1111-4111-8111-111111111111";
const ITEM_ID = "22222222-2222-4222-8222-222222222222";

function item(overrides: Partial<MediaRow> = {}): MediaRow {
  return {
    id: ITEM_ID,
    source_id: null,
    provider_file_ref: null,
    file_name: "sample.jpg",
    media_kind: "image",
    mime_type: "image/jpeg",
    file_size: 2048,
    category: "Events",
    title: null,
    caption: null,
    tags: null,
    has_thumbnail: false,
    thumbnail_version: "0123456789abcdef",
    link_url: null,
    auto_approve: false,
    state: "available",
    times_posted: 0,
    last_posted_at: null,
    created_at: "2026-10-01T13:00:00+00:00",
    ...overrides,
  };
}

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

/** The text a tree renders: its strings, in order. */
function text(node: unknown): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(text).join("");
  if (isValidElement(node)) return text((node.props as { children?: unknown }).children);
  return "";
}

type ToggleProps = {
  "aria-pressed": boolean;
  "aria-label": string;
  "aria-disabled": boolean;
  title: string;
  variant: string;
  disabled?: boolean;
  onClick: () => void;
};

const card = (row: MediaRow = item(), apiPublishing: boolean | null = true) =>
  MediaCard({
    item: row,
    workspaceId: WS,
    tz: "UTC",
    apiPublishing,
    onSchedule: () => {},
  }) as ReactElement;

/** The Auto-approve toggle: the one button that says whether it is pressed. */
function toggle(tree: ReactElement): ReactElement<ToggleProps> {
  const found = [...walk(tree)].filter(
    (el) => el.type === Button && (el.props as Partial<ToggleProps>)["aria-pressed"] !== undefined,
  );
  expect(found, "exactly one Auto-approve toggle").toHaveLength(1);
  return found[0] as ReactElement<ToggleProps>;
}

/** The 🔁 badge, if the card shows one. */
const badge = (tree: ReactElement) =>
  [...walk(tree)].find((el) => el.type === Badge && text(el).includes("🔁"));

type Captured = { url: string; init: RequestInit };
let captured: Captured[];
let fetchMock: ReturnType<typeof vi.fn>;

/** Reply with `body` at `status`, recording every call. */
function stubFetch(body: unknown = {}, status = 200) {
  captured = [];
  fetchMock = vi.fn(async (url: string, init: RequestInit) => {
    captured.push({ url, init });
    return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
}

/** The refusal under the actions: the card's one `useState`. */
const setNotice = () => hoisted.setters[0];

beforeEach(() => {
  vi.unstubAllGlobals();
  stubFetch({ outcome: "executed" });
  hoisted.refresh.mockClear();
  hoisted.setters.length = 0;
  hoisted.presets.length = 0;
  hoisted.optimistic.length = 0;
  hoisted.showers.length = 0;
  hoisted.pending.length = 0;
  hoisted.transitions.length = 0;
});

describe("Auto-approve: the toggle sends the flag", () => {
  it("is a third action after Schedule…, named for its item, and not pressed while the flag is off", () => {
    const tree = card();
    const labels = [...walk(tree)]
      .filter((el) => el.type === Button)
      .map((el) => text(el));
    expect(labels).toEqual(["Schedule…", "Auto-approve"]);
    const button = toggle(tree);
    expect(button.props["aria-pressed"]).toBe(false);
    expect(button.props["aria-label"]).toBe("Auto-approve sample.jpg");
    expect(button.props.variant).toBe("outline");
  });

  it("sends set_item_auto_approve with the value it moves to, shows it at once, then re-reads inside the same transition", async () => {
    toggle(card()).props.onClick();

    const [{ refreshedInside }] = await Promise.all(hoisted.transitions);
    // Shown at once, before the command leaves, and held there until the
    // re-read lands with the server's flag.
    expect(hoisted.showers[0].mock.calls).toEqual([[true]]);
    expect(hoisted.showers[0].mock.invocationCallOrder[0]).toBeLessThan(
      fetchMock.mock.invocationCallOrder[0],
    );
    expect(refreshedInside).toBe(true);
    expect(hoisted.refresh).toHaveBeenCalledTimes(1);
    expect(captured.map((c) => c.url)).toEqual([`/api/workspaces/${WS}/commands/set_item_auto_approve`]);
    const sent = JSON.parse(String(captured[0].init.body));
    expect(sent).toEqual({
      media_item_id: ITEM_ID,
      auto_approve: true,
      submission_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
    });
    expect(parseCommand("set_item_auto_approve", sent).ok).toBe(true);
  });

  it("is pressed while the flag is on, for a screen reader", () => {
    expect(toggle(card(item({ auto_approve: true }))).props["aria-pressed"]).toBe(true);
  });

  it("reads off, and stays a toggle, for an API that omits the flag", () => {
    const older = item({ auto_approve: undefined });
    expect(toggle(card(older)).props["aria-pressed"]).toBe(false);
  });

  it("turns a flag that is on off", async () => {
    toggle(card(item({ auto_approve: true }))).props.onClick();

    await Promise.all(hoisted.transitions);
    expect(JSON.parse(String(captured[0].init.body)).auto_approve).toBe(false);
  });

  it("says why under the actions when refused, with no undo, and re-reads after a refusal about the item", async () => {
    stubFetch({ error: "not_found" }, 404);
    toggle(card()).props.onClick();

    await Promise.all(hoisted.transitions);
    // No undo: the shown flag falls back to the server's once the transition ends.
    expect(hoisted.showers[0].mock.calls).toEqual([[true]]);
    expect(setNotice()).toHaveBeenLastCalledWith("That item is no longer in the library. Reload the page.");
    expect(hoisted.refresh).toHaveBeenCalledTimes(1);
  });

  it("re-reads after the refusal of a removed item, so its card leaves the library", async () => {
    // The port refuses a removed item `locked`, with the item's own state in
    // the way, and the route relays it at 409. The library lists available
    // items only, so this re-read is what takes the card away.
    stubFetch({ error: "locked", facts: { in_the_way: ["item_removed"], overridable: false } }, 409);
    toggle(card()).props.onClick();

    await Promise.all(hoisted.transitions);
    expect(setNotice()).toHaveBeenLastCalledWith("That item is no longer in the library. Reload the page.");
    expect(hoisted.refresh).toHaveBeenCalledTimes(1);
  });

  it("does not re-read after a refusal about the request", async () => {
    stubFetch({ error: "unauthenticated" }, 401);
    toggle(card()).props.onClick();

    await Promise.all(hoisted.transitions);
    // Cleared as the tap is taken, then the refusal said.
    expect(setNotice().mock.calls).toEqual([[null], [expect.stringMatching(/not signed in/i)]]);
    expect(hoisted.refresh).not.toHaveBeenCalled();
  });

  it("holds the toggle while its answer or its re-read is awaited, without disabling it, and shows a refusal as an alert", () => {
    hoisted.pending.push(true);
    hoisted.presets.push("Refused.");
    const tree = card();
    const button = toggle(tree);
    // `aria-disabled`, not `disabled`: a button that becomes disabled loses focus.
    expect(button.props["aria-disabled"]).toBe(true);
    expect(button.props.disabled).toBeUndefined();
    const alert = [...walk(tree)].find(
      (el) => el.type === "p" && (el.props as { role?: string }).role === "alert",
    );
    expect(text(alert)).toBe("Refused.");
  });

  it("sends nothing for a tap while the last answer or its re-read is awaited", async () => {
    hoisted.pending.push(true);
    toggle(card()).props.onClick();
    await Promise.resolve();
    expect(hoisted.transitions).toEqual([]);
    expect(captured).toEqual([]);
  });
});

describe("Auto-approve: the badge follows the flag", () => {
  it("shows 🔁 beside the posting badge while the flag is on, and names it for a screen reader", () => {
    const tree = card(item({ auto_approve: true, times_posted: 1 }));
    const shown = badge(tree);
    expect(shown, "the 🔁 badge").toBeDefined();
    expect(text(shown)).toBe("🔁Auto-approve is on");
    expect((shown!.props as { title?: string }).title).toBe("Auto-approve is on");
    expect(toggle(tree).props["aria-pressed"]).toBe(true);
    expect(toggle(tree).props.variant).toBe("default");

    // Beside the posting badge: the two share one group, the innermost element
    // holding the 🔁 badge (the walk is pre-order, so it is the last match).
    const group = [...walk(tree)]
      .filter(
        (el) => el.type === "div" && [...walk((el.props as { children?: unknown }).children)].includes(shown!),
      )
      .at(-1);
    expect(
      [...walk((group!.props as { children?: unknown }).children)]
        .filter((el) => el.type === Badge)
        .map(text),
    ).toEqual(["🔁Auto-approve is on", "Posted once"]);
  });

  it("shows none while the flag is off", () => {
    expect(badge(card())).toBeUndefined();
  });

  it("follows the toggle at once, before the re-read lands", () => {
    // What a toggle in flight shows, on an item the server still has off.
    hoisted.optimistic.push(true);
    const tree = card(item({ auto_approve: false }));
    expect(badge(tree)).toBeDefined();
    expect(toggle(tree).props["aria-pressed"]).toBe(true);
  });

  it("keeps each card mounted across a re-read, so the toggle just pressed keeps focus", () => {
    // Keyed on the item alone: a key that carried the flag would remount the
    // card when the flag moved, and take the focus with it.
    const rows = [item(), item({ id: "33333333-3333-4333-8333-333333333333", auto_approve: true })];
    const stateful = MediaGrid({
      items: rows,
      limit: 100,
      workspaceId: WS,
      targets: null,
      apiPublishing: true,
    }) as ReactElement<Parameters<typeof MediaGridView>[0]>;
    // The grid holds the state and hands the cards' drawing to `MediaGridView`,
    // which holds none: its output is the tree the cards are keyed in.
    expect(stateful.type).toBe(MediaGridView);
    const grid = MediaGridView(stateful.props) as ReactElement;
    const keys = [...walk(grid)].filter((el) => el.type === MediaCard).map((el) => el.key);
    expect(keys).toEqual([ITEM_ID, "33333333-3333-4333-8333-333333333333"]);
  });
});

describe("Auto-approve: the tooltip says when it acts", () => {
  const OFF = "It takes effect once Instagram API posting is on.";
  const LINKED = "This item has a link to add, and items with a link to add still ask.";
  const title = (row: MediaRow, apiPublishing: boolean | null) =>
    toggle(card(row, apiPublishing)).props.title;

  it("says it takes effect once API posting is on, while posting is off", () => {
    expect(title(item(), false)).toContain(OFF);
    expect(title(item(), true)).not.toContain(OFF);
    // Unread is not off: nothing is said about posting.
    expect(title(item(), null)).not.toContain(OFF);
  });

  it("says items with a link to add still ask, on a linked item", () => {
    const linked = item({ link_url: "https://example.com/menu" });
    expect(title(linked, true)).toContain(LINKED);
    expect(title(item(), true)).not.toContain(LINKED);
  });

  it("says both when both hold, and always what the flag does", () => {
    const both = title(item({ link_url: "https://example.com/menu" }), false);
    expect(both).toContain(OFF);
    expect(both).toContain(LINKED);
    expect(title(item(), true)).toBe(
      "Each story of this item is approved automatically, even in a folder set to Ask every time.",
    );
  });

  it("shows the toggle whatever the posting setting, as Configure does", () => {
    for (const apiPublishing of [true, false, null]) {
      expect(toggle(card(item(), apiPublishing)).props["aria-disabled"], String(apiPublishing)).toBe(false);
    }
  });
});
