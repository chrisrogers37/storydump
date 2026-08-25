import { NextResponse } from "next/server";
import { getSession, SessionUnavailableError } from "@/lib/session";

/**
 * GET /api/auth/session — who is signed in, for the client.
 *
 * Never returns the token, only what a UI needs to render a name. The token is
 * httpOnly precisely so the browser cannot read it, and echoing it through a
 * JSON body would undo that for the sake of nothing.
 *
 * Three outcomes, kept apart: signed in (200), not signed in (200 with a null
 * user — an ordinary answer, not an error), and could not tell (503). The third
 * is why this does not collapse into "user or nothing": a client that reads an
 * unreachable router as a signed-out user will helpfully clear its own state.
 */
export async function GET() {
  try {
    const session = await getSession();
    return NextResponse.json({ user: session });
  } catch (error) {
    if (error instanceof SessionUnavailableError) {
      return NextResponse.json(
        { user: null, error: "session_unavailable" },
        { status: 503 },
      );
    }
    throw error;
  }
}
