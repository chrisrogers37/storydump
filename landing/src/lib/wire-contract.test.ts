import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import path from "path";
import { IDEMPOTENCY_KEY_MAX, NOT_POSTED, RESOLUTIONS } from "./commands";
import {
  AT_RULE_COPY,
  LINK_URL_MAX,
  LOCK_CLAUSES,
  NO_PUSH_BINDING,
  PLAN_HORIZON_DAYS,
} from "./command-client";
import { LIVE_ACCOUNT_STATES } from "./destination";
import { IN_THE_WAY_MAX } from "./refusal-facts";
import { THUMBNAIL_TYPES } from "./thumbnails";
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

/**
 * The keys of a module-level dict of strings, anchored at column 0. The body
 * ends at the `}` that opens a line, since a value may be an f-string with
 * braces of its own.
 */
function mappingKeys(name: string): string[] {
  const m = source().match(new RegExp(`^${name}[^=]*=\\s*\\{([\\s\\S]*?)^\\}`, "m"));
  if (!m) throw new Error(`no module-level ${name} mapping in ${VOCABULARY}`);
  return [...m[1].matchAll(/^\s*"([^"]+)"\s*:/gm)].map((x) => x[1]);
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
  it("the planned story's warning that no chat is bound", () => {
    // A drift here would not fail a request: the warning would simply stop
    // being shown, and a person would not be told that nothing is asked.
    expect(NO_PUSH_BINDING).toBe(scalar("NO_PUSH_BINDING"));
  });
  it("how far ahead a story may be planned", () => {
    expect(PLAN_HORIZON_DAYS).toBe(scalar("PLAN_HORIZON_DAYS"));
  });
  it("the longest link an item may carry", () => {
    // The refusal sentence quotes this cap, so a drift would tell a person the wrong number.
    expect(LINK_URL_MAX).toBe(scalar("LINK_URL_MAX"));
  });
  it("the lock kinds a schedule refusal can name, each with its sentence", () => {
    // A kind the port adds without one here would leave a refusal unnamed.
    expect(Object.keys(LOCK_CLAUSES).sort()).toEqual(
      [...tuple("BLOCKING_LOCKS"), ...tuple("WARNING_LOCKS")].sort(),
    );
  });
  it("the image types a thumbnail may be", () => {
    // A type the API serves that is missing here would reach the page as a
    // placeholder, with no error on either side.
    expect([...THUMBNAIL_TYPES]).toEqual(tuple("THUMBNAIL_TYPES"));
  });
  it("the account states a story can be planned onto", () => {
    // A drift here would offer an account the port refuses, or hide one it takes.
    expect([...LIVE_ACCOUNT_STATES]).toEqual(tuple("LIVE_ACCOUNT_STATES"));
  });
  it("the rules a refused time can name, each with its sentence", () => {
    // A rule the port adds without one here would read as the sentence with
    // no rule, which names none of them.
    expect(Object.keys(AT_RULE_COPY).sort()).toEqual(mappingKeys("AT_RULE_SENTENCES").sort());
  });
  it("room under the in_the_way cap for the longest list the port can name", () => {
    // `schedule_item` names at most the item's own state, then each lock kind
    // once, so the cap must never cut a list the port can actually send.
    const longest = 1 + tuple("BLOCKING_LOCKS").length + tuple("WARNING_LOCKS").length;
    expect(IN_THE_WAY_MAX).toBeGreaterThanOrEqual(longest);
  });
});
