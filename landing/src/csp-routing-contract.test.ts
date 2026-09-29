/**
 * Every page gets exactly one Content-Security-Policy, from exactly one place.
 *
 * A prerendered page takes the static policy from `next.config.ts` and never
 * runs middleware, so it stays a CDN hit. A page rendered per request runs
 * middleware for its nonce policy and takes nothing from the config. A page
 * given both would be held to both at once; a page given neither would have
 * no policy at all. The two lists live in different files and must stay exact
 * complements, which is what this holds, through Next's own matching.
 *
 * The pages come from the tree, so a new page is checked without being listed
 * here. Which pages are prerendered comes from the build (`next build` prints
 * it) and is written below by hand: that is the one fact a unit test cannot
 * derive.
 */

import { readdirSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import {
  unstable_doesMiddlewareMatch,
  unstable_getResponseFromNextConfig,
} from "next/experimental/testing/server";
import { describe, expect, it } from "vitest";
import nextConfig from "../next.config";
import { staticPagePolicy } from "./lib/csp";
import { config as middlewareConfig } from "./middleware";

const APP = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "app");
const BASE = "https://app.example.test";

/** The pages `next build` renders per request. Every other page is prerendered. */
const PER_REQUEST = [
  "/auth/error",
  "/dashboard",
  "/dashboard/analytics",
  "/dashboard/media",
  "/dashboard/media/calendar",
  "/dashboard/queue",
  "/dashboard/settings",
  "/dashboard/sample/sample",
  "/join/sample",
  "/welcome",
  "/workspaces",
];

/** A path for every page file: route groups dropped, each dynamic segment filled in. */
function pagePaths(): string[] {
  const out: string[] = [];
  const walk = (dir: string, segments: string[]) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (entry.isDirectory()) {
        const name = entry.name;
        const next = /^\(.*\)$/.test(name)
          ? segments
          : [...segments, name.startsWith("[...") ? "sample/sample" : name.startsWith("[") ? "sample" : name];
        walk(path.join(dir, name), next);
      } else if (/^page\.(tsx?|mdx)$/.test(entry.name)) {
        out.push("/" + segments.join("/"));
      }
    }
  };
  walk(APP, []);
  return out.sort();
}

async function policySources(pathname: string) {
  const url = `${BASE}${pathname}`;
  const fromConfig = (await unstable_getResponseFromNextConfig({ url, nextConfig })).headers;
  return {
    config: fromConfig.get("content-security-policy"),
    middleware: unstable_doesMiddlewareMatch({ config: middlewareConfig, url, nextConfig }),
    headers: fromConfig,
  };
}

describe("the policy routing", () => {
  it("finds the pages, so an empty tree cannot pass as a clean one", () => {
    const pages = pagePaths();
    expect(pages.length).toBeGreaterThan(15);
    for (const known of ["/", "/login", "/join/sample", "/dashboard/queue"]) {
      expect(pages).toContain(known);
    }
  });

  it("lists only pages that exist", () => {
    expect(PER_REQUEST.filter((p) => !pagePaths().includes(p))).toEqual([]);
  });

  it("gives every page exactly one policy source", async () => {
    const wrong: string[] = [];
    for (const page of [...pagePaths(), "/no-such-page"]) {
      const { config, middleware } = await policySources(page);
      if ((config !== null) === middleware) {
        wrong.push(`${page}: config=${config !== null} middleware=${middleware}`);
      }
    }
    expect(wrong).toEqual([]);
  });

  it("runs middleware on exactly the pages rendered per request", async () => {
    const matched: string[] = [];
    for (const page of pagePaths()) {
      if ((await policySources(page)).middleware) matched.push(page);
    }
    expect(matched.sort()).toEqual([...PER_REQUEST].sort());
  });

  it("gives a prerendered page the static policy", async () => {
    const { config } = await policySources("/login");
    expect(config).toBe(staticPagePolicy());
  });

  it("keeps the API outside middleware", async () => {
    expect((await policySources("/api/workspaces")).middleware).toBe(false);
  });
});

describe("the other response headers", () => {
  it.each(["/", "/login", "/dashboard/queue", "/join/sample", "/api/workspaces"])(
    "reach %s",
    async (page) => {
      const { headers } = await policySources(page);
      for (const name of [
        "strict-transport-security",
        "referrer-policy",
        "permissions-policy",
        "x-frame-options",
        "x-content-type-options",
      ]) {
        expect(headers.get(name), name).not.toBeNull();
      }
    },
  );
});
