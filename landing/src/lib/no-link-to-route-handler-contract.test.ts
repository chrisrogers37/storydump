/**
 * No `<Link>` points at a route handler outside `/api`.
 *
 * `next/link` prefetches its target as a Server Components request, and a
 * route handler answers that request by RUNNING. `/join/[token]/start` set the
 * invite cookie on every view of the invitation page, before anyone clicked,
 * and then redirected the prefetch to the API's sign-in: a cross-origin fetch
 * the browser refuses, to CORS and to the Content-Security-Policy's
 * `connect-src`. A route handler is reached by navigating, so it takes a plain
 * `<a>`, as `GoogleLoginButton` does. (`/api` routes are fetched rather than
 * linked; `signout-never-a-link-contract.test.ts` holds the one that mutates.)
 *
 * The site's own `next/link` wrappers (`TextLink`, `TrackedLink`,
 * `MaybeTrackedLink`) prefetch the same way, so they count as `<Link>` here.
 *
 * WHAT THIS CANNOT SEE: an href held in a variable (`href={item.href}`).
 * A template literal is read, with each `${…}` standing for one segment.
 *
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP — the
 * `intent-states-contract` rule.
 */

import { readdirSync, readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";

const SELF = fileURLToPath(import.meta.url);
const SRC = path.resolve(path.dirname(SELF), "..");
const APP = path.join(SRC, "app");

function walk(dir: string, keep: (file: string) => boolean): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(full, keep));
    else if (keep(full)) out.push(full);
  }
  return out;
}

/** A pattern for the URL of every route handler outside `app/api`. */
function handlerPatterns(): RegExp[] {
  return walk(APP, (f) => /\/route\.tsx?$/.test(f))
    .map((f) => path.relative(APP, path.dirname(f)).split(path.sep))
    .filter((segments) => segments[0] !== "api")
    .map((segments) => {
      const parts = segments
        .filter((s) => !/^\(.*\)$/.test(s))
        .map((s) => (s.startsWith("[...") ? ".+" : s.startsWith("[") ? "[^/]+" : s.replace(/\./g, "\\.")));
      return new RegExp(`^/${parts.join("/")}$`);
    });
}

/** The literal hrefs of every `<Link>` (or wrapper) in a source, `${…}` read as one segment. */
function linkHrefs(source: string): string[] {
  const hrefs: string[] = [];
  const link = /<(?:Link|TextLink|TrackedLink|MaybeTrackedLink)\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|\{\s*`([^`]*)`\s*\}|\{\s*"([^"]*)"\s*\})/g;
  for (const m of source.matchAll(link)) {
    const href = m[1] ?? m[2] ?? m[4] ?? m[3].replace(/\$\{[^}]*\}/g, "x");
    hrefs.push(href.split(/[?#]/)[0]);
  }
  return hrefs;
}

function offenders(source: string, patterns: RegExp[]): string[] {
  return linkHrefs(source).filter((href) => patterns.some((p) => p.test(href)));
}

function sources(): { file: string; text: string }[] {
  return walk(SRC, (f) => /\.tsx$/.test(f) && f !== SELF).map((file) => {
    try {
      return { file: path.relative(SRC, file), text: readFileSync(file, "utf8") };
    } catch (err) {
      throw new Error(`cannot read ${file}: ${err}`);
    }
  });
}

describe("no <Link> points at a route handler", () => {
  it("finds the handlers and the links, so an empty search cannot pass as a clean one", () => {
    const patterns = handlerPatterns().map(String);
    expect(patterns).toContain(String(/^\/join\/[^/]+\/start$/));
    const literal = sources().flatMap(({ text }) => linkHrefs(text));
    expect(literal).toEqual(expect.arrayContaining(["/", "/login", "/dashboard"]));
  });

  it("holds across the tree", () => {
    const patterns = handlerPatterns();
    const found = sources().flatMap(({ file, text }) => offenders(text, patterns).map((h) => `${file}: ${h}`));
    expect(found).toEqual([]);
  });

  it("reports a link to one, whichever way its href is written", () => {
    const patterns = handlerPatterns();
    expect(offenders("<Link\n  href={`/join/${encodeURIComponent(token)}/start`}\n>", patterns)).toHaveLength(1);
    expect(offenders('<Link href="/og-image.png">', patterns)).toHaveLength(1);
    expect(offenders('<TrackedLink href="/og-image.png" track={t}>', patterns)).toHaveLength(1);
    expect(offenders('<TextLink href="/og-image.png">', patterns)).toHaveLength(1);
    expect(offenders('<a href={`/join/${token}/start`}>', patterns)).toEqual([]);
    expect(offenders('<Link href="/join/abc">', patterns)).toEqual([]);
  });
});
