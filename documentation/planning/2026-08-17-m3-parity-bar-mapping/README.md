# The M.3 parity bar — the legacy→target command mapping

**Status:** filed, not ratified. The mapping below is derived; **two forks are open and marked
blocked pending ruling** (§7). Nothing here decides them.

**Why it exists at all.** The bar's operative content is a mapping from the target's command
vocabulary onto what the current Telegram adapter actually serves. That mapping did not exist in
writing, and the first version of it was written into a comment on
[#790](https://github.com/chrisrogers37/storydump/issues/790#issuecomment-5318366133). That is
the shape [#806](https://github.com/chrisrogers37/storydump/issues/806) already caught once: the
F.2 increment split lived in a #746 comment, F.2 read as unowned for weeks, and a scan of every
open issue came back with zero owners. Re-filed here rather than into another comment, following
the precedent of `2026-08-14-f2-increment-split/`.

Mapping content is **derived by enumerating the legacy dispatch surface** at `origin/main`
(`c28066d`) — the command map, the callback table, the special-case router and the card keyboard
builder — not read off the plan prose, and not obtained by searching legacy for the target's
words. §3 explains why that distinction is load-bearing, and §5 is the finding it produced that
no word search would have reached.

---

## 1. The bar, and a discrepancy between the two documents that state it

The bar has two homes and **they do not say the same thing.** Recorded as its own line because it
is a live inconsistency between two plan documents, and it decides rows of the mapping.

| Source | Wording |
|---|---|
| `04-execution-sequence.md:262` | "before the window opens, **the target must serve the command vocabulary in production use** — approve · skip · reject · mark_posted · cancel · sync_now · settings_change · pause/resume — plus prompts, notifications, media sync, scheduling, manual mode, and the API publish path" |
| `00-fixed-constraints.md:91` (FC-7 clause 3) | "the **Telegram adapter** must be at parity for the commands in production use before the window opens (`04` M.3 parity bar)" |

`04` states the bar as a property of *the target*. FC-7 — which `04` cites as its authority —
states it as a property of *the Telegram adapter*. The narrower reading is the constraint's own,
and `04` is quoting its own constraint more broadly than the constraint reads.

**What turns on it:** `pause`/`resume` and `sync_now` are served by the **dashboard** today, and
were deliberately retired *from the adapter* (§5). Under FC-7's wording they are not served by
the surface the bar governs and are in scope to build. Under `04`'s broader wording one could
argue the product already serves them and nothing is owed. The two readings give opposite
verdicts on the same rows.

**This document uses FC-7's wording** — the constraint is the authority its own citer points at
— and flags the discrepancy rather than silently resolving it. Whoever ratifies should align the
two documents so this is not discovered a third time.

**Separately:** `01-target-architecture.md:49` is the normative home of the **closed** command
vocabulary (~25 commands). The bar names a subset. It matters that `01` lists `approve` **and**
`autopost_now` as distinct commands — see Fork 1.

## 2. What "the target" is today

`src/models/target/` is the **only** target-side directory in the source tree: `__init__.py` and
`base.py`. No target services, repositories, adapters or command handlers exist anywhere.

`TargetBase` has **zero model subclasses**. Its only references are its definition
(`src/models/target/base.py:10`), its re-export, and `tests/scripts/test_lineage_lane.py:656`,
which asserts `list(TargetBase.metadata.tables) == []`. The module docstring's claim — *"It is
empty right now, and that is a real state rather than a stub"* — is accurate and test-pinned.

So **"does the target serve X" is `no` for all fourteen, uniformly, by construction.** That is a
fact about the question, not a measurement of progress: recording `0/14` and stopping would be
true and useless. The bar's operative content is what the target must be *built* to serve, which
is §4.

## 3. Method — enumerate legacy's container, do not search it for the target's words

Legacy does not use the target's vocabulary. Searching legacy for `approve`, `mark_posted` or
`pause` produces false negatives, because those capabilities exist there as `autopost`, `posted`
and an `is_paused` column.

So the mapping was built by enumerating legacy's **own** dispatch containers and reading the
target's vocabulary onto them:

- `src/services/core/telegram_service.py:250-278` — the slash-command map, including its
  **retired-commands block** (§5)
- `src/services/core/telegram_service.py:337-366` — the callback action → handler table
- `src/services/core/telegram_service.py:368+` — `_handle_callback_special_cases`
- `src/services/core/telegram_utils.py:265-340` — the per-item card keyboard builder, the
  authoritative statement of what buttons a queue item actually offers

This is the inverse of the failure in
[#810](https://github.com/chrisrogers37/storydump/issues/810), where a Facebook-Page requirement
was invisible to word search because it survived encoded in an identifier (`pages_show_list`)
rather than in prose. Same hazard, opposite direction: there, searching for the word missed the
requirement; here, searching for the word would have missed the implementation — and would have
missed §5 entirely.

## 4. The mapping

Fourteen named items (eight commands, counting `pause`/`resume` as one; six capabilities).
**Target status is `absent` for every row** (§2), so it is not repeated per row. "Legacy
referent" is what parity would be measured *against*.

### A. Clean port — referent exists and is unconditionally reachable in the adapter (6)

| # | Bar item | Legacy referent |
|---|---|---|
| A1 | `skip` | `skip:{queue_id}` card button → `handle_skipped` |
| A2 | `reject` | `reject:{queue_id}` → `handle_reject_confirmation`, then `confirm_reject` / `cancel_reject` |
| A3 | `mark_posted` | `posted:{queue_id}` → `handle_posted`. Naming confirmed by `06-product-lifecycles.md:47` ("The Posted tap (`mark_posted` command)") |
| A4 | `settings_change` | `settings_toggle`, `settings_edit`, `settings_edit_cancel`, `settings_close` |
| A5 | manual mode | The Posted / Skip / Reject triple, appended unconditionally at `telegram_utils.py:298-309` |
| A6 | scheduling | `schedule_action` / `schedule_confirm` callbacks (reached via `/settings`) + `src/services/core/loops/scheduler_loop.py`. **Note:** the `/schedule` *slash command* is retired (§5); the capability survives through settings, so this is a port, not a build. |

### B. Present but not unconditionally reachable (2)

The category that reads as a pass and is not. Both rows exist in code; neither is reachable for
an arbitrary workspace without a condition being met first.

| # | Bar item | Legacy referent | The condition |
|---|---|---|---|
| B1 | API publish path | `autopost:{queue_id}` → `telegram_autopost.py` → `instagram_api.py` | The button is emitted **only** `if enable_instagram_api` (`telegram_utils.py:276-286`). That column is `Column(Boolean, default=False)` (`src/models/chat_settings.py:40`) — **per-workspace, default off** — set true only during onboarding (`src/api/routes/onboarding/setup.py:235`). Independently gated again at `src/services/integrations/instagram_credentials.py:125`. |
| B2 | `approve` | Nearest referent is the same `autopost` button, carrying the same gate | Same as B1 — **and see Fork 1**, which may move this row to category C entirely. |

**Consequence for any parity claim: it must name the workspace.** Both rows hang off a
per-workspace database row, not deployment config, so "the bar is met" is not a single-valued
statement. A workspace with the flag off and one with it on give different verdicts on the same
build.

### C. No working referent in the adapter — builds, not ports (3)

| # | Bar item | What the enumeration found |
|---|---|---|
| C1 | `cancel` | No per-intent cancel. The four `cancel`-shaped paths — `batch_approve_cancel`, `settings_edit_cancel`, `cancel_reject`, and the `action == "cancel"` branch of `handle_reset_callback` (`telegram_callbacks_admin.py:323`, registered as the `clear` callback, "Legacy name for reset" — replies *"Cancelled — Queue was not cleared"*) — are all **dialog dismissals**, not cancellation of a queued intent. Never present, not retired. |
| C2 | `sync_now` | **Retired, not absent** (§5). `/sync` is registered and answers *"has been retired… Sync from the dashboard."* The underlying capability runs as a background loop (`loops/media_sync_loop.py`, `core/media_sync.py:179`); the user-facing command was deliberately removed. |
| C3 | `pause` / `resume` | **Retired, not absent** (§5). `/pause` and `/resume` are registered and answer *"has been retired… Use Quick Controls in the dashboard."* State lives on `chat_settings.is_paused` (`src/models/chat_settings.py:41-43`), toggled from the dashboard (`src/api/routes/onboarding/settings.py:41-47`). |

**This is the finding that changes what M.3 costs.** "Parity" reads as *carry across what
exists*. These three have nothing working in the adapter to carry — and two of them are not gaps
at all but **shipped decisions** (§5). A schedule sized as porting will be wrong for them, and
for C2/C3 the work is not "build the missing thing" but "reverse a deliberate removal", which is
a product ruling rather than an implementation task.

### D. Present, depth not audited (2) — and one unscoreable (1)

| # | Bar item | Status |
|---|---|---|
| D1 | notifications | Present. `01:50` defines the target shape as an outbound interaction-request landing in `channel_outbox`; legacy notification code spans `telegram_lifecycle.py`, `settings_service.py`, `telegram_accounts.py`, `telegram_settings.py` and both OAuth services. Counted present; **which** notification classes exist, and whether the set is complete, is unaudited. |
| D2 | media sync | Present as `loops/media_sync_loop.py` + `core/media_sync.py`. Counted present; depth unaudited. Distinct from C2: the **loop** is the capability, `sync_now` is the **command**. |
| D3 | **prompts** | **Unscoreable.** The bar names it; `01-target-architecture.md` never defines it — `grep -n "prompt"` over that file returns nothing. Candidate readings — AI caption prompts (`core/caption_service.py`), conversational prompts (`core/telegram_utils.py`), onboarding prompts — are different capabilities with different owners. Deliberately left unscored rather than resolved by choosing one. |

### Tally

**6 clean ports · 2 present-but-conditional · 3 builds-not-ports · 2 present-depth-unaudited ·
1 unscoreable = 14 rows for 14 items.** Row B2 (`approve`) may move to category C under Fork 1.

## 5. The retired-commands block — two bar items are shipped decisions, not gaps

`src/services/core/telegram_service.py:265-277` carries a block commented **"Retired commands
(show helpful redirect)"**. Twelve commands are registered as real `CommandHandler`s whose only
behaviour is to answer that they no longer exist:

`queue` · **`pause`** · **`resume`** · `history` · **`sync`** · `schedule` · `stats` · `locks` ·
`reset` · `dryrun` · `backfill` · `connect`

The redirect text (`src/services/core/telegram_commands.py:565-577`) states where each went:

- `/pause` → *"Use Quick Controls in the dashboard. Use /start to open it."*
- `/resume` → *"Use Quick Controls in the dashboard. Use /start to open it."*
- `/sync` → *"Sync from the dashboard. Use /start to open it."*
- `/schedule` → *"Use /settings to adjust posting cadence, or the dashboard for full controls."*

**Why this matters more than the row count.** Three bar items name commands this product
deliberately removed from Telegram and moved to the dashboard. The bar, read at face value under
FC-7's adapter scoping, requires the target's Telegram adapter to serve them again. That is not
closing a gap; it is **reversing a shipped product decision**. Whether that is intended is Fork 2
— and it is an owner's ruling, not an implementer's.

It also explains the §1 discrepancy rather than merely restating it: under `04`'s product-level
wording these capabilities *are* served (by the dashboard) and nothing is owed; under FC-7's
adapter-level wording they are not. The two documents disagree precisely where a capability was
deliberately moved between surfaces.

### The false friend — `resume`

Called out by name so it cannot be re-scored by a later reader. **The token `resume` appears
twice in legacy and neither occurrence is workspace resume:**

| Occurrence | What it actually is |
|---|---|
| `telegram_service.py:268` — `"resume": self.commands.handle_removed_command` | The **retired** `/resume` slash command. Answers with a redirect to the dashboard. |
| `telegram_service.py:346` — `"resume": self.callbacks.handle_resume_callback` | A **callback** prefix for *reschedule / clear / force* recovery actions (`src/services/core/telegram_callbacks_admin.py:177-189`). Unrelated to pausing a workspace. |

A name-based audit of this bar scores `pause`/`resume` **PRESENT** on the strength of one of
these strings — the second one especially, since it sits in the live callback table next to rows
that *are* genuine referents. The row's real state is C3. The word matches twice; the capability
matches neither time.

## 6. What was measured, in one line

Every row above is a claim about the adapter's **surface**: whether a handler exists, and what
gates it. No row was exercised. See §8.

## 7. Open forks — blocked pending ruling

Neither is resolved here. Both are recorded with what changes under each shape, so the ruling can
be made without re-deriving the mapping.

### Fork 1 — is bar-`approve` the same command as `autopost_now`?

**Status:** open, blocked pending ruling. **Blocks:** whether row B2 is a port or a build, and
whether the bar is satisfiable by porting at all.

`01-target-architecture.md:49` lists `approve` **and** `autopost_now` as separate members of the
closed vocabulary. The bar (`04:262`) names `approve` and does not name `autopost_now`. Legacy
has exactly one tap that publishes: `autopost`.

- **Shape (a) — bar-`approve` *is* legacy's Auto Post tap.** B2 stays in category B: a port,
  subject to the `enable_instagram_api` gate. The bar then contains three builds (C1–C3).
- **Shape (b) — bar-`approve` is distinct**, meaning approve-the-intent-and-let-the-scheduler-
  publish, with `autopost_now` as publish-immediately. Legacy then has **no referent** for
  `approve`; B2 moves to category C and the bar contains four builds. The target's approval
  semantics would also differ from legacy's tap-to-publish — product-visible, not a refactor.

### Fork 2 — do C2 and C3 reverse a shipped decision, or fall out of scope?

**Status:** open, blocked pending ruling. **Blocks:** the size of the M.3 precondition, and
therefore when the window can be scheduled. **This is an owner ruling** — §5 shows the commands
were retired on purpose.

- **Shape (a) — all named items are in scope, as written.** The precondition includes
  **re-introducing** `/pause`, `/resume` and `/sync` to the Telegram adapter after they were
  deliberately retired and redirected to the dashboard, plus building `cancel`, which never
  existed. The bar is not a port exercise and must not be sized as one.
- **Shape (b) — "in production use" excludes them.** Both sources say the commands *in
  production use*; a command that answers "has been retired" is not in production use, and a
  capability the dashboard owns is not the adapter's to serve. C1–C3 fall out of the
  precondition; the bar reduces to A1–A6 plus B1–B2 plus D1–D2. This reading has direct textual
  support and is materially cheaper.

Shape (b) is textually available, materially cheaper, and consistent with the product's own
retirement decision — which is exactly why it should be ruled on explicitly rather than assumed
by whoever moves first. Under shape (a) the bar's cost is dominated by these three rows, not by
the ports.

## 8. Bounds

- **Derived at `origin/main` `c28066d`** by enumerating the dispatch containers listed in §3.
  Re-derive rather than quote: any handler added to the command map or callback table moves these
  rows.
- **No row's production reachability was verified.** The gates that decide B1/B2 and the state
  behind C3 (`enable_instagram_api`, `is_paused`) are **per-workspace database rows**, not
  deployment config, so there is no single production answer, and this was written without
  database access. That matters because the M.3 gate's own wording — *"owner confirms Telegram
  works in his chats"* — is exactly the per-workspace reading left unmeasured here.
- **No row was exercised.** Every claim is about the surface: a handler is registered, a button
  is emitted under a condition. Nothing was run, and no row was tested for whether it behaves
  correctly.
- **Scoped to the Telegram adapter**, per FC-7 (§1). The CLI surface (`cli/commands/`) and the
  Mini App's own command set were **not** audited. The Mini App may belong in scope: `01:50`
  states a Telegram tap and a Mini-App click "converge on the same command". Flagged rather than
  assumed either way.
- **D1 and D2 are counted present on existence, not completeness.** Neither was audited for
  whether its set of behaviours matches what the target requires.
- **The retirement dates are not established.** §5 records that the twelve commands are retired
  *now*; when each was retired, and under what decision, was not traced. If Fork 2 is ruled
  toward shape (a), that history is worth recovering first — it is the record of why they were
  moved.
- **Not derived here:** anything about M.1, M.2, the transform spec, or #787. This document
  covers the parity bar only.
