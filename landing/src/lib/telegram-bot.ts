/**
 * The product's Telegram bot handle, and the only place the site should get it.
 *
 * The handle is deployment configuration, not a constant: `NEXT_PUBLIC_TELEGRAM_BOT_NAME`
 * names the product's bot, and the account-link flow (`telegram-link.ts`) refuses a link to
 * any other. Anything on the site that names the bot has to agree with it, or the site
 * disagrees with itself, and a hardcoded link can point at someone else's handle.
 */

export const botName = process.env.NEXT_PUBLIC_TELEGRAM_BOT_NAME
