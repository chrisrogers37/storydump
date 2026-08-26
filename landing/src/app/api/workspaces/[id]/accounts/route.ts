import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isWorkspaceId } from "@/lib/session";
import { targetFetch } from "@/lib/target-api";

/**
 * POST /api/workspaces/[id]/accounts — add the Instagram destination this
 * workspace schedules for (#1089; the target route is #1053's
 * `POST /workspaces/{ws}/accounts`).
 *
 * ## Why this exists at all
 *
 * `connect_account` does NOT connect an Instagram account — it begins a Drive
 * connect for a SOURCE (#1079, gdrive epic P4). The vocabulary word made the
 * destination gap read as built, which is the defect #1089 records. A
 * destination is a different noun with a different route, and naming it that
 * way here is half the fix.
 *
 * ## Why REST and not a command
 *
 * F1 locked (b): commands for verbs on workspace state, REST for resources —
 * the same split the sibling `sources` route documents. A destination is a
 * resource and the vocabulary has no name for creating one.
 *
 * No `Idempotency-Key`, and not by omission: the target route is idempotent on
 * the destination by construction (`uq_ig_account_live`, inferred through the
 * partial index), so a second submit of the same handle returns the SAME row at
 * 200 rather than a second schedule against one real Instagram feed. There is
 * nothing for a submission key to add.
 *
 * ## Shape only, as the sibling route says
 *
 * WHAT a valid handle is belongs to the port (`provisioning.handle_from`),
 * which normalises a leading `@`, refuses interior whitespace, and caps at the
 * column's own 50. A second copy of that rule here is the copy that goes stale.
 * This checks only that a non-empty string arrived.
 */
export async function POST(
  request: NextRequest,
  context: { params: Promise<{ id: string }> },
) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });

  const { id } = await context.params;
  if (!isWorkspaceId(id)) {
    return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
  }

  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return NextResponse.json({ error: "malformed_body" }, { status: 400 });
  }

  const body = raw as { handle?: unknown };
  const handle = typeof body?.handle === "string" ? body.handle : "";
  // The emptiness check trims; the FORWARDED value does not. Normalising here
  // would be the second copy of the rule this file's header says belongs to the
  // port — and the port trims anyway. What this guard is for is answering an
  // empty submit without a round trip, not deciding what a handle is.
  if (!handle.trim()) {
    return NextResponse.json({ error: "handle_required" }, { status: 400 });
  }

  // `handle` only. The target derives the provisional `manual:<handle>`
  // reference itself — sending one from here would put the identity convention
  // in a second place, and the browser is the one caller that definitionally
  // cannot know a real Meta id.
  const result = await targetFetch<{ account_id?: string; created?: boolean }>(
    `/workspaces/${id}/accounts`,
    token,
    { method: "POST", body: JSON.stringify({ handle }) },
  );

  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }

  const accountId = result.data?.account_id;
  if (typeof accountId !== "string") {
    return NextResponse.json({ error: "malformed_response" }, { status: 502 });
  }

  // `created` distinguishes a new destination from the same handle submitted
  // twice. Passed through rather than flattened: "added" and "you already had
  // that one" are different sentences, and only the caller can say them.
  return NextResponse.json({ accountId, created: result.data?.created === true });
}
