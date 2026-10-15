import { invitationsForYouFrom, type MyInvitations } from "./invitations";
import { getSession, getSessionToken } from "./session";
import { targetFetch, type TargetResult } from "./target-api";

export type Workspace = {
  id: string;
  name: string;
  role: "owner" | "admin" | "member";
  state: "active" | "suspended" | "offboarding";
};

/**
 * The workspaces this user belongs to, for a server component.
 *
 * Reads the session rather than calling `GET /workspaces`, because
 * `GET /api/v1/me` already returned the memberships alongside the user — one
 * round trip instead of two, and one place where the null-versus-empty
 * distinction has to be got right instead of two that can disagree.
 *
 * Returns the TargetResult rather than an array, deliberately. An array forces
 * every caller to represent "could not ask" as `[]`, and `[]` already means
 * "you belong to none" — a real, common, and completely different state that
 * every screen here has to render differently.
 */
export async function listWorkspaces(): Promise<TargetResult<Workspace[]>> {
  const session = await getSession().catch(() => null);
  if (!session) return { ok: false, status: 401, error: "unauthenticated" };

  // NULL IS NOT AN EMPTY LIST, and this is the one place that could quietly
  // turn it into one. `GET /api/v1/me` answers `workspaces: null` with the
  // reason in `degraded` when the membership list cannot be read — the API's
  // own words: "so a front end can say 'cannot list your workspaces' instead
  // of 'you have none'." Returning `[]` here would spend that care at the last
  // hop and render "create your first workspace" to an owner of six.
  if (session.workspaces === null) {
    return {
      ok: false,
      status: 503,
      error: session.degraded[0] ?? "membership_list_unreadable",
    };
  }

  return { ok: true, data: session.workspaces as Workspace[] };
}

/**
 * The invitations addressed to this user that they could still accept
 * (`GET /api/v1/me/invitations`, `fn_invitations_for_invitee`), for /welcome and /workspaces, or null
 * when they could not be read: "we couldn't check" and "there are none" are
 * different screens, as with the workspaces above.
 */
export async function listMyInvitations(): Promise<MyInvitations | null> {
  const token = await getSessionToken();
  if (!token) return null;
  const result = await targetFetch<unknown>("/me/invitations", token);
  return result.ok ? invitationsForYouFrom(result.data) : null;
}

/**
 * Call a workspace-scoped target route.
 *
 * It returns the result, never `null` on failure: a `null` that every caller
 * reads as `data?.field ?? <zeros>` renders an unreachable backend as a
 * dashboard of zeros — "you have posted nothing", "your library is empty",
 * stated with full confidence to someone whose library is full. That is the
 * failure direction nobody reports, because it reads as good news rather than
 * as a fault.
 *
 * So the caller must look at the result. Rendering an unavailable state is two
 * lines; it just has to be possible to tell.
 */
export async function workspaceFetch<T = unknown>(
  path: string,
  workspaceId: string,
): Promise<TargetResult<T>> {
  const token = await getSessionToken();
  if (!token) return { ok: false, status: 401, error: "unauthenticated" };

  // An EMPTY path means the workspace itself (`GET /workspaces/{ws}`), which
  // the settings and calendar screens read for the config columns. Without
  // this the concatenation produces a trailing slash and FastAPI answers a
  // redirect rather than the row — a failure that would look like an
  // unreachable router rather than a malformed path.
  const suffix = path ? `/${path}` : "";
  return targetFetch<T>(`/workspaces/${workspaceId}${suffix}`, token);
}
