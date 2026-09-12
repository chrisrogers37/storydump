---
title: "Approved but never posted — the story frame's first fetch (2026-09-11)"
type: audit
status: completed
owner: chris
created: 2026-09-11
---

## Summary

At 12:37 ET on 2026-09-11 the owner approved four stories at once via Telegram taps; two posted, two failed at Meta's container call with error 9004 / subcode 2207052 ("the media could not be fetched from this uri"), and the failed cards kept reading "✅ Approved" with no notification. The same failure hit one post the night before and one at 16:02 ET: 4 of the last 7 publishes. A live re-test that evening (8 taps, 6 approvals) posted 6 of 6, including the three files that had failed earlier — the failure is transient, not the file.

## Evidence

- Production rows (read-only via `railway run … psql`): `provider_operations` container creates that succeeded took 4.5–7.3 s (Meta waiting for the on-demand frame); the failed ones settled in 0.34–0.55 s, created 0.5–1.5 s after the Cloudinary upload — Meta's fetch got an immediate non-image answer. No cap deferrals (`daily_post_counts` 2/20 at the time, no `cap_deferred` audit rows), the worker idle and healthy, no rate limiting, no pause/dry-run, credentials active, zero `notification` rows that day.
- Worker/API logs (Railway GraphQL for the removed deployment): six taps all `outcome=executed`, four pipelines run within 3–8 s of their taps, no errors beyond the pre-3a "fenced during finalize" noise (#1288 fixed it). Today's deploys (20:20, 20:55, 21:37 UTC) postdate the 16:37 UTC event.
- Cloudinary probes: the failed originals are small JPEGs (1170–1632 px wide, 0.18–0.64 MB); their story frames serve valid JPEG now (cached 0.2 s; a fresh derivation 2.3–3.1 s, n = 17); 13/13 throwaway uploads through the pipeline's own path served a JPEG on the first fetch after 2.4–3.1 s; 5/5 with the frame derived eagerly at upload served in 0.11–0.25 s.
- Meta docs: 9004/2207052 = "The media could not be fetched from the supplied URI. Advise the app user to make sure the URI is valid and publicly available."
- The card: `sweep_settled_cards` (`prompts.py`) rewrites only `approval_prompt` rows still pending/sending/sent/ambiguous; the tap's supersede had already moved them to `superseded`, and `_fail_terminal` wrote only `last_error` + the refund. The design's promised visibility (`06` notices table: "the web queue shows the intent approved with its next attempt time") was never built.
- Legacy reference: the old flow (`src/services/core/telegram_autopost.py`, `instagram_api.py`) handed Instagram a public on-demand URL too, but its container call came 1–3 s after the upload (a caption edit, an account lookup and a quota call in between), and on any failure it edited the caption within seconds with Retry/manual buttons and put a permanent-reject lock on a file Meta could not process.

## Root cause

1. The pipeline calls Meta within ~0.5 s of the upload; the story frame is derived on demand and a fresh asset's first fetch can answer with an error image; Meta reports "could not be fetched" (9004/2207052). Confidence: high (timings, probes, the same files posting later).
2. 9004 is treated as permanent on the first attempt: the post fails, the cap is refunded, no retry.
3. A terminal failure (and a poisoned ladder) writes no notification and cannot rewrite a card the tap already superseded.

Not the cause, each ruled out by the rows: the daily cap, the worker, rate limits, pause/dry-run, credentials, the media files, today's deploys.

## Fixes (built in the same PR; see `01_first-fetch-and-failure-reporting.md`)

1. Derive the story frame eagerly at upload (the store mints the asset id so the underlay can name it); verify the delivery URL serves media before the container call (20 s budget; not serving → the ladder, never Meta).
2. ~~A first 9004 at the container step is retried once on the ladder; a second is the file's own answer.~~ Corrected 2026-09-12: every 9004 rides the ladder (see 4).
3. Every terminal outcome is said at once: the card restated by reference and one notification per push binding, in the flip's transaction; a poisoned post likewise, with the reconciler's six-hour notice latched off. ~~A file Meta twice could not take gains a permanent `unsupported` lock~~ (withdrawn 2026-09-12).
4. Follow-ups: a Retry action on the card and in the web Queue (the legacy had one) — **built 2026-09-12** as the review card's three resolutions (`resolve_review`: post again / it posted / give up, by the workspace member); **the "second 9004 = the file" rule was wrong** (2026-09-12 live test: two fetch failures sixty seconds apart on a frame that served a valid JPEG; a 9004 now always rides the ladder and never locks the file); the burst-deferral and Queue-visibility gaps (#1279 area) — filed separately.

## Also observed (not this incident)

Phase 3a's Telegram pacing worked live: 14 card edits in two minutes hit the chat's 18/min budget, Telegram answered 429, the sender wrote the durable hold, rescheduled itself 31 s out and drained the rest.
