/**
 * A past-posts item's picture (#1644), drawn in each phase of its life
 * (`environment: "node"`: markup, not a browser). `PastPostsThumbnailView` holds no
 * state, so a phase is a prop, and what a person would see and be able to press
 * is read back from the markup; what the tile does between phases, and when it
 * asks, is `preview-loader.test.ts`'s.
 */

import { createElement, isValidElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ImageIcon } from "lucide-react";
import { describe, expect, it } from "vitest";
import { Button } from "@/components/ui/button";
import type { PreviewLayout, PreviewPhase } from "@/lib/preview-loader";
import { PastPostsThumbnail, PastPostsThumbnailView } from "./past-posts-thumbnail";

const SRC = "/api/workspaces/ws-1/media/media-1/thumbnail?v=5b0e1c2e-8c67-4d29-9a3f-0c1d2e3f4a5b.7";

const PHASES: PreviewPhase[] = ["idle", "queued", "loading", "loaded", "failed"];
const LAYOUTS: PreviewLayout[] = ["card", "tile"];

const noop = () => {};

type Props = Parameters<typeof PastPostsThumbnailView>[0];

function props(over: Partial<Props> = {}): Props {
  return {
    phase: "idle",
    src: SRC,
    alt: "Instagram post",
    video: false,
    layout: "card",
    retry: true,
    ids: "ID",
    onLoad: noop,
    onError: noop,
    onRetry: noop,
    ...over,
  };
}

const draw = (over: Partial<Props> = {}) =>
  renderToStaticMarkup(createElement(PastPostsThumbnailView, props(over)));

/** The visible and spoken text of some markup, tags dropped, entities read, white space folded. */
const textOf = (html: string) =>
  html
    .replace(/<[^>]*>/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();

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

const tree = (over: Partial<Props> = {}) => PastPostsThumbnailView(props(over)) as ReactElement;

describe("before a picture is asked for", () => {
  it("draws a skeleton and no picture, so nothing is requested, while idle and while waiting its turn", () => {
    for (const phase of ["idle", "queued"] as const) {
      for (const layout of LAYOUTS) {
        const html = draw({ phase, layout });
        expect(html, `${phase} ${layout}`).not.toContain("<img");
        expect(html, `${phase} ${layout}`).not.toContain("src=");
        expect(html, `${phase} ${layout}`).toContain('data-slot="skeleton"');
        expect(html).toContain(`data-preview-phase="${phase}"`);
        expect(textOf(html)).toBe("");
      }
    }
  });

  it("draws no picture on the server either: a tile's first render is idle", () => {
    const html = renderToStaticMarkup(
      createElement(PastPostsThumbnail, {
        workspaceId: "ws-1",
        src: SRC,
        alt: "Instagram post",
        video: false,
        layout: "card",
      }),
    );
    expect(html).toContain('data-preview-phase="idle"');
    expect(html).not.toContain("<img");
  });
});

describe("a picture with its place", () => {
  it("sets the picture's address once, under a skeleton, and never lazily", () => {
    const html = draw({ phase: "loading" });
    expect(html.match(/<img/g)).toHaveLength(1);
    expect(html).toContain(`src="${SRC}"`);
    expect(html).toContain('alt="Instagram post"');
    expect(html).toContain('data-slot="skeleton"');
    // A browser holding a picture back would hold its place with it.
    expect(html).not.toMatch(/\sloading=/);
  });

  it("is the picture alone once it has loaded", () => {
    const html = draw({ phase: "loaded" });
    expect(html).toContain(`src="${SRC}"`);
    expect(html).not.toContain('data-slot="skeleton"');
    expect(html).toContain("object-cover");
  });

  it("badges a video's provider thumbnail with the play mark, and a photo with nothing", () => {
    const badge = /<span[^>]*aria-hidden="true"[^>]*bg-black\/60/;
    for (const phase of ["loading", "loaded"] as const) {
      expect(draw({ phase, video: true }), phase).toMatch(badge);
      expect(draw({ phase, video: false }), phase).not.toMatch(badge);
    }
    // No picture yet, or none at all: no badge.
    for (const phase of ["idle", "queued", "failed"] as const) {
      expect(draw({ phase, video: true }), phase).not.toMatch(badge);
    }
  });

  it("is told apart for a reader by the file's name and nothing else", () => {
    expect(draw({ phase: "loaded", alt: "Instagram post" })).toContain('alt="Instagram post"');
  });
});

describe("a picture that did not come", () => {
  it("says an Instagram preview is unavailable, and offers Retry, in a card", () => {
    const html = draw({ phase: "failed", layout: "card" });
    expect(textOf(html)).toBe("Instagram preview unavailable Retry");
    expect(html).not.toContain("<img");
    expect(html).not.toContain('data-slot="skeleton"');
    const button = html.match(/<button[^>]*>Retry<\/button>/)?.[0] ?? "";
    expect(button).toContain('type="button"');
    // The button points at the sentence, so a reader hears what is being retried.
    expect(button).toContain('aria-describedby="ID-unavailable"');
    expect(html).toMatch(/<p id="ID-unavailable"[^>]*>Instagram preview unavailable<\/p>/);
  });

  it("offers Retry as a named icon button in a row's small box, with the sentence for a reader", () => {
    const html = draw({ phase: "failed", layout: "tile", retry: true });
    expect(html).toMatch(/<button[^>]*type="button"/);
    expect(html).toContain('aria-label="Retry Instagram preview"');
    expect(html).toMatch(/<span class="sr-only">Instagram preview unavailable<\/span>/);
    expect(html).not.toContain("<img");
    expect(html.match(/<button/g)).toHaveLength(1);
  });

  it("offers no button where the box sits inside a link, and draws the glyph it was given", () => {
    const html = renderToStaticMarkup(
      createElement(PastPostsThumbnailView, {
        ...props({ phase: "failed", layout: "tile", retry: false }),
        fallback: createElement(ImageIcon, { className: "glyph" }),
      }),
    );
    expect(html).not.toContain("<button");
    expect(html).toContain("glyph");
    expect(textOf(html)).toBe("Instagram preview unavailable");
  });

  it("never shows a reader the provider's failure, a URL or an id, only that it is unavailable", () => {
    for (const layout of LAYOUTS) {
      const text = textOf(draw({ phase: "failed", layout }));
      expect(text).not.toMatch(/https?:|\d{6,}|error|404|forbidden/i);
    }
  });

  it("presses Retry through the handler it was handed, and no other control does", () => {
    for (const layout of LAYOUTS) {
      const onRetry = () => {};
      const clicks = [...walk(tree({ phase: "failed", layout, onRetry }))]
        .map((el) => (el.props as { onClick?: unknown }).onClick)
        .filter((handler) => handler !== undefined);
      expect(clicks, layout).toEqual([onRetry]);
    }
    const card = [...walk(tree({ phase: "failed", layout: "card" }))].filter((el) => el.type === Button);
    expect(card).toHaveLength(1);
  });

  it("reports what the picture itself answered, through the handlers it was handed", () => {
    const onLoad = () => {};
    const onError = () => {};
    const img = [...walk(tree({ phase: "loading", onLoad, onError }))].find((el) => el.type === "img");
    expect(img?.props).toMatchObject({ onLoad, onError });
  });
});

describe("nothing ever asks for a video file", () => {
  it("draws only the thumbnail's own address, in every phase, layout and kind of item", () => {
    for (const phase of PHASES) {
      for (const layout of LAYOUTS) {
        for (const video of [true, false]) {
          // React hints the picture it renders as a preload of the same address; that
          // is the thumbnail again, and a client-only phase never reaches the server.
          const html = draw({ phase, layout, video }).replace(
            /<link rel="preload" as="image" href="[^"]*"\/>/g,
            "",
          );
          const where = `${phase} ${layout} ${video}`;
          expect(html, where).not.toMatch(/<video|<source|<audio|<iframe|<a\s|href=|srcset|\.mp4/i);
          const sources = [...html.matchAll(/\ssrc="([^"]*)"/g)].map((m) => m[1]);
          expect(sources.every((src) => src === SRC), where).toBe(true);
        }
      }
    }
  });

  it("hints no address but the thumbnail's, and only once the tile has its place", () => {
    for (const phase of PHASES) {
      const hints = [...draw({ phase }).matchAll(/<link[^>]*href="([^"]*)"/g)].map((m) => m[1]);
      expect(hints, phase).toEqual(phase === "loading" || phase === "loaded" ? [SRC] : []);
    }
  });
});

describe("a tile that holds focus", () => {
  it("is a box a keyboard can be left in when Retry goes, and shows no ring of its own", () => {
    const html = draw({ phase: "failed" });
    expect(html).toMatch(/<div[^>]*tabindex="-1"[^>]*outline-none/);
  });
});
