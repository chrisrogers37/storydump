# Execution sequence

The single consolidated sequence (G6): it supersedes `epic.md`'s phases, `implementation-plan.md`'s ordering, and #721's roadmap. Each increment names the #722 items it absorbs (**TT:P0-nn / P1-nn**, from `../2026-07-29-high-throughput-multi-tenant/tiered-issue-triage.md`) so the ratified gate rigor is traceable; where an absorbed item had a Redis half, that half is struck per C3 and the replacement named. Increments are PR-sized; an increment ships only when its **exit gate** passes.

## Ground rules (apply to every increment)

- Documentation-only until Phase 0's safety rails exist. No production migration runs before 0.2's runner ships.
- Every schema change rides the numbered-SQL runner with postconditions + replay-from-empty CI (0.2).
- New tables are born workspace-keyed, RLS-enabled, composite-FK'd. Only legacy tables ever need six-stage treatment.
- Rollback statement required per increment PR (what flips off, what data survives).
- **The six-stage migration machine (single normative statement — everything else cites this):** every legacy re-key runs **expand → backfill → dual-write → shadow-read → cutover → contract**, per table track.
  - *Backfill:* batched (initial batch 5,000 rows, config seam per `05` discipline) with a **stop rule**: any constraint violation, or per-batch error rate > 0.1%, halts the track, alerts, and blocks further batches until the cause is recorded in the track's log. Every backfill is rehearsed on a Neon branch before production.
  - *Dual-write:* a named single writer (the owning service) mirrors writes to both shapes; the track's PR names it explicitly.
  - *Shadow-read comparator:* nightly job compares the dual-written pair — row counts plus per-row field checksums over the mapped columns. Pass bar: **zero unexplained divergence for 14 consecutive days**; any divergence resets the clock after root-cause. (The 14-day window is a `05`-style initial value, revisable by the same rule.)
  - *Cutover:* reads flip behind a routing flag (C7 DB-row flag), reversible same-day.
  - *Contract:* the legacy surface is deleted — no shims, the team owns every caller.

## Phase 0 — External dependencies and safety rails (start immediately)

Parallelism: 0.1 ∥ 0.2 ∥ 0.4; **0.3 waits on 0.2** (its constraint ships as a runner migration).

**0.1 App Review submission (FC-4).** Submit Instagram-Login scopes (`instagram_business_basic`, `instagram_business_content_publish`) with screencasts. 2–4 weeks per permission of pure lead time — only Phase G's completion waits on it. *Gate:* submission accepted; tracking issue records scope status.

**0.2 Migration runner formalization (C6; absorbs TT:P0-01).** Numbered-SQL runner gains postconditions, checksums, replay-from-empty CI job, and parity tests (a fresh replayed DB detects a missing partial index / check constraint). *Gate:* CI replays all 49+ migrations from empty; a deliberately-broken parity fixture fails loudly; no production migration run.

**0.3 Production duplicate remediation (C10; absorbs TT:P0-03 prep; after 0.2).** Human-gated cleanup of the 6 known `posting_history` duplicate groups, then `UNIQUE(queue_item_id)` validated via the runner. *Gate:* constraint VALID in production; remediation log archived.

**0.4 Meta primary-doc verification.** Confirm against Meta's own documentation: 25/rolling-24h publish cap, 200/user/hr, Instagram-Login scope names and refresh semantics, usage-endpoint shape. The vault reference is corroborated but second-hand; `05`'s arithmetic hangs off these numbers. *Gate:* each figure confirmed-or-corrected in a doc commit; corrections propagate to `05` under its platform-input rule.

## Phase F — Foundations: workspace tier, expand tracks, ratchet

Parallelism: F.1 ∥ F.2; F.3/F.4/F.5 after F.2; F.6 independent after F.1.

**F.1 Ownership inventory and fail-closed interfaces (absorbs TT:P0-02).** Classify all 14 legacy tables global vs tenant-owned (`02` §9 is the answer key); every tenant-scoped repository method takes required leading `tenant_id`; the fail-open `if chat_settings_id:` pattern is extinct. *Gate:* fail-closed tests prove tenant access cannot run without context; cross-tenant coverage on every tenant-owned repo.

**F.2 Workspace tier — expand + backfill (six-stage stages 1–2 of the `chat_settings` track; W.5 completes it).** Create `workspaces`, `workspace_members`, `user_identities`, `channel_bindings` (`02` §1). Backfill: one workspace per existing `chat_settings` row (name from chat title; owner = the chat's owning user); each chat becomes a `telegram_group` binding; memberships copied; Telegram user ids into `user_identities`. *Gate:* the machine's backfill gates (batched, stop rule, Neon rehearsal) + spot invariants (every workspace has an owner member; every binding resolves).

**F.3 Neutral tenant resolution + dual-write (stage 3 of the same track).** One resolver: inbound (chat id | web session) → `workspace_id`; all service boundaries speak `tenant_id == workspaces.id` from here on. **Dual-write owner: the settings service** — every legacy `chat_settings`/membership mutation mirrors to the workspace tier from this increment until W.5's contract; the F.2 comparator watches the pair from here. *Gate:* no service-layer signature accepts a chat id (enforced as an F.6 ratchet rule, not a bespoke grep); adapter tests cover resolution; dual-write live.

**F.4 RLS harness + enablement on the new tier (C4/RF-R3; absorbs TT:P0-09).** Runtime-role harness (no owner role, no session affinity), then RLS on the Phase-F tables while tenant count is small. *Gate:* absent/wrong `app.tenant_id` cannot read or mutate as the exact runtime role; transaction reuse does not leak context; zero-NULL gates pass.

**F.5 Domain-table expand tracks (stages 1–2 for the three tables Phase L builds against).** `instagram_accounts → ig_accounts`, `api_tokens → oauth_credentials` (typed XOR FKs), `media_items` re-key + `media_posting_locks → post_locks` — expand + backfill only; legacy tables remain the read/write truth until their W tracks cut over; dual-write owners named per track at their stage-3 increments in W. *Gate:* machine backfill gates per track; Phase L's FKs have real targets.

**F.6 FC-2 ratchet install.** The shrink-only Telegram-reference allowlist lands in CI (baseline: the 75 measured modules), including the structural rules (core imports no Telegram; no service signature accepts a chat id). New adapter/sender modules enter the allowlist by deliberate PR review, never by default. *Gate:* ratchet proven by a red-test demo; baseline committed; Phase X later burns it down.

## Phase L — Ledger and execution machinery (workers before webhooks)

Parallelism: L.1→L.2 serial; L.3/L.4/L.6/L.7 independent after L.2; L.5 after L.3+L.4; L.8 deliberately last; L.9 after L.5+L.7+L.8.

**L.1 Intent ledger — create (absorbs TT:P0-04 vocabulary half).** `post_intents`, `audit_events`, `daily_post_counts` per `02` §3–4, FK'd against the F.5/F.2 tables. Transition function enforces the full matrix. *Gate:* model-based transition tests reject every illegal/double transition under concurrency; legacy queue untouched.

**L.2 Jobs + leases + fencing (absorbs TT:P0-04 rest, TT:P0-05).** `jobs` table, SKIP-LOCKED claim with lane + serialization-key + quarantine-check semantics, leases, heartbeats, lease-token fencing. *Gate:* one live owner per job under kill/resume tests; expired work recovers; a resumed stale owner cannot finalize.

**L.3 Provider operations rail (absorbs TT:P0-06).** `provider_operations` (ig, cloudinary) + adapter discipline: unique business key, persisted state machine, ambiguous-Meta-publish read-back reconciler job kind. *Gate:* kill/drop tests at every Meta boundary issue at most one publish call.

**L.4 Channel outbox (absorbs TT:P0-07, Redis half struck).** `channel_outbox` + Telegram sender jobs with per-chat/global pacing (absorbing the AIORateLimiter budgets into durable rows); outbox rows are the single send-state authority (`02` §6); no-blind-retry on ambiguous; supersede-then-send for edits. Redis wake-up replaced by indexed `run_at` polling at the `05` cadence. *Gate:* stopped-sender and lost-ack injections strand nothing; no duplicate sends under replay.

**L.5 New publish pipeline on the ledger (FC-3; after L.3+L.4).** The checkpointed pipeline (`02` `publish_step`) with cap-gated flip (R2), container-id-before-call (R1), FC-3.1–3.6 transit handling, Meta usage-endpoint advisory pre-check + error-9 deferral (`02` §8). Runs in shadow (no live dispatch) until L.9. *Gate:* each FC-3 requirement has a passing test; pipeline resumes correctly from every checkpoint kill.

**L.6 Instagram Login OAuth (FC-4).** New-connection flow end-to-end via Instagram Login in the ingress adapter; `oauth_credentials(provider='ig_login')`; refresh via `graph.instagram.com`. The FB-vintage path is structurally closed to new rows here: `CHECK (provider <> 'fb_login_legacy')` added `NOT VALID` (existing rows tolerated, new rows impossible — `02` §2). *Gate:* a fresh Professional account connects with zero Facebook surface; refresh proven on a real token; an attempted legacy-row insert fails at the DB.

**L.7 Scheduler-as-clock + dispatcher build (C5).** Advisory-lock-elected clock; `next_slot_at`/`next_sync_at`/`next_refresh_at` maintenance; the dispatcher (due-scan → idempotent job inserts; slot key 1 makes double-insert impossible) — the build item #722 never scheduled. *Gate:* clock tick is O(due) by EXPLAIN; killing the clock mid-tick loses nothing; the slot-storm scenario (all workspaces due at once) inserts within bounds — authored as the first versioned scenario of the S.1 harness, not a throwaway test.

**L.8 Webhook ingress + idempotent admission cutover (absorbs TT:P0-08; includes TT:P1-03).** Telegram webhook adapter (secret token, replay-safe, explicit callback ack), `drop_pending_updates=True` removed and its absence tested; Railway header/proxy-trust behavior verified in the real deployment before cutover (the SE proof obligation); polling retired. Deliberately the last machinery increment (workers-before-webhooks, IP order). *Gate:* 200 replayed callbacks yield one command; ack SLO measured under slow-worker injection; rollback lever = webhook delete + polling restart, rehearsed.

**L.9 Legacy-path cutover to the ledger (absorbs TT:P0-10 shape).** Dual-write shadow (legacy queue + ledger) → 1:1 comparator → live dispatch flips to the ledger + new pipeline; legacy queue/history become read-only, then W.4 backfills them in. *Gate:* comparator parity over the observation window; zero fake-adapter calls; enabling live dispatch cannot lease historical shadow jobs.

## Phase W — Re-key completion + consumer contracts (tracks parallelizable after L.9)

**W.1–W.4 Track completion (stages 3–6 for the F.5 tracks; 4–6 where dual-write began earlier).** (1) `ig_accounts`, (2) `oauth_credentials`, (3) `media_items` + `post_locks`, (4) `posting_history` → terminal intents + `audit_log`/`user_interactions` → `audit_events`. Each: dual-write owner named in the PR, comparator window, flag cutover, contract deletes the legacy surface. *Gate per track:* the machine's stage gates.

**W.5 `chat_settings` track completion (stages 4–6; closes what F.2/F.3 opened).** Comparator already running since F.3 → cutover reads to `workspaces`/`channel_bindings`/`workspace_members` → contract: legacy `chat_settings`, `user_chat_memberships` deleted; `category_post_case_mix` and `onboarding_sessions` re-keys ride this track. *Gate:* machine stage gates; post-contract, no code references the legacy names (ratchet-style grep in CI).

**W.6 Consumer contracts (from #721, restated in full).**
- *JWT:* tokens gain an **additive** `workspace_id` claim; both shapes accepted during the window; verifier rejects claim-less tokens only after cutover. *Gate:* contract tests on both shapes.
- *BFF:* the BFF and API deploy their workspace-aware versions in **one lockstep window** — no cross-version skew beyond it; rollback = redeploy both prior versions together. *Gate:* version-skew test matrix passes on both sides of the window.
- *Card payload stability:* `post_intents` carries a `legacy_queue_item_id` mapping column so in-flight Telegram card callbacks (which embed queue-item ids) resolve to intents throughout the transition; column dropped at W.5 contract + card-payload TTL expiry. *Gate:* a pre-cutover card answered post-cutover resolves correctly.
- *CLI:* the CLI routes through the API service (service-routing), never direct DB, so re-keys are invisible to it. *Gate:* CLI integration suite green against the API with legacy tables gone.

**W.7 RLS completion (C4).** RLS enabled on every re-keyed table as its track contracts; final zero-NULL + VALIDATE pass fleet-wide (absorbs TT:P1-10). *Gate:* P0-09 harness green across all tenant-scoped tables in production topology.

## Phase X — Interaction-layer burn-down (FC-2; starts alongside Phase W)

**X.1 Core-empty milestone.** Using the F.6 ratchet: the core-services segment of the Telegram allowlist burns to empty as handlers route through the command vocabulary; allowlist shrinks with each migrating PR. *Gate:* allowlist ≤ adapter+sender modules only; core segment empty.

**X.2 Web surface parity.** Mini-App/API reads pending approvals from the ledger and issues the same commands (pull model). *Gate:* an approval completed web-only (no Telegram binding on the workspace) passes end-to-end — the FC-1 "web-only workspace is legal" proof.

**X.3 Multi-workspace UX enablement.** Sign-up without Telegram (`user_identities` `email_otp`), workspace create/switch, account moves between workspaces (audited). *Gate:* the FC-1 end-state walkthrough — one user, two workspaces (4 + 3 accounts), one Telegram identity managing both — passes as an integration test.

## Phase G — Facebook-path sunset (FC-4)

**Starts when 0.1's scopes are approved and L.6 is live — independent of Phases W/X**; the campaign is elapsed-time-bound, so starting it early is free calendar.

**G.1 Re-auth campaign.** Existing FB-vintage accounts prompted (via their bindings) to reconnect over Instagram Login; dual-path refresh keeps them alive meanwhile; dashboard counts remaining `fb_login_legacy` credentials. *Gate:* campaign running; count monotonically decreasing; no forced cutoff.

**G.2 Sunset.** At zero active `fb_login_legacy` credentials: VALIDATE the L.6 CHECK, then delete the legacy refresh path, `/me/accounts` call, the CLI FB flow, and the enum value (no shims). `graph.facebook.com` joins the F.6 ratchet's forbidden list at zero — the ban is structural forever, not a one-shot grep. *Gate:* ratchet holds `graph.facebook.com` at zero outside historical docs; suite green.

## Phase S — Scale-out proof (absorbs the surviving P1 architecture items)

**S.1 Load harness + telemetry (absorbs TT:P1-01).** The versioned harness (200-click / 250-due-tenant scenarios, seeded by L.7's slot-storm scenario) plus per-lane depth, oldest-runnable-age, per-workspace last-success, quarantine gauge, parked-intent alarm. *Gate:* harness runs in CI-adjacent env; `05`'s revision rule exercised once (numbers revised from measurement, or confirmed).

**S.2 Admission + fairness, pg-shaped (absorbs TT:P1-02/P1-04, Redis struck; proves RF-R2/RF-R5).** pg fixed-window admission counter replacing SlowAPI `memory://` (fail-closed); lane capacity reservation + per-key serialization demonstrated as the fairness mechanism (no WFQ, no fairness scaffolding on uncontended paths). *Gate:* multi-replica admission holds under the harness; one workspace's 5k-file sync cannot delay another's tap past SLO.

**S.3 Egress + budgets (absorbs TT:P1-05/P1-06/P1-09).** Async unit-of-work + connection budget (replica pools sum under the Neon ceiling per `05`); shared timeout/retry-budget/SSRF-safe egress substrate; deployment-wide provider budgets — adding replicas does not raise any global provider budget. *Gate:* TT exit criteria verbatim.

**S.4 Worker extraction completion (absorbs TT:P1-07/P1-08).** Command and sync workers fully on the jobs machinery; scheduler startup no longer depends on a sequential full sync; bounded streaming sync with checkpoints; **`service_runs` retired** (the last loop's bookkeeping moves to jobs + `audit_events` — `03` kept-table decision). *Gate:* first-boot with a cold cache schedules within SLO; 5k-file library syncs chunked with visible progress; `service_runs` dropped.

**S.5 Canary + reconciliation at scale (absorbs TT:P1-12 and P1-11; honors the TT P1-tier note that narrowly allowlisted canaries may validate earlier steps before P1 completes).** Narrow allowlisted publish canary through the full new path in production, then broad enablement after W.7 (the P1-10 precondition #722 stated). *Gate:* canary weeks clean (zero duplicate effects, reconciler closes every ambiguity); rolling-deploy + resilience gates green.

## Superseded / struck (authoritative list — `03` G6 defers here)

- `epic.md` phase plan and `implementation-plan.md` ordering as standalone sequences (this file replaces them; absorbed items retain their TT gates).
- `epic.md` Phase-5 webhook placement → L.8 (IP order wins).
- Dedicated REC reconciler role → maintenance workers own reconciliation.
- All Redis increments (11–12 and the Redis halves of P0-07/P1-02/P1-04) → `05` §annex, measured-trigger only.
- #721's roadmap, its Alembic migration track (C6, contested), and its outbox-executes pipeline (D2).
- Both packages' deferral of operational numbers → closed by `05`.
