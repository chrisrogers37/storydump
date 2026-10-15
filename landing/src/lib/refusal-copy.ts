/**
 * The sentence every surface shows when the server did not accept the
 * credential (#1140).
 *
 * ONE function, because the risk is not a bad string — it is six chances to
 * write one. `if (status === 401) return "Your session expired. Sign in
 * again."` names ONE of roughly seven causes a 401 admits: expired · revoked ·
 * never sent · malformed · wrong audience · clock skew · server could not
 * verify. For the "never sent" branch that remedy is not merely unhelpful but
 * impossible, because a cookie unreadable on this origin is equally unreadable
 * after signing in again.
 *
 * **The honest word is the code's own.** The reason these branches match on
 * is `unauthenticated` — the one word true of all seven — and the sentence
 * keeps to it, never `expired`, a claim about WHY that nothing establishes.
 *
 * The shape is the one every OTHER branch in those same switch statements
 * already gets right, derived from this repo's own working strings rather than
 * imported from anywhere: what was OBSERVED, what did NOT happen, and a remedy
 * that is hedged or escalated instead of stated flat.
 *
 * **Why a shared function and not six corrected strings.** Adding a `case:`
 * means writing one line, and the honest siblings two lines above are only
 * examples — an example is seen by whoever reads the whole function, which is
 * exactly what editing one branch does not require. The correct version also
 * costs strictly more keystrokes than the wrong one. Single-sourcing removes
 * the choice at the moment it would be made.
 */

/**
 * What did NOT happen — the part that stops a retry loop, because it tells
 * someone whether pressing the button again repeats or duplicates.
 *
 * A closed union rather than `string`, and this is the only compiler-enforced
 * guarantee the shape actually admits. Whether a sentence is true of seven
 * causes is not checkable by a type; whether every caller supplied an outcome,
 * and whether it is one of the three phrasings this product uses, is. A seventh
 * site cannot quietly introduce "Nothing happened." — it will not compile.
 */
export type RefusalOutcome =
  | "Nothing was created."
  | "Nothing was added."
  | "Nothing changed.";

/**
 * The sentence for "the browser could not reach the app at all" — the sibling
 * of `notAuthenticatedCopy`, single-sourced for the same reason: one copy, not
 * one per refusal table. *outcome* is what did NOT happen
 * ("Nothing changed", "Nothing was added").
 */
export function unreachableCopy(outcome: string): string {
  return `Storydump cannot reach the server right now. ${outcome} — try again shortly.`;
}

/**
 * The sentence for a `replayed` answer (`REPLAYED_ERROR`): the write did not
 * happen, AND the app sent it under a key the server had already seen, so the
 * key mechanism is wrong. The remedy is a fresh try, and a report if it repeats.
 */
export function replayedCopy(): string {
  return "That did not go through — the app sent it under a key the server had already seen. Reload and try again; report this if it repeats.";
}

/**
 * Observation · outcome · hedged remedy · escalation.
 *
 * "may help" rather than "Sign in again": a remedy is a claim too, and for at
 * least one of the seven causes signing in again cannot work. Where the code
 * did not establish which, an unhedged imperative asserts something it does not
 * know — the same defect one clause later.
 */
export function notAuthenticatedCopy(outcome: RefusalOutcome): string {
  return (
    "You are not signed in, or the app could not prove it. " +
    outcome +
    " Signing in again may help — if it does not, this one is on us and" +
    " worth reporting."
  );
}

/**
 * A sentence for a failed workspace create.
 *
 * Lives here rather than beside the form, which is what makes it testable:
 * the component is `"use client"` and pulls React, `next/navigation` and an
 * icon set, so a node-environment test cannot import it to check one string.
 * Its sibling tables live in `lib/` too.
 *
 * The failure states stay separated because their remedies are opposite —
 * `invalid_name` is the person's to fix, `target_router_unreachable` is not
 * theirs at all — and the final fallback names no cause and takes the blame.
 *
 * The fallback is for a cause this function cannot name. A refusal whose cause
 * the app does know gets its own arm: `malformed_body` is decided by the route,
 * before the port is asked, so it can say that nothing was created. Rendered as
 * the fallback, a known cause is indistinguishable from an unknown one on the
 * screen — the one artifact a report usually carries.
 */
export function createWorkspaceRefusalCopy(reason: unknown, status: number): string {
  if (reason === "invalid_name") return "Give the workspace a name.";
  if (reason === "malformed_body") {
    return (
      "Storydump could not read what this form sent. Nothing was created." +
      " Trying again may help — if it does not, this one is on us and worth" +
      " reporting."
    );
  }
  if (status === 503 || reason === "target_router_unreachable") {
    return "Storydump cannot create workspaces yet. Nothing you did — check back shortly.";
  }
  if (status === 401) return notAuthenticatedCopy("Nothing was created.");
  return "That did not work. This one is on us.";
}
