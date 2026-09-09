---
title: "Phase 4 — Tenant fairness (flagged; built only on evidence)"
type: plan
status: draft
owner: chris
created: 2026-09-09
tags: [plan, worker, fairness, flagged]
links: []
---

# Phase 4 — Tenant fairness

## Summary

`fn_claim_job` is a global first-in-first-out on `run_at` with a per-workspace cap as the only brake, so a workspace that backlogs thousands of jobs sits ahead of every later arrival. `04:350` rules fairness a demonstration, not machinery, until shown otherwise; the archive's self-evaluation named the failure (`documentation/archive/*/self-evaluation.md:166-172`). This phase is **flagged, not scheduled**: it is built only if phase 2's `one_slow_chat` or a backlog scenario shows another workspace delayed past the 2 s interaction SLO or the slot budget.

## Evidence

- `scripts/migrations/059_security_definer_doors.sql:96-121` — `ORDER BY j.run_at LIMIT 1 FOR UPDATE SKIP LOCKED`, `p_ws_lane_cap`.
- `01-target-architecture.md:19` T2; `04-execution-sequence.md:350` S.2's "demonstration, not machinery".

## Implementation Plan

### Dependencies

Phase 2's harness with a `backlog_5000_one_workspace` scenario.

### Blocks

None.

### Steps

1. Add the scenario to the harness: workspace A enqueues 5,000 `sync_media_source` chunks; workspace B's tap and slot must land within SLO.
2. If it fails: F9 is re-served with the numbers. (a) age promotion — `ORDER BY j.run_at + (age bonus)` in a migration replacing `fn_claim_job`; (b) a per-tenant cursor table and round-robin claim.
3. Otherwise this document stays flagged and the README row reads "not needed at the envelope".

## Test Plan

The harness scenario; a gate on the replaced door if (a) or (b) is built.

## Verification Checklist

- [ ] The `backlog_5000_one_workspace` report is committed with B's p95.
- [ ] Either F9 is locked (c) with the report cited, or a migration and gate exist.

## What NOT To Do

- Do not build ordering machinery before the report exists.

## Context

area: jobs door · effort: S to plan, M to build · risk: low · priority: P3, evidence-gated.
