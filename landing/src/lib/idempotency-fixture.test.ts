import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import path from "path";
import { idempotencyKeyFor, parseCommand } from "./commands";

/**
 * The idempotency identities both doors derive, pinned once in
 * `tests/fixtures/idempotency_keys.json` and read by this test and by the
 * CLI's `tests/storydump_cli/test_writes.py`. Each case states the web's key
 * (`web`, null where the dashboard offers no such command) and the CLI's;
 * where they differ on purpose the case says why. Dedup is per channel and
 * principal, so the doors never replay each other — the fixture pins each
 * door's own rule against drift, not the doors against each other.
 */
const FIXTURE = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../tests/fixtures/idempotency_keys.json",
);

type Case = {
  command: string;
  body: Record<string, unknown>;
  web: string | null;
  cli: string;
  note?: string;
};

function cases(): Case[] {
  let text: string;
  try {
    text = readFileSync(FIXTURE, "utf8");
  } catch (err) {
    throw new Error(`cannot read the shared fixture at ${FIXTURE}: ${err}`);
  }
  return (JSON.parse(text) as { cases: Case[] }).cases;
}

describe("the web's idempotency keys match the shared fixture", () => {
  it("every case the web offers derives the fixture's key", () => {
    const offered = cases().filter((c) => c.web !== null);
    expect(offered.length).toBeGreaterThan(5);
    for (const c of offered) {
      const parsed = parseCommand(c.command, c.body);
      expect(parsed.ok, `${c.command} ${JSON.stringify(c.body)}`).toBe(true);
      if (!parsed.ok) continue;
      expect(idempotencyKeyFor(c.command, parsed.identity)).toBe(c.web);
    }
  });
  it("a case the web does not offer says so", () => {
    for (const c of cases().filter((x) => x.web === null)) {
      expect(parseCommand(c.command, c.body).ok, c.command).toBe(false);
    }
  });
});
