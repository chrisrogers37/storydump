import { NextRequest, NextResponse } from "next/server";
import { admissionOutcome, MAX_EMAIL_CHARS } from "@/lib/ops-waitlist";
import {
  passThrough,
  readJsonBody,
  refuseCrossSite,
  requireSessionToken,
} from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

/**
 * POST /api/admissions — the owner admits an address, or with `resend` emails
 * it again (#1723): the API's `POST /admissions`, for the people in
 * `OPS_USER_IDS`.
 *
 * The API decides who may (`require_ops`: `not_ops`, 403) and what an address
 * is (`invalid_email`). This route checks the body's shape first, one address
 * of at most 254 characters and a boolean, and forwards those two fields and
 * nothing else. The address travels in the body alone, never in a URL.
 *
 * Its answer is the outcome the API named, or the API's refusal with its
 * reason and status. A 2xx that cannot be read, or that names no outcome this
 * tier knows (`admissionOutcome`), is a 502 `malformed_response` (#1740): the
 * admission may have been made and its email queued, so it is neither a
 * success nor a refusal, and the page says it could not confirm.
 */
export async function POST(request: NextRequest) {
  const refused = refuseCrossSite(request);
  if (refused) return refused;

  const token = await requireSessionToken();
  if (token instanceof NextResponse) return token;

  const parsedBody = await readJsonBody(request);
  if (parsedBody instanceof NextResponse) return parsedBody;
  // A body that is not an object has no boolean `resend`, so this one check
  // refuses it too.
  const { email, resend } = (parsedBody.raw ?? {}) as { email?: unknown; resend?: unknown };
  if (typeof resend !== "boolean") {
    return NextResponse.json({ error: "malformed_body" }, { status: 400 });
  }
  // Trimmed as the API trims, and bounded by whole characters, as the
  // waitlist form's route bounds it. What an address is stays the API's call.
  const address = typeof email === "string" ? email.trim() : "";
  if (address === "" || [...address].length > MAX_EMAIL_CHARS) {
    return NextResponse.json({ error: "invalid_email" }, { status: 400 });
  }

  const result = await targetFetch<unknown>("/admissions", token, {
    method: "POST",
    body: JSON.stringify({ email: address, resend }),
  });
  if (!result.ok) {
    // `malformed_response` with a 2xx is the client's word for an answer it
    // could not read: relayed, its 2xx would reach the page as a success.
    // Every other failure passes through as it came: the API's refusal, or the
    // client's own answer that the API could not be reached.
    const unread =
      result.error === "malformed_response" && result.status >= 200 && result.status < 300;
    return unread ? unconfirmed() : passThrough(result);
  }
  const outcome = admissionOutcome(result.data);
  return outcome === null ? unconfirmed() : NextResponse.json({ outcome });
}

/** The answer for an API answer that names no outcome: not a yes, and not a refusal. */
function unconfirmed(): NextResponse {
  return NextResponse.json({ error: "malformed_response" }, { status: 502 });
}
