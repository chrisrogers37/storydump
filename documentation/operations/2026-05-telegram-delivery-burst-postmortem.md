# Postmortem: Telegram Delivery Failure Burst (2026-05-17 → 2026-05-19)

| | |
|---|---|
| **Window** | 2026-05-17 19:20 UTC → 2026-05-19 ~14:00 UTC (~43 hours) |
| **Failures recorded** | **958** rows in `posting_history` with `posting_method='telegram_manual'` + `status='failed'` |
| **Trailing failure** | 1 row on 2026-05-23, then zero through end of investigation (2026-06-02) |
| **Symptom** | Cards never delivered to Telegram chats; queue items burned through 3 retries each then recorded as failed |
| **User impact** | Posts didn't surface for manual approval; queue backlog grew to 995 items |
| **Resolved by** | Burst self-terminated. No code change deployed during the window. |
| **Related** | Issue #467 (this), #444 (queue backlog), `documentation/planning/investigations/ig-host-routing_2026-06-02/` |

## TL;DR

For ~43 hours starting 2026-05-17 19:20 UTC, the scheduler's `_send_to_telegram` path produced **958 consecutive failures** with the generic error `"send_notification returned False"`. The actual exception text was caught and discarded inside `TelegramNotificationService.send_notification` — only the boolean return reached `posting_history.error_message`. The system has a consecutive-failure counter, but it only emits a `logger.critical` line; **there is no circuit breaker and no alert**, so the loop ran unchecked through the entire backlog.

The burst overlaps three notable events: the start of the `posting_queue` backlog (May 17), the credential refactor migrations 035/036 (May 19 19:30 / 22:39), and the first Instagram code-190 "Cannot parse access token" failures (May 19 14:10). The proximate cause of `send_notification`'s False returns cannot be recovered from `posting_history` alone — the exception details are gone.

The burst self-resolved by 2026-05-23 and has not recurred. The systemic risk — **lossy error recording + no circuit breaker** — remains and is what this postmortem proposes to fix.

## Timeline (from `posting_history`)

```
Day            telegram_manual failures      Co-occurring events
─────────────────────────────────────────────────────────────────────────────
2026-05-17    217                              posting_queue backlog starts (oldest item 19:20)
2026-05-18    602  ← peak
2026-05-19    139                              IG code-190 begins (14:10); migrations 035/036 (19:30/22:39)
2026-05-23      1
2026-05-24+     0  ← self-resolved
```

Every failure has `queue_created_at ≈ posted_at` (within minutes). These are not aged-out approvals; they are immediate delivery failures. 955 of 959 have `error_message = "send_notification returned False"`; 4 have a SQLAlchemy `Can't reconnect until invalid transaction is rolled back` rollback message.

## Code path

```
SchedulerService._send_to_telegram                        scheduler.py:384-444
  └─ claim queue item (status → 'processing')
  └─ for attempt in 1..3:
       └─ telegram_service.send_notification(queue_item_id)  ── returns bool
            │
            └─ TelegramNotificationService.send_notification     telegram_notification.py:57-177
                 └─ bot.send_photo(...)
                 └─ on any Exception (except GoogleDriveAuthError):
                      logger.error("Failed to send Telegram notification: {e}")
                      return False   ◄── exception text logged but DROPPED
  └─ if returns False:
       last_error = "send_notification returned False"   ◄── generic placeholder
  └─ if all 3 retries failed:
       _record_send_failure(queue_item, last_error)
            └─ history_repo.create(error_message=last_error)   ◄── DB gets the placeholder
            └─ _consecutive_send_failures += 1
            └─ if >= 3:  logger.critical("SYSTEMIC FAILURE ...")   ◄── log only, no halt
```

### What works

- Retries (3× with 5s backoff) — good for transient errors.
- Queue item gets marked `failed` rather than deleted — preserves evidence.
- Consecutive-failure counter is maintained.
- Application-level structured logger receives the real exception text (`logger.error("Failed to send Telegram notification: {e}")`).

### What's broken

1. **Exception text never reaches `posting_history.error_message`.** `send_notification` swallows it into `return False`, and `_send_to_telegram` substitutes the literal string `"send_notification returned False"`. Postmortem-time forensics through DB queries is impossible. The 958 rows say nothing about *why*.

2. **No circuit breaker.** `_consecutive_send_failures` is incremented and a `logger.critical` is emitted at threshold 3, but the loop keeps polling the queue and trying the next item. During the burst this means: 958 items × 3 retries each = ~2,874 Telegram API calls in 43 hours, with no backoff at the system level. A real rate-limit cascade could easily fall into this trap.

3. **Operator never notified.** Existing alerting wires (PR #447 added Telegram-bot alerts on token-refresh failure) do not fire on send-loop failures. The 958-failure burst happened silently from the user's perspective until they noticed posts weren't going out.

4. **Catch-all `except Exception`** at `telegram_notification.py:171` is too broad. It hides the actual error class (telegram.error.BadRequest, NetworkError, RetryAfter, etc.) from any downstream handler.

## Likely proximate causes (cannot confirm from DB alone)

In rough order of probability given the timing and shape of the burst:

1. **Telegram API rate-limit cascade.** The scheduler started draining a growing backlog with no per-second throttle. Once Telegram returned 429 / RetryAfter for any one chat or globally, every subsequent send returned an error → caught → returned False → retried → counted as another failure. Self-resolves once the queue stops feeding it; matches the May 24+ recovery.
2. **Transient bot/network issue** during the credential-refactor deploy window (035/036 applied 19:30 and 22:39 on May 19). Connection pool churn during the migration could explain the 4 mixed-in SQLAlchemy session-recovery errors. Less likely to span 43 hours unaided.
3. **Media-source side effect.** `send_notification` reads the file bytes via `MediaSourceFactory` → Google Drive provider. A Google API outage or token issue could surface as a generic exception (anything not `RefreshError` is caught by the `except Exception` and returned as False without rerouting). Lower likelihood given May 17 start (before any other known issue).

We can't confirm without recovering application logs from the window. Railway log retention is typically a few weeks; by 2026-06-02 these specifics are likely gone.

## Fix recommendations

These are in order of expected impact and reversibility. They're sketched here, not implemented; ship as a separate PR (or PR series) following #467.

### F1 — Propagate the real exception to `error_message` (highest impact)

Change `send_notification`'s contract so the caller gets the exception detail, not a boolean. Two equivalent shapes:

```python
# Option a: return (success, error_str)
return False, f"{type(e).__name__}: {e}"

# Option b: keep returning bool, expose last error on self
self.last_send_error = f"{type(e).__name__}: {e}"
return False
```

Update `_send_to_telegram` to read whichever is chosen and pass it through to `_record_send_failure`. The 958-row "send_notification returned False" string in the DB is the artifact we want to eliminate going forward — every future failure should carry its real reason.

### F2 — Circuit breaker at the scheduler level (highest blast-radius reduction)

When `_consecutive_send_failures` crosses a threshold (3 today already exists), don't just log — also:

1. **Pause** the send loop for `_SEND_FAILURE_BACKOFF` (e.g. 5 minutes initially, double on each subsequent trip up to a cap).
2. **Alert** the operator via the same Telegram-admin channel that PR #447's token-refresh failures use.
3. **Reset** the counter on the first success after a pause.

This converts a multi-hour 958-failure burst into "fail 3 times, pause 5 min, fail 3 more, pause 10 min, alert operator, …" — bounded and observable.

### F3 — Narrow the `except Exception` in `send_notification`

Replace the catch-all at `telegram_notification.py:171` with:

```python
except GoogleDriveAuthError:
    raise
except telegram.error.RetryAfter as e:
    # Honor Telegram's backoff hint
    self.last_send_error = f"RetryAfter: {e.retry_after}s"
    return False
except telegram.error.BadRequest as e:
    self.last_send_error = f"BadRequest: {e}"
    return False
except telegram.error.NetworkError as e:
    self.last_send_error = f"NetworkError: {e}"
    return False
except Exception as e:  # truly unexpected — log and re-raise so it surfaces
    logger.exception(f"Unexpected error in send_notification: {e}")
    raise
```

Lets known transient errors stay as soft failures (with their actual class in the message); lets genuinely unexpected exceptions fail loud so they reach the scheduler's `except Exception` and get recorded with full text.

### F4 — Structured per-failure logging

When a failure occurs, log a single line with fields the operator can grep for:

```python
logger.error(
    "telegram_send_failed queue_id=%s chat=%s error_class=%s msg=%s retry_after=%s",
    queue_item_id, chat_id, type(e).__name__, str(e),
    getattr(e, "retry_after", None),
)
```

Easier than today's `Failed to send Telegram notification: {e}` for log aggregators.

### F5 — Postmortem dashboard signal

Once F1 ships and `error_message` is no longer a single placeholder string, the dashboard can break down failures by class (RetryAfter / BadRequest / NetworkError / Other). This is downstream of issue #466 (KPI redesign).

## Out of scope for this issue

- **Cleaning up the 958 historical rows.** They are accurate evidence of attempted-and-failed deliveries; deletion would distort the record. Issue #466 covers reframing them in the dashboard so users don't read them as ongoing failures.
- **Rate-limit token-bucket / global throttle.** Worth considering if F2's circuit breaker turns out to be insufficient. Deferred until F2 ships and we have at least one new burst's data with real error classes attached.

## Action items

| | Owner | Issue |
|---|---|---|
| F1 — propagate exception text | TBD | filed |
| F2 — circuit breaker + operator alert | TBD | filed |
| F3 — narrow exception handling | TBD | filed |
| F4 — structured logging | TBD | filed |
| F5 — dashboard breakdown by error class | TBD | follow-up after F1 |

Each becomes its own PR. F1 is the prerequisite for F4 and F5.
