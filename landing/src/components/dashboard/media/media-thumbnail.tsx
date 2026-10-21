"use client";

import { useCallback, useState, type ReactNode } from "react";
import { Play } from "lucide-react";
import { PastPostsThumbnail } from "@/components/dashboard/media/past-posts-thumbnail";
import { Skeleton } from "@/components/ui/skeleton";
import type { PreviewLayout } from "@/lib/preview-loader";

export type ThumbnailState = "loading" | "loaded" | "failed";

/**
 * How a past-posts item's picture is asked for (`PastPostsThumbnail`): in this
 * workspace's lane of two, by the box's `layout`, and with a Retry button
 * unless the box sits inside a link.
 */
export type PastPostsPreview = {
  workspaceId: string;
  layout: PreviewLayout;
  retry?: boolean;
};

/**
 * A media item's thumbnail, filling the box its caller draws: the box sets the
 * size, rounding and background, and must be `relative` and clip its overflow.
 * `fallback` takes the picture's place when the picture fails.
 *
 * A Drive item's picture is the browser's to load, lazily. A past-posts item's
 * (`pastPosts`) is asked for only under the past-posts source's own limits, and when it
 * fails shows the Instagram placeholder, with a Retry where the box has room
 * for one, rather than going without.
 */
export function MediaThumbnail({
  src,
  alt,
  video,
  fallback,
  pastPosts,
}: {
  src: string;
  /** The file's name. */
  alt: string;
  video: boolean;
  /** What the box shows when the picture fails: the glyph, or nothing. */
  fallback: ReactNode;
  pastPosts?: PastPostsPreview;
}) {
  const [state, setState] = useState<ThumbnailState>("loading");
  const settle = useCallback((img: HTMLImageElement | null) => {
    settleOnMount(img, setState);
  }, []);

  if (pastPosts) {
    return (
      <PastPostsThumbnail
        src={src}
        alt={alt}
        video={video}
        fallback={fallback}
        {...pastPosts}
      />
    );
  }
  if (state === "failed") return <>{fallback}</>;
  return (
    <>
      {state === "loading" ? (
        <Skeleton className="absolute inset-0 rounded-none" />
      ) : null}
      {/* A plain img: the route already serves a small, finished picture. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        ref={settle}
        src={src}
        alt={alt}
        loading="lazy"
        decoding="async"
        onLoad={() => setState("loaded")}
        onError={() => setState("failed")}
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
  );
}

/**
 * The answer an image got before React attached its handlers, which on a
 * server-rendered page it never reports. A loaded image is marked loaded. A
 * broken one is given its source again, so its error arrives now that the
 * handler is attached; Next's own image component does the same. An image
 * still on its way, or not yet asked for, is left alone.
 */
export function settleOnMount(
  img: Pick<HTMLImageElement, "complete" | "naturalWidth" | "src"> | null,
  setState: (state: ThumbnailState) => void,
): void {
  if (!img?.complete) return;
  if (img.naturalWidth > 0) {
    setState("loaded");
    return;
  }
  const source = img.src;
  img.src = source;
}
