/**
 * `MediaThumbnail` (#1634): a muted skeleton while the picture loads, the
 * picture cropped to fill and named by the file, a play badge on a video, and
 * the caller's fallback when the picture fails. Read as returned element
 * trees; the hooks are stubbed, so a test sets the state and reads what the
 * component asked the setter for.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement } from "react";

const hoisted = vi.hoisted(() => ({
  state: "loading" as string,
  set: [] as string[],
}));

vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return {
    ...actual,
    useState: () => [hoisted.state, (next: string) => hoisted.set.push(next)],
    useCallback: (fn: unknown) => fn,
  };
});

import { Play } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { MediaThumbnail, settleOnMount, type ThumbnailState } from "./media-thumbnail";

const SRC = "/api/workspaces/ws-1/media/media-1/thumbnail?v=0123456789abcdef";
const FALLBACK = <span>glyph</span>;

type ImgProps = {
  src: string;
  alt: string;
  loading: string;
  className: string;
  onLoad: () => void;
  onError: () => void;
};

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

const render = (video = false) =>
  MediaThumbnail({ src: SRC, alt: "beach.jpg", video, fallback: FALLBACK }) as ReactElement;

const picture = (tree: ReactElement) =>
  [...walk(tree)].find((el) => el.type === "img") as ReactElement<ImgProps> | undefined;

const has = (tree: ReactElement, type: unknown) => [...walk(tree)].some((el) => el.type === type);

beforeEach(() => {
  hoisted.state = "loading";
  hoisted.set.length = 0;
});

describe("MediaThumbnail", () => {
  it("shows a skeleton while the picture loads, lazily, cropped to fill and named by the file", () => {
    const tree = render();
    expect(has(tree, Skeleton)).toBe(true);
    const img = picture(tree)!;
    expect(img.props.src).toBe(SRC);
    expect(img.props.alt).toBe("beach.jpg");
    expect(img.props.loading).toBe("lazy");
    expect(img.props.className).toContain("object-cover");
  });

  it("drops the skeleton once the picture has loaded", () => {
    hoisted.state = "loaded";
    const tree = render();
    expect(has(tree, Skeleton)).toBe(false);
    expect(picture(tree)).toBeDefined();
  });

  it("badges a video, and nothing else", () => {
    expect(has(render(true), Play)).toBe(true);
    expect(has(render(false), Play)).toBe(false);
  });

  it("settles as loaded, or as failed, when the picture says which", () => {
    const img = picture(render())!;
    img.props.onLoad();
    img.props.onError();
    expect(hoisted.set).toEqual(["loaded", "failed"]);
  });

  it("draws the caller's fallback, and no picture, once the picture has failed", () => {
    hoisted.state = "failed";
    const tree = render();
    expect(picture(tree)).toBeUndefined();
    expect([...walk(tree)]).toContain(FALLBACK);
  });
});

describe("settleOnMount", () => {
  /** An image as the browser left it before React attached its handlers. */
  function image(complete: boolean, naturalWidth: number) {
    const img = { complete, naturalWidth, assigned: [] as string[] };
    return Object.defineProperty(img, "src", {
      get: () => SRC,
      set: (value: string) => img.assigned.push(value),
    }) as typeof img & { src: string };
  }

  function settle(img: ReturnType<typeof image> | null) {
    const set: ThumbnailState[] = [];
    settleOnMount(img, (state) => set.push(state));
    return set;
  }

  it("marks a picture that loaded before hydration as loaded", () => {
    expect(settle(image(true, 120))).toEqual(["loaded"]);
  });

  it("asks again for a picture that broke before hydration, so its error reaches the handler", () => {
    const img = image(true, 0);
    expect(settle(img)).toEqual([]);
    expect(img.assigned).toEqual([SRC]);
  });

  it("leaves a picture still on its way alone", () => {
    const img = image(false, 0);
    expect(settle(img)).toEqual([]);
    expect(img.assigned).toEqual([]);
    expect(settle(null)).toEqual([]);
  });
});
