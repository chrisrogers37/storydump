import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import path from "path";
import { APPROVAL_POSITIONS, APPROVAL_RULES, RULE_LABELS } from "./approvals";
import { ARCHIVE_ITEM_NAME, ARCHIVE_SLIDE_NAME, ARCHIVE_SLIDE_NAME_UNKNOWN } from "./archive-media";
import { IDEMPOTENCY_KEY_MAX, NOT_POSTED, RESOLUTIONS } from "./commands";
import {
  AT_RULE_COPY,
  LINK_URL_MAX,
  LOCK_CLAUSES,
  NO_PUSH_BINDING,
  PLAN_HORIZON_DAYS,
  REPEAT_COPY,
} from "./command-client";
import { parseConnectPreview, startBody } from "./connect-choice";
import { CONNECTION_OUTCOMES } from "./connection-result";
import { LIVE_ACCOUNT_STATES } from "./destination";
import { DELIVERY_STATES, OVER_BUDGET_REASONS } from "./invitations";
import { ADMIT_OUTCOMES, MAX_EMAIL_CHARS } from "./ops-waitlist";
import { readinessStateLabel, readinessTone, sourceStatusBadges } from "./readiness";
import { IN_THE_WAY_MAX } from "./refusal-facts";
import { REPEAT_UNITS, WEEKDAYS } from "./repeat";
import {
  SUM_TOLERANCE_BP,
  conversionNotice,
  resetEqualAction,
  saveCustomAction,
  setEnabledAction,
} from "./source-mix";
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

/**
 * Another file of the API, read the same way: some spellings come from
 * the mix, the connect choice and the schema, not from the vocabulary. AN
 * UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP.
 */
const API_SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../src");

function apiFile(relative: string): string {
  const file = path.join(API_SRC, relative);
  try {
    return readFileSync(file, "utf8");
  } catch (err) {
    throw new Error(
      `cannot read the API's ${relative} at ${file} — the contract is ` +
        `unverified, which is not the same as satisfied: ${err}`,
    );
  }
}

/** The quoted names in a piece of Python, in order. */
function quoted(text: string, quote: "'" | '"'): string[] {
  return [...text.matchAll(new RegExp(`${quote}([a-z0-9_]+)${quote}`, "g"))].map((x) => x[1]);
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

/** A module-level dict of string to plain string, key and value, anchored at column 0. */
function mapping(name: string): Record<string, string> {
  const m = source().match(new RegExp(`^${name}[^=]*=\\s*\\{([\\s\\S]*?)^\\}`, "m"));
  if (!m) throw new Error(`no module-level ${name} mapping in ${VOCABULARY}`);
  const entries = [...m[1].matchAll(/^\s*"([^"]+)"\s*:\s*"([^"]*)"\s*,?\s*$/gm)].map(
    (x) => [x[1], x[2]] as const,
  );
  if (entries.length === 0) throw new Error(`${name} parsed to no entries in ${VOCABULARY}`);
  return Object.fromEntries(entries);
}

/**
 * The entries of a module-level dict of string to string, anchored at column 0,
 * read the way `mappingKeys` reads its keys.
 */
function mappingEntries(name: string): Record<string, string> {
  const m = source().match(new RegExp(`^${name}[^=]*=\\s*\\{([\\s\\S]*?)^\\}`, "m"));
  if (!m) throw new Error(`no module-level ${name} mapping in ${VOCABULARY}`);
  const entries = [...m[1].matchAll(/^\s*"([^"]+)"\s*:\s*"([^"]+)"/gm)];
  return Object.fromEntries(entries.map((x) => [x[1], x[2]]));
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
  it("what an invitation's email came to, and whose day refused it", () => {
    // A drift here would read as "couldn't email" whatever happened, or tell
    // an inviter Storydump hit its limit when they hit their own.
    expect([...DELIVERY_STATES]).toEqual(tuple("INVITATION_DELIVERY_STATES"));
    expect([...OVER_BUDGET_REASONS]).toEqual(tuple("EMAIL_BUDGET_REASONS"));
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
  it("what an archive item is called where a file name would be", () => {
    // The API names every archive item this way on every read, and this tier
    // names one the same where it is told by its provenance: a drift would
    // show two names for one post, one from each tier.
    expect(ARCHIVE_ITEM_NAME).toBe(scalar("ARCHIVE_ITEM_NAME"));
    expect(ARCHIVE_SLIDE_NAME).toBe(scalar("ARCHIVE_SLIDE_NAME"));
    expect(ARCHIVE_SLIDE_NAME_UNKNOWN).toBe(scalar("ARCHIVE_SLIDE_NAME_UNKNOWN"));
  });
  it("the tones a destination's readiness is badged in, and the states that have words", () => {
    // A tone this build does not know would read as attention, and a state it
    // has no words for would read "Unknown state": neither fails a request.
    const tones = mappingEntries("READINESS_TONES");
    expect(Object.keys(tones).length).toBeGreaterThan(0);
    for (const [state, tone] of Object.entries(tones)) {
      expect(readinessTone(tone), `${state}: ${tone}`).toBe(tone);
      expect(readinessStateLabel(state), state).not.toBe("Unknown state");
    }
  });
  it("the tones of an archive's own status, which are those of the destination states they share", () => {
    // The mix's per-source badges (indexing, stalled, needs reconnect) are drawn
    // in the tone the server gives the state of that name.
    const tones = mappingEntries("READINESS_TONES");
    const flags = { indexing: "indexing", stalled: "stalled", needsReconnect: "needs_reconnect" } as const;
    for (const [flag, state] of Object.entries(flags)) {
      const view = {
        sourceId: "s",
        assignable: true,
        candidates: 0,
        effectiveShare: "0",
        indexing: null,
        partialCoverage: null,
        stalled: null,
        needsReconnect: null,
        oldestObservedAt: null,
        [flag]: true,
      };
      expect(sourceStatusBadges(view)[0].tone, flag).toBe(tones[state]);
    }
  });
  it("the tolerance a custom save's shares may miss 100% by", () => {
    // `versioned_mix.decimal_rows` refuses a custom mix whose shares miss 1 by
    // more than this (`sum_not_one`). The page's own check holds the same line
    // in basis points: a looser one would call "adds up to 100%" a total the API
    // then refuses, and a tighter one would refuse a total the API takes.
    const found = apiFile("services/target/versioned_mix.py").match(
      /def decimal_rows[\s\S]*?abs\(total - 1\) > Decimal\("(\d*)\.(\d+)"\)/,
    );
    if (!found) throw new Error("no sum tolerance in versioned_mix.decimal_rows: the contract is unverified");
    const [, whole, fraction] = found;
    expect(whole === "" || /^0+$/.test(whole), `a tolerance of a whole point or more: ${found[0]}`).toBe(true);
    // Read off the digits, no float between: .001 is 10 basis points. A finer one
    // cannot be held by a page that types two decimals of a percent.
    expect(fraction.length, "a tolerance finer than a basis point").toBeLessThanOrEqual(4);
    expect(SUM_TOLERANCE_BP).toBe(Number(fraction.padEnd(4, "0")));
  });
  it("the actions a converted mix takes, which are the controls the card draws", () => {
    // The card draws a control only for an action the API lists, so a rename on
    // either side would take its control away without a refusal.
    const listed = apiFile("services/target/versioned_mix.py").match(
      /"supported_actions":\s*\[("reset_equal"[^\]]*)\]/,
    );
    if (!listed) throw new Error("no converted supported_actions in versioned_mix.py: the contract is unverified");
    expect(
      [resetEqualAction(0).action, setEnabledAction(0, "s", true).action, saveCustomAction(0, []).action].sort(),
    ).toEqual(quoted(listed[1], '"').sort());
  });
  it("what a past-posts connect can come to, each outcome with its sentence", () => {
    // `parseConnectionResult` answers null for an outcome it does not know, which
    // is a banner that never shows: the callback committed and the page said nothing.
    const check = apiFile("models/target/auth_plane.py").match(
      /"outcome IN \(([\s\S]*?)\)",\s*name="ck_connection_result_outcome"/,
    );
    if (!check) throw new Error("no ck_connection_result_outcome in auth_plane.py: the contract is unverified");
    expect([...CONNECTION_OUTCOMES].sort()).toEqual(quoted(check[1], "'").sort());
  });
  it("the conversions a mix can come from, and the two that are no news", () => {
    // `conversionNotice` says a conversion by its kind; a kind the API adds
    // without a sentence here would be a conversion nobody is told of.
    const kind = apiFile("services/target/versioned_mix.py").match(
      /\n\s+kind = \(\s*("hybrid_to_custom"[\s\S]*?)\n\s+\)\n/,
    );
    if (!kind) throw new Error("no conversion kinds in versioned_mix.convert_workspace: the contract is unverified");
    const converted = quoted(kind[1], '"');
    expect(converted.length).toBeGreaterThanOrEqual(3);
    // A sentence is said while the mix is still the mode it left it in, so a kind
    // has one if some mode says it.
    const said = (name: string) =>
      (["equal", "custom"] as const).some((mode) => conversionNotice(name, mode) !== null);
    for (const name of converted) expect(said(name), name).toBe(true);
    // A mix born on protocol 2, and one whose record says nothing, are not conversions.
    const born = apiFile("services/target/versioned_mix.py");
    for (const name of [
      born.match(/async def mark\([^)]*kind="([a-z_]+)"/)?.[1],
      born.match(/else "([a-z_]+)",\s*\}/)?.[1],
    ]) {
      expect(name, "a non-conversion kind this test could not find").toBeTruthy();
      expect(said(name as string), String(name)).toBe(false);
    }
  });
  it("the version of the preview a connect says it was shown", () => {
    // A page that knows another version reads every preview as unreadable, and
    // every connect then starts with no choice.
    const version = Number(apiFile("services/target/connect_choice.py").match(/^PREVIEW_VERSION\s*=\s*(\d+)\s*$/m)?.[1]);
    expect(Number.isInteger(version), "no PREVIEW_VERSION in connect_choice.py").toBe(true);
    const preview = parseConnectPreview({
      archive_available: true,
      entry: "add",
      handle: null,
      history: { editable: true, default: true, saved_enabled: null },
      expected_mix_revision: 8,
      preview_version: version,
      reset: null,
    });
    expect(preview, `a preview at the API's version ${version}`).not.toBeNull();
    expect(startBody(preview!, true)).toEqual({
      archive_choice: { use_history: true, expected_mix_revision: 8, preview_version: version },
    });
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
  it("the approval positions a workspace or a folder can hold", () => {
    // A drift here would offer a position the port refuses, or hide one it takes.
    expect([...APPROVAL_POSITIONS]).toEqual(tuple("APPROVAL_POSITIONS"));
  });
  it("the rules that approve a story, and the name each goes by", () => {
    // The Queue's "Approved by YOLO" is the card's and the CLI's: a drift
    // would name one approval two ways.
    expect([...APPROVAL_RULES]).toEqual(tuple("APPROVAL_RULES"));
    expect(RULE_LABELS).toEqual(mapping("RULE_LABELS"));
  });
  it("the units and weekdays a repeat rule is made of (#485)", () => {
    // A unit the ledger admits that is missing here could not be chosen, and a
    // weekday out of order would send the wrong ISO number.
    expect([...REPEAT_UNITS]).toEqual(tuple("REPEAT_UNITS"));
    expect([...WEEKDAYS]).toEqual(tuple("WEEKDAYS"));
  });
  it("the facts a refused repeat rule can carry, each with its sentence (#485)", () => {
    expect(Object.keys(REPEAT_COPY).sort()).toEqual(mappingKeys("REPEAT_SENTENCES").sort());
  });
  it("what admitting an address can come to, and the longest address it takes (#1723)", () => {
    // An outcome the API adds without one here would leave the owner's page
    // a line that names none of them; a drifted bound would refuse an address
    // the API takes, or forward one it cannot.
    expect([...ADMIT_OUTCOMES]).toEqual(tuple("ADMIT_OUTCOMES"));
    expect(MAX_EMAIL_CHARS).toBe(scalar("MAX_EMAIL_LENGTH"));
  });
  it("room under the in_the_way cap for the longest list the port can name", () => {
    // `schedule_item` names at most the item's own state, then each lock kind
    // once, so the cap must never cut a list the port can actually send.
    const longest = 1 + tuple("BLOCKING_LOCKS").length + tuple("WARNING_LOCKS").length;
    expect(IN_THE_WAY_MAX).toBeGreaterThanOrEqual(longest);
  });
});
