"use client";

import { useEffect, useId, useRef, useState, type ReactNode, type Ref } from "react";
import { ImageOff, Play, RotateCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { PREVIEW_UNAVAILABLE_COPY } from "@/lib/past-posts-media";
import {
  mountPreview,
  type PreviewLayout,
  type PreviewLoader,
  type PreviewPhase,
} from "@/lib/preview-loader";

/**
 * A past-posts item's picture, filling the box its caller draws, as `MediaThumbnail`
 * does for Drive. Each picture is a fresh
 * lookup at Instagram, so it is asked for under limits Drive's is not: not until
 * the tile is within one row of the screen, then two at a time for the
 * workspace, and never again by itself.
 *
 * A picture that does not come shows what it is, an Instagram preview that is
 * unavailable, with a Retry that asks once for each press. What it shows is
 * still the provider's thumbnail: a video is its poster with the play badge, and
 * nothing here ever asks for a video file.
 *
 * `layout` is the size of the box. A `card` has room for the sentence and a
 * Retry button. A `tile` is a row's small box, which holds the Retry as an icon,
 * or, with `retry` off, none: a box inside a link may not hold a button.
 *
 * Its caller keys it by `src`, as it does `MediaThumbnail`: a new version of the
 * picture is a new tile, with its own wait and its own chance.
 */
export function PastPostsThumbnail({
  workspaceId,
  src,
  alt,
  video,
  layout,
  retry = true,
  fallback,
}: {
  workspaceId: string;
  src: string;
  alt: string;
  video: boolean;
  layout: PreviewLayout;
  retry?: boolean;
  /** What a `tile` without Retry shows when the picture fails: the glyph. */
  fallback?: ReactNode;
}) {
  const [phase, setPhase] = useState<PreviewPhase>("idle");
  const tile = useRef<PreviewLoader | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const ids = useId();

  useEffect(() => {
    const element = box.current;
    if (!element) return;
    const mounted = mountPreview(element, { workspaceId, layout }, setPhase);
    tile.current = mounted.loader;
    return () => {
      mounted.unmount();
      tile.current = null;
    };
  }, [workspaceId, layout]);

  return (
    <PastPostsThumbnailView
      phase={phase}
      src={src}
      alt={alt}
      video={video}
      layout={layout}
      retry={retry}
      fallback={fallback}
      ids={ids}
      boxRef={box}
      onLoad={() => tile.current?.send("loaded")}
      onError={() => tile.current?.send("failed")}
      onRetry={() => {
        // The button is about to go: keep a keyboard where it was.
        box.current?.focus({ preventScroll: true });
        tile.current?.send("retry");
      }}
    />
  );
}

/**
 * What a tile draws in each phase, from the phase alone: it holds no state, so
 * every phase is drawn by a test as it is. The picture's address is set only
 * once the tile has its place, and a tile that has not asked, or is waiting,
 * draws only the skeleton, which asks for nothing.
 */
export function PastPostsThumbnailView({
  phase,
  src,
  alt,
  video,
  layout,
  retry,
  fallback,
  ids,
  boxRef,
  onLoad,
  onError,
  onRetry,
}: {
  phase: PreviewPhase;
  src: string;
  alt: string;
  video: boolean;
  layout: PreviewLayout;
  retry: boolean;
  fallback?: ReactNode;
  /** Prefix of the ids this tile's sentences take, for the button that points at them. */
  ids: string;
  boxRef?: Ref<HTMLDivElement>;
  onLoad: () => void;
  onError: () => void;
  onRetry: () => void;
}) {
  const showing = phase === "loading" || phase === "loaded";
  return (
    <div
      ref={boxRef}
      tabIndex={-1}
      data-preview-phase={phase}
      className="absolute inset-0 outline-none"
    >
      {phase === "failed" ? (
        unavailable({ layout, retry, fallback, ids, onRetry })
      ) : (
        <>
          {phase === "loaded" ? null : <Skeleton className="absolute inset-0 rounded-none" />}
          {showing ? (
            <>
              {/* Not lazy: the tile is already near, and a browser holding back a picture
                  that has its place would hold the place with it. */}
              {/* A plain img: the route serves one small, finished picture. */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={src}
                alt={alt}
                decoding="async"
                onLoad={onLoad}
                onError={onError}
                className="absolute inset-0 h-full w-full object-cover"
              />
              {video ? (
                <span
                  aria-hidden
                  className="absolute bottom-0.5 right-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-black/60 text-white"
                >
                  <Play className="h-2.5 w-2.5 fill-current" />
                </span>
              ) : null}
            </>
          ) : null}
        </>
      )}
    </div>
  );
}

/** The placeholder, drawn in the box where the picture would be. */
function unavailable({
  layout,
  retry,
  fallback,
  ids,
  onRetry,
}: {
  layout: PreviewLayout;
  retry: boolean;
  fallback?: ReactNode;
  ids: string;
  onRetry: () => void;
}) {
  if (layout === "card") {
    return (
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-muted p-2 text-center text-muted-foreground">
        <ImageOff className="h-5 w-5" aria-hidden />
        <p id={`${ids}-unavailable`} className="text-xs">
          {PREVIEW_UNAVAILABLE_COPY}
        </p>
        <Button
          type="button"
          size="xs"
          variant="outline"
          aria-describedby={`${ids}-unavailable`}
          onClick={onRetry}
        >
          Retry
        </Button>
      </div>
    );
  }
  return (
    <>
      {retry ? (
        <button
          type="button"
          aria-label="Retry Instagram preview"
          title={`${PREVIEW_UNAVAILABLE_COPY}. Retry.`}
          onClick={onRetry}
          className="absolute inset-0 flex items-center justify-center bg-muted text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-hidden"
        >
          <RotateCw className="h-4 w-4" aria-hidden />
        </button>
      ) : (
        fallback
      )}
      <span className="sr-only">{PREVIEW_UNAVAILABLE_COPY}</span>
    </>
  );
}
