import { notAuthenticatedCopy, unreachableCopy } from "./refusal-copy";
import { isHttpsUrlOnHost } from "./redirect-guard";
import { requestGrant } from "./start-grant";
import type { GrantResult } from "./start-grant";
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

// ── The connect leg (#1220 step 2) ────────────────────────────────────────

/** The one host the browser may be sent to by this flow. */
const INSTAGRAM_AUTHORIZATION_HOST = "api.instagram.com";

/**
 * HTTPS, and Instagram's authorize host exactly — `source-connect.ts`'s guard
 * for the other provider, and for the same reason: the value is NAVIGATED TO.
 * Equality on `host`, never `endsWith`, so `evil-api.instagram.com` and
 * `api.instagram.com.evil.example` are both refused, and a non-default port
 * with them.
 */
export function isInstagramAuthorizationUrl(value: string): boolean {
  return isHttpsUrlOnHost(value, INSTAGRAM_AUTHORIZATION_HOST);
}

export type ConnectControl = { label: string; kind: "connect" | "reconnect" };

/** The one-line connection status beside a destination, from the same fact
 *  `connectControlFor` reads, so the caption and the control cannot disagree. */
export function destinationConnectionCaption(status: string | null | undefined): string {
  const control = connectControlFor(status);
  if (control === null) return "Instagram connected";
  return control.kind === "reconnect"
    ? "Instagram access needs reconnecting"
    : "Not connected to Instagram — posting is by hand until it is";
}

/**
 * Which control a destination row offers, from its credential status.
 *
 * `none` and an unrecognised value both offer Connect: a status this build
 * does not know is not evidence of a live connection. `active` offers
 * nothing — the badge beside the handle says connected, and a Reconnect
 * button on a live account invites people to re-consent for no reason.
 */
export function connectControlFor(status: string | null | undefined): ConnectControl | null {
  switch (status) {
    case "active":
      return null;
    case "expired":
    case "revoked":
      return { label: "Reconnect Instagram", kind: "reconnect" };
  }
  return { label: "Connect Instagram", kind: "connect" };
}

/** Where the browser goes to authorize, or why it cannot — `start-grant.ts`'s shape. */
export type DestinationConnectResult = GrantResult;

/**
 * Start the Instagram grant that ADDS a destination to this workspace (owner
 * ruling 2026-09-04): no account is named — Instagram says which account
 * signed in, and the callback adopts or creates the destination from that.
 */
export function requestWorkspaceConnect(workspaceId: string): Promise<DestinationConnectResult> {
  return requestGrant(`/api/workspaces/${workspaceId}/accounts/connect`, isInstagramAuthorizationUrl);
}

/** Connect or reconnect ONE existing destination. */
export function requestDestinationConnect(
  workspaceId: string,
  accountId: string,
): Promise<DestinationConnectResult> {
  return requestGrant(
    `/api/workspaces/${workspaceId}/accounts/${accountId}/connect`,
    isInstagramAuthorizationUrl,
  );
}

export function destinationConnectRefusalCopy(reason: unknown): string {
  switch (reason) {
    case "http_503":
      return "Instagram sign-in is not set up on this deployment yet. Nothing changed.";
    case "insufficient_role":
    case "http_403":
      return "You need to be an admin of this workspace to connect an Instagram account.";
    case "not found":
    case "http_404":
      return "That workspace or destination is no longer here. Reload the page.";
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing changed.");
    case "malformed_authorization_url":
      return "Storydump could not start the Instagram sign-in. Nothing changed — report this if it repeats.";
    case "unreachable":
    case "target_router_unreachable":
      return unreachableCopy("Nothing changed");
  }
  return "Could not start the Instagram sign-in. Nothing changed — try again shortly.";
}
