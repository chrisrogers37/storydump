import { getSession, getSessionToken } from "./session";
import { targetFetch, type TargetResult } from "./target-api";

export type Workspace = {
  id: string;
  name: string;
  role: "owner" | "admin" | "member";
  state: "active" | "suspended" | "offboarding";
};

/** `GET /api/v1/workspaces/{id}` — the `02` §1 config row, typed columns only. */
export type WorkspaceConfig = {
  id: string;
  name: string;
  state: Workspace["state"];
  tz: string;
  posts_per_day: number;
  posting_hours_start: number;
  posting_hours_end: number;
  approval_mode: "manual" | "auto";
  auto_reapprove_returning: boolean;
  approval_ttl_minutes: number | null;
  dry_run_mode: boolean;
  is_paused: boolean;
  paused_at: string | null;
  repost_ttl_days: number | null;
  skip_ttl_days: number | null;
  caption_style: string | null;
  enable_ai_captions: boolean;
  api_publishing_enabled: boolean;
  offboarding_at: string | null;
  created_at: string;
  updated_at: string;
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

  // An EMPTY path means the workspace itself (`GET /workspaces/{ws}`), which
  // the settings and calendar screens read for the config columns. Without
  // this the concatenation produces a trailing slash and FastAPI answers a
  // redirect rather than the row — a failure that would look like an
  // unreachable router rather than a malformed path.
  const suffix = path ? `/${path}` : "";
  return targetFetch<T>(`/workspaces/${workspaceId}${suffix}`, token);
}

/**
 * The workspace's own config row, for a server component. Not through
 * `workspaceFetch`, whose path always names a child collection — this is the
 * parent. The queue reads two facts off it: `tz` (every slot renders in the
 * workspace's clock, never UTC) and `api_publishing_enabled` (whether Approve
 * is a button at all, `06` §3).
 */
export async function getWorkspaceConfig(
  workspaceId: string,
): Promise<TargetResult<WorkspaceConfig>> {
  const token = await getSessionToken();
  if (!token) return { ok: false, status: 401, error: "unauthenticated" };

  return targetFetch<WorkspaceConfig>(`/workspaces/${workspaceId}`, token);
}
