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

**C2 — Topology: multi-service (ruled by envelope).** FC-0 (thousands of tenants) overrules #721's single-process non-goal (N3). Decided by the product owner's envelope, not by taste.

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

**C3 — Postgres-only core; Redis is a gated annex.** RF-R4 as ratified. Peak job rates in the low tens per second with ~1.5/s sustained (`05` arithmetic) plus the transactional co-location argument (`01`) make a broker structurally unnecessary; #722's Redis increments are re-based to a pg fixed-window admission counter and indexed polling, and survive only as the `05` §annex behind a measured SLO breach. TT items P0-07/P1-02/P1-04 are incorporated with their Redis halves struck (`04` notes each).

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
- Onboarding merge (D10) → `onboarding_sessions` re-keyed to (workspace, user).

## Overruled, explicitly

#721: N3 single-process (by FC-0) · N7 RLS-later (by RF-R3) · N10 in-process scheduler (by C5) · outbox-executes (by D2) · chatless-tenant *schema letter* (by `02`, direction kept via FC-1) · Alembic preference (by C6, contested) · the (tenant, content) live-intent key (by C8).
#722: Redis-in-core (by C3/RF-R4) · epic's webhooks-before-workers order (by IP order) · dedicated REC role (by intra-fix) · its silence on dispatcher build, numbers, and failure-terminal vocabulary (each filled above).
Cold design: 200-tenant envelope (by FC-0) · `tenants.tg_chat_id` column and Telegram-shaped ingress as the only surface (by FC-1/FC-2) · its 15-min RLS skepticism was already withdrawn in cross-check (RF-R3 stands).
