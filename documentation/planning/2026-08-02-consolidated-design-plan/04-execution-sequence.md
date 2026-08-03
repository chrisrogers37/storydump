# Execution sequence

The single consolidated sequence (G6): it supersedes `epic.md`'s phases, `implementation-plan.md`'s ordering, and #721's roadmap. Each increment names the #722 items it absorbs (**TT:P0-nn / P1-nn**, from `../2026-07-29-high-throughput-multi-tenant/tiered-issue-triage.md`) so the ratified gate rigor is traceable; where an absorbed item had a Redis half, that half is struck per C3 and the replacement named. Increments are PR-sized; an increment ships only when its **exit gate** passes.

## Ground rules (apply to every increment)

- Documentation-only until Phase 0's safety rails exist. No production migration runs before 0.2's runner ships.
- Every schema change rides the numbered-SQL runner with postconditions + replay-from-empty CI (0.2).
- New tables are born workspace-keyed, RLS-enabled, composite-FK'd. Only legacy tables ever need six-stage treatment.
- Rollback statement required per increment PR (what flips off, what data survives).
- **The six-stage migration machine (single normative statement — everything else cites this):** every legacy re-key runs **expand → backfill → dual-write → shadow-read → cutover → contract**, per table track.
  - *Backfill:* batched (initial batch 5,000 rows, config seam per `05` discipline) with a **stop rule**: any constraint violation, or per-batch error rate > 0.1%, halts the track, alerts, and blocks further batches until the cause is recorded in the track's log. Every backfill is rehearsed on a Neon branch before production. Naive legacy timestamps convert with `AT TIME ZONE 'UTC'` (`02` §0) — a bare cast fails review.
  - *Dual-write:* a named single writer (the owning service) mirrors writes to both shapes **in the same database transaction** — both shapes live in one Postgres, so dual-write here has no distributed-failure mode, no ordering problem, and no partial-failure matrix: the mirror commits or the whole write rolls back. The track's PR names the owner; this plan already names them per track below.
  - *Shadow-read comparator:* nightly `comparator_run` job compares the dual-written pair — row counts plus per-row field checksums **over the track's canonical mapping** (each track's spec defines the mapped-column list and, for non-bijective tracks, the mapping rule the checksum runs over — counts alone cannot validate splits/merges; W.4's is stated inline below). Pass bar: **zero unexplained divergence for 14 consecutive days**; any divergence resets the clock after root-cause.
  - *Cutover:* reads flip behind a routing flag (C7 DB-row flag; the flag table and its transactional evaluation are part of 0.2's runner PR — flags are read in the same transaction as the routed operation, no cache, so visibility is immediate and atomic per operation).
  - *Contract:* the legacy surface is deleted — no shims, the team owns every caller. **Before any contract-stage DROP:** a `pg_dump --table` snapshot of the legacy table to the `05` archive location, retained 90 days; the increment's rollback statement says what restoring it means (data written after contract is not in the dump — rollback beyond the window is a declared non-goal).
- **Version-skew rule (N-1):** all JSONB payloads (`jobs.payload`, `channel_outbox.payload`, settings, evidence) carry `v`; every reader accepts `v` and `v-1`; a deploy may therefore roll worker and ingress replicas in any order within one version window. Payload shape changes ship reader-first.

## Phase 0 — External dependencies and safety rails (start immediately)

Parallelism: 0.1 ∥ 0.2 ∥ 0.4; **0.3 waits on 0.2** (its constraint ships as a runner migration).

**0.1 App Review submission (FC-4).** Submit Instagram-Login scopes (`instagram_business_basic`, `instagram_business_content_publish`) with screencasts. 2–4 weeks per permission of pure lead time — only Phase G's completion waits on it. *Gate:* submission accepted; tracking issue records scope status.

**0.2 Migration runner formalization (C6; absorbs TT:P0-01).** The numbered-SQL runner becomes a real program with a real contract:
- **Ledger:** `schema_migrations(version INT PK, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), applied_by TEXT NOT NULL, execution_ms INT, status TEXT CHECK (status IN ('applied','repaired')))` — supersedes `schema_version` (which stays read-only until W-phase contract). Checksum = SHA256 of the file; an edited applied file is a **hard failure** (fix-forward with a new migration, never edit history); `runner repair --version N --reason …` records a deliberate exception as `status='repaired'`.
- **Locking:** the runner takes `pg_advisory_lock(<fixed key>)` for the whole run — concurrent deploys serialize; second runner finds versions applied and no-ops. This is the deploy-locking answer (review A §5.16): Railway predeploy runs the runner; two services deploying concurrently cannot interleave DDL.
- **Transactions:** one migration = one transaction, except files carrying the `-- runner:no-transaction` marker (required for `CREATE INDEX CONCURRENTLY`); such files must be idempotent (`IF NOT EXISTS` discipline) since a mid-file crash leaves them partially applied.
- **Postconditions:** each migration may declare `-- runner:postcondition <SQL returning bool>` lines, executed after apply; false = failed migration.
- **Chain reconciliation (fix-forward for the known defects):** migration 050 drops the 004/008 orphaned unique if present (`ALTER TABLE api_tokens DROP CONSTRAINT IF EXISTS api_tokens_service_name_token_type_key` — the *actual* auto-generated name), stamps the missing 010/034 ledger rows, and normalizes `chat_settings.caption_style` to TEXT — after which replay-from-empty equals production. **Precondition, before writing 050:** `\d api_tokens` and `\d media_posting_locks` against production to confirm the file-derived residue analysis.
- *Gate:* CI replays all migrations from empty; a deliberately-broken parity fixture fails loudly; replayed-schema == models-schema parity test green (catches DB-only indexes/defaults drifting); no production migration run.

**0.3 Production duplicate remediation (C10; absorbs TT:P0-03 prep; after 0.2).** Human-gated cleanup of the 6 known `posting_history` duplicate groups, then `UNIQUE(queue_item_id)` validated via the runner. *Gate:* constraint VALID in production; remediation log archived.

**0.4 Meta primary-doc verification.** Confirm against Meta's own documentation: 25/rolling-24h publish cap, 200/user/hr, Instagram-Login scope names and refresh semantics, usage-endpoint shape, **container `status_code` vocabulary (PUBLISHED/ERROR/EXPIRED/IN_PROGRESS/FINISHED) and stories-list lookback validity** — the `02` §6 reconciliation contract hangs off the last two. *Gate:* each figure confirmed-or-corrected in a doc commit; corrections propagate to `05`/`02` §6 under the platform-input rule.

## Phase F — Foundations: workspace tier, expand tracks, ratchet

Parallelism: F.1 ∥ F.2; F.3/F.4/F.5 after F.2; F.6 independent after F.1.

**F.1 Ownership inventory and fail-closed interfaces (absorbs TT:P0-02).** Classify all 14 legacy tables global vs tenant-owned (`02` §9 is the answer key); every tenant-scoped repository method takes required leading `tenant_id`; the fail-open `if chat_settings_id:` pattern is extinct. *Gate:* fail-closed tests prove tenant access cannot run without context; cross-tenant coverage on every tenant-owned repo.

**F.2 Workspace tier — expand + backfill (six-stage stages 1–2 of the `chat_settings` track; W.5 completes it).** Create `workspaces`, `workspace_members`, `user_identities`, `channel_bindings`, `workspace_invitations` (`02` §1) with their triggers. Backfill: one workspace per existing `chat_settings` row (name from `display_name`, else chat title); each chat becomes a `telegram_group` binding; memberships copied (instance_role maps 1:1); Telegram user ids into `user_identities`. **Owner derivation (deterministic):** the `user_chat_memberships` row with `instance_role='owner'` (earliest `joined_at` if several); else earliest `admin`; else earliest active membership; else — zero resolvable members — the chat goes to the track's quarantine list for manual assignment. *Gate:* machine backfill gates + spot invariants (every workspace has exactly one owner member — `uq_members_one_owner` proves it; every binding resolves; **quarantine list empty or manually resolved**).

**F.3 Neutral tenant resolution + dual-write (stage 3 of the same track).** One resolver: inbound (chat id | web session) → `workspace_id`; all service boundaries speak `tenant_id == workspaces.id` from here on. **Two dual-write owners, named now** (the aggregates have different owning services today — one owner per aggregate, review A §3.23): the **settings service** mirrors every `chat_settings` mutation to `workspaces`/`channel_bindings`; the **membership service** mirrors every `user_chat_memberships` mutation to `workspace_members`. Both mirrors are same-transaction (ground rule). *Gate:* no service-layer signature accepts a chat id (F.6 ratchet rule); adapter tests cover resolution; both dual-writes live; comparator running on both pairs.

**F.4 RLS harness + enablement on the new tier (C4/RF-R3; absorbs TT:P0-09).** Runtime-role + **system-role** harness per `02` §7 (no owner role, no `BYPASSRLS`, no session affinity; system roles exercise their enumerated `USING (true)` policies), then RLS on the Phase-F tables while tenant count is small. *Gate:* absent/wrong `app.tenant_id` cannot read or mutate as the exact runtime role; a system role reads only its enumerated tables; transaction reuse does not leak context; zero-NULL gates pass.

**F.5 Domain-table expand tracks (stages 1–2 for the tables Phase L builds against — order inside this increment is load-bearing, review A §3.22):** (1) `media_sources` created and backfilled first (one row per chat with Drive config: `media_source_type/root` + `gdrive_alerted_at`); (2) `instagram_accounts → ig_accounts` fan-out (one row per (workspace, account) pair derived from `api_tokens` ownership per `02` §9); (3) `api_tokens → oauth_credentials` (typed XOR FKs — its `media_source_id` targets now exist); (4) `media_items` re-key + `media_posting_locks → post_locks`. Expand + backfill only; legacy tables remain the read/write truth until their W tracks cut over. *Gate:* machine backfill gates per track; Phase L's FKs have real targets; credential backfill has zero XOR violations.

**F.6 FC-2 ratchet install.** The shrink-only Telegram-reference allowlist lands in CI (baseline: the 75 measured modules), including the structural rules (core imports no Telegram; no service signature accepts a chat id) and the `07` §5 hygiene pattern list (`provider_account_ref` out of logging call sites). *Gate:* ratchet proven by a red-test demo; baseline committed; Phase X later burns it down.

## Phase L — Ledger and execution machinery (workers before webhooks)

Parallelism: **L.0 first, serial**; then L.1→L.2 serial; L.3/L.4/L.6/L.7 independent after L.2; L.5 after L.3+L.4; L.8 deliberately last; L.9 after L.5+L.7+L.8.

**L.0 Transaction substrate (new in pass 2 — review A §3.21 / B§5: decided before L.1, not at S.3).** The **final** async unit-of-work: async engine + pooling (budgets from `05`), the UoW factory with required `tenant_id` and automatic `SET LOCAL app.tenant_id` + role selection (`02` §7), actor GUC helpers, transaction-per-checkpoint discipline (a transaction never spans a provider call — `02` §5). Phase L machinery is all new code, so it is born on the final substrate; legacy sync paths keep their existing session until their own cutovers, and S.3 retains only egress hardening. *Gate:* harness proves tenant-scoping + role selection + GUC hygiene under pool reuse; a UoW without tenant context is unconstructible; a transaction spanning a stubbed provider call fails the discipline test.

**L.1 Intent ledger — create (absorbs TT:P0-04 vocabulary half).** `post_intents`, `post_intent_transitions` (seeded with the `02` §4 matrix), `audit_events`, `daily_post_counts`, and the three triggers (guard, audit-with-required-actor, insert guard) exactly as in `02` §4; audit grants per `02` §7. *Gate:* model-based transition tests reject every illegal/double transition **via raw SQL as well as the service** (the trigger, not the service, is the authority); terminal rows reject every UPDATE; an actor-less state change raises; legacy queue untouched.

**L.2 Jobs + leases + fencing (absorbs TT:P0-04 rest, TT:P0-05).** `jobs` per `02` §5: kind registry CHECK, claim query + `uq_jobs_serialized_lease`, leases, heartbeat task, lease-token CAS finalization. *Gate:* one live owner per job under kill/resume tests; expired work recovers; a resumed stale owner cannot finalize (CAS proves it); two claimers on one serialization key — one wins by unique index, the loser retries clean.

**L.3 Provider-operations rail (absorbs TT:P0-06).** `provider_operations` (**ig only** — Cloudinary is off the rail, `02` §6) + the permit protocol verbatim: permit-insert + lease CAS in one transaction, call only after commit, resumed pipelines never re-call an unresolved permit. *Gate:* kill/drop tests at every Meta boundary issue at most one publish call — including the kill-between-permit-and-call case, which must land in `publishing_ambiguous` with zero retries.

**L.4 Channel outbox (absorbs TT:P0-07, Redis half struck).** `channel_outbox` + Telegram sender jobs with per-chat/global pacing (AIORateLimiter budgets into durable rows); outbox rows are the single send-state authority; the `02` §6 per-kind ambiguity policy (notification retry-once; approval-prompt resend + supersede-all; edits always supersede-then-send). Redis wake-up replaced by indexed `run_at` polling at the `05` cadence. *Gate:* stopped-sender and lost-ack injections strand nothing; duplicate sends bounded exactly as the policy states (≤1 extra notification; prompts converge on supersede).

**L.5 New publish pipeline on the ledger (FC-3; after L.3+L.4).** The checkpointed pipeline (`02` `publish_step`) with the §4 flip transaction (atomic cap debit + key-4 acquisition), container-id-before-call (R1), FC-3.1–3.6 transit handling, **lazy inline usage pre-check** (`02` §8 — in-process cache on `provider_account_ref`; no background refresh exists), error-9 deferral. Runs in shadow (no live dispatch) until L.9. *Gate:* each FC-3 requirement has a passing test; pipeline resumes correctly from every checkpoint kill; cap debit/refund proven under concurrency (R2 test: N concurrent approvals, cap-many succeed, rest deferred, zero over-cap).

**L.6 Instagram Login OAuth (FC-4).** New-connection flow end-to-end via `oauth_states` (`07` §2) in the ingress adapter; `oauth_credentials(provider='ig_login')` under the MultiFernet ring (`07` §3); refresh via `graph.instagram.com`; the `ck_no_new_fb_legacy` CHECK added NOT VALID (`02` §2). *Gate:* a fresh Professional account connects with zero Facebook surface; refresh proven on a real token; an attempted legacy-row insert fails at the DB; state replay/expiry/cross-workspace rejection tests green.

**L.7 Scheduler-as-clock + dispatcher build (C5).** Advisory-lock-elected clock; **per-account** `next_slot_at` maintenance (+ `next_sync_at`/`next_refresh_at`); the dispatcher (due-scan over `ix_ig_accounts_due` → idempotent intent/job inserts; slot key 1 makes double-insert impossible) running as `svc_clock` (`02` §7). *Gate:* clock tick is O(due) by EXPLAIN; killing the clock mid-tick loses nothing; the slot-storm scenario (all accounts due at once) inserts within bounds — authored as the first versioned scenario of the S.1 harness.

**L.8 Webhook ingress + idempotent admission cutover (absorbs TT:P0-08; includes TT:P1-03).** Telegram webhook adapter (secret token, replay-safe, explicit callback ack), `drop_pending_updates=True` removed and its absence tested; Railway header/proxy-trust behavior verified in the real deployment before cutover; polling retired. *Gate:* 200 replayed callbacks yield one command; ack SLO measured under slow-worker injection; rollback lever = webhook delete + polling restart, rehearsed.

**L.9 Legacy-path cutover to the ledger (absorbs TT:P0-10 shape).** The shadow spec, precisely (review A §3.25 / B's "two meanings of shadow" — this plan has exactly one meaning: **shadow = mirrored intent rows, zero jobs**):
- *Shadow phase:* the legacy queue remains authority. Every legacy queue insert also creates a ledger intent **in the same transaction** (correlation key: `legacy_queue_item_id` = the queue row's id; the intent id is new). Every legacy state change mirrors as the corresponding intent transition (mapping table in `02` §9). **No jobs are created for shadow intents** — nothing is leaseable, the flag keeps dispatch off, and enabling live dispatch cannot lease them because they carry no jobs at all.
- *Comparator:* nightly, over the correlation key — queue+history state vs intent state via the §9 mapping; counts per state-class + field checksums (scheduled_for/slot, container id, message refs).
- *Cutover (flag flip, per workspace cohort):* new work is created as live intents with jobs (the clock takes over slot planning for cohorted workspaces); legacy dispatch stops creating queue rows for them; **in-flight legacy items drain to terminal on the legacy path** (bounded by the queue's own lifecycle — days, not weeks); mirror direction reverses: the ledger now writes terminal states back to `posting_history` (same transaction) so the legacy read surface stays truthful until W.4 contracts it.
- *Gate:* comparator parity over the observation window; zero fake-adapter calls; cutover rehearsed on a Neon branch including the drain; a pre-cutover card tapped post-cutover resolves via `legacy_queue_item_id` (W.6's column, populated since shadow start).

## Phase W — Re-key completion + consumer contracts (tracks parallelizable after L.9)

**W.1–W.4 Track completion (stages 3–6 for the F.5 tracks; 4–6 where dual-write began earlier). Dual-write owners, named now (review A §3.24):**
- **W.1 `ig_accounts`** — owner: the **accounts service** (today's instagram_account_service surface). Legacy `instagram_accounts`+selection mutations mirror to the fan-out rows.
- **W.2 `oauth_credentials`** — owner: the **credentials/token service** (today's token_repository writers). Token issue/refresh/revoke mirrors to the typed rows.
- **W.3 `media_items` + `post_locks`** — owner: the **media service** (indexing + lock writers). **Extra stage-4 gate: per-workspace content-hash dedup remediation** (existing `dedup-media` tooling, human-gated like 0.3) — `uq_media_dedup` cannot land until the duplicate count is zero; the comparator's mapping excludes the deduped losers by recorded id list.
- **W.4 `posting_history` → terminal intents + `audit_log`/`user_interactions` → `audit_events`** — owner: the **posting service** (history writer) for the mirror; the backfill of pre-ledger history runs under `app.migration_mode` (`02` §4 insert guard). Canonical mapping for the comparator: history row ↔ terminal intent via `legacy_queue_item_id` (post-L.9 rows) or (media_item_id, posted_at±tolerance) for pre-ledger rows — the non-bijective tolerance rule is stated in the track PR and versioned with the comparator.
*Gate per track:* the machine's stage gates.

**W.5 `chat_settings` track completion (stages 4–6; closes what F.2/F.3 opened).** Comparator running since F.3 → cutover reads to `workspaces`/`channel_bindings`/`workspace_members` (per-account `next_slot_at` seeded from `last_post_sent_at` + window params at this flip) → contract: `chat_settings`, `user_chat_memberships` deleted; `category_post_case_mix` re-key and `onboarding_sessions` re-key ride this track. *Gate:* machine stage gates; post-contract, no code references the legacy names (ratchet-style grep in CI).

**W.6 Consumer contracts (from #721, restated in full).**
- *JWT:* tokens gain an **additive** `workspace_id` claim; both shapes accepted during the window; verifier rejects claim-less tokens only after cutover. *Gate:* contract tests on both shapes.
- *BFF:* BFF and API deploy workspace-aware versions in **one lockstep window**; rollback = redeploy both prior versions together. *Gate:* version-skew matrix passes on both sides of the window.
- *Card payload stability:* `legacy_queue_item_id` (populated since L.9 shadow start) resolves in-flight Telegram cards. **Drop condition (mechanical):** no non-terminal intent carries the column AND 30 days (card TTL, `05`) have passed since W.5 contract — then the column drops. *Gate:* a pre-cutover card answered post-cutover resolves correctly.
- *CLI:* routes through the API service with `service_tokens` auth (`07` §6), never direct DB. *Gate:* CLI integration suite green against the API with legacy tables gone.

**W.7 RLS completion (C4).** RLS enabled on every re-keyed table as its track contracts; final zero-NULL + staged-NOT-NULL passes (`02` §7 procedure) fleet-wide (absorbs TT:P1-10). *Gate:* P0-09 harness green across all tenant-scoped tables in production topology.

## Phase X — Interaction-layer burn-down (FC-2; starts alongside Phase W)

**X.1 Core-empty milestone.** F.6 ratchet: core-services segment of the Telegram allowlist burns to empty; allowlist shrinks with each migrating PR. *Gate:* allowlist ≤ adapter+sender modules only; core segment empty.

**X.2 Web surface parity.** Mini-App/API reads pending approvals from the ledger and issues the same commands (pull model), including the `review_required` operator surface with its evidence trail (`06` §5). *Gate:* an approval completed web-only (no Telegram binding on the workspace) passes end-to-end — the FC-1 "web-only workspace is legal" proof.

**X.3 Multi-workspace UX enablement.** Sign-up without Telegram (`07` §1 OTP + sessions), invitations (`06` §2), workspace create/switch, account movement (`06` §4, audited), offboarding command (`06` §1). *Gate:* the FC-1 end-state walkthrough — one user, two workspaces (4 + 3 accounts), one Telegram identity managing both — passes as an integration test.

## Phase G — Facebook-path sunset (FC-4)

**Starts when 0.1's scopes are approved and L.6 is live — independent of Phases W/X.**

**G.1 Re-auth campaign.** FB-vintage accounts prompted via their bindings to reconnect over Instagram Login (`reauth_prompt` jobs at the `05` campaign cadence); dual-path refresh keeps them alive meanwhile; dashboard counts remaining `fb_login_legacy` credentials. *Gate:* campaign running; count monotonically decreasing; no forced cutoff.

**G.2 Sunset.** At zero active `fb_login_legacy` credentials: VALIDATE the L.6 CHECK, then delete the legacy refresh path, `/me/accounts` call, the CLI FB flow, and the CHECK-list value (no shims). `graph.facebook.com` joins the F.6 ratchet's forbidden list at zero. *Gate:* ratchet holds `graph.facebook.com` at zero outside historical docs; suite green.

## Phase S — Scale-out proof (absorbs the surviving P1 architecture items)

**S.1 Load harness + telemetry (absorbs TT:P1-01).** The versioned harness (200-click / 250-due-account scenarios, seeded by L.7's slot-storm scenario) plus per-lane depth, oldest-runnable-age, per-workspace last-success, quarantine gauge, parked-intent alarm. *Gate:* harness runs in CI-adjacent env; `05`'s revision rule exercised once.

**S.2 Admission, pg-shaped (absorbs TT:P1-02/P1-04, Redis struck; proves RF-R2/RF-R5 as *absence*).** pg fixed-window **per-workspace** admission replacing SlowAPI `memory://` (fail-closed). There is **no global command ceiling** (pass-2 cut, review A §4.1/B: the platform has no app-wide API budget to protect; the global guard is pool bounds + backpressure visibility). S.2's fairness content is a **demonstration, not machinery**: lanes + reservations + per-key serialization suffice — the gate shows it, and nothing beyond them exists to build. *Gate:* multi-replica per-workspace admission holds under the harness; one workspace's 5k-file sync cannot delay another's tap past SLO.

**S.3 Egress hardening (absorbs TT:P1-05/P1-06/P1-09; UoW itself moved to L.0).** Shared timeout/retry-budget/SSRF-safe egress substrate; deployment-wide provider budgets — adding replicas does not raise any global provider budget; connection budgets re-verified against the actual Neon plan. *Gate:* TT exit criteria verbatim.

**S.4 Worker extraction completion (absorbs TT:P1-07/P1-08).** Command and sync workers fully on the jobs machinery; scheduler startup no longer depends on a sequential full sync; bounded streaming sync with checkpoints; **retention sweep live** (`05` retention table + audit archive export, `07` §4); **`service_runs` retired**. *Gate:* first-boot with a cold cache schedules within SLO; 5k-file library syncs chunked with visible progress; retention sweep proven on aged fixtures; `service_runs` dropped.

**S.5 Canary + reconciliation at scale (absorbs TT:P1-12 and P1-11).** Narrow allowlisted publish canary through the full new path in production, then broad enablement after W.7. *Gate:* canary weeks clean (zero duplicate effects; reconciler closes every ambiguity within its evidence budget); rolling-deploy + resilience gates green.

## Deployment and rollout mechanics (review A §3.38)

- **Railway services (3):** `api` (uvicorn, ingress role), `worker` (new entrypoint `python -m src.worker`, N replicas — the clock elects itself among them via advisory lock), `landing` (unchanged). The legacy `python -m src.main` process retires at S.4's completion; until then it coexists as the legacy path exactly as the cutover flags dictate.
- **Migrate-before-code:** the runner executes as each service's predeploy step (advisory lock serializes the race — 0.2); expand/contract discipline means code N and N-1 both run against every mid-track schema.
- **Health:** `api` exposes `/healthz` (readiness = DB reachable); workers expose liveness via heartbeat freshness (a worker whose leases go stale is dead — Railway restarts on process exit, and the lease machinery makes a zombie harmless anyway).
- **Rolling order within a release:** runner → workers → api (payloads are reader-first N-1 safe, so strict order is a convention, not a correctness requirement).
- **Scaling limits:** worker replica count is bounded by the `05` connection budget inequality — raising replicas without re-running the `05` arithmetic is a review-blocking defect (the seam is config, the invariant is the inequality).

## Superseded / struck (authoritative list — `03` G6 defers here)

- `epic.md` phase plan and `implementation-plan.md` ordering as standalone sequences (this file replaces them; absorbed items retain their TT gates).
- `epic.md` Phase-5 webhook placement → L.8 (IP order wins).
- Dedicated REC reconciler role → maintenance workers own reconciliation.
- All Redis increments (11–12 and the Redis halves of P0-07/P1-02/P1-04) → `05` §annex, measured-trigger only.
- #721's roadmap, its Alembic migration track (C6, contested), and its outbox-executes pipeline (D2).
- Both packages' deferral of operational numbers → closed by `05`.
- **Pass-2 strikes of pass-1 content:** `workspaces.category_mix` JSONB (the SCD table survives instead — `02` §9) · Cloudinary on the provider-operations rail (`02` §6) · the global 50/s admission ceiling (S.2) · any eager/background Meta usage pre-check (`02` §8) · the phrase "`NOT NULL` added `NOT VALID`" (impossible in PostgreSQL — replaced by the `02` §7 staged procedure) · S.3 as the async-UoW home (→ L.0).
