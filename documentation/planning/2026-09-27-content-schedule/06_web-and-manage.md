---
title: "[plan] Phase 6: the web — schedule, mark and manage planned items — content schedule"
type: plan
status: draft
owner: astrid
created: 2026-09-27
tags: [storydump, content-schedule, web, landing, queue, calendar, issue-1413]
repos: storydump
---

# Phase 6 — The web: schedule, mark, manage

## Summary

Part of #1413. Size: M. The dashboard gets a Schedule action on a media item. Planned rows are
marked in the Queue and on the calendar ("Scheduled by <name>"), and a planned row that has not
been served yet can be rescheduled or cancelled from the Queue. Everything goes through phase 5's
verbs. The calendar's own editor (drag-to-move and creating from the grid) stays with #181.

## Evidence

- The Queue reads non-terminal intents (`landing/src/app/(dashboard)/dashboard/queue/page.tsx:4`),
  and the calendar plots `schedule_slot_at` (`landing/src/app/(dashboard)/dashboard/media/calendar/page.tsx:90-96`).
- The intent types to extend are `landing/src/lib/intents.ts` and
  `landing/src/lib/dashboard-payloads.ts`.

## Implementation Plan

### Dependencies
Phase 5 (and so phase 1, for cancel).

### Blocks
None.

### Steps
1. **Types:** add `origin` and `scheduled_by` to the intent types.
2. **Media Library:** a "Schedule…" action on an item, with an account picker and a date and time
   shown in that account's zone. A `locked` refusal shows its reason, and when the lock is
   overridable a confirmation re-sends the verb with `override_locks`.
3. **Queue:** a planned badge ("Scheduled by <name> · <time>"), a "Scheduled" filter, and
   Reschedule and Cancel actions on planned rows still in `scheduled`.
4. **Calendar:** planned rows drawn distinctly from cadence rows.
5. Add a `CHANGELOG.md` entry.

## Test Plan

- Unit tests for the badge, the filter and the lock-override confirmation, alongside the existing
  Queue and calendar tests.
- API route tests for the three verbs through the web adapter, including the `locked` status
  mapping.
- A manual pass on a Vercel preview against a Neon branch: schedule, reschedule and cancel an
  item, and see it on the calendar.

## Verification Checklist

- [ ] The landing test suite and build pass.
- [ ] On the preview, a scheduled item shows its badge in the Queue and on the calendar, and
      Cancel moves it to *Cancelling* and then off the Queue.

## What NOT To Do

- Don't compute the account's local time in the browser from the viewer's zone. Show the
  account's zone, which is the one the server resolves against.
- Don't build the calendar editor here (#181).

## Context
- Source skill: forge · Area: `landing/src/` · Effort: M · Risk: Low · Priority: High

## As built

- **6a, #1535:** the Media library's Schedule… (step 2).
- **6b:** steps 1, 3 and 4. The API needed no change.
  - **Types.** The web `Intent` carries the five keys the API already served: `origin`,
    `scheduled_by_user_id`, `scheduled_by`, `tz` and `miss_reason`.
  - **Queue.** The badge reads "Planned by <name>" rather than "Scheduled by <name> · <time>":
    "planned" is the word 6a's copy uses beside "a regular slot", and the row already shows the
    time. The "Scheduled" filter is a Planned view at `?origin=planned`, which the API filters,
    so it stays exact past the page limit. Reschedule… sends `reschedule_item`, keyed per
    submission; Cancel sends `cancel`, keyed on the intent and confirmed first. Both are offered
    on planned stories in `scheduled` only.
  - **Calendar.** A planned story is drawn as an outline in the queue's colour, and the
    predicted strip reads `origin=cadence`, so it no longer shows planned stories as
    predictions.
  - **Changelog.** A fragment in `changelog.d/`: the repository moved to fragments after this
    plan was written.
- **Found, not fixed here:** a cadence story in `scheduled` is drawn twice on the calendar, in
  the queue lane and as a prediction, because both of the page's reads include `scheduled`.
