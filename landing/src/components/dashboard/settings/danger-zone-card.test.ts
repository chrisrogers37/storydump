import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));

import { DangerZoneCard, deletionConfirmed, restoreDeadlineCopy } from "./danger-zone-card";

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

/**
 * The card's one button, which is where focus has to end up (#1577).
 *
 * Rendered to a string, which is as far as this suite's `environment: "node"`
 * goes. It shows what each button is. It does not show focus moving: that
 * takes a DOM.
 */
describe("the card's button", () => {
  const card = (state: string) =>
    renderToString(
      createElement(DangerZoneCard, {
        workspaceId: "ws-1",
        workspaceName: "Northside Coffee",
        state,
        restorableUntil: "2026-11-08T15:00:00Z",
      }),
    );

  /** The opening tag of the button that holds exactly this label. */
  const button = (html: string, label: string) =>
    html.match(new RegExp(`<button[^>]*>${label}</button>`))?.[0] ?? "";

  it("opens the confirmation as its trigger, which is where Radix returns focus on close", () => {
    expect(button(card("active"), "Delete this workspace\\.\\.\\.")).toContain(
      'aria-haspopup="dialog"',
    );
  });

  it("is described, as Restore, by what deleting did: what is heard when focus lands on it", () => {
    const html = card("offboarding");
    const describedBy = button(html, "Restore workspace").match(/aria-describedby="([^"]+)"/)?.[1];
    expect(describedBy).toBeDefined();
    expect(html).toContain(`<p id="${describedBy}"`);
  });
});
