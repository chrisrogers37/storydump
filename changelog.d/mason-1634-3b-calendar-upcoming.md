### Added

- **The calendar shows the days ahead: planned stories on their days, and the slots the cadence will open, counted and labelled as predicted (#1634, Phase 3b).** The month reads `GET …/upcoming` for its whole grid, so a future month shows what is coming there too. A story a person planned that is still scheduled comes from that read alone, on the day the API places it on; the planned read now brings only the planned stories past their slot. Each day ahead shows "N predicted": a count, because a slot has no file until it draws one, and never as a scheduled story. A day opened from its cell lists its predicted slots among its stories in time order, each at its time in the workspace's zone, with the account it posts for and no picture. When either list was cut at its limit, every day from the cut on says "Not all shown", the month says from which day, and an opened day says it may hold more predicted slots. A day's screen-reader label counts what the day draws ("1 planned, 6 predicted") rather than "0 posted". The sample workspace counts its scheduled stories the same way.

### Changed

- **The calendar hands the browser a count of predicted slots per day and each story's picture fields, not whole rows (#1634).** The upcoming read can return 500 planned stories and 4,500 slots for a month; the page counts the slots per day on the server and passes each story's five picture fields, so a busy month's page stays small.

### Removed

- **The calendar's old predicted strip: the slot plan's scheduled stories, labelled by their folder (#1634).** Those are stories the cadence has already created, which the queue lane draws, so only the slots the cadence will open are called predicted. `SCHEDULED_STATES`, which only that strip read, is gone with it.
