import { describe, it, expect, vi, afterEach } from "vitest";
import { isUuid, isWorkspaceId, resolveSessionToken, SessionUnavailableError } from "./session";

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

// Deliberately structured rather than random. A realistic high-entropy UUID
// here reads to a secret scanner as a hardcoded credential — a 36-character
// high-entropy string followed by a slash (`<uuid>/../other`) looks like a URL
// with an embedded credential. The remedy is a fixture that cannot be mistaken
// for one, not an ignore-file entry: an ignore entry suppresses the detector on
// this path permanently, so the next REAL finding here would be silent.
//
// Nothing is lost. These are shape fixtures — the regex cares about the shape
// of a UUID, never its entropy — and a fixture that cannot be mistaken for a
// credential is a better fixture.
const VALID_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";

describe("isWorkspaceId", () => {
  it("accepts a UUID and rejects everything that becomes a path segment", () => {
    expect(isWorkspaceId(VALID_ID)).toBe(true);

    // Every junk case is DERIVED from the id that passes on its own, so what
    // the assertion isolates is the mutation rather than the base: a
    // traversal case with its own unrelated uuid could be rejected for a
    // malformed prefix, not the traversal, and pass for the wrong reason.
    for (const junk of [
      undefined,
      "",
      "../../etc/passwd",
      `${VALID_ID}/../other`,
      `${VALID_ID}/`,
      `/${VALID_ID}`,
      `${VALID_ID}\u0000`,
      "not-a-uuid",
      VALID_ID.replace(/-/g, ""),
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

  it("resolves a live token against the API's /me shape", async () => {
    respond(200, {
      user: { id: "11111111-2222-3333-4444-555555555555", display_name: "Sam" },
      workspaces: [],
      degraded: [],
    });
    const session = await resolveSessionToken("good");
    expect(session?.userId).toBe("11111111-2222-3333-4444-555555555555");
    // Signed in with no workspace is a NORMAL state, not an error — it is every
    // user's state between signing in and creating their first one, and the API
    // says so too: "a user with zero workspaces is the normal first state".
    expect(session?.workspaces).toEqual([]);
    expect(session?.activeWorkspaceId).toBeNull();
  });

  it("keeps `workspaces: null` distinct from an empty list", async () => {
    // The invariant: `null` means the membership list could not be READ —
    // flattening it to [] would render "create your first workspace" to
    // someone who owns six, which is the failure this design exists to stop.
    respond(200, {
      user: { id: "11111111-2222-3333-4444-555555555555", display_name: "Sam" },
      workspaces: null,
      degraded: ["membership_list_unreadable"],
    });
    const session = await resolveSessionToken("good");
    expect(session?.workspaces).toBeNull();
    expect(session?.degraded).toContain("membership_list_unreadable");
  });

  it.each([
    [true, true],
    [false, false],
    [undefined, false],
    ["true", false],
  ])("reads /me's ops %s as isOps %s: only the API's true counts", async (ops, isOps) => {
    respond(200, {
      user: { id: "11111111-2222-3333-4444-555555555555", display_name: "Sam" },
      workspaces: [],
      ops,
    });
    expect((await resolveSessionToken("good"))?.isOps).toBe(isOps);
  });

  it("treats a body with no user as not signed in", async () => {
    respond(200, { user: null, workspaces: null, degraded: [] });
    await expect(resolveSessionToken("good")).resolves.toBeNull();
  });
});

describe("isUuid", () => {
  it("is the shape every forwarded id must have — a UUID or nothing, never a free-form string", () => {
    expect(isUuid("0b6e5f1a-2f4d-4c1e-9a3b-7d8e9f0a1b2c")).toBe(true);
    expect(isUuid("0B6E5F1A-2F4D-4C1E-9A3B-7D8E9F0A1B2C")).toBe(true);
    for (const bad of ["", "not-a-uuid", "0b6e5f1a-2f4d-4c1e-9a3b-7d8e9f0a1b2c ", 42, null, undefined, {}]) {
      expect(isUuid(bad), String(bad)).toBe(false);
    }
  });
});
