import { describe, expect, it } from "vitest";

import { deletionConfirmed, restoreDeadlineCopy } from "./danger-zone-card";

/**
 * The two pure decisions behind the Delete / Restore card (#1127, `06` §1).
 *
 * Typing the workspace name is the front end's half of "owner (explicit,
 * confirmed)". It has to be an exact match of the name on screen: a
 * case-insensitive or trimmed-inside match would let "northside coffee" delete
 * "Northside Coffee", which is the kind of near-miss a confirmation exists to
 * catch. Outer whitespace is forgiven because a trailing space is a paste
 * artefact, not a different name.
 */
describe("deletionConfirmed", () => {
  it("accepts the exact name", () => {
    expect(deletionConfirmed("Northside Coffee", "Northside Coffee")).toBe(true);
  });

  it("forgives outer whitespace only", () => {
    expect(deletionConfirmed("  Northside Coffee ", "Northside Coffee")).toBe(true);
    expect(deletionConfirmed("Northside  Coffee", "Northside Coffee")).toBe(false);
  });

  it("is case-sensitive", () => {
    expect(deletionConfirmed("northside coffee", "Northside Coffee")).toBe(false);
  });

  it("never confirms against a blank name — a nameless workspace cannot be confirmed by typing nothing", () => {
    expect(deletionConfirmed("", "")).toBe(false);
    expect(deletionConfirmed("   ", "")).toBe(false);
  });
});

describe("restoreDeadlineCopy", () => {
  it("states the day the window closes, from the server's deadline", () => {
    expect(restoreDeadlineCopy("2026-10-02T15:00:00Z")).toBe("until 2026-10-02");
  });

  it("says the window exists without inventing a date when the server gave none", () => {
    expect(restoreDeadlineCopy(null)).toBe("until the grace period ends");
  });
});
