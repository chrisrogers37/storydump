---
title: "Phase 4 — Tenant fairness (flagged; built only on evidence)"
type: plan
status: deferred
owner: chris
created: 2026-09-09
tags: [plan, worker, fairness, flagged]
links: []
---

# Phase 4 — Tenant fairness

> **Deferred by ruling (F9 (c), 2026-09-09) — evidence-gated, not scheduled; re-armed on measurements by the owner, 2026-09-28.** Trigger: the worker status line's `ws_oldest_wait=` at or past 600 s (the tightest lane deadline, interactive +10 min) that the named workspace's quarantine (`provider_quarantine.quarantined_until`) does not explain; `tg_global_paced=` above 0 in five or more consecutive status lines (about five minutes; `src/services/target/backpressure.py`); or a tenant reporting late cards. *Evidence* gives the reading that set it and how to take it again.

## Summary

`fn_claim_job` is a global first-in-first-out on `run_at`, and its one brake, a per-workspace cap, cannot bind on a single worker (`05-operational-numbers.md` row 3): one workspace can hold a whole lane, and one that backlogs thousands of jobs sits ahead of every later arrival. `04:350` rules fairness a demonstration, not machinery, until shown otherwise; the archive's self-evaluation named the failure (`documentation/archive/2026-07-29-high-throughput-multi-tenant/self-evaluation.md:166-172`); #716 ruled the trigger must be live evidence. This phase is **flagged, not scheduled**: its scenario runs only when the trigger above opens, and its machinery is built only if that scenario shows another workspace delayed past the interaction SLO or its lane's deadline budget.

## Evidence

- `scripts/migrations/059_security_definer_doors.sql:96-121` — `ORDER BY j.run_at LIMIT 1 FOR UPDATE SKIP LOCKED`, `p_ws_lane_cap`; the per-workspace count (`:109-112`) is served by the leased `(lane, workspace_id)` index 3b adds.
- `01-target-architecture.md:19` T2; `04-execution-sequence.md:350` S.2's "demonstration, not machinery"; `05-operational-numbers.md:38` the per-lane deadline budgets (interactive +10 min; bulk = slot end) that define "delayed past budget" here.
- #716 — fairness is evidence-triggered from a live saturation counter on the global bucket; 3a's status line (`tg_global_paced=`, `ws_oldest_wait=`) is that counter.
- **The reading that re-armed it (2026-09-28).** The third arm was "a second live workspace", a
  stand-in for someone to be unfair to. It read as met on 2026-09-21 (#1383), so the measured arms
  were checked instead (2026-09-22 13:35 to 2026-09-28 13:31 UTC, 8,586 status lines across 20
  worker deployments): the worst `ws_oldest_wait` was 47.9 s, and `tg_global_paced` never rose
  above 0. The owner replaced the stand-in with the thresholds in the banner.
- **How to check the trigger** (read-only; written against railway 4.30.3). `railway logs` reads
  one deployment per call, so a window needs the deployment list first:

  ```
  storydump health    # now: the backpressure cell, from /health/scheduling
  railway deployment list --service worker --json --limit 100   # the default is 20
  railway logs <deployment-id> --service worker --filter ws_oldest_wait --lines 5000
  # --lines is capped below 20000. Per line: ws_oldest_wait=(?:[0-9a-f]{8} )?([\d.]+)s and
  # tg_global_paced=(\d+). One status line a minute (status_interval_seconds): far fewer than
  # the window's minutes means a call was cut short; slice it with --since/--until.
  ```

## Implementation Plan

### Dependencies

Phase 2's harness (this phase adds the `backlog_5000_one_workspace` scenario to it); 3a's status line (the production trigger); 3b's leased index (a migration, before any door is replaced). The scenario itself is gated: run it when the trigger in the banner opens.

### Blocks

None.

### Steps

1. When the gate above opens, add the scenario to the harness: workspace A enqueues 5,000 `sync_media_source` chunks; workspace B's tap must answer within the 2 s SLO and B's oldest ready job must not wait past its lane's `05:38` deadline (interactive +10 min; bulk: the slot end). Both are properties of seeded rows and captured timestamps, never a wall-clock sleep (#672).
2. If it fails: F9 is re-served with the numbers. (a) age promotion — `ORDER BY j.run_at + (age bonus)` in a migration replacing `fn_claim_job`; (b) a per-tenant cursor table and round-robin claim. Either is a schema change under owner approval.
3. Otherwise this document stays flagged and the README row reads "not needed at the envelope", citing the report or the status-line numbers.

## Test Plan

The harness scenario; a gate on the replaced door if (a) or (b) is built, proving the property (B's job claimed before A's later arrivals) rather than timing it.

## Verification Checklist

- [ ] The scenario's gate is recorded: the status-line numbers, or the late-card report, that opened it.
- [ ] The `backlog_5000_one_workspace` report is committed with B's answer p95 and B's oldest-wait against its lane's deadline.
- [ ] Either F9 is locked (c) with the report cited, or a migration and gate exist.

## What NOT To Do

- Do not build ordering machinery before the report exists.
- Do not run the scenario as routine spend while the trigger is closed.

## Context

area: jobs door · effort: S/M, evidence-gated (S to plan, M to build) · risk: low · priority: P3.
