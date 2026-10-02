/**
 * The home page says only what the product does and what Chris approved.
 *
 *   1. None of the old page's unsupported claims comes back: the stats with
 *      no source, "Autopilot", "Unlimited Stories", the paid-tier promise,
 *      "within a week", and media that never leaves Drive.
 *   2. The two lines Chris locked on 2026-10-01 appear word for word: the
 *      price line, and the promise after signup.
 *   3. Every signup button says "Join the waitlist", and the form after
 *      signup offers no Telegram community link (linking a group is setup).
 *   4. No real account appears in the demos: only the placeholders.
 *
 * Checked by source, across the landing and layout components, the blog
 * posts, the use-case pages and the 404 page.
 */

import { readdirSync, readFileSync } from "fs"
import path from "path"
import { fileURLToPath } from "url"
import { describe, expect, it } from "vitest"

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..")
const DIRS = ["components/landing", "components/layout"]
// Marketing pages that carry their own signup call to action, and the copy
// they render.
const ARTICLES = "app/(marketing)/blog/[slug]/_articles"
const USE_CASES = "app/(marketing)/use-cases/[slug]"
function useCaseFiles() {
  return [
    `${USE_CASES}/page.tsx`,
    ...readdirSync(path.join(SRC, USE_CASES, "_content")).map((f) => `${USE_CASES}/_content/${f}`),
    "lib/use-cases.ts",
  ]
}
const FILES = [
  "app/(marketing)/blog/[slug]/page.tsx",
  ...readdirSync(path.join(SRC, ARTICLES)).map((f) => `${ARTICLES}/${f}`),
  ...useCaseFiles(),
  "app/not-found.tsx",
]

const sources = [
  ...DIRS.flatMap((dir) =>
    readdirSync(path.join(SRC, dir))
      .filter((f) => f.endsWith(".tsx") && !f.includes(".test."))
      .map((f) => `${dir}/${f}`)
  ),
  ...FILES,
].map((file) => ({ file, text: readFileSync(path.join(SRC, file), "utf8") }))
const all = sources.map((s) => s.text).join("\n")
const read = (file: string) => readFileSync(path.join(SRC, file), "utf8")

describe("the home page's claims", () => {
  it("reads every file it checks", () => {
    expect(sources.map((s) => s.file)).toEqual(
      expect.arrayContaining([
        "components/landing/hero.tsx",
        "components/landing/waitlist-form.tsx",
        "components/landing/final-cta.tsx",
        "components/layout/header.tsx",
        "app/(marketing)/blog/[slug]/page.tsx",
        "app/(marketing)/blog/[slug]/_articles/automate-instagram-stories.tsx",
        "app/(marketing)/use-cases/[slug]/page.tsx",
        "app/(marketing)/use-cases/[slug]/_content/evergreen.tsx",
        "lib/use-cases.ts",
      ])
    )
  })

  it.each([
    "Autopilot",
    "2,400",
    "5,000",
    "50+",
    "Unlimited",
    "paid tiers",
    "within a week",
    "third-party servers",
    "Never stored",
    "Trusted by",
    "Get Early Access",
    "Early Access",
    "auto-approve",
    "Auto Post",
    "hands-off",
  ])("never says %s", (phrase) => {
    expect(all.toLowerCase()).not.toContain(phrase.toLowerCase())
  })

  it("carries the locked price line wherever there is a signup form", () => {
    for (const file of [
      "components/landing/hero.tsx",
      "components/landing/final-cta.tsx",
      "app/(marketing)/use-cases/[slug]/page.tsx",
    ]) {
      expect(read(file)).toContain("Free during beta · No credit card required")
    }
  })

  it("makes the locked promise after signup, and links nowhere else", () => {
    const form = read("components/landing/waitlist-form.tsx")
    expect(form.replace(/&apos;/g, "’").replace(/\s+/g, " ")).toContain(
      "We’re inviting people in small batches as spots open, and we’ll email you when yours is ready."
    )
    expect(form).not.toMatch(/telegram/i)
    expect(form).not.toMatch(/<a\s|<Link/)
  })

  it("labels every signup button Join the waitlist", () => {
    expect(read("components/landing/waitlist-form.tsx")).toContain("Join the waitlist")
    expect(read("components/layout/header.tsx")).toMatch(/Join\s.*waitlist/)
    expect(read("app/(marketing)/blog/[slug]/page.tsx")).toContain("Join the waitlist")
    expect(read("app/(marketing)/use-cases/[slug]/page.tsx")).toContain("Join the waitlist")
  })

  it("uses only placeholder handles and addresses", () => {
    const handles = all.match(/@[a-z0-9_.]+\.[a-z]+/gi) ?? []
    for (const h of handles) expect(["@example.brand", "@example.com"]).toContain(h)
  })
})

describe("the use-case pages' claims", () => {
  // The Phase 3 content brief's "never on these pages" list: no automation
  // the product doesn't do, no superlatives, no sync speed, no store
  // integration, no competitor, and no counts.
  const pages = useCaseFiles()
    .map(read)
    .join("\n")
    .toLowerCase()

  it.each([
    "autopilot",
    "automatic",
    "auto-post",
    "set and forget",
    "the only",
    "the best",
    "instantly",
    "in minutes",
    "real time",
    "jitter",
    "create a bot",
    "shopify",
    "shoppable",
    "catalog sync",
    "product feed",
    "storrito",
    "buffer",
    "later.com",
    "zapier",
    "make.com",
    "pabbly",
  ])("never says %s", (phrase) => {
    expect(pages).not.toContain(phrase)
  })

  it("says the store is not connected", () => {
    expect(pages).toContain("doesn’t connect to your store")
  })
})
