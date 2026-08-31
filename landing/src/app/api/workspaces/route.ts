import { NextRequest, NextResponse } from "next/server";
import { idempotencyKeyFor } from "@/lib/commands";
import { getSessionToken } from "@/lib/session";
import { targetFetch } from "@/lib/target-api";

export type Workspace = {
  id: string;
  name: string;
  role: "owner" | "admin" | "member";
  state: "active" | "suspended" | "offboarding";
};

/** GET /api/workspaces — the ones this user is a member of. Possibly none. */
export async function GET() {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });

  const result = await targetFetch<{ workspaces: Workspace[] }>(
    "/workspaces",
    token,
  );

  // An empty list and an unreachable router are NOT the same answer and must
  // not share a status. "You have no workspaces yet" sends someone to create
  // one; "we could not ask" must not.
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }
  return NextResponse.json({ workspaces: result.data.workspaces });
}

/**
 * POST /api/workspaces — create one, with this user as its owner.
 *
 * ATOMICITY IS THE ROUTER'S AND IT IS NOT OPTIONAL. `ct_workspaces_owner_at_insert`
 * is a DEFERRED constraint trigger, so a workspace with no owner row does not
 * fail at the INSERT — it fails at COMMIT. Creating the workspace and the
 * membership in two transactions therefore does not half-work; it fails, late,
 * with an error that points at neither statement.
 */
export async function POST(request: NextRequest) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });

  let name: unknown;
  try {
    ({ name } = await request.json());
  } catch {
    return NextResponse.json({ error: "malformed_body" }, { status: 400 });
  }

  // `workspaces.name` is VARCHAR(100) NOT NULL. Checked here so a blank field
  // is a field error next to the input rather than a 500 from a constraint.
  const trimmed = typeof name === "string" ? name.trim() : "";
  if (!trimmed || trimmed.length > 100) {
    return NextResponse.json({ error: "invalid_name" }, { status: 400 });
  }

  // `Idempotency-Key` is REQUIRED, not optional. `POST /workspaces` reaches the
  // port through `_dispatch`, whose first statement refuses a keyless command
  // (`v1.py:104-109`) — before the body is even read. Omitting it here returned a
  // 400 that `createWorkspaceRefusalCopy` could not name, so the first real user saw only "That
  // did not work. This one is on us." and nothing was written.
  //
  // Identity is the trimmed NAME, which is the only thing stable across a retry
  // of this submission — the workspace id is minted server-side per request, so
  // it cannot serve. Dedup is scoped `(channel, principal, external_ref)`, so the
  // name never collides across users, and `principal` is the SESSION: a
  // double-click replays, while a deliberate second workspace of the same name
  // later is a new session and is created.
  const result = await targetFetch<Workspace>("/workspaces", token, {
    method: "POST",
    body: JSON.stringify({ name: trimmed }),
    headers: { "Idempotency-Key": idempotencyKeyFor("create_workspace", trimmed) },
  });

  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }
  return NextResponse.json(result.data, { status: 201 });
}
