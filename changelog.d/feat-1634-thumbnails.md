### Added

- **Thumbnails in the Queue and the Media Library, served through the authenticated API (#1648; #1634).** The sync stores each Drive file's thumbnail link, and `GET /api/v1/workspaces/{ws}/media/{media_id}/thumbnail` fetches the picture from Drive on the server, under the workspace's grant and behind the member check the media reads use; the page never receives a Google link. Only raster images within a 1 MiB cap are served, never SVG. An expired link is re-read from Drive once and retried once; any other failure is a 404, and the page shows its placeholder. The browser keeps each thumbnail privately for 30 days, under a URL that carries the file's version. A Queue row draws the thumbnail in its 40 px box, with a skeleton while it loads, a play badge on a video and the glyph when there is none; a Media Library tile keeps its file-type label as the fallback.

### Changed

- **The intents and media reads return `has_thumbnail` and `thumbnail_version` in place of `thumbnail_url` (#1634).** The provider's link stays on the server, and the pages' Content-Security-Policy loads images from this origin only (`img-src 'self' data:`).
