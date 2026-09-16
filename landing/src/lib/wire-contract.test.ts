import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import path from "path";
import { IDEMPOTENCY_KEY_MAX, NOT_POSTED, RESOLUTIONS } from "./commands";
import {
  EXPIRY_DAYS_DEFAULT,
  EXPIRY_DAYS_MAX,
  EXPIRY_DAYS_MIN,
  SECRET_PREFIX,
  TOKEN_NAME_MAX,
  TOKEN_ROLES,
} from "./tokens";

/**
 * The wire spellings this tier copied from the API, pinned to their source.
 *
 * `src/services/target/vocabulary.py` is the API's one spelling of the token
 * roles, the secret prefix, the token bounds, the idempotency key's bound and
 * the review resolutions; this tier repeats each in TypeScript because nothing
 * here can import a Python constant. A repeated spelling is free to drift —
 * a prefix drift alone would make `mintedTokenFrom` return null and the BFF
 * answer 502 over a token that was minted and never shown — so every one is
 * read back from the Python source and compared, the `intent-states-contract`
 * way. AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP.
 */
const VOCABULARY = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../src/services/target/vocabulary.py",
);

function source(): string {
  try {
    return readFileSync(VOCABULARY, "utf8");
  } catch (err) {
    throw new Error(
      `cannot read the API's vocabulary at ${VOCABULARY} — the contract is ` +
        `unverified, which is not the same as satisfied: ${err}`,
    );
  }
}

/** A module-level `NAME = <int>` or `NAME = "<str>"`, anchored at column 0. */
function scalar(name: string): string | number {
  const m = source().match(new RegExp(`^${name}\\s*(?::[^=]+)?=\\s*("([^"]*)"|(\\d+))\\s*$`, "m"));
  if (!m) throw new Error(`no module-level ${name} in ${VOCABULARY}`);
  return m[2] !== undefined ? m[2] : Number(m[3]);
}

/** A module-level tuple of strings, anchored at column 0. */
function tuple(name: string): string[] {
  const m = source().match(new RegExp(`^${name}[^=]*=\\s*\\(([\\s\\S]*?)\\)`, "m"));
  if (!m) throw new Error(`no module-level ${name} tuple in ${VOCABULARY}`);
  return [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
}

describe("the wire spellings are the vocabulary's", () => {
  it("token roles", () => {
    expect([...TOKEN_ROLES]).toEqual(tuple("TOKEN_ROLES"));
  });
  it("the secret prefix", () => {
    expect(SECRET_PREFIX).toBe(scalar("TOKEN_PREFIX"));
  });
  it("the token's name and expiry bounds", () => {
    expect(TOKEN_NAME_MAX).toBe(scalar("TOKEN_NAME_MAX"));
    expect(EXPIRY_DAYS_MIN).toBe(scalar("TOKEN_EXPIRY_DAYS_MIN"));
    expect(EXPIRY_DAYS_DEFAULT).toBe(scalar("TOKEN_EXPIRY_DAYS_DEFAULT"));
    expect(EXPIRY_DAYS_MAX).toBe(scalar("TOKEN_EXPIRY_DAYS_MAX"));
  });
  it("the idempotency key's bound", () => {
    expect(IDEMPOTENCY_KEY_MAX).toBe(scalar("IDEMPOTENCY_KEY_MAX"));
  });
  it("the review resolutions and the one verdict", () => {
    expect([...RESOLUTIONS]).toEqual(tuple("RESOLUTIONS"));
    expect(NOT_POSTED).toBe(scalar("NOT_POSTED"));
  });
});
