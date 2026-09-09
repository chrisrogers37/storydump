---
title: "Phase 1 — The tap: a button on a card flips the intent, once, and answers"
type: plan
status: draft
owner: chris
created: 2026-09-09
tags: [plan, telegram, ingress, commands, outbox]
links: []
---

# Phase 1 — The tap

## Summary

A tap on `🚀 Post now`, `✅ Posted myself`, `⏭️ Skip` or `🚫 Reject` reaches the command port as the tapping member, flips the intent in one transaction, answers the tap at once, and replaces the card's buttons with the outcome through the outbox. A second tap on the same card, a tap by someone else after the first, or a tap on a finished card answers with the card's current state and writes nothing. This is the W4 inbound half (`01` §1; `02` §5) and closes #854 for taps.

## Evidence

- `src/services/target/telegram_dispatch.py:71-91` — `__call__` serves `/start` and group speech; `:84-88` returns `NOT_A_START` for everything else, callbacks included. `tests/src/services/target/test_telegram_dispatch.py:49-54` pins that.
- `src/services/target/prompts.py:51-66` — `_ACTIONS_API = ("post","posted","skip","reject")`, `_ACTIONS_MANUAL`, `_LABELS`, `_token()` = `v1:<action>:<intent_id>` ≤ 64 bytes; `render_card` (`:84`) builds `reply_markup.inline_keyboard`; `prompt_intent` (`:143`) enqueues `approval_prompt` rows per binding; `push_bindings` (`:178`) lists a workspace's push bindings.
- `src/services/target/commands.py:173-183` `Command(kind, workspace_id, actor_user_id, channel, args)`; `:274-311` `execute` (role floor → `authorize_member` → executor); `:315-336` `ingest` (admit + execute; not used here — the webhook already admitted the update).
- `src/services/target/command_executors.py:91-111` `_intent_row` (no lock); `:114-122` `_refuse_if_cancelling`; `:125-130` `_flip`; `:138-162` `approve` (flips to `approved`, enqueues `publish_pipeline` on `ig:<provider_account_ref>`); `:164-188` `skip` (flip + `post_locks` skip row); `:190-212` `reject` (flip + permanent lock); `:214` `mark_posted`.
- `src/services/target/intent_ledger.py:94-133` `transition` is `UPDATE post_intents SET state = :s WHERE id = :i`, mapping the guard's refusal to `IntentTransitionRefused`.
- `scripts/migrations/055_intent_ledger_tables.sql:306-323` `trg_intent_guard`; legal edges from `awaiting_approval`: `approved`, `posted`, `skipped`, `rejected`, `expired`, `cancelled`.
- `src/services/target/tenant_resolution.py:78-113` `resolve_chat("telegram", chat_id)` → `ResolvedTenant(workspace_id, channel_binding_id)` through `fn_resolve_binding`; `:113-150` `authorize_member`.
- `src/services/target/identity.py:140-155` `user_for_identity(provider="telegram", external_id=<tg user id>)`.
- `src/services/target/outbox.py:198-230` `enqueue`; `:366-403` `supersede_all(workspace_id, binding_id, intent_id)` → `prompt_supersede` rows with `{"v":1,"supersedes_ref": <message ref>}` for every live card of that binding.
- `src/channels/telegram_transport.py:166-222` `_call`; `:230` `send_text`; `:245` `send_media`; `:281-380` `for_chat().send` renders `approval_prompt` rows by payload; no `answerCallbackQuery`, no `editMessage*`.
- `src/api/routes/webhooks.py:114-197` — admission then `runtime.dispatch(conn, payload)` then commit; `:200-218` `_acknowledge` (text reply for handled `/start` only). `src/api/app.py:404-422` wires `IngressRuntime(connect, dispatch=TelegramDispatcher(), reply=…)`.

## Implementation Plan

### Dependencies

None. (Migration: none — no schema change in this phase.)

### Blocks

Phase 2's harness (it taps); #1220 step 3 (the `post` button's job).

### Steps

1. **Tests first** (see Test Plan) — unit tests for the parser and the dispatcher branch, executor tests for the read-then-decide flip, a W4 gate `tests/scripts/test_w4_tap_gate.py` on the Docker database.
2. **Parser.** New `src/services/target/callback_tokens.py`: `parse(callback_data: str) -> Tap | None` returning `Tap(version=1, action, intent_id)`; refuses anything but `v1:` with the four actions and a UUID; `prompts._token` moves here (import back into `prompts`) so mint and parse are one module.
3. **Transport.** `TelegramTransport.answer_callback(callback_query_id: str, text: str, *, timeout_s: float = 2.0)` → `answerCallbackQuery` (best effort: log on failure, never raise past the route); `edit_reply_markup(chat_ref, message_ref, reply_markup)` → `editMessageReplyMarkup`; `edit_caption(chat_ref, message_ref, caption)` → `editMessageCaption` (for photo cards) and `edit_text` for text cards. The 400 "message is not modified" answer is success.
4. **The card edit (F4 (a)).** `for_chat().send` learns the `prompt_supersede` kind: payload gains `outcome_text` (e.g. `✅ Approved by Chris · 2:14 PM`); the sender edits the message named by `supersedes_ref` to that caption/text with no buttons. Today's supersede rows without `outcome_text` keep today's behaviour.
5. **Read-then-decide executors (F2 (a)).** `_intent_row` gains `FOR UPDATE OF i`. A new `_settle(intent, action)` returns an `answered` `CommandResult` (`{intent_id, state, settled_by, settled_at}`) when the row is already in the tapped state's family (`approved|publishing|publishing_ambiguous|posted` for `post`/`posted`; `skipped` for `skip`; `rejected` for `reject`) or terminal for any action; `approve`, `skip`, `reject`, `mark_posted` call it before `_flip`. The guard's `illegal_transition` remains the answer for a truly illegal edge (e.g. `scheduled`). `settled_by`/`settled_at` come from the newest `audit_events` row for the intent (the audit trigger writes it) — one indexed read.
6. **Supersede across every binding.** After a successful flip each executor calls `outbox.supersede_all` for **each** binding from `prompts.push_bindings(session, workspace_id)`, passing the outcome text; the rows are in the flip's transaction, so a rolled-back flip takes its edits with it.
7. **Dispatcher branch.** `TelegramDispatcher.__call__`: when `payload["callback_query"]` exists → `_tap(conn, payload)`: parse the token (unknown → answer "This button is from an older card" and stop); `resolve_chat("telegram", str(message.chat.id))` (unknown/revoked → answer "This chat is not connected to a workspace"); `user_for_identity("telegram", str(from.id))` (None → answer "Link your Telegram account in Settings first" — F3 (a)); `commands.execute(conn, Command(kind, workspace_id, user_id, "telegram", {"intent_id": …}))` with `apply_gucs(actor_kind="user", actor_user_id, channel="telegram")` on the connection; map `TenantResolutionError` (not a member / below floor) and `CommandRefused` to answers; return a `TapResult(handled=True, callback_query_id, answer_text, outcome)` — never raise (the delivery is admitted).
8. **Answer after commit.** `webhooks._acknowledge` grows the callback case: when the result carries `callback_query_id`, `runtime.answer_callback(id, text)` (a new optional `IngressRuntime.answer_callback`, wired in `app.py` beside `reply`). Outside the connection, after the commit, best effort.
9. **Action → command mapping.** `post → approve` (requires `api_publishing_enabled`; otherwise answers "This workspace posts by hand — tap Posted myself after posting"), `posted → mark_posted`, `skip → skip`, `reject → reject`. Answer texts per outcome: executed ("✅ Approved — posting shortly", "⏭️ Skipped for 30 days", "🚫 Rejected"), answered ("Already approved by Chris at 2:14 PM"), refused by name.
10. **Docs.** `02` §5 amendment: the tap's read-then-decide rule and the supersede-across-bindings rule; `06` §3: what each tap answers; `03` post-ratification ruling with the locked forks; CHANGELOG entry; the `.claude/rules/telegram.md` table gains the target-tier tap (the file describes the legacy bot today).

## Test Plan

Unit (`tests/src/services/target/`): `test_callback_tokens.py` (round-trip with `prompts._token`, refusals); `test_telegram_dispatch.py` — the `NOT_A_START` pin for callbacks is **replaced** by: unknown token answered, unknown chat answered, unlinked tapper answered, member below floor answered, executed → `handled=True` with the outcome text; `test_command_executors.py` — read-then-decide: already-approved answers without a write, terminal answers, illegal edge refuses; `test_telegram_transport.py` — `answer_callback` best effort, `edit_caption` "not modified" is success.

Gate (`tests/scripts/test_w4_tap_gate.py`, Docker database, real triggers): (1) a tap flips `awaiting_approval → approved`, writes the audit row as the tapper, enqueues `publish_pipeline` on `ig:<ref>`, supersedes the card in **both** of two bindings; (2) two taps on one card racing (two connections, `asyncio.gather`): exactly one `approved` audit row, the loser's result is `answered`; (3) a tap on a `posted` card answers and writes nothing; (4) taps on 20 different cards in parallel all land; (5) `skip` writes the lock row keyed to the tapper; (6) an unlinked tapper flips nothing and no audit row exists.

## Verification Checklist

- [ ] `pytest tests/src/services/target/test_callback_tokens.py tests/src/services/target/test_telegram_dispatch.py tests/src/services/target/test_command_executors.py -q` green.
- [ ] `pytest tests/scripts/test_w4_tap_gate.py -q` green on `localhost:65433`.
- [ ] `grep -n "answerCallbackQuery\|editMessageCaption\|editMessageReplyMarkup" src/channels/telegram_transport.py` finds all three.
- [ ] In the owner's bound group: a tap on a live card answers within 2 s, the card's buttons are replaced by the outcome line, a second tap answers "Already …".
- [ ] `ruff check src tests` clean; CHANGELOG entry present; `02`/`06`/`03` amended.

## What NOT To Do

- Do not call Telegram inside the flip's transaction.
- Do not admit the update a second time through `commands.ingest`; the webhook's `update_id` admission is the ledger.
- Do not answer a tap 200 without either executing or answering it by name.
- Do not edit a card whose message ref is unknown.

## Context

area: ingress, commands, outbox, transport · effort: L · risk: high · priority: P0.
