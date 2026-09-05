import { getSession, SessionUnavailableError, type SessionUser } from "./session";

/**
 * The three things an entry page can learn about the visitor, told apart.
 *
 * `getSession().catch(() => null)` folded two of them together, and the
 * fold was the bug: an API that is restarting (every merge redeploys it) read
 * as "not signed in", and the page sent a signed-in person to /login with a
 * perfectly good cookie. "We could not ask" and "the answer was no" send a
 * reader to opposite actions — the same reasoning `RouterUnavailable` records
 * for the workspace list — so they are separate outcomes here.
 */
export type EntrySession =
  | { kind: "session"; session: SessionUser }
  | { kind: "signed_out" }
  | { kind: "unavailable"; status: number };

export async function resolveEntrySession(
  get: () => Promise<SessionUser | null> = getSession,
): Promise<EntrySession> {
  try {
    const session = await get();
    return session ? { kind: "session", session } : { kind: "signed_out" };
  } catch (error) {
    if (error instanceof SessionUnavailableError) {
      return { kind: "unavailable", status: error.status };
    }
    throw error;
  }
}
