### Added

- **The calendar shows the whole month's history, steps to the previous and next month, and opens a day (#1634, Phase 3a).** The month on screen is one read, `GET /workspaces/{ws}/intents/days`, which counts every local day of the grid in the workspace's own zone and carries each day's three newest names, so a day early in a busy month is counted as fully as yesterday, and "+N more" comes from that count rather than from a list's length. Previous and next month are links (`?month=`), and tapping a day opens its full list (`?day=`): every story it holds, with its time and its state, through the intents read's new `from` and `to` (the workspace's local days, half-open, at most 45 days apart).

### Changed

- **The outcomes are indexed by slot, so the newest-first and day-at-a-time reads stop walking a workspace's whole history (#1640).** A migration adds `ix_intents_history_slot`, a partial index on `post_intents (workspace_id, schedule_slot_at)` for posted, skipped and rejected stories. It covers all three outcomes, not posted alone, because the reads that ship ask for all three: the calendar's month asks for posted, and the Overview's recent activity and the history tab for all three. The month read spells its states into its SQL, so the planner can prove the index's predicate under any plan.
