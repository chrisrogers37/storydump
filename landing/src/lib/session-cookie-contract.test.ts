import { describe, it, expect } from "vitest";
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import path from "path";
import { SESSION_COOKIE } from "./session";

/**
 * The cookie name is a CROSS-TIER CONTRACT.
 *
 * The API mints, sets and revokes the session cookie; this tier only reads it
 * (#1032). Nothing in TypeScript can see a Python constant, so the two names
 * are free to drift: a constant here naming a cookie the API does not set
 * passes every unit test and a green build, while a completed Google consent
 * bounces the user to /login, because the two halves of the handoff never meet
 * in one place.
 *
 * They meet here. Both tiers live in this repo, so the test reads the API's
 * source directly rather than restating its value — restating it would just be
 * a third copy free to drift the same way.
 *
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP. A contract test that quietly
 * stops running when its counterparty moves is indistinguishable from one that
 * passes, which is the precise shape of the bug it exists to catch.
 */
const API_PRINCIPAL = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../src/api/principal.py",
);

describe("the session cookie name agrees with the API", () => {
  it("reads the API's own constant, and fails if it cannot", () => {
    let source: string;
    try {
      source = readFileSync(API_PRINCIPAL, "utf8");
    } catch (err) {
      throw new Error(
        `cannot read the API's cookie constant at ${API_PRINCIPAL} — ` +
          `the contract is unverified, which is not the same as satisfied: ${err}`,
      );
    }

    // Anchored at column 0: the module-level `COOKIE`, not a longer name that
    // happens to end in it and not a local binding inside a function.
    const match = source.match(/^COOKIE = "([^"]+)"/m);
    expect(
      match,
      `no module-level COOKIE = "..." found in ${API_PRINCIPAL}`,
    ).not.toBeNull();

    expect(SESSION_COOKIE).toBe(match![1]);
  });
});
