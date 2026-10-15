/**
 * The state badge must survive the trip to the screen (#1121).
 *
 * WHY A SOURCE-READING TEST AND NOT A RENDER TEST. The payload carries `state`
 * to the browser as a string (#1092), and a component can collapse it to
 * `state === "active"` and render the other three values as the ABSENCE of a
 * badge. So `destination.test.ts` can prove every state has a distinct label
 * and the screen can still discard all of it. There is no render test to
 * write it as: this package has no jsdom, no `@testing-library`, and
 * `vitest.config` sets `environment: "node"`.
 *
 * WHAT THIS CAN AND CANNOT SEE, stated so a green tick is not read as more than
 * it is. It matches TEXT. It catches the literal form — the badge behind an
 * `isActive &&` guard — and it catches the helper being dropped. It CANNOT
 * catch a collapse spelled differently: a ternary, a differently named
 * boolean, an early `return null`, or CSS that hides the badge. It is a
 * tripwire on one way this breaks, not proof it cannot break.
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
    // If this is gone, the screen is deriving its own presentation and
    // everything `destination.test.ts` proves about the vocabulary is moot.
    expect(accountsTabSource()).toContain("destinationStateBadge(");
  });

  it("renders the label the helper returns", () => {
    expect(accountsTabSource()).toContain("stateBadge.label");
  });

  it("does not put the badge behind the active boolean", () => {
    // The form it forbids, verbatim: `{isActive && (<Badge …>Active</Badge>)}`.
    //
    // The lookbehind is load-bearing. `isActive` is a substring of
    // `!isActive`, so the naive pattern fails on correct code: the guard being
    // forbidden is the POSITIVE one. The lookbehind makes this case say "no
    // positive guard" rather than "no mention of isActive", so a `!isActive` on
    // this screen is not the thing that turns the badge into a boolean.
    expect(accountsTabSource()).not.toMatch(/(?<![!\w])isActive\s*&&/);
  });
});
