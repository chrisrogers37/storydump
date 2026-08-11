# Tiered Issue Triage — Data Model Epic

**Baseline:** `main` at `683f7cf`; issue numbers checked against the open-issue
snapshot of 2026-07-29 (265 open issues).
**Companion:** phases in [04-epic.md](04-epic.md); this document orders the same work
by priority and reconciles it with the existing GitHub backlog so known work is
**referenced, not duplicated**.

## How to read this

- **Tier P0** — stop the bleeding: incident classes and rails that everything else
  depends on. Ship regardless of whether the rest of the epic proceeds.
- **Tier P1** — structural foundations: tenancy root, identity, transactions,
  credential ownership.
- **Tier P2** — model completion: media decomposition, publishing ledger, outbox,
  config unification, and the per-capability cutovers.
- **Tier P3** — post-contract hardening and opportunistic cleanup.
- **Existing issue** = already filed; the epic adopts it (possibly re-scoped as
  noted). **Proposed** = new issue to file, with a suggested title. Nothing below
  should be filed if an existing issue already covers it.

Existing epics this plan adopts wholesale as tracking umbrellas: **#576**
(multi-tenant isolation), **#577** (migration tooling / schema drift), **#578**
(process-local state), **#560** (queue/posting race class). The plan intersects but
does not own **#579/#623** (TelegramService god-facade) and the security review
cluster; see "Out of scope" below.

---

## Tier P0 — Rails and incident classes (Epic Phase 0 + urgent DB guards)

| Work item | Existing issues | Disposition |
|---|---|---|
| Adopt Alembic; baseline current schema | #638, #577 (epic) | Adopt as filed |
| CI replays migrations + ORM↔SQL parity diff | #654, #639 | Adopt; #654 is the test harness, #639 the drift class it closes |
| Mirror SQL-only constraints/partial indexes in ORM until parity gate lands | #641, #424 | Interim step; superseded once the parity diff is authoritative |
| Auto-apply migrations on Railway deploy | #712 | Adopt as filed |
| Retire/replace stale bootstrap paths (`setup_database.sql`, `init_db` imports) | #411, #640 | #640 becomes moot when Alembic replaces `create_all`; close with rationale rather than implement twice |
| DB unique on `posting_history.queue_item_id` + prod dup cleanup | #695, #551 | Adopt; prerequisite for the Phase 4 outcome unique (AC4.1) |
| DB guard against concurrent double-claim / unguarded `mark_publishing` | #711, #549 | Adopt; interim CAS guard now, structural fix arrives with `publish_attempts` |
| DB uniqueness against double-queued media | #604 | Adopt; becomes the "one live intent" partial unique in Phase 4 |
| Stuck-`publishing` triage tooling (read-only list + guarded release) | #565, #366 | Adopt; interim until the Phase 4 reconciler |
| Stamp `chat_settings_id` on all remaining write paths, then backfill | #669, #598, #599, #412 | Adopt; this is the legacy-side prerequisite for the Phase 1 workspace backfill (AC1.1) |
| Enforce `media_items.chat_settings_id NOT NULL` when stamping complete | #670 | Adopt; lands under the Phase 0/1 constraint-after-compliance rule |
| Backfill `api_tokens.auth_method` → NOT NULL so the unique holds | #596, #595 | Adopt; prerequisite for Phase 2 credential keys |
| Single current `category_post_case_mix` row per category/tenant | #643 | Adopt; partial unique + data fix |
| `get_or_create` retry on IntegrityError (tenant row creation race) | #602 | Adopt |

**Proposed new issues (P0):**

1. *"Archive `schema_version` history and map it to the Alembic baseline revision"*
   — completes #638's adoption story; also records that migrations `010`/`034` never
   inserted version rows.
2. *"Reconciliation report: enumerate and resolve every NULL-tenant row before
   ownership constraints"* — the data companion to #669/#670 (AC1.1 evidence
   artifact).

## Tier P1 — Tenancy root, identity, transactions, credential ownership (Phases 1–2)

| Work item | Existing issues | Disposition |
|---|---|---|
| Multi-tenant isolation enforced structurally | #576 (epic) | Umbrella for this tier |
| Unit of Work at service seams; repositories stop committing | #608, #630 | Adopt as filed; retires `atomic_session` monkey-patch |
| Session/transaction hygiene folded into UoW work | #629, #631, #632, #633, #634, #635 | Adopt; close individually as the UoW absorbs each |
| Workspace root + chat binding + settings split (expand/backfill/dual-write) | — | **Proposed** (below); no existing issue asks for a first-class tenant |
| External identities + membership re-rooting | #380 (identity/credential separation, older framing) | Re-scope #380 to Phase 2 credential work; identity split is new (below) |
| Kill phantom DM `chat_settings` creation paths | #524 | Adopt; acceptance test AC1.4 |
| Workspace-owned social accounts + connections + credentials | #595, #596, #627, #380 | Adopt; #627's fallback class must become structurally impossible (AC2.3) |
| OAuth start authorization + state replay hardening | #513, #587 | Adopt from the security cluster; Phase 2 touches these paths anyway |
| Role checks on account/settings mutations move to workspace memberships | #585, #530 | Adopt; membership model is the enabler |
| Tenant-scope the remaining reader gaps while legacy paths live | #593, #594, #600, #601, #677, #575, #584, #667 | Adopt as filed (cheap now; structurally obsolete post-cutover) |

**Proposed new issues (P1):**

3. *"Introduce `workspaces`, `workspace_chat_bindings`, `workspace_settings`
   (expand + backfill + reconciliation)"* — Phase 1 core; AC1.1–AC1.2.
4. *"Single tenant-resolution seam (`chat_id` → workspace) shared by API, bot, CLI"*
   — AC1.3; subsumes ad-hoc lookups.
5. *"Split Telegram identity into `external_identities`; re-root memberships on
   workspace"* — Phase 1; JWT/BFF type impact tracked in
   [06-migration-and-consumer-plan.md](06-migration-and-consumer-plan.md) §C7.
6. *"Merge the two onboarding state machines into workspace onboarding"* — absorbs
   the `onboarding_sessions` vs `chat_settings.onboarding_step` split; related UX
   issues #650, #652 stay independent.

## Tier P2 — Media decomposition, publishing ledger, outbox, config (Phases 3–5)

| Work item | Existing issues | Disposition |
|---|---|---|
| Queue/posting race class eliminated by ledger | #560 (epic), #692 | #692's shipped enum-SSOT/delivery-state work is the foundation; the ledger is its continuation, not a replacement |
| Durable attempts + outbox replace process-local state | #578 (epic), #611, #612, #606, #607 | Adopt; AC4.2–AC4.4 |
| Residual double-post / requeue windows | #680, #566 | Adopt; closed by intent/attempt uniques + reconciler |
| Failed-send and failed-auto-approve slot handling | #615, #616, #552, #556 | Adopt; re-express as intent/attempt transitions |
| Consolidate cleanup loops into one reconciler with metrics | #571, #565, #363, #366, #628, #636 | Adopt; AC4.5 |
| Media split: sources / provider objects / assets / content | #418, #420 | These audit issues are *resolved by* the split; close with the migration |
| Hash identity unification (MD5 vs SHA-256 backfill mismatch) | #619 | Adopt; must land before the asset `content_hash` unique |
| Sync correctness during dual-write | #365, #496, #362, #622 | Adopt; folded into Phase 3 ACs |
| Media eligibility shadow-read + cutover | #414 (perf motivation) | Adopt as the perf sibling of AC3.3 |
| Config truth unification (DB-only reads; env = bootstrap defaults) | #532, #322, #461 | Adopt; AC5.1 grep-gate |
| Durable uploads off `/tmp` | #592 | Adopt; asset storage decision belongs to Phase 3 |
| Read models replace denormalized counters | #416 | Adopt; AC5.2 |
| API/CLI service-layer bypasses removed at cutover seams | #314, #610 | Adopt; cutover PRs must route through services/UoW |
| Audit spine widened to intents/credentials | — (the `audit_log` table shipped with migration `025`; widening it is new work) | **Proposed** (below) |

**Proposed new issues (P2):**

7. *"Transactional outbox: table, dispatcher loop, claim CAS, metrics"* — Phase 4
   core (O4).
8. *"Publishing ledger: `publish_intents` / `approval_requests` /
   `publish_attempts`; `posting_history` gains `intent_id` unique"* — Phase 4 core
   (O3), sequenced after #695.
9. *"Per-workspace cutover flags + shadow-read comparators for eligibility and
   ledger reads"* — the observability half of AC3.3/AC4.2.
10. *"Widen `audit_log` → `audit_events` (intents, credentials, holds)"* — Phase 5.

## Tier P3 — Post-contract hardening and opportunistic cleanup

| Work item | Existing issues | Disposition |
|---|---|---|
| Native/validated enums for status columns | #642, #520 | Adopt during contract, when columns are rebuilt anyway |
| Explicit `ON DELETE` on person/ledger FKs | #417 | Adopt in contract migrations |
| History retention policy | #423 | Adopt post-ledger (outcomes table growth owns this) |
| Observability tables consolidation/retention | #415, #658 | Keep independent (non-goal N8); revisit after contract |
| Timezone normalization (TIMESTAMPTZ everywhere) | #421, #337 | Adopt during contract column rebuilds |
| UUID prefix-scan lookups replaced | #422 | Obsolete once ledger IDs are queried exactly; verify then close |
| Hot-read caching (settings, mix, accounts) | #425, #613 | Re-evaluate after workspace_settings cutover changes read patterns |
| Analytics read-path efficiency | #413 | Re-evaluate against ledger projections |
| RLS evaluation (defense-in-depth beyond composite FKs) | — | **Proposed**: *"Evaluate Postgres RLS on workspace-scoped tables post-contract"* (non-goal N7 until then) |
| Test-suite stabilization under load | #672, #655, #657 | Adopt alongside; the migration-replay CI DB (P0) is a prerequisite for #655's real-constraint tests |

## Out of scope for this epic (tracked elsewhere, deliberately)

- **Telegram scale topology:** #715, #716, #409 (webhooks), #554 — orthogonal to the
  data model; the outbox makes them easier later but does not solve them.
- **Connection-pool and Neon sizing:** #713, #690, #635 (the sizing half) — revisit
  after UoW changes session lifetimes.
- **TelegramService decomposition:** #579, #623, #624, #626, #505, #315 — code
  architecture, not data model; coordinate so cutover PRs don't fight refactors.
- **Security review cluster not touched by Phases 1–2:** #581, #580, #586, #588,
  #589, #591, #512, #518 — remain with their owners; Phase 2 explicitly picks up
  only #513/#587/#585/#627.
- **Monetization/insights epics:** #661–#665, #666 — the workspace root is their
  prerequisite (P1), but their schemas are out of scope (non-goal N4).
- **Product/GTM/design backlog:** #150–#310 range, #430, #485, #484 — untouched.
- **Cloudinary feature work:** #559 — only the egress-state relocation (Phase 4)
  intersects.

## Sequencing summary

P0 has no dependencies and starts immediately; every P0 item lands value even if the
epic stops there (they are Approach A of
[02-self-evaluation.md](02-self-evaluation.md) §5). P1 depends on P0's rails. P2
depends on P1's workspace ids and UoW. P3 rides the contract phase. Within each tier,
the referenced existing issues keep their own numbers and get closed by the epic's
PRs — the epic files only the eleven proposed issues above (ten numbered, plus the
P3 RLS evaluation).
