/**
 * `GET /api/workspaces/[id]/media/[mediaId]/thumbnail` (#1634, #1644). The
 * picture streams through as the API sent it, with its type and the API's cache
 * policy. Nothing that is not a raster picture is relayed, and every failure
 * is a bare status the browser never keeps, so the page draws its placeholder.
 *
 * `?v=` goes to the API, which needs it for a past-posts item, and only after it
 * is checked; a past-posts item's `private, no-store, max-age=0` is relayed as it
 * came and nothing is added that would let a browser or a CDN keep it.
 */

import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const WS = "11111111-1111-4111-8111-111111111111";
const MEDIA = "22222222-2222-4222-8222-222222222222";
const PICTURE = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3]);

const state: { token: string | null; upstream: () => Response } = {
  token: "tok-test",
  upstream: () => new Response(null, { status: 500 }),
};
const captured: Array<{ path: string; token: string | null }> = [];

vi.mock("@/lib/session", () => {
  const UUID =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  return {
    getSessionToken: async () => state.token,
    isUuid: (v: unknown) => typeof v === "string" && UUID.test(v),
    isWorkspaceId: (v: unknown) => typeof v === "string" && UUID.test(v),
  };
});

vi.mock("@/lib/target-api", () => ({
  targetFetchBytes: async (path: string, token: string | null) => {
    captured.push({ path, token });
    return state.upstream();
  },
}));

const { GET } = await import("./route");

/** The thumbnail URL a page asks for, with its query as written (none when `query` is empty). */
function get(mediaId = MEDIA, id = WS, query = "v=0123456789abcdef") {
  const url = `https://storydump.app/api/workspaces/${id}/media/${mediaId}/thumbnail${
    query === "" ? "" : `?${query}`
  }`;
  return GET(new NextRequest(url), { params: Promise.resolve({ id, mediaId }) });
}

const DRIVE_POLICY = "private, max-age=2592000";
/** What the API says of a past-posts item's picture, and of its refusal. */
const PAST_POSTS_POLICY = "private, no-store, max-age=0";
/** A past-posts item's `thumbnail_version`: `<generation id>.<source use epoch>`. */
const PAIR = "5b0e1c2e-8c67-4d29-9a3f-0c1d2e3f4a5b.7";

const picture =
  (type = "image/jpeg", cache: string | null = DRIVE_POLICY, extra: Record<string, string> = {}) =>
  () =>
    new Response(PICTURE, {
      status: 200,
      headers: {
        "Content-Type": type,
        ...(cache === null ? {} : { "Cache-Control": cache }),
        ...extra,
      },
    });

beforeEach(() => {
  captured.length = 0;
  state.token = "tok-test";
  state.upstream = picture();
});

describe("GET /api/workspaces/[id]/media/[mediaId]/thumbnail", () => {
  it("without a session: 401, and nothing reaches the API", async () => {
    state.token = null;
    expect((await get()).status).toBe(401);
    expect(captured).toHaveLength(0);
  });

  it("refuses ids that are not UUIDs without reaching the API", async () => {
    const badWs = await get(MEDIA, "nope");
    expect(badWs.status).toBe(400);
    expect((await badWs.json()).error).toBe("invalid_workspace");
    const badMedia = await get("nope");
    expect(badMedia.status).toBe(400);
    expect((await badMedia.json()).error).toBe("invalid_media");
    expect(captured).toHaveLength(0);
  });

  it("streams the picture through with its type and the API's cache policy", async () => {
    const res = await get();
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("image/jpeg");
    expect(res.headers.get("cache-control")).toBe(DRIVE_POLICY);
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(PICTURE);
    // The API is asked by id and the version the page named, nothing else.
    expect(captured).toEqual([
      {
        path: `/workspaces/${WS}/media/${MEDIA}/thumbnail?v=0123456789abcdef`,
        token: "tok-test",
      },
    ]);
  });

  it("relays the API's 404 as a bare status the browser never keeps", async () => {
    state.upstream = () => new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
    const res = await get();
    expect(res.status).toBe(404);
    expect(res.headers.get("cache-control")).toBe("no-store");
    expect(await res.text()).toBe("");
  });

  it("never relays an SVG, or anything else that is not a raster picture", async () => {
    for (const type of ["image/svg+xml", "text/html", "application/json", ""]) {
      state.upstream = picture(type);
      const res = await get();
      expect(res.status).toBe(502);
      expect(res.headers.get("cache-control")).toBe("no-store");
      expect(await res.text()).toBe("");
    }
  });

  it("answers the API's own status when the API cannot answer", async () => {
    state.upstream = () => new Response(null, { status: 503 });
    expect((await get()).status).toBe(503);
  });
});

describe("a failed answer waits on nothing (#1644)", () => {
  // Under Next's runtime the cancel of an upstream body nobody reads can stay
  // pending for good (reproduced under `next dev` and `next build` + `next
  // start`, on Node 20 and 22). A route that waits on it never answers a 404:
  // an Off source's, a stale version's, a Drive item's with no link. The page
  // asks for past-posts pictures two at a time, so two such requests hold every
  // other preview in the tab. The body below is one whose cancel never settles,
  // and each test's short timeout is what a route that waits on it would hit.
  const NEVER = 1000;
  const neverCancels = (init: ResponseInit) => () =>
    new Response(new ReadableStream({ cancel: () => new Promise<void>(() => {}) }), init);

  it.each([404, 410, 429, 500, 503])(
    "answers the API's %i as a bare, never-kept status at once",
    { timeout: NEVER },
    async (status) => {
      state.upstream = neverCancels({ status });
      const res = await get(MEDIA, WS, `v=${PAIR}`);
      expect(res.status).toBe(status);
      expect(res.headers.get("cache-control")).toBe("no-store");
      expect(await res.text()).toBe("");
    },
  );

  it.each(["text/html", "image/svg+xml", ""])(
    "answers a 200 of type %j as a bare 502 at once",
    { timeout: NEVER },
    async (type) => {
      state.upstream = neverCancels({ status: 200, headers: type ? { "Content-Type": type } : {} });
      const res = await get();
      expect(res.status).toBe(502);
      expect(res.headers.get("cache-control")).toBe("no-store");
      expect(await res.text()).toBe("");
    },
  );

  it("releases the body it does not relay, once, and a cancel that fails changes nothing", async () => {
    const cancelled: string[] = [];
    state.upstream = () =>
      new Response(
        new ReadableStream({
          cancel: () => {
            cancelled.push("cancelled");
            return Promise.reject(new Error("the upstream stream had already failed"));
          },
        }),
        { status: 404 },
      );
    const res = await get(MEDIA, WS, `v=${PAIR}`);
    expect(res.status).toBe(404);
    expect(await res.text()).toBe("");
    expect(cancelled).toEqual(["cancelled"]);
  });

  it("does not release the picture it relays", async () => {
    // The positive control: the body of an answer that is relayed is the answer.
    let cancelled = false;
    state.upstream = () =>
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(PICTURE);
            controller.close();
          },
          cancel: () => {
            cancelled = true;
          },
        }),
        { status: 200, headers: { "Content-Type": "image/jpeg" } },
      );
    const res = await get();
    expect(res.status).toBe(200);
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(PICTURE);
    expect(cancelled).toBe(false);
  });
});

describe("the version the page names (#1644)", () => {
  const path = (query: string) => `/workspaces/${WS}/media/${MEDIA}/thumbnail${query}`;

  it("forwards a Drive item's version, and a past-posts item's generation and epoch, as written", async () => {
    expect((await get(MEDIA, WS, "v=0123456789abcdef")).status).toBe(200);
    expect((await get(MEDIA, WS, `v=${PAIR}`)).status).toBe(200);
    expect(captured.map((c) => c.path)).toEqual([
      path("?v=0123456789abcdef"),
      path(`?v=${PAIR}`),
    ]);
  });

  it("asks the API with no version when the page names none, which the API refuses for a past-posts item", async () => {
    expect((await get(MEDIA, WS, "")).status).toBe(200);
    expect(captured.map((c) => c.path)).toEqual([path("")]);
  });

  it("forwards no other query, and the first version when several are named", async () => {
    await get(MEDIA, WS, `parent=root&token=secret&v=${PAIR}&v=other`);
    expect(captured.map((c) => c.path)).toEqual([path(`?v=${PAIR}`)]);
  });

  it("refuses a version that is not a bounded run of URL-safe characters, before the API is asked", async () => {
    const refused = [
      "v=",
      `v=${"a".repeat(129)}`,
      "v=a%20b",
      "v=a%2Fb",
      "v=a%26b%3Dc",
      "v=a%3Fb",
      "v=a%23b",
      "v=a%0Ab",
      "v=%C3%A9",
      "v=%2e%2e%2f",
      "v=a%00b",
    ];
    for (const query of refused) {
      const res = await get(MEDIA, WS, query);
      expect(res.status, query).toBe(400);
      expect(await res.json(), query).toEqual({ error: "invalid_version" });
      expect(res.headers.get("cache-control"), query).toBe("no-store");
    }
    expect(captured).toEqual([]);
  });

  it("forwards the longest version it allows, and every character a version may hold", async () => {
    const longest = "a".repeat(128);
    for (const version of [longest, "a.b-c_d~E9"]) {
      expect((await get(MEDIA, WS, `v=${version}`)).status).toBe(200);
    }
    expect(captured.map((c) => c.path)).toEqual([
      path(`?v=${"a".repeat(128)}`),
      path("?v=a.b-c_d~E9"),
    ]);
  });
});

describe("a past-posts item's picture is never kept (#1644)", () => {
  it("relays the API's no-store policy as it came, and its bytes", async () => {
    state.upstream = picture("image/jpeg", PAST_POSTS_POLICY);
    const res = await get(MEDIA, WS, `v=${PAIR}`);
    expect(res.status).toBe(200);
    expect(res.headers.get("cache-control")).toBe(PAST_POSTS_POLICY);
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(PICTURE);
  });

  it("relays no validator and no freshness of its own, so nothing can answer 304 or be reused", async () => {
    state.upstream = picture("image/jpeg", PAST_POSTS_POLICY, {
      ETag: '"abc"',
      "Last-Modified": "Wed, 01 Jan 2020 00:00:00 GMT",
      Expires: "Wed, 01 Jan 2030 00:00:00 GMT",
      Age: "3600",
      Vary: "Authorization",
    });
    const res = await get(MEDIA, WS, `v=${PAIR}`);
    for (const header of ["etag", "last-modified", "expires", "age", "vary"]) {
      expect(res.headers.get(header), header).toBeNull();
    }
    expect(res.headers.get("cache-control")).toBe(PAST_POSTS_POLICY);
  });

  it("says no-store, not a default that could be kept, when the API names no policy", async () => {
    state.upstream = picture("image/jpeg", null);
    const res = await get(MEDIA, WS, `v=${PAIR}`);
    expect(res.status).toBe(200);
    expect(res.headers.get("cache-control")).toBe("no-store");
  });

  it("turns every failure into a bare status that is never kept, with nothing of the API's body", async () => {
    for (const status of [400, 401, 403, 404, 410, 429, 500, 502, 503]) {
      state.upstream = () =>
        new Response(JSON.stringify({ detail: "not found", url: "https://upstream.example/p" }), {
          status,
          headers: { "Cache-Control": PAST_POSTS_POLICY, ETag: '"abc"' },
        });
      const res = await get(MEDIA, WS, `v=${PAIR}`);
      expect(res.status, String(status)).toBe(status);
      expect(res.headers.get("cache-control"), String(status)).toBe("no-store");
      expect(res.headers.get("etag"), String(status)).toBeNull();
      expect(await res.text(), String(status)).toBe("");
    }
  });

  it("never answers 304, or any other redirect, to a browser", async () => {
    for (const status of [300, 301, 302, 304, 307, 308]) {
      state.upstream = () =>
        new Response(null, { status, headers: { ETag: '"abc"', Location: "https://x.example/" } });
      const res = await get(MEDIA, WS, `v=${PAIR}`);
      expect(res.status, String(status)).toBe(502);
      expect(res.headers.get("cache-control"), String(status)).toBe("no-store");
      expect(res.headers.get("etag"), String(status)).toBeNull();
      expect(res.headers.get("location"), String(status)).toBeNull();
    }
    // The positive control: the API's own refusals keep their status.
    state.upstream = () => new Response(null, { status: 404 });
    expect((await get(MEDIA, WS, `v=${PAIR}`)).status).toBe(404);
  });

  it("still relays a Drive picture's long-lived policy, which the past-posts source's does not touch", async () => {
    state.upstream = picture("image/jpeg", DRIVE_POLICY);
    const res = await get(MEDIA, WS, "v=0123456789abcdef");
    expect(res.headers.get("cache-control")).toBe(DRIVE_POLICY);
  });
});
