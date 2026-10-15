import { NextRequest, NextResponse } from "next/server";
import { requireWorkspace } from "@/lib/route-guards";
import { isUuid } from "@/lib/session";
import { targetFetchBytes } from "@/lib/target-api";
import { THUMBNAIL_TYPES } from "@/lib/thumbnails";

/**
 * What `?v=` may be: the item's `thumbnail_version` as `thumbnailSrc` puts it in
 * the URL, a Drive item's hash prefix or an archive item's
 * `<generation>.<epoch>`. Opaque here, checked only for length and for the
 * characters a URL leaves alone; the API decides whether it is the current one.
 */
const VERSION = /^[A-Za-z0-9._~-]{1,128}$/;

/**
 * GET /api/workspaces/[id]/media/[mediaId]/thumbnail?v= — a media item's
 * thumbnail, streamed from the API, which fetched it from the provider behind
 * the workspace check.
 *
 * `v` is forwarded to the API, which needs it for an archive item: it is the
 * generation and epoch the picture may be answered for, and an archive item
 * answers 404 to any other. A Drive item's API ignores it, and its picture is
 * kept by the browser by URL. No other query is forwarded.
 *
 * The API's `Cache-Control` rides through unchanged, so an archive picture's
 * `private, no-store, max-age=0` is never turned into a copy anyone keeps. A
 * failure is a bare status that is never kept, and the page draws its
 * placeholder in the picture's place. Only the API's own refusals keep their
 * status: a redirect or a "not modified" would have a browser reuse a picture.
 *
 * A failure answers at once. The upstream body is released, never awaited: under
 * Next's runtime (`next dev` and `next build` + `next start`, Node 20 and 22) the
 * cancel of a body nobody reads can stay pending for good, and a route that
 * waits on it never sends its 404. The page asks for archive pictures two at a
 * time, so two such requests would hold every other preview in the tab.
 */
export async function GET(
  request: NextRequest,
  context: { params: Promise<{ id: string; mediaId: string }> },
) {
  const guard = await requireWorkspace(context);
  if (guard instanceof NextResponse) return guard;
  const { token, id } = guard;
  const { mediaId } = await context.params;
  if (!isUuid(mediaId)) {
    return NextResponse.json({ error: "invalid_media" }, { status: 400 });
  }
  const version = request.nextUrl.searchParams.get("v");
  if (version !== null && !VERSION.test(version)) {
    return NextResponse.json(
      { error: "invalid_version" },
      { status: 400, headers: { "Cache-Control": "no-store" } },
    );
  }
  const upstream = await targetFetchBytes(
    `/workspaces/${id}/media/${mediaId}/thumbnail` +
      (version === null ? "" : `?v=${encodeURIComponent(version)}`),
    token,
  );
  const type =
    upstream.headers.get("content-type")?.split(";")[0].trim().toLowerCase() ?? "";
  if (!upstream.ok || !THUMBNAIL_TYPES.includes(type)) {
    // Released and not awaited, so the answer waits on nothing; a cancel that
    // fails has nothing to say to anyone.
    void upstream.body?.cancel().catch(() => {});
    return new NextResponse(null, {
      status: upstream.status >= 400 ? upstream.status : 502,
      headers: { "Cache-Control": "no-store" },
    });
  }
  return new NextResponse(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": type,
      "Cache-Control": upstream.headers.get("cache-control") ?? "no-store",
    },
  });
}
