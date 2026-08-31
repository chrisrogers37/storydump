/**
 * The sentence every surface shows when the server did not accept the
 * credential (#1140).
 *
 * ONE function, because the defect was never a bad string — it was six chances
 * to write one. `if (status === 401) return "Your session expired. Sign in
 * again."` names ONE of roughly seven causes a 401 admits: expired · revoked ·
 * never sent · malformed · wrong audience · clock skew · server could not
 * verify. On the first delivered sign-in this product has ever had it named the
 * wrong one — the sessions were REVOKED, with `expires_at` thirty days out —
 * and the person did exactly what the sentence told him, twice. It could never
 * have worked: for the "never sent" branch the prescribed remedy is not merely
 * unhelpful but impossible, because a cookie unreadable on this origin is
 * equally unreadable after signing in again.
 *
 * **The honest word was already in the code.** The reason these branches match
 * on is `unauthenticated` — the one word true of all seven — and the display
 * layer replaced it with `expired`, a claim about WHY that nothing established.
 * This is not a new error vocabulary; it is the identifier the code already
 * had, not discarded at the last step.
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
 * Lives here rather than beside the form, and the move is what makes it
 * testable: the component is `"use client"` and pulls React, `next/navigation`
 * and an icon set, so a node-environment test cannot import it to check one
 * string. Five of the six sites this issue names were already in `lib/`; this
 * puts the sixth where its siblings are, with no behaviour change.
 *
 * The failure states stay separated because their remedies are opposite —
 * `invalid_name` is the person's to fix, `target_router_unreachable` is not
 * theirs at all — and the final fallback is deliberately untouched: it names no
 * cause and takes the blame, which is what the rest of this now does too.
 */
export function createWorkspaceRefusalCopy(reason: unknown, status: number): string {
  if (reason === "invalid_name") return "Give the workspace a name.";
  if (status === 503 || reason === "target_router_unreachable") {
    return "Storydump cannot create workspaces yet. Nothing you did — check back shortly.";
  }
  if (status === 401) return notAuthenticatedCopy("Nothing was created.");
  return "That did not work. This one is on us.";
}
