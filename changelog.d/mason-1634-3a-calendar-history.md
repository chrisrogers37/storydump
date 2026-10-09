### Added

- **The calendar shows the whole month's history, steps to the previous and next month, and opens a day (#1634, Phase 3a).** The month on screen is one read, `GET /workspaces/{ws}/intents/days`, which counts every local day of the grid in the workspace's own zone and carries each day's three newest names, so a day early in a busy month is counted as fully as yesterday, and "+N more" comes from that count rather than from a list's length. Previous and next month are links (`?month=`), and tapping a day opens its full list (`?day=`): every story it holds, with its time and its state, through the intents read's new `from` and `to` (the workspace's local days, half-open, at most 45 days apart).

### Changed

- **A workspace's stories are indexed by slot, so the calendar's month, its day view and the Overview's recent activity stop walking a workspace's whole history (#1640).** A migration adds `ix_intents_workspace_slot` on `post_intents (workspace_id, schedule_slot_at)`, over every state. #1640 proposed a partial index on the outcomes (posted, skipped and rejected), but the day view reads every state, which that index could not serve.
