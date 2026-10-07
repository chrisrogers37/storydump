/**
 * How the waitlist form reads an answer that is not a signup. A 429 carrying
 * `reason: "busy"` (the site route's BUSY body: the list is full for a minute)
 * is busy: the form shows it but leaves the address unmarked, and analytics
 * counts it as `busy`. Anything else is an error, counted as `server_error`.
 */
export function waitlistRefusal(
  status: number,
  body: unknown
): { status: "busy" | "error"; reason: "busy" | "server_error" } {
  const reason = (body as { reason?: unknown } | null)?.reason
  return status === 429 && reason === "busy"
    ? { status: "busy", reason: "busy" }
    : { status: "error", reason: "server_error" }
}
