/**
 * What a media source's CONNECTION says, as opposed to its operating state.
 *
 * These are two axes and collapsing them is the defect this file exists for.
 * `media_sources.state` is `NOT NULL DEFAULT 'active'` (migration 054), so a
 * folder added and never granted is `active` — and the screen rendered that as
 * a green badge. The first real user was told, by the product, that an
 * unauthorised folder was connected.
 *
 * `credential_status` (#1080) answers the other question and is DERIVED at the
 * port rather than passed through, because `state` "would be a field that
 * cannot say the interesting thing" (`workspaces.py:210-227`). Its four values
 * are not a boolean: `none` is CONNECT, while `expired` and `revoked` are
 * RECONNECT — different user actions, which is exactly the distinction this
 * screen could not previously make.
 *
 * The invariant, pinned by a test rather than by this paragraph: **only
 * `active` is ever green.** The prose version of this rule already existed, one
 * file over, and did not stop the badge shipping wrong.
 */

export type SourceCredentialBadge = {
  label: string;
  tone: "active" | "attention" | "inert";
};

/** The vocabulary the port derives. */
export type CredentialStatus = "none" | "active" | "expired" | "revoked";

/**
 * A `Record` keyed on the union rather than a `switch`, so adding a status
 * without giving it a badge is a COMPILE error rather than a silent fall
 * through — the same construction `destinationStateBadge` uses, and for the
 * same reason.
 *
 * `expired` and `revoked` share a TONE because they share a remedy, and keep
 * distinct LABELS because they do not share a cause. Someone whose access was
 * revoked did something; someone whose token expired did not.
 */
const CREDENTIAL_BADGE: Record<CredentialStatus, SourceCredentialBadge> = {
  none: { label: "Awaiting Google access", tone: "attention" },
  active: { label: "Connected", tone: "active" },
  expired: { label: "Access expired — reconnect", tone: "attention" },
  revoked: { label: "Access revoked — reconnect", tone: "attention" },
};

export function sourceCredentialBadge(
  status: string | null | undefined,
): SourceCredentialBadge {
  // The payload types this as a bare string, so an unknown value is reachable
  // at runtime even though the union is closed at compile time. `inert`, not
  // `attention`: an unrecognised value is not evidence of a problem. It is
  // emphatically not `active` — the one tone that would repeat the defect.
  return (
    CREDENTIAL_BADGE[status as CredentialStatus] ?? {
      label: "Connection status unknown",
      tone: "inert",
    }
  );
}
