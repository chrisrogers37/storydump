import { describe, it, expect } from "vitest";
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import path from "path";
import {
  HISTORY_STATES,
  QUEUE_STATES,
  REVIEW_REQUIRED_STATE,
  SCHEDULED_STATES,
  TERMINAL_STATES,
} from "./dashboard-payloads";

/**
 * The intent vocabulary is a CROSS-TIER CONTRACT, and this tier partitions it.
 *
 * The API owns `INTENT_STATES`; this tier splits it into a queue half and a
 * terminal half and renders a count from the queue half under a plain label.
 * Nothing in TypeScript can see a Python tuple, so the split was free to go
 * stale — and did. `QUEUE_STATES` claimed to be "the pre-terminal members" and
 * omitted `review_required`, a reachable, operator-owned, explicitly
 * non-terminal state. "In Queue" undercounted by exactly the number of stuck
 * intents, with nothing on the page saying so.
 *
 * The old tests looked like they pinned the set and did not. They asserted
 * membership ("every name is real") and one-way exclusion ("no terminal state
 * is in the queue"). Both pass whether or not `review_required` is present,
 * because neither asks the completeness question. A set can be entirely valid
 * and still be missing something.
 *
 * So the assertion here is EXHAUSTIVENESS, not membership: every state the API
 * admits lands in exactly one half. That is the property that fails loudly on
 * the original defect, and on the next state anyone adds server-side.
 *
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP — the `session-cookie-
 * contract` rule, for the same reason. A contract test that quietly stops
 * running when its counterparty moves is indistinguishable from one that
 * passes, which is the precise shape of the bug it exists to catch.
 */
const API_WORKSPACES = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../src/services/target/workspaces.py",
);

function apiIntentStates(): string[] {
  let source: string;
  try {
    source = readFileSync(API_WORKSPACES, "utf8");
  } catch (err) {
    throw new Error(
      `cannot read the API's INTENT_STATES at ${API_WORKSPACES} — the ` +
        `contract is unverified, which is not the same as satisfied: ${err}`,
    );
  }

  // Anchored at column 0: the module-level tuple, not a local rebinding.
  const block = source.match(/^INTENT_STATES[^=]*=\s*\(([\s\S]*?)\n\)/m);
  if (!block) {
    throw new Error(
      `no module-level INTENT_STATES tuple found in ${API_WORKSPACES} — the ` +
        `constant moved or was renamed; the partition below is unverified`,
    );
  }

  const states = [...block[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  if (states.length === 0) {
    throw new Error(`INTENT_STATES parsed to an empty set in ${API_WORKSPACES}`);
  }
  return states;
}

const split = (s: string) => s.split(",").filter(Boolean);

describe("the intent-state partition agrees with the API", () => {
  it("reads the API's own constant, and fails if it cannot", () => {
    const states = apiIntentStates();
    // A canary on the parse itself: a regex that silently matched the wrong
    // block would hand back a plausible-looking list, so pin one member the
    // vocabulary cannot lose without this whole file needing rewriting.
    expect(states).toContain("scheduled");
    expect(new Set(states).size, "duplicate state in INTENT_STATES").toBe(
      states.length,
    );
  });

  it("accounts for EVERY state — the queue and terminal halves are exhaustive", () => {
    const api = new Set(apiIntentStates());
    const ours = new Set([...split(QUEUE_STATES), ...split(TERMINAL_STATES)]);

    const unaccounted = [...api].filter((s) => !ours.has(s));
    expect(
      unaccounted,
      `state(s) the API emits that this tier neither queues nor treats as ` +
        `terminal — they would vanish from every derived count silently`,
    ).toEqual([]);

    const invented = [...ours].filter((s) => !api.has(s));
    expect(
      invented,
      `state(s) named here that the API does not admit — a filter on one is ` +
        `a 422, not an empty list`,
    ).toEqual([]);
  });

  it("puts each state in exactly one half", () => {
    const queue = new Set(split(QUEUE_STATES));
    const overlap = split(TERMINAL_STATES).filter((s) => queue.has(s));
    expect(overlap, "state is both queued and terminal").toEqual([]);
  });

  it("counts review_required — the state the queue silently dropped", () => {
    // Named rather than folded into the exhaustiveness check above: that check
    // would also go green if review_required were moved to TERMINAL_STATES,
    // which is the same undercount wearing a different label. It is non-
    // terminal in `055`'s transition table (it exits to approved/posted/
    // failed/cancelled), so it belongs on the queue side specifically.
    expect(split(QUEUE_STATES)).toContain(REVIEW_REQUIRED_STATE);
    expect(split(TERMINAL_STATES)).not.toContain(REVIEW_REQUIRED_STATE);
  });

  it("keeps the history tab a subset of the terminal outcomes", () => {
    const terminal = new Set(split(TERMINAL_STATES));
    for (const state of split(HISTORY_STATES)) {
      expect(terminal.has(state), `${state} shown as history but not terminal`).toBe(
        true,
      );
    }
  });

  it("keeps the schedule strip a subset of the queue", () => {
    const queue = new Set(split(QUEUE_STATES));
    for (const state of split(SCHEDULED_STATES)) {
      expect(queue.has(state), `${state} on the schedule strip but not queued`).toBe(
        true,
      );
    }
  });
});
