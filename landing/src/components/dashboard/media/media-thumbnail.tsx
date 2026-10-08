"use client";

import { useCallback, useState, type ReactNode } from "react";
import { Play } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";

export type ThumbnailState = "loading" | "loaded" | "failed";

/**
 * A media item's thumbnail, filling the box its caller draws: the box sets the
 * size, rounding and background, and must be `relative` and clip its overflow.
 * `fallback` takes the picture's place when the picture fails.
 */
export function MediaThumbnail({
  src,
  alt,
  video,
  fallback,
}: {
  src: string;
  /** The file's name. */
  alt: string;
  video: boolean;
  /** What the box shows when the picture fails: the glyph, or nothing. */
  fallback: ReactNode;
}) {
  const [state, setState] = useState<ThumbnailState>("loading");
  const settle = useCallback((img: HTMLImageElement | null) => {
    settleOnMount(img, setState);
  }, []);

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
