---
description: "Check the Telegram bot's webhook, what is owed on the chats and recent decisions, through storydump (read-only)"
---

Report the state of the Telegram channel through the `storydump` CLI. The bot
is a webhook on the API (inbound) and the worker's outbox sender (outbound);
nothing polls, and there is no table of "interactions" to read — the ledger's
audit rows and the outbox are the record. Every command below is a bounded read
through the API; add `--json` for one envelope `{"v": 1, "kind", "data", "error"}`.

Exit codes: 0 ok · 1 not found · 3 not authorized · 4 API unreachable or not
well · 64 usage. Exit 3 means no usable token (`storydump whoami` shows which
workspaces a token can read): stop and say so — minting one is the user's.

## 1. Is Telegram reaching the API?

```bash
storydump health --json
```

Needs no token. Read three things from it:

- `data.verdicts.webhook` — `registered` is well. `unregistered` (the startup
  registration failed; `detail` says why), `undelivered` (Telegram holds a
  backlog behind a delivery error — our door is failing) and `sampler_failed`
  (the API could not ask Telegram) are not well. `skipped` (this API chose not
  to register: not the production environment, autoregister switched off, or no
  token or secret) and `unsampled` (the API reports no webhook at all) are
  facts for the report, not outages.
- `data.api.webhook` is the registration this API made at startup (`bot`, `url`,
  `allowed_updates` — it must include `callback_query`, or every button tap is
  dropped before it reaches the route); `data.api.webhook_live` is what Telegram
  holds now, sampled each minute (`pending_update_count`, `last_error_message`).
  A growing backlog with no error is Telegram not delivering; an error naming a
  response code is our route failing.
- `data.api.taps` — since this API process started: `taps_total`, `taps` by
  outcome (`executed`, `answered`, `older_card`, `unlinked`, `busy`,
  `tap_failed`, …) and `answer_failed`, the answers that did not land.

## 2. What is owed or lost on the chats (outbound)

```bash
storydump outbox --since 24h
```

One row per binding × kind × state for `pending`, `sending`, `ambiguous` and
`failed` rows (a failed row only inside the window; the rest at any age).
`pending` that does not drain means no sender is running for that chat;
`ambiguous` is a send whose answer was lost, resolved by the binding's next
sender; `failed` is a message that did not land.

```bash
storydump jobs --since 24h
```

Look at the `deliver_outbox` rows. `pending` rows above with NO `ready` or
`leased` sender job here means the worker is not minting senders: its Telegram
channel is parked (no token, a dead token, or a token for a bot other than the
configured one — `src/worker.py:370`), or the worker is down (step 1's
`scheduling` verdict says which). A `ready` group whose `oldest_run_at` is long
past means nothing is claiming the interactive lane; a `failed` group is a
sender that spent its attempts. The reason is in the worker's log on Railway,
which is the user's to open.

## 3. Recent decisions (inbound)

```bash
storydump burst --since 3h
```

One timeline. The `tap` rows are decisions made on a story awaiting approval,
with the channel that made them (`telegram`, `web` or `cli`) and the state they
led to; `review` rows are stories parked for the workspace to resolve, with the
error; `outcome` rows close the window with counts by state.

For one story, everything sent for it, by chat, in send order — adopted twins
included:

```bash
storydump cards <intent_id>
```

## 4. Only if the deployment's variables are already in this shell

```bash
storydump webhook status
```

It asks Telegram who the bot is and what webhook it holds, and proves the API
accepts the secret (an empty POST the API refuses by design — nothing is
changed). Without `TARGET_TELEGRAM_BOT_TOKEN` it exits 64 naming it; without
`TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` the door check reports NOT CHECKED and
the verb exits 4; `TARGET_TELEGRAM_BOT_USERNAME` lets it confirm the bot is the
configured one. Do not ask the user to paste a token or a secret into the
conversation — step 1 answers the same question from `/health`.
The runbook is `documentation/operations/telegram-webhook.md`.

## What no verb answers

Stories still waiting for a decision are not listed by any verb — the web's
Queue shows them. `storydump account <handle>` gives an account's next slot and
its last twenty outcomes.

## Presenting Results

```
## Telegram Status

**Webhook:** registered as @… → <url> · pending N · last error: none

### Owed on the chats
| Chat | Kind | State | Count | Oldest |
|------|------|-------|-------|--------|

### Sender jobs (deliver_outbox)
| Lane | State | Count | Oldest run_at |
|------|-------|-------|---------------|

### Recent decisions (3h)
| Time | Story | From → To | By | Channel |
|------|-------|-----------|----|---------|
```

**REMINDER**: every command here reads. `storydump webhook register` and
`deregister` re-point the production bot and are in `CLAUDE.md`'s safety block;
so are the verbs that post. In Telegram Web, view and screenshot only — no
typing, no clicking.
