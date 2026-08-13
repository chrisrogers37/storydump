import type { MetadataRoute } from "next"
import { siteConfig } from "@/config/site"
import { posts } from "@/lib/blog"

export const dynamic = "force-dynamic"

export default function sitemap(): MetadataRoute.Sitemap {
  const now = new Date()

  const blogEntries: MetadataRoute.Sitemap = [
    {
      url: `${siteConfig.url}/blog`,
      lastModified: now,
      changeFrequency: "weekly",
      priority: 0.8,
    },
    ...posts.map((post) => ({
      url: `${siteConfig.url}/blog/${post.slug}`,
      lastModified: new Date(post.date),
      changeFrequency: "monthly" as const,
      priority: 0.7,
    })),
  ]

  return [
    {
      url: siteConfig.url,
      lastModified: now,
      changeFrequency: "weekly",
      priority: 1,
    },
    ...blogEntries,
    {
      url: `${siteConfig.url}/setup`,
      lastModified: now,
      changeFrequency: "monthly",
      priority: 0.8,
    },
    {
      url: `${siteConfig.url}/setup/instagram`,
      lastModified: now,
      changeFrequency: "monthly",
      priority: 0.7,
    },
    // `/setup/meta-developer` and `/setup/google-drive` are deliberately absent.
    // Both instruct the reader to register their own Meta app / Google Cloud
    // project, and nothing in the product accepts a tenant-supplied App ID and
    // Secret — credentials are deployment-level. Submitting them for indexing
    // routes search traffic into an hour of setup with nowhere to enter the
    // result. They are noindexed at the page level too; this list is the second
    // half of that, not the whole of it. See #802.
    {
      url: `${siteConfig.url}/setup/media-organize`,
      lastModified: now,
      changeFrequency: "monthly",
      priority: 0.7,
    },
    {
      url: `${siteConfig.url}/login`,
      lastModified: now,
      changeFrequency: "monthly",
      priority: 0.5,
    },
    {
      url: `${siteConfig.url}/privacy`,
      lastModified: now,
      changeFrequency: "monthly",
      priority: 0.5,
    },
    {
      url: `${siteConfig.url}/terms`,
      lastModified: now,
      changeFrequency: "monthly",
      priority: 0.5,
    },
  ]
}
