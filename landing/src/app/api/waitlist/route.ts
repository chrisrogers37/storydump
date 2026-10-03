import { NextRequest, NextResponse } from "next/server"
import { refuseCrossSite } from "@/lib/route-guards"
import { targetFetch } from "@/lib/target-api"
import { notifyAdmin } from "@/lib/telegram"
import { UTM_KEYS } from "@/lib/analytics"

const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
/** The longest address SMTP can carry: RFC 5321's 256-octet path less its brackets. */
const MAX_EMAIL_LENGTH = 254
/** Each UTM value is cut to this before it is forwarded; the API cuts it too. */
const MAX_UTM_LENGTH = 100

const JOINED = { status: "success", message: "You're on the list!" }
const INVALID = { status: "error", message: "Please enter a valid email address." }
const FAILED = { status: "error", message: "Something went wrong. Please try again." }

/**
 * The waitlist form's server side. It validates, then hands the address to
 * the API (`POST /public/waitlist`), which owns the write: this tier holds no
 * database credential. The API answers a new address and one already on the
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
  const email = typeof body.email === "string" ? body.email.trim().toLowerCase() : ""

  if (!email || email.length > MAX_EMAIL_LENGTH || !EMAIL_REGEX.test(email)) {
    return NextResponse.json(INVALID, { status: 400 })
  }

  const utm: Record<string, string> = {}
  for (const key of UTM_KEYS) {
    const value = body[key]
    if (typeof value === "string" && value.trim()) {
      utm[key] = value.trim().slice(0, MAX_UTM_LENGTH)
    }
  }

  const result = await targetFetch("/waitlist", null, {
    method: "POST",
    plane: "public",
    body: JSON.stringify({ email, ...utm }),
  })

  if (result.ok) {
    // Fire-and-forget Telegram notification. The API does not say whether the
    // address was new, so a repeat signup pings again.
    notifyAdmin(email).catch(console.error)
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
