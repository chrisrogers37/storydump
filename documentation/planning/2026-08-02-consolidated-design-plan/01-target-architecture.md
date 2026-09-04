# Target architecture

## Requirements ledger (normative — the R/T/H ids cited throughout this plan)

Derived from the product and platform, validated by cross-check and #730. `RF-Rn`/`RF-Gn` elsewhere in this plan cite `review-findings.md` (#730); bare `Rn`/`Tn`/`Hn` cite this ledger.

**Correctness invariants**
- **R1** At-most-once **intended** IG publish per approved item — over-posting to a customer's account is the worst failure. Claim-before-publish; container id persisted before the publish call; the residual lost-response window (request reached Meta, confirmation lost) always routes to `publishing_ambiguous` and is resolved by evidence, never by retry — a duplicate therefore requires both a lost response *and* an operator resolution error (`02` §6 states the guarantee's exact boundary).
- **R2** Cadence caps enforced atomically per (workspace, account, account-local day) — no check-then-act gap under concurrent approvals.
- **R3** Exactly one immutable terminal record per posting attempt, **database-enforced (constraint or trigger), never application discipline** — the enforcing objects are named in `02` §4.
- **R4** Workspace-scoped repost prevention keyed on content hash; TTL and permanent lock semantics; no global hash namespace.
- **R5** Every user interaction answered fast, even when the work continues async.
- **R6** Prompts expire gracefully: terminal-state-first reads — a late interaction renders the terminal state, never acts on a stale row.
- **R7** Crash-anywhere recovery: no state that matters lives only in memory; every in-flight pipeline resumes or terminates deterministically; **no undead states** — every state has an owning process and an exit.
- **R8** Retry taxonomy per provider adapter: retryable (network/5xx/429) vs terminal (validation/revoked) vs **ambiguous** (timeout after submit). Ambiguous Telegram sends are never blind-retried; ambiguous IG publishes are reconciled by read-back, never stranded.

**Tenancy invariants**
- **T1** Hard data isolation by construction: NOT NULL workspace FK on every tenant-scoped table; no query path that returns cross-tenant rows when a parameter is omitted; enforcement structural (schema + required parameters + DB backstop), never per-handler discipline.
- **T2** Performance isolation and fairness: one workspace's bulk load, failing token, or provider outage never delays another's post or interaction; fault quarantine scoped to the workspace's failing resource (the serialization-key grain, `02` §2), never global; no structural starvation.
- **T3** Workspaces are rows, not deploys: onboarding = INSERT; offboarding = cascade + credential revocation; no per-tenant env, processes, or baked-in ids.
- **T4** One bot identity suffices at the envelope: per-chat limits are per-binding by construction; what does not scale is polling (one token = one poller), so ingress is **webhook**, send pacing is central, and token-sharding stays a documented escape hatch only.

**Throughput requirements**
- **H1** Sustain hundreds of concurrent publish pipelines, I/O-bound, with per-real-account serialization (one in-flight publish per Instagram account) as the concurrency unit.
- **H2** Interactions answered < 2 s p95 independent of bulk load (two QoS lanes).
- **H3** Slot evaluation is O(due), not O(N): one indexed query over precomputed `next_*_at`, never N sequential evaluations.
- **H4** Media sync is demand-driven: ahead of an approaching slot, on human request, and on a slow jittered baseline — not fixed short-cadence polling per source.
- **H5** Everything bounded: LIMIT on every sweep, timeout on every external call, visible backpressure, slip-a-slot rather than pile-up.
- **H6** Horizontal scale-out safe by default: any worker replica executes any workspace's job; adding a replica needs zero coordination.

## Workload truth (sets every ceiling)

Meta caps API publishing per real account (**100** / rolling 24 h — corrected at the pass-4 anchor from the stale 25; Meta has raised it over time and the authoritative per-account value is fetchable live, which `main` already does — `05` platform inputs) and per user (200 calls/hr) — so throughput comes from account count, never posting faster. At the FC-0 envelope (with the FC-1 multi-account correction to the bounding account count) the fleet publish ceiling is ~8.7/s at absolute cap — realistic steady state a fraction — and interactive peaks reach the low tens per second; each publish is a long-latency I/O pipeline while interactions are latency-sensitive small operations. The defining requirement is lane isolation (H2/T2), not raw rate. **All figures, their derivations, and the bounding-case definition live in `05-operational-numbers.md` — the only home; do not re-derive or copy without citing it.**

## Process roles

Three roles, one Postgres. Any replica of a role serves any workspace — tenancy is data, not topology (T3, H6).

**1. Ingress (stateless, N replicas).** The existing FastAPI service, extended. Hosts every *inbound channel adapter* (FC-2): the Telegram webhook adapter (secret-token validated; kills the single-poller ceiling per T4) and the web/Mini-App API adapter, plus OAuth callbacks. An adapter does zero business work: it authenticates the transport, resolves external identity → (`user_id`, `workspace_id`, `channel_binding_id`) via `user_identities` + `channel_bindings`, runs the **one central authorization gate** (`workspace_members` role check — one place, not per handler), acknowledges the transport immediately (R5), and executes the command inline when it is a single-transaction state flip or enqueues its specific job kind when it spawns real work (`02` §5). Idempotent admission: replayed deliveries collapse on `command_dedup` (`02` §6).

**2. Workers (N replicas).** Claim jobs via `SELECT … FOR UPDATE SKIP LOCKED` with leases (`locked_until`), lease-token fencing, and heartbeats; asyncio pools; async DB end-to-end. All provider I/O behind adapters carrying timeouts, the R8 taxonomy, and the generic (workspace, provider) quarantine check (`02` §quarantine). Workers host the *outbound channel senders* (FC-2): dedicated sender jobs drain `channel_outbox` with per-chat + per-token pacing for Telegram; future channels add senders, not core changes.

**3. Scheduler-as-clock (tiny singleton, pg-advisory-lock elected from among the workers).** Converts time into jobs, nothing else: one indexed scan per tick over precomputed `next_*_at` columns → idempotent job inserts (H3). Every heavy step is a worker job, so a tick is milliseconds and never slips. The **dispatcher** (due-scan → job insert) is a named build item — it lands in `04` L.7, where #722's sequence never built it at all.

## Interaction-layer port (FC-2)

The domain is already **state-centric** at its heart — an approval is a database state, not a Telegram card (today `posting_queue.status`; target `post_intents`). What it is *not* yet is channel-neutral: the current queue row carries `telegram_message_id`/`telegram_chat_id` columns and a delivered-requires-message-id constraint (pass-4 anchor). The port makes the neutrality explicit and strips the channel columns off domain state:

- **Inbound:** adapters normalize to the **closed command vocabulary** — this list is its normative home, and adding a command is a deliberate change here, ratchet-visible: `approve` · `skip` · `reject` · `mark_posted` (manual mode) · `cancel` · `autopost_now` · `sync_now` · `settings_change` / `account_settings_change` · `pause_workspace` / `resume_workspace` · `connect_account` / `reconnect_account` / `disconnect_account` · `move_account` / `disable_account` · `create_workspace` / `rename_workspace` / `offboard_workspace` / `restore_workspace` · `invite_member` / `remove_member` / `change_role` / `transfer_ownership` · `resolve_review` (posted|failed|retry|cancel) · `clear_quarantine`. Each carries resolved domain ids only; core services never see a chat id or callback payload.
- **Outbound:** domain code emits **interaction requests** (approval-request, notification, digest) addressed to a `workspace_id` + audience; rows land in `channel_outbox` per push binding. A Telegram approval card and a Mini-App approval list are two renderings of the same `awaiting_approval` intent — the web surface reads the same state via API (pull, no outbox rows); a tap and a click converge on the same command.
- **Answer semantics (R5, R6):** transport acks are the adapter's job (interactive lane, H2). A late interaction on a terminal intent renders the terminal state, never acts.
- **Enforcement:** the FC-2 ratchet (installed `04` F.6) — including its structural rule that `src/services/core/**` and domain models import no Telegram library and hold no Telegram-typed columns. One mechanism; no parallel grep gates.

## Tenancy spine (FC-1, T1)

`workspaces.id` is the tenant root. Service boundaries pass the neutral `tenant_id` (== `workspaces.id`) as a **required leading parameter** — no optional-tenant code path exists to fail open. Repositories are fail-closed by construction; RLS (keyed on `SET LOCAL app.tenant_id`) is the DB backstop (`02` §RLS, C4). Isolation holds **between workspaces of the same user** exactly as between strangers — the sharing unit is the workspace, membership is the only bridge.

## Job machinery

- **Jobs live in Postgres, not a broker** (C3): peak job rates sit in the low tens per second against a publish ceiling of ~8.7/s at absolute cap — ~2.2/s at the cadence-realistic working figure (`05`, pass-4 corrected) — what the product needs is transactional co-location: claim job + flip intent state + increment cap ledger + append audit event in ONE transaction. That boundary is where R1/R3 live. The jobs interface is deliberately narrow; a Redis annex exists as a gated escape hatch (`05` §annex) behind measured SLO breach.
- **Two lanes with reserved capacity** (`interactive` / `bulk`): bulk can never occupy interactive capacity (H2, T2).
- **Per-key serialization:** a job whose `serialization_key` has a running peer is skipped by the claim query. Publish serialization keys on the **provider account ref** (Meta IG user id) — one in-flight publish per *real* account even when it appears in multiple workspaces (H1, G1 in `03`).
- **Leases + step checkpoints (R7):** publish is a resumable pipeline; each step checkpoints on the intent row (`02` §ledger). Dead worker → lease expires → job resumes at the checkpoint; a resumed stale owner is fenced and cannot produce a second provider effect.
- **Ambiguity is owned (R8):** `publishing_ambiguous` intents belong to a reconciler job (bounded LIMIT sweeps, cadence + evidence budget in `05`) running the `02` §6 evidence contract — container status decides; an exhausted budget parks the intent `review_required` for a human, never a blind retry.
- **Quarantine, not global circuit-breaking (T2):** provider faults write a `provider_quarantine` row at the (workspace, provider[, account]) grain (`02` §quarantine); the claim query defers matching jobs with backoff, the workspace gets one alert on quarantine entry, everyone else is untouched.
- **Demand-driven sync (H4):** pre-slot sync when a slot approaches and the source is stale; on-demand via command; slow jittered baseline for idle workspaces; first-time large-library ingest as chunked, checkpointed bulk jobs. Everything bounded per H5.

## Media transit (FC-3)

The publish pipeline implements FC-3.1–3.6 exactly as tabled in `00-fixed-constraints.md` — workspace-prefixed, **per-request-signed**, authenticated uploads (FC-3.4 delivered via D28's signed-params mechanism, `03`), signed non-expiring delivery with the TTL attached to the asset (FC-3.2 as amended; D38), reap-on-success, and the hard-TTL sweep job — with values in `05`. Drive remains per-workspace OAuth (already scales per FC-0).

## Media-source port (FC-8, D37 — FC-2's discipline applied to media)

Media sources are a **pluggable adapter surface**: provider-neutral core, adapters at the edge, adding a provider costs an adapter rather than a core change — the third instance of this plan's per-provider discipline, after the interaction-layer port above and the D33/D34 auth providers. The port, justified operation-by-operation by an existing core need:

- `list_changes(config, checkpoint) → (items, checkpoint')` — sync (H4); `items` carry the adapter's **canonical stable item ref** (the `02` §2 stable-ref contract — its normative home) plus name/kind/size/hash inputs.
- `stream(ref) → bytes` — the publish pipeline's transit fetch.
- `probe(config) → ok | error-class` — connect/repair validation (`media_sources.state` machine).

Provider-scoped shapes (`config`, `sync_checkpoint`) are versioned JSONB the core never interprets (`02` §2). **v1 implements exactly one adapter — Google Drive — through this port; there is no upload/write operation** (media ingestion is sync-only; the command vocabulary has no upload, a recorded non-goal with this port as the extension seam). The boundary is D37's core sentence: **the ruling asks for the seam, not the second implementation** — a Dropbox adapter is a drop-in when asked for, and building it unasked is out of scope.

## Instagram auth (FC-4, under FC-7)

Connect-account flows use Instagram Login OAuth in the ingress adapters; refresh via `graph.instagram.com`. **No FB-vintage credential exists in the target** — the `00` FC-4 application note is the argument's home; the target is Instagram-Login-only from its first production day.

## Observability (thin but load-bearing)

Per-lane queue depth + oldest-runnable-age (the backpressure signal), per-workspace last-success timestamps (post, sync, refresh), quarantine-state gauge, parked-intent alarm (`publishing_ambiguous` beyond the `05` threshold pages), heartbeats per role, transit-asset count vs TTL (FC-3.6 health). The state of the world is rows; ops questions are SQL.

## What deliberately does not exist

No broker/Celery/second datastore in the core; no per-workspace bot tokens, processes, or env vars (T3); no polling-loop hierarchy (the legacy background loops collapse into clock → jobs → workers + webhook); no global mutable singletons (breakers, sync gates, class-var counters — all cross-request state is rows); no fixed **short-cadence per-source** Drive polling as the freshness mechanism (H4's demand-driven shape + slow jittered baseline replaces it); no per-user Cloudinary credentials (FC-3); no Facebook Page auth (FC-4); no local-filesystem media path (FC-8 — full cloud; sources speak only through the media-source port above).
