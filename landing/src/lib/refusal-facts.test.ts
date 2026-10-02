import { readFileSync, readdirSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";
import { isPlainCode, refusalFacts } from "./refusal-facts";

/**
 * What of a port refusal may reach the browser (#1413 phase 6).
 *
 * `readError` keeps only a refusal's reason, on purpose: a failed call's body
 * is where a token or a subject lands if anything upstream is careless. These
 * tests hold the one widening to its terms: the command route only, an
 * allow-list of keys with one type each, no free text, unknown keys dropped.
 * Every string that survives is a plain code.
 */

/** Every string anywhere in a value. */
function strings(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap(strings);
  if (value !== null && typeof value === "object") {
    return Object.values(value).flatMap(strings);
  }
  return [];
}

const FREE_TEXT = [
  "item 5 is locked", // a sentence
  "Bearer eyJhbGciOiJIUzI1NiJ9", // a credential
  "../../etc/passwd", // a path
  "Locked", // not lower-case
  "item-hold", // punctuation
  "x".repeat(65), // past the code bound
  "", // empty
];

describe("the allow-list", () => {
  it("passes each listed key with its one type", () => {
    const facts = {
      overridable: true,
      in_the_way: ["skip", "recent"],
      at_rule: "past",
      missing: "item",
      existing: { state: "scheduled", origin: "planned", cancel_requested: false },
    };
    expect(refusalFacts(facts)).toEqual(facts);
  });

  it("keeps a false boolean, which is a fact and not an absence", () => {
    expect(refusalFacts({ overridable: false })).toEqual({ overridable: false });
    expect(refusalFacts({ existing: { cancel_requested: false } })).toEqual({
      existing: { cancel_requested: false },
    });
  });
});

describe("free text never passes", () => {
  for (const text of FREE_TEXT) {
    it(`drops ${JSON.stringify(text.slice(0, 24))} wherever a code is expected`, () => {
      expect(
        refusalFacts({
          at_rule: text,
          missing: text,
          in_the_way: [text],
          existing: { state: text, origin: text },
        }),
      ).toBeNull();
    });
  }

  it("drops a whole list when one entry is not a code, rather than misstate what is in the way", () => {
    expect(refusalFacts({ in_the_way: ["skip", "two words"], overridable: true })).toEqual({
      overridable: true,
    });
  });

  it("leaves no string in its output that is not a plain code", () => {
    const out = refusalFacts({
      overridable: true,
      in_the_way: ["hold"],
      at_rule: "Bearer eyJhbGciOiJIUzI1NiJ9",
      missing: "account",
      detail: "item 5: hold",
      existing: { state: "awaiting_approval", origin: "a cadence story", note: "free" },
    });
    expect(strings(out).length).toBeGreaterThan(0);
    for (const s of strings(out)) expect(isPlainCode(s), s).toBe(true);
  });
});

describe("unknown keys are dropped", () => {
  it("at the top", () => {
    expect(
      refusalFacts({
        detail: "item 5 is locked",
        token: "sdt_abc",
        workspace_id: "11111111-1111-4111-8111-111111111111",
        overridable: true,
      }),
    ).toEqual({ overridable: true });
  });

  it("inside `existing`, the story's id included", () => {
    expect(
      refusalFacts({
        existing: {
          intent_id: "3f8a1c2e-5b4d-4e6f-9a1b-7c2d3e4f5a6b",
          workspace_id: "11111111-1111-4111-8111-111111111111",
          note: "x",
          state: "scheduled",
        },
      }),
    ).toEqual({ existing: { state: "scheduled" } });
  });
});

describe("a value of the wrong type is dropped", () => {
  it("key by key", () => {
    expect(
      refusalFacts({
        overridable: "true",
        in_the_way: "skip",
        at_rule: 7,
        missing: null,
        existing: { cancel_requested: 1, state: ["scheduled"] },
      }),
    ).toBeNull();
  });

  it("`existing` that is not an object", () => {
    for (const bad of [["scheduled"], "scheduled", 1, null]) {
      expect(refusalFacts({ existing: bad, at_rule: "past" }), String(bad)).toEqual({
        at_rule: "past",
      });
    }
  });

  it("facts that are not an object at all are none", () => {
    for (const bad of [null, undefined, "facts", 3, true, ["skip"]]) {
      expect(refusalFacts(bad), String(bad)).toBeNull();
    }
  });
});

describe("isPlainCode — the one shape a refusal's text may take", () => {
  it("accepts a lower-case code", () => {
    for (const code of ["locked", "item_hold", "a1"]) expect(isPlainCode(code), code).toBe(true);
  });

  it("refuses everything else", () => {
    for (const bad of [...FREE_TEXT, 7, null, undefined, ["locked"]]) {
      expect(isPlainCode(bad), String(bad)).toBe(false);
    }
  });
});

describe("only the command route asks for facts", () => {
  // `refusalFacts: true` is the one switch that lets facts cross, so where it
  // is thrown is pinned: in the command route and nowhere else in the app.
  const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

  function sources(dir: string): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) return sources(full);
      return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [full] : [];
    });
  }

  it("is the command route", () => {
    const asking = sources(SRC).filter((file) =>
      /refusalFacts:\s*true/.test(readFileSync(file, "utf8")),
    );
    expect(asking.map((file) => path.relative(SRC, file))).toEqual([
      path.join("app", "api", "workspaces", "[id]", "commands", "[command]", "route.ts"),
    ]);
  });
});
