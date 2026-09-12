---
title: "Fix: the story frame's first fetch, and saying it when a post fails"
type: plan
status: completed
owner: chris
created: 2026-09-11
---

## Summary

Make the story frame exist and serve before Meta is told to fetch it, treat a first "could not be fetched" as a retry rather than a verdict, and say every terminal outcome on the card and to the workspace at once. Built as one PR on the target flow (the legacy's behaviours adopted onto the new machinery).

## Evidence

See `00_INVESTIGATION.md`.

## Implementation Plan

### Dependencies

None (main after #1291).

### Blocks

The Retry action follow-up — built 2026-09-12: the parked card carries 🔁 Post again · ✅ It posted · 🚫 Give up (`resolve_review`), and the web Queue the same three.

### Steps

1. `src/services/target/transit.py`: `upload` mints `ws/<workspace>/<20 hex>` as `public_id` and passes `eager=[{transformation: story_transformation(ref), format}]`, `eager_async` false for images, true for videos; `ready(ref, media_kind, budget_s=20, sleep=)` polls an injectable probe (`url -> (status, content_type)`; default an egress-floored ranged GET of the delivery URL) every 1 s until 200/206 with an image/video type.
2. `src/services/target/publish_pipeline.py`: at `transit_uploaded`, `ready()` first — not serving → `_retry_or_poison(TransitNotReady)`; on `MetaTerminalError` 9004 with `generation < 2` → `_retry_or_poison(... first_fetch)`; otherwise `_fail_terminal(lock_media=True)` *(superseded 2026-09-12: every 9004 → `_retry_or_poison(... fetch_failed)`; `lock_media` and the `unsupported` insert are gone)*. `_fail_terminal` then runs `_say_outcome` (restate by ref + one notification per binding) in the flip's transaction. `_retry_or_poison`'s exhausted branch calls `_say_outcome("review_required")` and sets `last_error.evidence.customer_notified`.
3. Tests first: `test_transit.py` (eager options, readiness polling), `test_l5_pipeline_gate.py` `TestTheFirstFetch` (not serving → retry, never Meta; first 9004 → retry → posted; second 9004 → failed + lock + notice + card *(superseded 2026-09-12: a second 9004 rides the ladder; exhausted → review card, no lock)*; file gone → notice; poison → notice + latch + card).

## Test Plan

Unit + the pipeline gate above; the full suites.

## Verification Checklist

- [x] `pytest tests/src/services/target/test_transit.py tests/scripts/test_l5_pipeline_gate.py -q` green.
- [x] A production publish after the deploy: the container create settles in under 2 s (the frame already derived) — read from `provider_operations`.
- [ ] The next failed publish, if any, shows a `notification` row and a restated card within the minute.

## What NOT To Do

Do not lock media on a FIRST 9004 (the race, not the file). ~~Do not retry a second 9004 (the file).~~ **Corrected 2026-09-12:** a second 9004 is not the file's answer either — photo-output (105) failed twice, sixty seconds apart, in under half a second each, on a frame that served a valid JPEG to every client tried (HEAD, GET, Meta's user agent, encoded commas); a sibling from the same batch posted on its retry. A 9004 rides the ladder every time; the ladder's end is the workspace's review card (`resolve_review`), and nothing locks the file on Meta's word. Do not notify on a retry in progress.

## Context

area: publish pipeline, transit · effort: S · risk: low · priority: P0
