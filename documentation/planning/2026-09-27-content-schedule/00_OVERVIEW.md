---
title: "[plan] Content schedule: serve one chosen item into approval at a set time (#1413)"
type: plan
status: draft
owner: astrid
created: 2026-09-27
tags: [storydump, content-schedule, post-intents, approval, command-port, issue-1413]
repos: storydump
---

# Content schedule — one chosen item, at a set time, into approval (#1413)

## Summary

A member picks one media item, one account, and a local date and time. At that time the item
enters the normal approval flow: a card in every bound chat and a row in the Queue, both marked
as scheduled and by whom. Only a person's approval publishes it. The account's cadence does not
change: the item takes no slot, and no slot displaces it. Before it is served it can be moved or
cancelled. If it cannot be served at its time, the bound chats are told; it never lapses in
silence.

Seven phases, each landing as one reviewed PR, except phase 1, which lands as two (1a and 1b). Every code reference in the issue was checked
against `main` at `f228f4f87`, and all of them hold. The check also found nine gaps the issue does
not name (G1–G9 below). Three of them would let today's machinery drop a planned item silently
(G1–G3). The plan closes them before any planned row can exist.

**Decisions:** all eleven forks are locked, and each fork links its lock. The owner ratified the
product forks F7–F11 (locks, the daily cap, lateness and pause, the link to add, the new verbs),
and the team locked the engineering forks F1–F6. **Still open: the owner's approval of phase 3's
schema change** (089, PR #1471), listed under "Owner decisions" below; phases 1 and 2's are on main.

Issue: https://github.com/chrisrogers37/storydump/issues/1413

## Evidence

Every reference in #1413, re-read at `f228f4f87` (equal to `origin/main` on 2026-09-27). All hold.
The last column adds what the check found beside each claim.

| Issue claim | At `f228f4f87` | Found beside it |
|---|---|---|
| `execute_plan_slot`, `scheduler.py:295` | holds | |
| the `plan_slot` payload carries only the account and the slot | holds: `083_clock_tick_deadlines.sql:108` builds `{v, ig_account_id, slot_at}` | a cadence intent is minted once `next_slot_at <= now()` (`083:102`), so it is born at or after its slot |
| writes go through `commands.py` and `vocabulary.COMMANDS` | holds: `vocabulary.py:29-56`, floors `commands.py:105-111` | a new verb changes the vocabulary, which `PROJECT_MISSION.md:93-94` puts under owner approval (G7) |
| `approval_mode` and `auto_reapprove_returning` are stored, and nothing acts on them | holds (details below) | |
| locks are enforced only in the slot query, `scheduler.py:362-374` | holds: the lock clause is `:369-373`; every other `post_locks` hit is a writer or the reaper deleting expired locks (`059:373`, `076:46`) | six lock kinds exist (`054:324-326`). `skip`, `reject` and `recent` have writers; `unsupported`, `seasonal` and `hold` have none in `src/` |
| `uq_intent_slot`, `055:246` | holds | it has no `WHERE`, so a terminal row keeps its slot key forever (G4) |
| `uq_intent_live_subject`, `055:249` | holds | |
| the daily cap: `cap_consumed_on`, `_say_waiting(day_spent=...)` | holds: `publish_pipeline.py:639` (call), `:999` (definition) | debit `publish_cap.py:133-159`, refunds `:204` and `:239`; a `publishing` row must carry `cap_consumed_on` (`055:174`) |
| the reaper expires past-due `scheduled` and `prompt_pending` rows, paused workspaces included, and `awaiting_approval` rows after `approval_ttl_minutes` (`076`) | holds: `076:33-36` and `:38-44` | there is no grace period: a row can be expired the instant its time passes (G1) |
| `cancel` only sets a flag, and nothing ends a waiting `scheduled` intent (#1235) | holds: `command_executors.py:783`; #1235 is open | the flag change writes no audit row, because `trg_intent_audit` fires only on a state change (`055:328-343`) (G8) |
| the prompt sweep `fn_prompts_due` (`082`) serves due rows | holds: `082:105-121` | it filters on state, time and workspace only, not on `cancel_requested`, media state, account state or locks (G2, G3) |
| the Queue and the calendar already list such rows | holds: the Queue reads `NON_TERMINAL_STATES` (`landing/src/app/(dashboard)/dashboard/queue/page.tsx:4`); the calendar plots `schedule_slot_at` (`landing/src/app/(dashboard)/dashboard/media/calendar/page.tsx:90-96`) | |
| the next migration is 084 | holds: the last is `083`, and no open PR adds one (checked 2026-09-27) | the number is taken at merge time, so each phase takes the next free one |
| the publish leg sends only `media_type` and the media URL, `instagram_graph.py:134-137` | holds | |
| `media_items.link_url` exists and nothing reads it (#418) | holds: `054:282`, model `accounts_sources_media.py:272` | nothing writes it either, so a link needs a verb (G9) |

**Only a person approves, and nothing acts on the two settings** (issue rule 3, confirmed):

- `approval_mode` and `auto_reapprove_returning` can be set through `settings_change`
  (`workspaces.py:60-61`), are stored (`053:136-138`), are copied onto every cadence intent
  (`work_loop.py:249` to `scheduler.py:483`), and are displayed (`workspaces.py:395`,
  `storydump_cli/output.py:273`, the web's settings and Queue types). No reader in `src/`, the
  CLI, the web or any SQL function branches on either value.
- The only edge from `awaiting_approval` to `approved` is driven by the `approve` command, floor
  `member` (`commands.py:106`). The other two writers of `approved` start from other states:
  `publish_cap.py:251` is `review_required → approved` (a person's `resolve_review`), and
  `publish_pipeline.py:962` is the `publishing → approved` wait edge (`076:17`). There is no
  `scheduled → approved` edge (`055:291-304`).

**Gaps the issue does not name.**

- **G1 — a planned row can expire silently.** The reaper's first leg (`076:33-36`) expires any
  `scheduled` or `prompt_pending` row once `schedule_slot_at < now()`, with no grace and in paused
  workspaces too. Cadence rows survive it only because they are minted and prompted in the same
  transaction (the `plan_slot` adapter in `work_loop.py`). A planned row is served by the
  5-second sweep instead, so it loses that race whenever the worker is down at its time.
- **G2 — a cancelled planned row would still be served.** `fn_prompts_due` does not read
  `cancel_requested`. The row would arrive as a card with no buttons ("Cancelling", #1233's
  belt) and then expire at the approval TTL.
- **G3 — an unservable planned row would still be served.** Removed or unsupported media, a
  disabled account and a later `reject` lock all pass `fn_prompts_due`.
- **G4 — slot-key collisions.** `uq_intent_slot` is unconditional. A planned row at a cadence
  instant would make `plan_slot` mint nothing for that slot, and a cancelled one would block
  that instant forever.
- **G5 — cancel-then-reschedule is blocked.** `uq_intent_live_subject` still counts a
  cancel-flagged row as live, so the same item cannot be scheduled again for that account until
  #1235 ends the row. That leg runs with the reaper, which as built is minted every 6 hours
  (#1329), so the block could last up to six hours; phase 1a restores the designed 60 seconds.
- **G6 — a partial slot index breaks cadence minting.** `plan_slot` spells its conflict target
  without a predicate (`scheduler.py:485`). Against a partial index Postgres then finds no
  arbiter and raises on every mint, which would stop cadence on every workspace.
- **G7 — new verbs need the owner.** The command vocabulary is owner-gated
  (`PROJECT_MISSION.md:93-94`), just as schema is (`:92`).
- **G8 — `cancel` is not audited today.** It changes a flag, not a state. Requirement 7 needs
  an audit row.
- **G9 — nothing writes `link_url`.** "A link to add on the item" needs a verb.
- **Checked and clean.** No health or ops reader treats `scheduled` as short-lived (checked:
  `scheduling_health`, `posting_health`, `ops_views`, `worker_health`, `readers`, `081`), so a
  planned row that sits in `scheduled` for weeks is not reported as stuck. Posting hours do not
  gate publishing (`publish_pipeline.py` never reads them), so an approved 23:00 item publishes
  at once.

## Architecture

A planned item is a `post_intents` row in the ledger cadence already uses. Three things tell it
apart: `origin = 'planned'` (cadence rows get `'cadence'`, the default), `scheduled_by_user_id`,
and `approval_mode = 'manual'`, enforced by a CHECK. Its `schedule_slot_at` is the chosen instant,
and the `schedule_item` verb creates it in `scheduled`.

```
schedule_item ──▶ scheduled (future) ◀── reschedule_item moves the time in place
                    │   cancel ──▶ cancel_requested ──▶ cancelled   (#1235's reaper leg)
                    ▼ its time comes
   servable, and within the late window?
     ├─ yes ──▶ prompt_pending ──▶ awaiting_approval ──▶ a person approves ──▶ publish
     │            card and Queue row say "Scheduled by <name>"      (outside the daily cap, F8)
     └─ no  ──▶ expired, last_error {class: planned_missed, reason}, and a notice in every bound chat
```

- **Serve.** `fn_prompts_due` serves a planned row only while it is servable (the media is
  available, the account is active or awaiting reconnection, and no blocking lock applies, F7)
  and still within the late window (F9). It never serves a cancel-flagged row, whatever its
  origin.
- **Miss.** A new door returns the due planned rows that cannot be served or are past the late
  window. The worker expires each one with its reason and tells the bound chats in the same
  transaction. The reaper's silent expiry leg stops touching planned rows, so nothing can end a
  planned row unserved without saying so.
- **Approve.** A trigger admits `awaiting_approval → approved` on a planned row only when the
  actor is a person (`app.actor_kind = 'user'` with a user id). That makes issue rule 3
  structural, in the database, which is the one authority for state.
- **Cadence.** Unchanged. `uq_intent_slot` becomes cadence-only (a partial index), so the two
  kinds of row never collide, and the cadence draw already skips an item that is live for the
  account.
- **Publish.** Unchanged except the daily cap (F8): a planned post neither spends the cap nor
  waits on it. Meta's own limit, pause and publish exclusivity still apply.

## Decision Forks

Each fork carries its call: **ENGINEERING** (the team decides; ratifier ari) or **PRODUCT** (the
owner decides).

### Fork F1: Where a planned item lives — ENGINEERING
- **Context:** the issue offers two storage shapes.
- **Options:** **(a)** a future-dated `post_intents` row with `origin = 'planned'`. The serve sweep,
  the Queue and the calendar already see such rows; the live-subject key blocks double-scheduling
  and keeps the item out of that account's cadence draw; there is one ledger. · **(b)** a separate
  table, plus a clock leg or job kind that mints the intent at the time. Editing or cancelling
  before the serve touches only the new table, so it needs no #1235. But it is a second ledger of
  future posts: a new table with RLS, grants and audit, a `fn_clock_tick` or `ck_jobs_kind` change,
  new Queue and calendar readers, a new predicate in the cadence draw, and the minted intent still
  meets `uq_intent_slot` at its time.
- **Lean:** (a). The one real advantage of (b) is cancelling without #1235, and #1235 is owed
  anyway: it breaks `cancel` and `disable_account` today. "Consolidate, don't fork."
- **Ratifier:** ari · **Status:** locked (a)
- **Evidence:** G1–G5 are what (a) must pay; phases 2 and 3 pay it. [FORK-LOCK F1 by ari](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5859737736) · review concurrence: [rajan](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5859711758).

### Fork F2: The slot-key collision — ENGINEERING
- **Context:** G4.
- **Options:** **(a)** make `uq_intent_slot` a partial index, `WHERE origin = 'cadence'`, expanded
  and contracted across two deploys (G6), the contract only once phase 2 is deployed and drained on
  every worker (`railway.toml:30`, `drainingSeconds = 60`) · **(b)** keep the index and refuse planned times that
  equal a cadence instant. Cadence instants move whenever posting hours or posts-per-day change,
  and terminal planned rows still hold their keys. · **(c)** keep the index and offset planned
  instants by one second. That relies on `fn_next_slot` staying minute-aligned forever.
- **Lean:** (a). The key exists so that "re-running slot planning cannot double-create"
  (`055:245`), and a planned row is not slot planning.
- **Ratifier:** ari · **Status:** locked (a)
- **Evidence:** [FORK-LOCK F2 by ari](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5859737838) · review concurrence: [rajan](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5859711986).

### Fork F3: Cancelling before the serve — ENGINEERING
- **Context:** `cancel` only flags, #1235 adds the leg that ends the row, and the port's rule is
  that the user never writes a terminal state (`command_executors.py:37`).
- **Options:** **(a)** build #1235 first (phase 1), reuse `cancel`, and make the serve door skip
  flagged rows · **(b)** let the cancel command write `scheduled → cancelled` directly for planned
  rows, breaking that rule · **(c)** ship without cancel before the serve; people Skip the card
  when it arrives.
- **Lean:** (a). **The dependency is hard: phase 5 (the verbs) must not ship before phase 1.**
  Phase 3 takes planned rows out of the reaper's expiry leg, and the miss door skips flagged
  rows, so without #1235 a cancelled planned row would stay live forever and block that item for
  that account (`uq_intent_live_subject`).
- **Ratifier:** ari · **Status:** locked (a)
- **Evidence:** [FORK-LOCK F3 by ari](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5859737938) · review concurrence: [rajan, F3](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5859712177), [rajan, sequencing](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5859710692).

### Fork F4: Changing the time — ENGINEERING
- **Options:** **(a)** update `schedule_slot_at` in place while the row is planned, `scheduled`
  and not cancel-flagged. This is legal: the guard freezes only terminal rows (`055:306-323`) and
  `svc_ingress` holds UPDATE (`057:100-106`). It keeps the row id and needs no #1235. · **(b)**
  cancel and re-create, which depends on #1235 and on the old row leaving
  `uq_intent_live_subject`.
- **Lean:** (a). A race with the serve sweep resolves to "already served": the guarded update
  matches no row, and the verb answers `illegal_transition`.
- **Ratifier:** ari · **Status:** locked (a)
- **Evidence:** [FORK-LOCK F4 by ari](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5859738116) · review concurrence: [rajan](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5859712386).

### Fork F5: Where the serve and miss rules live — ENGINEERING
- **Options:** **(a)** in the SQL doors: `fn_prompts_due` returns only servable planned rows, and
  a sibling door returns the unservable and late ones · **(b)** a Python check in `prompts.py`
  before prompting.
- **Lean:** (a). The mission says "the database decides what a story may become next; no Python
  pre-check copies that rule" (`PROJECT_MISSION.md`, One authority), and a door is one read for
  the whole sweep.
- **Ratifier:** ari · **Status:** locked (a)
- **Evidence:** [FORK-LOCK F5 by ari](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5859738238) · review concurrence: [rajan](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5859712782).

### Fork F6: Making person-only approval structural — ENGINEERING
- **Context:** issue rule 3 holds today only because nothing acts on the two settings, and any
  future PR could change that.
- **Options:** **(a)** a trigger that lets a planned row enter `approved` from `awaiting_approval`
  only when `app.actor_kind = 'user'` and `app.actor_user_id` is set, plus a CHECK that planned
  rows are `approval_mode = 'manual'` · **(b)** the CHECK only · **(c)** tests only.
- **Lean:** (a), for planned rows only. Applying it to cadence rows would pre-empt the stored
  but unbuilt auto mode, which is the owner's call, not this plan's. The risk is a trigger that
  blocks a real approve path, so phase 2 drives every adapter's approve through it before merge:
  the Telegram tap sets `actor_kind="user"` (`telegram_dispatch.py:441`), and the web and CLI
  paths get the same test.
- **Ratifier:** ari · **Status:** locked (a), planned rows only
- **Evidence:** [FORK-LOCK F6 by ari](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5859738368) · review concurrence: [rajan](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5859712979).

### Fork F7: Locks and item state — PRODUCT
- **Context:** the issue suggests that a permanent `reject` or removed media blocks scheduling,
  and that a live `recent` or `skip` lock shows a warning the person can override. There are six
  lock kinds, and an item can also change between scheduling and its time.
- **Options:** **(a)** block on removed or unsupported media and on `reject` and `unsupported`
  locks; warn with an override on `skip` and `recent`; block on `hold` and `seasonal` (nothing
  writes them today). At the time, only a blocker causes a miss; a warning the person overrode,
  or a `skip` added later, does not. An account awaiting reconnection is still served: Posted
  myself works, and the existing reconnect prompt keeps running. · **(b)** block on any live lock,
  which is the cadence rule · **(c)** warn on everything except media that cannot post.
- **Lean:** (a): the issue's suggestion, extended to all six kinds and to the moment of serving.
- **Ratifier:** owner · **Status:** locked (a)
- **Evidence:** [FORK-LOCK F7](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5861426311), ratified by the owner on Telegram (2026-09-28), relayed by ari.

### Fork F8: The daily cap — PRODUCT
- **Context:** `posts_per_day` (`053:129`, with a per-account override at `054:74`) is the cadence's own cap;
  the counter it is enforced against is "OUR product cadence cap — never Meta's" (`daily_post_counts`, `055:242`). A spent day defers an approved story to tomorrow (`publish_pipeline.py:639`).
- **Options:** **(a)** exempt: a planned post neither spends the cap nor waits on it · **(b)** it
  spends but never waits, so it counts toward the day and can push that day's last cadence story
  to tomorrow · **(c)** it is treated like cadence, and a spent day moves the planned post to
  tomorrow.
- **Lean:** (a). It is the only option under which "the cadence is unchanged that day" holds.
  Meta's own limit still applies through the pre-publish check.
- **Ratifier:** owner · **Status:** locked (a)
- **Evidence:** [FORK-LOCK F8](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5871950570), ratified by the owner on Telegram (2026-09-28), relayed by ari.

### Fork F9: Lateness and pause — PRODUCT
- **Context:** G1. The sweep runs every 5 seconds, so "late" means the worker was down or the
  workspace was paused at the time.
- **Options:** how late may it still be served? **(a)** 15 minutes · **(b)** 60 minutes · **(c)**
  until the end of the account's local day. And a pause at the time: **(i)** if the pause ends
  inside the window, serve late · **(ii)** a pause at the time is always a miss.
- **Lean:** (b) with (i). A planned drop 40 minutes late is still worth asking about, and a
  few-minute pause should not cost it. Past the window it is a miss, with a notice. The window
  is a worker setting passed to the doors, so changing it later needs no migration.
- **Ratifier:** owner · **Status:** locked (b) with (i)
- **Evidence:** [FORK-LOCK F9](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5882764379), (b) chosen by the owner; (i), the team lean, confirmed by the owner ("sure", 2026-09-29); relayed by ari.

### Fork F10: The link to add — PRODUCT
- **Context:** stories published through the API cannot carry stickers (Meta's
  `POST /{ig-user-id}/media` reference), so a link can only be added by hand.
- **Options:** **(a)** an item-level link in the existing `media_items.link_url` (no schema
  change), shown on the card, with Post now hidden for linked items so that Open Instagram and
  Posted myself remain · **(b)** as (a), but with Post now shown under a "no link" label · **(c)**
  a per-schedule link in a new `post_intents` column · **(d)** a separate issue after this ships.
- **Lean:** (a), as the last phase. One implication to accept: the link belongs to the item, so
  the cadence cards for that item show it and lose Post now too.
- **Ratifier:** owner · **Status:** locked (a)
- **Evidence:** [FORK-LOCK F10](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5882764569), the team lean, confirmed by the owner ("sure", 2026-09-29), relayed by ari.

### Fork F11: The new verbs — PRODUCT (owner-gated, `PROJECT_MISSION.md:93-94`)
- **Options:** **(a)** add `schedule_item` and `reschedule_item` at floor `member` (the floor of
  `approve` and `cancel`), `set_item_link` if F10 is in scope, and one refusal reason, `locked`.
  The web adapter's status table is pinned total over `REASONS` (`vocabulary.py:58-72`), so the
  reason is part of the ask. Cancel reuses `cancel`. · **(b)** a single `schedule_item` verb whose
  arguments also carry edits (fewer verbs, a wider contract).
- **Sub-question:** may an agent schedule? Lean yes: scheduling asks a person and posts nothing,
  and approving stays with a person.
- **Lean:** (a).
- **Ratifier:** owner · **Status:** locked (a)
- **Evidence:** [FORK-LOCK F11](https://github.com/chrisrogers37/storydump/pull/1414#issuecomment-5882764782), the team lean, confirmed by the owner ("sure", 2026-09-29), relayed by ari; `set_item_link` is in, since F10 is.

## Owner decisions

The short list for the owner. Items 1–5 are locked (2026-09-28 and 2026-09-29; see each fork).
Item 6, the schema changes, is open for phase 3 only. Everything else is the team's.

1. **F7 — locks and item state:** what blocks scheduling, what warns with an override, and what
   causes a miss at the time. Lean: the issue's suggestion, extended to all six lock kinds.
2. **F8 — the daily cap:** is a planned post exempt? Lean: yes, it neither spends the cap nor
   waits on it.
3. **F9 — lateness and pause:** how late may a planned item still be served, and does a pause
   that ends in time still serve it? Lean: up to 60 minutes late, and yes.
4. **F10 — the link to add:** item-level or per-schedule, and is Post now hidden or labeled?
   Lean: item-level, Post now hidden, last phase.
5. **F11 — the new verbs:** `schedule_item`, `reschedule_item` (and `set_item_link`) at floor
   `member`, the refusal reason `locked`, and whether an agent may schedule. Lean: yes to all.
6. **The schema changes** (`PROJECT_MISSION.md:92`). The plan does not presume where "schema"
   ends and "door change" begins, so every DDL change is listed here:
   - **Phase 1a (#1329):** `fn_clock_tick`'s recurring leg returns its own kind's expired lease to
     `ready` before its guard, plus `GRANT UPDATE (state, locked_by, lease_token, locked_until) ON
     jobs TO svc_clock`. (The reaper's designed cadence, every 60 seconds and 500 rows a sweep, is a
     worker setting, not DDL.)
   - **Phase 1b (#1235):** `fn_reaper_sweep` gains the leg that cancels flagged waiting intents.
     Function body only.
   - **Phase 2:** `post_intents.origin` (text, default `'cadence'`, CHECK);
     `post_intents.scheduled_by_user_id` (uuid, references `users`, `ON DELETE SET NULL`); a CHECK
     that planned rows are `manual`; a cadence-only partial unique index on (workspace, account,
     slot); a trigger that lets only a person approve a planned row.
   - **Phase 3:** the old unconditional `uq_intent_slot` is dropped and the partial index takes
     its name; `fn_prompts_due` is redefined (skips cancel-flagged rows, serves planned rows only
     while servable and within the window, returns the origin and the scheduler's name); the
     reaper's expiry leg skips planned rows; a new door lists planned misses.
   - **Phases 4 to 7:** no DDL. Phase 7 writes the existing `link_url` column.

## Implementation Plan

### Dependencies
- The owner's approval of the DDL list above before phases 1–3 are built, and F7–F11 locked
  before the phases that consume them (see the table under Complexity and Sequencing).
- #1329 and #1235 are built as phase 1 of this plan (1a, then 1b).

### Blocks
- #181 (calendar view) can create and move planned items on this foundation.
- #485 and #295 (recurrence) can later mint planned rows from a rule; `origin` leaves room.

### Steps
1. [Phase 1 — 1a: lease recovery and the reaper's cadence (#1329); 1b: cancel-flagged waiting intents end as `cancelled` (#1235)](01_cancel-waiting-intents.md) — M (1a S, 1b M)
2. [Phase 2 — the ledger learns `planned`](02_ledger-shape.md) — M
3. [Phase 3 — serve on time, and never miss in silence](03_serve-and-misses.md) — L
4. [Phase 4 — outside the daily cap](04_daily-cap-exemption.md) — M
5. [Phase 5 — schedule and reschedule through the port, and the CLI](05_schedule-verbs.md) — L
6. [Phase 6 — the web: schedule, mark, manage](06_web-and-manage.md) — M
7. [Phase 7 — the link to add](07_link-to-add.md) — M

## Test Plan

- Each phase carries its own tests, listed in its doc. Every guard test must fail when its guard
  is reverted.
- End to end, once phase 6 has landed: on a Neon branch, with `dry_run_mode` on for the
  workspace so nothing reaches Instagram, schedule an item from the CLI and another from the web,
  watch both served at their times, approve one with a Telegram tap, and see it publish in dry
  run. Confirm the same day's cadence intents match a control day. Then trigger each miss reason
  and read the notices in the bound chat.
- Before merging each phase: the full suite, lint through `scripts/lint.sh`, and the migration
  replay gate for the phases that add DDL.

## Verification Checklist

- [ ] `schedule_item` from the web and from the CLI creates a `post_intents` row with
      `origin = 'planned'`, `scheduled_by_user_id` set and `approval_mode = 'manual'`.
- [ ] At its time the row is `awaiting_approval`, and every active push binding has a card
      carrying "Scheduled by <name>"; the Queue row is marked the same way.
- [ ] No planned row reaches `approved` except under `app.actor_kind = 'user'` with a user id, and
      the trigger test goes red when the trigger is reverted.
- [ ] On a rehearsal day with a planned item, the account's cadence intents (their count and
      each `schedule_slot_at`) equal those of a control day without it, and the planned post
      leaves `daily_post_counts` unchanged.
- [ ] `reschedule_item` and `cancel` work before the serve; a cancelled planned row ends
      `cancelled` and is never served.
- [ ] Each miss reason (item removed, item locked, account removed, paused past the window, late)
      leaves an `expired` row with `last_error.class = 'planned_missed'` and a notice in the bound
      chats. No planned row ends `expired` without one.
- [ ] `audit_events` has a row for create, edit and cancel (written explicitly) and for the serve
      (the `scheduled → prompt_pending` transition).
- [ ] An item with a link shows it on its card, and Post now is absent (or labeled, per F10).

## What NOT To Do

- Don't drop the unconditional `uq_intent_slot` in the same deploy that changes `plan_slot`'s
  `ON CONFLICT` spelling (G6). A worker still on the old spelling would raise on every slot, and
  cadence would stop everywhere. Expand in phase 2; contract in phase 3, once phase 2 is
  deployed and drained on every worker (`drainingSeconds = 60`).
- Don't redefine `fn_reaper_sweep` from an older body. Every leg must survive each redefinition:
  job leases, cadence expiry, the approval TTL, expired locks, invitations, onboarding sessions
  (`076:24-58`) and #1235's cancel leg. The phase tests pin each one.
- Don't pre-check in Python what the doors decide (live-subject conflicts, whether a row can be
  served). Map the database's refusal instead.
- Don't copy the workspace's `approval_mode` onto planned rows the way `plan_slot` does; planned
  rows are `manual` by CHECK.
- Don't clamp planned times into posting hours. Posting hours shape cadence only.
- Don't render `link_url` into an `href` without an https allowlist.
- Don't add recurrence, calendar drag-to-move or product tagging here (#485 and #295, #181, and
  #739 with FC-4).

## Companion Plans

- `documentation/planning/2026-08-02-consolidated-design-plan/` — `02` (the intent state machine,
  its keys and the reaper) and `06` §3 (selection). The phases that add DDL update its advertised
  DDL in step, as every migration since `055` has.
- #1329 (the lease wedge and the cadence) is built here as phase 1a, and #1235 as phase 1b.
  #181, #485, #295, #739 and #418 are related and out of scope.
- `shared/planning/active/` holds no plan for this area (checked 2026-09-27).

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| The partial-index change ships in one deploy with the `ON CONFLICT` change (G6) | High: cadence stops on every workspace | expand/contract across phases 2 and 3, the contract waiting until phase 2 is deployed and drained on every worker (`drainingSeconds = 60`); phase 2 tests the old spelling against the new schema |
| A redefinition of `fn_reaper_sweep` drops a leg | High: silent expiry or cancel stops working | build each body from the current live definition; phase 3 depends on phase 1, so the two redefinitions land in order; a test for each leg |
| The person-only trigger blocks a real approve path | Medium: approvals fail for planned rows | phase 2 drives every adapter's approve through the trigger before merge |
| #1329 or #1235 slips | Medium: phases 3 and 5 wait | phase 1 comes first, and both are live defects in their own right |
| The job system's crash recovery (#1329). The reaper's first leg is the only lease recovery for every job kind, and one worker death while holding `reap_expired`'s own lease ends it for good. Phase 1 raises the reaper from 4 runs a day to 1,440. | High: every kind's dead lease then blocks its key indefinitely (deliveries, publishing, slot planning, credential refresh), and nothing alerts, because the clock keeps ticking | phase 1a lets the clock revive its own singletons, and it lands before anything raises the cadence; reproduced on Postgres (phase 1, Evidence) |
| The first runs after 1a deploys carry the backlog built up under the 6-hour cadence | Medium: the first sweeps meet the 500-row budget (expired leases, cards past their TTL, locks, invitations, onboarding sessions), and up to 500 card supersessions queue at once for the outbox poller | before deploying, count each leg's backlog on a Neon branch of production: at least one 6-hour window's worth, plus any standing backlog the 200 budget never cleared. Deploy in a quiet window, and watch the reaper's counts and the outbox depth until a sweep comes in under budget |
| Owner decisions are pending when a phase is ready | Medium: an idle phase | one consolidated ask (the list above); the phases needing none go first |
| Meta's quota defers a planned post after approval | Medium, external: the drop posts late | the card already states the wait; the docs say so |
| A workspace with no bound chat cannot be served (an existing limitation until X.2) | Low: a planned item always misses there | `schedule_item` warns at creation; the Queue shows the miss |
| Time-zone and DST errors | Medium: the wrong hour | conversion in SQL from the account's zone; a local time inside a DST gap is refused; tests on a DST boundary |
| Scope creep into recurrence or a calendar editor | Medium | out of scope, stated; `origin` leaves room |

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|---|---|---|---|
| 1a — #1329 lease recovery and cadence | S | owner DDL approval | 2, 4 |
| 1b — #1235 cancel leg | M | 1a | 2, 4 |
| 2 — ledger shape | M | owner DDL approval; F1, F2 and F6 locked | 1 |
| 3 — serve and misses | L | 1b merged (both redefine `fn_reaper_sweep`); 2 deployed and drained on every worker; F5, F7 and F9 locked | 4 |
| 4 — daily-cap exemption | M | 2; F8 locked | 1, 3 |
| 5 — verbs and CLI | L | 1, 3 and 4; F3, F4 and F11 locked | — |
| 6 — web and manage | M | 5 | 7 |
| 7 — link to add | M | 3 and 5; F10 locked | 6 |

Critical path: owner approval, then 1a and 1b in order (alongside phase 2), then 3, 5 and 6
(S + M + L + L + M). Profile: 1 S, 5 M, 2 L. No
planned row can exist before phase 5, so phases 1–4 change nothing a customer sees.

## Context
- Source skill: forge · Area: `src/services/target/` (scheduler, prompts, publish_cap, command_executors), `scripts/migrations/`, `storydump_cli/`, `landing/` · Effort: XL overall · Risk: Medium · Priority: High (handed to the team by the owner, 2026-09-27)
