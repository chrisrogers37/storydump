/**
 * Sign-out is never a link, ANYWHERE in this tier (#1139 follow-up).
 *
 * #1142 fixed the route — `GET` is gone, so a prefetch now gets a 405 instead
 * of revoking the session — and collapsed the two correct call sites into one
 * `SignOutButton`. It did not fix `join/[token]`, which still rendered a
 * `<Link href="/api/auth/logout">`: no longer a security hole, but a sign-out
 * control that cannot sign anyone out.
 *
 * **This asserts the RULE over the whole tree, not the one file.** Pinning
 * `join/[token]` would say nothing about the next page that offers sign-out,
 * and "one more caller" was itself a finding somebody had to go looking for.
 * The property is cheap to state and covers every file at once: nothing in
 * `src` may point a link at the sign-out route.
 *
 * WHY A LINK IS THE WRONG ELEMENT, restated so the rule is not cargo. Sign-out
 * revokes `session_tokens.revoked_at`, so it MUTATES. Anything that
 * speculatively fetches a URL will perform it with no person involved —
 * Next's own `<Link>` prefetch did, and crawlers, link previewers, chat
 * unfurlers and email clients all would. A `<button>` that POSTs cannot be
 * fetched speculatively.
 *
 * WHAT THIS CANNOT SEE. It matches text, so a dynamically built href
 * (`href={someVar}`) is invisible to it. Those were enumerated by hand when
 * this landed — two in the tier, `/blog/${post.slug}` and `EmptyState`'s
 * `action.href`, whose only caller passes `/dashboard/settings` — and neither
 * can resolve to the sign-out route. That enumeration is a measurement with a
 * date on it, not a guarantee; a future dynamic href would need re-checking by
 * hand, and this test will not do it for you.
 *
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP — the
 * `intent-states-contract` rule. A contract test that quietly stops finding
 * files is indistinguishable from one that passes.
 */

import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";

const SELF = fileURLToPath(import.meta.url);
const SRC = path.resolve(path.dirname(SELF), "..");

/**
 * Every `.ts`/`.tsx` file under `src`, except THIS one.
 *
 * The single exclusion is exact-path, not a pattern: this file quotes the
 * offending form in its docstring and again as a positive control, so it
 * matches itself. Excluding `*.test.*` instead would have been the easy
 * spelling and would carve out every other test file with it — a hole the
 * width of a category to solve a problem the width of one file.
 */
function sourceFiles(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (/\.tsx?$/.test(entry.name) && full !== SELF) out.push(full);
    }
  };
  walk(SRC);
  return out;
}

describe("sign-out is never a link", () => {
  it("finds the tier to search, so an empty result cannot pass as a clean one", () => {
    // The #1112 shape: a search that stopped matching would report zero
    // offenders, which is the same output as compliance.
    expect(sourceFiles().length).toBeGreaterThan(50);
  });

  it("has no <Link> or <a> pointing at the sign-out route", () => {
    const offenders = sourceFiles().filter((file) => {
      const src = readFileSync(file, "utf8");
      // A link element whose href names the route, allowing attributes and
      // newlines between the tag and the href.
      // No `s` flag: there is no `.` in this pattern, and `[^>]*` already
      // spans newlines, so dotAll buys nothing and needs an es2018 target.
      return /<(?:Link|a)\b[^>]*href\s*=\s*["'`][^"'`]*\/api\/auth\/logout/.test(
        src,
      );
    });
    expect(offenders.map((f) => path.relative(SRC, f))).toEqual([]);
  });

  it("catches the form it is written to catch", () => {
    // The positive control. Without it, a pattern that matches NOTHING — a
    // typo in the route, a broken regex — passes exactly like a clean tree,
    // which is the failure this whole file exists to rule out.
    const REGRESSION = '<Link href="/api/auth/logout">Sign out</Link>';
    expect(
      /<(?:Link|a)\b[^>]*href\s*=\s*["'`][^"'`]*\/api\/auth\/logout/.test(
        REGRESSION,
      ),
    ).toBe(true);
  });
});
