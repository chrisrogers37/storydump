import { readdirSync, readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";
import { notAuthenticatedCopy, unreachableCopy } from "./refusal-copy";
import {
  SOURCE_REFUSAL_COPY,
  sourceRefusalCopy,
  type SourceRefusalReason,
} from "./source-refusals";

/**
 * Every named refusal of the source experience has a sentence, and the reasons
 * the API sends are read back from its source (`wire-contract.test.ts`'s way):
 * a reason the API names that has no sentence here would reach a person as
 * "That did not work", which names no remedy, and a sentence keyed on a reason
 * the API never sends is copy nobody will ever read.
 *
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP — and so is a reader that
 * finds nothing, which looks exactly like a source with no reasons in it.
 */

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");

function source(relative: string): string {
  const file = path.join(REPO, relative);
  try {
    return readFileSync(file, "utf8");
  } catch (err) {
    throw new Error(
      `cannot read ${file} — the contract is unverified, which is not the same as satisfied: ${err}`,
    );
  }
}

function filesUnder(relative: string, wanted: (name: string) => boolean): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== "__pycache__" && entry.name !== "node_modules") walk(full);
      } else if (wanted(entry.name)) {
        out.push(path.relative(REPO, full));
      }
    }
  };
  walk(path.join(REPO, relative));
  return out;
}

const everything = (parts: string[]) => parts.map(source).join("\n");

function codes(pattern: RegExp, text: string): string[] {
  return [...text.matchAll(pattern)].map((m) => m[1]);
}

const NAMED = Object.keys(SOURCE_REFUSAL_COPY) as SourceRefusalReason[];
const FALLBACK = sourceRefusalCopy("not_a_reason_anyone_sends", 500);
const BOUNDS = { min: 0, max: 36500 };

describe("every named reason says its own sentence", () => {
  it("is the table's sentence, never the fallback, and never another reason's", () => {
    const owner = new Map<string, string>();
    for (const reason of NAMED) {
      const copy = sourceRefusalCopy(reason, 400);
      expect(copy, reason).toBe(SOURCE_REFUSAL_COPY[reason]);
      expect(copy, reason).not.toBe(FALLBACK);
      expect(owner.get(copy), `${reason} repeats ${owner.get(copy)}`).toBeUndefined();
      owner.set(copy, reason);
    }
    expect(NAMED.length).toBeGreaterThan(15);
  });

  it("answers the same whatever status carried it, so a 403 or a 404 never overrides a name", () => {
    for (const reason of NAMED) {
      for (const status of [0, 400, 401, 403, 404, 409, 500, 503]) {
        expect(sourceRefusalCopy(reason, status), `${reason} at ${status}`).toBe(
          SOURCE_REFUSAL_COPY[reason],
        );
      }
    }
  });

  it("says what happened to the change: nothing changed, or nothing from the connection was kept", () => {
    for (const reason of NAMED) {
      expect(SOURCE_REFUSAL_COPY[reason], reason).toMatch(
        /Nothing changed|nothing from this connection was kept/,
      );
    }
  });

  it("shows a person no reason code, status or field name", () => {
    const every = [
      ...NAMED.map((reason) => SOURCE_REFUSAL_COPY[reason]),
      sourceRefusalCopy("mix_conflict", 409, { surface: "connect" }),
      sourceRefusalCopy("invalid_source_policy", 400, { bounds: BOUNDS }),
      sourceRefusalCopy("invalid_mix_sum_not_one", 400),
      sourceRefusalCopy("malformed_response", 200),
      FALLBACK,
    ];
    for (const copy of every) {
      expect(copy).not.toMatch(/[a-z0-9]+_[a-z0-9_]+/);
      expect(copy).not.toMatch(/\bhttp\b|\bstatus\b|\bJSON\b|\bundefined\b|\bnull\b/i);
    }
  });
});

describe("what depends on where the refusal was met", () => {
  it("reads a conflict at connect as a stale preview, and a conflict anywhere else as an edit to review", () => {
    const connect = sourceRefusalCopy("mix_conflict", 409, { surface: "connect" });
    expect(connect).toMatch(/preview/);
    expect(connect).not.toBe(SOURCE_REFUSAL_COPY.mix_conflict);
    expect(sourceRefusalCopy("mix_conflict", 409, { surface: "mix" })).toBe(
      SOURCE_REFUSAL_COPY.mix_conflict,
    );
    expect(sourceRefusalCopy("mix_conflict", 409)).toBe(SOURCE_REFUSAL_COPY.mix_conflict);
    // The policy's conflict is its own, and the connect surface does not touch it.
    expect(sourceRefusalCopy("policy_conflict", 409, { surface: "connect" })).toBe(
      SOURCE_REFUSAL_COPY.policy_conflict,
    );
  });

  it("puts the server's limits into the refused age, and only into that refusal", () => {
    const withBounds = sourceRefusalCopy("invalid_source_policy", 400, { bounds: BOUNDS });
    expect(withBounds).toContain("from 0 to 36,500");
    expect(withBounds).toContain("Nothing changed");
    expect(sourceRefusalCopy("invalid_source_policy", 400, { bounds: { min: 7, max: 365 } })).toContain(
      "from 7 to 365",
    );
    // No bounds sent, none invented.
    expect(sourceRefusalCopy("invalid_source_policy", 400)).toBe(
      SOURCE_REFUSAL_COPY.invalid_source_policy,
    );
    expect(sourceRefusalCopy("invalid_source_policy", 400, { bounds: null })).toBe(
      SOURCE_REFUSAL_COPY.invalid_source_policy,
    );
    expect(sourceRefusalCopy("policy_conflict", 409, { bounds: BOUNDS })).toBe(
      SOURCE_REFUSAL_COPY.policy_conflict,
    );
  });
});

describe("the reasons that are not named one by one", () => {
  it("says any other invalid mix as a refused mix, and keeps the source-set refusal apart", () => {
    for (const reason of [
      "invalid_mix_sum_not_one",
      "invalid_mix_bad_ratio",
      "invalid_mix_action",
      "invalid_mix_precision_loss",
      "invalid_mix_all_off",
    ]) {
      const copy = sourceRefusalCopy(reason, 400);
      expect(copy, reason).toMatch(/The mix was refused/);
      expect(copy, reason).toMatch(/Nothing changed/);
    }
    expect(sourceRefusalCopy("invalid_mix_source_set", 400)).toBe(
      SOURCE_REFUSAL_COPY.invalid_mix_source_set,
    );
    expect(SOURCE_REFUSAL_COPY.invalid_mix_source_set).not.toMatch(/The mix was refused/);
  });

  it("is the one sentence that does not say nothing changed, for a success it could not read", () => {
    const copy = sourceRefusalCopy("malformed_response", 200);
    expect(copy).toMatch(/may or may not have been saved/);
    expect(copy).toMatch(/Reload the page/);
    expect(copy).not.toMatch(/Nothing changed/);
  });

  it("falls back by status and by this tier's own words, each with its own remedy", () => {
    expect(sourceRefusalCopy("http_401", 401)).toBe(notAuthenticatedCopy("Nothing changed."));
    expect(sourceRefusalCopy("unauthenticated", 401)).toBe(notAuthenticatedCopy("Nothing changed."));
    const admin = sourceRefusalCopy("http_403", 403);
    expect(admin).toMatch(/admin/);
    expect(sourceRefusalCopy("insufficient_role", 400)).toBe(admin);
    expect(sourceRefusalCopy("unreachable", 0)).toBe(unreachableCopy("Nothing changed"));
    expect(sourceRefusalCopy("target_router_unreachable", 503)).toBe(unreachableCopy("Nothing changed"));
    const missing = sourceRefusalCopy("http_404", 404);
    expect(missing).toMatch(/could not be found/);
    expect(FALLBACK).toMatch(/Nothing changed/);
    expect(new Set([admin, missing, FALLBACK]).size).toBe(3);
  });

  it("does not take an inherited property for a named reason", () => {
    for (const reason of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
      expect(sourceRefusalCopy(reason, 500), reason).toBe(FALLBACK);
    }
  });
});

/** Read on first use, so a source that cannot be read fails the test that needs it. */
function lazy<T>(make: () => T): () => T {
  let made: { value: T } | undefined;
  return () => (made ??= { value: make() }).value;
}

describe("the reasons the API sends are the ones that have sentences", () => {
  const pythonSource = lazy(() => everything(filesUnder("src", (name) => name.endsWith(".py"))));

  it("the mix's own refusals (versioned_mix.py)", () => {
    const reasons = codes(/^\s+reason = "([a-z0-9_]+)"/gm, source("src/services/target/versioned_mix.py"));
    expect(reasons).toEqual(
      expect.arrayContaining([
        "mix_conflict",
        "mix_protocol_required",
        "mix_membership_change_requires_action",
        "mix_conversion_unavailable",
      ]),
    );
    for (const reason of reasons) {
      expect(NAMED, `${reason} has no sentence`).toContain(reason);
    }
  });

  it("every refusal a source's policy can meet (app.py's status map)", () => {
    const table = source("src/api/app.py").match(/^_SOURCE_POLICY_STATUS[^=]*=\s*\{([\s\S]*?)^\}/m);
    if (!table) throw new Error("no _SOURCE_POLICY_STATUS in src/api/app.py — the contract is unverified");
    const reasons = codes(/^\s*"([a-z0-9_]+)"\s*:/gm, table[1]);
    expect(reasons).toEqual(
      expect.arrayContaining([
        "policy_conflict",
        "unsupported_source_policy",
        "invalid_source_policy",
        "source_not_found",
        "unsupported_source_provider",
        "past_posts_not_activated",
      ]),
    );
    for (const reason of reasons) {
      expect(NAMED, `${reason} has no sentence`).toContain(reason);
    }
  });

  it("every way a mix can be invalid reads as a refused mix, and the source set is named", () => {
    const raised = codes(
      /MixInvalid\(\s*"([a-z0-9_]+)"/g,
      source("src/services/target/versioned_mix.py") + source("src/services/target/category_mix.py"),
    );
    expect(raised).toEqual(
      expect.arrayContaining(["source_set", "bad_ratio", "sum_not_one", "not_a_list", "precision_loss"]),
    );
    for (const reason of new Set(raised)) {
      // The API sends it as `invalid_mix_<reason>` (`src/api/app.py`, `_mix`).
      const copy = sourceRefusalCopy(`invalid_mix_${reason}`, 400);
      expect(copy, reason).not.toBe(FALLBACK);
    }
    expect(NAMED).toContain("invalid_mix_source_set");
  });

  it("the past-posts refusals a switch or a connect can meet are raised by the API, and have sentences", () => {
    // `PastPostsRefused` also carries the sync and fence machinery's reasons
    // (observations, tombstone keys, stale captures, an allocation writer that is
    // missing), which no request of a browser can cause, so the fallback says
    // those ("this one is on us") and no sentence is owed. These three are what
    // turning a source on or off, or choosing history at connect, can say.
    const raised = new Set(codes(/PastPostsRefused\(\s*"([a-z0-9_]+)"\s*\)/g, pythonSource()));
    for (const reason of ["past_posts_not_activated", "invalid_past_posts_choice", "past_posts_source_unavailable"]) {
      expect(raised.has(reason), `${reason} is no longer raised`).toBe(true);
      expect(NAMED, `${reason} has no sentence`).toContain(reason);
    }
  });

  it("the connected-source change the provisioning layer refuses", () => {
    expect(pythonSource()).toMatch(/"sources_changed"/);
    expect(NAMED).toContain("sources_changed");
  });

  it("is a table with no sentence for a reason nothing sends", () => {
    // Each key is spelled somewhere authoritative: the Python services or this
    // tier's own routes (`cross_site`, `malformed_body`, `invalid_source`,
    // `invalid_workspace`).
    const authorities = [
      pythonSource(),
      source("landing/src/lib/route-guards.ts"),
      everything(filesUnder("landing/src/app/api", (name) => name === "route.ts")),
    ].join("\n");
    for (const reason of NAMED) {
      expect(authorities, `${reason} is spelled nowhere the API or the contract does`).toMatch(
        new RegExp(`\\b${reason}\\b`),
      );
    }
  });
});
