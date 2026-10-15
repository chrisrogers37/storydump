/**
 * `/ops` is the rail's first page (#1723): it redirects there for an
 * operator, and is the site's 404 for anyone else on its own, not only
 * through the layout. Next renders the two in parallel, so a redirect run for
 * a stranger put `/ops/waitlist` into the 404 page a stranger was sent.
 * `notFound` and `redirect` throw what they were asked, so one cannot pass as
 * the other.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { OPS_PAGES } from "@/lib/ops-pages";
import type { SessionUser } from "@/lib/session";

const world = vi.hoisted(() => ({ session: null as unknown }));

vi.mock("@/lib/session", async (actual) => ({
  ...(await actual<typeof import("@/lib/session")>()),
  getSession: async () => world.session,
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

const { default: OpsHome } = await import("./page");

const OPERATOR = { userId: "u-ops", displayName: "Owner", isOps: true } as SessionUser;

beforeEach(() => {
  world.session = null;
});

describe("/ops", () => {
  it("redirects an operator to the first page of the rail", async () => {
    world.session = OPERATOR;
    expect(OPS_PAGES[0].href).toBe("/ops/waitlist");
    await expect(OpsHome()).rejects.toThrow(/^NEXT_REDIRECT \/ops\/waitlist$/);
  });

  it.each([
    ["no session", null],
    ["a person who is not an operator", { ...OPERATOR, isOps: false }],
  ])("is the site's 404 for %s, and never redirects", async (_who, session) => {
    world.session = session;
    await expect(OpsHome()).rejects.toThrow(/^NEXT_NOT_FOUND$/);
  });
});
