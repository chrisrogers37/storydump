/**
 * Nothing the invitation page loads can fire an analytics event.
 *
 * An invite link's path is the invitation token, and every event carries the
 * page's address: an event fired by any module the join route renders would
 * send the token to a third party. The root layout sends pageviews on every
 * route through `lib/posthog.ts`, which sends nothing from a path under /join
 * (`posthog.test.ts` holds that). Custom events get a second line of defence.
 *
 * So this walks the join route's module graph (its page and route handler,
 * the root layout that wraps them, and everything they import, transitively)
 * and fails when it reaches `lib/analytics.ts`, the module custom events go
 * through, or any module but the pageview sender (`lib/posthog.ts`,
 * `components/analytics/pageviews.tsx`) that loads posthog-js or `lib/posthog.ts`. The marketing home page is walked as the positive
 * control: its FAQ tracks an event, so a walker that could not follow an
 * import fails there first.
 *
 * WHAT THIS CANNOT SEE: a module loaded through a computed path.
 *
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP — the
 * `intent-states-contract` rule. An import this cannot resolve is an error.
 */

import { existsSync, readdirSync, readFileSync, statSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const APP = path.join(SRC, "app");
const ANALYTICS = path.join(SRC, "lib", "analytics.ts");
/** The pageview sender every route loads, and the module it goes through. */
const PAGEVIEWS = [path.join(SRC, "lib", "posthog.ts"), path.join(SRC, "components", "analytics", "pageviews.tsx")];
/** posthog-js itself, or the site's wrapper around it. */
const LOADS_POSTHOG = /["'](?:posthog-js[/"']|@\/lib\/posthog["']|\.\/posthog["'])/;

/** `import … from "x"`, `export … from "x"`, `import "x"` and `import("x")`. */
const IMPORT =
  /(?:import|export)\s[^'"`;]*?from\s*["']([^"']+)["']|import\s*\(\s*["']([^"']+)["']\s*\)|^\s*import\s+["']([^"']+)["']/gm;

/** The source file an import names, or null for a package. */
function resolveImport(from: string, spec: string): string | null {
  let base: string;
  if (spec.startsWith("@/")) base = path.join(SRC, spec.slice(2));
  else if (spec.startsWith(".")) base = path.resolve(path.dirname(from), spec);
  else return null;
  for (const candidate of [
    base,
    `${base}.ts`,
    `${base}.tsx`,
    path.join(base, "index.ts"),
    path.join(base, "index.tsx"),
  ]) {
    if (existsSync(candidate) && statSync(candidate).isFile()) return candidate;
  }
  throw new Error(`cannot resolve ${JSON.stringify(spec)} from ${path.relative(SRC, from)}`);
}

/** Every TypeScript module reachable from `entries`. */
function moduleGraph(entries: string[]): Set<string> {
  const seen = new Set<string>();
  const queue = [...entries];
  while (queue.length > 0) {
    const file = queue.pop()!;
    if (seen.has(file)) continue;
    seen.add(file);
    for (const m of readFileSync(file, "utf8").matchAll(IMPORT)) {
      const target = resolveImport(file, m[1] ?? m[2] ?? m[3]);
      if (target && /\.tsx?$/.test(target)) queue.push(target);
    }
  }
  return seen;
}

function sourcesUnder(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return sourcesUnder(full);
    return /\.tsx?$/.test(entry.name) && !entry.name.includes(".test.") ? [full] : [];
  });
}

/** The modules in a graph that can send an analytics event. */
function eventSenders(graph: Set<string>): string[] {
  return [...graph]
    .filter(
      (file) =>
        file === ANALYTICS ||
        (!PAGEVIEWS.includes(file) && LOADS_POSTHOG.test(readFileSync(file, "utf8"))),
    )
    .map((file) => path.relative(SRC, file));
}

const JOIN = moduleGraph([path.join(APP, "layout.tsx"), ...sourcesUnder(path.join(APP, "join"))]);

describe("the join route", () => {
  it("is walked through the modules it renders", () => {
    const reached = [...JOIN].map((file) => path.relative(SRC, file));
    expect(reached).toEqual(
      expect.arrayContaining([
        "app/join/[token]/page.tsx",
        "components/workspace/accept-invitation.tsx",
        "components/auth/sign-out-button.tsx",
        "lib/bff.ts",
        "lib/posthog.ts",
      ]),
    );
  });

  it("loads nothing that can fire an analytics event", () => {
    expect(eventSenders(JOIN)).toEqual([]);
  });
});

describe("the walker itself", () => {
  it("finds the event a marketing page fires, so a clean result is not a blind one", () => {
    const home = moduleGraph([
      path.join(APP, "layout.tsx"),
      path.join(APP, "(marketing)", "layout.tsx"),
      path.join(APP, "(marketing)", "page.tsx"),
    ]);
    expect(eventSenders(home)).toContain("lib/analytics.ts");
  });
});
