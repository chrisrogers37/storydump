import { afterEach, describe, expect, it, vi } from "vitest";
import {
  EXPIRY_DAYS_DEFAULT,
  expiryCopy,
  expiryDaysValid,
  isTokenRole,
  isTokenSecret,
  lastUsedCopy,
  mintMyToken,
  mintServiceToken,
  mintTokenRefusalCopy,
  mintedTokenFrom,
  revokeMyToken,
  revokeServiceToken,
  revokeTokenRefusalCopy,
  tokenNameValid,
  tokenRowFrom,
  tokenRowState,
  tokenRowsFrom,
} from "./tokens";
import { notAuthenticatedCopy, unreachableCopy } from "./refusal-copy";

const WS = "11111111-1111-4111-8111-111111111111";
const TOK = "22222222-2222-4222-8222-222222222222";
const SECRET = "sdt_" + "a".repeat(43);
const NOW = new Date("2026-09-15T00:00:00Z");

/** A row as the API lists it — snake_case, every column present. */
const API_ROW = {
  id: TOK,
  name: "claude-code on chris-mbp",
  role: "operator",
  expires_at: "2026-12-14T00:00:00Z",
  revoked_at: null,
  last_used_at: null,
  created_at: "2026-09-15T00:00:00Z",
};

let captured: { url: string; init?: RequestInit }[] = [];

function stubFetch(body: unknown, status = 200) {
  captured = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      captured.push({ url, init });
      return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
}

function stubUnreachable() {
  captured = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new TypeError("fetch failed");
    }),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("the form's own rules — the same ones the BFF enforces", () => {
  it("a name is 1–80 characters once trimmed", () => {
    expect(tokenNameValid("claude-code")).toBe(true);
    expect(tokenNameValid("a".repeat(80))).toBe(true);
    expect(tokenNameValid("a".repeat(81))).toBe(false);
    expect(tokenNameValid("")).toBe(false);
    expect(tokenNameValid("   ")).toBe(false);
  });

  it("an expiry is a whole number of days from 1 to 365, and the default is inside that", () => {
    expect(expiryDaysValid(1)).toBe(true);
    expect(expiryDaysValid(365)).toBe(true);
    expect(expiryDaysValid(EXPIRY_DAYS_DEFAULT)).toBe(true);
    expect(expiryDaysValid(0)).toBe(false);
    expect(expiryDaysValid(366)).toBe(false);
    expect(expiryDaysValid(1.5)).toBe(false);
    expect(expiryDaysValid("90")).toBe(false);
    expect(expiryDaysValid(NaN)).toBe(false);
    expect(expiryDaysValid(null)).toBe(false);
  });

  it("knows the two roles and nothing else", () => {
    expect(isTokenRole("operator")).toBe(true);
    expect(isTokenRole("readonly")).toBe(true);
    expect(isTokenRole("admin")).toBe(false);
    expect(isTokenRole("")).toBe(false);
    expect(isTokenRole(undefined)).toBe(false);
  });

  it("recognises a secret by its prefix — the same prefix the API routes bearer values on", () => {
    expect(isTokenSecret(SECRET)).toBe(true);
    expect(isTokenSecret("sdt_")).toBe(false);
    expect(isTokenSecret("sess_" + "a".repeat(43))).toBe(false);
    expect(isTokenSecret("sdt_has spaces")).toBe(false);
    expect(isTokenSecret(null)).toBe(false);
  });
});

describe("the reshape from the API's row to the screen's", () => {
  it("renames every column and keeps the nulls as nulls", () => {
    expect(tokenRowFrom(API_ROW)).toEqual({
      id: TOK,
      name: "claude-code on chris-mbp",
      role: "operator",
      expiresAt: "2026-12-14T00:00:00Z",
      revokedAt: null,
      lastUsedAt: null,
      createdAt: "2026-09-15T00:00:00Z",
      workspaceId: null,
    });
  });

  it("carries a service identity's workspace", () => {
    expect(tokenRowFrom({ ...API_ROW, workspace_id: WS })?.workspaceId).toBe(
      WS,
    );
  });

  it("refuses a row it could neither render nor revoke", () => {
    // No id: no Revoke target. No name: nothing to show. Both are the API's
    // contract and a row without them is a fault, not a token.
    expect(tokenRowFrom({ ...API_ROW, id: undefined })).toBeNull();
    expect(tokenRowFrom({ ...API_ROW, name: 7 })).toBeNull();
    expect(tokenRowFrom("nope")).toBeNull();
    expect(tokenRowFrom(null)).toBeNull();
  });

  it("reads a list, dropping the malformed rows, and refuses a non-list", () => {
    expect(tokenRowsFrom([API_ROW, { junk: true }])).toHaveLength(1);
    expect(tokenRowsFrom([])).toEqual([]);
    expect(tokenRowsFrom("nope")).toBeNull();
    expect(tokenRowsFrom(undefined)).toBeNull();
  });

  it("reads the mint answer only when the secret is really there", () => {
    expect(
      mintedTokenFrom({
        id: TOK,
        name: "n",
        role: "readonly",
        expires_at: "2026-12-14T00:00:00Z",
        secret: SECRET,
      }),
    ).toEqual({
      id: TOK,
      name: "n",
      role: "readonly",
      expiresAt: "2026-12-14T00:00:00Z",
      secret: SECRET,
      workspaceId: null,
    });
    // A 201 without a usable secret is a failure: the person's next act is
    // to paste it, and there would be nothing to paste.
    expect(
      mintedTokenFrom({
        id: TOK,
        name: "n",
        role: "readonly",
        expires_at: null,
      }),
    ).toBeNull();
    expect(
      mintedTokenFrom({
        id: TOK,
        name: "n",
        role: "readonly",
        expires_at: null,
        secret: "nope",
      }),
    ).toBeNull();
  });
});

describe("what a row says about itself", () => {
  it("revoked beats expired beats live", () => {
    expect(
      tokenRowState(
        { revokedAt: null, expiresAt: "2026-12-14T00:00:00Z" },
        NOW,
      ),
    ).toBe("live");
    expect(
      tokenRowState(
        { revokedAt: null, expiresAt: "2026-09-14T00:00:00Z" },
        NOW,
      ),
    ).toBe("expired");
    expect(
      tokenRowState(
        {
          revokedAt: "2026-09-10T00:00:00Z",
          expiresAt: "2026-09-14T00:00:00Z",
        },
        NOW,
      ),
    ).toBe("revoked");
    expect(tokenRowState({ revokedAt: null, expiresAt: null }, NOW)).toBe(
      "live",
    );
  });

  it("an expiry on the clock's exact tick is expired, not live", () => {
    expect(
      tokenRowState({ revokedAt: null, expiresAt: NOW.toISOString() }, NOW),
    ).toBe("expired");
  });

  it("expiryCopy — days remaining, rounded up so a token never reads as expired early", () => {
    expect(expiryCopy("2026-12-13T00:00:00Z", NOW)).toBe("expires in 89 days");
    expect(expiryCopy("2026-12-12T12:00:00Z", NOW)).toBe("expires in 89 days");
    expect(expiryCopy("2026-09-16T00:00:00Z", NOW)).toBe("expires in 1 day");
    expect(expiryCopy("2026-09-15T06:00:00Z", NOW)).toBe("expires in 1 day");
    expect(expiryCopy("2026-09-14T00:00:00Z", NOW)).toBe("expired");
    expect(expiryCopy(NOW.toISOString(), NOW)).toBe("expired");
    expect(expiryCopy(null, NOW)).toBe("no expiry");
    expect(expiryCopy("not a date", NOW)).toBe("expiry unknown");
  });

  it("lastUsedCopy — never, or how long ago", () => {
    expect(lastUsedCopy(null, NOW)).toBe("never used");
    expect(lastUsedCopy("2026-09-14T23:30:00Z", NOW)).toBe("used just now");
    expect(lastUsedCopy("2026-09-14T22:30:00Z", NOW)).toBe("used 1 hour ago");
    expect(lastUsedCopy("2026-09-14T19:00:00Z", NOW)).toBe("used 5 hours ago");
    expect(lastUsedCopy("2026-09-14T00:00:00Z", NOW)).toBe("used yesterday");
    expect(lastUsedCopy("2026-09-10T00:00:00Z", NOW)).toBe("used 5 days ago");
    expect(lastUsedCopy("not a date", NOW)).toBe("last use unknown");
  });
});

describe("minting a personal token at its proxy", () => {
  it("posts the API's body to /api/me/tokens and hands back the token with its secret", async () => {
    stubFetch(
      {
        id: TOK,
        name: "laptop",
        role: "operator",
        expiresAt: "2026-12-14T00:00:00Z",
        secret: SECRET,
        workspaceId: null,
      },
      201,
    );
    const result = await mintMyToken({
      name: "laptop",
      role: "operator",
      expiresInDays: 90,
    });
    expect(result).toEqual({
      ok: true,
      token: {
        id: TOK,
        name: "laptop",
        role: "operator",
        expiresAt: "2026-12-14T00:00:00Z",
        secret: SECRET,
        workspaceId: null,
      },
    });
    expect(captured[0].url).toBe("/api/me/tokens");
    expect(captured[0].init?.method).toBe("POST");
    expect(JSON.parse(String(captured[0].init?.body))).toEqual({
      name: "laptop",
      role: "operator",
      expires_in_days: 90,
    });
  });

  it("carries the refusal by name and status", async () => {
    stubFetch({ error: "invalid_args" }, 400);
    expect(
      await mintMyToken({
        name: "laptop",
        role: "operator",
        expiresInDays: 90,
      }),
    ).toEqual({
      ok: false,
      error: "invalid_args",
      status: 400,
    });
  });

  it("names an unreachable app rather than a refusal", async () => {
    stubUnreachable();
    expect(
      await mintMyToken({
        name: "laptop",
        role: "operator",
        expiresInDays: 90,
      }),
    ).toEqual({
      ok: false,
      error: "unreachable",
      status: 0,
    });
  });

  it("refuses a 201 with no secret in it — there would be nothing to paste", async () => {
    stubFetch(
      { id: TOK, name: "laptop", role: "operator", expiresAt: null },
      201,
    );
    const result = await mintMyToken({
      name: "laptop",
      role: "operator",
      expiresInDays: 90,
    });
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error).toBe("malformed_response");
  });
});

describe("listing and revoking personal tokens", () => {
  it("revokes with a DELETE on the token", async () => {
    stubFetch({ tokenId: TOK, revoked: true });
    expect(await revokeMyToken(TOK)).toEqual({ ok: true });
    expect(captured[0].url).toBe(`/api/me/tokens/${TOK}`);
    expect(captured[0].init?.method).toBe("DELETE");
  });

  it("a revoke the server did not confirm is not a success", async () => {
    stubFetch({ tokenId: TOK, revoked: false });
    const result = await revokeMyToken(TOK);
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error).toBe("malformed_response");
  });

  it("carries a revoke refusal by name, and an unreachable app by its own name", async () => {
    stubFetch({ error: "http_404" }, 404);
    expect(await revokeMyToken(TOK)).toEqual({
      ok: false,
      error: "http_404",
      status: 404,
    });
    stubUnreachable();
    expect(await revokeMyToken(TOK)).toEqual({
      ok: false,
      error: "unreachable",
      status: 0,
    });
  });
});

describe("workspace service identities", () => {
  it("mints under the workspace, with NO role in the body — the API fixes readonly", async () => {
    stubFetch(
      {
        id: TOK,
        name: "ci",
        role: "readonly",
        expiresAt: "2026-12-14T00:00:00Z",
        secret: SECRET,
        workspaceId: WS,
      },
      201,
    );
    const result = await mintServiceToken(WS, {
      name: "ci",
      expiresInDays: 90,
    });
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("expected ok");
    expect(result.token.workspaceId).toBe(WS);
    expect(result.token.role).toBe("readonly");
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/tokens`);
    expect(captured[0].init?.method).toBe("POST");
    const body = JSON.parse(String(captured[0].init?.body));
    expect(body).toEqual({ name: "ci", expires_in_days: 90 });
    expect("role" in body).toBe(false);
  });

  it("carries the role refusal by name", async () => {
    stubFetch({ error: "insufficient_role" }, 403);
    expect(
      await mintServiceToken(WS, { name: "ci", expiresInDays: 90 }),
    ).toEqual({
      ok: false,
      error: "insufficient_role",
      status: 403,
    });
    stubUnreachable();
    expect(
      await mintServiceToken(WS, { name: "ci", expiresInDays: 90 }),
    ).toEqual({
      ok: false,
      error: "unreachable",
      status: 0,
    });
  });

  it("refuses a mint answer without a secret", async () => {
    stubFetch(
      {
        id: TOK,
        name: "ci",
        role: "readonly",
        expiresAt: null,
        workspaceId: WS,
      },
      201,
    );
    const result = await mintServiceToken(WS, {
      name: "ci",
      expiresInDays: 90,
    });
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error).toBe("malformed_response");
  });

  it("revokes under the workspace", async () => {
    stubFetch({ tokenId: TOK, revoked: true });
    expect(await revokeServiceToken(WS, TOK)).toEqual({ ok: true });
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/tokens/${TOK}`);
    expect(captured[0].init?.method).toBe("DELETE");

    stubFetch({ error: "http_404" }, 404);
    expect(await revokeServiceToken(WS, TOK)).toEqual({
      ok: false,
      error: "http_404",
      status: 404,
    });

    stubFetch({ revoked: "yes" });
    const odd = await revokeServiceToken(WS, TOK);
    expect(odd.ok).toBe(false);
    if (odd.ok) throw new Error("unreachable");
    expect(odd.error).toBe("malformed_response");

    stubUnreachable();
    expect(await revokeServiceToken(WS, TOK)).toEqual({
      ok: false,
      error: "unreachable",
      status: 0,
    });
  });
});

describe("the sentences", () => {
  it("mint: every branch says what did not happen", () => {
    for (const reason of [
      "invalid_args",
      "invalid_name",
      "invalid_role",
      "invalid_expiry",
      "malformed_body",
      "insufficient_role",
      "http_403",
      "malformed_response",
      "something_new",
    ]) {
      expect(mintTokenRefusalCopy(reason), reason).toMatch(
        /Nothing was created|mint another/,
      );
    }
  });

  it("mint: the not-signed-in and unreachable sentences are the shared ones", () => {
    for (const reason of ["unauthenticated", "http_401"]) {
      expect(mintTokenRefusalCopy(reason)).toBe(
        notAuthenticatedCopy("Nothing was created."),
      );
    }
    for (const reason of ["unreachable", "target_router_unreachable"]) {
      expect(mintTokenRefusalCopy(reason)).toBe(
        unreachableCopy("Nothing was created"),
      );
    }
  });

  it("mint: a bad body points at the two fields the person can fix", () => {
    const copy = mintTokenRefusalCopy("invalid_args");
    expect(copy).toMatch(/80/);
    expect(copy).toMatch(/365/);
  });

  it("mint: a role refusal names the floor", () => {
    expect(mintTokenRefusalCopy("insufficient_role")).toMatch(/admin or owner/);
    expect(mintTokenRefusalCopy("http_403")).toMatch(/admin or owner/);
  });

  it("revoke: gone, not signed in, unreachable, and a default that takes no blame it cannot place", () => {
    expect(revokeTokenRefusalCopy("http_404")).toMatch(
      /no longer here|already revoked/,
    );
    expect(revokeTokenRefusalCopy("not_found")).toMatch(
      /no longer here|already revoked/,
    );
    expect(revokeTokenRefusalCopy("insufficient_role")).toMatch(
      /admin or owner/,
    );
    expect(revokeTokenRefusalCopy("http_403")).toMatch(/admin or owner/);
    for (const reason of ["unauthenticated", "http_401"]) {
      expect(revokeTokenRefusalCopy(reason)).toBe(
        notAuthenticatedCopy("Nothing changed."),
      );
    }
    for (const reason of ["unreachable", "target_router_unreachable"]) {
      expect(revokeTokenRefusalCopy(reason)).toBe(
        unreachableCopy("Nothing changed"),
      );
    }
    expect(revokeTokenRefusalCopy("something_new")).toMatch(/Nothing changed/);
  });
});
