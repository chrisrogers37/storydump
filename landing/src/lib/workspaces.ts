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
 * Call a workspace-scoped target route.
 *
 * REPLACES `backendFetchJson`, and not only its credential. The old helper
 * returned `null` on failure and every caller wrote `data?.field ?? <zeros>`,
 * so an unreachable backend rendered as a dashboard of zeros — "you have posted
 * nothing", "your library is empty", stated with full confidence to someone
 * whose library is full. That is the failure direction nobody reports, because
 * it reads as good news rather than as a fault.
 *
 * So this returns the result and the caller must look at it. Rendering an
 * unavailable state is two lines; it just has to be possible to tell.
 */
export async function workspaceFetch<T = unknown>(
  path: string,
  workspaceId: string,
): Promise<TargetResult<T>> {
  const token = await getSessionToken();
  if (!token) return { ok: false, status: 401, error: "unauthenticated" };

  return targetFetch<T>(`/workspaces/${workspaceId}/${path}`, token);
}
