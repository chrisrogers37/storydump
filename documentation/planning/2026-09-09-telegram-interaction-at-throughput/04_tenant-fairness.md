---
title: "Phase 4 — Tenant fairness (flagged; built only on evidence)"
type: plan
status: active
owner: chris
created: 2026-09-09
tags: [plan, worker, fairness, flagged]
links: []
---

# Phase 4 — Tenant fairness

## Summary

`fn_claim_job` is a global first-in-first-out on `run_at` with a per-workspace cap as the only brake, so a workspace that backlogs thousands of jobs sits ahead of every later arrival. `04:350` rules fairness a demonstration, not machinery, until shown otherwise; the archive's self-evaluation named the failure (`documentation/archive/2026-07-29-high-throughput-multi-tenant/self-evaluation.md:166-172`); #716 ruled the trigger must be live evidence. This phase is **flagged, not scheduled**: its scenario runs only when a second workspace exists in production or the status line shows saturation, and its machinery is built only if that scenario shows another workspace delayed past the interaction SLO or its lane's deadline budget.

## Evidence

- `scripts/migrations/059_security_definer_doors.sql:96-121` — `ORDER BY j.run_at LIMIT 1 FOR UPDATE SKIP LOCKED`, `p_ws_lane_cap`; the per-workspace count (`:109-112`) is served by the leased `(lane, workspace_id)` index 3b adds.
- `01-target-architecture.md:19` T2; `04-execution-sequence.md:350` S.2's "demonstration, not machinery"; `05-operational-numbers.md:38` the per-lane deadline budgets (interactive +10 min; bulk = slot end) that define "delayed past budget" here.
- #716 — fairness is evidence-triggered from a live saturation counter on the global bucket; 3a's status line (`tg_global_paced=`, `ws_oldest_wait=`) is that counter.

## Implementation Plan

### Dependencies

Phase 2's harness (this phase adds the `backlog_5000_one_workspace` scenario to it); 3a's status line (the production trigger); 3b's leased index (a migration, before any door is replaced). The scenario itself is gated: run it when a second workspace is live in production, or when the status line shows `ws_oldest_wait` past a lane's deadline budget or `tg_global_paced` sustained.

### Blocks

None.

### Steps

1. When the gate above opens, add the scenario to the harness: workspace A enqueues 5,000 `sync_media_source` chunks; workspace B's tap must answer within the 2 s SLO and B's oldest ready job must not wait past its lane's `05:38` deadline (interactive +10 min; bulk: the slot end). Both are properties of seeded rows and captured timestamps, never a wall-clock sleep (#672).
2. If it fails: F9 is re-served with the numbers. (a) age promotion — `ORDER BY j.run_at + (age bonus)` in a migration replacing `fn_claim_job`; (b) a per-tenant cursor table and round-robin claim. Either is a schema change under owner approval.
3. Otherwise this document stays flagged and the README row reads "not needed at the envelope", citing the report or the status-line numbers.

## Test Plan

The harness scenario; a gate on the replaced door if (a) or (b) is built, proving the property (B's job claimed before A's later arrivals) rather than timing it.

## Verification Checklist

- [ ] The scenario's gate is recorded: a second live workspace, or the status-line numbers that opened it.
- [ ] The `backlog_5000_one_workspace` report is committed with B's answer p95 and B's oldest-wait against its lane's deadline.
- [ ] Either F9 is locked (c) with the report cited, or a migration and gate exist.

## What NOT To Do

- Do not build ordering machinery before the report exists.
- Do not run the scenario as routine spend while one workspace exists and the status line is quiet.

## Context

area: jobs door · effort: S/M, evidence-gated (S to plan, M to build) · risk: low · priority: P3.
