import { notAuthenticatedCopy, unreachableCopy } from "./refusal-copy";

/**
 * Every named refusal of the source experience, in one place: the posting mix
 * of protocol 2, a source's policy and the past-posts choice at connect. The
 * mix card, the policy editor and the connect dialog all say their refusals
 * through `sourceRefusalCopy`, so one reason reads the same on every surface
 * that can meet it.
 *
 * Each sentence says what was observed, what did not happen, and what to do.
 * None of them discards a draft: the card and the editor keep what was typed
 * whatever the reason, and say so where it matters (a conflict).
 *
 * `source-refusals.test.ts` reads the reasons back from the Python services
 * and fails when one has no sentence here.
 */

export type SourceRefusalSurface = "mix" | "policy" | "connect";

/** The editable age's limits, as the policy resource sent them (`bounds`). */
export type AgeBounds = { min: number; max: number };

const RELOAD = "Reload the page";

/** The reasons with a sentence of their own, by name. */
export const SOURCE_REFUSAL_COPY = {
  // ── The posting mix, protocol 2 ──────────────────────────────────────────
  mix_conflict:
    "The posting mix changed while you were working on it. The current mix is shown; review it, then try again. Nothing changed.",
  mix_protocol_required: `This page is older than the workspace's posting mix. ${RELOAD}, then make the change again. Nothing changed.`,
  mix_membership_change_requires_action:
    "Turning a source on or off is done with its switch, which shows the new equal shares first. Nothing changed.",
  invalid_mix_source_set: `The sources in the mix are not the ones this page sent. ${RELOAD} and review the mix. Nothing changed.`,
  mix_conversion_unavailable:
    "This workspace's posting mix cannot be changed from here yet. Nothing changed.",
  sources_changed: `The connected sources changed at the same time. ${RELOAD} and try again. Nothing changed.`,
  // ── A source's policy ────────────────────────────────────────────────────
  policy_conflict:
    "Someone saved these settings while you were editing. The saved values are shown beside yours; review them, then save again. Nothing changed.",
  invalid_source_policy:
    "Those settings were refused: the minimum age must be a whole number of days within the allowed range. Nothing changed.",
  unsupported_source_policy: `These settings are of a version this page cannot change. ${RELOAD}; if it persists, this one is on us and worth reporting. Nothing changed.`,
  source_not_found: `This source is no longer connected. ${RELOAD}. Nothing changed.`,
  unsupported_source_provider:
    "Only an Instagram past-posts source has these settings. Nothing changed.",
  // ── Past posts, wherever they are switched or chosen ─────────────────────
  archive_not_activated:
    "Using past posts is not switched on for this workspace yet. Nothing changed.",
  archive_source_unavailable: `This Instagram source is no longer available here. ${RELOAD}. Nothing changed.`,
  invalid_archive_choice: `The past-posts choice could not be read. ${RELOAD} and choose again. Nothing changed.`,
  history_setup_failed:
    "Instagram was not connected: setting up your past posts failed, so nothing from this connection was kept. Try connecting again; if it fails again, this one is on us and worth reporting.",
  // ── Refused by this tier's own route, before the API was asked ───────────
  cross_site: `This request did not come from Storydump's own page, so it was refused. ${RELOAD} and try again. Nothing changed.`,
  malformed_body:
    "Storydump could not read what this page sent. Nothing changed. Trying again may help — if it does not, this one is on us and worth reporting.",
  invalid_source: `This source could not be found. ${RELOAD}. Nothing changed.`,
  invalid_workspace: `This workspace could not be found. ${RELOAD}. Nothing changed.`,
} as const satisfies Record<string, string>;

export type SourceRefusalReason = keyof typeof SOURCE_REFUSAL_COPY;

/** A conflict at connect is a stale preview, not an edit to review. */
const CONNECT_MIX_CONFLICT =
  "The posting mix changed after this preview was shown. Look at the new preview, then connect again. Nothing changed.";

/**
 * A 2xx the page could not read. The change MAY have been made, so this is
 * the one sentence that does not say nothing changed.
 */
const MALFORMED_RESPONSE = `Storydump answered in a way this page could not read, so the change may or may not have been saved. ${RELOAD} to see the current values.`;

const NOT_AN_ADMIN = "You need to be an admin of this workspace to change this. Nothing changed.";

/**
 * What to show for a refused source call. *reason* is the BFF's `error`
 * string (`callBff`); *status* its HTTP status. *bounds*, when the policy
 * resource sent them, put the real limits into the invalid-age sentence.
 */
export function sourceRefusalCopy(
  reason: string,
  status: number,
  options: { surface?: SourceRefusalSurface; bounds?: AgeBounds | null } = {},
): string {
  if (reason === "mix_conflict" && options.surface === "connect") return CONNECT_MIX_CONFLICT;
  if (reason === "invalid_source_policy" && options.bounds) {
    const { min, max } = options.bounds;
    return `Those settings were refused: the minimum age must be a whole number of days from ${min.toLocaleString("en-US")} to ${max.toLocaleString("en-US")}. Nothing changed.`;
  }
  if (Object.hasOwn(SOURCE_REFUSAL_COPY, reason)) {
    return SOURCE_REFUSAL_COPY[reason as SourceRefusalReason];
  }
  if (reason === "malformed_response") return MALFORMED_RESPONSE;
  if (reason.startsWith("invalid_mix_")) {
    return "The mix was refused: each source that is on needs a share above 0%, and the shares must add up to 100%. Nothing changed.";
  }
  if (status === 401 || reason === "unauthenticated" || reason === "http_401") {
    return notAuthenticatedCopy("Nothing changed.");
  }
  if (status === 403 || reason === "insufficient_role") return NOT_AN_ADMIN;
  if (reason === "unreachable" || reason === "target_router_unreachable") {
    return unreachableCopy("Nothing changed");
  }
  if (status === 404) return `This could not be found. ${RELOAD}. Nothing changed.`;
  return "That did not work. Nothing changed — try again shortly; if it keeps failing, this one is on us.";
}
