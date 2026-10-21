/**
 * When a past-posts preview is asked for, and what a failure does (#1644).
 * The loader is driven with the events a tile sends, against the real lane, so
 * what is pinned is what a page of tiles does: asks for nothing until near, two
 * at a time, one more only when a place comes free, and never again by itself.
 * Each test names a workspace of its own, as the lanes are shared.
 */

import { afterEach, describe, expect, it } from "vitest";
import {
  ROW_PX,
  createPreviewLoader,
  mountPreview,
  previewFlow,
  scrollParent,
  watchProximity,
  type PreviewEvent,
  type PreviewLoader,
  type PreviewPhase,
} from "./preview-loader";
import { requestPreviewSlot } from "./preview-limiter";

const PHASES: PreviewPhase[] = ["idle", "queued", "loading", "loaded", "failed"];
const EVENTS: PreviewEvent[] = ["near", "far", "granted", "loaded", "failed", "retry"];

describe("previewFlow", () => {
  /** The only moves there are; every other event leaves a tile where it is. */
  const MOVES: Record<string, PreviewPhase> = {
    "idle near": "queued",
    "queued far": "idle",
    "queued granted": "loading",
    "loading loaded": "loaded",
    "loading failed": "failed",
    "failed retry": "queued",
  };

  it("makes exactly these moves, and no other, across every phase and event", () => {
    for (const phase of PHASES) {
      for (const event of EVENTS) {
        expect(previewFlow(phase, event), `${phase} ${event}`).toBe(
          MOVES[`${phase} ${event}`] ?? phase,
        );
      }
    }
  });

  it("leaves a failed tile failed however it is nudged: nothing but a press on Retry asks again", () => {
    let phase: PreviewPhase = "failed";
    for (const event of ["near", "far", "granted", "loaded", "failed", "near"] as const) {
      phase = previewFlow(phase, event);
    }
    expect(phase).toBe("failed");
  });

  it("lets a tile that has asked for its picture finish it, near or far", () => {
    expect(previewFlow("loading", "far")).toBe("loading");
    expect(previewFlow("loaded", "far")).toBe("loaded");
  });
});

/** Tiles of one workspace, each recording the phases it entered. */
function page(workspaceId: string, count: number) {
  const phases: PreviewPhase[][] = Array.from({ length: count }, () => []);
  const gone = new Set<number>();
  const tiles: PreviewLoader[] = phases.map((entered, i) => {
    const tile = createPreviewLoader(workspaceId, (phase) => entered.push(phase));
    // A removed tile's last phase is the last it reported, but it no longer loads.
    return {
      send: tile.send,
      destroy() {
        gone.add(i);
        tile.destroy();
      },
    };
  });
  return {
    tiles,
    phases,
    now: (i: number) => phases[i].at(-1) ?? "idle",
    /** Which tiles are loading, by index. */
    loading: () =>
      phases.flatMap((entered, i) => (entered.at(-1) === "loading" && !gone.has(i) ? [i] : [])),
    /** How many times tile `i` began loading: how many pictures it asked the API for. */
    asked: (i: number) => phases[i].filter((phase) => phase === "loading").length,
    destroy: () => tiles.forEach((tile) => tile.destroy()),
  };
}

let open: ReturnType<typeof page> | null = null;
const tilesOf = (workspaceId: string, count: number) => (open = page(workspaceId, count));
afterEach(() => open?.destroy());

describe("a page of tiles", () => {
  it("asks for nothing, and holds no place, until a tile is near", () => {
    const p = tilesOf("ws-loader-idle", 4);
    expect(p.phases).toEqual([[], [], [], []]);
    // The lane is empty: two probes are granted at once.
    const granted: number[] = [];
    const gives = [1, 2].map((n) => requestPreviewSlot("ws-loader-idle", () => granted.push(n)));
    expect(granted).toEqual([1, 2]);
    gives.forEach((give) => give());
  });

  it("starts two pictures and queues the rest, in the order the tiles came near", () => {
    const p = tilesOf("ws-loader-two", 5);
    [3, 0, 4, 1, 2].forEach((i) => p.tiles[i].send("near"));
    expect(p.phases[3]).toEqual(["queued", "loading"]);
    expect(p.phases[0]).toEqual(["queued", "loading"]);
    expect(p.loading()).toEqual([0, 3]);
    expect([4, 1, 2].map(p.now)).toEqual(["queued", "queued", "queued"]);
  });

  it("starts the next tile when a picture arrives, and again when one fails", () => {
    const p = tilesOf("ws-loader-free", 4);
    p.tiles.forEach((tile) => tile.send("near"));
    expect(p.loading()).toEqual([0, 1]);
    p.tiles[0].send("loaded");
    expect(p.now(0)).toBe("loaded");
    expect(p.loading()).toEqual([1, 2]);
    p.tiles[1].send("failed");
    expect(p.now(1)).toBe("failed");
    expect(p.loading()).toEqual([2, 3]);
  });

  it("frees the place of a tile that is removed while it loads, and of one that is removed while it waits", () => {
    const p = tilesOf("ws-loader-gone", 5);
    p.tiles.forEach((tile) => tile.send("near"));
    expect(p.loading()).toEqual([0, 1]);
    // Tile 3, waiting, is removed: it will never be granted.
    p.tiles[3].destroy();
    // Tile 0, loading, is removed: its place goes to the first still waiting, 2.
    p.tiles[0].destroy();
    expect(p.loading()).toEqual([1, 2]);
    p.tiles[2].send("loaded");
    expect(p.loading()).toEqual([1, 4]);
    expect(p.asked(3)).toBe(0);
  });

  it("takes a tile out of the queue when it scrolls away, but lets a loading one finish", () => {
    const p = tilesOf("ws-loader-away", 4);
    p.tiles.forEach((tile) => tile.send("near"));
    // 0 and 1 load; 2 and 3 wait. 2 scrolls away, and 0, which is loading, does too.
    p.tiles[2].send("far");
    p.tiles[0].send("far");
    expect(p.now(2)).toBe("idle");
    expect(p.now(0)).toBe("loading");
    p.tiles[0].send("loaded");
    // The place goes to 3: 2 gave up its wait, and never asked for its picture.
    expect(p.loading()).toEqual([1, 3]);
    expect(p.asked(2)).toBe(0);
    // Scrolling back to 2 asks for a place again, at the back.
    p.tiles[2].send("near");
    expect(p.now(2)).toBe("queued");
    p.tiles[1].send("loaded");
    expect(p.loading()).toEqual([2, 3]);
  });

  it("never has more than two pictures in flight through loads, failures, retries and removals", () => {
    const p = tilesOf("ws-loader-peak", 12);
    let peak = 0;
    const check = () => {
      peak = Math.max(peak, p.loading().length);
    };
    p.tiles.forEach((tile) => {
      tile.send("near");
      check();
    });
    for (let round = 0; round < 40; round++) {
      const running = p.loading();
      if (running.length === 0) break;
      const i = running[round % running.length];
      p.tiles[i].send(round % 3 === 0 ? "failed" : "loaded");
      check();
      if (round % 5 === 0) {
        p.tiles[i].send("retry");
        check();
      }
      if (round % 7 === 0) {
        p.tiles[(round + 5) % 12].destroy();
        check();
      }
    }
    expect(peak).toBe(2);
  });
});

describe("a failed picture", () => {
  /** Two tiles, the first of which has failed. */
  function failed(workspaceId: string) {
    const p = tilesOf(workspaceId, 2);
    p.tiles[0].send("near");
    p.tiles[0].send("failed");
    return p;
  }

  it("asks nothing more by itself, near or far, however often it is nudged", () => {
    const p = failed("ws-loader-failed");
    for (let i = 0; i < 5; i++) {
      p.tiles[0].send("far");
      p.tiles[0].send("near");
    }
    expect(p.now(0)).toBe("failed");
    expect(p.asked(0)).toBe(1);
    expect(p.loading()).toEqual([]);
  });

  it("gives up its place, so a tile behind it is not held back", () => {
    const p = failed("ws-loader-failed-place");
    p.tiles[1].send("near");
    expect(p.loading()).toEqual([1]);
  });

  it("asks once for each press on Retry: not before, and not twice for one", () => {
    const p = failed("ws-loader-retry");
    expect(p.asked(0)).toBe(1);
    p.tiles[0].send("retry");
    expect(p.phases[0]).toEqual(["queued", "loading", "failed", "queued", "loading"]);
    expect(p.asked(0)).toBe(2);
    // The press lands again while it is loading: nothing more is asked.
    p.tiles[0].send("retry");
    expect(p.asked(0)).toBe(2);
    // It fails again, and waits for the next press.
    p.tiles[0].send("failed");
    expect(p.asked(0)).toBe(2);
    p.tiles[0].send("retry");
    expect(p.asked(0)).toBe(3);
  });

  it("waits its turn like any other picture when the workspace's lane is full", () => {
    const p = tilesOf("ws-loader-retry-turn", 4);
    p.tiles[0].send("near");
    p.tiles[0].send("failed");
    p.tiles[1].send("near");
    p.tiles[2].send("near");
    expect(p.loading()).toEqual([1, 2]);
    p.tiles[0].send("retry");
    expect(p.now(0)).toBe("queued");
    p.tiles[1].send("loaded");
    expect(p.loading()).toEqual([0, 2]);
  });

  it("ignores Retry from any phase but failed", () => {
    const p = tilesOf("ws-loader-retry-ignored", 1);
    p.tiles[0].send("retry");
    expect(p.phases[0]).toEqual([]);
    p.tiles[0].send("near");
    p.tiles[0].send("loaded");
    p.tiles[0].send("retry");
    expect(p.phases[0]).toEqual(["queued", "loading", "loaded"]);
  });
});

describe("a loader that is told things after it is gone", () => {
  it("hears nothing and holds nothing", () => {
    const p = tilesOf("ws-loader-destroyed", 3);
    p.tiles[0].send("near");
    p.tiles[0].destroy();
    p.tiles[0].send("loaded");
    p.tiles[0].send("near");
    expect(p.phases[0]).toEqual(["queued", "loading"]);
    // Its place went back: two other tiles take both.
    p.tiles[1].send("near");
    p.tiles[2].send("near");
    expect(p.loading()).toContain(1);
    expect(p.loading()).toContain(2);
  });

  it("does not keep a place granted inside the call that asked for it, when its owner is gone by then", () => {
    const workspaceId = "ws-loader-reentrant";
    const owner: { tile?: PreviewLoader } = {};
    // A tile whose owner removes it the moment it starts loading, as a render
    // that unmounts it in answer to its own state change would.
    owner.tile = createPreviewLoader(workspaceId, (phase) => {
      if (phase === "loading") owner.tile?.destroy();
    });
    owner.tile.send("near");
    const others = page(workspaceId, 2);
    others.tiles.forEach((other) => other.send("near"));
    expect(others.loading()).toEqual([0, 1]);
    others.destroy();
  });
});

/** A stand-in for the page's observer that records how it was made. */
function observer() {
  const made: Array<{
    options: IntersectionObserverInit | undefined;
    observed: unknown[];
    disconnected: boolean;
    callback: (entries: Array<{ isIntersecting: boolean }>) => void;
  }> = [];
  class Fake {
    record: (typeof made)[number];
    constructor(
      callback: (entries: Array<{ isIntersecting: boolean }>) => void,
      options?: IntersectionObserverInit,
    ) {
      this.record = { options, observed: [], disconnected: false, callback };
      made.push(this.record);
    }
    observe(target: unknown) {
      this.record.observed.push(target);
    }
    disconnect() {
      this.record.disconnected = true;
    }
  }
  return { made, Observer: Fake as unknown as typeof IntersectionObserver };
}

describe("how near is near", () => {
  it("is one row of the tiles being drawn: a library card, or a Queue or day row", () => {
    expect(ROW_PX).toEqual({ card: 300, tile: 64 });
  });

  it("watches the tile in its scroll area, a row beyond its top and bottom, and says when it is near", () => {
    const { made, Observer } = observer();
    const target = { id: "tile" } as unknown as Element;
    const scroller = { id: "main" } as unknown as Element;
    const heard: boolean[] = [];
    const stop = watchProximity(
      target,
      { rowPx: ROW_PX.card, root: scroller },
      (near) => heard.push(near),
      Observer,
    );
    expect(made).toHaveLength(1);
    expect(made[0].options).toEqual({ root: scroller, rootMargin: "300px 0px" });
    expect(made[0].observed).toEqual([target]);

    made[0].callback([{ isIntersecting: true }]);
    made[0].callback([{ isIntersecting: false }]);
    // Entries come in the order they happened: the last says where the tile is now.
    made[0].callback([{ isIntersecting: false }, { isIntersecting: true }]);
    expect(heard).toEqual([true, false, true]);

    expect(made[0].disconnected).toBe(false);
    stop();
    expect(made[0].disconnected).toBe(true);
  });

  it("watches a row's small tile a row of its own size out, in the page when nothing scrolls", () => {
    const { made, Observer } = observer();
    watchProximity({} as Element, { rowPx: ROW_PX.tile, root: null }, () => {}, Observer);
    expect(made[0].options).toEqual({ root: null, rootMargin: "64px 0px" });
  });

  it("counts a tile as near where nothing can say it is not, and leaves the lane to bound it", () => {
    const heard: boolean[] = [];
    const stop = watchProximity(
      {} as Element,
      { rowPx: 64, root: null },
      (near) => heard.push(near),
      undefined,
    );
    expect(heard).toEqual([true]);
    expect(() => stop()).not.toThrow();
  });
});

describe("scrollParent", () => {
  type Fake = { name: string; overflow: string; parentElement: Fake | null };
  const node = (name: string, overflow: string, parentElement: Fake | null = null): Fake => ({
    name,
    overflow,
    parentElement,
  });
  const overflowOf = (n: Element) => (n as unknown as Fake).overflow;
  const find = (from: Fake) => scrollParent(from as unknown as Element, overflowOf);

  it("is the nearest ancestor that scrolls, whichever way it scrolls", () => {
    for (const scrolls of ["auto", "scroll", "overlay"]) {
      const main = node("main", scrolls, node("body", "visible"));
      const card = node("card", "hidden", node("grid", "visible", main));
      const tile = node("tile", "visible", card);
      expect(find(tile)).toBe(main);
    }
  });

  it("takes the nearest of two, and never the tile itself", () => {
    const outer = node("outer", "auto");
    const inner = node("inner", "scroll", outer);
    const tile = node("tile", "auto", inner);
    expect(find(tile)).toBe(inner);
  });

  it("is null when nothing between the tile and the page scrolls, so the page is the root", () => {
    const tile = node("tile", "auto", node("box", "hidden", node("body", "visible")));
    expect(find(tile)).toBeNull();
    expect(find(node("alone", "auto"))).toBeNull();
  });
});

describe("a tile on the page", () => {
  type Fake = { overflow: string; parentElement: Fake | null };
  const overflowOf = (n: Element) => (n as unknown as Fake).overflow;

  /** A tile's box inside the dashboard's scrolling main area. */
  function boxInMain() {
    const main: Fake = { overflow: "auto", parentElement: null };
    const box: Fake = { overflow: "visible", parentElement: main };
    return { main: main as unknown as Element, box: box as unknown as Element };
  }

  it("watches its box in the scrolling area, a row of its layout out, and queues when that is near", () => {
    const { made, Observer } = observer();
    const { main, box } = boxInMain();
    const phases: PreviewPhase[] = [];
    mountPreview(box, { workspaceId: "ws-mount-near", layout: "card" }, (p) => phases.push(p), {
      Observer,
      overflowOf,
    });
    expect(made[0].options).toEqual({ root: main, rootMargin: "300px 0px" });
    expect(made[0].observed).toEqual([box]);
    // Far from the area: nothing is asked.
    expect(phases).toEqual([]);
    made[0].callback([{ isIntersecting: true }]);
    expect(phases).toEqual(["queued", "loading"]);
  });

  it("is a Queue row's small box a row of its own size out, and turns back when it scrolls away while waiting", () => {
    const { made, Observer } = observer();
    const { box } = boxInMain();
    // Two others hold the workspace's places, so this tile has to wait.
    const others = page("ws-mount-away", 2);
    others.tiles.forEach((tile) => tile.send("near"));
    const phases: PreviewPhase[] = [];
    mountPreview(box, { workspaceId: "ws-mount-away", layout: "tile" }, (p) => phases.push(p), {
      Observer,
      overflowOf,
    });
    expect(made[0].options?.rootMargin).toBe("64px 0px");
    made[0].callback([{ isIntersecting: true }]);
    expect(phases).toEqual(["queued"]);
    made[0].callback([{ isIntersecting: false }]);
    expect(phases).toEqual(["queued", "idle"]);
    others.tiles[0].send("loaded");
    // The place did not go to the tile that turned away.
    expect(phases).toEqual(["queued", "idle"]);
    others.destroy();
  });

  it("stops watching and gives up its place when it comes off the page", () => {
    const { made, Observer } = observer();
    const { box } = boxInMain();
    const mounted = mountPreview(box, { workspaceId: "ws-mount-off", layout: "tile" }, () => {}, {
      Observer,
      overflowOf,
    });
    made[0].callback([{ isIntersecting: true }]);
    // The tile holds a place: two others fill the lane only behind it.
    const waiting = page("ws-mount-off", 2);
    waiting.tiles.forEach((tile) => tile.send("near"));
    expect(waiting.loading()).toEqual([0]);
    mounted.unmount();
    expect(made[0].disconnected).toBe(true);
    expect(waiting.loading()).toEqual([0, 1]);
    waiting.destroy();
  });
});
