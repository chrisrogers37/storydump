import { notAuthenticatedCopy } from "./refusal-copy";
/**
 * Destinations, browser side (#1089).
 *
 * A DESTINATION is the Instagram account a workspace schedules for. It is not
 * a source (a Drive folder), and it is not what `connect_account` connects —
 * that command begins a Drive connect and the name collision is the defect
 * #1089 records. This module is named for the noun so the confusion cannot be
 * re-made from the client side.
 *
 * Separate from `source-connect.ts` for the same reason: those functions are
 * about Drive, and folding a destination into them would put the two nouns back
 * in one file the day after the issue was filed for conflating them.
 */

export type AddDestinationResult =
  | { ok: true; accountId: string; created: boolean }
  | { ok: false; error: string; status: number };

/**
 * Add the Instagram handle this workspace posts to.
 *
 * One field, and that is the whole input in manual mode: `ig_accounts` needs
 * only a workspace and a reference, there is no credential and no Meta call,
 * and a workspace with `api_publishing_enabled` false publishes through a
 * person. Creating the destination is also what SCHEDULES it — the posting
 * cursor is seeded server-side, which is the only thing that makes the clock
 * able to see the row.
 *
 * Sends the handle ALONE. The provisional `manual:<handle>` reference is
 * derived by the port, because the browser definitionally cannot know a real
 * Meta id and a second copy of that convention is the copy that goes stale.
 *
 * No submission id: the target route is idempotent on the destination, so a
 * repeat returns the SAME row and `created` says which happened.
 */
export async function addDestination(
  workspaceId: string,
  handle: string,
): Promise<AddDestinationResult> {
  let response: Response;
  try {
    response = await fetch(`/api/workspaces/${workspaceId}/accounts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ handle }),
    });
  } catch {
    return { ok: false, error: "unreachable", status: 0 };
  }

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    // Two carriers, and the second is not optional. `error` is what THIS tier's
    // route handlers emit; `reason` is the port's own code carrier
    // (`target-api.ts`). Today the BFF re-wraps `reason` into `error`, so only
    // the first arm fires — but `command-client.ts` reads both, and a module
    // that reads one degrades a named refusal like `handle_malformed` into
    // `http_500` the moment any route passes a port body through unflattened.
    const error =
      typeof data?.error === "string"
        ? data.error
        : typeof data?.reason === "string"
          ? data.reason
          : `http_${response.status}`;
    return { ok: false, error, status: response.status };
  }
  if (typeof data?.accountId !== "string") {
    return { ok: false, error: "malformed_response", status: response.status };
  }
  return { ok: true, accountId: data.accountId, created: data.created === true };
}

/**
 * A sentence for a failed destination add.
 *
 * Every branch says what happened to the DATA, because "it failed" and "it
 * failed and nothing was written" are different facts to the person deciding
 * whether to press the button again.
 */
export function addDestinationRefusalCopy(reason: unknown): string {
  switch (reason) {
    case "handle_required":
      return "Type the Instagram handle this workspace posts to.";
    case "handle_malformed":
      return "A handle has no spaces in it. Enter just the username, without the @.";
    case "handle_too_long":
      return "That handle is too long. Enter just the username, without the @.";
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing was added.");
    case "insufficient_role":
    case "http_403":
      return "You need to be an admin of this workspace to add a destination.";
    case "unreachable":
    case "target_router_unreachable":
      return "Storydump cannot reach the server right now. Nothing was added — try again shortly.";
  }
  return "Could not add that destination. Nothing was added — try again shortly.";
}

/**
 * The display handle for a destination row.
 *
 * `handle` is the column a person typed into and is what should be shown. The
 * reference is the identity key and is deliberately NOT a fallback for display:
 * for a manual destination it renders as `manual:thehandle`, which is an
 * internal convention leaking into a screen. A row with no handle says so.
 */
export function destinationHandle(handle: string | null | undefined): string | null {
  const value = typeof handle === "string" ? handle.trim() : "";
  return value ? value : null;
}

/**
 * Whether a destination is one the clock will act on.
 *
 * Kept, and kept a BOOLEAN, because one caller genuinely asks a yes/no
 * question — whether to offer "Make Active" on a row that is not active. It is
 * NOT how the state is displayed: see `destinationStateBadge`.
 */
export function destinationIsActive(state: string | null | undefined): boolean {
  return state === "active";
}

/**
 * How a destination's state should read on screen.
 *
 * **Every state returns a badge, including one this build does not recognise.**
 * That is the whole point of the function rather than a defensive default.
 * `ck_ig_accounts_state` admits four values and the payload has carried the
 * string since #1092, but the screen collapsed it back to `state === "active"`
 * and rendered the other three as the ABSENCE of a badge — so `reauth_required`,
 * `disabled` and `moved` were indistinguishable from each other, and worse,
 * indistinguishable from a component that had not loaded or had thrown. A
 * boolean at least rendered "not active"; nothing renders as nothing (#1121).
 *
 * The distinction is carried by the LABEL, never by colour alone: `disabled`
 * and `moved` share a tone because neither is actionable, and a reader who
 * cannot see the difference between two greys can still read two words. Same
 * reason the source badge (#1065) prints its own state rather than a hardcoded
 * one.
 *
 * An unrecognised value is `attention`, not `inert`. The vocabulary is closed
 * by a CHECK constraint, so a value outside it means the schema moved or the
 * payload is wrong — both worth looking at, and neither is "nothing to see".
 *
 * Tone is semantic and deliberately carries no Tailwind: the class map belongs
 * to the component tier, so this stays unit-testable and the visual language
 * stays in one place.
 */
export type DestinationStateBadge = {
  label: string;
  tone: "active" | "attention" | "inert";
};

/** The vocabulary `ck_ig_accounts_state` admits. */
export type DestinationState = "active" | "reauth_required" | "disabled" | "moved";

/**
 * A `Record` keyed on the union rather than a `switch`, so adding a state to
 * `DestinationState` without giving it a badge is a COMPILE error rather than
 * a silent fall through to "Unknown state". The vocabulary is closed
 * server-side; this makes the screen's coverage of it closed too.
 */
const STATE_BADGE: Record<DestinationState, DestinationStateBadge> = {
  active: { label: "Active", tone: "active" },
  reauth_required: { label: "Reconnect needed", tone: "attention" },
  disabled: { label: "Disabled", tone: "inert" },
  moved: { label: "Moved", tone: "inert" },
};

export function destinationStateBadge(
  state: string | null | undefined,
): DestinationStateBadge {
  // The payload types `state` as a bare string, so an unknown value is
  // reachable at runtime even though the union is closed at compile time.
  return (
    STATE_BADGE[state as DestinationState] ?? {
      label: "Unknown state",
      tone: "attention",
    }
  );
}
