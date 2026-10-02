import type { Metadata } from "next"
import { siteConfig } from "@/config/site"

/**
 * The public pages search engines may index, and the date each one's content
 * last changed. The sitemap is built from this list, and
 * `seo-contract.test.ts` holds every entry to a page that declares itself as
 * its canonical. A page missing from here is not in the sitemap.
 *
 * `lastModified` is the date the page's visible content last changed. Bump it
 * by hand in the same commit as a content change. The repository's history
 * starts on 2026-09-21, so that is the oldest date it can prove for a page
 * nobody has touched since.
 */
export const indexablePages = [
  { path: "/", lastModified: "2026-10-02", changeFrequency: "weekly", priority: 1 },
  { path: "/blog", lastModified: "2026-09-21", changeFrequency: "weekly", priority: 0.8 },
  { path: "/setup", lastModified: "2026-09-21", changeFrequency: "monthly", priority: 0.8 },
  { path: "/setup/instagram", lastModified: "2026-09-21", changeFrequency: "monthly", priority: 0.7 },
  { path: "/setup/media-organize", lastModified: "2026-09-21", changeFrequency: "monthly", priority: 0.7 },
  { path: "/privacy", lastModified: "2026-09-30", changeFrequency: "monthly", priority: 0.5 },
  { path: "/terms", lastModified: "2026-09-30", changeFrequency: "monthly", priority: 0.5 },
] as const

/**
 * Routes that must never be indexed: the signed-in app, sign-in and its error
 * page, first run, and invitations, whose path IS a bearer token. Each sets
 * `noindexMetadata`, and `app/robots.ts` disallows crawling the ones a
 * crawler has no reason to fetch. `/login` stays crawlable so a crawler can
 * read its noindex.
 */
export const noindexMetadata = {
  robots: { index: false, follow: false },
} satisfies Metadata

const homeTitle = `${siteConfig.name} — Instagram Stories from Google Drive, on tap`

/**
 * The home page's social card, which the root layout also sets as every
 * page's default. It carries no `url`: the home page adds `url: "/"` itself,
 * and every indexable page sets its own through `pageMetadata`.
 */
export const homeSocial = {
  openGraph: {
    title: homeTitle,
    description: siteConfig.description,
    siteName: siteConfig.name,
    type: "website",
    locale: "en_US",
    images: [
      { url: "/og-image.png", width: 1200, height: 630, alt: homeTitle },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: homeTitle,
    description: siteConfig.description,
    images: ["/og-image.png"],
  },
} satisfies Metadata

/**
 * A path on this site as a full URL. The home page is the bare origin, with no
 * trailing slash, which is how Next renders its canonical.
 */
export function absoluteUrl(path: string): string {
  return path === "/" ? siteConfig.url : new URL(path, siteConfig.url).toString()
}

/** Shortens `text` to at most `max` characters at a word break, adding "…". */
function truncateAtWord(text: string, max: number): string {
  if (text.length <= max) return text
  const cut = text.slice(0, max - 1)
  const space = cut.lastIndexOf(" ")
  return `${(space > 0 ? cut.slice(0, space) : cut).replace(/[\s,;:.—-]+$/, "")}…`
}

/** The per-page social card from the `/og-image.png` route. */
export function ogImageUrl(title: string, subtitle: string): string {
  const url = new URL(absoluteUrl("/og-image.png"))
  url.searchParams.set("title", title)
  url.searchParams.set("subtitle", truncateAtWord(subtitle, 100))
  return url.toString()
}

interface PageMetadataInput {
  /** The page's own title; the root layout's template adds " | Storydump". */
  title: string
  description: string
  /** The page's path, e.g. "/blog". It becomes the canonical and `og:url`. */
  path: string
  /** "article" for a blog post; every other page is a "website". */
  type?: "website" | "article"
  /** A blog post's dates, for `article:published_time` and `article:modified_time`. */
  publishedTime?: string
  modifiedTime?: string
  /** The social card's own lines, when the title and description don't fit it. */
  ogTitle?: string
  ogSubtitle?: string
}

/**
 * Title, description, self-canonical, Open Graph and Twitter card for one
 * public page. Every indexable page builds its metadata here, so none can
 * fall back to the root layout's values and claim to be the home page.
 */
export function pageMetadata({
  title,
  description,
  path,
  type = "website",
  publishedTime,
  modifiedTime,
  ogTitle = title,
  ogSubtitle = description,
}: PageMetadataInput): Metadata {
  const socialTitle = `${title} | ${siteConfig.name}`
  const image = ogImageUrl(ogTitle, ogSubtitle)
  return {
    title,
    description,
    alternates: { canonical: path },
    openGraph: {
      title: socialTitle,
      description,
      url: path,
      siteName: siteConfig.name,
      locale: "en_US",
      images: [{ url: image, width: 1200, height: 630, alt: ogTitle }],
      ...(type === "article"
        ? { type: "article" as const, publishedTime, modifiedTime }
        : { type: "website" as const }),
    },
    twitter: {
      card: "summary_large_image",
      title: socialTitle,
      description,
      images: [image],
    },
  }
}
