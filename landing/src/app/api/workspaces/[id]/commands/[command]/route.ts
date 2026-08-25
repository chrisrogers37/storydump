import { NextRequest, NextResponse } from "next/server";
import { getSessionToken, isWorkspaceId } from "@/lib/session";
import { targetFetch } from "@/lib/target-api";
import { idempotencyKeyFor, isIntentId, isQueueCommand } from "@/lib/intents";

/**
 * POST /api/workspaces/[id]/commands/[command] — one intent command, forwarded
 * to the API's one write route (`POST /api/v1/workspaces/{ws}/commands/{command}`)
 * with the header it requires.
 *
 * THE KEY IS MINTED HERE, never by the browser. `Idempotency-Key` exists so a
 * repeated request executes once; a client that minted its own would mint a
 * fresh one per click, which is exactly the double execution the header is
 * for. Stable per (command, intent), so the second click is a `200 replayed`
 * and a refused first try — its dedup row rolled back with everything else —
 * is still retryable.
 *
 * The command segment is checked against the QUEUE's allowlist, not the
 * port's vocabulary: this surface renders a refusal sentence for exactly four
 * commands, and forwarding a fifth would be a button this page does not have.
 * The port re-validates the name, the role floor and the transition itself;
 * nothing is trusted from here.
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
  if (!isQueueCommand(command)) {
    return NextResponse.json({ error: "unknown_command" }, { status: 404 });
  }

  let intentId: unknown;
  try {
    ({ intent_id: intentId } = await request.json());
  } catch {
    return NextResponse.json({ error: "malformed_body" }, { status: 400 });
  }
  if (!isIntentId(intentId)) {
    return NextResponse.json({ error: "invalid_intent" }, { status: 400 });
  }

  const result = await targetFetch<Record<string, unknown>>(
    `/workspaces/${id}/commands/${command}`,
    token,
    {
      method: "POST",
      body: JSON.stringify({ intent_id: intentId }),
      headers: { "Idempotency-Key": idempotencyKeyFor(command, intentId) },
    },
  );

  // The port's 409s (`illegal_transition`, `manual_mode`) are normal answers
  // and ride through with their reason; the client turns them into a
  // sentence and re-reads the ledger.
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }
  return NextResponse.json(result.data);
}
