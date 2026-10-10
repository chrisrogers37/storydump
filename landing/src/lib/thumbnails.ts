/**
 * The image types a thumbnail may be: raster only, the API's
 * `vocabulary.THUMBNAIL_TYPES` (held to it by `wire-contract.test.ts`). The
 * thumbnail route relays no other, so an SVG never reaches the page.
 */
export const THUMBNAIL_TYPES: readonly string[] = [
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
  "image/avif",
];

/**
 * A media item's thumbnail URL: this tier's route
 * (`app/api/workspaces/[id]/media/[mediaId]/thumbnail`), never a provider's
 * link. The version is the payload's `thumbnail_version`; one version is always
 * one picture, so the browser may keep each URL's answer.
 */
export function thumbnailSrc(
  workspaceId: string,
  mediaId: string,
  version: string,
): string {
  const ws = encodeURIComponent(workspaceId);
  const media = encodeURIComponent(mediaId);
  return `/api/workspaces/${ws}/media/${media}/thumbnail?v=${encodeURIComponent(version)}`;
}

/**
 * What a picture needs from a payload row. The intents read and the calendar's
 * month read carry all of it; a row without the thumbnail's fields (the sample
 * workspace's calendar) draws its glyph.
 */
export type ThumbnailMedia = {
  /** The picture's alt text. */
  file_name: string;
  media_kind?: string;
  media_item_id?: string;
  has_thumbnail?: boolean;
  thumbnail_version?: string;
};

/**
 * The row's thumbnail URL, or null when it has no picture or there is no
 * workspace to ask, which is how the sample workspace draws.
 */
export function thumbnailFor(
  media: ThumbnailMedia,
  workspaceId: string | null | undefined,
): string | null {
  return workspaceId &&
    media.has_thumbnail &&
    media.media_item_id &&
    media.thumbnail_version !== undefined
    ? thumbnailSrc(workspaceId, media.media_item_id, media.thumbnail_version)
    : null;
}
