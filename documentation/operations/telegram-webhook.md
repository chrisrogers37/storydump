# Telegram webhook — registering the target bot

The target tier receives Telegram through one door, `POST /webhooks/telegram`
on the API, authenticated by the secret Telegram echoes in
`X-Telegram-Bot-Api-Secret-Token`. Nothing polls: the target worker only
sends. Arming delivery is two deliberate acts (`settings.py`'s own note): set
the secret on the API, then register the webhook on the bot with that secret.

## Nothing polls this bot

A Telegram bot cannot be polled and webhooked at once, and nothing in the tree
polls: the worker only sends (`src/channels/telegram_transport.py`), the API
receives at the door, and `python -m src.main` runs the target worker and
nothing else (`src/main.py`). The legacy scheduler polled this same bot with
`getUpdates`, and its poller **deleted any webhook** on the bot when it
started; it was retired in the tear-out (#1216, September 2026). So if `status`
suddenly reports no webhook, suspect a poller started somewhere else with this
bot's token — a checkout of a commit from before the tear-out, or a second
holder of the token — before suspecting the API.

## Which bot

**`storydump_app_bot`** — the product's bot, the one the site links to
(`NEXT_PUBLIC_TELEGRAM_BOT_NAME`) and the one already in the owner's chats.

Its token is `TARGET_TELEGRAM_BOT_TOKEN`, the same value on **both** services:
the worker sends cards with it, and the API registers the webhook, samples it
and answers taps with it (`src/api/app.py:401-449`, `529-541`) — the API never
holds a second credential for the one bot. Its username is
`TARGET_TELEGRAM_BOT_USERNAME`: on the API it renders the
`t.me/…?start=link-…` link and is the bot a registration insists on; on the
worker it is the bot the startup probe insists on — a token that is another
bot's parks the sender rather than mint cards whose buttons nobody listens on
(`src/worker.py:395-410`, the 2026-09-10 crosswire).

A second bot, `storydumpapp_bot`, was created for the target tier while the
legacy scheduler still polled the first; that reason is gone and it should be
retired in BotFather once the webhook below is confirmed.

## The tool

`storydump webhook` (the v2 CLI, `pip install -e '.[cli]'`) reads the three
deployment variables from the shell, and **never prints a token or a secret**, so its
output is safe to paste anywhere. It refuses a non-`https` URL and never
follows a redirect (either would carry the secret somewhere else), and
`register` refuses unless the token's bot is the configured bot. Run it from
a laptop with the values exported, or via `railway run` against the service
that holds them.

```bash
export TARGET_TELEGRAM_BOT_TOKEN='…'              # worker or API → Variables (the same value)
export TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN='…'   # API → Variables
export TARGET_TELEGRAM_BOT_USERNAME='storydump_app_bot'
storydump webhook status
```

That block is safe to paste: `status` only reads. **Arming the bot is a separate act and the
owner's**: `register` re-points the production bot's webhook and `--drop-pending` discards
the taps Telegram has queued, so both are in `CLAUDE.md`'s safety block and an agent asks
first. For the first arming of a bot, with the same three variables exported:

```bash
storydump webhook register --drop-pending   # CLAUDE.md's safety block: the owner's decision
```

`status` answers three questions and exits 4 if any check fails (64 for a missing variable):

1. **Who is the bot** — `getMe`; and that it IS `TARGET_TELEGRAM_BOT_USERNAME`.
2. **Is a webhook registered, where, with what backlog** — `getWebhookInfo`.
   A `LAST ERROR` line is Telegram's own complaint about the last delivery
   (a failed check); a failed `url` check means the webhook points somewhere
   other than `--url` (default `https://api.storydump.app/webhooks/telegram`,
   override with `--url` or `TARGET_TELEGRAM_WEBHOOK_URL` for a preview).
3. **Does the API accept the secret** — a POST to the door with the secret
   and an empty body: `400` means accepted (the empty body is refused one
   step later, as "malformed body"); `403` means the secret does not match
   the API's variable, or it is unset there; `503` means the API has no
   ingress wired; a 3xx is reported and never followed. With the secret not
   exported the door is `NOT CHECKED`, and that counts as a failure.

**Since 2026-09-09 the API registers the webhook itself at startup** (`src/api/app.py`
`_register_webhook`, idempotent on every deploy; `/health` reports the result under `webhook` as a startup
snapshot; on by default only in Railway's `production` environment — `RAILWAY_ENVIRONMENT_NAME` —
so a laptop or a preview holding the token never re-points production's webhook;
`TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER=1` forces it on, `0` off). This tool remains for `status`,
`deregister` and a manual `register`. `register` calls `setWebhook` with the door URL, the secret, the served update kinds — `message` and, since the 2026-09-09 tap (W4), `callback_query`: Telegram delivers ONLY what is asked for, so a registration without it drops every button tap silently — and `max_connections` from `TARGET_TELEGRAM_WEBHOOK_MAX_CONNECTIONS` (default 10, the ingress's connection budget; 1..100). **Re-run `register` after a deploy that changes the served kinds** (the W4 deploy is one); `status` prints `allowed_updates` so the omission is visible. The ingress serves
`/start` taps, button taps and group messages; chat-inbound commands are still
#854. `register` ends by running `status`. `--drop-pending` discards updates Telegram queued before now: use
it when first arming a bot, never when re-registering a live one, because
real taps would be lost. `deregister` deletes the webhook.

## What a tap does today

`/start link-…` attaches the tapping Telegram account to the user who minted
the link; `/start bind-…` binds the group it was opened in (see *Groups*).
Those are the two lanes served: `build_router` registers `link-` and `bind-`
only (`src/services/target/telegram_dispatch.py:283-285`; the module docstring says
the same), so an `inv-` payload reaches no
handler — an invitation is accepted on the web. **The bot answers a handled tap in the
chat** (since #1239) and stays silent on a refusal. A bare `/start` in a group
is treated as speech (see *Members*), never a greeting. The person sees the
result on the site after a reload.

## Order of operations

1. `TARGET_TELEGRAM_BOT_TOKEN`, `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` and
   `TARGET_TELEGRAM_BOT_USERNAME` on the **API** service; the token and the
   username on the **worker** too (each change redeploys its service).
2. In production the API's startup registration has now registered the
   webhook, keeping whatever Telegram had queued; `storydump webhook status`
   confirms it. A first arming that must discard a stale backlog is
   `storydump webhook register --drop-pending` — the user's decision
   (`CLAUDE.md`'s never-run list).
3. Settings › Integrations → Link Telegram → open in Telegram → Start →
   reload.

If `status` reports `403` at the door after step 1, the API has not finished
redeploying or the two values differ by a character.

## Groups

A workspace's approval cards and notices go to its **bound** Telegram groups.
An admin binds one from Settings › Integrations › *Add a Telegram group*: the
link opens Telegram's group picker, Telegram adds the bot to the chosen group
and sends `/start bind-<state>` there, and the door binds that chat to the
workspace (`07` §13). Only the admin who minted the link can use it, and their
own Telegram must be linked first (Settings refuses otherwise). The bot answers
in the group. If the bot was already in the group and nothing arrived, send the
command the card shows — `/start@<bot> bind-…` — in the group.

**Precondition:** the bot must be allowed into groups. In BotFather send
`/setjoingroups`, pick the bot, and choose *Enable*. Without it the picker
cannot add the bot and nothing arrives at the door.

**What a tap does now (since #1239):** a handled `/start` — `link-` (identity)
or `bind-` (group) — is acknowledged in the chat after the delivery is
committed. Refusals stay silent.

**A kicked bot is not a dead token.** A bound group that removes the bot (or a
deleted chat) makes the next delivery fail definitively; the worker revokes the
binding and stops minting for it. A group upgraded to a supergroup is followed
to its new chat id when Telegram announces the upgrade. A delivery that reaches
the old id before then — the notice never arrived, or a send was already under
way — finds the move itself: that one message fails, and the binding follows.
Only a 401 from Telegram means the credential itself died.

**Members.** Anyone who speaks in a bound group and has linked their Telegram
(Settings › Integrations › Link Telegram) becomes a member of that workspace
automatically, at the member role — silently, the first time the bot sees them
post or are added. Owners and admins keep their role. Leaving the group removes
nobody; an admin removes people from Settings › General › Members.

**Precondition for members:** under Telegram's default *privacy mode* a bot in
a group receives only commands, replies, mentions and service messages — so it
would see almost nobody speak. Either send `/setprivacy` to BotFather, pick the
bot, and choose *Disable* (then remove and re-add the bot to existing groups, as
Telegram requires), or make the bot an admin of each group.
