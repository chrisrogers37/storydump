---
paths:
  - "src/api/routes/webhooks.py"
  - "src/channels/telegram_*"
  - "src/services/target/telegram_dispatch.py"
  - "src/services/target/webhook_ingress.py"
  - "src/services/target/callback_tokens.py"
  - "src/services/target/prompts.py"
  - "src/services/target/outbox.py"
---

# Telegram (the target adapter)

Telegram is one channel adapter over the ledger: inbound through a webhook on
the API, outbound through `channel_outbox` and the worker's sender. Nothing
polls, and nothing inland of the adapter sees a chat id. The legacy polling bot
was retired in the tear-out (#1216, September 2026). Registering and checking
the webhook is `documentation/operations/telegram-webhook.md`; reading what was
sent is `storydump cards <intent_id>` and `storydump outbox`.

## Inbound: the webhook (`src/api/routes/webhooks.py`)

`POST /webhooks/telegram` (`:146`, mounted at `app.py:673`). In order:

1. The secret header must match `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` — 403
   otherwise, and an unset secret refuses every delivery (`:162`).
2. No dispatcher wired (`app.state.ingress`, which exists only with an engine) →
   503 **before admission**, so Telegram redelivers (`:207`).
3. The pool's wait ran out at checkout → a tap is answered "Busy — tap again."
   and consumed; anything else is 503 and redelivered (`_refuse_saturated`,
   `:298`).
4. `admit` (`webhook_ingress.py:129`): one `INSERT … ON CONFLICT` into
   `command_dedup` keyed on the `update_id`. A replay is acknowledged without
   re-execution; the same key with different content is 409.
5. `dispatch`, then `commit` — admission and effect are ONE transaction (`:244`).
   A database fault rolls the admission back with it and answers 503.
6. After the 200, as a background task: the tap's `answerCallbackQuery` or the
   `/start` reply, best effort (`:271`).

**The ordering rule: a delivery that cannot be executed is refused BEFORE
admission, never after.** Admission is irreversible — an admitted update that
was not executed is destroyed, wearing the shape of a successful dedup.

## The dispatcher (`telegram_dispatch.py`)

Served (`TelegramDispatcher.__call__`, `:332`): a `callback_query` (the tap);
a group's migration notice — Telegram retired the group's chat id for a
supergroup's, and the binding follows it (`chat_migration.follow`, #743);
`/start <payload>` for the prefixes `build_router` registers (`:308`) — `link-`
(link a Telegram identity) and `bind-` (a group joins a workspace); and a
message in a bound group (the people it shows become workspace members through
`fn_group_member_seen`). Chat-typed COMMANDS are not served (#854): such an
update is the named outcome `not_a_start`, logged — not a silent drop, and not
a raise, because the delivery is already admitted and a raise would make
Telegram redeliver it forever.

The tap (`_tap`, `:360`): parse the token (`callback_tokens.parse`,
`v1:<action>:<intent-uuid>`; actions `post`, `posted`, `skip`, `reject`, and the
review card's `itposted`, `notposted`, `giveup`) → resolve the chat
(`tenant_resolution.resolve_chat`, the `fn_resolve_binding` door) → resolve the
tapper (`user_identities`; none is `unlinked`) → `apply_gucs` with the tenant,
the actor and a 2 s `lock_timeout` → `commands.execute` as that member, inside a
savepoint. Action → command is `ACTION_TO_COMMAND` (`:82`); the review buttons
all run `resolve_review` with the resolution in `args`, and `notposted` carries
the member's `not_posted` verdict.

Rules the code keeps:

- The executors read the intent `FOR UPDATE` and decide from that read
  (`command_executors._intent_row`, `_settle`). A card not in the state its
  button expects ANSWERS with its current state and writes nothing to the
  intent; two racing taps queue on the lock.
- Every refusal is an answer (`ANSWERS`), never an error. `_tap` raises only for
  a database error; a poisoned update is the named outcome `tap_failed`, and the
  delivery stays admitted.
- Nothing speaks to Telegram inside the transaction. The route answers after the
  commit; the card's edit is an outbox row.
- The workspace's tap admission (`ws_admission`,
  `TARGET_TAP_ADMISSION_PER_MINUTE`) is debited inside the savepoint, only for
  a tap that wrote; at the limit the flip rolls back and the tapper is told.
- This adapter's words stay here: the CLI's sentences
  (`vocabulary.REASON_SENTENCES`) never say tap, button, card or keyboard
  (`TAP_WORDS`), and `ANSWERS` never reach a terminal.

## The card (`prompts.py`)

`render_card` (`:210`) builds the approval card: **Post now** only when the
workspace has `api_publishing_enabled`, **Posted myself**, **Skip**, **Reject**,
and a link to Instagram. `prompt_intent` (`:268`) flips `scheduled →
prompt_pending` and writes one `approval_prompt` outbox row per active push
binding in the SAME transaction; the sweep advances to `awaiting_approval` in
the same pass whether or not a card was delivered, because the web queue is a
surface every workspace has. A workspace with no Telegram binding gets no card
and loses nothing. The words a card shows for a state are `OUTCOME_WORDS`
(`:68`) — the outcome line and a repeat tap's answer read the same table. A
`review_required` intent gets the review keyboard (`review_keyboard`, `:176`).

## Outbound: the outbox (`outbox.py`) and the transport

- **The outbox row is the delivery record** and the only authority on "did this
  send". Kinds: `approval_prompt`, `prompt_supersede`, `notification`, `ack`,
  `invitation`. States: `pending`, `sending`, `sent`, `ambiguous`, `failed`,
  `superseded`. The payload is channel-neutral (`{"v": 1, "text", …}`).
- One sender per chat: the `deliver_outbox` job is keyed `tg:<binding_id>`, so
  `uq_jobs_serialized_lease` admits one live sender per binding. The sender
  sweeper mints it while pending rows exist (`work_loop.ensure_sender_jobs`).
  Every write out of `sending` is a CAS on `sending`.
- The sender commits per checkpoint: pace-and-claim, commit, call Telegram with
  no transaction open, settle in another. A row left `sending` by a dead
  predecessor is recovered by whoever holds the binding's lease — to
  `ambiguous`, never a blind resend.
- Ambiguity is per kind: `notification` / `ack` retry once, then fail;
  `approval_prompt`, `invitation` and `prompt_supersede` resend (`RESEND_KINDS`,
  `:116`), a prompt at most three times. Two live cards for one intent are
  tolerable; a duplicate notification is bounded at one.
- **Edits go supersede-then-send**, never edit-in-place on an ambiguous ref. A
  flip writes `prompt_supersede` rows for every known card of the intent in
  every binding (`supersede_all`, `:504`); that paced edit writes the outcome
  line and removes the keyboard — one Telegram message per tap per binding. The
  route does not strip buttons. A card the ledger never learned (a resend's
  twin) is adopted when it is tapped (`adopt_card`, `:664`).
- Pacing is two `rate_counters` scopes, `tg_chat` per binding and `tg_global`.
  Over budget is a deferral (the row stays `pending`), and a 429 writes the
  provider's `retry_after` as a durable hold — never an in-task sleep.
- A gone chat (`DestinationGone`: kicked, blocked, deleted, migrated) fails the
  row outright, and the sender re-points the binding at the successor chat or
  revokes it (`bindings.follow_or_retire`, the rule the migration notice uses
  too — the notice normally moves the binding first; this is the backstop for
  a send that reaches the old id before it has). It is a chat-level fact,
  never the credential's.
- The transport (`src/channels/telegram_transport.py`) probes `getMe` at worker
  start: a dead token, or a token for a bot other than
  `TARGET_TELEGRAM_BOT_USERNAME`, parks `deliver_outbox` with the reason. The
  bot token never appears in an exception or a log line — every raise path is
  redacted. A card with a `media` block is sent as the photo or video; when the
  file cannot be fetched, or Telegram DEFINITIVELY refuses the upload, the text
  card goes instead. A lost upload answer is not a refusal: it propagates as
  ambiguous, like any send (`for_chat`, `:469`).

## Registration

Telegram delivers only the update kinds a registration asks for; the one
spelling is `vocabulary.ALLOWED_UPDATES` (`message`, `callback_query`), with the
variable names beside it (`vocabulary.py:384`-`:398`). The API registers itself
at startup in Railway's production environment (`app.py:401`;
`TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER` overrides), and `/health` reports the
registration and Telegram's live backlog. `storydump webhook status` checks the
bot, the webhook and the door and changes nothing; `register` and `deregister`
re-point the production bot and are in `CLAUDE.md`'s safety block.
