/**
 * The owner area's layout (#1723): anyone who is not a signed-in operator
 * gets the site's ordinary 404, never a redirect, and an operator gets the
 * rail, built from `OPS_PAGES`, with a way back to the dashboard.
 *
 * The layout, an async server component, is called directly with the session
 * stubbed and rendered to a string (`environment: "node"`). `notFound` and
 * `redirect` throw what they were asked, so a 404 cannot pass as a redirect.
 */

import { renderToString } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { OpsPage } from "@/lib/ops-pages";
import { SessionUnavailableError, type SessionUser } from "@/lib/session";

const world = vi.hoisted(() => ({
  session: null as unknown,
  pages: [{ href: "/ops/waitlist", label: "Waitlist" }] as OpsPage[],
}));

vi.mock("@/lib/session", async (actual) => ({
  ...(await actual<typeof import("@/lib/session")>()),
  getSession: async () => {
    if (world.session instanceof Error) throw world.session;
    return world.session;
  },
}));
vi.mock("@/lib/ops-pages", () => ({
  get OPS_PAGES() {
    return world.pages;
  },
}));
vi.mock("next/navigation", async (original) => ({
  ...(await original<typeof import("next/navigation")>()),
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
  redirect: (url: string) => {
    throw new Error(`NEXT_REDIRECT ${url}`);
  },
}));

const { default: OpsLayout, metadata } = await import("./layout");

const OPERATOR = { userId: "u-ops", displayName: "Owner", isOps: true } as SessionUser;

const layout = () => OpsLayout({ children: <p>the page</p> });

beforeEach(() => {
  world.session = null;
  world.pages = [{ href: "/ops/waitlist", label: "Waitlist" }];
});

describe("the owner area, to anyone but an operator", () => {
  it.each([
    ["no session", null],
    ["a session the API could not resolve", new SessionUnavailableError("target_router_unreachable", 503)],
    ["a person who is not an operator", { ...OPERATOR, isOps: false }],
  ])("is the site's 404 for %s, and no redirect", async (_who, session) => {
    world.session = session;
    await expect(layout()).rejects.toThrow(/^NEXT_NOT_FOUND$/);
  });

  it("lets any other failure be what it is", async () => {
    world.session = new Error("a bug");
    await expect(layout()).rejects.toThrow("a bug");
  });
});

describe("the owner area, to an operator", () => {
  it("renders the rail from OPS_PAGES, a way back to the dashboard, and the page", async () => {
    world.session = OPERATOR;
    world.pages = [
      { href: "/ops/waitlist", label: "Waitlist" },
      { href: "/ops/later", label: "A later tool" },
    ];
    const html = renderToString(await layout());
    const links = [...html.matchAll(/<a[^>]*href="([^"]+)"[^>]*>([^<]*)<\/a>/g)].map((m) => [m[1], m[2]]);
    expect(links).toEqual([
      ["/ops/waitlist", "Waitlist"],
      ["/ops/later", "A later tool"],
      ["/dashboard", "← Back to the dashboard"],
    ]);
    expect(html).toContain("<p>the page</p>");
  });

  it("is never indexed", () => {
    expect(metadata.robots).toEqual({ index: false, follow: false });
  });
});
