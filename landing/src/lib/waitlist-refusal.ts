/**
 * How the waitlist form reads an answer that is not a signup: "busy" when the
 * site's route said the list is full for a minute (a 429 carrying
 * `reason: "busy"`, its BUSY body), else "error". The form shows both, but a
 * busy answer leaves the address unmarked and is counted apart in analytics.
 */
export function waitlistRefusal(status: number, body: unknown): "busy" | "error" {
  const reason = (body as { reason?: unknown } | null)?.reason
  return status === 429 && reason === "busy" ? "busy" : "error"
}
