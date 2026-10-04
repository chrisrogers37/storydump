/** How long the ping may take before it gives up, well inside the function's life. */
const SEND_TIMEOUT_MS = 5000

/**
 * Tells the admin chat about a waitlist signup. It never throws: every way a
 * ping can go missing is one log line naming the cause, so a missed ping is
 * explained in the deployment's function log. The token never reaches a log.
 */
export async function notifyAdmin(email: string): Promise<void> {
  // Read per call, not at import, and trimmed so a pasted newline cannot break the URL.
  const token = process.env.TELEGRAM_BOT_TOKEN?.trim()
  const chatId = process.env.ADMIN_TELEGRAM_CHAT_ID?.trim()
  const missing = [
    !token && "TELEGRAM_BOT_TOKEN",
    !chatId && "ADMIN_TELEGRAM_CHAT_ID",
  ].filter(Boolean)
  if (missing.length) {
    console.warn("waitlist admin ping skipped: not set:", missing.join(", "))
    return
  }

  const message = `New waitlist signup!\n\nEmail: ${email}\nTime: ${new Date().toISOString()}`

  let response: Response
  try {
    // Plain text (no parse_mode), so a visitor's address cannot format the
    // message, and no link preview for a URL-shaped one.
    response = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: message,
        link_preview_options: { is_disabled: true },
      }),
      signal: AbortSignal.timeout(SEND_TIMEOUT_MS),
    })
  } catch (err) {
    // The error's name and code only (TimeoutError, ENOTFOUND): a message
    // could quote the URL, which carries the token.
    const name = err instanceof Error ? err.name : typeof err
    const code = (err as { cause?: { code?: unknown } }).cause?.code
    if (typeof code === "string") console.error("waitlist admin ping failed:", name, code)
    else console.error("waitlist admin ping failed:", name)
    return
  }
  if (!response.ok) {
    // Telegram's own reason ("Bad Request: chat not found", "Unauthorized",
    // "Forbidden: bot was blocked by the user") says what to fix.
    console.error("waitlist admin ping refused:", response.status, await refusalReason(response))
  }
}

/** Telegram's `description`, bounded, or a placeholder when the body has none. */
async function refusalReason(response: Response): Promise<string> {
  try {
    const { description } = await response.json()
    if (typeof description === "string") return description.slice(0, 200)
  } catch {
    // Not JSON: a proxy's page, never quoted.
  }
  return "(no description)"
}
