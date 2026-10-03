import { NextRequest, NextResponse } from "next/server"
import { refuseCrossSite } from "@/lib/route-guards"
import { getDb } from "@/lib/db"
import { waitlistSignups } from "@/lib/schema"
import { notifyAdmin } from "@/lib/telegram"
import { UTM_KEYS } from "@/lib/analytics"

const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
/** The longest address SMTP can carry: RFC 5321's 256-octet path less its brackets. */
const MAX_EMAIL_LENGTH = 254
/** Each UTM value is cut to this, so a public form cannot grow `notes` without bound. */
const MAX_UTM_LENGTH = 100

/**
 * Postgres's unique-violation code, on the error or on its `cause`. Drizzle
 * wraps a failed query in a DrizzleQueryError whose own `code` is undefined
 * and puts the driver's error, which carries the code, in `cause`. Reading
 * only the outer error sent every returning visitor a 500.
 */
function isUniqueViolation(err: unknown): boolean {
  for (let e = err, depth = 0; e && depth < 3; depth++) {
    if (typeof e === "object" && (e as { code?: unknown }).code === "23505") {
      return true
    }
    e = (e as { cause?: unknown }).cause
  }
  return false
}

const JOINED = { status: "success", message: "You're on the list!" }

export async function POST(req: NextRequest) {
  const refused = refuseCrossSite(req)
  if (refused) return refused

  try {
    const body = await req.json()
    const email = typeof body?.email === "string" ? body.email.trim().toLowerCase() : ""

    if (!email || email.length > MAX_EMAIL_LENGTH || !EMAIL_REGEX.test(email)) {
      return NextResponse.json(
        { status: "error", message: "Please enter a valid email address." },
        { status: 400 }
      )
    }

    // Capture UTM params
    const utm: Record<string, string> = {}
    for (const key of UTM_KEYS) {
      if (typeof body[key] === "string" && body[key].trim()) {
        utm[key] = body[key].trim().slice(0, MAX_UTM_LENGTH)
      }
    }
    const notes = Object.keys(utm).length > 0 ? JSON.stringify(utm) : null

    try {
      await getDb().insert(waitlistSignups).values({ email, notes })
    } catch (err: unknown) {
      // A unique violation means the email is already on the list. It gets
      // the same answer as a new signup, so the form cannot be used to test
      // whether an address is on the list; only the admin ping is skipped.
      if (!isUniqueViolation(err)) throw err
      return NextResponse.json(JOINED)
    }

    // Fire-and-forget Telegram notification
    notifyAdmin(email).catch(console.error)

    return NextResponse.json(JOINED)
  } catch (err) {
    // The visitor sees one generic sentence; the cause (an unset
    // DATABASE_URL, a missing table) goes to the server log, where the
    // deployment's function logs show it.
    console.error("waitlist signup failed:", err)
    return NextResponse.json(
      { status: "error", message: "Something went wrong. Please try again." },
      { status: 500 }
    )
  }
}
