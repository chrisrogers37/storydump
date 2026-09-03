# Archive — completed, superseded and abandoned plans

Moved out of `documentation/planning/` on 2026-09-02 after an audit of every plan against `main`, the
GitHub tracker and the recorded production probes (issue #1212). Each file carries a one-line status
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
| `investigations/ig-oauth-cross-flow-reconnect_2026-05-25/` | RESOLVED | Reconnect-loop fix shipped as PR #441; the token-corruption half was answered by the host-routing investigation |
| `investigations/ig-posting-persistent-failure_2026-05-26/` | SUPERSEDED | Storage-corruption theory replaced by the host-routing root cause; its recommendations became plan decision D31 (#732 closed) |
| `investigations/ig-host-routing_2026-06-02/` | RESOLVED | PRs #462, #476–#479 merged 2026-06-02 → 06-04; issue #468 closed |
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
| `feed-queue-features/01_live_story_visibility.md` | STALE | "Ready" since 2026-03, no code or issue since; targets the legacy Telegram `/status` |
| `feed-queue-features/02_feed_reset.md` | PARKED | Instagram has no story-delete API (the document says so) |

Earlier archives (Jan–Mar 2026 plans) were deleted in #311 (2026-04-28) and exist only in git history.
