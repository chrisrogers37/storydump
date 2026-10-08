/**
 * The sample workspace cannot reach the API (#1480).
 *
 * "Nothing is stored and nothing can post" is a claim about structure, so it
 * is pinned as one. No file the sample owns, nor the `QueueView` it shares
 * with the real Queue:
 *
 *   - imports the API door (`lib/bff`), the server's clients
 *     (`lib/workspaces`, `lib/target-api`, `lib/page-guards`), the database
 *     (`lib/db`), or the request's headers and cookies (`next/headers`);
 *   - calls `fetch(`, or touches `localStorage`, `sessionStorage` or
 *     `document.cookie`.
 *
 * Imports are RESOLVED TO FILES before they are compared, so `./bff` and
 * `@/lib/bff` are one ban.
 *
 * WHAT THIS CANNOT SEE. It reads the sample's own imports, not theirs. The
 * dashboard derivations the sample reuses reach `lib/bff.ts` without calling
 * it, because those modules keep pure helpers beside their API calls:
 * `dashboard-payloads` → `category-mix` → `bff`; `conditions` → `drive` →
 * `bff`; `conditions` → `destination` → `start-grant` → `bff`. So what this
 * pins is that no code of the sample's own imports, holds or calls the API
 * door. That the dashboard components it reuses call none is theirs to keep.
 *
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP.
 */

import { existsSync, readdirSync, readFileSync, statSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";

const SRC = path.dirname(fileURLToPath(import.meta.url));

/** What the sample owns, and the one shared file its Queue draws with. */
const OWNED_DIRS = [
  path.join("app", "(demo)"),
  path.join("components", "demo"),
  path.join("lib", "demo"),
];
const SHARED_FILES = [
  path.join("components", "dashboard", "queue", "queue-view.tsx"),
  // QueueView draws Reschedule… with it, so the sample imports it too.
  path.join("components", "dashboard", "queue", "reschedule-dialog.tsx"),
  // QueueView draws an item's link with it, so the sample imports it too.
  path.join("components", "dashboard", "item-link.tsx"),
  // QueueView draws a row's thumbnail with these, so the sample imports them
  // too; it passes no workspace, so it never asks for a thumbnail.
  path.join("components", "dashboard", "media", "media-thumbnail.tsx"),
  path.join("lib", "thumbnails.ts"),
];

const BANNED_MODULES = ["bff", "workspaces", "page-guards", "target-api", "db"].map(
  (name) => path.join("lib", `${name}.ts`),
);
const BANNED_PACKAGES = ["next/headers"];
const BANNED_TEXT: [string, RegExp][] = [
  ["fetch(", /\bfetch\s*\(/],
  ["localStorage", /\blocalStorage\b/],
  ["sessionStorage", /\bsessionStorage\b/],
  ["document.cookie", /\bdocument\.cookie\b/],
];

const IMPORT =
  /(?:import|export)\s[^'"`;]*?from\s*["']([^"']+)["']|import\s*\(\s*["']([^"']+)["']\s*\)|^\s*import\s+["']([^"']+)["']/gm;

/** Every source file under `dir`, relative to `src/`; tests are not the sample. */
function sourcesUnder(dir: string): string[] {
  return readdirSync(path.join(SRC, dir), { withFileTypes: true }).flatMap((entry) => {
    const rel = path.join(dir, entry.name);
    if (entry.isDirectory()) return sourcesUnder(rel);
    return /\.tsx?$/.test(entry.name) && !entry.name.includes(".test.") ? [rel] : [];
  });
}

function read(file: string): string {
  try {
    return readFileSync(path.join(SRC, file), "utf8");
  } catch (err) {
    throw new Error(`cannot read ${file}: ${err}`);
  }
}

/** A local import as the file it names, relative to `src/`; a package as its name. */
function resolveImport(from: string, spec: string): string {
  let base: string;
  if (spec.startsWith("@/")) base = path.join(SRC, spec.slice(2));
  else if (spec.startsWith(".")) base = path.resolve(path.dirname(path.join(SRC, from)), spec);
  else return spec;
  for (const candidate of [
    base,
    `${base}.ts`,
    `${base}.tsx`,
    path.join(base, "index.ts"),
    path.join(base, "index.tsx"),
  ]) {
    if (existsSync(candidate) && statSync(candidate).isFile()) {
      return path.relative(SRC, candidate);
    }
  }
  throw new Error(`cannot resolve ${JSON.stringify(spec)} from ${from}`);
}

/** Each banned import in `source`, as `<file>: <what it reaches>`. */
function bannedImports(file: string, source: string): string[] {
  return [...source.matchAll(IMPORT)]
    .map((m) => resolveImport(file, m[1] ?? m[2] ?? m[3]))
    .filter((target) => BANNED_MODULES.includes(target) || BANNED_PACKAGES.includes(target))
    .map((target) => `${file}: ${target}`);
}

/** Each banned call or global in `source`, as `<file>: <what>`. */
function bannedText(file: string, source: string): string[] {
  return BANNED_TEXT.filter(([, pattern]) => pattern.test(source)).map(
    ([name]) => `${file}: ${name}`,
  );
}

const FILES = [...OWNED_DIRS.flatMap(sourcesUnder), ...SHARED_FILES];

describe("the sample workspace", () => {
  it("is found where it lives", () => {
    // An empty scan passes every check below, so first prove it is not empty.
    expect(FILES).toEqual(
      expect.arrayContaining([
        path.join("app", "(demo)", "demo", "layout.tsx"),
        path.join("app", "(demo)", "demo", "queue", "page.tsx"),
        path.join("components", "demo", "demo-provider.tsx"),
        path.join("lib", "demo", "fixtures.ts"),
        path.join("components", "dashboard", "queue", "queue-view.tsx"),
      ]),
    );
  });

  it("imports nothing that reaches the API, the session or the database", () => {
    expect(FILES.flatMap((file) => bannedImports(file, read(file)))).toEqual([]);
  });

  it("fetches nothing and touches no storage or cookie", () => {
    expect(FILES.flatMap((file) => bannedText(file, read(file)))).toEqual([]);
  });
});

describe("the detector", () => {
  it("sees the real Queue's import of the API door", () => {
    // The positive control, on the real tree: the split left the Queue's one
    // network call in QueueList, so the detector must find it there.
    const file = path.join("components", "dashboard", "queue", "queue-list.tsx");
    expect(bannedImports(file, read(file))).toEqual([`${file}: ${path.join("lib", "bff.ts")}`]);
  });

  it("reads a relative import and an alias as one file", () => {
    const from = path.join("lib", "category-mix.ts");
    const bff = `${from}: ${path.join("lib", "bff.ts")}`;
    expect(bannedImports(from, 'import { callBff } from "./bff";')).toEqual([bff]);
    expect(bannedImports(from, 'import { callBff } from "@/lib/bff";')).toEqual([bff]);
    expect(bannedImports(from, 'import { cookies } from "next/headers";')).toEqual([
      `${from}: next/headers`,
    ]);
    expect(bannedImports(from, 'import { cn } from "@/lib/utils";')).toEqual([]);
  });

  it("sees a fetch, storage and a cookie", () => {
    expect(
      bannedText(
        "x.ts",
        'await fetch("/api"); localStorage.getItem("k"); window.sessionStorage; document.cookie = "a";',
      ),
    ).toEqual(["x.ts: fetch(", "x.ts: localStorage", "x.ts: sessionStorage", "x.ts: document.cookie"]);
  });
});
