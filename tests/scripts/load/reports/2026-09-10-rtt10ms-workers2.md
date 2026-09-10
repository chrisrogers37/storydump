# Load harness report — 2026-09-10 22:13 UTC

Phase 2 of the 2026-09-09 tap plan, step 6 (`02_api-under-load.md`). The client
delivers at most `max_connections` = 10 taps at once and
redelivers a non-2xx after 0.5, 1, 2, 4 s.

**Which latency is which.** `answer` = tap → `answerCallbackQuery` at the fake, from
the FIRST DELIVERY ATTEMPT, redelivery included — the plan's SLO number (p95 < 2 s), a
DELIVERY-SIDE figure. `wait` = the same answer measured from the TAP ITSELF, the
harness's queue included — the user's spinner when a burst exceeds `max_connections`;
`answer_late` counts answers later than 30 s after the tap, which Telegram
no longer accepts. `queue_wait` = how long the tap sat before its first attempt.
`route` = the 200's round trip. `strip` / `outcome` = the card's keyboard gone / its
outcome line landed, for the tap that flipped the card, within the scenario's settle
window (reported, not judged; the sender's throughput is phase 3's). `xact_per_tap` =
database commits per tap from `pg_stat_database`. Loopback numbers swing several-fold
run to run; the injected-RTT run is the one to read.

## Run

- RTT: loopback + 10 ms injected per direction on the database socket (≈ twice that per round trip)
- Database: 15.19 (Debian 15.19-1.pgdg13+2); `synchronous_commit` = off; `max_connections` = 100
- API: pool {"size": 10, "overflow": 0, "timeout_s": 1.0, "checked_out": 0, "checked_out_peak": 10}; ingress_workers = 2

## Scenarios

### `taps_1000_across_50_workspaces` — 50 workspaces × 20 cards, one tap each; 1,000 taps offered within 1 s; delivered at max_connections

| number | value |
|---|---|
| taps | 1000 |
| five_xx | 0 |
| refused_503 | 0 |
| busy | 0 |
| redeliveries | 0 |
| unanswered | 0 |
| answer_late | 490 |
| by_outcome | executed: 1000 |
| answer_p50_s | 0.569 |
| answer_p95_s | 0.619 |
| answer_max_s | 1.672 |
| queue_wait_p95_s | 54.418 |
| wait_p95_s | 54.983 |
| wait_max_s | 57.753 |
| route_p95_s | 0.623 |
| strip_p95_s | 0.620 |
| outcome_p95_s | 44.773 |
| outcome_landed | 19 |
| flips | 1000 |
| audit_rows | 1000 |
| pending_peak | 990 |
| pool_peak | 4 |
| xact_commit | 1244 |
| xact_per_tap | 1.240 |
| rows_written | 9552 |

> fake calls during the scenario: 2058

> delivered at max_connections = 10

### `double_tap_one_card` — 1 workspace, 1 card; 50 taps offered within 100 ms

| number | value |
|---|---|
| taps | 50 |
| five_xx | 0 |
| refused_503 | 0 |
| busy | 0 |
| redeliveries | 0 |
| unanswered | 0 |
| answer_late | 0 |
| by_outcome | answered: 49, executed: 1 |
| answer_p50_s | 1.712 |
| answer_p95_s | 2.022 |
| answer_max_s | 2.375 |
| queue_wait_p95_s | 7.112 |
| wait_p95_s | 8.823 |
| wait_max_s | 9.163 |
| route_p95_s | 2.024 |
| strip_p95_s | 0.566 |
| outcome_p95_s | None |
| outcome_landed | 0 |
| flips | 1 |
| audit_rows | 1 |
| pending_peak | 40 |
| pool_peak | 5 |
| xact_commit | 134 |
| xact_per_tap | 2.680 |
| rows_written | 215 |

> exactly one flip; the other 49 answered with the card's state

> fake calls during the scenario: 118

> delivered at max_connections = 10

### `taps_across_many_cards` — 10 workspaces × 20 cards, one tap each, all at once (20/workspace, inside F12's 120/min)

| number | value |
|---|---|
| taps | 200 |
| five_xx | 0 |
| refused_503 | 0 |
| busy | 0 |
| redeliveries | 0 |
| unanswered | 0 |
| answer_late | 0 |
| by_outcome | executed: 200 |
| answer_p50_s | 0.570 |
| answer_p95_s | 0.672 |
| answer_max_s | 1.341 |
| queue_wait_p95_s | 10.983 |
| wait_p95_s | 11.553 |
| wait_max_s | 12.164 |
| route_p95_s | 0.674 |
| strip_p95_s | 0.673 |
| outcome_p95_s | None |
| outcome_landed | 0 |
| flips | 200 |
| audit_rows | 200 |
| pending_peak | 190 |
| pool_peak | 9 |
| xact_commit | 271 |
| xact_per_tap | 1.350 |
| rows_written | 1864 |

> fake calls during the scenario: 420

> delivered at max_connections = 10

### `one_slow_chat` — 10 workspaces × 20 cards; the fake answers every call for chat -400000000 after 5 s, the others at once

| number | value |
|---|---|
| taps | 200 |
| five_xx | 0 |
| refused_503 | 0 |
| busy | 0 |
| redeliveries | 0 |
| unanswered | 0 |
| answer_late | 0 |
| by_outcome | executed: 200 |
| answer_p50_s | 0.592 |
| answer_p95_s | 0.637 |
| answer_max_s | 1.098 |
| queue_wait_p95_s | 19.143 |
| wait_p95_s | 19.747 |
| wait_max_s | 20.370 |
| route_p95_s | 4.603 |
| strip_p95_s | 2.594 |
| outcome_p95_s | None |
| outcome_landed | 0 |
| flips | 200 |
| audit_rows | 200 |
| pending_peak | 190 |
| pool_peak | 9 |
| xact_commit | 309 |
| xact_per_tap | 1.540 |
| rows_written | 1980 |

> edit-landed criterion for the OTHER chats rides phase 3a's sender hold (`03_worker-throughput.md`); reported here, judged there

> fake calls during the scenario: 428

> delivered at max_connections = 10

> other chats: answer p95 = 0.620 s over 180 taps

### `taps_1000_at_twenty_connections` — 50 workspaces × 20 cards, one tap each; 1,000 taps offered within 1 s; delivered at TWENTY connections against a pool of ten

| number | value |
|---|---|
| taps | 1000 |
| five_xx | 0 |
| refused_503 | 0 |
| busy | 1 |
| redeliveries | 0 |
| unanswered | 0 |
| answer_late | 403 |
| by_outcome | busy: 1, executed: 999 |
| answer_p50_s | 1.073 |
| answer_p95_s | 1.176 |
| answer_max_s | 1.544 |
| queue_wait_p95_s | 46.483 |
| wait_p95_s | 47.510 |
| wait_max_s | 50.043 |
| route_p95_s | 1.187 |
| strip_p95_s | 1.186 |
| outcome_p95_s | None |
| outcome_landed | 0 |
| flips | 999 |
| audit_rows | 999 |
| pending_peak | 971 |
| pool_peak | 6 |
| xact_commit | 1182 |
| xact_per_tap | 1.180 |
| rows_written | 9331 |

> the boundary's own scenario: the only run where the pool can saturate

> fake calls during the scenario: 2053

> delivered at max_connections = 20

