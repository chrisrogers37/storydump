/**
 * When a past-posts preview is asked for, and what it does when it fails.
 * Nothing here is React's or the DOM's, so each rule is pinned without either;
 * `PastPostsThumbnail` wires it to a tile.
 *
 * A tile starts idle and asks for nothing until it is near the screen: the
 * viewport and one row of tiles beyond it. Near, it queues for one of the
 * workspace's two places (`preview-limiter.ts`) and only then sets its picture's
 * address. A picture that arrives is loaded, and one that fails is failed, and
 * stays failed: scrolling away and back never asks again. Only `retry`, which a
 * person's press on Retry sends, queues a failed tile again, and it asks once
 * for each press.
 */

import { requestPreviewSlot } from "./preview-limiter";

export type PreviewPhase = "idle" | "queued" | "loading" | "loaded" | "failed";

export type PreviewEvent =
  /** The tile is within the viewport and a row of it, or no longer is. */
  | "near"
  | "far"
  /** The workspace has a place for it. */
  | "granted"
  /** What the picture itself answered. */
  | "loaded"
  | "failed"
  /** A press on Retry. */
  | "retry";

/** The phase an event leaves a tile in. An event that does not apply changes nothing. */
export function previewFlow(phase: PreviewPhase, event: PreviewEvent): PreviewPhase {
  switch (event) {
    case "near":
      return phase === "idle" ? "queued" : phase;
    // Only a tile still waiting turns back: one that has asked for its picture finishes.
    case "far":
      return phase === "queued" ? "idle" : phase;
    case "granted":
      return phase === "queued" ? "loading" : phase;
    case "loaded":
      return phase === "loading" ? "loaded" : phase;
    case "failed":
      return phase === "loading" ? "failed" : phase;
    case "retry":
      return phase === "failed" ? "queued" : phase;
  }
}

/** A tile holds a place from the moment it queues until its picture settles or it goes. */
const holdsPlace = (phase: PreviewPhase) => phase === "queued" || phase === "loading";

export type PreviewLoader = {
  send(event: PreviewEvent): void;
  /** The tile is gone: its place is given up, or its request withdrawn. */
  destroy(): void;
};

/**
 * One tile's loader. `onPhase` hears each phase it enters, in order. It owns the
 * tile's place in the workspace's lane: asked for on entering `queued`, kept
 * through `loading`, and given up on leaving them by any road, which is how a
 * picture that arrived, one that failed, a tile that turned back and a tile that
 * was removed all free their place.
 */
export function createPreviewLoader(
  workspaceId: string,
  onPhase: (phase: PreviewPhase) => void,
): PreviewLoader {
  let phase: PreviewPhase = "idle";
  let release: (() => void) | undefined;
  let destroyed = false;

  function send(event: PreviewEvent) {
    if (destroyed) return;
    const next = previewFlow(phase, event);
    if (next === phase) return;
    const asking = holdsPlace(next) && !holdsPlace(phase);
    const leaving = holdsPlace(phase) && !holdsPlace(next);
    phase = next;
    onPhase(phase);
    if (leaving) {
      release?.();
      release = undefined;
    }
    if (asking) {
      // After the phase is reported: a free lane grants at once, inside this call,
      // and what that call set going may already have ended the tile's wait.
      const place = requestPreviewSlot(workspaceId, () => send("granted"));
      if (destroyed || !holdsPlace(phase)) place();
      else release = place;
    }
  }

  return {
    send,
    destroy() {
      destroyed = true;
      release?.();
      release = undefined;
    },
  };
}

/**
 * One row of tiles, in pixels, by what the tile is. A library card is about 300
 * tall with the grid's gap; a Queue row is about 72 and a day row about 56, so
 * 64 stands for both.
 */
export const ROW_PX = { card: 300, tile: 64 } as const;

export type PreviewLayout = keyof typeof ROW_PX;

/**
 * A tile's loader, watching its box: what a tile runs for as long as it is on
 * the page. The box is asked about in the area it scrolls in, a row beyond it
 * (`ROW_PX`), and a tile near that area queues for its place. `unmount` stops
 * the watching and gives up the place or the wait.
 */
export function mountPreview(
  element: Element,
  { workspaceId, layout }: { workspaceId: string; layout: PreviewLayout },
  onPhase: (phase: PreviewPhase) => void,
  env: { Observer?: typeof IntersectionObserver; overflowOf?: (node: Element) => string } = {},
): { loader: PreviewLoader; unmount: () => void } {
  const loader = createPreviewLoader(workspaceId, onPhase);
  const stop = watchProximity(
    element,
    { rowPx: ROW_PX[layout], root: scrollParent(element, env.overflowOf) },
    (near) => loader.send(near ? "near" : "far"),
    env.Observer,
  );
  return {
    loader,
    unmount() {
      stop();
      loader.destroy();
    },
  };
}

/**
 * The nearest ancestor that scrolls, or null for the page. The dashboard scrolls
 * inside its main area, and a margin only widens the area it scrolls in when
 * that area is the observer's root.
 */
export function scrollParent(
  element: Element,
  overflowOf: (node: Element) => string = (node) => getComputedStyle(node).overflowY,
): Element | null {
  for (let node = element.parentElement; node; node = node.parentElement) {
    if (["auto", "scroll", "overlay"].includes(overflowOf(node))) return node;
  }
  return null;
}

/**
 * Tells `onChange` whether `target` is within `rowPx` of the area it scrolls in
 * (`root`; null for the page), now and as it scrolls. Where there is no observer
 * to ask, the tile counts as near, and the lane alone bounds it. Returns the
 * function that stops watching.
 */
export function watchProximity(
  target: Element,
  { rowPx, root }: { rowPx: number; root: Element | null },
  onChange: (near: boolean) => void,
  Observer: typeof IntersectionObserver | undefined = globalThis.IntersectionObserver,
): () => void {
  if (!Observer) {
    onChange(true);
    return () => {};
  }
  const observer = new Observer(
    (entries) => {
      // Entries arrive in the order they happened: the last is where the tile is now.
      const latest = entries[entries.length - 1];
      if (latest) onChange(latest.isIntersecting);
    },
    { root, rootMargin: `${rowPx}px 0px` },
  );
  observer.observe(target);
  return () => observer.disconnect();
}
