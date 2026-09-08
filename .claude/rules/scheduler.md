---
paths:
  - "src/services/core/scheduler*"
---

# Scheduler Algorithm (JIT)

The scheduler polls on a loop. Each tick, `is_slot_due()` checks if enough time has elapsed since the last post. If due, `process_slot()` selects media on-demand.

## Selection Logic (priority order)

1. **Filter eligible**: `is_active = TRUE`, not locked, not already queued, matches target category
2. **Sort by**: `last_posted_at ASC NULLS FIRST` → `times_posted ASC` → `RANDOM()`
3. **Slot timing**: `interval_hours = (POSTING_HOURS_END - POSTING_HOURS_START) / POSTS_PER_DAY`
4. **Folder selection** (target tier, `scheduler.execute_plan_slot` + `category_mix.weights`): the draw is weighted over the CONNECTED FOLDERS that have eligible media — explicit ratios, automatic folders in proportion to their files (capped at the smallest explicit weight), Off never — then oldest-first within the drawn folder; the whole pool minus Off when nothing weighted has media (legacy tier: `_pick_category_for_slot()` by subfolder name)

## After Posting

- Create 30-day TTL lock automatically
- Increment `times_posted`
- Update `last_posted_at`
