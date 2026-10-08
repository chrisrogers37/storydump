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
