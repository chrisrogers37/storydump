# Archive — completed, superseded and abandoned plans

Moved out of `documentation/planning/` as plans completed — first in bulk after an audit of every plan against `main`, the
GitHub tracker and the recorded production probes (issue #1212), then one at a time; each row says when. Each file carries a one-line status
banner at its top; this index says why it is here and where its successor lives. Nothing in this
directory is normative — the authoritative plan is
[`../planning/2026-08-02-consolidated-design-plan/`](../planning/2026-08-02-consolidated-design-plan/README.md).

Status vocabulary: **COMPLETED** (built or fixed) · **SUPERSEDED** (replaced by something newer, named) ·
**ABANDONED** (no code, no issue, contradicted by current direction) · **STALE / PARKED** (pending with no
activity; revive by filing an issue).

| Path | Status | Why it is here / successor |
|---|---|---|
| `2026-07-29-high-throughput-multi-tenant/` (7 files) | SUPERSEDED | #722's architecture package, adjudicated into the consolidated plan (its `03-decision-record.md` records what survived). `review-findings.md` (#730) is honored, not superseded. Cited from the plan as provenance only |
| `2026-07-29-data-model-evaluation/` (8 files) | SUPERSEDED | #721's data-model package (merged 2026-08-10). Its intent-ledger and workspace-rooted direction survive in the plan; its six-stage migration machine was struck by FC-7. Banners added 2026-09-02 |
| `2026-07-system-review/` (4 files) | COMPLETED audit record | 2026-07-02 full-system review. Live state is the GitHub `system-review` label: 5 epics (#560, #576, #577, #578, #579) + one issue per finding (#580–#658); 72 open / 12 closed on 2026-09-02. The document's "36 issues" was the planned clustering, not what was filed |
| `2026-09-07-category-registry-and-full-walk/` (5 files) | BUILT (1a, 04) · SUPERSEDED (1b, 2, 3) | The full-depth Drive walk (#1256, 070) and the posting mix keyed on the connected folder (#1262, 071) under the 2026-09-08 ruling *sources are the groups*; the id-keyed registry phases are the record of the design that ruling replaced. Follow-up: #1263 (drop the v1 mix keys after the web deploy) |
| `investigations/ig-oauth-cross-flow-reconnect_2026-05-25/` | RESOLVED | Reconnect-loop fix shipped as PR #441; the token-corruption half was answered by the host-routing investigation |
| `investigations/ig-posting-persistent-failure_2026-05-26/` | SUPERSEDED | Storage-corruption theory replaced by the host-routing root cause; its recommendations became plan decision D31 (#732 closed) |
| `investigations/ig-host-routing_2026-06-02/` | RESOLVED | PRs #462, #476–#479 merged 2026-06-02 → 06-04; issue #468 closed |
| `2026-08-11-f1-ownership-inventory/` | COMPLETED as legacy → SUPERSEDED | F.1's fail-closed interface was built as `src/repositories/tenant_scope.py` (#846) and deleted with the legacy repositories (#1316); the contract lives on in `unit_of_work.py` (a tenant id is required to construct one) and `tenant_resolution.py` (F.3); the 14-table inventory is `tests/scripts/legacy_inventory.py`. Moved 2026-09-20 |
| `2026-08-14-f2-increment-split/` | COMPLETED | All nine segments landed as migrations 052–060 (#806), replayed in CI by the lineage lane and the advertised-DDL gate, applied to production 2026-08-26. Moved 2026-09-20 |
| `2026-08-17-m1-transform-spec/` | ABANDONED | Owner ruling 2026-09-02: legacy data is not migrated, the target is greenfield (`00` FC-7 §6). Its inputs are gone — the legacy models (#1316) and the `legacy` schema (079, 2026-09-19); the lineage survives only as the `archive.*_pre_cutover_20260917` snapshots (90 days). Moved 2026-09-20 |
| `2026-08-17-m2-rehearsal-spec/` | SUPERSEDED (executed differently than written) | 3a–3d by hand (2026-08-24/26), 3e abandoned, 3f/3g/step 8 as migrations 078–080 rehearsed on a PITR branch under `operations/legacy-window-close.md`; 080 is the partial stand-down (F8 (a)). The record is `2026-09-16-legacy-tear-out/RUN_LOG.md`. Moved 2026-09-20 |
| `2026-09-20-tech-debt-audit/` | COMPLETED | The tech-debt lens over the whole repo, three days after the tear-out's last phase. 75 findings, 16 one-PR-each cleanups, all merged 2026-09-20/21 (#1336–#1356); the baseline is unregressed at 3,679 passed. The thirteen flagged defects — the audit's twelve plus the display-name erasure doc 02's gate re-homing exposed — were resolved after the closeout by their own PRs (#1374–#1391; B19 ruled deliberate). `RUN_LOG.md` is the ledger, and §8 records which of the audit's own claims did not survive contact with the code. Moved 2026-09-21 |
| `2026-08-17-m3-parity-bar-mapping/` | SUPERSEDED | The parity bar was retired as a window precondition (`00` FC-7 §8, 2026-09-02); its two forks were ruled 2026-08-21 (`src/services/target/prompts.py`: the tap IS `approve`; parity is out); every bar item is served on a target surface today; chat-inbound typed commands (#854) remain owed. Moved 2026-09-20 |
| `investigations/2026-09-04-signin-bounce-and-instagram-redirect/` | RESOLVED | `OAUTH_REDIRECT_BASE_URL` restored and the Meta redirect-URI list corrected (2026-09-04); the entry-page fix #1236. Unbuilt: `/health`'s OAuth-presence booleans (#1229). Moved 2026-09-20 |
| `investigations/2026-09-06-empty-library-after-first-sync/` | RESOLVED | The full-depth Drive walk (#1256, 070) and the posting mix keyed on the connected folder (#1262, 071), re-ruled 2026-09-08. Moved 2026-09-20 |
| `2026-09-09-telegram-interaction-at-throughput/` | COMPLETED except phase 4 (deferred; its trigger may have fired — see the banner) | The Telegram tap built for throughput: phases 1, 2, 3a and 3b built (W4, #1271). Phase 4, tenant fairness, waits for its trigger, one arm of which — a second live workspace — #1383 (merged 2026-09-21) records as met: an owner decision. Moved 2026-09-22 |
| `2026-09-15-cli-v2/` + `2026-09-15-cli-v2-spec.md` | COMPLETED | The `storydump` v2 CLI and its approved spec: #1310 tokens, #1311 reads, #1312 writes and environment verbs, corrected by #1314. Live description: `AGENTS.md` › "The `storydump` CLI (v2)". Moved 2026-09-22 |
| `2026-09-16-cli-v2-audit/` | COMPLETED audit record | The system review of the v2 CLI surface; its findings folded in #1314. Moved 2026-09-22 |
| `2026-09-16-legacy-tear-out/` | COMPLETED | Retiring the legacy tier (#1216): #1316, #1319, #1318, #1321, #1322, and the owner's window on 2026-09-19 (079, 080). `RUN_LOG.md` is the ledger. Moved 2026-09-22 |
| `2026-09-21-worker-login-doors/` | COMPLETED | #751 part 2: the worker's doors (082, #1349) and its switch to `svc_worker` (#1358). Moved 2026-09-22 |
| `investigations/publish-first-fetch_2026-09-11/` | RESOLVED | Approved but never posted: the first fetch, and its three follow-ups (the failure reporting, the fetch path, the float). Moved 2026-09-22 |
| `2026-03-31-meta-app-launch-design.md` | COMPLETED as legacy → SUPERSEDED | Instagram-Login OAuth shipped in the legacy tier; FC-4 rules out Facebook Login; target rebuild is `src/services/target/ig_login_oauth.py` (#863). App Review: #410, `../operations/meta-app-review.md` |
| `2026-05-18-instagram-credential-refactor.md` | COMPLETED as legacy → SUPERSEDED | Migrations 035–041 (#468 closed; parent #380 still open). Target tier: `ig_accounts` / `oauth_credentials` |
| `per-request-session-isolation.md` | COMPLETED (different mechanism) | `concurrent_updates(8)` + ContextVar sessions in PR #573; async unit of work is the target tier's `unit_of_work.py` (L.0) |
| `web-app-migration-plan.md` | EXECUTED → SUPERSEDED | Phases merged as PRs #196/#201/#202 (2026-04); replaced by the target tier's Google sign-in + API (#1015, #1028, #1032) |
| `instagram-deeplink-redirect.md` | BUILT, not activated | Redirect page lives at `docs/index.html` (PR #116); button wiring removed later; activation tracked at #528 |
| `phases/00_MASTER_ROADMAP.md` | SUPERSEDED | Phases 1–2.5 are accurate history; the e-commerce-hub vision and phases 3–8 are not the current program (FC-9) |
| `phases/02_shopify_integration.md` | ABANDONED | No code, table, route or issue |
| `phases/03_printify_integration.md` | ABANDONED | No code or issue; depends on Shopify |
| `phases/04_media_product_linking.md` | ABANDONED | No code or issue; depends on Shopify + Printify |
| `phases/05_llm_integration.md` | SUPERSEDED | Only the caption slice shipped (`caption_service.py`, #182) |
| `phases/06_order_email_automation.md` | ABANDONED | No Gmail / order-notification code; depends on Shopify |
| `phases/07_dashboard_ui.md` | SUPERSEDED | Delivered via `web-app-migration-plan.md`, then rebuilt on the target schema (#1032) |
| `updates/` (3 files: `2026-01-04-bugfixes.md`, `2026-01-10-category-scheduling.md`, `2026-01-11-force-posting-queue-shift.md`) | HISTORY of the legacy tier | Dated update notes of January 2026 over the legacy scheduler, queue and bot, deleted in the legacy tear-out (#1216). Moved from `documentation/updates/` on 2026-09-18 (the tear-out's phase 05) |
| `2026-05-telegram-delivery-burst-postmortem.md` | HISTORY of the legacy tier | The May 2026 postmortem of the legacy Telegram delivery path; the target delivers through `channel_outbox`. Moved from `documentation/operations/` on 2026-09-18 |
| `2026-07-14-cloudinary-feature-gap-analysis.md` | STALE / PARKED | Proposals measured against the legacy tier's Cloudinary use; the target's path is `src/services/target/transit.py`. Moved from `documentation/cloudinary/` on 2026-09-18 |
| `2026-01-11-security-review.md` | HISTORY of the legacy tier | The January–February 2026 security review; every finding names legacy code deleted in the tear-out (#1216). The target's model is the plan's `07-security-model.md`. Moved from `documentation/SECURITY_REVIEW.md` on 2026-09-18 |
| `multi-account-dashboard.md` | SUPERSEDED | The legacy dashboard's instance picker; replaced by the target's `workspaces` / `workspace_members` and the dashboard rebuilt on the target schema (#1032). Moved from the top-level `planning/` on 2026-09-18 |

Earlier archives (Jan–Mar 2026 plans) were deleted in #311 (2026-04-28) and exist only in git history. So, since 2026-09-22, do `feed-queue-features/` (a backlog stale since 2026-03 that nothing referenced) and the sixteen phase docs of `2026-09-20-tech-debt-audit/` (every one merged): `git show 29acea2ec4d9cf72b5e6d8ddcbff23080d4da06e:documentation/archive/<path>` recovers any of them.
