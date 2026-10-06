/**
 * The indexing rules, held together: canonicals, noindex, robots and the
 * sitemap must agree, or a search engine is told two things at once.
 *
 *   1. Every sitemap URL is a page that names itself as canonical and og:url. The
 *      marketing pages are imported and their metadata read, so this checks
 *      the value Next will render, not a string in the source.
 *   2. No page inherits "/" as its canonical: the root layout sets none, and
 *      every page under `app/` either declares its own canonical or is
 *      noindex (itself or through a layout above it).
 *   3. Every page that is not noindex is in the sitemap, and nothing noindex
 *      is: the two lists cannot drift.
 *   4. robots.txt keeps crawlers out of `/join/` (invitation paths are bearer
 *      tokens) and `/api/`, and disallows no sitemap URL.
 *   5. No sitemap entry is stamped with the request time.
 *
 * The app, auth and invite pages are checked by source, not imported: they
 * pull in server-only session modules that cannot load here.
 *
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP.
 */

import { readdirSync, readFileSync, statSync } from "fs"
import path from "path"
import { fileURLToPath } from "url"
import type { Metadata } from "next"
import { describe, expect, it } from "vitest"
import robots, { disallowedPaths } from "@/app/robots"
import sitemap from "@/app/sitemap"
import { siteConfig } from "@/config/site"
import { posts } from "@/lib/blog"
import { useCases } from "@/lib/use-cases"
import { blogPosting, breadcrumbList } from "@/lib/json-ld"
import { homeSocial, ogImageUrl } from "@/lib/seo"

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const APP = path.join(SRC, "app")

/** Every page.tsx under app/, as a path relative to app/. */
function pageFiles(dir = APP): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name)
    if (statSync(full).isDirectory()) return pageFiles(full)
    return name === "page.tsx" ? [path.relative(APP, full)] : []
  })
}

/** The URL path a page file serves: route groups dropped, segments kept. */
function routeOf(file: string): string {
  const segments = path
    .dirname(file)
    .split(path.sep)
    .filter((s) => s !== "." && !(s.startsWith("(") && s.endsWith(")")))
  return "/" + segments.join("/")
}

/** The page's own source plus every layout above it, nearest first. */
function sourcesFor(file: string): string[] {
  const out = [readFileSync(path.join(APP, file), "utf8")]
  let dir = path.dirname(file)
  while (dir !== ".") {
    const layout = path.join(APP, dir, "layout.tsx")
    try {
      out.push(readFileSync(layout, "utf8"))
    } catch {
      // no layout at this level
    }
    dir = path.dirname(dir)
  }
  return out
}

const isNoindex = (file: string) =>
  sourcesFor(file).some((src) => src.includes("...noindexMetadata"))

const sitemapPaths = sitemap().map(
  (entry) => entry.url.slice(siteConfig.url.length) || "/"
)

/** The rendered metadata of the marketing page serving `urlPath`. */
async function metadataFor(urlPath: string): Promise<Metadata> {
  const blog = urlPath.match(/^\/blog\/([^/]+)$/)
  if (blog) {
    const mod = await import("@/app/(marketing)/blog/[slug]/page")
    return mod.generateMetadata({ params: Promise.resolve({ slug: blog[1] }) })
  }
  const useCase = urlPath.match(/^\/use-cases\/([^/]+)$/)
  if (useCase) {
    const mod = await import("@/app/(marketing)/use-cases/[slug]/page")
    return mod.generateMetadata({ params: Promise.resolve({ slug: useCase[1] }) })
  }
  const file = pageFiles().find(
    (f) => f.startsWith("(marketing)") && routeOf(f) === urlPath
  )
  if (!file) throw new Error(`no marketing page serves ${urlPath}`)
  const mod = await import(/* @vite-ignore */ path.join(APP, file))
  return mod.metadata as Metadata
}

describe("canonicals", () => {
  it.each(sitemapPaths)("%s names itself as canonical", async (urlPath) => {
    const metadata = await metadataFor(urlPath)
    expect(metadata.alternates?.canonical).toBe(urlPath)
    expect(metadata.openGraph?.url).toBe(urlPath)
  })

  it("the default social card names no og:url for pages to inherit", () => {
    expect(homeSocial.openGraph).not.toHaveProperty("url")
  })

  it("the root layout sets no canonical for pages to inherit", () => {
    const layout = readFileSync(path.join(APP, "layout.tsx"), "utf8")
    expect(layout).not.toMatch(/canonical/)
  })

  it.each(pageFiles())(
    "%s declares its own canonical or is noindex",
    (file) => {
      const own = readFileSync(path.join(APP, file), "utf8")
      const declaresCanonical = /canonical:|pageMetadata\(/.test(own)
      expect(declaresCanonical || isNoindex(file)).toBe(true)
    }
  )
})

describe("noindex and the sitemap agree", () => {
  const indexable = pageFiles()
    .filter((file) => !isNoindex(file))
    .map(routeOf)

  it("every indexable page is in the sitemap", () => {
    for (const route of indexable) {
      if (route === "/blog/[slug]") {
        for (const post of posts) {
          expect(sitemapPaths).toContain(`/blog/${post.slug}`)
        }
      } else if (route === "/use-cases/[slug]") {
        for (const useCase of useCases) {
          expect(sitemapPaths).toContain(`/use-cases/${useCase.slug}`)
        }
      } else {
        expect(sitemapPaths).toContain(route)
      }
    }
  })

  it("no noindex page is in the sitemap", () => {
    const hidden = pageFiles().filter(isNoindex).map(routeOf)
    expect(hidden).toEqual(
      expect.arrayContaining([
        "/login",
        "/welcome",
        "/workspaces",
        "/join/[token]",
        "/auth/error",
        "/dashboard",
      ])
    )
    for (const route of hidden) expect(sitemapPaths).not.toContain(route)
  })

  it("lists every URL once", () => {
    expect(new Set(sitemapPaths).size).toBe(sitemapPaths.length)
  })
})

describe("robots", () => {
  const rules = robots().rules
  const rule = Array.isArray(rules) ? rules[0] : rules

  it("blocks invitation paths and the API", () => {
    expect(rule.disallow).toEqual(expect.arrayContaining(["/join/", "/api/"]))
  })

  it("disallows no URL the sitemap lists", () => {
    for (const urlPath of sitemapPaths) {
      for (const prefix of disallowedPaths) {
        expect(urlPath.startsWith(prefix)).toBe(false)
      }
    }
  })

  it("points at the sitemap", () => {
    expect(robots().sitemap).toBe(`${siteConfig.url}/sitemap.xml`)
  })
})

describe("sitemap dates", () => {
  it("are fixed calendar dates, not the request time", () => {
    for (const entry of sitemap()) {
      expect(entry.lastModified).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    }
  })
})

describe("JSON-LD", () => {
  it("a blog post's BlogPosting carries its own dates and no invented fields", () => {
    for (const post of posts) {
      const data = blogPosting(post)
      expect(data.datePublished).toBe(post.date)
      expect(data.url).toBe(`${siteConfig.url}/blog/${post.slug}`)
      for (const invented of ["aggregateRating", "review", "interactionStatistic"]) {
        expect(data).not.toHaveProperty(invented)
      }
    }
  })

  it("breadcrumbs are absolute and numbered from 1", () => {
    const data = breadcrumbList([
      { name: "Home", path: "/" },
      { name: "Blog", path: "/blog" },
    ])
    expect(data.itemListElement.map((i) => [i.position, i.item])).toEqual([
      [1, siteConfig.url], // the canonical form: no trailing slash
      [2, `${siteConfig.url}/blog`],
    ])
  })
})

describe("social card", () => {
  const subtitleOf = (subtitle: string) =>
    new URL(ogImageUrl("Title", subtitle)).searchParams.get("subtitle")

  it("passes a short subtitle through unchanged", () => {
    expect(subtitleOf("One tap posts it.")).toBe("One tap posts it.")
  })

  it("shortens a long subtitle at a word break, never mid-word", () => {
    const long = "word ".repeat(40).trim()
    const shown = subtitleOf(long)!
    expect(shown.length).toBeLessThanOrEqual(100)
    expect(shown).toMatch(/ word…$/)
  })
})

describe("snippet lengths", () => {
  // The root layout's template adds " | Storydump" (12 characters); Google
  // shows about 60 characters of a title and 155 to 160 of a description.
  const titled = [
    ...posts.map((p) => ({ name: p.slug, title: p.seoTitle ?? p.title, description: p.description })),
    ...useCases.map((u) => ({ name: u.slug, title: u.seoTitle, description: u.description })),
  ]

  it("no two pages share a title", () => {
    const titles = titled.map((t) => t.title)
    expect(new Set(titles).size).toBe(titles.length)
  })

  it.each(titled)("$name fits a search result", ({ title, description }) => {
    expect(`${title} | Storydump`.length).toBeLessThanOrEqual(60)
    expect(description.length).toBeLessThanOrEqual(160)
  })
})
