/**
 * The owner's waitlist actions (#1723): a row's Admit or Send again, and the
 * field that admits any address.
 *
 * Asserted without a DOM, as `sign-out-button.test.tsx` is: each control is
 * called as a function with its hooks stubbed, and its handler is invoked
 * against a scripted `fetch`. Pinned: what each posts, its one in-flight
 * state (held, so it cannot send twice), the refresh once the route has
 * answered, and the line each answer leaves. An answer that cannot be
 * confirmed keeps the typed address (#1740); some answers come through the
 * real route, its API answer scripted, rather than as a made-up route answer.
 */

import { renderToString } from "react-dom/server";
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement, type ReactNode } from "react";

const router = { refresh: vi.fn() };
const state: { status: unknown; set: ReturnType<typeof vi.fn> } = {
  status: { pending: false, line: null },
  set: vi.fn(),
};

vi.mock("next/navigation", () => ({ useRouter: () => router }));
vi.mock("react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react")>()),
  useState: () => [state.status, state.set],
}));
// The route's one seam, for the answers that come through it.
vi.mock("@/lib/session", async (actual) => ({
  ...(await actual<typeof import("@/lib/session")>()),
  getSessionToken: async () => "tok-test",
}));

const { AdmitAddress, WaitlistAction } = await import("./waitlist-actions");
const { Button } = await import("@/components/ui/button");
const { Input } = await import("@/components/ui/input");
const { admissionLine, LOST_ANSWER } = await import("@/lib/ops-waitlist");
const { POST } = await import("@/app/api/admissions/route");
const { TARGET_API_URL } = await import("@/lib/target-api");

const EMAIL = "person@example.com";

type Props = {
  children?: ReactNode;
  disabled?: boolean;
  role?: string;
  onClick?: () => Promise<unknown>;
  onSubmit?: (event: unknown) => Promise<void>;
};

/** Every element in a returned tree, depth-first. */
function* walk(node: ReactNode): Generator<ReactElement<Props>> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (!isValidElement<Props>(node)) return;
  yield node;
  yield* walk(node.props.children);
}

/** The text a tree renders: its strings, in order. */
function text(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(text).join("");
  if (isValidElement<Props>(node)) return text(node.props.children);
  return "";
}

const of = (tree: ReactNode, type: unknown) => [...walk(tree)].filter((el) => el.type === type);
/** The lines a tree shows, rendered: each is a status, so it is read out when it changes. */
const said = (tree: ReactNode) =>
  [...renderToString(tree).matchAll(/<p role="status"[^>]*>([^<]*)<\/p>/g)].map((m) => m[1]);

/** `fetch` answering *status* with *body*, recording each call. */
function answer(status: number, body: unknown) {
  const fetch = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
    async () =>
      new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

/** `fetch` answering *status* with *text* as it comes: a body that is not JSON. */
function answerText(status: number, text: string) {
  const fetch = vi.fn(async () => new Response(text, { status, headers: { "content-type": "text/html" } }));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

const posted = (fetch: ReturnType<typeof answer>) =>
  fetch.mock.calls.map(([url, init]) => [url, JSON.parse(init?.body as string)]);

/** Answers of 200 the page cannot read as an outcome, as the route might send them. */
const UNCONFIRMED_ANSWERS: Array<[string, () => void]> = [
  ["is not JSON", () => answerText(200, "<html><body>Bad gateway</body></html>")],
  ["is null", () => answer(200, null)],
  ["names an outcome it does not know", () => answer(200, { outcome: "approved" })],
];

const UNCONFIRMED = { text: LOST_ANSWER, ok: false };

/** `fetch` that throws, as a request that never left the browser does. */
function neverLeaves() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }),
  );
}

/**
 * `fetch` as the whole chain answers it: the page's `/api/admissions` is the
 * real route, whose call to the API gets *upstream*. The route's statuses
 * are kept, and any other URL is recorded and refused, so neither a throw
 * nor a stray call can pass for the answer under test.
 */
function throughTheRoute(upstream: () => Response) {
  const chain = { api: 0, route: [] as number[], unexpected: [] as string[] };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (url === "/api/admissions") {
        const headers = new Headers(init?.headers);
        headers.set("sec-fetch-site", "same-origin");
        const request = new NextRequest(`https://storydump.app${url}`, {
          method: init?.method,
          headers,
          body: init?.body,
        });
        const response = await POST(request);
        chain.route.push(response.status);
        return response;
      }
      if (url === `${TARGET_API_URL}/api/v1/admissions`) {
        chain.api += 1;
        return upstream();
      }
      chain.unexpected.push(url);
      throw new TypeError(`unexpected fetch: ${url}`);
    }),
  );
  return chain;
}

/** The API's answer of *status* carrying *text*. */
const fromTheApi =
  (text: string, status = 200, type = "application/json") =>
  () =>
    new Response(text, { status, headers: { "content-type": type } });

/**
 * The line `admissionLine` leaves for an answer, which a control must show.
 * Its words are `ops-waitlist.test.ts`'s to pin, so a copy edit is one test's.
 */
const outcomeLine = (outcome: string) => admissionLine({ ok: true, status: 200, data: { outcome } });
const refusalLine = (status: number, error: string) =>
  admissionLine({ ok: false, status, error, body: { error } });

beforeEach(() => {
  router.refresh.mockClear();
  state.status = { pending: false, line: null };
  state.set.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a row's action", () => {
  it("offers Admit to a waiting address and posts it without resend", async () => {
    const fetch = answer(200, { outcome: "admitted" });
    const [admit] = of(WaitlistAction({ email: EMAIL, state: "waiting" }), Button);
    expect(text(admit)).toBe("Admit");
    await admit.props.onClick!();
    expect(posted(fetch)).toEqual([["/api/admissions", { email: EMAIL, resend: false }]]);
    expect(state.set.mock.calls).toEqual([
      [{ pending: true, line: null }],
      [{ pending: false, line: outcomeLine("admitted") }],
    ]);
    expect(router.refresh).toHaveBeenCalledTimes(1);
  });

  it.each(["admitted", "emailed"] as const)(
    "offers Send again to an address %s, posting it with resend",
    async (rowState) => {
      const fetch = answer(200, { outcome: "resent" });
      const [again] = of(WaitlistAction({ email: EMAIL, state: rowState }), Button);
      expect(text(again)).toBe("Send again");
      await again.props.onClick!();
      expect(posted(fetch)).toEqual([["/api/admissions", { email: EMAIL, resend: true }]]);
      expect(state.set).toHaveBeenLastCalledWith({ pending: false, line: outcomeLine("resent") });
      expect(router.refresh).toHaveBeenCalledTimes(1);
    },
  );

  it("offers nothing once the address has signed up", () => {
    expect(WaitlistAction({ email: EMAIL, state: "signed_up" })).toBeNull();
  });

  it.each([
    ["waiting", "Admitting…"],
    ["emailed", "Sending…"],
  ] as const)("is held while its request is in flight (%s)", (rowState, label) => {
    state.status = { pending: true, line: null };
    const [held] = of(WaitlistAction({ email: EMAIL, state: rowState }), Button);
    expect(held.props.disabled).toBe(true);
    expect(text(held)).toBe(label);
  });

  it("shows the line its last answer left, under a control that can be used again", () => {
    state.status = { pending: false, line: { text: "Already admitted.", ok: true } };
    const tree = WaitlistAction({ email: EMAIL, state: "admitted" });
    expect(said(tree)).toEqual(["Already admitted."]);
    expect(of(tree, Button)[0].props.disabled).toBe(false);
  });

  it.each([
    [409, "signed_up"],
    [403, "not_ops"],
    [503, "not_configured"],
  ])("says a %s %s in its own words, and refreshes", async (status, error) => {
    answer(status, { error });
    await of(WaitlistAction({ email: EMAIL, state: "waiting" }), Button)[0].props.onClick!();
    expect(state.set).toHaveBeenLastCalledWith({ pending: false, line: refusalLine(status, error) });
    expect(router.refresh).toHaveBeenCalledTimes(1);
  });

  it("refreshes nothing when the request never left the browser, and says to look first", async () => {
    neverLeaves();
    await of(WaitlistAction({ email: EMAIL, state: "emailed" }), Button)[0].props.onClick!();
    expect(state.set).toHaveBeenLastCalledWith({ pending: false, line: UNCONFIRMED });
    expect(router.refresh).not.toHaveBeenCalled();
  });

  it.each(UNCONFIRMED_ANSWERS)(
    "says it could not confirm an answer of 200 that %s, and refreshes once",
    async (_why, script) => {
      script();
      await of(WaitlistAction({ email: EMAIL, state: "waiting" }), Button)[0].props.onClick!();
      expect(state.set.mock.calls).toEqual([
        [{ pending: true, line: null }],
        [{ pending: false, line: UNCONFIRMED }],
      ]);
      expect(router.refresh).toHaveBeenCalledTimes(1);
    },
  );

  it("says so under a control that can be used again", () => {
    state.status = { pending: false, line: UNCONFIRMED };
    const tree = WaitlistAction({ email: EMAIL, state: "emailed" });
    expect(said(tree)).toHaveLength(1);
    expect(said(tree)[0]).toContain("Reload the page before trying again.");
    expect(of(tree, Button)[0].props.disabled).toBe(false);
    expect(text(of(tree, Button)[0])).toBe("Send again");
  });

  it("says it could not confirm when the API answers null, through the route", async () => {
    const chain = throughTheRoute(fromTheApi("null"));
    await of(WaitlistAction({ email: EMAIL, state: "emailed" }), Button)[0].props.onClick!();
    expect(chain).toEqual({ api: 1, route: [502], unexpected: [] });
    expect(state.set.mock.calls).toEqual([
      [{ pending: true, line: null }],
      [{ pending: false, line: UNCONFIRMED }],
    ]);
    expect(router.refresh).toHaveBeenCalledTimes(1);
  });

  it("says the API's own refusal in its own words, through the route", async () => {
    const chain = throughTheRoute(
      fromTheApi(JSON.stringify({ detail: "that address has already signed up", reason: "signed_up" }), 409),
    );
    await of(WaitlistAction({ email: EMAIL, state: "waiting" }), Button)[0].props.onClick!();
    expect(chain).toEqual({ api: 1, route: [409], unexpected: [] });
    expect(state.set).toHaveBeenLastCalledWith({ pending: false, line: refusalLine(409, "signed_up") });
    expect(router.refresh).toHaveBeenCalledTimes(1);
  });
});

describe("the admit field", () => {
  /** A submit of the form holding *value*, and its reset. */
  function submitted(value: string) {
    const reset = vi.fn();
    const event = {
      preventDefault: vi.fn(),
      currentTarget: {
        elements: { namedItem: (name: string) => (name === "email" ? { value } : null) },
        reset,
      },
    };
    return { event, reset };
  }

  const submit = (event: unknown) => {
    const [form] = of(AdmitAddress(), "form");
    return form.props.onSubmit!(event);
  };

  it("is one field and one button, under its heading", () => {
    const tree = AdmitAddress();
    expect(of(tree, Input)).toHaveLength(1);
    expect(of(tree, Button).map(text)).toEqual(["Admit"]);
    expect(text(tree)).toContain("Admit an address");
  });

  it("posts the typed address, never as a resend, and clears once it is admitted", async () => {
    const fetch = answer(200, { outcome: "admitted" });
    const { event, reset } = submitted("off.list@example.com");
    await submit(event);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(posted(fetch)).toEqual([["/api/admissions", { email: "off.list@example.com", resend: false }]]);
    expect(reset).toHaveBeenCalledTimes(1);
    expect(router.refresh).toHaveBeenCalledTimes(1);
  });

  it("keeps the address and says so when the API refuses it as invalid_email", async () => {
    answer(400, { error: "invalid_email" });
    const { event, reset } = submitted("not an address");
    await submit(event);
    expect(state.set).toHaveBeenLastCalledWith({ pending: false, line: refusalLine(400, "invalid_email") });
    expect(reset).not.toHaveBeenCalled();
  });

  it("sends nothing while its request is in flight", async () => {
    state.status = { pending: true, line: null };
    const fetch = answer(200, { outcome: "admitted" });
    const [held] = of(AdmitAddress(), Button);
    expect(held.props.disabled).toBe(true);
    expect(text(held)).toBe("Admitting…");
    await submit(submitted(EMAIL).event);
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each(UNCONFIRMED_ANSWERS)(
    "keeps the typed address when an answer of 200 %s, says so, and refreshes once",
    async (_why, script) => {
      script();
      const { event, reset } = submitted("off.list@example.com");
      await submit(event);
      expect(state.set.mock.calls).toEqual([
        [{ pending: true, line: null }],
        [{ pending: false, line: UNCONFIRMED }],
      ]);
      expect(reset).not.toHaveBeenCalled();
      expect(router.refresh).toHaveBeenCalledTimes(1);
    },
  );

  // Written out, never derived from the module: the field clears only when
  // the answer left the address admitted.
  it.each([
    ["admitted", 1],
    ["resent", 1],
    ["admitted_before", 1],
    ["signed_up", 0],
    ["invalid_email", 0],
    ["not_configured", 0],
  ] as const)(
    "clears for an answer of 200 naming %s only when it went as asked (%i)",
    async (outcome, resets) => {
      answer(200, { outcome });
      const { event, reset } = submitted(EMAIL);
      await submit(event);
      expect(reset).toHaveBeenCalledTimes(resets);
      expect(router.refresh).toHaveBeenCalledTimes(1);
    },
  );

  // A refusal is never a yes, however its reason is spelled: the field clears
  // only for a success whose answer says so (#1740).
  it.each([
    [409, "admitted"],
    [400, "resent"],
    [500, "admitted_before"],
  ])("keeps the address after a %s whose reason is spelled %s", async (status, error) => {
    answer(status, { error });
    const { event, reset } = submitted(EMAIL);
    await submit(event);
    expect(reset).not.toHaveBeenCalled();
    expect(router.refresh).toHaveBeenCalledTimes(1);
  });

  it.each([
    [409, "signed_up"],
    [503, "not_configured"],
  ])("keeps the address after a %s %s, said in its own words", async (status, error) => {
    answer(status, { error });
    const { event, reset } = submitted(EMAIL);
    await submit(event);
    expect(state.set).toHaveBeenLastCalledWith({ pending: false, line: refusalLine(status, error) });
    expect(reset).not.toHaveBeenCalled();
    expect(router.refresh).toHaveBeenCalledTimes(1);
  });

  it("keeps the address, and refreshes nothing, when the request never left the browser", async () => {
    neverLeaves();
    const { event, reset } = submitted(EMAIL);
    await submit(event);
    expect(state.set.mock.calls).toEqual([
      [{ pending: true, line: null }],
      [{ pending: false, line: UNCONFIRMED }],
    ]);
    expect(reset).not.toHaveBeenCalled();
    expect(router.refresh).not.toHaveBeenCalled();
  });

  it("says so under a field and a button that can be used again", () => {
    state.status = { pending: false, line: UNCONFIRMED };
    const tree = AdmitAddress();
    expect(said(tree)).toHaveLength(1);
    expect(said(tree)[0]).toContain("Reload the page before trying again.");
    expect(of(tree, Input)).toHaveLength(1);
    expect(of(tree, Button)[0].props.disabled).toBe(false);
  });

  it.each([
    ["is not JSON", fromTheApi("<html><body>Bad gateway</body></html>", 200, "text/html")],
    ["is an empty object", fromTheApi("{}")],
    ["names an outcome it does not know", fromTheApi(JSON.stringify({ outcome: "approved" }))],
  ])("keeps the typed address when the API's 200 %s, through the route", async (_why, upstream) => {
    const chain = throughTheRoute(upstream);
    const { event, reset } = submitted("off.list@example.com");
    await submit(event);
    expect(chain).toEqual({ api: 1, route: [502], unexpected: [] });
    expect(state.set.mock.calls).toEqual([
      [{ pending: true, line: null }],
      [{ pending: false, line: UNCONFIRMED }],
    ]);
    expect(reset).not.toHaveBeenCalled();
    expect(router.refresh).toHaveBeenCalledTimes(1);
  });

  it("clears once the API admits the address, through the route", async () => {
    const chain = throughTheRoute(fromTheApi(JSON.stringify({ outcome: "admitted" })));
    const { event, reset } = submitted("off.list@example.com");
    await submit(event);
    expect(chain).toEqual({ api: 1, route: [200], unexpected: [] });
    expect(state.set).toHaveBeenLastCalledWith({ pending: false, line: outcomeLine("admitted") });
    expect(reset).toHaveBeenCalledTimes(1);
    expect(router.refresh).toHaveBeenCalledTimes(1);
  });
});
