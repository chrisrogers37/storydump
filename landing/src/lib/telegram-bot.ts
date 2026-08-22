/**
 * The product's Telegram bot handle, and the only place the site should get it.
 *
 * The handle is deployment configuration, not a constant: `NEXT_PUBLIC_TELEGRAM_BOT_NAME`
 * is what the Telegram Login Widget is initialised with, so it is already the value that
 * decides which bot authenticates users. Anything else on the site that names the bot has
 * to agree with it or the site disagrees with itself — which it did: two hardcoded links
 * pointed at a handle that belongs to someone else entirely.
 *
 * `botUrl` returns `null` rather than a best-effort URL when the variable is unset. A
 * missing link is a nuisance; a link to a handle we do not control is the defect this
 * module exists to make unrepeatable, and `t.me/undefined` is exactly that shape.
 */

export const botName = process.env.NEXT_PUBLIC_TELEGRAM_BOT_NAME

export function botUrl(startParam?: string): string | null {
  if (!botName) return null
  const query = startParam ? `?start=${encodeURIComponent(startParam)}` : ""
  return `https://t.me/${botName}${query}`
}
