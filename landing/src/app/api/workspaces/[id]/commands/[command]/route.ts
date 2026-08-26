import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isWorkspaceId } from "@/lib/session";
import { targetFetch } from "@/lib/target-api";
import { idempotencyKeyFor, isOfferedCommand, parseCommand } from "@/lib/commands";

/**
 * POST /api/workspaces/[id]/commands/[command] — one command, forwarded to the
 * API's one write route (`POST /api/v1/workspaces/{ws}/commands/{command}`) with
 * the header it requires.
 *
 * This route holds no knowledge of any command's body. Every shape — including
 * the intent-id one it used to hard-code — is a row in `@/lib/commands`, which
 * parses the body and yields the identity the key is derived from. That is what
 * makes a settings write reachable here at all, and it is deliberate that the
 * intent shape is not privileged: a per-command schema with one shape still
 * special-cased beside the dispatcher would be the two-dialect defect surviving
 * inside its own fix (#1057/#1063 epic, F2).
 *
 * `Idempotency-Key` rides on EVERY call, not on the ones that look like they need
 * it: the port refuses a command without one (`v1.py:104-109`), so an omission
 * trades a 404 for a 400 and reads as a different bug.
 *
 * The command segment is checked against this tier's offered set, not the port's
 * whole vocabulary. The port re-validates the name, the role floor and the
 * transition; nothing is trusted from here.
 */
export async function POST(
  request: NextRequest,
  context: { params: Promise<{ id: string; command: string }> },
) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });

  const { id, command } = await context.params;
  if (!isWorkspaceId(id)) {
    return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
  }
  if (!isOfferedCommand(command)) {
    return NextResponse.json({ error: "unknown_command" }, { status: 404 });
  }

  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return NextResponse.json({ error: "malformed_body" }, { status: 400 });
  }

  const parsed = parseCommand(command, raw);
  if (!parsed.ok) {
    return NextResponse.json({ error: parsed.error }, { status: 400 });
  }

  const result = await targetFetch(`/workspaces/${id}/commands/${command}`, token, {
    method: "POST",
    body: JSON.stringify(parsed.body),
    headers: { "Idempotency-Key": idempotencyKeyFor(command, parsed.identity) },
  });

  // The port's 409s (`illegal_transition`, `manual_mode`) are normal answers
  // and ride through with their reason; the client turns them into a
  // sentence and re-reads the ledger.
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }
  return NextResponse.json(result.data);
}
