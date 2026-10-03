import { NextRequest, NextResponse } from "next/server"
import { refuseCrossSite } from "@/lib/route-guards"
import { targetFetch } from "@/lib/target-api"
import { notifyAdmin } from "@/lib/telegram"
import { UTM_KEYS } from "@/lib/analytics"

/** The API's bounds, in characters: an address, and each campaign value. */
const MAX_EMAIL_CHARS = 254
const MAX_UTM_CHARS = 100
/** How long the form waits on the API before answering the generic error. */
const API_TIMEOUT_MS = 8000

const JOINED = { status: "success", message: "You're on the list!" }
const INVALID = { status: "error", message: "Please enter a valid email address." }
const FAILED = { status: "error", message: "Something went wrong. Please try again." }

/**
 * The waitlist form's server side. It hands the address and the campaign to
 * the API (`POST /public/waitlist`), which owns the write and decides what an
 * address is: this tier holds no database credential. The API answers a new address and one already on the
 * list identically, so neither it nor this route says who has joined.
 */
export async function POST(req: NextRequest) {
  const refused = refuseCrossSite(req)
  if (refused) return refused

  let body: Record<string, unknown>
  try {
    body = await req.json()
  } catch {
    return NextResponse.json(INVALID, { status: 400 })
  }
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return NextResponse.json(INVALID, { status: 400 })
  }
  // Only strings cross, each bounded by whole characters (a cut never splits
  // an emoji into half a pair). What an address is stays the API's call; one
  // longer than any address can be is refused here without asking.
  // Trimmed here as the API trims, so pasted whitespace never counts.
  const email = typeof body.email === "string" ? body.email.trim() : ""
  if (email.length > MAX_EMAIL_CHARS && [...email].length > MAX_EMAIL_CHARS) {
    return NextResponse.json(INVALID, { status: 400 })
  }
  const forwarded: Record<string, string> = { email }
  for (const key of UTM_KEYS) {
    const value = body[key]
    if (typeof value !== "string") continue
    // A UTF-16 length within the cap is within it in characters too.
    forwarded[key] =
      value.length <= MAX_UTM_CHARS ? value : [...value].slice(0, MAX_UTM_CHARS).join("")
  }

  const result = await targetFetch("/waitlist", null, {
    method: "POST",
    plane: "public",
    body: JSON.stringify(forwarded),
    // A hung API is the same generic error, not a wait until the host kills
    // the function: targetFetch reports the abort as target_router_unreachable.
    signal: AbortSignal.timeout(API_TIMEOUT_MS),
  })

  if (result.ok) {
    // Fire-and-forget Telegram notification. The API does not say whether the
    // address was new, so a repeat signup pings again.
    notifyAdmin(email.toLowerCase()).catch(console.error)
    return NextResponse.json(JOINED)
  }
  if (result.error === "invalid_email") {
    return NextResponse.json(INVALID, { status: 400 })
  }
  // The visitor sees one generic sentence; the cause (the API unreachable or
  // refusing) goes to the server log, where the deployment's function logs
  // show it.
  console.error("waitlist signup failed:", result.status, result.error)
  return NextResponse.json(FAILED, { status: 500 })
}
