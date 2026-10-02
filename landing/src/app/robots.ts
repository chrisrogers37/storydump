import type { MetadataRoute } from "next"
import { siteConfig } from "@/config/site"

/**
 * The one robots source (`public/robots.txt` is gone, so the two cannot
 * disagree). Crawlers are kept out of the API, of invitation links — whose
 * path IS a bearer token — and of the signed-in app, which they could not
 * render anyway. Each of those pages is also noindex (`noindexMetadata` in
 * `lib/seo.ts`). `/login` is left crawlable so its noindex can be read.
 * `seo-contract.test.ts` holds this list and the sitemap apart.
 */
export const disallowedPaths = [
  "/api/",
  "/join/",
  "/dashboard",
  "/workspaces",
  "/welcome",
  "/auth/",
]

export default function robots(): MetadataRoute.Robots {
  return {
    rules: { userAgent: "*", allow: "/", disallow: disallowedPaths },
    sitemap: `${siteConfig.url}/sitemap.xml`,
  }
}
