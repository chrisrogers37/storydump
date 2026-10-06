import { readFileSync, readdirSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";
import { IN_THE_WAY_MAX, isPlainCode, refusalFacts } from "./refusal-facts";

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
  "past\n", // a code with a trailing newline
  "locked\nBearer eyJhbGciOiJIUzI1NiJ9", // a code, then a second line
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

  // The cases above feed values the code SHAPE already refuses (hyphenated ids,
  // sentences), so they pin the shape, not the key list. These feed code-shaped
  // values, which only the key list can stop.
  it("drops a code-shaped value at a key the list does not name, at both levels", () => {
    expect(
      refusalFacts({
        intent_id: "abc123",
        workspace_id: "abc123",
        detail: "abc123",
        token: "abc123",
        overridable: true,
      }),
    ).toStrictEqual({ overridable: true });
    expect(
      refusalFacts({
        existing: { intent_id: "abc123", workspace_id: "abc123", note: "abc123", state: "scheduled" },
      }),
    ).toStrictEqual({ existing: { state: "scheduled" } });
  });

  it("drops a nested object under a key the list does not name, at both levels", () => {
    expect(refusalFacts({ extra: { state: "scheduled" }, overridable: true })).toStrictEqual({
      overridable: true,
    });
    expect(
      refusalFacts({ existing: { state: "scheduled", extra: { origin: "planned" } } }),
    ).toStrictEqual({ existing: { state: "scheduled" } });
  });
});

describe("in_the_way is bounded, all or none", () => {
  const codes = (n: number) => Array.from({ length: n }, (_, i) => `lock_${i}`);

  it("is bounded at 16", () => {
    expect(IN_THE_WAY_MAX).toBe(16);
  });

  it("passes a list at the bound whole", () => {
    expect(refusalFacts({ in_the_way: codes(IN_THE_WAY_MAX) })).toStrictEqual({
      in_the_way: codes(IN_THE_WAY_MAX),
    });
  });

  it("drops a longer list whole rather than cutting it short", () => {
    expect(refusalFacts({ in_the_way: codes(IN_THE_WAY_MAX + 1), overridable: true })).toStrictEqual({
      overridable: true,
    });
    expect(refusalFacts({ in_the_way: codes(100_000) })).toBeNull();
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
  // `targetFetch`'s `refusalFacts` option is the one switch that lets facts
  // cross. It is pinned by the property NAME, so any spelling of the value
  // (`true`, `!0`, a variable, a shorthand) is caught, not only the literal.
  const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

  /** Where the name is the definition: the option's own module and the allow-list's. */
  const DEFINING = [path.join("lib", "target-api.ts"), path.join("lib", "refusal-facts.ts")];

  function sources(dir: string): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) return sources(full);
      return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [full] : [];
    });
  }

  /** A line that uses the name as a property: not an import, not a call of the allow-list. */
  function asksForFacts(source: string): boolean {
    return source
      .split("\n")
      .some((line) => !/^\s*import\b/.test(line) && /\brefusalFacts\b(?!\s*\()/.test(line));
  }

  it("catches every spelling of the switch, and not the allow-list's own calls", () => {
    for (const asking of [
      "targetFetch(p, t, { refusalFacts: true });",
      "targetFetch(p, t, { refusalFacts: !0 });",
      "targetFetch(p, t, { refusalFacts });",
      'targetFetch(p, t, { "refusalFacts": on });',
      "init.refusalFacts = true;",
    ]) {
      expect(asksForFacts(asking), asking).toBe(true);
    }
    for (const other of [
      "const facts = refusalFacts(result.body.facts);",
      'import { refusalFacts, type RefusalFacts } from "./refusal-facts";',
    ]) {
      expect(asksForFacts(other), other).toBe(false);
    }
  });

  it("is the command route", () => {
    const asking = sources(SRC)
      .map((file) => path.relative(SRC, file))
      .filter((file) => !DEFINING.includes(file))
      .filter((file) => asksForFacts(readFileSync(path.join(SRC, file), "utf8")));
    expect(asking).toEqual([
      path.join("app", "api", "workspaces", "[id]", "commands", "[command]", "route.ts"),
    ]);
  });
});
