/**
 * The state badge must survive the trip to the screen (#1121 Step 0).
 *
 * WHY A SOURCE-READING TEST AND NOT A RENDER TEST. This defect is a REGRESSION,
 * and the shape of the regression is the reason: #1092 fixed the payload so
 * `state` reached the browser as a string, and the component then collapsed it
 * back to `state === "active"` and rendered the other three values as the
 * ABSENCE of a badge. So `destination.test.ts` can prove every state has a
 * distinct label and the screen can still discard all of it — the fix landing
 * at one tier and dying at the next is exactly what happened last time. There
 * is no render test to write it as: this package has no jsdom, no
 * `@testing-library`, and `vitest.config` sets `environment: "node"`. Adding
 * that stack is front-end infrastructure and belongs to whoever owns this
 * area, not to a two-line badge fix.
 *
 * WHAT THIS CAN AND CANNOT SEE, stated so a green tick is not read as more than
 * it is. It matches TEXT. It catches the literal prior form — the badge behind
 * an `isActive &&` guard — and it catches the helper being dropped. It CANNOT
 * catch a re-collapse spelled differently: a ternary, a differently named
 * boolean, an early `return null`, or CSS that hides the badge. It is a
 * tripwire on the way this broke once, not proof it cannot break again.
 *
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP — the
 * `intent-states-contract` / `session-cookie-contract` rule. A contract test
 * that quietly stops running when its counterparty moves is indistinguishable
 * from one that passes, which is the precise shape of the bug it exists to
 * catch.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";

const ACCOUNTS_TAB = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../components/dashboard/settings/accounts-tab.tsx",
);

function accountsTabSource(): string {
  try {
    return readFileSync(ACCOUNTS_TAB, "utf8");
  } catch (err) {
    throw new Error(
      `cannot read the accounts tab at ${ACCOUNTS_TAB} — the render contract ` +
        `is unverified, which is not the same as satisfied: ${err}`,
    );
  }
}

describe("the accounts tab renders destination state, not a boolean", () => {
  it("asks the helper for the badge", () => {
    // If this is gone, the screen is deriving its own presentation again and
    // everything `destination.test.ts` proves about the vocabulary is moot.
    expect(accountsTabSource()).toContain("destinationStateBadge(");
  });

  it("renders the label the helper returns", () => {
    expect(accountsTabSource()).toContain("stateBadge.label");
  });

  it("does not put the badge back behind the active boolean", () => {
    // The regression, verbatim: `{isActive && (<Badge …>Active</Badge>)}`.
    //
    // The lookbehind is load-bearing and the first version of this test was
    // wrong without it. `destinationIsActive` still has a legitimate caller on
    // this screen — `{!isActive && <Button>Make Active</Button>}`, which is a
    // genuine boolean question — and `isActive` is a substring of `!isActive`,
    // so the naive pattern failed on correct code. The guard being forbidden
    // is the POSITIVE one.
    expect(accountsTabSource()).not.toMatch(/(?<![!\w])isActive\s*&&/);
  });
});
