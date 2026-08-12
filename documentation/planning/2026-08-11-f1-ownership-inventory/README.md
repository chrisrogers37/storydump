# F.1 — Ownership inventory and fail-closed interface spec

Verification against the ratified plan, per `04` F.1. **Documentation only — no schema, no
migration, no production-table change.** Classification is read off the plan, not invented here.

## 0. Where the answer key actually lives

`04` F.1 says "`02` §9 is the answer key". §9 is necessary but **not sufficient**, and this
matters for anyone repeating the work:

- **`02` §9** is a *disposition index* — legacy table → what it becomes. Its own header says so.
  It never uses the words "global" or "tenant-owned"; those appear in exactly one place in the
  whole plan, the F.1 line in `04`.
- **`02` §7-DDL** carries the ownership taxonomy, as **seven normative policy classes**, with the
  Class-1 membership list explicitly marked *"the list IS the normative enumeration"*.

So the answer key is **§9 ∘ §7-DDL**: §9 says what a legacy table becomes, §7-DDL says which
class that target sits in. Every classification below is that composition — nothing is derived
from judgement.

**It is also not a binary.** The plan's classes are richer than global/tenant-owned, and two of
the fourteen legacy tables have *no target at all*, which the binary cannot express. Forcing them
into it would be filling a gap quietly, so they are named as their own outcome.

## 1. The inventory — all 14

Legacy set taken from the code (`__tablename__` across `src/models/`), not from the plan: **14
tables**, matching §9 exactly. §9 additionally covers `schema_version` (the ledger) and
`waitlist_signups` (Drizzle-owned, permanently out of scope) — neither is a SQLAlchemy model, which
is why §9 has 16 rows for 14 tables.

| # | Legacy table | §9 target disposition | §7-DDL class | Ownership |
|---|---|---|---|---|
| 1 | `users` | `users` + `user_identities` | Class 3 user-plane | **GLOBAL** |
| 2 | `chat_settings` | splits: `workspaces`, `channel_bindings`, `media_sources`, `onboarding_sessions` | Class 1 **and** Class 3 | **TENANT — but see §2** |
| 3 | `user_chat_memberships` | `workspace_members` | Class 1 | **TENANT-OWNED** |
| 4 | `user_interactions` | `audit_events` | Class 6 audit | **TENANT-OWNED** |
| 5 | `instagram_accounts` | `ig_accounts` (fan-out, per-workspace) | Class 1 | **TENANT-OWNED** |
| 6 | `api_tokens` | **not migrated** (FC-7.2) | — | **NO TARGET** |
| 7 | `media_items` | `media_items` re-keyed | Class 1 | **TENANT-OWNED** |
| 8 | `media_posting_locks` | `post_locks` | Class 1 | **TENANT-OWNED** |
| 9 | `posting_queue` | **not transformed** (FC-7.4) | — | **NO TARGET** |
| 10 | `posting_history` | `post_intents` terminal states | Class 1 | **TENANT-OWNED** |
| 11 | `category_post_case_mix` | kept row-shaped, re-keyed to `workspace_id` | Class 1 | **TENANT-OWNED** |
| 12 | `onboarding_sessions` | kept, re-keyed (`pending_workspace_id`) | Class 3 user-plane | **GLOBAL** |
| 13 | `audit_log` | merged into `audit_events` | Class 6 audit | **TENANT-OWNED** |
| 14 | `service_runs` | consumed by M.1, archived, **never lands** | — | **NO TARGET** |

**Totals: 9 tenant-owned · 2 global (user-plane) · 3 no-target.**

## 2. The finding that matters for the interface work

**`chat_settings` does not classify as one thing.** It is the only legacy table that splits across
two ownership classes:

- config → `workspaces` (Class 1, tenant-plane — and `workspaces` *is* the tenant root, keyed on
  `id` rather than `workspace_id`)
- chat facts → `channel_bindings` (Class 1)
- media source config → `media_sources` (Class 1)
- **onboarding columns → `onboarding_sessions` (Class 3, user-plane — no workspace key exists)**

F.1's rule is "every tenant-scoped repository method takes required leading `tenant_id`". Applied
uniformly to everything `chat_settings` becomes, that rule **breaks onboarding**: §7-DDL Class 3
states the reason in normative terms — *"identity precedes tenancy (sign-in, identity upsert,
onboarding all run before any `app.tenant_id` can be set)"*. A required `tenant_id` on the
onboarding path demands a value that does not exist yet.

This is the concrete shape of "a table that cannot be migrated": not a mapping error, but a
*fail-closed rule applied one class too widely*. The interface spec below is written so the rule
attaches to the class, not to the legacy table.

`users` carries a milder version of the same: today it is reached through chat-scoped paths, but
its target is user-plane, so its repository must **not** gain a required `tenant_id`.

## 3. Fail-closed interface spec

Three rules, keyed to the class rather than the table.

**Rule 1 — Class 1 (tenant-plane) and Class 6 (audit): required leading `tenant_id`.**
Every repository method takes `tenant_id: UUID` as its first positional parameter. No default, no
`Optional`, no keyword fallback. A caller without a tenant cannot construct the call.
*Covers the 9 tenant-owned tables above.*

**Rule 2 — Class 3 (user-plane): required leading `user_id`, and `tenant_id` is forbidden.**
`users`, `user_identities`, `onboarding_sessions`. These run before tenancy exists. Their
isolation authority is the central authorization gate (`01` §Process roles), stated by §7-DDL as a
deliberate class — not RLS. Adding `tenant_id` here would be a false guarantee, since the DB has
no predicate to enforce it against.

**Rule 3 — the fail-open pattern is extinct.** `if chat_settings_id:` and every variant that lets a
call proceed when tenant context is absent is removed. Absent context is an error at the call
boundary, never a widened query. This is the F.1 gate: *"fail-closed tests prove tenant access
cannot run without context."*

**Test obligations** (F.1 gate, restated executably):
- per Class-1/6 repository: a call omitting `tenant_id` fails at construction, not at the DB
- per Class-1/6 repository: a cross-tenant read returns zero rows with a foreign `tenant_id` set
- per Class-3 repository: a call *with* `tenant_id` is rejected — the negative direction, so the
  classes cannot silently converge
- the extinction check is a grep-gate over the fail-open pattern, and it must be shown to FAIL on
  a reintroduced instance, or it proves nothing

## 4. F.6 baseline — re-measured, and it does not reproduce

`04` F.6 anchors the ratchet at *"75 at the 2026-08-02 measurement, 76 at the pass-4 anchor"*, and
the dispatch quoted 75 of 147 modules. **Neither number reproduces, and the denominator has moved
too.** Measured today (`main @ 0af141a`) and at the plan date (`main @ 2e13f97c`):

| scope | 2026-08-02 | today | plan says |
|---|---|---|---|
| `src/` modules | 135 | 136 | — |
| `src/` referencing telegram | 64 | 64 | — |
| `src/`+`cli/` modules | 148 | 149 | **147** |
| `src/`+`cli/` referencing telegram | 67 | 67 | **75** |

Predicate variants today, over `src/`: any `telegram` 64 · imports the `telegram` package 18 ·
`telegram` or `chat_id` 70 · case-insensitive 72. **None is 75 or 76.**

Two conclusions, and the second is the actionable one:

1. **The code has not drifted on this axis.** 64 → 64 in `src/`, 67 → 67 in `src/`+`cli/` across
   the whole window. The dispatch's hypothesis — that #744 moved it — is **refuted**: #744 changed
   the module count by **zero** and every file it touched was already Telegram-referencing (2, 11,
   33 and 37 matches respectively *before* the change). It raised density in one file, 11 → 18, and
   moved no module across the boundary.
2. **The predicate is unrecoverable from the plan.** The number cannot be re-derived because
   nothing records *what was counted*. F.6 says it "re-measures at install and commits that count"
   — but a committed count without its predicate is not a baseline, it is a number. **F.6 should
   commit the predicate as executable code (the grep/AST rule the CI ratchet runs) and derive the
   count from it**, so the baseline and the gate cannot disagree. Until then the ratchet has no
   defensible starting point, which is the "blocks nothing or blocks everything" failure.

## 5. What I did not check

- **No schema was written, no migration authored, no production table touched** — the F.1 remit is
  classification and interface spec, and the Phase-0 documentation-only rule holds until the safety
  rails exist.
- **Target-side tables not in §9** (`jobs`, `rate_counters`, `command_dedup`, `session_tokens`,
  `oauth_states`, `service_tokens`, `daily_post_counts`, `channel_outbox`, `provider_operations`,
  `provider_quarantine`, `workspace_invitations`) are **not** classified here. They are target-only
  and have no legacy counterpart, so they are outside "classify all 14 legacy tables". Their classes
  are already stated in §7-DDL.
- **I did not verify the Class-1 enumeration against a real migration file** — none exists yet.
  §7-DDL says the replay fixture generates the policies from that list; that fixture is F.2's.
- **`chat_settings`'s column-level split was not re-derived.** §9 defers the full column mapping to
  `04` M.1 and §1; I read the disposition, not each column's destination.
- **No repository code was changed.** The interface spec is a specification; the extinction of
  `if chat_settings_id:` is not implemented, and I did not count its current instances.
- **The F.6 predicate is stated as four candidate variants, not resolved.** Choosing one is F.6's
  call and needs whoever set the original 75 — I could not recover it from the plan or the history.
