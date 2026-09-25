/**
 * The overview's condition panel and the tab row it links to say the same
 * words about the same thing (`conditions.ts`) — only because both call the
 * same helper. The panel's side is tested in `conditions.test.ts`; these pin
 * the row's side, at the two call sites where the words are the property:
 * the Accounts row's title and the Drive card's state badge.
 *
 * WHY SOURCE-READING. Reverting either call site to an inline expression
 * keeps tsc, lint and every other test green — the helpers stay imported and
 * stay tested — so nothing else notices the two surfaces drifting apart. The
 * `destination-badge-contract` shape, and its bound: this matches TEXT. It
 * catches the call being dropped; it cannot catch the call made and its
 * result discarded.
 *
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";

const SETTINGS = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../components/dashboard/settings",
);

function source(file: string): string {
  const at = path.join(SETTINGS, file);
  try {
    return readFileSync(at, "utf8");
  } catch (err) {
    throw new Error(
      `cannot read ${at} — the same-words contract is unverified, which is ` +
        `not the same as satisfied: ${err}`,
    );
  }
}

describe("the rows the condition panel links to use the panel's words", () => {
  it("the Accounts row is titled with the shared destination name", () => {
    expect(source("accounts-tab.tsx")).toContain("{destinationName(account)}");
  });

  it("the Drive card's badge prints the shared source-state label", () => {
    expect(source("drive-card.tsx")).toContain("{sourceStateLabel(source.state)}");
  });
});
