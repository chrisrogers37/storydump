# Telegram webhook — registering the target bot

The target tier receives Telegram through one door, `POST /webhooks/telegram`
on the API, authenticated by the secret Telegram echoes in
`X-Telegram-Bot-Api-Secret-Token`. Nothing polls: the worker only sends. Arming
delivery is two deliberate acts (`settings.py`'s own note): set the secret on
the API, then register the webhook on the bot with that secret.

## Which bot

**`storydump_app_bot`** — the product's bot, the one the site links to
(`NEXT_PUBLIC_TELEGRAM_BOT_NAME`) and the one already in the owner's chats. Its
token is the worker's `TARGET_TELEGRAM_BOT_TOKEN`; its username is the API's
`TARGET_TELEGRAM_BOT_USERNAME` (renders the `t.me/…?start=link-…` link). A
second bot, `storydumpapp_bot`, was created for the target tier while the
legacy scheduler still polled the first; that reason is gone and it should be
retired in BotFather once the webhook below is confirmed.

## The tool

`scripts/telegram_webhook.py` — stdlib only, reads the two deployment
variables from the shell, and **never prints a token or a secret**, so its
output is safe to paste anywhere. Run it from a laptop with the values
exported, or via `railway run` against the service that holds them.

```bash
export TARGET_TELEGRAM_BOT_TOKEN='…'              # worker → Variables
export TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN='…'   # API → Variables
python -m scripts.telegram_webhook status
python -m scripts.telegram_webhook register
```

`status` answers three questions and exits 1 if any fails:

1. **Who is the bot** — `getMe`; expect `@storydump_app_bot`.
2. **Is a webhook registered, where, with what backlog** — `getWebhookInfo`;
   a `LAST ERROR` line is Telegram's own complaint about the last delivery.
3. **Does the API accept the secret** — a POST to the door with the secret and
   an empty body: `400` means accepted (the empty body is refused one step
   later, by design); `403` means the secret does not match the API's
   variable, or it is unset there; `503` means the API has no ingress wired.

`register` calls `setWebhook` with the door URL, the secret, messages only
(the ingress serves `/start` taps and nothing else yet — #854), and drops any
backlog, then runs `status`. `deregister` deletes the webhook.

## Order of operations

1. `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` on the **API** service (redeploys).
2. `python -m scripts.telegram_webhook register`.
3. `TARGET_TELEGRAM_BOT_USERNAME=storydump_app_bot` on the API.
4. Settings › Integrations → Link Telegram → open in Telegram → Start → reload.

If `status` reports `403` at the door after step 1, the API has not finished
redeploying or the two values differ by a character.
