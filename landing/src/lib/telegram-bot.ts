/**
 * The product's Telegram bot handle, and the only place the site should get it.
 *
 * The handle is deployment configuration, not a constant: `NEXT_PUBLIC_TELEGRAM_BOT_NAME`
 * names the product's bot, and the account-link flow (`telegram-link.ts`) refuses a link to
 * any other. Anything on
 * the site that names the bot has to agree with it or the site disagrees with itself —
 * which it did: two hardcoded links pointed at a handle that belongs to someone else entirely.

 */

export const botName = process.env.NEXT_PUBLIC_TELEGRAM_BOT_NAME
