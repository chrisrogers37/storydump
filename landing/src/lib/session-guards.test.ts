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

// Deliberately structured rather than random. An earlier version used a
// realistic high-entropy UUID here, and GitGuardian's "Generic Password"
// detector flagged `<uuid>/../other` as a hardcoded secret — a 36-character
// high-entropy string followed by a slash reads as a URL with an embedded
// credential. It was a false positive, but the remedy is to reword rather than
// to add an ignore-file entry: an ignore entry suppresses the detector on this
// path permanently, so the next REAL finding here would be silent.
//
// Nothing is lost. These are shape fixtures — the regex cares about the shape
// of a UUID, never its entropy — and a fixture that cannot be mistaken for a
// credential is a better fixture.
const VALID_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";

describe("isWorkspaceId", () => {
  it("accepts a UUID and rejects everything that becomes a path segment", () => {
    expect(isWorkspaceId(VALID_ID)).toBe(true);

    // Every junk case is DERIVED from the id that passes on its own, so what
    // the assertion isolates is the mutation rather than the base. Under the
    // old fixtures a traversal case carried its own unrelated uuid, and a
    // rejection could have come from a malformed prefix instead of the
    // traversal — the test would have passed for the wrong reason.
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
      user: { id: "11111111-2222-3333-4444-555555555555", display_name: "Chris" },
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
    // The invariant this whole PR argued for, now that the API actually
    // implements it. `null` means the membership list could not be READ —
    // flattening it to [] would render "create your first workspace" to
    // someone who owns six, which is the failure this design exists to stop.
    respond(200, {
      user: { id: "11111111-2222-3333-4444-555555555555", display_name: "Chris" },
      workspaces: null,
      degraded: ["membership_list_unreadable"],
    });
    const session = await resolveSessionToken("good");
    expect(session?.workspaces).toBeNull();
    expect(session?.degraded).toContain("membership_list_unreadable");
  });

  it("treats a body with no user as not signed in", async () => {
    respond(200, { user: null, workspaces: null, degraded: [] });
    await expect(resolveSessionToken("good")).resolves.toBeNull();
  });
});
