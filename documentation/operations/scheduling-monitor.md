# Scheduling monitor

`scripts/scheduling_monitor.py` polls `GET /health/scheduling` and pages a human
when scheduling stops advancing or the worker stops finishing work. It runs on a
separate host, on a timer, and remembers what it said so that it speaks once per
change and repeats on a schedule.

## Why it is a separate poller

`GET /health/scheduling` (`src/api/routes/health.py`) answers a question and
raises nothing. It is unauthenticated, so it gives aggregates only (counts and
ages, never a workspace, an account or a handle), and it answers 503 when it
cannot read, so a poller can tell "scheduling is fine" from "I could not look".

An alert sent by the system it watches cannot fire when that system is down. So
the alert path is this poller, on a separate host that shares nothing with its
subject: a different machine, process, network path, clock and notification
channel. It mints no job and writes nothing to the database, so a failure of the
API, the worker or the outbox cannot silence it.

The script classifies each reading with
`storydump_cli/health_classify/scheduling.py`, the same classifier
`storydump health` uses, and keeps only the polling: fetch, decide and announce,
the state file and the notify command. `storydump health` judges the delivery
axis below with the same classifier; the commit split is the script's own. It
needs no install: it runs on the system Python from a checkout.

## Two axes

| Axis | Reads | Default | Flag |
|---|---|---|---|
| Cursor | stalled destinations and the worst lag | `DEFAULT_STALL_THRESHOLD_S`, 600 s | `--stall-threshold` |
| Worker, staleness | age of the last finished system job | `DEFAULT_WORKER_STALE_S`, 600 s | `--worker-stale-threshold` |
| Worker, backlog | how late the worst unclaimed due system job is; catches a stuck lane that another kind's successes hide from the age | `DEFAULT_WORKER_OVERDUE_S`, 900 s | `--worker-overdue-threshold` |

The defaults are constants in the classifier. The cursor axis sees nothing while
no destination is active: with none, nothing can stall. The worker axis has no
baseline until a system job has finished once, but system jobs run on a cadence
whatever the estate holds, so it keeps watching an empty estate. It reads what
the worker has already finished or left unclaimed and alarms on the absence of
it, so a dead worker cannot suppress the signal by dying.

The cursor threshold is forty times the clock's 15-second tick, so only a
sustained lag counts. The staleness age is that of the freshest success, so the
fastest recurring system kinds decide how stale a healthy worker can read:
`reap_expired` and `reconcile_ambiguous`, which the clock mints every 60 s
whatever the estate holds. 600 s is ten of those beats, and
`tests/src/test_worker.py` ties the threshold to them: it fails when three beats
of the fastest recurring kind (two plus one of slack) no longer fit inside it.
Slow or retire those beats and the threshold must rise with them.

## States

A reading becomes one state. `worker-down` is decided first, above every cursor
reading: a dead worker explains a stalled cursor, and with no destination active
the cursor axis cannot see it at all.

| State | Reading | What happens |
|---|---|---|
| `worker-down` | HTTP 200, and no system job has finished for longer than the staleness threshold or a due one has sat unclaimed past the backlog threshold, whatever the cursors say | pages on the first reading: the endpoint answered and said work has stopped |
| `stalled` | 200, a destination active, worst lag above the cursor threshold | pages on the first reading: the lag already contains its duration |
| `unreachable` | anything else: a non-200 status, a timeout or network failure, a body that is not a JSON object, a missing or mistyped field | pages after two consecutive readings: one failed request looks like a dropped packet |
| `worker-unknown` | 200, no destination active, and no system job has ever finished | a notice, repeated weekly |
| `no-signal` | 200, no destination active, and the worker provably alive | a notice, repeated weekly |
| `healthy` | 200, a destination active, lag within the threshold or null, the worker not down | silent |

While a fault lasts, its page repeats every 6 hours, so a long outage never reads
as resolved. The repeat clock runs within one state: a different state is
announced as soon as its own rule is met.

The notices are neither alerts nor all-clears. `no-signal` means the cursor axis
has nothing to watch; `worker-unknown` is worse, because neither axis can answer.
Both repeat weekly, since "we have no coverage" must not fade because it was said
once. They never render like `healthy`: "nothing is late" and "nothing exists to
be late" have opposite remedies.

An estate that HAD destinations and now has none is not that empty estate.
Either notice then says `NO ACTIVE DESTINATIONS`, with the most destinations any
reading has shown, because nothing will be scheduled or posted until one is
re-authorized or resumed. That one repeats daily.

A fault a human was told about, followed by a reading that can see nothing, is
closed in the notice's first words:
- after `worker-down` or `unreachable`, `RECOVERED from <state>`;
- after `stalled`, "the stall is no longer observable", never `RECOVERED`,
  because pausing the only workspace takes away the stall's evidence, not the
  stall.

`healthy` is silent except for `RECOVERED`, after an alert, and `SIGNAL ACQUIRED`,
after a notice. Both key on what a human was last told, not on the last reading,
so one `unreachable` reading that never paged ends in silence.

## Delivery, its own axis

Each poll also reads `/health/delivery`: the one beside `--url`, or
`--delivery-url`. A `--url` that does not end in `/health/scheduling`, with no
`--delivery-url`, is refused at start (exit 2), so a delivery surface nothing
watches is a failed unit, never a quiet one. Cards can fail while posts land,
so delivery keeps its own surface and is judged with its own state, by
`storydump_cli/health_classify/delivery.py`. One axis never masks the other.

- **Pages, failing:** any send in the last hour that failed or is ambiguous,
  the endpoint's `failed_or_ambiguous`, pages with its count, the hour's sends
  and the alerting classes with their provider codes. A deferral (a 429) is
  never a failure.
- **Pages, unreadable:** a reading that is not a whole one (a non-200, not a
  JSON object, a missing or mistyped count) is `unreachable`, and pages on the
  second in a row, as an unreachable scheduling endpoint does. A non-200 names
  the body's `reason` when it gives one, such as `HTTP 503 (statement_timeout)`.
- **One cause, one page:** when `/health/scheduling` could not be fetched in
  the same poll (no answer, or a 5xx), the API is down and the scheduling axis
  pages it. An unreadable delivery reading in that poll counts toward nothing,
  so once the API answers, delivery still needs two readings of its own.
- **Quiet:** an hour with nothing sent and nothing failed reads `quiet`, not
  `delivering`: it is not an hour that delivered, and not a fault, so it never
  pages.
- **Repeats:** every 6 hours while it lasts. `RECOVERED` once an hour passes
  with no failure, or once the surface reads again after an unreadable page; a
  failing hour after that page pages failing instead.

## A version split

The scheduling reading carries the API's commit (`api_commit`) beside the
worker's (`worker.commit`). The two services deploy one after the other, so they
disagree for a deploy's length.

- **Pages:** a split that lasts `SPLIT_AFTER_S` (30 minutes) pages with both
  commits. A deploy then reached one service and not the other.
- **Repeats:** daily while it lasts, and `VERSION SPLIT RESOLVED` once the
  commits match.
- **Never said:** a split that ends inside the window.
- **Changes nothing:** a reading without both commits, such as an unreachable
  one or rows written before the worker stamped its commit.

## A broken instrument is not a clean bill of health

The classifier reads a missing or mistyped field as `unreachable`, never as
`healthy`: the counts (`stalled`, `accounts_active`, `succeeded_ever`,
`overdue_ready`) must be whole numbers, not booleans, and the lag and the two
worker ages whole numbers or null. Only `max_lag_seconds` may be absent, and it
reads as null.

A payload without the `worker` block is `unreachable` whatever `accounts_active` says:
the route always serves the block, so its absence means a broken instrument.

## Running it

| Flag | Meaning |
|---|---|
| `--url`, `--state-file` | the endpoint to poll, and where state is kept between polls (both required) |
| `--delivery-url` | the `/health/delivery` endpoint; by default the one beside `--url`, and required when `--url` does not end in `/health/scheduling` |
| `--notify-command` | an executable given one argument, the message; exit 0 within 60 seconds means a human was told |
| `--stall-threshold`, `--worker-stale-threshold`, `--worker-overdue-threshold` | the three thresholds, in seconds |
| `--timeout` | seconds to wait for the endpoint (default 20) |
| `--status` | print the last recorded state and exit, without polling |

Exit codes: `0` nothing to say, `10` spoke (the unit below counts it a success),
`11` had something to say and the notify command failed. `11` is deliberately a
failed unit: a monitor cannot page about its own paging failure, but it can
refuse to record that it spoke, so the next poll tries again, and the supervisor
sees a failed unit.

Every poll prints one `<state>: <detail> · delivery <state>: <detail>` line,
even when silent. A message is printed only after it was delivered.

## The state file

A JSON object, replaced atomically after every poll. It records the last reading
and what a human last heard:

- `state` and `detail`: what the endpoint last said, and the detail line
- `consecutive`: how many readings in a row have had this state
- `observed_at`: when that reading was taken, in Unix time
- `announced` and `spoke_at`: the state of the last message a human was actually
  told, and when it was delivered, in Unix time
- `max_active_seen`: the most active destinations any reading has shown. A
  reading with no counts, such as an `unreachable` one, never lowers it. Losing
  the file makes the monitor speak LESS about a lost destination: the estate then
  reads as one that never had any, until a reading shows one again.
- `delivery`: the delivery axis's own `state` (`delivering`, `quiet`, `failing`
  or `unreachable`), `detail`, `consecutive`, `observed_at`, `announced` and
  `spoke_at`, kept apart from the scheduling axis's.
- `split`: the version split's `state` (`split` or `same`), `since`, the two
  commits, `announced` and `spoke_at`.

`announced` and `spoke_at` describe the last *delivered* message and move only
when the notify command exits 0. A failed delivery leaves them where they were,
so the next poll derives the same message and sends it again. A missing or
unreadable file reads as no history: the monitor forgets what a human was told.

## Deploying it

A user-level systemd service and a timer, on a host that shares nothing with the
API. The paths are examples.

```ini
# ~/.config/systemd/user/storydump-scheduling-monitor.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/ops/storydump/scripts/scheduling_monitor.py \
  --url https://api.storydump.app/health/scheduling \
  --state-file %h/.local/state/storydump-scheduling-monitor.json \
  --notify-command <absolute path of the notify command>
SuccessExitStatus=0 10

# ~/.config/systemd/user/storydump-scheduling-monitor.timer
[Timer]
OnBootSec=3min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

The notify command is deployment configuration: the repository holds no
host-specific path, chat or credential. A user unit has no login environment, so
a command that needs a destination or credential gets it from the unit's
`Environment=` lines. Prove the path by sending before trusting the unit: the
monitor cannot say its pager is broken until it has something urgent to say. Run
the command once under `systemd-run --user --wait --pipe` with those values and
confirm a human received it.

**The checkout.** The unit runs from a pinned checkout used by nothing else,
never a working tree anyone develops in: a branch switch there would change or
break monitoring without a word. The script, the classifier and the route change
together, so a stale checkout drifts from the endpoint it reads. A daily timer
fast-forwards it with `git merge --ff-only`; anything else fails that unit.

**Dependencies.** The timer must run, the checkout must stay current and the notify
command must work. A stopped monitor looks exactly like one with nothing to say, so a
supervisor should alert when either unit fails: the poll or the checkout update.

## Checking it

```bash
systemctl --user list-timers     # the next and last run of each timer
systemctl --user --failed        # a failed poll, update or delivery
journalctl --user -u storydump-scheduling-monitor.service
python3 scripts/scheduling_monitor.py --url <url> --state-file <file> --status
```

The journal holds one verdict line per poll, about twelve an hour: count them,
because a gap is a timer that stopped. `--status` prints the state file without
polling; a stale `observed_at` shows the same gap.
