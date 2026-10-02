import type { MetadataRoute } from "next"
import { posts } from "@/lib/blog"
import { absoluteUrl, indexablePages } from "@/lib/seo"
import { pathForUseCase, useCases } from "@/lib/use-cases"

/**
 * Every indexable page with the date its content last changed: the static
 * pages from `indexablePages`, the posts from `lib/blog.ts` and the use cases
 * from `lib/use-cases.ts`. No entry is
 * stamped with the request time, which told crawlers every page changed on
 * every fetch.
 */
export default function sitemap(): MetadataRoute.Sitemap {
  return [
    ...indexablePages.map((page) => ({
      url: absoluteUrl(page.path),
      lastModified: page.lastModified,
      changeFrequency: page.changeFrequency,
      priority: page.priority,
    })),
    ...posts.map((post) => ({
      url: absoluteUrl(`/blog/${post.slug}`),
      lastModified: post.updated ?? post.date,
      changeFrequency: "monthly" as const,
      priority: 0.7,
    })),
    ...useCases.map((useCase) => ({
      url: absoluteUrl(pathForUseCase(useCase.slug)),
      lastModified: useCase.lastModified,
      changeFrequency: "monthly" as const,
      priority: 0.7,
    })),
  ]
}
