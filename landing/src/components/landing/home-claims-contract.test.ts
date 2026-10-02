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
 * Checked by source, across every file the marketing pages render from.
 */

import { readdirSync, readFileSync } from "fs"
import path from "path"
import { fileURLToPath } from "url"
import { describe, expect, it } from "vitest"

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..")
const DIRS = ["components/landing", "components/layout"]

const sources = DIRS.flatMap((dir) =>
  readdirSync(path.join(SRC, dir))
    .filter((f) => f.endsWith(".tsx") && !f.includes(".test."))
    .map((f) => ({
      file: `${dir}/${f}`,
      text: readFileSync(path.join(SRC, dir, f), "utf8"),
    }))
)
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
  ])("never says %s", (phrase) => {
    expect(all.toLowerCase()).not.toContain(phrase.toLowerCase())
  })

  it("carries the locked price line in the hero and the closing section", () => {
    for (const file of ["components/landing/hero.tsx", "components/landing/final-cta.tsx"]) {
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
  })

  it("uses only placeholder handles and addresses", () => {
    const handles = all.match(/@[a-z0-9_.]+\.[a-z]+/gi) ?? []
    for (const h of handles) expect(["@example.brand", "@example.com"]).toContain(h)
  })
})
