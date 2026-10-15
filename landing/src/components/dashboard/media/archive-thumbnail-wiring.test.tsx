/**
 * How `ArchiveThumbnail` joins its box, the page's observer and the workspace's
 * lane to what it draws (#1644). The hooks are stubbed, as in
 * `media-thumbnail.test.tsx`, so a test runs the effect the component asked
 * for against a stand-in observer and reads what the tile reported and what its
 * handlers do. What each phase draws is `archive-thumbnail.test.tsx`'s, and what
 * the loader decides is `preview-loader.test.ts`'s.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";

const hoisted = vi.hoisted(() => ({
  phases: [] as string[],
  effects: [] as Array<{ run: () => void | (() => void); deps: unknown[] }>,
  refs: [] as Array<{ current: unknown }>,
}));

vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return {
    ...actual,
    useState: () => ["idle", (next: string) => hoisted.phases.push(next)],
    useRef: (initial: unknown) => {
      const ref = { current: initial };
      hoisted.refs.push(ref);
      return ref;
    },
    useEffect: (run: () => void | (() => void), deps: unknown[]) => {
      hoisted.effects.push({ run, deps });
    },
    useId: () => "ID",
  };
});

import { requestPreviewSlot } from "@/lib/preview-limiter";
import { ArchiveThumbnail, ArchiveThumbnailView } from "./archive-thumbnail";

const SRC = "/api/workspaces/ws-1/media/media-1/thumbnail?v=gen.7";

/** The page's observer, recording how it was made so a test can say where the tile is. */
const observers: Array<{
  options: IntersectionObserverInit | undefined;
  observed: unknown[];
  disconnected: boolean;
  callback: (entries: Array<{ isIntersecting: boolean }>) => void;
}> = [];

class FakeObserver {
  record: (typeof observers)[number];
  constructor(
    callback: (entries: Array<{ isIntersecting: boolean }>) => void,
    options?: IntersectionObserverInit,
  ) {
    this.record = { options, observed: [], disconnected: false, callback };
    observers.push(this.record);
  }
  observe(target: unknown) {
    this.record.observed.push(target);
  }
  disconnect() {
    this.record.disconnected = true;
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
});

beforeEach(() => {
  hoisted.phases.length = 0;
  hoisted.effects.length = 0;
  hoisted.refs.length = 0;
  observers.length = 0;
  vi.stubGlobal("IntersectionObserver", FakeObserver);
});

type ViewProps = Parameters<typeof ArchiveThumbnailView>[0];

/** Draws a tile and runs the effect it asked for against a box with nothing scrolling around it. */
function mount(workspaceId = "ws-wiring", over: Record<string, unknown> = {}) {
  const tree = ArchiveThumbnail({
    workspaceId,
    src: SRC,
    alt: "Instagram post",
    video: false,
    layout: "card",
    ...over,
  }) as ReactElement<ViewProps>;
  // Refs are taken in the order the component takes them: the tile's loader, then its box.
  const [tile, boxRef] = hoisted.refs;
  const box = { parentElement: null, focus: vi.fn() };
  boxRef.current = box;
  const [{ run, deps }] = hoisted.effects;
  const cleanup = run() as () => void;
  return { props: tree.props, tile, box, boxRef, deps, cleanup };
}

const near = (isIntersecting: boolean) =>
  observers[0].callback([{ isIntersecting }]);

describe("a tile on the page", () => {
  it("is watched for as long as the workspace and the box's size stay what they were", () => {
    const { deps } = mount("ws-wiring-deps", { layout: "tile" });
    expect(deps).toEqual(["ws-wiring-deps", "tile"]);
  });

  it("hands the view its box, its words' ids and what it was given, and reports nothing until it is near", () => {
    const fallback = "glyph";
    const { props, boxRef } = mount("ws-wiring-props", { layout: "tile", retry: false, fallback });
    expect(props).toMatchObject({
      phase: "idle",
      src: SRC,
      alt: "Instagram post",
      video: false,
      layout: "tile",
      retry: false,
      fallback,
      ids: "ID",
      boxRef,
    });
    expect(observers).toHaveLength(1);
    expect(hoisted.phases).toEqual([]);
  });

  it("watches its box a row of its layout out, and asks for its place only when the page says it is near", () => {
    const { box } = mount("ws-wiring-near", { layout: "card" });
    expect(observers[0].observed).toEqual([box]);
    expect(observers[0].options).toEqual({ root: null, rootMargin: "300px 0px" });
    near(false);
    expect(hoisted.phases).toEqual([]);
    near(true);
    expect(hoisted.phases).toEqual(["queued", "loading"]);
  });

  it("reports what its picture answered, and a press on Retry asks once and keeps the keyboard in the box", () => {
    const { props, box } = mount("ws-wiring-retry");
    near(true);
    props.onError();
    expect(hoisted.phases).toEqual(["queued", "loading", "failed"]);
    props.onRetry();
    expect(box.focus).toHaveBeenCalledTimes(1);
    expect(box.focus).toHaveBeenCalledWith({ preventScroll: true });
    expect(hoisted.phases).toEqual(["queued", "loading", "failed", "queued", "loading"]);
    // A picture that loads is loaded, and says so; a second press while it loads asks nothing.
    props.onRetry();
    expect(hoisted.phases).toHaveLength(5);
    props.onLoad();
    expect(hoisted.phases.at(-1)).toBe("loaded");
  });

  it("gives up its place, and stops watching, when it comes off the page, and then hears nothing", () => {
    const { props, cleanup } = mount("ws-wiring-gone");
    near(true);
    // It holds one of the workspace's two places: a probe of two gets only the other.
    const granted: number[] = [];
    const probes = [1, 2].map((n) => requestPreviewSlot("ws-wiring-gone", () => granted.push(n)));
    expect(granted).toEqual([1]);
    cleanup();
    expect(observers[0].disconnected).toBe(true);
    expect(granted).toEqual([1, 2]);
    props.onLoad();
    props.onError();
    props.onRetry();
    expect(hoisted.phases).toEqual(["queued", "loading"]);
    probes.forEach((give) => give());
  });
});
