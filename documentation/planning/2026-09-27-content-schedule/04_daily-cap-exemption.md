---
title: "[plan] Phase 4: planned posts sit outside the daily cap — content schedule"
type: plan
status: draft
owner: astrid
created: 2026-09-27
tags: [storydump, content-schedule, publish-cap, daily-cap, issue-1413]
repos: storydump
---

# Phase 4 — Outside the daily cap

## Summary

Part of #1413. Size: M. Written for F8's lean (a). A planned post neither spends the account's
daily cap nor waits on it, so a spent day never pushes it to tomorrow, and it never pushes a
cadence story to tomorrow either. Meta's own limit, pause and publish exclusivity still apply.
If the owner picks (b), the debit stays and only the cap comparison is skipped. If the owner picks
(c), this phase is dropped.

## Evidence

- The flip and the debit: `publish_cap.flip_to_publishing` upserts `daily_post_counts` against
  `effective_cap` (`publish_cap.py:133-159`), and the pipeline passes `eff_ppd`
  (`publish_pipeline.py:403`, flip at `:589`).
- A spent day defers to the next slot and says "tomorrow" (`publish_pipeline.py:639`).
- The refunds decrement the count (`publish_cap.py:204`, `:239`).
- A `publishing` row must carry `cap_consumed_on` (`055:174`).

## Implementation Plan

### Dependencies
Phase 2 (`origin` exists); F8 locked.

### Blocks
Phase 5.

### Steps
1. The flip reads `origin`. For a planned row it skips both the cap comparison and the
   `daily_post_counts` upsert, but still stamps `cap_consumed_on` with the account-local date,
   because `055:174` requires the column on a `publishing` row. Everything else in the flip is
   unchanged: key 4, cancel honour, and the busy wait.
2. Both refund legs (`publish_cap.py:204`, `:239`) skip planned rows, so a count that was never
   debited is never decremented.
3. The considered alternative: relax `055:174` so that planned rows keep `cap_consumed_on` NULL.
   It reads more cleanly, but it is DDL for a naming nicety. Rejected unless review prefers it.
4. Add a `CHANGELOG.md` entry.

## Test Plan

- With the account's day at its cap, an approved planned row publishes at once (no deferral,
  no "tomorrow" line), and `daily_post_counts` is unchanged.
- A cadence story later that day behaves exactly as it would without the planned post.
- A planned publish that fails after the flip, and one resolved for retry, both leave the count
  unchanged.
- A Meta advisory still defers a planned row. That limit is Meta's, not ours.
- Every existing cap and refund test passes. Revert check on the two skips.

## Verification Checklist

- [ ] The full suite passes.
- [ ] On a Neon branch in dry run: record `daily_post_counts.count` for the account and day,
      approve a planned row, and read the same count afterwards.

## What NOT To Do

- Don't leave `cap_consumed_on` NULL on a planned `publishing` row. `055:174` rejects it.
- Don't skip the debit without also skipping the refunds, or the day's count drifts below zero.

## Context
- Source skill: forge · Area: `src/services/target/publish_cap.py`, `src/services/target/publish_pipeline.py` · Effort: M · Risk: Medium · Priority: High
