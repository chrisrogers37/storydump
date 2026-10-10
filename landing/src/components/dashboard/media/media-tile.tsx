import type { ReactElement } from "react";
import { ImageIcon, Video } from "lucide-react";
import { MediaThumbnail } from "@/components/dashboard/media/media-thumbnail";
import { thumbnailFor, type ThumbnailMedia } from "@/lib/thumbnails";
import { cn } from "@/lib/utils";

/**
 * A media item's picture in a box of the caller's size (#1634): the thumbnail
 * through this tier's route when the row has one and there is a workspace to
 * ask, else the image or video glyph, which also stands in when the picture
 * fails. The Queue's rows, the calendar's chips and its day view all draw it.
 *
 * Called as a function, not drawn as a component, so the box's own elements sit
 * in the caller's tree. This module is not a client module, so a server
 * component (the day view) may call it; `MediaThumbnail` is the client part.
 */
export function mediaTile({
  media,
  workspaceId,
  box,
  glyph: glyphSize,
  badge = true,
}: {
  media: ThumbnailMedia;
  workspaceId: string | null | undefined;
  /** The box's size and rounding, as "h-10 w-10 rounded-md". */
  box: string;
  /** The glyph's size, as "h-5 w-5". */
  glyph: string;
  /** A video's play badge, which is too big for a chip's box. */
  badge?: boolean;
}): ReactElement {
  const video = media.media_kind === "video";
  const Glyph = video ? Video : ImageIcon;
  const glyph = <Glyph className={cn(glyphSize, "text-muted-foreground")} aria-hidden />;
  const src = thumbnailFor(media, workspaceId);
  return (
    <div
      className={cn(
        "relative flex shrink-0 items-center justify-center overflow-hidden bg-muted",
        box,
      )}
    >
      {src ? (
        <MediaThumbnail
          key={src}
          src={src}
          alt={media.file_name}
          video={badge && video}
          fallback={glyph}
        />
      ) : (
        glyph
      )}
    </div>
  );
}
