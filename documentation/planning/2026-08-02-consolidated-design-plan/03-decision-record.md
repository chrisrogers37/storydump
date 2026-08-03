# Decision record

Every contested point between #721, #722 (as amended by #730), the cold design, and the 2026-08-02 rulings — what was decided, why, and what it supersedes. Implementers do not relitigate these; a reviewer who finds new *evidence* (not a re-argument) escalates.

Citation conventions: **EP/IP/SE/TT** = `../2026-07-29-high-throughput-multi-tenant/{epic,implementation-plan,self-evaluation,tiered-issue-triage}.md`; **RF-Rn / RF-Gn** = that package's `review-findings.md` (#730: required changes R1–R5, gaps G1–G2); **FC** = `00-fixed-constraints.md`; bare **Rn/Tn/Hn** = `01` §Requirements ledger. Decision ids (**Cn/Dn/Gn**) preserve the fleet-side adjudication numbering — they are stable identifiers, not sequential (absent numbers were absorbed into other items before this plan; nothing is missing).

## #730 traceability (every RF item's consolidated home)

| RF item | Where it lands here |
|---|---|
| RF-R1 bound the ready-job reconciler's aggregate emission | Bounded sweeps everywhere (H5): reconciler + reapers run LIMITed scans at the `05` cadences; no unbounded aggregate emission path exists in `04` L.2/L.4 |
| RF-R2 choose a scale-free fair-dispatch algorithm before building one | Chosen pre-build: lanes + reserved capacity + per-key serialization, **no WFQ** (C5 here, proven at `04` S.2) |
| RF-R3 move RLS enforcement early | C4 — `04` F.4 enforces while tenant count is small |
| RF-R4 justify Redis against a real number, or cut | C3 — cut from core; measured-trigger annex (`05` §annex) |
| RF-R5 scope fairness machinery to lanes where contention is physically possible | C5/S.2 — fairness machinery exists only at the lane/claim layer; no fairness scaffolding on uncontended paths |
| RF-G1 queue/history terminal-record seam | D1 — the intent ledger closes it (`02` §3) |
| RF-G2 `instagram_accounts` tenancy unclassified | FC-1 — workspace-owned `ig_accounts` (`02` §2) |

## Tenancy and topology

**C1 — Tenant root: `workspaces.id` (ruled).** FC-1 flips the earlier chat-rooted default: the pre-ruling adjudication kept `chat_settings.id` as the physical key because a rename bought zero isolation while costing a dual-key window; the product owner then ruled that multi-workspace/multi-account/multi-surface is the product. That is the exact contingency the default reserved ("if web-first onboarding / multi-chat / chatless tenants are on the roadmap, the workspace root should win now"). #722's neutral-`tenant_id`-at-service-boundaries rule is what makes the flip affordable: service code re-keys once, to the neutral name; only resolution changes. #721's workspaces *direction* is thereby resurrected; its letter is not (24 recorded quality flags — the five load-bearing ones are cited in this plan as FLAG-1/2/3/7/10; the full inventory is fleet-side working-set material, available on request, and nothing here depends on the other nineteen).

**C2 — Topology: multi-service (envelope-ruled scale-out; role separation justified on its own grounds).** FC-0 (thousands of tenants) overrules #721's single-process non-goal (N3) for **horizontal replicas** — that much is the product owner's envelope. The three-**role** split is justified separately (pass-2 correction, review A §4.3: tenant cardinality alone does not mandate role separation): (1) R5/H2 fault isolation — a poisoned publish pipeline must not take down ingress ack latency; (2) webhook ingress needs an always-up HTTP surface deploy-decoupled from workers; (3) the deployment already runs API and worker as two Railway services today — the target topology is the status quo plus an elected clock, not new machinery.

**C4 — RLS: enforce early, constant-expression policies.** RF-R3 as ratified (see map above); #721's RLS-later posture (N7) overruled; #721's composite tenant FKs (D12) kept — they complement RLS, catching what policies structurally cannot (cross-workspace FK references). Extended to credentials by the typed XOR owner FKs in `02` §2 (a polymorphic owner id would have broken the convention exactly where tokens live).

## Pipeline and execution

**D1 — Two proposed pipelines become one, layered.** #721's domain decomposition (intents/approvals/attempts/outcomes) and #722's jobs/leases both survive — at different layers: the **intent ledger is product state**, **jobs are execution units**, `provider_operations` is the external-effect rail. Three independent derivations converged on intent-centric domain state (#721, cold design, the shipped delivery-state trajectory), which also closes RF-G1. Only a lease holder touches a provider.

**D2 — Outbox never authorizes effects.** #722's semantics win; #721's outbox-executes model loses (it reopens the duplicate-effect window and had no lease concept — FLAG-7). The outbox is a delivery record + wake-up hint — and for sends it is the *single* authority on delivery state (`02` §6), so no second machine tracks "did it send".

**C9 — Cancellation is cooperative.** #722's model: `cancel_requested` flag, honored at checkpoints; `publishing → cancelled` forbidden; reconciler consults the flag where a real choice remains.

**C5 — Scheduling: O(due) clock + dispatcher as a named build item.** Precomputed `next_*_at` columns, one indexed scan per tick, all work in jobs (H3). #721's in-process scheduler (N10) overruled. The dispatcher is built inside `04` L.7 — #722 described it in prose but never gave it an increment.

**G1 — Real-account serialization and Meta budgets key on `provider_account_ref`.** One in-flight publish per real Instagram account by constraint (`02` §3-keys key 4), and Meta-side budget handling addresses the real account — both survive workspace-owned account rows precisely because they key on the Meta id, not our PK.

**G5 — `review_required` in both vocabularies.** Adopted from #722 into the intent ledger too; #721's enums lacked failure terminals entirely (FLAG-3). Poison edges added to jobs (#722 intra-fix).

**C8 — Idempotency keys compose.** Slot key + live-subject key including `ig_account_id` (FLAG-2 fix) + publishing-exclusivity key — stated fully in `02` §3-keys. The slot key carries no intent-kind discriminator: exactly one kind exists, and the closed-enum convention makes a future kind a deliberate migration that widens the key, so pre-adding it bought nothing.

**C10 — History uniqueness: interim then target.** Interim (pre-ledger): validated unique on `posting_history.queue_item_id` after the human-gated remediation of the 6 known production duplicate groups (TT:P0-03 prep, `04` 0.3). Target: one terminal outcome per intent (`02` §3-keys key 3). The interim step is not skippable — the backfill inherits its cleanliness.

## Infrastructure

**C3 — Postgres-only core; Redis is a gated annex.** RF-R4 as ratified. Peak job rates in the low tens per second against a ~2.2/s sustained publish ceiling (`05` arithmetic, pass-2 corrected) plus the transactional co-location argument (`01`) make a broker structurally unnecessary; #722's Redis increments are re-based to a pg fixed-window admission counter and indexed polling, and survive only as the `05` §annex behind a measured SLO breach. TT items P0-07/P1-02/P1-04 are incorporated with their Redis halves struck (`04` notes each).

**C6 — Migration tooling: formalized numbered-SQL runner, not Alembic (contested, reversible-with-cost).** The repo has 49 numbered migrations; #722's requirements (postconditions, checksums, replay-from-empty CI) bolt onto that reality cleanly and the known defects (023; #721's 010/034 missing-version-rows) are covered by the replay gate. #721 preferred Alembic and called its choice "ratified OWD" — no human ratification exists in the record, so this plan decides on evidence. Recorded as **contested**: if the team later wants ORM-autogen workflows, revisit after Phase W; the runner's artifacts (pure SQL + version rows) remain Alembic-importable.

**C7 — Flag split.** Routing/cohort flags (per-workspace) are DB rows; process/infrastructure enablement is env. FLAG-10's bootstrap objection (a flag store you need before the DB is up) is why env survives at all.

**Kept-table decision — `service_runs`.** Stays untouched through the program (it is the only bookkeeping for legacy loops that migrate incrementally), gains nullable `workspace_id`, and is retired at `04` S.4 when the last loop lands on jobs + `audit_events` — the consolidate-don't-fork endpoint, on a schedule instead of a fork.

## Constraint-driven (from the FC rulings)

FC-1/FC-2/FC-3/FC-4 are constraints, not adjudications — see `00`. Two derived decisions worth recording:
- **Interaction-layer shape (FC-2):** command vocabulary + `channel_outbox` + adapter-resolved identity (`01` §port) is the minimal structure that satisfies FC-2 while preserving the R5/R6 semantics the Telegram flow already earned. The web surface is pull-based (reads intents), not outbox-based — one less delivery machine, and the Mini-App already works this way.
- **FB-vintage sunset is a gate, not a date (FC-4):** zero active `fb_login_legacy` credentials → delete the legacy refresh + `/me/accounts` + CLI FB flow. Forcing a re-auth date would break working tenants for tidiness. New-row prevention is structural from L.6 (`02` §2 CHECK), not procedural.

## Sequencing and numbers

**G6 — One consolidated sequence.** `04` supersedes both packages' phase plans; its §Superseded list is the authoritative strike list. #722's P0 exit-gate rigor is kept item-by-item (each increment names the TT items it absorbs); its workers-before-webhooks ordering (IP) wins over the epic's Phase-5 placement; reconciliation is owned by maintenance workers (dedicated REC role deleted); the epic's Redis-degraded allowances list is the harmonized one wherever a residual reference survives (all #722 intra-fixes, applied).

**Numbers — supplied, marked initial.** Both packages shipped zero operational numbers. `05` closes that gap and is the sole normative home for the values, the derivation discipline, and the revision rule.

## #721 content carried forward (direction kept, letter re-derived)

- Six-stage migration machine — ratified; single normative statement in `04` §Ground rules; applied per track in Phases F/W.
- Consumer-contract track — restated in full as `04` W.6 (JWT claim, BFF lockstep, card-payload mapping column, CLI routing).
- `integration_connections` concept → typed `oauth_credentials` (`02` §2, FLAG-1 fixed via typed owner FKs).
- Settings materialization (D9) → `workspaces` config columns + `channel_bindings.settings`.
- Onboarding merge (D10) → `onboarding_sessions` re-keyed per `02` §9: user-keyed with `pending_workspace_id`, one live session per user.

## Second-pass decisions (Codex reviews A/B, 2026-08-02→03)

Same relitigation rule as above. "A §n"/"B §n" cite the two review comments on PR #731.

**D13 — Chat cardinality 0..n per workspace is deliberate.** A §2 flagged it as a silent change to #721's one-chat v1 restriction. It is FC-1.3 applied (a Telegram identity manages one-to-many workspaces; web-only workspaces are legal), now recorded: the widening is a decision, not drift. `uq_binding_external` still binds each external chat exactly once globally.

**D14 — R3's mechanism: frozen ledger row, not an outcomes table.** The immutable terminal record is the terminal `post_intents` row itself, made immutable by database machinery (`02` §4: legal-edge reference table + BEFORE UPDATE freeze trigger + AFTER UPDATE audit trigger requiring a named actor + INSERT guard + state-completeness CHECKs). The alternative — a separate terminal-outcomes table — was considered and **rejected**: a second terminal-truth home is precisely the queue/history seam (RF-G1) D1 kills. R3's ledger wording is amended to "database-enforced (constraint or trigger), never application discipline" — the honest claim for what triggers deliver (A §3.1, B §3: implement or drop — implemented).

**D15 — Enums are TEXT + named CHECK, never native ENUM.** Members must be removable (`fb_login_legacy` dies at G.2; native `DROP VALUE` does not exist), and the existing enum-SSOT parity gate already verifies CHECK text. `02` §0.

**D16 — ON DELETE is a three-class policy, not per-FK judgment.** Workspace-rooted/tenant-child edges CASCADE (deletion happens exactly once, at offboarding, by `svc_maintenance` — runtime roles hold no DELETE); `users` attributions SET NULL; `audit_events` carries no FK (audit outlives the tenant; retention is its only death). `02` §0. Replaces #721's per-FK RESTRICT posture (A §3.3 noted we carried nothing): CASCADE-from-root composes with T3 offboarding, and the two NO-CASCADE composite FKs on `post_intents` keep mid-life deletion of referenced rows impossible.

**D17 — Cloudinary is off the provider-operations rail.** Pass 1 gave upload/destroy the same at-most-once weight as IG publish; A §4.6 is right that recoverable, TTL-reaped effects do not need an effect rail. `provider_operations` covers `ig` only; Cloudinary state lives in job attempts + audit. Reversal cost if wrong: re-adding a provider value is one CHECK migration.

**D18 — Scheduling is per account; the workspace holds defaults (FC-1 completion).** Cadence, window, tz, and the slot cursor live on `ig_accounts` (NULL = inherit workspace column); caps debit per (workspace, account, account-local day); `recent` locks are account-scoped, human-judgment locks workspace-scoped; approvals carry the target account. `06` §3 is the normative statement (answers A §5.4/5.5, B §2's scheduling half).

**D19 — Account movement is clone-and-retire.** New row in target workspace, credential ciphertext copied (the token is an app+user grant — it moves without provider ceremony), source row terminal `'moved'` and excluded from live uniqueness; history/counts/locks stay home; zero live intents precondition; admin+ in both workspaces. In-place re-key is inexpressible by design (composite FKs). `06` §4 (answers A §3.6).

**PA-1 — OPEN FORK (ratifier: product owner): provider-account identity across workspaces.** B §2's question — may unrelated users connect the same real Instagram account, and what is the authority model? **(a) Independent connections** (the default this plan implements; DDL as in `02` §2): each workspace's connection is its own OAuth grant — independently refreshed, revoked, quarantined, alerted; possession of the Instagram login is the authority boundary (Meta's, not ours); global publish serialization + Meta budgets already key on `provider_account_ref`. **(b) Global single-ownership**: one live row per real account fleet-wide (partial unique), connect-elsewhere blocked, movement = transfer. (b) is one migration away from (a); nothing else in the plan changes. Recommendation on record: (a) — the platform fact that tokens are independent per-grant makes (b) a product restriction, not a technical necessity. **Until ruled, implementers build (a).**

**D20 — Transaction substrate: the final async UoW, built at L.0, before any Phase-L machinery.** B §5 demanded the decision; A conflict I named the double-rewrite cost of deferring it. Phase L is all new code — it is born async on the final substrate; legacy sync paths keep their session until their cutovers; S.3 keeps egress hardening only.

**D21 — The Meta usage pre-check is lazy, inline, and keyed on `provider_account_ref` — the eager reading is struck.** B §6 (the finding both reviews initially missed): a 5-min per-account cache read as a refresher is ~1,500 queries/min at the corrected envelope against ~130 publishes/min of real work — the advisory mechanism manufacturing the load it advises about. No background refresh exists (no job kind); cost is bounded by publish traffic; error 9 stays authoritative. `02` §8.

**D22 — Admission is per-workspace only; there is no global command ceiling.** A §4.1 / B's S.2 criticism accepted: no app-wide platform budget exists to protect; a fleet-wide fixed ceiling invents a bottleneck. Global protection = bounded pools + backpressure visibility. `04` S.2, `05`.

**D23 — Category mix keeps its row shape.** Ground truth: `category_post_case_mix` is a Type 2 SCD table; pass 1's `workspaces.category_mix` JSONB would have flattened history for nothing. Re-keyed on the W.5 track, SCD semantics unchanged, sum-to-1 stays service-enforced (a deferred cross-row aggregate trigger was considered and rejected as complexity without a failure mode it prevents — the writer is one service). Settles A §3.14's delegated choice.

**D24 — Runner ledger and chain reconciliation.** `schema_migrations` (checksums, advisory-lock serialization, postconditions, repair states) supersedes `schema_version`; migration 050 fix-forwards the known chain defects (004/008 orphaned unique, 010/034 missing stamps, caption_style type) so replay-from-empty equals production — with a `\d`-against-prod precondition before 050 is written. `04` 0.2 (answers A §3.26/§5.16).

**D25 — Ownership has one home: the owner member row.** The pass-2 draft carried `workspaces.owner_user_id` beside the `role='owner'` member row and spent two sync triggers keeping them equal; the consolidation review cut the column. `uq_members_one_owner` (≤1) + a deferred owner-exists trigger (≥1) enforce exactly-one-owner at every commit; transfer is demote+promote in one transaction.

**D26 — Quarantine grain = the serialization key.** The draft's `''`-wildcard (workspace, provider) scope could not be matched against prefixed serialization keys — the fine grain was silently a no-op, and the wildcard deferred *every* provider's jobs. Rows now always carry the exact serialization key they defer (what the faulting adapter actually knows); entry also pushes matching ready jobs' `run_at` out of the claim scan. T2's isolation intent is met at a finer grain than its original wording.

**D27 — The manual posting path is first-class (`published_via`).** The first pass designed only the API pipeline; production's phase-1 manual flow (human posts by hand, taps Posted) had no edge — which also made the W.4 history backfill unsatisfiable against the completeness CHECKs. `published_via ('api'|'manual'|'legacy_backfill')` scopes the evidence requirements per path; `awaiting_approval → posted` (manual) and `review_required → posted` (operator resolve-posted) are legal, guarded edges. Surfaced by the pass-2 consolidation review, not by either Codex review.

**Count-grain note (B comparison, for the record):** "5 blocking conflicts" was the cross-check's coarse count; A's A–P inventory is the same incompatibility at finer grain. Both reviews and this plan agree on the conclusion; neither count is a disagreement with the other.

## Overruled, explicitly

#721: N3 single-process (by FC-0) · N7 RLS-later (by RF-R3) · N10 in-process scheduler (by C5) · outbox-executes (by D2) · chatless-tenant *schema letter* (by `02`, direction kept via FC-1) · Alembic preference (by C6, contested) · the (tenant, content) live-intent key (by C8).
#722: Redis-in-core (by C3/RF-R4) · epic's webhooks-before-workers order (by IP order) · dedicated REC role (by intra-fix) · its silence on dispatcher build, numbers, and failure-terminal vocabulary (each filled above).
Cold design: 200-tenant envelope (by FC-0) · `tenants.tg_chat_id` column and Telegram-shaped ingress as the only surface (by FC-1/FC-2) · its 15-min RLS skepticism was already withdrawn in cross-check (RF-R3 stands).
