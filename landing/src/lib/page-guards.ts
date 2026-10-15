import { notFound, redirect } from "next/navigation";
import { resolveEntrySession } from "./entry-session";
import { getSession, type SessionUser } from "./session";

/**
 * Every dashboard page's first four lines, and why they exist at all.
 *
 * Middleware already required a selected workspace to reach any route under
 * `/dashboard`. This is repeated because a page is reachable in tests and in
 * a direct render without it, and `activeWorkspaceId!` would be a non-null
 * assertion on a value that is legitimately null for every brand-new user.
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

/**
 * The owner area's one gate (#1723): the operator's session, or the
 * site's ordinary 404 for anyone else, a session the API could not resolve
 * included. Never a redirect to /login: sign-in lands on /welcome, so it
 * would not bring the owner back, and it would tell a stranger the area
 * exists.
 *
 * The layout calls it, and so does any page that acts before the API is
 * asked. Next renders a page in parallel with its layout, so a page cannot
 * lean on the layout's 404: `/ops`'s redirect, run beside it, would put its
 * target in a stranger's 404 page. A page that reads leans on the API's answer
 * instead (`require_ops`).
 */
export async function requireOpsPage(): Promise<SessionUser> {
  const entry = await resolveEntrySession();
  if (entry.kind !== "session" || !entry.session.isOps) notFound();
  return entry.session;
}
