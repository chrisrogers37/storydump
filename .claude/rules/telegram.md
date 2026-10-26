---
paths:
  - "src/api/routes/webhooks.py"
  - "src/channels/telegram_ops_notice.py"
  - "src/channels/telegram_transport.py"
  - "src/channels/telegram_waitlist_ping.py"
  - "src/channels/telegram_webhook_registration.py"
  - "src/services/target/prompt_rendering.py"
  - "src/services/target/telegram_dispatch.py"
  - "src/services/target/webhook_ingress.py"
  - "src/services/target/callback_tokens.py"
  - "src/services/target/prompts.py"
  - "src/services/target/outbox.py"
---

# Telegram (the target adapter)

Telegram is one channel adapter over the ledger: inbound through a webhook on
the API, outbound through `channel_outbox` and the worker's sender. Nothing
polls, and nothing inland of the adapter sees a chat id. Checking the webhook
is `storydump webhook status` (Registration, below); reading what was sent is
`storydump cards <intent_id>` and `storydump outbox`.

## Inbound: the webhook (`src/api/routes/webhooks.py`)

`POST /webhooks/telegram` (`:144`, mounted at `app.py:1050`). In order:

1. The secret header must match `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` — 403
   otherwise, and an unset secret refuses every delivery (`:165`).
2. No dispatcher wired (`app.state.ingress`, which exists only with an engine) →
   503 **before admission**, so Telegram redelivers (`:203`).
3. The pool's wait ran out at checkout → a tap is answered "Busy — tap again."
   and consumed; anything else is 503 and redelivered (`_refuse_saturated`,
   `:296`).
4. `admit` (`webhook_ingress.py:137`): one `INSERT … ON CONFLICT` into
   `command_dedup` keyed on the `update_id`. A replay is acknowledged without
   re-execution; the same key with different content is 409.
5. `dispatch`, then `commit` — admission and effect are ONE transaction (`:243`).
   A database fault rolls the admission back with it and answers 503.
6. After the 200, as a background task: the tap's `answerCallbackQuery` or the
   `/start` reply, best effort (`:269`).

**The ordering rule: a delivery that cannot be executed is refused BEFORE
admission, never after.** Admission is irreversible — an admitted update that
was not executed is destroyed, wearing the shape of a successful dedup.

## The dispatcher (`telegram_dispatch.py`)

Served (`TelegramDispatcher.__call__`, `:559`): a `callback_query` (the tap);
a group's migration notice — Telegram retired the group's chat id for a
supergroup's, and the binding follows it (`chat_migration.follow`);
`/start <payload>` for the prefixes `build_router` registers (`:458`) — `link-`
(link a Telegram identity: the `/start` only asks, naming the account by a
masked email; the prompt's Confirm tap, `v1:linkok:<state>:<tg-user>` from
`callback_tokens.link_token`, links — `identity_link.handle_tap` — and only for
the Telegram user it was offered to) and `bind-` (a group joins a workspace); and a
message in a bound group (the people it shows become workspace members through
`fn_group_member_seen`). Chat-typed COMMANDS are not served: such an
update is the named outcome `not_a_start`, logged — not a silent drop, and not
a raise, because the delivery is already admitted and a raise would make
Telegram redeliver it forever.

The tap (`_tap`, `:595`): parse the token (`callback_tokens.parse`,
`v1:<action>:<intent-uuid>`; actions `post`, `posted`, `skip`, `reject`, the
review card's `itposted`, `notposted`, `giveup`, and Configure's `cfg`, `back`,
`aa1`, `aa0`, served by `_menu` below) → resolve the chat
(`tenant_resolution.resolve_chat`, the `fn_resolve_binding` door) → resolve the
tapper (`user_identities`; none is `unlinked`) → `apply_gucs` with the tenant,
the actor and a 2 s `lock_timeout` → `commands.execute` as that member, inside a
savepoint. Action → command is `ACTION_TO_COMMAND` (`:89`); the review buttons
all run `resolve_review` with the resolution in `args`, and `notposted` carries
the member's `not_posted` verdict.

Every delivery is dispatched with the API's past-posts capability on its
connection, read from `app.state` as the delivery runs, as the web's unit of
work reads it for each request (`principal.open_tenant`): `create_app` hands
the dispatcher a reader (`TelegramDispatcher(capability=lambda: …)`,
`app.py:994`), so Post now's `approve` admits a past-posts story's media as
the web's Approve does, and an app armed after `create_app` arms its taps too.
The connection is pooled, and `AsyncConnection.info` is the pooled DBAPI
connection's own dict, which outlives the checkout, so the dispatch puts back
what the connection carried before (`_carrying_capability`). At startup the
API logs `telegram ingress: instagram past posts on|off`. A refusal's closed
suitability reason (`CommandRefused.facts["source_reason"]`) ends both tap log
lines as `source_reason=<code>` (`TapResult.source_reason`); the API process
emits the route's `tap answered …` line, not the dispatcher's module-logger
one.

Two buttons are on no card, so `_tap` sends their taps on first, before any
card parse or chat resolution. Both are user-plane: no tenant, no command port,
no tap admission debit. The link prompt's Confirm and Cancel go to
`_link_tap`. The waitlist ping's ✅ Admit, `v1:admit:<ref>`
(`callback_tokens.admit_token`; the ref is `signup_admissions.ref`, never the
address), goes to `_admit_tap`, for operators only: a linked user in
`OPS_USER_IDS` admits the ping's entry through `signup_admissions.admit` in the
delivery's transaction, and the route edits the ping to say so, its buttons
gone (Open waitlist ↗, the URL button beside Admit, goes too). Anyone else
hears one refusal.

Rules the code keeps:

- The executors read the intent `FOR UPDATE` and decide from that read
  (`command_executors._intent_row`, `_settle`). A card not in the state its
  button expects ANSWERS with its current state and writes nothing to the
  intent; two racing taps queue on the lock.
- Every refusal is an answer (`ANSWERS`), never an error. `_tap` raises only for
  a database error; a poisoned update is the named outcome `tap_failed`, and the
  delivery stays admitted.
- Nothing speaks to Telegram inside the transaction. The route answers after the
  commit. A card command's edit is an outbox row; a menu tap's is the route's
  one edit (below).
- The workspace's tap admission (`ws_admission`,
  `TARGET_TAP_ADMISSION_PER_MINUTE`) is debited inside the savepoint, only for
  a tap that wrote, and for every Configure tap, navigation included, so one
  workspace's burst of menu taps is capped; at the limit the flip rolls back
  and the tapper is told.
- This adapter's words stay here: the CLI's sentences
  (`vocabulary.REASON_SENTENCES`) never say tap, button, card or keyboard
  (`TAP_WORDS`), and `ANSWERS` never reach a terminal.

## The card (`prompts.py`, `prompt_rendering.py`)

`prompt_rendering.render_card` (`prompt_rendering.py:375`) builds the approval card: **Post now** only when the
workspace has `api_publishing_enabled`, **Posted myself**, **Skip**, **Reject**,
and a link to Instagram. `prompts.prompt_intent` (`prompts.py:154`) flips `scheduled →
prompt_pending` and writes one `approval_prompt` outbox row per binding a push
may reach (`bindings.deliverable_binding_where`: active, Telegram, and a private
chat only while its person belongs to the workspace) in the SAME transaction; the sweep advances to `awaiting_approval` in
the same pass whether or not a card was delivered, because the web queue is a
surface every workspace has. A workspace with no Telegram binding gets no card
and loses nothing. The words a card shows for a state are `OUTCOME_WORDS`
(`prompt_rendering.py:39`) — the outcome line and a repeat tap's answer read the same table.

**Every approval card's keyboard is the sender's to render**, and it reaches a
chat only as an edit of a message whose id the ledger holds: a card is sent
with no keyboard, and settling its send queues the edit that brings one
(`outbox._edit_sent_card`), so a copy a lost answer left behind has no
buttons. `prompt_rendering.card_keyboard` is the one spelling, and the sender applies it to an
edit of a card once the row's pacing has passed (`outbox._with_the_storys_keyboard`
over `prompts.card_facts`, one read of the story and its item). No outbox door
takes a keyboard, so no caller picks one, and a rejected card loses its
buttons because it is rejected. ⚙️ **Configure** is on every card for the
card's whole life, except a rejected story's or a removed item's:
- a waiting card shows the decision buttons, with Configure beside 📱 Open Instagram;
- a `review_required` card shows the three resolutions, then Configure;
- any other card shows Configure alone.

Configure opens a menu one level down (`menu_keyboard`): 🔁 Auto-approve this
item (on/off, the member command `set_item_auto_approve`), ⚙️ Approval settings
↗ (only for an https web origin), and ‹ Back.
- **Taps:** a menu tap (`cfg`, `back`, `aa1`, `aa0`) passes the gate the flag's
  command applies (`commands.authorize`) and spends the tap's admission.
- **The edit:** the menu edits the tapped message itself, once, after the
  commit, through the route's one edit seam (`TapResult.edit_markup` →
  `editMessageReplyMarkup`). It never goes through the outbox.
- **Healing:** a menu tap on a card whose story carries no keyboard (a
  rejected story's, a removed item's) heals it on touch. Back and the heal read
  the story as the sender does (`card_facts`, `keyboard_of`).
- **The toggle:** it never decides the card it was tapped on; the flag
  acts the next time the item is served. Its answer names what still holds
  the flag back as the serve step decides it (`auto_approval.decide`, read
  after the write): the first block, as a waiting card's note does.

A planned story's card names who planned it, and a repeating one adds its rule
in `vocabulary.describe_repeat`'s words, naming the account's zone where it is
not the workspace's (`prompt_rendering._planned_line`; the rule is read in `prompts._with_scheduler`,
since neither the due door nor `_CARD_SELECT` carries it). A miss notice for a
repeating story says whether the series goes on, from the ledger after the
miss. A repeat's occurrence born after its time ends with no notice while its
series goes on; one whose miss stops the series says so
(`prompts.sweep_planned_misses`).

The card and its notices name the person who planned a story only while they
are still a member of the workspace (`identity.member_name_sql`). A series
outlives the person who planned it, and after they leave it reads as planned,
with no name. A card's header keeps the rule it was sent with, so when an act
ends the series, the outcome line under it says so: "the series stopped" after
a cancel with `stop_repeat`, and "the series ended" after a Reject
(`prompt_rendering.SERIES_STOPPED`, `SERIES_ENDED`). A rule cleared while its card is
still waiting adds no line, so that header keeps the rule until the card
settles.

A story a rule approved at serve keeps its card, which is sent settled.
`rerender_prompt` renders the card as usual and puts the outcome line naming
the mechanism ("✅ Approved by YOLO · …") in the payload's `outcome_text`, the
outbox's own record of the line a card carries. Its keyboard is the sender's,
as every card's is (above): ⚙️ Configure alone, or none for a removed item. The
transport sends that line under the card's text or caption, as an edit does,
and the sender records the card `superseded`. The card's own text stays its
header, so a later line replaces the first instead of stacking. The
settled-card sweep spares the pending card for `prompts.SETTLED_CARD_GRACE`.
The rule's label ("YOLO", "Auto-approve", "Returning items") names a line only
where no person moved the story, and only on the approved, waiting, posting and
posted lines, a dry run's included. A waiting card whose rule a block stopped
says so in one line under its slot (`prompt_rendering.block_note`).

## Outbound: the outbox (`outbox.py`) and the transport

- **Two sends go outside it, to operators, never to a workspace:** the API's
  waitlist ping (`telegram_waitlist_ping`) and the worker's ops notice
  (`telegram_ops_notice`), a direct message to each person in `OPS_USER_IDS`
  who has linked Telegram. An operator's private chat is no workspace's, so
  neither is an outbox row.
- **The outbox row is the delivery record** and the only authority on "did this
  send". Kinds: `approval_prompt`, `prompt_supersede`, `notification`, `ack`,
  `invitation`. States: `pending`, `sending`, `sent`, `ambiguous`, `failed`,
  `superseded`. The payload is channel-neutral (`{"v": 1, "text", …}`).
- One sender per chat: the `deliver_outbox` job is keyed `tg:<binding_id>`, so
  `uq_jobs_serialized_lease` admits one live sender per binding. The sender
  sweeper mints it while pending rows exist (`work_loop.ensure_sender_jobs`);
  its door reads no membership, so the sender re-checks
  `bindings.deliverable_binding_where` at claim and retires the queue of a
  binding it refuses.
  Every write out of `sending` is a CAS on `sending`.
- The sender commits per checkpoint: pace-and-claim, commit, call Telegram with
  no transaction open, settle in another. A row left `sending` by a dead
  predecessor is recovered by whoever holds the binding's lease — to
  `ambiguous`, never a blind resend.
- Ambiguity is per kind: `notification` / `ack` retry once, then fail;
  `approval_prompt` and `invitation` resend at most three times
  (`RESEND_KINDS`, `MAX_PROMPT_RESENDS`); a `prompt_supersede` resends until
  Telegram confirms it, for at most `EDIT_RESEND_HORIZON_SECONDS`. Each lost
  answer waits `AMBIGUOUS_RESOLVE_AFTER_SECONDS` times its attempts
  (`fn_sender_sweep` mints the sender on the same schedule).
  A card's resend leaves no second live card, since a card leaves without its
  keyboard; what a lost answer can leave is one inert copy, which no tap
  reaches. A duplicate notification is bounded at one.
- **Edits go supersede-then-send**, never edit-in-place on an ambiguous ref. A
  flip writes `prompt_supersede` rows for every known card of the intent in
  every binding (`supersede_all`, `:610`); that paced edit writes the outcome
  line with the story's keyboard as the sender claims it (⚙️ Configure alone on
  a settled card, or none for a rejected story or a removed item) — one Telegram
  message per tap per binding. The route edits a card only for Configure's menu
  (`edit_markup`). A card with buttons the ledger never learned is adopted
  when it is tapped, and a tapped card whose newest edit did not land is
  edited again (`adopt_card`). "Message to edit not found" fails the edit
  after one call: the message is gone.
- Pacing is two `rate_counters` scopes, `tg_chat` per binding and `tg_global`.
  Over budget is a deferral (the row stays `pending`), and a 429 writes the
  provider's `retry_after` as a durable hold — never an in-task sleep.
- A gone chat (`DestinationGone`: kicked, blocked, deleted, migrated) fails the
  row outright, and the sender re-points the binding at the successor chat or
  revokes it (`bindings.follow_or_retire`, the rule the migration notice uses
  too — the notice normally moves the binding first; this is the backstop for
  a send that reaches the old id before it has). It is a chat-level fact,
  never the credential's.
- A dead credential (`CredentialDead`: Telegram's 401) fails the row outright
  too, because every resend would meet the same answer until the token is
  replaced. The binding stays — the chat is not gone — and the
  transport is the loud surface: one log line, and `auth_failures` counting.
- The transport (`src/channels/telegram_transport.py`) probes `getMe` at worker
  start: a dead token, or a token for a bot other than
  `TARGET_TELEGRAM_BOT_USERNAME`, parks `deliver_outbox` with the reason. The
  bot token never appears in an exception or a log line — every raise path is
  redacted. A card with a `media` block is sent as the photo or video; when the
  file cannot be fetched, or Telegram DEFINITIVELY refuses the upload, the text
  card goes instead. A lost upload answer is not a refusal: it propagates as
  ambiguous, like any send (`for_chat`, `:612`).

## Registration

Telegram delivers only the update kinds a registration asks for; the one
spelling is `vocabulary.ALLOWED_UPDATES` (`message`, `callback_query`), with the
variable names beside it (`vocabulary.py:735`-`:744`). The API registers itself
at startup in Railway's production environment (`_register_webhook`, `app.py:689`;
`TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER` overrides), and the operating details
(`/api/v1/ops/health`, for `OPS_USER_IDS`) report the registration and
Telegram's live backlog. `storydump webhook status` checks the
bot, the webhook and the door and changes nothing; `register` and `deregister`
re-point the production bot and are in `CLAUDE.md`'s safety block.
