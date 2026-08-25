import { getSessionToken } from "./session";
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
 * Returns the TargetResult rather than an array, deliberately. An array forces
 * every caller to represent "could not ask" as `[]`, and `[]` already means
 * "you belong to none" — a real, common, and completely different state that
 * every screen here has to render differently.
 */
export async function listWorkspaces(): Promise<TargetResult<Workspace[]>> {
  const token = await getSessionToken();
  if (!token) return { ok: false, status: 401, error: "unauthenticated" };

  const result = await targetFetch<{ workspaces: Workspace[] }>(
    "/workspaces",
    token,
  );
  if (!result.ok) return result;
  return { ok: true, data: result.data.workspaces ?? [] };
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
