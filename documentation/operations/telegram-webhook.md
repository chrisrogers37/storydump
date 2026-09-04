# Telegram webhook — registering the target bot

The target tier receives Telegram through one door, `POST /webhooks/telegram`
on the API, authenticated by the secret Telegram echoes in
`X-Telegram-Bot-Api-Secret-Token`. Nothing polls: the target worker only
sends. Arming delivery is two deliberate acts (`settings.py`'s own note): set
the secret on the API, then register the webhook on the bot with that secret.

## Precondition: the worker is on the target tier

A Telegram bot cannot be polled and webhooked at once. The legacy scheduler
(`python -m src.main` without `WORKER_IMPL=target`) polls `TELEGRAM_BOT_TOKEN`'s
bot with `getUpdates`, and python-telegram-bot's polling start **deletes any
webhook** on that bot. So before registering on `storydump_app_bot`, confirm
the worker service has `WORKER_IMPL=target` (it has since 2026-08-24). If the
legacy scheduler is ever started again, it clears the webhook — that is the
failure to suspect if `status` suddenly reports no webhook.

## Which bot

**`storydump_app_bot`** — the product's bot, the one the site links to
(`NEXT_PUBLIC_TELEGRAM_BOT_NAME`) and the one already in the owner's chats. Its
token is the worker's `TARGET_TELEGRAM_BOT_TOKEN`; its username is the API's
`TARGET_TELEGRAM_BOT_USERNAME` (renders the `t.me/…?start=link-…` link). A
second bot, `storydumpapp_bot`, was created for the target tier while the
legacy scheduler still polled the first; that reason is gone and it should be
retired in BotFather once the webhook below is confirmed.

## The tool

`scripts/telegram_webhook.py` — stdlib only, reads the three deployment
variables from the shell, and **never prints a token or a secret**, so its
output is safe to paste anywhere. It refuses a non-`https` URL and never
follows a redirect (either would carry the secret somewhere else), and
`register` refuses unless the token's bot is the configured bot. Run it from
a laptop with the values exported, or via `railway run` against the service
that holds them.

```bash
export TARGET_TELEGRAM_BOT_TOKEN='…'              # worker → Variables
export TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN='…'   # API → Variables
export TARGET_TELEGRAM_BOT_USERNAME='storydump_app_bot'
python -m scripts.telegram_webhook status
python -m scripts.telegram_webhook register --drop-pending   # first arming of a bot
```

`status` answers four questions and exits 1 if any fails:

1. **Who is the bot** — `getMe`; and that it IS `TARGET_TELEGRAM_BOT_USERNAME`.
2. **Is a webhook registered, where, with what backlog** — `getWebhookInfo`.
   A `LAST ERROR` line is Telegram's own complaint about the last delivery;
   a `NOTE: registered URL differs` line means the webhook points somewhere
   other than `--url` (default `https://api.storydump.app/webhooks/telegram`,
   override with `--url` or `TARGET_TELEGRAM_WEBHOOK_URL` for a preview).
3. **Does the API accept the secret** — a POST to the door with the secret
   and an empty body: `400` means accepted (the empty body is refused one
   step later, as "malformed body"); `403` means the secret does not match
   the API's variable, or it is unset there; `503` means the API has no
   ingress wired; a 3xx is reported and never followed. With the secret not
   exported the door is `NOT CHECKED`, and that counts as a failure.

`register` calls `setWebhook` with the door URL, the secret and messages only
(the ingress serves `/start` taps and nothing else yet — #854), then runs
`status`. `--drop-pending` discards updates Telegram queued before now: use
it when first arming a bot, never when re-registering a live one, because
real taps would be lost. `deregister` deletes the webhook.

## What a tap does today

`/start link-…` attaches the tapping Telegram account to the user who minted
the link; `/start inv-…` accepts an invitation. **The bot does not reply** —
the API holds no bot token and enqueues nothing on this path (#854 covers
chat-inbound generally). The person sees the result on the site: Settings ›
Integrations shows *Linked* with the Telegram display name after a reload.

## Order of operations

1. `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` and `TARGET_TELEGRAM_BOT_USERNAME`
   on the **API** service (redeploys).
2. `python -m scripts.telegram_webhook register --drop-pending`.
3. Settings › Integrations → Link Telegram → open in Telegram → Start →
   reload.

If `status` reports `403` at the door after step 1, the API has not finished
redeploying or the two values differ by a character.
