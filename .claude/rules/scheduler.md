---
paths:
  - "src/services/target/scheduler*"
  - "src/services/target/category_mix.py"
---

# Scheduler (the target tier)

The legacy JIT scheduler (`src/services/core/scheduler*`: the polling loop,
`is_slot_due()`, `process_slot()`, `POSTING_HOURS_*`, the 30-day TTL lock) was
deleted in the legacy tear-out (#1216). What runs is the target tier's clock
(`src/services/target/scheduler.py`) and its slot plan.

## Folder selection (`scheduler.execute_plan_slot` + `category_mix.weights`)

The draw is weighted over the CONNECTED FOLDERS that have eligible media —
explicit ratios, automatic folders in proportion to their files (capped at the
smallest explicit weight), Off never — then oldest-first within the drawn
folder; the whole pool minus Off when nothing weighted has media.

See `documentation/operations/reading-the-ledger.md` for the clock, the slots
and the wait classes as the ledger records them.
