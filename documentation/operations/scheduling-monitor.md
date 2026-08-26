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
ExecStart=/usr/bin/python3 %h/ops/storydump/scripts/scheduling_monitor.py \
  --url https://<api-host>/health/scheduling \
  --state-file %h/.local/state/storydump-scheduling-monitor.json \
  --notify-command %h/claudlobby/lib/tg-post.sh
SuccessExitStatus=0 10
```

```ini
# ~/.config/systemd/user/storydump-scheduling-monitor.timer
[Unit]
Description=poll storydump scheduling health every 5 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
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
