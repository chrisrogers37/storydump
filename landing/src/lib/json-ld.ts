import { siteConfig } from "@/config/site"
import type { BlogPost } from "@/lib/blog"
import { ogImageUrl } from "@/lib/seo"

/**
 * schema.org builders for the JSON-LD the public pages carry. Every field is
 * read from the site's own config or content: none is invented, and there are
 * deliberately no ratings, reviews or counts, because the site has none.
 */

const absolute = (path: string) => new URL(path, siteConfig.url).toString()

/** A trail of `{ name, path }` crumbs, home first. */
export function breadcrumbList(crumbs: { name: string; path: string }[]) {
  return {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: crumbs.map((crumb, i) => ({
      "@type": "ListItem",
      position: i + 1,
      name: crumb.name,
      item: absolute(crumb.path),
    })),
  }
}

export function blogPosting(post: BlogPost) {
  const url = absolute(`/blog/${post.slug}`)
  return {
    "@context": "https://schema.org",
    "@type": "BlogPosting",
    headline: post.title,
    description: post.description,
    url,
    mainEntityOfPage: url,
    image: ogImageUrl(post.title, post.description),
    datePublished: post.date,
    dateModified: post.updated ?? post.date,
    inLanguage: "en",
    author: {
      "@type": "Person",
      name: siteConfig.author.name,
      url: siteConfig.contact.portfolio,
    },
    publisher: {
      "@type": "Organization",
      name: siteConfig.name,
      url: siteConfig.url,
    },
  }
}
