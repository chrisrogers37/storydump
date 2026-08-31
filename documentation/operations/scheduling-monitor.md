# Scheduling-outage monitor

`scripts/scheduling_monitor.py` polls `GET /health/scheduling` and raises a FLEET
ALERT when the schedule cursor stops advancing. It is the caller that #1090 F1's
detector never had (#1099).

## Why it runs outside the app

Two total scheduling outages ran 18 and 19 hours with **no alert**, both found by
someone looking for something else. The detector was built without a caller on
purpose — an alert whose *sending* is performed by the system it monitors cannot
fire when that system is down — which left the class exactly where it started: a
detector nobody polls raises no alert either.

So the poller runs on the **fleet host**, and the alert path shares nothing with
its subject: different machine, process, network path, clock and notification
channel. It touches none of the jobs table, `ck_jobs_kind`, `fn_clock_tick`, the
outbox, `channel_bindings`, the worker, or the app's own notification routing —
which has no writer, so an alert delivered there would vanish silently.

## The four states

| reading | state | what happens |
|---|---|---|
| `200`, `accounts_active == 0` | **`no-signal`** | says so once, then quiet; re-states weekly. **Never an alert, never an all-clear.** |
| `200`, `accounts_active > 0`, lag ≤ threshold or null | `healthy` | quiet; announces RECOVERED / SIGNAL ACQUIRED on entry from another state |
| `200`, `accounts_active > 0`, lag > threshold | **`stalled`** | FLEET ALERT on the **first** reading; repeats every 6h |
| anything else — non-200, timeout, malformed body | **`unreachable`** | FLEET ALERT on the **second consecutive** reading; repeats every 6h |

### `no-signal` is the primary case, not an edge case

Measured on production 2026-08-26: `ig_accounts` has **no rows at all**,
`workspaces` 0, `media_sources` 0, and the legacy tables are absent entirely. The
tier is live — `jobs` carried 27 rows, the clock was minting — but it has **zero
destinations**.

`scheduling_lag` reads only `ig_accounts WHERE state = 'active'`, so with no rows
`stalled` is 0 and `max_lag_seconds` is null **structurally**. A poller reporting
"nothing is stalled" from that is a green light wired to nothing, and it would
have stayed green through the entire 19-hour outage had the estate been empty
then.

Hence the distinction the module is built around:

- **nothing is late** → genuinely healthy
- **nothing exists to be late** → `no-signal`, and it says so

Those have opposite remedies and must never render the same.

### Why `stalled` and `unreachable` confirm differently

Not two tunings of one knob. A lag past the threshold already *contains* its
duration — 600s of non-advance is what the number means — so the observation is
the sustain and confirming it only adds delay. A failed request contains no
duration at all: one is indistinguishable from a dropped packet.

## The threshold

Default **600s**: 40× the clock cadence (`work_loop.py`
`clock_interval_seconds = 15.0`) and 1/114th of the shorter of the two outages.
Polling every 5 minutes puts worst-case detection at ~15 minutes against 18–19
hours.

The margin is deliberate. The gap between a healthy 15-second lag and a 19-hour
outage is four orders of magnitude; a threshold near the noise floor buys nothing
and costs false alarms.

## Deploying it

Stdlib only — **no venv, no dependencies**. Verified on system `python3` 3.11.

```ini
# ~/.config/systemd/user/storydump-scheduling-monitor.service
[Unit]
Description=storydump scheduling-outage monitor

[Service]
Type=oneshot
# THE NOTIFY COMMAND NEEDS AN ENVIRONMENT AND A systemd --user UNIT HAS ALMOST
# NONE. This is not optional decoration: a `oneshot` under `systemd --user`
# inherits neither the shell profile nor `bot.conf`, so a notify command that
# reads its destination and its credential from the environment finds neither.
#
# TWO variables, with UNRELATED causes -- and the second was found only by
# RUNNING the notify path, after the first had been applied and the unit looked
# fixed. Measured on the fleet host 2026-08-31:
#
#   (1) no chat id     -> tg-post.sh exits 2, before any network call.
#   (2) chat id set,    -> tg-post.sh exits 3: "send REJECTED -- message NOT
#       wrong token         delivered (ok=<none>; error: Unauthorized)".
#
# (2) is the one that reads as fixed. With no TELEGRAM_STATE_DIR, tg-post.sh
# falls back to the GENERIC channel dir, whose token is not authorised for the
# group -- so a token resolves, and resolving is not the same as working.
# Probing the file for a token line cannot tell these apart; only a real send
# can. Point TELEGRAM_STATE_DIR at a channel dir whose token is authorised for
# the destination chat.
#
# NOT `EnvironmentFile=bot.conf`. Every line there is `export KEY=value`, and
# systemd does not strip the keyword -- it would create a variable literally
# named `export TELEGRAM_GROUP_CHAT_ID` (measured: 53 of 60 lines).
Environment=TELEGRAM_GROUP_CHAT_ID=<the operator group chat id>
Environment=TELEGRAM_STATE_DIR=<a channel dir whose token is authorised there>
ExecStart=/usr/bin/python3 %h/ops/storydump/scripts/scheduling_monitor.py \
  --url https://<api-host>/health/scheduling \
  --state-file %h/.local/state/storydump-scheduling-monitor.json \
  --notify-command %h/claudlobby/lib/tg-post.sh
SuccessExitStatus=0 10
```

**Verify the notify path by SENDING, before trusting the unit.** The monitor
cannot tell you its pager is broken until it already has something urgent to
say, which is the worst possible moment to find out — and the second failure
above proves inspection is not enough, because a present token and an
authorised token look identical in the file.

```bash
# Exactly the environment systemd gives it. rc=0 means DELIVERED; rc=2 is a
# missing chat id, rc=3 is a rejected send (wrong or unauthorised token).
systemd-run --user --wait --collect --pipe --quiet \
  -p Environment=TELEGRAM_GROUP_CHAT_ID=<id> \
  -p Environment=TELEGRAM_STATE_DIR=<dir> \
  %h/claudlobby/lib/tg-post.sh "scheduling-monitor notify probe"; echo "rc=$?"
```

`--notify-command` is given the message as its single argument. It is
**configuration, not code**: the fleet path lives in the unit, so this repository
holds no fleet-specific path.

Exit codes: `0` nothing to say · `10` spoke · `11` had something to say and could
not deliver it. `11` is deliberately a failed unit — a monitor cannot page about
its own paging failure, but it can refuse to record that it spoke (so the next
poll retries) and let the supervisor log the failure.

`--status` prints the last recorded state without polling.

### Operational dependency: a dedicated checkout

The timer must point at a **pinned checkout used by nothing else**.

- **Not a bot's working tree.** Those get `git checkout -b` constantly; a branch
  switch would silently change or break production monitoring.
- **Something must keep it current**, or the script drifts from the endpoint —
  the exact failure keeping them in one repository was meant to prevent,
  reintroduced through the deployment instead.
- **A missing checkout must fail loudly at enrollment**, not leave a timer firing
  into nothing.

## Bound: this cannot be validated against real traffic yet

Nobody has run the target tier end to end; sign-in needs a real Google account,
and production has no workspaces and no destinations. Every path here is
exercised against captured payloads, mutation checks, and the live endpoint's
`no-signal` answer — but **the `stalled` path has never seen a real stalled
cursor.**

That is a bound on this work, not a defect in it. A later reader must not mistake
*tested* for *seen in production*. The first real destination is what turns this
from a monitor that is correct into a monitor that is proven.
