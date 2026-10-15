/**
 * What an Instagram connect's callback applied, as the Settings banner says
 * it (the API's Result resource, #1644).
 *
 * The callback lands on `?connection=<id>`, an opaque id and nothing else, and
 * the page reads the result it names from the API, which answers only for the
 * person and session that started the connect, for fifteen minutes. Nothing
 * here trusts the query beyond its shape: a result that cannot be read, has
 * expired or is someone else's is no banner at all.
 *
 * A result is history. It says what the callback committed at revision
 * `mix_revision`; when the mix has moved on since (`current_mix_revision`),
 * the banner says so and points at the current status instead of presenting
 * the old outcome as today's.
 */

export const CONNECTION_OUTCOMES = [
  "connected_history_on",
  "connected_history_off",
  "connected_history_off_mix_conflict",
  "reconnected_preserved",
] as const;

export type ConnectionOutcome = (typeof CONNECTION_OUTCOMES)[number];

export type ConnectionResult = {
  outcome: ConnectionOutcome;
  /** The committed past-posts setting: what a reconnect kept. */
  historyEnabled: boolean;
  /** The mix revision the callback committed at. */
  mixRevision: number;
  /** The mix's revision now; null from an API that did not say. */
  currentMixRevision: number | null;
};

function revision(value: unknown): number | null {
  return Number.isSafeInteger(value) && (value as number) >= 0 ? (value as number) : null;
}

/** The result resource's answer, or null for anything that is not one. */
export function parseConnectionResult(data: unknown): ConnectionResult | null {
  if (typeof data !== "object" || data === null || Array.isArray(data)) return null;
  const raw = data as Record<string, unknown>;
  const outcome = CONNECTION_OUTCOMES.find((name) => name === raw.outcome);
  const mixRevision = revision(raw.mix_revision);
  if (!outcome || typeof raw.history_enabled !== "boolean" || mixRevision === null) return null;
  return {
    outcome,
    historyEnabled: raw.history_enabled,
    mixRevision,
    currentMixRevision: revision(raw.current_mix_revision),
  };
}

/**
 * Where the way on is, said wherever a past-posts setting that is Off is kept
 * through a reconnect: a reconnect is never
 * enablement, so Off stays Off until it is turned on in Posting mix. The dialog
 * before the redirect and the banner after it say the same words.
 */
export const TURN_ON_PAST_POSTS_COPY = "Turn past posts on in Posting mix.";

/** What happened, in the outcome's own sentence. */
export function connectionOutcomeCopy(result: ConnectionResult): string {
  switch (result.outcome) {
    case "connected_history_on":
      return "Instagram connected. Your past posts are now a content source — indexing has started.";
    case "connected_history_off":
      return "Instagram connected for publishing. Past posts are Off; you can turn them on in Posting mix.";
    case "connected_history_off_mix_conflict":
      return "Instagram connected. Past posts were left Off because the posting mix changed while you were connecting.";
    case "reconnected_preserved":
      return `Instagram reconnected. Your past-posts setting was kept: ${
        result.historyEnabled ? "On." : `Off. ${TURN_ON_PAST_POSTS_COPY}`
      }`;
  }
}

/** Said beside a result the mix has moved past since it was recorded. */
export const SUPERSEDED_COPY =
  "The posting mix has changed since; its current status is shown in Posting mix.";

export function isSuperseded(result: ConnectionResult): boolean {
  return result.currentMixRevision !== null && result.currentMixRevision !== result.mixRevision;
}
