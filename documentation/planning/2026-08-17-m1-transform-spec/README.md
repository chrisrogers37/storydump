# M.1 transform spec — legacy → target, the 3e band

**Status:** draft for review (#790 M.1). **Four inputs are open forks, not settled rulings** (#790 comment 5318301047, mason's menu): sections depending on them are marked **BLOCKED PENDING RULING** with per-option deltas, and this document picks none of them. Everything else below is executable specification derived from ratified text.

**Scope:** the `02` §9 disposition index made executable — per-legacy-table transform statements as runner-migration files, the per-column mapping with the `02` §0 timestamp rule applied per column, machine-checkable postconditions (row-count reconciliation + per-table invariants), and the quarantine feeds. This is `04` Phase M step **3e**: files that read `legacy.<table>` and write the target tables, inside the single advisory-locked runner invocation, after 3d (the F.2 schema files) and before 3f (snapshots).

**What this spec does NOT decide** (§3 is the register): Forks A–C from mason's menu (**D is answered by the shipped schema rather than by a ruling — §3.4/§4.2**); Fork E (surfaced here, same discipline); #793 (`instagram_backfill` disposition — open, unforced at the 2026-08-13 census); the one-PR-vs-two program-shape split proposed in #790 comment 5280282481. **One** fork is conditional on mason's §0 measurement (§3.3; being chased separately): **C**, which collapses at `group_chats = 1` — one live *group* tenant, since 044's derivation keys on a single group chat. Its section is marked **conditional**, not blocked. **D is no longer among them**: it was previously paired with C here as collapsing at `chats = 1` total, on the reading that a DM-rooted row makes minting rule W and a collapse shape disagree. The shipped schema retires rule W outright (§3.4/§4.2), so no count decides D and the pairing of the two measurements is itself retired. Two register entries are additionally **lifted to the plan author** (§3.8) and are likewise not decided here.

**Sources of truth** (all at `origin/main` `c28066d`): `02-domain-model.md` §0/§1/§2/§3/§4/§9 · `04-execution-sequence.md` L76–160 (Phase M) · `03-decision-record.md` D39/D40/D41 · `documentation/planning/2026-08-14-f2-increment-split/README.md` (the 3d band this spec lands after) · `documentation/operations/migration-runner.md` + `scripts/migration_runner.py` (file contract) · `scripts/fc8_gate.py` (#792) · `src/models/*.py` (legacy column ground truth) · #790 + comments 5280282481 / 5286087397 / 5286618365 / 5318301047 · #787 (measured inert for the production world) · #793 (open).

---

## §1. File inventory, band, and the file contract

### 1.1 The band

M.1 files are the **3e band**: numbered immediately after the last F.2.9 file, before the 3f snapshot files. Numbers are **assigned at land time, not here** — `053`+ is claimed by F.2.2–F.2.9 (eight increments, F.2 split doc §2), and pinning absolute numbers now would re-create the collision that doc exists to prevent. This spec names files by slot:

| Slot | File (name at land time) | Writes | Reads (`legacy.*`) | Fork exposure |
|---|---|---|---|---|
| M1-01 | `NNN_m1_users_identities.sql` | `users`, `user_identities` | `users` | — |
| M1-02 | `NNN_m1_workspaces_members_bindings.sql` | `workspaces`, `workspace_members`, `channel_bindings`, `audit_events` (drop records + tz log) | `chat_settings`, `user_chat_memberships` | **A** (rung-4 feed); ~~D~~ **answered by the schema, §3.4** |
| M1-03 | `NNN_m1_media_sources.sql` | `media_sources` | `chat_settings` | — |
| M1-04 | `NNN_m1_ig_accounts.sql` | `ig_accounts` | `instagram_accounts`, `api_tokens`, `chat_settings` | **B** (`next_slot_at`), **C** (conditional: NULL-chat tokens) |
| M1-05 | `NNN_m1_media_items.sql` | `media_items` | `media_items`, `chat_settings` | #793 (fail-closed assert) |
| M1-06 | `NNN_m1_post_locks.sql` | `post_locks` | `media_posting_locks` | **E** (live `recent` locks) |
| M1-07 | `NNN_m1_category_mix.sql` | `category_post_case_mix` | `category_post_case_mix` | **C** (conditional) |
| M1-08 | `NNN_m1_post_intents.sql` | `post_intents`, `audit_events` (companion detail rows) | `posting_history`, `service_runs`, `posting_queue` (exclusion check only) | **A** (attribution feed), **C** (conditional) |
| M1-09 | `NNN_m1_audit_merge.sql` | `audit_events` | `audit_log`, `user_interactions`, `chat_settings` | **C** (conditional + the `user_interactions` addendum, §3.4) |
| M1-10 | `NNN_m1_onboarding.sql` | nothing (drop-assert) *or* `onboarding_sessions` (re-key alternate) | `onboarding_sessions`, `chat_settings` | proposed-drop batch nod (`04` L88) |
| M1-11 | `NNN_m1_cross_assertions.sql` | nothing | several | — |

Dependency order is FK-forced and matches the slot order: workspaces need users (`paused_by_user_id`); everything tenant-scoped needs workspaces; `media_items` needs `media_sources`; `post_locks` needs `media_items` + `ig_accounts`; `post_intents` needs `ig_accounts` + `media_items`.

**Why M1-02 is one file, not three:** `ct_workspaces_owner_at_insert` (`02` L201) is DEFERRABLE INITIALLY DEFERRED and fires **at commit** of the transaction that inserted the workspace — a workspace INSERT whose owner `workspace_members` row is not in the *same transaction* fails. The runner wraps each file in one transaction (`migration-runner.md` L48–49), so the workspaces insert and the member inserts must share a file. `channel_bindings` rides along because it is small and chat-derived.

### 1.2 The file contract (binding on every 3e file)

1. **Runner form:** `NNN_name.sql`, SQL-only, **no `BEGIN`/`COMMIT`** (the runner wraps; ledger row commits atomically with the DML), header idiom per `052`: `-- Migration:` / `-- Description:` / `-- Rollback:`.
2. **First statement:** `SET LOCAL app.actor_kind = 'migration';` — required twice over: `trg_intent_insert_guard` (`02` L885–897) refuses terminal-state intent inserts from any other actor, and `trg_governance_audit` (`02` L913) raises on governance-table writes without an actor. `SET LOCAL` is legal because the runner's per-file transaction encloses it. `app.actor_user_id` and `app.channel` stay unset (NULL).
3. **Schema-qualified legacy reads** — `legacy.<table>`, never bare (`04` L149, part of the M.1 file contract). Writes go to bare (new `public`) names.
4. **≥ 1 `-- runner:postcondition` line per file** — each a single SELECT returning exactly one boolean `true` (runner contract; anything else fails the migration). Every file's lines are enumerated in §6. This also makes each file self-adopting (`adopt` derives probe evidence from postcondition lines), and it is the mechanical half of gate item 1.
5. **No `-- runner:no-transaction`, no `-- runner:reapply-safe`, no `-- runner:schema-move`** in this band. Transforms must be transactional; re-entry after a mid-band failure is the D41 rollback lever (legs 1–4, then retry), never per-file reapply.
6. **Rollback header:** every file's `-- Rollback:` line reads `via the 04 in-window lever (D41), never per-file` — 3e writes only into the new `public`, which leg 1 discards wholesale.
7. **Timestamp discipline (G-TIME, §2.1):** a bare cast of a naive legacy timestamp is a review-blocking defect (`02` §0 L8).
8. **Fork gate:** no 3e file lands before Fork A is ruled — see the trap note in §5.3. A quarantine postcondition passing on empty CI fixtures is **not** evidence the procedure exists.

### 1.3 Not in this band

**3f snapshots** (`archive.<t>_pre_cutover_<date>`, every legacy table incl. `api_tokens`, `posting_queue`, `service_runs`, `schema_version`) and **3g** (`DROP SCHEMA legacy CASCADE`, gated on 3e postconditions + parity) are M.3-filing artifacts (`04` L150–151), not transform spec. The FC-8 gate (`scripts/fc8_gate.py`, #792) is **window prep**, not a 3e file — §7 gives it its documented home. The **pre-window profile queries** (§4 per table, collected in §5.1) run against live legacy before the window; they are read-only SELECTs and belong to window prep regardless of Fork A's outcome.

---

## §2. Global rules

Applied by every mapping table in §4. Each rule cites its ratified source; the two marked **[SD-n]** are spec-introduced decisions registered in §3.7 for explicit review.

### 2.1 G-TIME — the §0 timestamp rule, per column

- Every naive legacy `TIMESTAMP` converts with **`AT TIME ZONE 'UTC'`** — no exceptions among the naive set (`02` §0 L8, §9 L2088).
- Exactly three legacy columns are already `TIMESTAMPTZ` and copy **as-is** (conversion would corrupt them): `chat_settings.last_post_sent_at`, `chat_settings.gdrive_alerted_at`, `api_tokens.revoked_at`. The first is Fork B's input; the second maps to `media_sources.alerted_at`; the third never migrates (FC-7.2) and is listed only so the exception set is complete.
- Verified against `src/models/*.py` at `c28066d`: those three are the **only** `DateTime(timezone=True)` columns in the legacy schema. Every §4 mapping row states which rule applies.

### 2.2 G-TZ — timezone *values* (ruled, `04` L84)

`legacy.chat_settings.posting_timezone`: `NULL` → `'UTC'` (legacy NULL meant UTC; target `tz` is `NOT NULL DEFAULT 'UTC'`). Non-NULL `v`: copy verbatim iff `fn_safe_tz(v) = v`, else `'UTC'` **plus a transform-log row naming the workspace and the discarded value** (G-LOG). `ck_ws_tz_valid` / `ck_iga_tz_valid` are the mechanical backstop — a bad copy fails the file loudly.

### 2.3 G-IDS — id preservation **[SD-5]**

Rows that map 1:1 **keep their legacy UUID as the target id**: `users`, `workspaces` (:= `chat_settings.id`), `media_items`, `post_locks`, `category_post_case_mix`, `post_intents` (:= `posting_history.id`), `onboarding_sessions` (alternate branch). New ids: `ig_accounts` (fan-out mints per-pair rows), `audit_events` (BIGINT identity; legacy UUIDs ride in `detail`), `workspace_members` (composite PK, no id column). Why: every §6 reconciliation query becomes an id-join instead of a heuristic match; `audit_log.entity_id` referents stay live for `setting` (→ workspace, same UUID) and `lock` rows; `posting_history.media_item_id` needs no translation table. Nothing in the plan rules against it; nothing requires it — hence registered.

### 2.4 G-STAMPS — `created_at` / `updated_at` on transformed rows **[SD-6]**

`created_at` := converted legacy `created_at` (or the semantically-true birth column where one exists and is named in §4 — e.g. `workspace_members.created_at` := `joined_at`); fallback `now()` where legacy is NULL (legacy stamps are nullable with Python-side defaults, so NULLs are possible from raw inserts). `updated_at` := converted legacy `updated_at` where the legacy table has one, else := the row's `created_at`. History is preserved; `now()` appears only as a disclosed fallback.

### 2.5 G-LOG — the transform log

A transform-log row is an `audit_events` insert: `actor_kind='migration'`, `entity_kind` = the target table's governance kind (or the table name where no governance kind exists), `detail` = `{"v":1, "transform_log":"<rule-id>", ...facts...}`, `workspace_id` = the affected workspace, `created_at` default. This is not new mechanism: `04` L84 mandates a "transform log row", `02` §9 routes drop/audit facts into `audit_events`, and `ck_audit_actor` already carries `'migration'`. Every G-LOG rule-id is named at its §4 site.

### 2.6 G-ACCOUNTING — audit rows have three provenances

The §6 audit arithmetic distinguishes: (a) **trigger-minted** — `trg_governance_audit` fires on every governance INSERT this band performs (`workspaces`, `workspace_members`, `ig_accounts`, `channel_bindings`; zero `oauth_credentials`), writing `detail = {"v":1,"op":"INSERT"}` rows automatically; (b) **transform-minted** — G-LOG rows and the M1-08/M1-09 explicit inserts, all carrying a `detail->>'transform_log'` or `detail->>'migrated_from'` marker; (c) none other — runtime is stopped (FC-7.1). Every reconciliation query filters by these markers, so the counts close exactly.

### 2.7 G-PROFILE — profile-then-assert

Every "this should be impossible/zero" edge in §4 gets two artifacts: a **pre-window profile query** (read-only, against live legacy, run at window prep; §5.1 collects them) and an **in-file assertion** (a postcondition or a WHERE-exclusion that feeds quarantine). A zero measured at prep can drift by 3e (legacy is live until step 1), so the in-file arm re-checks; a nonzero at prep routes to the owner *before* the window opens instead of detonating inside it. This is the FC-8 shape (#792) generalized.

---

## §3. Fork register — what is open, what each option changes here

**This spec resolves none of these.** Forks A–D are mason's (#790 comment 5318301047) — consumed, not rebuilt; per-fork detail lives there. E is surfaced by this spec under the same discipline. §3.5 is the separate register of *spec-tier* decisions (reviewable proposals, not forks).

### 3.1 Fork A — quarantine home / shape / **actor** (blocks gate item 3)

The ruled feeds (owner-derivation rung 4, attribution gaps — §5.2) are specified below with executable detection queries. What cannot be written: the **adjudication procedure**, because no printed actor can perform it — the stream runs as `svc_migration`, whose only legacy privilege is `GRANT SELECT` (`04` L120, D40), and in-window manual adjudication needs a legacy write path that neither step 0 nor step 8 provides.

| Option (mason's) | What changes in THIS spec |
|---|---|
| **A1** — pre-window adjudication | §5.1's profile queries are promoted to the normative detection instrument (a `fc8_gate.py`-style script, window prep); every quarantine feed's in-file arm becomes a **hard zero postcondition** (fires never; firing = re-run prep); §5.3's procedure section becomes a window-prep runbook step; no schema, no new actor |
| **A2** — quarantine table + resolution file | This spec gains a quarantine table DDL (schema home to be ruled with it — it cannot be in the byte-parity F.2 stream without changing `02`), M1-08/M1-02 write unresolvable rows there and complete green, a **resolution file slot** lands after the adjudication, and the adjudicator needs a write grant — which splits `04` L144's "one runner invocation" into two and amends step-0/step-8 |
| **A3** — halt and retry | No spec change at all — which is exactly the trap: the postconditions in §6 *already* halt the runner on a nonzero quarantine count. A3 is what happens **by default if nobody rules**, it needs the same undeclared write-on-legacy privilege as #787's 3b hole to repair anything, and each occurrence costs a D41 partial rollback |

**Ratifier asymmetry (carried from the menu, with its routing target corrected):** A1/A2 are plan-shape → the plan author — the menu routed these to #731, which has since closed as a completed artifact, so plan-author routing is unresolved (§3.8 states the situation). **A3 is a privilege grant on legacy tables — same class as #787 — whose ratifier is the owner.** A default is not a ruling: landing 3e files with quarantine postconditions before A is ruled silently selects A3 and quietly enlarges #787. Hence file-contract rule 8.

### 3.2 Fork B — `next_slot_at` seeding (blocks one cell of M1-04)

One legacy value (`chat_settings.last_post_sent_at`, TIMESTAMPTZ, copies as-is) fans out to N `ig_accounts` rows. The three shapes and their resume behaviour are in the menu (all-N burst / formerly-active-only with the others invisible to `ix_ig_accounts_due` / NULL + recompute whose base is unstated). §4.4 marks the cell **BLOCKED PENDING RULING**; every other M1-04 column is executable now. Ratifier: plan author, escalating to owner if the shape changes resume-time posting behaviour.

### 3.3 Fork C — NULL-tenant rows (conditional) — and one structural narrowing

Ruled for `media_items` only (the 044 sole-tenant rule, cited in `02` §9). Open for `category_post_case_mix`, `posting_history`, `audit_log` — their §4 workspace-derivation cells are marked **BLOCKED (C1/C2/C3) — conditional**. C3 couples into Fork A (quarantined NULL-tenant rows join its list).

**Narrowing this spec contributes (verified in the #827 review — three open C tables, not four):** for `media_posting_locks` the fork is structurally closed — `fk_locks_media (workspace_id, media_item_id)` is composite, so a lock's `workspace_id` **must** equal its media item's workspace, which is determined (044 covers media). The lock's own `chat_settings_id` is demoted to a cross-check; a mismatch is a profile/quarantine class, not a mapping input. C's residue for locks is only the discrepancy class. The same composite-FK argument fixes `post_intents.media_item_id` consistency but **not** `post_intents.workspace_id` itself for NULL-chat history rows — C stands there.

**Ratifier: the product owner.** Migration 044's own header sets that bar for this class of write ("DO NOT APPLY WITHOUT RATIFICATION… apply only on explicit sign-off, never via CI or on merge"). Named because C is the fork most likely to be resolved by a measurement and then quietly actioned by whoever ran the query — the measurement *sizes* it; only the owner ratifies it.

**Conditional collapse:** if the measurement below returns **`group_chats = 1`** (one live group tenant — migration 044's standing assertion; the total `chats` count is **no longer a fork's number** — D is answered by the schema (§3.4); it is printed because it bounds the DM-binding derivation's ambiguity, §3.4, not this one), C1/C2/C3 coincide for every affected row and C is a one-line ruling. ari is chasing it; this spec does not block on it:

```sql
SELECT count(*) AS chats,
       count(*) FILTER (WHERE telegram_chat_id < 0) AS group_chats
  FROM chat_settings;
```

### 3.4 Fork D — workspace cardinality — **ANSWERED BY THE SHIPPED SCHEMA (2026-08-21), not by a ruling**

**This is no longer a fork.** `channel_bindings` is UNIQUE on `(channel, external_ref)` and
carries **no** uniqueness on `workspace_id`, so **N channels per tenant is the design**, and
`telegram_group`/`telegram_dm` are two **channel kinds rather than two tenants**. Minting rule W
is retired in §4.2 — measured there against a scratch build of 052+053 and against production:
the schema *refuses* W for a chat with no active memberships, and production's DM chat has
exactly that shape. A fork asks which of two admissible readings to pick; only one of these is
admissible, so there is nothing to rule. **The 1,095-row quarantine dissolves with it** — it was
the cost of a mapping the target does not admit.

Per Chris's ruling that parity is not the bar (*"best from first principles in the next state"*),
where the legacy shape and the schema's shape disagree the schema's is the one encoded. They
disagree exactly here: legacy gives each Telegram chat its own settings row, the target gives one
tenant many channels.

The original register entry is kept below because the reasoning it records is still how the
question was framed, and a reader arriving from `02` or `04` will look for it.

**A register-hygiene note, because this will recur.** Decision **D13** — *"A Telegram chat is one
binding of a workspace… 0..n per workspace"* — was ratified **2026-08-10**, eleven days before this
correction, and a comment citing it sits in `053` immediately after the `channel_bindings` table,
untouched by this change. **The question was already settled in prose before the register posed it
as open**, and the D-numbers this document cites as sources of truth (D39/D40/D41) never picked it
up. So the failure was not that nobody knew — it is that a ratified decision did not propagate into
the register that a later reader consults, and the register is the surface that gets trusted. Worth
naming as a process defect rather than a one-off: the same gap will hide the next D13. Not fixed
here; fixing it means auditing the D-number chain, which is a separate piece of work.

---

> **SUPERSEDED — retained as the record of how the question was framed, not as a current
> statement. Every present-tense claim below is false as of 2026-08-21: §4.2 no longer mints by
> rule W, and no count decides D.**

Nothing states how many workspaces the transform mints. §4.2 is written against a **minting rule W** = "one workspace per `chat_settings` row, `workspace_id := chat_settings.id`" — labeled as Fork D's obvious reading, **not** a ruling. At **`chats = 1` total** — the first count of §3.3's query, deliberately not `group_chats` — every candidate shape degenerates to the same transform, so §4.2's mapping stands regardless. The distinction is live, not theoretical: `.claude/rules/database.md` gives each Telegram chat its own settings row, so `chats > 1, group_chats = 1` is an ordinary state — and there a single DM-rooted row makes W mint two workspaces where a collapse shape mints one, so D does **not** collapse even though C does. If `chats` returns > 1, W is D's ruling to make (product owner — merging minted workspaces afterwards is `06` §4 clone-and-retire per account, on live data).

**Restated 2026-08-21 — M1-02's postconditions no longer assume W.** The master identity used to
be counted in workspaces, which is rule W's shape, and it stops balancing under a collapse:
evaluated against the shipped target schema at production's two-chat shape it returns **`False`**.
It is now a **partition over legacy chats** — every chat becomes a binding, a recorded drop, or a
quarantine entry — which holds under either ruling without editing a character. §6's M1-02 block
carries the lines and the reasoning. **This does not decide D**, and under a separate-workspace
ruling the file needs more than a postcondition change: the shipped schema fails that shape for
two independent reasons that are not about counting, both named in §6.

**Addendum this spec surfaces (C-adjacent, for the C ruling to also cover; confirmed as a genuine menu addition in the #827 review):** `user_interactions` has **no tenant column at all** — only a raw, nullable `telegram_chat_id`. M1-09 resolves it via `legacy.chat_settings.telegram_chat_id`; rows with NULL or unresolvable chat ids have no workspace under any C option as written. Same class, one table further out; named here so the C ruling can say whether it covers them (until then they are a §5.2 feed). Also profiled: `chat_settings` rows with `telegram_chat_id > 0` (a DM-rooted tenant would be product-shaped — Fork D's territory, and the reason D's collapse keys on `chats`, not `group_chats`).

### 3.5 Fork E — live `recent` locks have no account (surfaced by this spec; menu, no pick)

**Where it lives:** `02` L598 `ck_locks_recent_scope CHECK ((kind = 'recent') = (ig_account_id IS NOT NULL))` vs `src/models/media_lock.py` — legacy locks carry **no account column**. The ruled kind mapping (`02` L616: `recent_post→recent`) therefore cannot be executed for a `recent_post` row that is still live at 3e: the target row requires an `ig_account_id` the legacy row does not have.

**Sizing:** only **unexpired** rows bite (see SD-14: expired locks are not carried), and `recent_post` TTLs are repost-TTL-scale (days–weeks), so live rows at window time are plausible but few. Profile query in §5.1; if it returns 0 at the window, E is moot at execution and the in-file assert documents that.

**Measured 2026-08-20 (#943), superseding the sizing guess above:** **233 live rows** — E is not moot. The same round empties E1's source: the `service_runs` switch timeline holds **zero** attribution-bearing rows, so E1 is **unavailable on this corpus** (row kept for the record; it becomes available only if a switch-record mechanism starts writing before the window).

| Shape | What it is | Cost |
|---|---|---|
| **E1** | Attribute via the switch timeline at `locked_at` (reuse the ruled ±6h mechanism from the history transform) | Extends a mechanism ruled for *history attribution* to a second consumer — a plan-text extension, however natural |
| **E2** | Quarantine live `recent` rows | Couples into Fork A; adds adjudication volume |
| **E3** | Drop live `recent` rows with a G-LOG record (recency is advisory; the scheduler re-derives; snapshot preserves) | Loses an active anti-repeat hold for up to one TTL — a real, bounded product effect |
| **E4** *(added 2026-08-21 from the #943 review; menu completion, no pick)* | Attribute a live `recent` lock via its media item's **latest attributed posted history row at/before `locked_at`** — a `recent_post` lock exists *because* a post happened; that post is in `posting_history`; M1-08 attributes it; the lock inherits that attribution | Rides the §4.8 history attribution the way E1 rode the switch timeline, **except its source has rows** — so it couples M1-06's cell to whatever resolution §4.8's attribution question lands (its single-account short-circuit gate is falsified on the current corpus, #943). Ordering is legal: nothing FK-forces M1-06 before M1-08 (`post_locks` needs `media_items` + `ig_accounts` only). If attribution quarantines a row's history, E4 quarantines the same lock — never worse than E2 — and it preserves the live anti-repeat holds E3 discards **iff §4.8 attribution yields** (measured-yield note below the table: today it yields nothing, so E4 ties E2 exactly). E3 remains the honest fallback if the plan author declines the extension |

**Measured yield (rajan, #955 review, 2026-08-21): 0 of 226 live locks attribute under E4 today.** `posting_history` carries **no account column for either posting method** (verified against `information_schema`; the provider columns name media, the people columns name humans), so every row's attribution runs through §4.8's switch-timeline reconstruction — which is empty — and E4 ties E2 exactly on the current corpus: all 226 quarantine. The 226-vs-233 gap against #943 is **unreconciled** — measured: 0 locks expired and 0 created between the two readings (rajan, #955) — and it stays open here rather than carrying a guessed cause; the §5.1 profile re-measures the population at prep. **The zero is a property of today's corpus under §4.8's currently-ruled mechanism, not of E4 itself** — E4 has no yield of its own; it forwards §4.8's, by construction. Of §4.8's two named resolution routes, **one survives contact with the data**: the owner's one-decision fallback mapping (2 accounts, 1 posting chat) makes attribution total on this corpus, under which E4 preserves every live hold. The **epoch-gate route does not** — its precondition fails: 4.5 months of history predates both accounts (rajan, #955). Until an owner mapping (or an equivalent §4.8 resolution) lands, the dominance claim holds only as a floor.

**Ratifier:** plan author (mapping-rule shape; no privilege dimension). Flagged to the #790 thread alongside this PR.

### 3.6 #793 — `instagram_backfill` (open issue; fail-closed here)

Not a fork of this spec's making and not decided here. The transform **cannot** give such rows a destination — `ck_sources_provider` admits `'gdrive'` only — so M1-05 admits `source_type = 'google_drive'` rows alone and **asserts zero others** (profile + in-file, G-PROFILE). Census 2026-08-13: zero rows on four independent markers, so the assert is unforced; the corpus is operator-triggered (`backfill-instagram`), so zero is frozen, not guaranteed — which is the standing reason the assert exists in-file and not only at prep. If #793 rules an allow-variant, `ck_sources_provider` gains a value via F.2 (sequenced last per mason's surviving §5(a) advice) and M1-05 gains a mapping row — an amendment to this spec, named here so it is expected.

### 3.7 [SD-n] register — spec-tier decisions (proposals, explicitly reviewable)

These are **not forks**: no privilege/actor gap, no cross-artifact contradiction, each reversible before the window, each with an obvious-but-unratified answer that a transform spec must state to be executable. Review of this PR is their approval mechanism (the same review gate item 2 puts on the reconciliation queries). Silence would be the failure mode; here they are in one place. **Two entries originally filed here failed this test on review** (#827 verdict, Finding 4) — SD-2 is a contradiction between two ratified texts and SD-11 deviates from a ratified text — so both are **lifted to §3.8** and routed to the plan author; their ids stay reserved so §4's references hold.

| id | Site | Proposal | Alternative considered |
|---|---|---|---|
| SD-1 | §4.2 `caption_style` | value outside `('enhanced','simple')` → `NULL` (= app default) + G-LOG `caption_style_discarded` | copy verbatim and let `ck_ws_caption_style` halt the file (A3-shaped; rejected for the same reason the ruled tz rule exists) |
| SD-2 | §4.3 `media_sources.state` | **LIFTED to §3.8-A** — a precedence call between two ratified texts is not register-tier | — |
| SD-3 | §4.8 `approval_mode` | `posting_method = 'auto_reapproval'` → `'auto'`; else → `'manual'` (legacy approval was human via cards; auto-reapproval was the one auto path) | `'manual'` unconditionally |
| SD-4 | §4.8 `ig_media_id` | `COALESCE(instagram_media_id, instagram_story_id)` (a story's id *is* its published-media id); both verbatim in the companion detail row | story id to detail only |
| SD-5 | §2.3 | G-IDS as stated | fresh UUIDs everywhere (kills id-join reconciliation) |
| SD-6 | §2.4 | G-STAMPS as stated | `now()` everywhere (destroys history ordering) |
| SD-7 | §4.5 `media_kind` | `image` iff `mime_type LIKE 'image/%'`, `video` iff `'video/%'`; **assert zero** rows matching neither (profile first) | guess from file extension (unverifiable) |
| SD-8 | §4.9 `entity_kind` | `setting`→`'workspace'`, `membership`→`'member'`, `lock`→`'post_lock'`, interactions→`'interaction'` (open vocabulary, `02` L734) | carry legacy names verbatim |
| SD-9 | §4.1 `user_identities.display_name` | `NULLIF(trim(concat_ws(' ', telegram_first_name, telegram_last_name)), '')`, fallback `telegram_username` | username-first |
| SD-10 | §4.5 `content_hash` | carry `file_hash` **verbatim** (SQL cannot rehash bytes). Named open edge: the `02` L540 comment says SHA256; legacy values are MD5 (`src/utils` file-hash). Dedup (`uq_media_dedup`) only needs consistency, but post-cutover sync computing SHA256 against MD5 rows would re-mint rows — **adapter-side concern, out of M.1 scope, flagged to #790 so it is not silent** | none viable at SQL level |
| SD-11 | §4.4 `ig_accounts.state` | **LIFTED to §3.8-B** — the ruled text answers this the other way; a deviation is a plan amendment, not a register entry. The ruled blanket stands in §4.4 until the plan author rules (routing unresolved — §3.8) | — |
| SD-12 | §4.2 `workspaces.name` | `COALESCE(display_name, 'Workspace ' || left(id::text, 8))` + G-LOG when defaulted (target `NOT NULL`; legacy nullable) | halt on NULL name |
| SD-13 | §4.1 `users.role` | dissolves (ruled) — plus one G-LOG row per legacy system-admin so the fact survives in audit | silent drop |
| SD-14 | §4.6 | rows with `locked_until <= ` transform instant are **not carried** (equivalent to the runtime reap sweep's steady state; 3f snapshot preserves them). Also shrinks Fork E to live rows only | carry expired rows (noise; `ix_locks_expiry` sweeps them immediately) |
| SD-15 | §4.7 | `effective_to` is authoritative; `is_current` not carried (ruled redundant, `02` L627–629); profile counts disagreement rows first | trust `is_current` |
| SD-16 | §4.8 `created_at` | := `queue_created_at` (the intent's semantic birth — the queue row's creation); `updated_at` := `posted_at` | := history row's `created_at` (the write instant, later than birth) |
| SD-17 | §4.8 `approved_by_user_id` | := `posted_by_user_id` (in the manual flow the poster is the approver) | NULL |
| SD-18 | §4.8 `last_error` | `{"v":1,"class":"legacy","message":error_message}` when `error_message` is non-NULL, else NULL | detail-only |
| SD-19 | §4.8 status/success disagreement | `status` is authoritative; `success` verbatim into the companion detail row; profile counts disagreements first | quarantine them (volume risk for a cosmetic legacy inconsistency) |
| SD-20 | §4.9 `actor_kind` | the **original** actor (`'user'` when a user id is present, else `'system'`); migration provenance in `detail.migrated_from` | `'migration'` everywhere (queryably wrong about who acted) |
| SD-21 | §4.2 binding settings | carry `show_verbose_notifications` / `send_lifecycle_notifications` into `channel_bindings.settings` keys only when legacy non-NULL (absent key = app default, materialization contract `02` L318) | always-write (freezes env defaults as per-binding overrides) |

**INT-1 (interpretation, not decision):** the owner-derivation ladder (`04` L81) is evaluated over `is_active = true` memberships at every rung. Rung 3 says "earliest **active**"; rungs 1–2 are silent — but a derived owner whose membership is dropped as inactive (`04` L82) would leave the workspace ownerless, which `ct_workspaces_owner_at_insert` refuses at commit. The active-only reading is the only one that composes with the ratified DDL; an inactive owner-row's existence is recorded in its drop audit record.

### 3.8 Lifted to the plan author — two items the register cannot hold; the routing itself is unresolved

Both were filed as [SD-n] entries and failed the register's own entry test on review (#827 verdict, Finding 4). **Neither is resolved here.** Both await a **plan-author ruling** — and that ruling currently has **no live address**: the original routing target, #731, has been **closed since 2026-08-08** as a *completed* planning artifact (it is the consolidated design plan itself), with no successor issue. It is not stale-and-moved; there is no forwarding address to repair to. A pointer at a closed issue reads as routed when it is not — the entries would sit silently *parked*, which is the same failure this section exists to prevent, one layer over. So the honest statement is: **where plan-author rulings live is itself an open question, and choosing that destination is not this spec's call.** Live context in the meantime: **#790** (the Phase M tracker, open — comment 5318877296 carries the full situation and flags the destination question without answering it). Until ruled, the affected §4 cells read exactly as marked, and the two interims below stay deliberately **opposite** — that opposition is the standing proof nothing was quietly picked, and it is why parking here is stable rather than drifting.

- **§3.8-A (was SD-2) — `media_sources.state` at birth: two ratified texts disagree.** `04` L83: sources "born `state='error'` pending reconnect". `02` §1 L155: "`media_sync_enabled` → `media_sources.state` (false = `'paused'`)". For a sync-disabled chat these conflict, and either can be cited as authority — worse than a silence. Candidate precedences, stated without a pick: (i) `'paused'` wins at `media_sync_enabled = false`, `'error'` otherwise — preserves user intent, and an unpaused source finds `'error'` organically at first sync; (ii) `'error'` unconditionally — FC-7.2's reconnect posture wins, the pause fact surviving only in the 3f snapshot. Needs a one-line precedence ruling; §4.3's cell is **PENDING** until it lands.
- **§3.8-B (was SD-11) — `ig_accounts.state` for legacy-inactive accounts: the ruled text answers it; this spec proposes amending it.** `04` L83 and `02` §9 rule blanket `'reauth_required'`. The proposal: `is_active = false` → `'disabled'`, because a user-disabled account should not come back reconnectable. **Until the plan author rules (routing above), the ruled blanket stands and §4.4 maps accordingly** — following the ratified text pending the amendment decision is the only non-picking interim; the reverse default would be exactly the silent deviation this lift exists to prevent.

---

## §4. Per-table transforms

Format per table: disposition (from `02` §9) · per-column mapping (every legacy column accounted: **carried** with its rule, **derived**, **not carried** with destination/rationale) · derivations · quarantine feeds · postconditions live in §6. G-TIME rule abbreviations: **conv** = `AT TIME ZONE 'UTC'`; **as-is** = TIMESTAMPTZ exception; **G-STAMPS/G-IDS** as §2.

### 4.1 `legacy.users` → `users` + `user_identities` (M1-01)

| Legacy | → | Rule |
|---|---|---|
| `id` | `users.id` | G-IDS |
| `is_active` | `users.state` | `false` → `'disabled'`, else (`true`/NULL) → `'active'`; NULL count profiled |
| `telegram_user_id` | `user_identities.external_id` | `::text` (provider's immutable subject, D32); `provider='telegram'` |
| `telegram_username`, `telegram_first_name`, `telegram_last_name` | `user_identities.display_name` | SD-9; raw values also in 3f snapshot |
| `role` | — | **dissolves into memberships** (ruled, `02` §9) + SD-13 G-LOG per `'admin'` |
| `total_posts`, `last_seen_at` | — | not carried — advisory aggregates; authority is the ledger; 3f snapshot |
| `created_at`, `updated_at` | stamps | G-STAMPS (conv) |

`users.primary_email` := NULL (telegram-only users, `02` L61). One `user_identities` row per user; `verified_at` := NULL. No governance trigger on these tables — no auto-audit rows.

### 4.2 `legacy.chat_settings` → `workspaces` + `channel_bindings` (M1-02, with 4.3's members) — **minting rule W RETIRED (2026-08-21)**

**Minting rule W — "one workspace per chat row, `workspaces.id := chat_settings.id`" — is retired.
Not as a fork resolution. The shipped target schema REFUSES it on the production corpus**, so it
was never one of two readings that a ruling could pick between.

**What the schema actually says** (053:322–343, still the authority — verified that none of
054–061 alters these objects; 058 only enables RLS):

| constraint | on | consequence |
|---|---|---|
| `uq_binding_external` | **`(channel, external_ref)`** | a legacy chat binds **exactly once**; says nothing about workspaces |
| *(no unique on `workspace_id`)* | — | **N bindings per workspace is the design** |
| `ck_bindings_channel` | `channel IN ('telegram_group','telegram_dm')` | two **channel kinds**, explicitly not two tenants |
| `ct_workspaces_owner_at_insert` | `workspaces` | a workspace with no owner member row **fails at commit** |

`uq_bindings_ws_id UNIQUE (workspace_id, id)` is *not* a cardinality limit — `id` is already the
primary key, so the pair is trivially unique. It exists as a composite-FK target so child tables
can enforce tenant consistency. Reading it as "one binding per workspace" is the misreading this
section exists to close.

**Measured, both directions, on a scratch build of 052+053** (positive control first, so a pass
cannot come from everything failing):

| probe | result |
|---|---|
| one workspace + owner + **two** bindings (`telegram_group` + `telegram_dm`) | **COMMITS**; 2 rows land |
| rule W on a DM chat with zero memberships (its own workspace) | **REFUSED** — `trg_workspaces_owner_at_insert`: *"workspace … created without an owner member row"* |
| same `(channel, external_ref)` twice | **REFUSED** — `uq_binding_external` |

**And production is exactly that shape** (read-only, 2026-08-21): group `-1003688539654` carries
**3** active memberships; DM `7668871620` carries **0**. So rule W would mint an ownerless
workspace for the DM and be refused at commit. **Rule W is inexpressible against the shipped
schema on the actual corpus** — which is why the 1,095-row quarantine dissolves: it was the cost
of a mapping the schema does not admit, not a cost to weigh.

**Precisely which thing refuses it, because the DDL trigger alone does not.** A workspace minted
for a DM-shaped chat with an *arbitrary* user inserted as owner **commits fine** — measured by
rajan on an independent build, and it is the right probe to have run, because "the schema refuses
W" invites a literal reader to test exactly that and conclude the claim is overstated. The trigger
enforces *an* owner row, not a **derived** one. What closes that door is the trigger **jointly with
the pre-existing reconciliation postcondition** `count(workspace_members) = count(active legacy
memberships)` (§6, untouched by this change): a synthetic owner has no backing legacy membership,
so it breaks the count identity in the same transaction. Neither half is sufficient alone —
the trigger admits a fabricated owner, and the postcondition alone would admit an ownerless
workspace that mints no members at all. **The refusal is the pair, and stating it as the trigger's
work would leave the load-bearing half undocumented.**

**The replacement is derived, not asserted.** A DM chat binds to the workspace of the user it is
with, reached by a join that already exists — `chat_settings.telegram_chat_id` (DM) →
`users.telegram_user_id` → that user's active `user_chat_memberships` → the group chat's
workspace. Measured on production: **0** DM-chat users hold membership in more than one group
(so no tiebreak is needed), **1 of 1** DM chats resolves to exactly one group, **0** resolve to
none. **The DM user's role is `member`, not `owner`** — so the rule is *membership*, not
ownership, and a version phrased as "the workspace this user owns" would fail here.

**What would make this wrong**, stated so it is checkable rather than trusted:

- **A DM user in two groups** → the join returns >1 workspace and the rule needs a tiebreak it
  does not have. Currently 0 — but **that floor is lower than it sounds, and saying so is the
  point**: exactly ONE group chat exists, so there is only one candidate to resolve to and the
  ambiguous case is **structurally untestable today** rather than merely unobserved. A zero from
  a population that cannot produce the condition is not evidence the rule handles it. The second
  group chat is what first exercises this, and it must be re-derived then, not assumed to hold.
- **A DM user in no group** → nothing to bind to; that row is a quarantine feed, not a drop.
  Currently 0.
- **A group chat with zero active memberships** → mints no workspace under any rule, because the
  owner trigger refuses it. This is rung 4 and is unchanged.
- **A migration relaxing `ct_workspaces_owner_at_insert` or adding a unique on `workspace_id`** →
  re-derive. Verified absent through 061 today.

**Consequence for G-IDS, which the retired rule was carrying silently:** `workspaces.id :=
chat_settings.id` is only well-defined when exactly one chat mints the workspace. Under the
derived shape the **group** chat mints it and supplies the id; the DM contributes a binding and
no id. A corpus where two *group* chats collapse to one workspace has no such rule and would
need one — not this corpus, and named rather than assumed.

| Legacy | → `workspaces` | Rule |
|---|---|---|
| `id` | `id` | G-IDS; supplied by the **group** chat that mints the workspace (rule W retired — see above) |
| `display_name` | `name` | SD-12 |
| — | `state` | `'active'` (no legacy counterpart; pause is separate) |
| `posting_timezone` | `tz` | **G-TZ (ruled)** + G-LOG `tz_discarded` |
| `posts_per_day`, `posting_hours_start`, `posting_hours_end` | same names | `COALESCE(v, 3 / 14 / 2)` — target NOT NULL; literals = the target defaults (`02` L106–111) |
| — | `approval_mode`, `auto_reapprove_returning`, `approval_ttl_minutes` | `'manual'` / `false` / NULL — the three **new** columns (`02` L96–99), born at their defaults |
| `dry_run_mode`, `is_paused`, `enable_ai_captions` | same names | `COALESCE(v, false)` |
| `paused_at` | `paused_at` | conv |
| `paused_by_user_id` | `paused_by_user_id` | carried (user ids preserved) |
| `repost_ttl_days`, `skip_ttl_days` | same names | carried (NULL = app default, both sides) |
| `caption_style` | `caption_style` | SD-1 + G-LOG `caption_style_discarded` |
| `enable_instagram_api` | `api_publishing_enabled` | `COALESCE(v, false)` (ruled home, `02` L151) |
| `telegram_chat_id` | `channel_bindings.external_ref` | `::text`; `channel='telegram_group'`, `state='active'` |
| `show_verbose_notifications`, `send_lifecycle_notifications` | `channel_bindings.settings` | SD-21: `{"v":1}` + `verbose_notifications` / `lifecycle_notifications` keys when non-NULL |
| `last_post_sent_at` | — | **Fork B input** (as-is TIMESTAMPTZ) — consumed by M1-04's blocked cell, carried nowhere else |
| `active_instagram_account_id` | — | dissolves (multi-account, ruled); consumed by 4.4's profile |
| `media_source_type`, `media_source_root`, `gdrive_alerted_at` | — | → `media_sources` (4.3) |
| `onboarding_step`, `onboarding_completed` | — | → 4.10 |
| `created_at`, `updated_at` | stamps | G-STAMPS (conv) |

Auto-audit: one trigger-minted `workspace` row + one `channel_binding` row per insert (G-ACCOUNTING). Profile: `telegram_chat_id > 0` rows (§3.4 addendum).

### 4.3 `legacy.chat_settings` (Drive config) → `media_sources` (M1-03)

One row **only** where `media_source_type = 'google_drive'` — the only value `ck_sources_provider` admits; `'local'`/NULL chats mint no source (their workspace has zero sources until the owner connects one — consistent with FC-8's zero-local/upload world). New ids. `provider='gdrive'`; `config := jsonb_build_object('v',1,'folder_ref', media_source_root)`; `state`: **PENDING the §3.8-A precedence ruling** (two ratified texts conflict for sync-disabled chats — not this spec's call); `alerted_at := gdrive_alerted_at` (**as-is**); `sync_checkpoint`/`next_sync_at`/`last_sync_success_at` := NULL (checkpoint restarts post-reconnect). Zero `oauth_credentials` rows (FC-7.2, ruled). Profile: gdrive chats with NULL `media_source_root` (config would be invalid — `folder_ref` required); nonzero routes to owner at prep.

### 4.4 `legacy.instagram_accounts` × `legacy.api_tokens` → `ig_accounts` (M1-04) — **Fork B; Fork C conditional**

**Fan-out (ruled, `02` §9):** one target row per distinct (workspace, legacy account) pair **derived from `api_tokens` ownership**: pairs = `SELECT DISTINCT t.chat_settings_id, t.instagram_account_id FROM legacy.api_tokens t WHERE t.instagram_account_id IS NOT NULL AND t.chat_settings_id IS NOT NULL`. NULL-`chat_settings_id` tokens are the **Fork C conditional** here (at `group_chats = 1` they resolve to the sole group tenant — C's collapse count; otherwise C's ruling applies). `api_tokens` itself: snapshotted + dropped, never transformed; no ciphertext carried (FC-7.2, ruled) — it contributes ownership *facts* only.

| Source | → | Rule |
|---|---|---|
| `instagram_accounts.instagram_account_id` | `provider_account_ref` | verbatim (the real Meta account id) |
| `instagram_accounts.instagram_username` | `handle` | verbatim |
| `instagram_accounts.display_name` | `display_name` | verbatim |
| `instagram_accounts.is_active` | `state` | `'reauth_required'` blanket — **the ruled text** (`04` L83, `02` §9); the `is_active=false`→`'disabled'` amendment is lifted to the plan author (**§3.8-B**) and applies only if ruled; zero rows born `'active'` (FC-7.2) |
| — | `posts_per_day`, `posting_hours_start/end`, `tz` | NULL (inherit workspace, `02` L355) |
| `chat_settings.last_post_sent_at` | `next_slot_at` | **BLOCKED PENDING RULING — Fork B** (§3.2; the only blocked cell in this file) |
| — | `last_posted_at` | NULL (advisory; derivable post-hoc from terminal intents) |
| `instagram_accounts.created_at/updated_at` | stamps | G-STAMPS (conv) |

Auto-audit: one trigger-minted `ig_account` row per insert. **Profiles:** (a) legacy accounts referenced by *zero* token pairs (they would mint no row; any attributed history pointing at them quarantines downstream — surfaced at prep, since widening the ruled fan-out source is a plan change, not this spec's call); (b) `active_instagram_account_id` values outside the pair set (same class).

### 4.5 `legacy.media_items` → `media_items` (M1-05) — **#793 fail-closed**

**Ruled preconditions:** per-workspace hash dedup remediation (human-gated, zero-duplicates before the window — else `uq_media_dedup` halts the file, which is the backstop, not the plan) **and** the FC-8 gate green (§7). Admission: `source_type = 'google_drive'` rows only; in-file assert that no other `source_type` exists at 3e (`local`/`upload` re-checks FC-8 in-window; `instagram_backfill` is #793, §3.6).

| Legacy | → | Rule |
|---|---|---|
| `id` | `id` | G-IDS |
| `chat_settings_id` | `workspace_id` | non-NULL: the chat's workspace (= same UUID under W). **NULL: the 044 sole-tenant rule — ruled for this table** (`02` §9) |
| — | `source_id` | the workspace's M1-03 `media_sources` row; **profile:** gdrive media in a workspace with no source row (nonzero → owner at prep; residue at 3e → quarantine feed) |
| `source_identifier` | `provider_file_ref` | verbatim (Drive file id — the D37 canonical stable ref); **assert non-NULL** on admitted rows (target NOT NULL; profile first) |
| `file_hash` | `content_hash` | **SD-10** (verbatim; MD5/SHA256 edge flagged) |
| `mime_type` | `media_kind` + `mime_type` | **SD-7** derivation + carried |
| `file_name`, `file_size`, `category`, `title`, `caption`, `generated_caption`, `link_url`, `tags`, `custom_metadata`, `thumbnail_url` | same names | verbatim |
| `is_active` | `state` | `true`→`'available'`, `false`→`'removed'`; NULL: **assert zero** (census: 0; the gate's disclosure bucket stays the instrument) |
| `times_posted` | `times_posted` | `COALESCE(v,0)` |
| `last_posted_at` | `last_posted_at` | conv |
| `source_type` | — | dissolves into the source row's `provider` (admission-checked above) |
| `file_path` | — | not carried — identity is `provider_file_ref`; Drive path context folds into `file_name` (`02` L575) |
| `cloud_url`, `cloud_public_id`, `cloud_uploaded_at`, `cloud_expires_at` | — | not carried — transit state is per-attempt (`post_intents.transit_asset_ref`) |
| `instagram_media_id`, `backfilled_at` | — | not carried — posted evidence is per-intent; values ride the M1-08 history rows (`02` L574–576) |
| `indexed_by_user_id` | — | not carried — no target column; 3f snapshot |
| `created_at`, `updated_at` | stamps | G-STAMPS (conv) |

### 4.6 `legacy.media_posting_locks` → `post_locks` (M1-06) — **Fork E for live `recent` rows**

**SD-14 admission:** rows with `locked_until IS NULL OR locked_until > ` the transform instant. Kind map ruled verbatim (`02` L616): `recent_post→recent, skip→skip, permanent_reject→reject, manual_hold→hold, seasonal→seasonal`.

| Legacy | → | Rule |
|---|---|---|
| `id` | `id` | G-IDS |
| `media_item_id` | `media_item_id` | carried (ids preserved) |
| — | `workspace_id` | **:= the media item's workspace** (forced by composite `fk_locks_media` — §3.3's narrowing); `chat_settings_id` is a cross-check only, mismatch → profile + quarantine feed |
| `lock_reason` | `kind` | ruled map above |
| `locked_until` | `expires_at` | conv; NULL = permanent, kept (ruled) |
| — | `ig_account_id` | non-`recent` kinds: NULL (workspace-scoped, `ck_locks_recent_scope`). **`recent` kinds: BLOCKED PENDING RULING — Fork E** (§3.5); profile counts live `recent` rows — zero moots E at execution |
| `created_by_user_id` | `created_by_user_id` | carried |
| — | `created_by_intent_id` | NULL (no legacy intent) |
| `locked_at` / `created_at` | stamps | `created_at := locked_at` conv (semantic birth); `updated_at := created_at` |

### 4.7 `legacy.category_post_case_mix` → `category_post_case_mix` (M1-07) — **Fork C conditional**

| Legacy | → | Rule |
|---|---|---|
| `id` | `id` | G-IDS |
| `chat_settings_id` | `workspace_id` | non-NULL: the chat's workspace. **NULL: BLOCKED (C1/C2/C3) — conditional** (§3.3) |
| `category`, `ratio` | same names | verbatim (target CHECK is `>= 0`, weaker than legacy's 0..1 — no admission risk) |
| `effective_from`, `effective_to` | same names | conv |
| `is_current` | — | **not carried — ruled redundant** (`02` L627–629); SD-15: profile `is_current`↔`effective_to` disagreements first; `uq_case_mix_current` is the backstop against double-current rows |
| `created_by_user_id` | `created_by_user_id` | carried |
| `created_at` | stamps | G-STAMPS (conv) |

### 4.8 `legacy.posting_history` → `post_intents` + companion `audit_events` (M1-08) — **attribution; Fork A feed; Fork C conditional**

**Attribution (ruled mechanism, `04` L85):** the account-switch timeline reconstructed from `legacy.service_runs` — `service_name = 'InstagramAccountService'`, `method_name IN ('switch_account','add_account')`, completed rows; the switched-to account is `input_params->>'account_id'` (a legacy `instagram_accounts.id`), ordered by `started_at` (conv). A history row attributes to the account active at `posted_at`; **±6h tolerance at boundaries, nearest-timestamp tie-break** (ruled). Feeds to quarantine (§5.2): rows in a timeline gap; rows matching nothing; ambiguous-boundary rows beyond the tie-break; the two auto-set paths that write no `service_runs` record (the pass-4-named known gap). **Degenerate short-circuit:** a workspace whose pair-set (4.4) holds exactly one account attributes every row to it trivially — no timeline consulted; at the current census this is the expected world, and the profile sizes it. `service_runs` carries **no tenant column**: at `chats = 1` the global timeline is trivially exact; at `group_chats = 1` with DM rows present it is exact iff no attributable history resolves outside the sole group tenant **and no switch-timeline events originate outside it** — pollution enters on the *events* side, not only the history side (#827 approval note), so the §5.1 battery profiles **both**. An events-side polluter is loud rather than silent: it pulls attribution toward a (workspace, account) pair absent from the workspace's pair-set, which the composite `fk_intent_account` refuses into the quarantine feed instead of admitting quietly. Under N > 1 group tenants per-workspace timelines need `user_id`-membership inference — that residue is **C-conditional and corpus-conditional, not D-conditional** — the axis is how many GROUP tenants exist (Fork C's count), plus whether any switch events were ever recorded; D no longer names an axis at all (§3.4). Named rather than silently absorbed. **And the second half is now measured: there are ZERO `switch_account`/`add_account` rows in `service_runs`, all time** — the table holds 7,551 rows across 7 services and `InstagramAccountService` is not one of them, so this mechanism has no input on the migration corpus regardless of tenant count (#943). `service_runs` itself: consumed here, snapshotted at 3f, never lands in target (ruled).

| Legacy | → `post_intents` | Rule |
|---|---|---|
| `id` | `id` | G-IDS |
| `chat_settings_id` | `workspace_id` | non-NULL: the chat's workspace. **NULL: BLOCKED (C1/C2/C3) — conditional** |
| — | `ig_account_id` | **derived: attribution above** (NOT NULL — unattributable rows are a quarantine feed, ruled) |
| — | `provider_account_ref` | the attributed account's `instagram_account_id` (immutable copy, `02` key 4) |
| `media_item_id` | `media_item_id` | carried; composite-FK consistency profiled (media in a different workspace → quarantine feed) |
| `status` | `state` | **1:1, ruled** (`posted/failed/skipped/rejected/expired`); SD-19 on `success` disagreement |
| — | `published_via` | `'legacy_backfill'` (ruled; exempts evidence CHECKs) |
| — | `approval_mode` | **SD-3** |
| `scheduled_for` | `schedule_slot_at` | conv |
| `posted_at` | `entered_state_at` | conv (the terminal instant) |
| `queue_created_at` | `created_at` | conv — **SD-16** (semantic birth) |
| `posted_at` | `updated_at` | conv |
| `instagram_media_id`, `instagram_story_id` | `ig_media_id` | **SD-4** |
| `instagram_permalink` | `ig_permalink` | verbatim |
| `queue_item_id` | `legacy_queue_item_id` | verbatim (provenance; 30-day post-M.3 drop rule is printed in the DDL) |
| `posted_by_user_id` | `approved_by_user_id` | **SD-17** |
| `error_message` | `last_error` | **SD-18** |
| — | `publish_step` | `'none'`; `ig_container_id`/`transit_asset_ref`/cap columns NULL; `attempts_by_step` default (all legal under the `legacy_backfill` exemption, `02` L708–713) |
| `posting_method`, `posted_by_telegram_username`, `success`, `queue_deleted_at`, `error_message` (verbatim), `instagram_story_id` (verbatim) | companion `audit_events` row | **ruled destination** (`02` §9 "posting_method/usernames → audit detail"): one row per intent — `entity_kind='post_intent'`, `entity_id=` intent id, `to_state=` terminal state, `actor_kind='migration'`, `detail={"v":1,"migrated_from":"posting_history",...}` |

Terminal-at-insert is the guard's sanctioned path (`trg_intent_insert_guard`, actor `'migration'`); no UPDATE ever touches these rows, so the freeze machinery is never in play. Queue exclusion: **no intents are minted for pending `posting_queue` rows** (FC-7.4, ruled) — asserted in §6 against the surviving `legacy.posting_queue` ids.

### 4.9 `legacy.audit_log` + `legacy.user_interactions` → `audit_events` (M1-09) — **Fork C conditional + §3.4 addendum**

Rows migrate **verbatim into `detail`** (ruled), typed fields mapped for queryability:

| Source | Mapping |
|---|---|
| `audit_log` | `workspace_id`: non-NULL `chat_settings_id` → its workspace; **NULL → BLOCKED (C) — conditional**. `entity_kind` per **SD-8**; `entity_id` carried (G-IDS keeps `setting`/`lock` referents live; membership referents survive only in detail); `from_state`/`to_state` := NULL (field-grain changes live in detail); `actor_kind` per **SD-20** + `actor_user_id := changed_by_user_id`; `channel` := NULL; `detail := {"v":1,"migrated_from":"audit_log", entity_type, action, field_changed, old_value, new_value, legacy_id}`; `created_at` conv |
| `user_interactions` | `workspace_id`: via `telegram_chat_id` → `legacy.chat_settings.telegram_chat_id` → workspace; **NULL/unresolvable → the §3.4 addendum class** (C-adjacent feed). `entity_kind='interaction'` (SD-8); `entity_id` := NULL (legacy id in detail); `actor_kind`: `'user'` unless `interaction_type='bot_response'` → `'system'` (SD-20); `channel='telegram'`; `detail := {"v":1,"migrated_from":"user_interactions", interaction_type, interaction_name, context, telegram_message_id, telegram_chat_id, legacy_id}`; `created_at` conv |

No triggers fire on `audit_events` inserts; no actor GUC is required by the DDL for them (the guard set is governance tables + intents) — the file still sets it (contract rule 2). Profile: `user_interactions.interaction_type` values outside the legacy CHECK set (the known `onboarding_dropout` writer at `conversation_service.py:133` implies drift is possible; the mapping above carries any value — `detail` is typed open).

### 4.10 `legacy.onboarding_sessions` (+ `chat_settings` onboarding columns) — proposed drop behind its check (M1-10)

**Default branch (the printed proposal, `04` L88, pending the owner's batch nod — none load-bearing):** drop behind a zero-row check. M1-10 then contains only assertions: zero non-expired legacy sessions (`expires_at > ` transform instant, conv) **and** zero chats with `onboarding_completed = false`; nothing minted; 3f snapshot preserves rows. Nonzero at profile → owner at prep (finish or void the session), since the two step vocabularies cannot honestly map: legacy `chat_settings.onboarding_step` (`welcome/media_folder/indexing/schedule`, unenforced) names a *different machine* than the target CHECK (`naming/awaiting_group/connect_identity/complete`).

**Alternate branch (nod withheld):** re-key per `02` §9 — `id`/`user_id` kept; `step` maps `naming→naming`, `awaiting_group→awaiting_group`, `complete→complete` (the legacy table's own CHECK set maps 1:1; `connect_identity` is target-new); `pending_instance_name → pending_workspace_name`; `pending_chat_settings_id → pending_workspace_id`; `expires_at`/`created_at` conv; `updated_at := created_at`.

### 4.11 Not transformed (each with its guarantee)

| Table | Disposition (all ruled) | 3e artifact |
|---|---|---|
| `api_tokens` | snapshot + drop; no ciphertext carried; contributes 4.4's ownership facts only (FC-7.2) | §6 assert: `oauth_credentials` is empty |
| `posting_queue` | **not transformed** (FC-7.4): no intents minted; archived; scheduler re-plans from cadence; R6 renders stale cards "expired". Load-bearing: the step-8 identity guards key on `posting_queue` as the legacy-only marker | §6 assert: no intent carries a surviving queue id |
| `service_runs` | consumed by 4.8, snapshotted, never lands | none (nothing written) |
| `schema_version` | superseded by the runner ledger via 3a adopt (its 010/034 gaps are the point); snapshot + drop | none |
| `daily_post_counts` (target) | **starts empty** — days-long window makes rolling caps correct by construction | §6 assert: empty |
| `waitlist_signups` | out of scope permanently (Drizzle-owned; no Python migration touches it) | none |

---

## §5. Quarantine — feeds and detection (written) · procedure (BLOCKED: Fork A)

### 5.1 The pre-window profile battery (G-PROFILE; read-only, live legacy, window prep)

Collected from §4 — each query names its section, expected result **0**, and its nonzero route. This battery is Fork-A-invariant: under A1 it *is* the detection instrument; under A2/A3 it is still the cheap early warning. (Battery: users.is_active NULL · chats with `telegram_chat_id > 0` · gdrive chats with NULL root · accounts outside the token pair-set · active-pointer orphans · non-gdrive `media_items` incl. the FC-8 pair + `instagram_backfill` + NULL `is_active` · NULL `source_identifier` on gdrive rows · media/lock/history workspace mismatches · unmappable `media_kind` · live `recent` locks (Fork E sizer) · `is_current`↔`effective_to` disagreements · status/success disagreements · attribution gap/ambiguity/recordless counts + the single-account short-circuit sizer + switch-timeline events not attributable to the sole group tenant (the events-side pollution check, #827 approval note) · NULL-tenant row counts per Fork C table · unresolvable `user_interactions` chat ids · live onboarding rows. Each is a plain SELECT over §4's stated predicates; the assembled script belongs beside `fc8_gate.py` and lands with the M1 files, not before Fork A is ruled.)

### 5.2 The feeds (ruled)

1. **Owner derivation rung 4** (`04` L81): chats with zero `is_active=true` memberships at any rung (INT-1). Detection: the ladder as a `SELECT` with a `NOT EXISTS` tail.
2. **History attribution** (`04` L85): timeline-gap rows, no-match rows, ambiguous-boundary rows beyond the ±6h nearest tie-break, and the two recordless auto-set paths.
3. **Discrepancy classes promoted by this spec:** lock/media and history/media workspace mismatches (4.6, 4.8); Fork E rows under E2 only; Fork C rows under C3 only.

The gate requires the list **empty** before proceeding (`04` L81); M.3's window log records the adjudications (`04` L262).

### 5.3 The procedure — BLOCKED PENDING RULING (Fork A), deliberately

What can be stated without pre-empting the ruling: detection (above), the empty-list gate, and the log obligation. What CANNOT be written yet: **where the list lives, what shape it has, and who adjudicates it** — because the printed privilege model has no actor who can write to legacy data in-window (§3.1). Under A1 this section becomes a prep runbook; under A2 it becomes DDL + a resolution file + a grant; under A3 it becomes "the runner halts and the owner repairs," which requires the owner-actor privilege #787's class covers.

**The trap, stated so it cannot fire silently:** every quarantine-adjacent postcondition in §6 is written as the *detection* arm and will pass green on empty CI fixtures whether or not an adjudication procedure exists. Landing 3e files before Fork A is ruled therefore selects A3 **by default** — a green check that cannot fail until M.2 runs it against real branched data, the expensive place. File-contract rule 8 exists for exactly this; gate item 3 is honestly **short** until A is ruled (§8).

---

## §6. Postconditions and reconciliation queries

Per file, the verbatim `-- runner:postcondition` lines (each a single-boolean SELECT; the runner fails the file on anything but one `true`). These are the review objects gate item 2 names. Conventions: `L.` = `legacy.`; fork-conditional terms are marked and enter only under their ruling; every count identity closes over §2.6's provenance markers.

**M1-01**
```
-- runner:postcondition SELECT (SELECT count(*) FROM users) = (SELECT count(*) FROM legacy.users)
-- runner:postcondition SELECT (SELECT count(*) FROM user_identities WHERE provider='telegram') = (SELECT count(*) FROM legacy.users)
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM legacy.users lu JOIN users u ON u.id = lu.id WHERE u.state <> CASE WHEN lu.is_active IS FALSE THEN 'disabled' ELSE 'active' END)
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM user_identities ui JOIN legacy.users lu ON lu.id = ui.user_id WHERE ui.external_id <> lu.telegram_user_id::text)
```

**M1-02** (workspaces + members + bindings; owner invariant is *also* enforced by the deferred triggers at this file's commit — these make the arithmetic visible)
```
-- runner:postcondition SELECT NOT EXISTS (SELECT cs.telegram_chat_id::text FROM legacy.chat_settings cs EXCEPT (SELECT b.external_ref FROM channel_bindings b UNION SELECT d.external_ref FROM <recorded chat drops> d UNION SELECT q.external_ref FROM <quarantine: rung-4 chats> q))   -- THE MASTER IDENTITY, restated as a PARTITION over legacy chats: every chat becomes a binding, a recorded drop, or a quarantine entry. See "why this is no longer counted in workspaces" below
-- runner:postcondition SELECT NOT EXISTS (SELECT ref FROM (SELECT b.external_ref AS ref FROM channel_bindings b UNION ALL SELECT d.external_ref FROM <recorded chat drops> d UNION ALL SELECT q.external_ref FROM <quarantine: rung-4 chats> q) buckets EXCEPT SELECT cs.telegram_chat_id::text FROM legacy.chat_settings cs)   -- NOTHING INVENTED: no bucket names a chat legacy never had. Covers all three buckets, not just bindings -- a drop recorded for a nonexistent chat is as wrong as a binding minted for one
-- runner:postcondition SELECT NOT EXISTS (SELECT ref FROM (SELECT b.external_ref AS ref FROM channel_bindings b UNION ALL SELECT d.external_ref FROM <recorded chat drops> d UNION ALL SELECT q.external_ref FROM <quarantine: rung-4 chats> q) buckets GROUP BY ref HAVING count(*) > 1)   -- DISJOINT: no chat lands in two buckets. UNION ALL is load-bearing here -- UNION would dedupe the duplicate away and the check would pass on exactly what it exists to catch
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM workspaces w WHERE NOT EXISTS (SELECT 1 FROM channel_bindings b WHERE b.workspace_id = w.id))   -- replaces the bindings-1:1-to-workspaces check; see below for why that one had to go
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM workspaces w WHERE NOT EXISTS (SELECT 1 FROM workspace_members m WHERE m.workspace_id = w.id AND m.role='owner'))
-- runner:postcondition SELECT (SELECT count(*) FROM workspace_members) = (SELECT count(*) FROM legacy.user_chat_memberships WHERE is_active IS TRUE)   -- + deferred-chat term under A2 (a deferred chat defers its members with it)
-- runner:postcondition SELECT (SELECT count(*) FROM audit_events WHERE actor_kind='migration' AND entity_kind='member' AND detail->>'transform_log'='membership_dropped_inactive') = (SELECT count(*) FROM legacy.user_chat_memberships WHERE is_active IS NOT TRUE)   -- + deferred-chat term under A2
-- runner:postcondition SELECT (SELECT count(*) FROM workspaces WHERE tz='UTC' AND id IN (SELECT id FROM legacy.chat_settings WHERE posting_timezone IS NOT NULL AND fn_safe_tz(posting_timezone) <> posting_timezone)) = (SELECT count(*) FROM audit_events WHERE detail->>'transform_log'='tz_discarded')   -- both sides defer together under A2; holds under every A shape
```

**Why the master identity is no longer counted in workspaces** (restated 2026-08-21; mason
raised both of these against Fork D, and neither had been printed).

The earlier form was `workspaces + rung-4 quarantine = legacy chat count`. It assumed one
workspace per legacy chat — minting rule W, which §3.4 has always labelled Fork D's obvious
reading rather than a ruling. **Under a collapse shape it stops balancing**: production has
two legacy chats and one of them is a DM that mints no workspace of its own, so the identity
reads `1 + 0 = 2`. Measured, not argued — evaluated against the shipped target schema with
that shape built: **`False`**.

What is actually conserved is not workspaces. **Every legacy chat becomes exactly one of
three things: a binding, a recorded drop, or a quarantine entry.**

**Saying "partition" is a claim with three parts, and the first version of this section only
made one of them.** A partition is exhaustive *and* disjoint *and* introduces nothing from
outside the source set. The set-difference over legacy chats proves only the first. Both gaps
were reachable — measured against the same modelled buckets:

| defect | exhaustiveness alone | with the two lines added |
|---|---|---|
| a chat recorded as **both** a binding and a drop | **passes** | caught by the disjointness line |
| a **drop recorded for a chat legacy never had** | **passes** | caught by the nothing-invented line |

The earlier reverse-direction check was scoped to bindings, so the second row escaped it
entirely: a phantom drop is exactly as wrong as a phantom binding and was not being looked
for. `UNION ALL` rather than `UNION` in both lines is not stylistic — `UNION` would dedupe a
double-bucketed chat away and the disjointness check would pass on the one thing it exists to
catch.

**The form is fork-independent, which is why it is written now rather than after the ruling.**
It holds under a collapse shape (two bindings on one workspace, or one binding plus one
recorded drop) and under separate-workspace minting (two bindings on two workspaces) without
changing a character. Only the values it evaluates to depend on the ruling. **Under a
separate-workspace ruling this postcondition is still not sufficient on its own** — the
shipped schema fails that shape for reasons that are not about counting (a DM chat with zero
memberships cannot satisfy `ct_workspaces_owner_at_insert`, and its history rows point at
media owned by the other workspace, which the composite tenant FK makes inexpressible). That
is a DDL question and it is deliberately not answered here.

**It is a partition rather than a re-balanced count, and that is load-bearing.** A count
identity balances whenever two errors cancel. Measured: drop the DM chat silently and mint one
binding for a chat that does not exist in legacy, and the count form returns **`True`** while
the set form returns **`False`**. A count that can be satisfied by two mistakes is not a
conservation check.

**Why the bindings check had to be replaced, and why it is the more dangerous of the two.**
`count(bindings WHERE channel='telegram_group') = count(workspaces)` does **not fail** under a
collapse shape — measured, it returns **`True`**. `ck_bindings_channel` admits `telegram_dm`
as well, so a DM binding carried onto the group workspace is simply invisible to a predicate
filtered on `telegram_group`. The check goes on passing while no longer conserving what it was
written to conserve. A postcondition that breaks announces itself; one that goes quiet does
not. Chat-to-binding conservation now lives in the partition above, and what remains here is
the property that genuinely holds under every shape: **no minted workspace is binding-less**.

**The rung-4 term stays, and stating why matters more than the line itself.** Under a collapse
shape the DM mints no workspace, so on *this corpus* the owner-less-workspace failure does not
arise. **That is a fact about this corpus and not about the design.** The ladder still has four
rungs, rung 4 still has no adjudication procedure (Fork A), and production exercises neither
rung 1 nor rung 2 — `m1_preflight` prints exactly that on every run. A chat with memberships
that are all inactive still resolves to rung 4 under any Fork D shape. The term is therefore
kept rather than dropped, and what keeps that honest is a test rather than this paragraph:
`test_m1_ladder.py::TestEachRungIsReachedAndTakesPrecedence::test_rung4_quarantine_when_every_membership_is_inactive`
seeds a chat whose every membership is inactive and asserts it still resolves to rung 4. It is
marked `integration`, and CI runs `pytest tests/` with no marker filter, so it executes on every
run regardless of which way Fork D is ruled.

**A third line in this block is exposed to the same fork and is NOT restated here, because it
is latent rather than live.** `count(workspace_members) = count(active legacy memberships)`
assumes the two sides cannot collide. Under a collapse they can: `workspace_members` is keyed
`(workspace_id, user_id)`, so a user holding a membership in *both* collapsed chats would
produce one target row from two legacy rows and the count would come up short — a genuine
failure, and one that reads as a miscount rather than as a collapse consequence.

**Measured on production (read-only, 2026-08-21): zero users hold a membership in more than one
chat.** One chat carries all three memberships; the DM carries none. So the identity holds at
`3 = 3` under either ruling and there is nothing to fix today. It is named because the
condition protecting it is a property of the *data*, not of the mapping — a second chat
gaining a shared member before the window would make it live, and the same query re-run at
window prep is what would catch that.

**M1-03**
```
-- runner:postcondition SELECT (SELECT count(*) FROM media_sources) = (SELECT count(*) FROM legacy.chat_settings WHERE media_source_type='google_drive')
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM media_sources WHERE config->>'folder_ref' IS NULL)
```

**M1-04**
```
-- runner:postcondition SELECT (SELECT count(*) FROM ig_accounts) = (SELECT count(DISTINCT (t.chat_settings_id, t.instagram_account_id)) FROM legacy.api_tokens t WHERE t.instagram_account_id IS NOT NULL AND t.chat_settings_id IS NOT NULL)   -- + C-term when ruled
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM ig_accounts WHERE state = 'active')
-- runner:postcondition SELECT (SELECT count(*) FROM oauth_credentials) = 0
-- [Fork B arm] next_slot_at assertion per B ruling
```

**M1-05**
```
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM legacy.media_items WHERE source_type NOT IN ('google_drive'))   -- FC-8 in-window re-check + #793 fail-closed
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM legacy.media_items WHERE is_active IS NULL)
-- runner:postcondition SELECT (SELECT count(*) FROM media_items) = (SELECT count(*) FROM legacy.media_items WHERE source_type='google_drive')
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM media_items mi JOIN legacy.media_items lmi ON lmi.id = mi.id WHERE mi.content_hash <> lmi.file_hash)
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM legacy.media_items WHERE mime_type IS NULL OR (mime_type NOT LIKE 'image/%' AND mime_type NOT LIKE 'video/%'))
```

**M1-06**
```
-- runner:postcondition SELECT (SELECT count(*) FROM post_locks) = (SELECT count(*) FROM legacy.media_posting_locks WHERE locked_until IS NULL OR locked_until > now() AT TIME ZONE 'UTC')   -- E-term adjusts under E2/E3
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM post_locks WHERE (kind='recent') <> (ig_account_id IS NOT NULL))
```

**M1-07**
```
-- runner:postcondition SELECT (SELECT count(*) FROM category_post_case_mix) = (SELECT count(*) FROM legacy.category_post_case_mix)   -- + C-term when ruled
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM category_post_case_mix c1 JOIN category_post_case_mix c2 ON c1.workspace_id=c2.workspace_id AND c1.category=c2.category AND c1.id<c2.id WHERE c1.effective_to IS NULL AND c2.effective_to IS NULL)
```

**M1-08** (the master identity: every history row lands or is accounted)
```
-- runner:postcondition SELECT (SELECT count(*) FROM post_intents WHERE published_via='legacy_backfill') + (SELECT count(*) FROM <quarantine: history feeds>) = (SELECT count(*) FROM legacy.posting_history)   -- quarantine term's shape per Fork A; C-term when ruled
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM post_intents WHERE published_via='legacy_backfill' AND state NOT IN ('posted','skipped','rejected','expired','failed'))
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM post_intents pi JOIN legacy.posting_history ph ON ph.id = pi.id WHERE pi.state <> ph.status)
-- runner:postcondition SELECT (SELECT count(*) FROM audit_events WHERE detail->>'migrated_from'='posting_history') = (SELECT count(*) FROM post_intents WHERE published_via='legacy_backfill')
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM post_intents WHERE legacy_queue_item_id IN (SELECT id FROM legacy.posting_queue))   -- FC-7.4: pending queue minted nothing
```

**M1-09**
```
-- runner:postcondition SELECT (SELECT count(*) FROM audit_events WHERE detail->>'migrated_from'='audit_log') = (SELECT count(*) FROM legacy.audit_log)   -- C-term when ruled
-- runner:postcondition SELECT (SELECT count(*) FROM audit_events WHERE detail->>'migrated_from'='user_interactions') = (SELECT count(*) FROM legacy.user_interactions)   -- addendum-class term per C ruling
```

**M1-10** (default branch)
```
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM legacy.onboarding_sessions WHERE expires_at > now() AT TIME ZONE 'UTC')
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM legacy.chat_settings WHERE onboarding_completed IS FALSE)
```

**M1-11** — cross-table closure
```
-- runner:postcondition SELECT (SELECT count(*) FROM daily_post_counts) = 0
-- runner:postcondition SELECT (SELECT count(*) FROM oauth_credentials) = 0
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM post_intents pi WHERE NOT EXISTS (SELECT 1 FROM ig_accounts a WHERE a.workspace_id=pi.workspace_id AND a.id=pi.ig_account_id))
-- runner:postcondition SELECT (SELECT count(*) FROM audit_events WHERE actor_kind='migration' AND detail->>'op'='INSERT') = (SELECT count(*) FROM workspaces) + (SELECT count(*) FROM workspace_members) + (SELECT count(*) FROM ig_accounts) + (SELECT count(*) FROM channel_bindings)   -- G-ACCOUNTING closes
```

*(The `<quarantine …>` terms are the one notation this file cannot make concrete: their table/expression is Fork A's output. Under A1 each collapses to a literal `0`. Under A2 note the subtree consequence: a deferred **chat** defers its whole subtree — workspace, members, bindings, sources, accounts, media, history — so every downstream count identity gains the same deferred term, all closed by the resolution file; the `-- + deferred-chat term under A2` markers name the sites.)*

---

## §7. The FC-8 gate's documented home (bullet already landed, #792)

`scripts/fc8_gate.py` — window prep, run before step 1 against production (read-only): exit 0 CLEAR / 1 HALT / 2 ERROR. Halts on exactly FC-8's two counts (live `local`/`upload` media; history resolving to them); reports any other origin as UNCLASSIFIED **without halting** and the `is_active IS NULL` bucket separately — both are disclosure, not authority; widening the halt set is #793's ruling, not the gate's. Resolutions on HALT (ruled): migrate the files to Drive first, or an explicit accept-loss list — both zero-schema; weakening the `02` NOT NULL chain is recorded as rejected. M1-05 re-asserts the zero **in-window** (the prep-to-3e drift guard). Last production run: 2026-08-13, CLEAR, all four counts zero (#793 census comment). This section is the gate's first documentation home outside `CHANGELOG.md`; the M.3 runbook should cite it as a named precondition line.

---

## §8. Gate self-assessment (`04` L89, verbatim: "every transform file carries postconditions; the spec's per-table reconciliation queries are review-approved; the quarantine procedure is written")

| Gate item | State | Where |
|---|---|---|
| Every transform file carries postconditions | **Met by this spec** — §6 enumerates ≥1 per file; file-contract rule 4 makes an empty set unlandable; adopt-probe evidence comes free | §1.2, §6 |
| Per-table reconciliation queries review-approved | **Met at this PR's approval** — the queries are in §6 in full; review of this document *is* the approval mechanism; fork-conditional terms are approved as marked, and their concretization re-enters review with the ruling | §6 |
| The quarantine procedure is written | **SHORT — deliberately.** Feeds and detection are written (§5.1–5.2); the procedure's home, shape, and actor are Fork A's output and are **not invented here** (per the dispatch: a spec that says so beats one that quietly picks A3) | §5.3, §3.1 |

**Executable now vs awaiting a ruling:** M1-01, M1-05 (modulo its ruled preconditions), M1-07 (C-conditional cell aside), M1-10, M1-11 are fully determined. M1-03 is determined except its `state` cell (§3.8-A — two ratified texts conflict, so no interim default exists). M1-02/M1-08 are determined except their Fork A quarantine terms (and C cells); M1-04 except Fork B's one cell (and C's token residue — §3.8-B's interim is the ruled blanket, so that cell IS determined); M1-06 except Fork E's live-`recent` rows. **No file lands before Fork A is ruled** (contract rule 8) — but under the F.2 constraint none *can* land yet anyway (the 3e band opens when F.2.9 closes), so the fork-ruling window is exactly the F.2 build window, and it costs nothing on the critical path **if** the rulings land within it.

**Program-shape note (not this spec's call):** #790 comment 5280282481 proposes splitting M.1 into derivations-now / INSERTs-after-F.2. This document is deliberately structured so either shape works: §4's derivations (owner ladder, role map, attribution timeline, tz classification) are testable against seeded legacy fixtures without any target table; §6's INSERT-side postconditions bind only when the files land.
