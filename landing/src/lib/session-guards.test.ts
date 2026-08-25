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

  it("resolves a live token to a user carrying no workspace", async () => {
    respond(200, { user_id: "11111111-2222-3333-4444-555555555555", display_name: "Chris" });
    const session = await resolveSessionToken("good");
    expect(session?.userId).toBe("11111111-2222-3333-4444-555555555555");
    // Signed in with no workspace is a NORMAL state, not an error — it is every
    // user's state between signing in and creating their first one.
    expect(session?.activeWorkspaceId).toBeNull();
  });
});
