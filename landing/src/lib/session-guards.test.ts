import { describe, it, expect, vi, afterEach } from "vitest";
import { isWorkspaceId, resolveSessionToken, SessionUnavailableError } from "./session";

afterEach(() => vi.unstubAllGlobals());

function respond(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
}

describe("isWorkspaceId", () => {
  it("accepts a UUID and rejects everything that becomes a path segment", () => {
    expect(isWorkspaceId("3f2a1c44-9b0e-4d21-8a77-1c5e9f0b2d31")).toBe(true);
    for (const junk of [
      undefined,
      "",
      "../../etc/passwd",
      "3f2a1c44-9b0e-4d21-8a77-1c5e9f0b2d31/../other",
      "not-a-uuid",
      "3f2a1c449b0e4d218a771c5e9f0b2d31",
    ]) {
      expect(isWorkspaceId(junk as string | undefined), `for ${junk}`).toBe(false);
    }
  });

  it("is a SHAPE check and nothing more", () => {
    // A well-formed id the caller has no membership in still passes. Stated as
    // a test so nobody later reads this function as an authorization gate — the
    // router authorizes, this only stops junk becoming a URL.
    expect(isWorkspaceId("00000000-0000-0000-0000-000000000000")).toBe(true);
  });
});

describe("resolveSessionToken separates 'nobody' from 'could not ask'", () => {
  it("returns null when the token is presented and rejected", async () => {
    respond(401, { error: "invalid_session" });
    await expect(resolveSessionToken("stale")).resolves.toBeNull();

    respond(403, { error: "revoked" });
    await expect(resolveSessionToken("revoked")).resolves.toBeNull();
  });

  it("THROWS rather than returning null when the router cannot answer", async () => {
    // The load-bearing case. A null here would sign a valid user out on every
    // blip, because every caller treats null as "not signed in" — so the two
    // outcomes must not share a return value.
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new TypeError("fetch failed");
    }));
    await expect(resolveSessionToken("good")).rejects.toBeInstanceOf(
      SessionUnavailableError,
    );

    respond(500, { error: "boom" });
    await expect(resolveSessionToken("good")).rejects.toBeInstanceOf(
      SessionUnavailableError,
    );
  });

  it("resolves a live token to a user carrying no workspace", async () => {
    respond(200, { user_id: "11111111-2222-3333-4444-555555555555", display_name: "Chris" });
    const session = await resolveSessionToken("good");
    expect(session?.userId).toBe("11111111-2222-3333-4444-555555555555");
    // Signed in with no workspace is a NORMAL state, not an error — it is every
    // user's state between signing in and creating their first one.
    expect(session?.activeWorkspaceId).toBeNull();
  });
});
