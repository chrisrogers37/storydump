import { afterEach, describe, expect, it, vi } from "vitest";
import { callBff, postJson } from "./bff";

/**
 * The shape nine modules used to hand-write. Each case here is a line one of
 * those copies could have got wrong on its own, and two of them had: the
 * non-JSON body and the thrown fetch are the paths that produce a blank
 * banner rather than a sentence.
 */
const respond = (status: number, body: unknown, json = true) =>
  vi.spyOn(globalThis, "fetch").mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: json
      ? async () => body
      : async () => {
          throw new Error("not json");
        },
  } as unknown as Response);

afterEach(() => vi.restoreAllMocks());

describe("callBff", () => {
  it("returns the parsed body on success", async () => {
    respond(200, { link: "x" });
    const r = await callBff("/api/x");
    expect(r).toEqual({ ok: true, status: 200, data: { link: "x" } });
  });

  it("reads the route's own error string verbatim", async () => {
    respond(400, { error: "invalid_workspace" });
    const r = await callBff("/api/x");
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toBe("invalid_workspace");
  });

  it("falls back to http_<status> when the route named no error", async () => {
    respond(403, { detail: "forbidden" });
    const r = await callBff("/api/x");
    if (!r.ok) expect(r.error).toBe("http_403");
  });

  it("keeps the refused body for the one caller that reads a second key", async () => {
    respond(409, { reason: "illegal_transition" });
    const r = await callBff("/api/x");
    if (!r.ok) expect(r.body.reason).toBe("illegal_transition");
  });

  it("survives a body that is not JSON at all", async () => {
    respond(502, null, false);
    const r = await callBff("/api/x");
    if (!r.ok) expect(r.error).toBe("http_502");
  });

  it("answers `unreachable` with status 0 when fetch throws", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network"));
    const r = await callBff("/api/x");
    expect(r).toEqual({ ok: false, error: "unreachable", status: 0, body: {} });
  });

  it("treats a 200 whose body is not an object as an empty one", async () => {
    // `json()` resolving to `null` is a 200 with no readable body. Every
    // post-`ok` guard the callers apply reads a key off `data`, so `data` must
    // be an object or those reads throw where they used to return a refusal.
    respond(200, null);
    const r = await callBff("/api/x");
    expect(r).toEqual({ ok: true, status: 200, data: {} });
  });
});

describe("postJson", () => {
  it("posts by default and takes a method for the one PUT", () => {
    expect(postJson({ a: 1 })).toEqual({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: '{"a":1}',
    });
    expect(postJson({ a: 1 }, "PUT").method).toBe("PUT");
  });
});
