---
paths:
  - "src/services/target/telegram_dispatch.py"
  - "src/channels/telegram_*"
  - "src/api/routes/webhooks.py"
---

# Telegram (the target tier)

## Target tier — the tap (W4, 2026-09-09)

The legacy bot (`src/services/core/telegram_*`: commands, callbacks, the polling
service) was deleted in the legacy tear-out (#1216). In the target tier a button tap on an
approval card is a `callback_query` on the webhook (`src/api/routes/webhooks.py`):
admitted on its `update_id` (`command_dedup`), dispatched by
`src/services/target/telegram_dispatch.py::TelegramDispatcher._tap` — parse
(`callback_tokens.parse`, `v1:<post|posted|skip|reject|itposted|notposted|giveup>:<intent-uuid>` — the last three are the review card's, all → `resolve_review`; `notposted` carries the member's verdict), resolve the
chat (`fn_resolve_binding`), resolve the tapper (`user_identities`), set the actor
GUCs, `commands.execute` as the tapping member — and committed; the route then
answers (`answerCallbackQuery`), best effort. The card's keyboard goes with the
paced supersede edit that writes its outcome line — one Telegram message per tap
per binding (2026-09-12); the route does not strip.
Rules: the executors read `FOR UPDATE` and a card past `awaiting_approval` ANSWERS
with its state, never errors; every flip supersedes the card in every binding;
`_tap` never raises for anything but a database error (a poisoned update is the
named outcome `tap_failed`); nothing speaks to Telegram inside the transaction. The
registration must ask for `callback_query` (`storydump webhook register`; the one
spelling of the update kinds is `src/services/target/vocabulary.py`).

