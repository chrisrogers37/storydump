import { redirect } from "next/navigation";
import { getSession, type SessionUser } from "./session";

/**
 * Every dashboard page's first four lines, and why they exist at all.
 *
 * Middleware already required a selected workspace to reach any route under
 * `/dashboard`. This is repeated because a page is reachable in tests and in
 * a direct render without it, and `activeWorkspaceId!` would be a non-null
 * assertion on a value that is legitimately null for every brand-new user.
 *
 * Five pages carried this, four of them with that paragraph pasted verbatim
 * — which is the tell: a comment worth writing once was written four times
 * and could have been edited in one of them.
 *
 * `getSession` is `cache()`d, so calling it here costs no extra JWT
 * verification even though the layout has already called it; and
 * `redirect()` throws, so the return type has no null arm.
 */
export async function requireWorkspacePage(): Promise<{
  session: SessionUser;
  workspaceId: string;
}> {
  const session = await getSession().catch(() => null);
  if (!session) redirect("/login");
  const workspaceId = session.activeWorkspaceId;
  if (!workspaceId) redirect("/welcome");
  return { session, workspaceId };
}
