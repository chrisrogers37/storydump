# Delivery-failure monitor

`scripts/delivery_monitor.py` polls `GET /health/delivery` and says so, in the operator's Telegram group, when
the outbox's deliveries start failing: approval cards, notifications, card edits (#1482). It is the fourth health
surface's poller, a sibling of `posting-monitor.md` and `scheduling-monitor.md`.

**Status: built, NOT installed.** The units below are the recipe. Installing them on the fleet host is a separate
step, taken after the API that serves `/health/delivery` has been deployed (see *Order* below).

## What it watches

Migration 090 records, on every outbox row, the class of its last failed send:

| Class | Meaning |
|---|---|
| `rate_limited` | a 429 |
| `destination_gone` | the chat will not take messages |
| `refused` | the message as shaped will never land |
| `credential_dead` | the bot token was refused (401) |
| `ambiguous` | no answer came back: a timeout or a 5xx |

090 also records the provider's code and the time. `/health/delivery` counts, estate-wide, the rows whose last
failure fell in the last hour, by class and code. It gives two numbers:

- **`failed_or_ambiguous`:** the rows that ended `failed` or sit `ambiguous`. This is the number that alerts.
- **`sent_in_window`:** the rows sent in the same hour. It is context, so an hour with no failures and no traffic
  is not read as an hour that delivered.

A 429 is a deferral, not a failure: the payload lists it as context, and it never counts toward the alert.

## When it speaks

| Reading | What it does |
|---|---|
| **5 or more** (`--raise-at`) | Raises at once: `FLEET ALERT: storydump OUTBOX DELIVERIES ARE FAILING`, naming the classes and codes. |
| **2 to 4** | Holds whatever it last said. That band between the thresholds is the hysteresis. |
| **1 or fewer** (`--clear-at`), on two consecutive polls | The first such poll makes a failure `clearing`, which never pages; the second clears it, and says `RECOVERED` if it had alerted. One quiet poll inside a burst is not a recovery. |
| Still failing 6 hours after the last alert | Repeats, so a long outage does not look like a resolved one. |
| The endpoint unreachable on two consecutive polls | Says so. The detector cannot look, which is not the same as deliveries being fine. |

**Why 5 an hour.** Measured on production over the 30 days to 2026-09-30:

- 21 failed rows, all in one burst on 2026-09-12 (16 in one hour, 5 in another), and none in any other hour.
- At most 43 sends in any hour.

With traffic that low, an absolute count is steadier than a rate: a rate over three sends is noise. A raise at 5
fires on that burst and on nothing else in the month. Re-derive the number if the traffic grows by an order of
magnitude.

## Deploying it

Stdlib only; no venv. The script imports its HTTP, state-file and notify helpers from `posting_monitor.py`, so it
runs as a module from the root of a checkout that has both (`python3 -m scripts.delivery_monitor`). The fleet
host's `~/ops/storydump` does.

```ini
# ~/.config/systemd/user/storydump-delivery-monitor.service
[Unit]
Description=storydump delivery-failure monitor

[Service]
Type=oneshot
# The notify environment is PER UNIT: fixing another monitor's unit does not fix this one. What each variable is
# for, and the two failures a missing or wrong one produces, are in `posting-monitor.md` (Deploying it).
Environment=TELEGRAM_GROUP_CHAT_ID=<the operator group chat id>
Environment=TELEGRAM_STATE_DIR=<a channel dir whose token is authorised there>
WorkingDirectory=%h/ops/storydump
ExecStart=/usr/bin/python3 -m scripts.delivery_monitor \
  --url https://<api-host>/health/delivery \
  --state-file %h/.local/state/storydump-delivery-monitor.json \
  --notify-command %h/claudlobby/lib/tg-post.sh
SuccessExitStatus=0 10
```

```ini
# ~/.config/systemd/user/storydump-delivery-monitor.timer
[Unit]
Description=poll storydump delivery health every 5 minutes

[Timer]
OnBootSec=7min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

**A 5-minute cadence.** The window is an hour and a burst is minutes long, so a 15-minute poll would see a
short burst once at most, and the two-poll clear would take half an hour. The boot delay is offset from the
siblings' so the three do not wake together.

```bash
systemctl --user daemon-reload
systemctl --user enable --now storydump-delivery-monitor.timer
systemctl --user list-timers storydump-delivery-monitor.timer
```

**Order.** Deploy the API first, so `/health/delivery` exists, and only then enable the timer. Enabling it first
makes the monitor's first act an alert about the missing endpoint. Noise on day one is how a monitor gets muted
(#1270 records the same trap for the posting monitor).

**Verify the notify path by sending, before trusting the unit.** It is the same check, and the same exit codes,
as `posting-monitor.md`'s: run `tg-post.sh` once under exactly the unit's environment and require `rc=0`.

**Exit codes**, the siblings':

| Code | Meaning |
|---|---|
| `0` | quiet |
| `10` | it spoke |
| `11` | the notify failed |

On `11` the state file keeps what the human was last told, so the next poll says it again. `--status` prints the
last recorded state without polling.

## The state file

`--state-file` holds:

- **`effective`:** delivering, failing, or clearing (a failure waiting out its two quiet polls).
- **`reading`** and **`consecutive`:** the raw reading, and how many polls in a row have given it.
- **`announced`** and **`spoke_at`:** what the human was last told, and when.

Deleting it makes the next poll a first poll. That is safe: a failure still in progress is re-raised on the next
reading at 5 or more. But a recovery the human was owed is lost.
