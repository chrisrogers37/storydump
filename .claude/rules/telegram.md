---
paths:
  - "src/services/core/telegram_*"
---

# Telegram Bot

## Active Commands

| Command | Description | Handler |
|---------|-------------|---------|
| `/start` | Open setup wizard or show dashboard | `telegram_commands.py` |
| `/status` | Health, media stats, queue status | `telegram_commands.py` |
| `/help` | Show available commands | `telegram_commands.py` |
| `/next` | Force-send next post now | `telegram_commands.py` |
| `/cleanup` | Delete recent bot messages | `telegram_commands.py` |
| `/setup` / `/settings` | Quick settings & toggles | `telegram_settings.py` |

**Retired commands**: `/queue`, `/pause`, `/resume`, `/history`, `/sync`, `/schedule`, `/stats`, `/locks`, `/reset`, `/dryrun`, `/backfill`, `/connect` — respond with redirect messages but are not listed in the bot menu.

## Callback Actions

| Action | Description | Handler |
|--------|-------------|---------|
| `posted:{queue_id}` | Mark as posted | `telegram_callbacks.py` |
| `skip:{queue_id}` | Skip for later | `telegram_callbacks.py` |
| `reject:{queue_id}` | Initiate rejection | `telegram_callbacks.py` |
| `confirm_reject:{queue_id}` | Confirm rejection | `telegram_callbacks.py` |
| `autopost:{queue_id}` | Auto-post via API | `telegram_autopost.py` |
| `settings_toggle:{setting}` | Toggle setting | `telegram_settings.py` |
| `sa:{queue_id}` | Account selector | `telegram_accounts.py` |
| `sap:{queue_id}:{account_id}` | Switch account | `telegram_accounts.py` |

## Handler Architecture

Telegram handler modules use a **composition pattern** — they receive a reference to the parent `TelegramService` and are NOT standalone services. `InteractionService` intentionally does NOT extend `BaseService` to avoid recursive tracking.

## Target tier — the tap (W4, 2026-09-09)

The tables above describe the **legacy** bot. In the target tier a button tap on an
approval card is a `callback_query` on the webhook (`src/api/routes/webhooks.py`):
admitted on its `update_id` (`command_dedup`), dispatched by
`src/services/target/telegram_dispatch.py::TelegramDispatcher._tap` — parse
(`callback_tokens.parse`, `v1:<post|posted|skip|reject>:<intent-uuid>`), resolve the
chat (`fn_resolve_binding`), resolve the tapper (`user_identities`), set the actor
GUCs, `commands.execute` as the tapping member — and committed; the route then
answers (`answerCallbackQuery`) and strips the tapped card's keyboard, best effort.
Rules: the executors read `FOR UPDATE` and a card past `awaiting_approval` ANSWERS
with its state, never errors; every flip supersedes the card in every binding;
`_tap` never raises for anything but a database error (a poisoned update is the
named outcome `tap_failed`); nothing speaks to Telegram inside the transaction. The
registration must ask for `callback_query` (`scripts/telegram_webhook.py`).

