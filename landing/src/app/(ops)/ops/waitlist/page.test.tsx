/**
 * The owner's waitlist page (#1723): each state's row and its one action, an
 * admission made off the waitlist, the field above the table, the read that
 * failed, and the 404 on the API's 401, 403 and 404, which is what holds:
 * Next renders the layout and this page in parallel.
 *
 * The page, an async server component, is called directly with the session
 * token and the API stubbed, and rendered to a string (`environment:
 * "node"`), its client controls with it. `notFound` throws, so a 404 cannot
 * pass as a page.
 */

import { renderToString } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const world = vi.hoisted(() => ({
  token: "tok-ops" as string | null,
  upstream: null as unknown,
  asked: [] as Array<[string, string | null]>,
}));

vi.mock("@/lib/session", async (actual) => ({
  ...(await actual<typeof import("@/lib/session")>()),
  getSessionToken: async () => world.token,
}));
vi.mock("@/lib/target-api", () => ({
  targetFetch: async (path: string, token: string | null) => {
    world.asked.push([path, token]);
    return world.upstream;
  },
}));
vi.mock("next/navigation", async (original) => ({
  ...(await original<typeof import("next/navigation")>()),
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
  useRouter: () => ({ refresh: () => {} }),
}));

const { default: OpsWaitlistPage } = await import("./page");

const JOINED = "2026-10-12T09:30:00.123456+00:00";

function row(email: string, state: string, joinedAt: string | null = JOINED) {
  return {
    email,
    on_waitlist: joinedAt !== null,
    joined_at: joinedAt,
    state,
    admitted_at: state === "waiting" ? null : JOINED,
    emailed_at: state === "emailed" ? JOINED : null,
  };
}

const listed = (rows: unknown[]) => ({
  ok: true,
  data: { v: 1, kind: "waitlist", data: { rows }, error: null },
});

const ROWS = [
  row("waiting@example.com", "waiting"),
  row("off.list@example.com", "admitted", null),
  row("emailed@example.com", "emailed"),
  row("signed.up@example.com", "signed_up"),
  row("second@example.com", "waiting"),
];

const render = async () => renderToString(await OpsWaitlistPage());

/** The table row that holds *email*. */
function rowFor(html: string, email: string): string {
  const found = html.split("<tr").find((segment) => segment.includes(`>${email}<`));
  if (!found) throw new Error(`no row for ${email}`);
  return found;
}

/** The labels of the buttons in a piece of HTML. */
const buttons = (html: string) => [...html.matchAll(/<button[^>]*>(?:<svg[\s\S]*?<\/svg>)?([^<]*)<\/button>/g)].map((m) => m[1]);

beforeEach(() => {
  world.token = "tok-ops";
  world.upstream = listed(ROWS);
  world.asked.length = 0;
});

describe("the waitlist, read", () => {
  it("is read from the API with the session", async () => {
    await render();
    expect(world.asked).toEqual([["/ops/waitlist", "tok-ops"]]);
  });

  it.each([
    ["waiting@example.com", "Waiting", "Oct 12, 2026", ["Admit"]],
    ["off.list@example.com", "Admitted", "not on the waitlist", ["Send again"]],
    ["emailed@example.com", "Emailed", "Oct 12, 2026", ["Send again"]],
    ["signed.up@example.com", "Signed up", "Oct 12, 2026", []],
  ])("lists %s as %s, joined %s, with its one action", async (email, label, joined, actions) => {
    const tr = rowFor(await render(), email);
    expect(tr).toContain(`>${label}</td>`);
    expect(tr).toContain(`>${joined}</td>`);
    // A phone folds the two columns into one line under the address.
    expect(tr.replace(/<!-- -->/g, "")).toContain(`>${label} · ${joined}<`);
    expect(buttons(tr)).toEqual(actions);
  });

  it("counts the addresses still waiting", async () => {
    expect(await render()).toContain(">2 still waiting<");
  });

  it("puts the field that admits any address above the table, with one field and one button", async () => {
    const html = await render();
    const field = html.slice(html.indexOf("Admit an address"), html.indexOf("<table"));
    expect(html.indexOf("Admit an address")).toBeGreaterThan(-1);
    expect(field.match(/<input/g)).toHaveLength(1);
    expect(buttons(field)).toEqual(["Admit"]);
  });

  it("says when no one is on the waitlist", async () => {
    world.upstream = listed([]);
    const html = await render();
    expect(html).toContain("No one is on the waitlist yet.");
    expect(html).not.toContain("<table");
  });

  it.each([
    ["a server error", { ok: false, status: 500, error: "http_500" }],
    ["an unreachable API", { ok: false, status: 503, error: "target_router_unreachable" }],
    ["an answer that is not the waitlist", { ok: true, data: { v: 1, kind: "waitlist", data: {} } }],
  ])("says the read failed after %s, never an empty list", async (_why, upstream) => {
    world.upstream = upstream;
    const html = await render();
    expect(html).toContain("We couldn&#x27;t read the waitlist just now.");
    expect(html).not.toContain("<table");
    expect(html).not.toContain("No one is on the waitlist");
  });
});

describe("the waitlist, refused", () => {
  it.each([401, 403, 404])("is the site's 404 when the API answers %s", async (status) => {
    world.upstream = { ok: false, status, error: status === 403 ? "not_ops" : `http_${status}` };
    await expect(OpsWaitlistPage()).rejects.toThrow(/^NEXT_NOT_FOUND$/);
  });

  it("is the site's 404 with no session, and asks the API nothing", async () => {
    world.token = null;
    await expect(OpsWaitlistPage()).rejects.toThrow(/^NEXT_NOT_FOUND$/);
    expect(world.asked).toEqual([]);
  });
});
