/**
 * `GET /api/workspaces/[id]/media/[mediaId]/thumbnail` (#1634). The picture
 * streams through as the API sent it, with its type and the API's cache
 * policy. Nothing that is not a raster picture is relayed, and every failure
 * is a bare status the browser never keeps, so the page draws its placeholder.
 */

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

function get(mediaId = MEDIA, id = WS) {
  const request = new Request(
    `https://storydump.app/api/workspaces/${id}/media/${mediaId}/thumbnail?v=0123456789abcdef`,
  ) as unknown as Parameters<typeof GET>[0];
  return GET(request, { params: Promise.resolve({ id, mediaId }) });
}

const picture =
  (type = "image/jpeg", cache = "private, max-age=2592000") =>
  () =>
    new Response(PICTURE, {
      status: 200,
      headers: { "Content-Type": type, "Cache-Control": cache },
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
    expect(res.headers.get("cache-control")).toBe("private, max-age=2592000");
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(PICTURE);
    // The version keys the browser's cache only; the API is asked by id.
    expect(captured).toEqual([
      { path: `/workspaces/${WS}/media/${MEDIA}/thumbnail`, token: "tok-test" },
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
