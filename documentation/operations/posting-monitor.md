# Posting-outage monitor

`scripts/posting_monitor.py` polls `GET /health/posting` and raises a FLEET
ALERT when **no post has landed**. It is the axis `scheduling_monitor.py` names
in its own docstring as a bound and does not watch (#1268).

## Why a second monitor rather than a field on the first

`/health/scheduling` is correct and it is not the problem. It reads the **clock**
and the **worker**, and both were entirely healthy through a sixteen-day silence
in which nothing posted — **1936 consecutive `healthy` readings**, ~6.7 days,
every published field true.

`scheduling_health` says so itself:

> **It cannot see a failure PAST the mint.** Cursors advancing while nothing
> posts is a different outage and wants its own signal.

The outage had **two stacked causes and one green light across both**:

| window | state | why the gauge stayed green |
|---|---|---|
| 2026-08-24 → ~09-02 | tier EMPTY — 0 `ig_accounts`, 0 `workspaces`, 0 `media_sources` | nothing could mint, so nothing could be late |
| 2026-09-07 → onward | populated, minting, six intents stranded `awaiting_approval` | `approval_mode` defaults to `manual`, so an intent awaiting approval **is not overdue — it is waiting correctly** |

Different causes, opposite remedies, identical reading. The one assertion false
in both is *a post landed*.

**It is a separate URL, poller, timer and state file** — and the rule is worth
stating, because #1268 names the next axes (stranded approvals, an empty media
pool, an undelivered outbox) and each will ask the same question:

> An axis **joins** an existing payload when it can share that poller's single
> verdict. It gets its **own surface** when it would have to be *ranked* against
> an existing one.

Ranking is what masks. `scheduling_monitor.classify` returns `WORKER_DOWN`
"FIRST, and above every cursor reading" — right there, and it means a stalled
cursor is unreportable while the worker is down. That is a fair trade for two
axes that share a cause. It is not one here: "nothing posted" and "the clock
stopped" are the pair *observed to disagree for sixteen days*, so whichever lost
the ranking is the one silenced — and this axis exists precisely because the
other read healthy.

The tempting discriminator — "it needs its own verdict, thresholds and state
file" — **does not hold**, and is recorded as rejected so it is not reached for
again: the worker axis has its own verdict states and two thresholds of its own,
and was folded in anyway.

## Why it runs outside the app

Unchanged from the sibling, and it is the same argument: **an alert whose
*sending* is performed by the system it monitors cannot fire when that system is
down.** The poller runs on the fleet host and shares no machine, process,
network path, clock or notification channel with its subject. The app's own
notification routing has no writer, so an alert delivered there would vanish
silently.

## The five states

| reading | state | what happens |
|---|---|---|
| `posted_ever > 0`, age ≤ threshold | `posting` | quiet; announces RECOVERED / FIRST POST OBSERVED on entry from another state |
| `posted_ever > 0`, age > threshold | **`silent`** | FLEET ALERT on the **first** reading; repeats every 6h |
| `posted_ever == 0`, grace **not** elapsed | `never-posted` | says so once, then quiet; re-states weekly. **Never an alert, never an all-clear.** |
| `posted_ever == 0`, grace elapsed | **`never-posted-overdue`** | FLEET ALERT on the **first** reading; repeats every 6h |
| anything else — non-200, timeout, malformed body, or a count/age pair that disagrees | **`unreachable`** | FLEET ALERT on the **second consecutive** reading; repeats every 6h |

### `never-posted` EXPIRES, and that is the whole design

The sibling draws the distinction this instrument depends on — *nothing is late*
versus *nothing exists to be late* — and resolves it with a `no-signal` that
**never alerts**. On the cursor axis that is right: an estate with no
destinations is a legitimate pre-launch state and the remedy is to connect one,
not to page someone.

**On the posting axis the identical shape is the outage.** Phase (a) above *is*
"nothing exists to be late". A permanent `no-signal` would have excused all
sixteen days, inside the instrument built to end them.

So the state is split in two with a clock between them: `never-posted` is the
notice, `never-posted-overdue` is what it becomes. That is #1268's third
requirement stated mechanically — *"no posts yet" must expire into "no posts in
N hours"*.

### The grace clock is anchored where it cannot be excused away

The obvious anchor is `accounts_active > 0`: stay quiet until a destination
exists, because until then nothing is expected to post. **That is the rejection
this design turns on** — phase (a) *is* zero destinations, so the anchor excuses
exactly the state it would need to catch, permanently. It is the same shape as
the gauge it replaces: an explanation for the silence that is true and useless.

The anchor is the **latest rung of a ladder**, so no single rung can suppress
the others. Each dates the estate's expectation to post from a different point,
and each can only ever make the check speak **sooner**:

| rung | source | covers |
|---|---|---|
| how long this monitor has been watching | its own state file, off host | everything, including a genuinely empty tier |
| `oldest_active_destination_age_seconds` | `ig_accounts.created_at` | a tier with destinations but no intents yet |
| `oldest_intent_age_seconds` | `post_intents.created_at` | a tier that is minting and stranding |

The middle rung is not decoration. Production held two active destinations and
**zero intents** from 2026-09-02 to 09-06: inside that window there is no intent
to date anything from, so a monitor installed then would have had no
database-side anchor at all and would have sat on a *notice* for 72h while two
destinations idled for six days.

**Each rung arrives as a COUNT and an AGE, and the poller refuses a reading
where they disagree.** They are one fact told twice — `max()` over no rows is
`NULL`, exactly when `count(*)` is 0 — so a nonzero count beside a null age is
an instrument fault, and `classify` answers `unreachable` rather than trusting
either field. The reason it must, rather than picking the safe-looking reading:
a null age would otherwise fall into `max(…, age or 0)` as **zero elapsed**, and
the verdict would drop from `never-posted-overdue` (pages on the first reading)
to `never-posted` (a weekly notice) *while the estate was genuinely overdue* —
not a degradation, a silence, in the direction that looks fine.

This is defence in depth, not a live bug: today each pair is one `count(*)` and
one `max(...)` over one population in a single statement, so they cannot
diverge. It fires the moment an edit decouples a pair — and `posting_health`
already filters one side of one pair deliberately (`_REAL_POST`), so that edit
has a precedent in the very file that produces these fields.

In phase (a) the tier was empty, so the local clock is the only rung — which is
the honest answer and the reason it cannot be removed. In phase (b) both
database rungs fire, even on a monitor deployed yesterday.

`accounts_active` **is** still read — for the alert *text*, so a human is told
which of the two failures they are looking at. It never reaches the verdict, and
`test_an_empty_estate_still_alerts` is what keeps it that way.

## The evidence is `post_intents`, not the cap ledger

#1268 names `SELECT local_date, sum(count) FROM daily_post_counts`. That table
is read and reported, but **the verdict does not rest on it**, and the reason is
a fact about *when it is written* rather than a preference.

`publish_cap` debits it at the `approved → publishing` flip — cap debit and
state change coupled in one CTE — which happens **before the publish call**. So
`count > 0` means *an attempt claimed a cap slot*, not that anything reached
Instagram. A publish path that debits and fails every time moves that number and
lands nothing; a monitor resting on it would go green on exactly the failure it
exists to catch. **That is this issue's own shape, one layer inward.**

`post_intents.state = 'posted'` is the landing, and the database refuses such a
row unless it carries proof: `ck_posted_complete` requires
`ig_container_id IS NOT NULL AND publish_step = 'effect_confirmed'` for an API
post, or a debited cap for a confirmed manual one. The strongest assertion
available is a constraint a row cannot violate.

The ledger earns its place in the **contrast**, which goes in the alert text:

| reading | means |
|---|---|
| `posted_ever = 0`, `debited_total = 0` | nothing ever even tried |
| `posted_ever = 0`, `debited_total > 0` | **attempts are being made and none is landing** |
| `posted_ever > 0` | it has posted; the age says when |

The middle row is invisible to either number alone and is the sharper diagnosis.

### `legacy_backfill` rows are excluded — for a live schema reason

The obvious justification is the M.3 history transform, and it is **wrong**: no
M.1/M.3 transform file was ever written and none will be (FC-7 §6, owner ruling
confirmed 2026-09-02 — the target is greenfield). Nothing produces these rows
today.

The filter stays for what the schema still **permits**, which is the stronger
reason anyway. `ck_posted_complete` lists `published_via = 'legacy_backfill'` as
its first disjunct, exempting such a row from every evidence requirement — so it
is the one `posted` row the database accepts with **no proof that anything
reached Instagram**, which is exactly the row this check must not count. Any
bulk insert of them carries a fresh `entered_state_at` and would read as *a post
just landed*: a recovery nobody observed, followed by a full threshold of
silence. The filter is the module refusing to be an instance of its own subject.

## The thresholds

**Silence — 48h (`--silence-threshold`).** Two full product cycles.
`daily_post_counts` is keyed on `local_date` and `posts_per_day` is a per-day
quantity (`ck_ws_posts_per_day BETWEEN 1 AND 50`, default 3), so one cycle is
24h and this tolerates one entirely blank day — an empty media pool, every slot
skipped — before speaking.

The margin is deliberate, as it is for the sibling's 600s: the gap between a
daily cadence and a sixteen-day silence is nearly three orders of magnitude, so
a threshold near one day buys nothing and costs false alarms on a quiet Sunday.
It is **deliberately not tight enough** to catch the 18- and 19-hour scheduling
outages — those are the sibling's, and it sees them in minutes.

**Grace — 72h (`--grace`).** A separate knob, not a duplicate. Silence is
measured against a cadence the estate has *demonstrated*; grace is measured
against no cadence at all, only the expectation that a deployed product posts.
It covers onboarding end to end — a workspace, an account, a media source, and
reaching the first slot — which is why it is the longer of the two.

Polling every 15 minutes puts worst-case detection at ~48.25h against a
sixteen-day silence.

## Deploying it

Stdlib only — **no venv, no dependencies**. Verified on system `python3` 3.11.

```ini
# ~/.config/systemd/user/storydump-posting-monitor.service
[Unit]
Description=storydump posting-outage monitor

[Service]
Type=oneshot
# THE NOTIFY COMMAND NEEDS AN ENVIRONMENT AND A systemd --user UNIT HAS ALMOST
# NONE. Both variables below, and the reasoning, are the scheduling monitor's
# and were paid for there -- see `scheduling-monitor.md`, which records the two
# unrelated causes and the debugging session they cost.
#
# WHAT IS NEW HERE, AND IS THE TRAP FOR ANYONE WHO ALREADY FIXED IT ONCE:
# `Environment=` is PER UNIT. Repairing the scheduling monitor's unit did not
# make this one work, and nothing about the host is different -- so the failure
# arrives looking like a fresh, unrelated bug on a host where "that was already
# fixed". It was, in a file this unit does not read.
#
# Restated compactly so this unit is followable without a second document:
#   (1) no TELEGRAM_GROUP_CHAT_ID -> tg-post.sh exits 2, before any network call.
#   (2) chat id set, wrong token  -> tg-post.sh exits 3, "send REJECTED".
# (2) is the one that reads as fixed: with no TELEGRAM_STATE_DIR, tg-post.sh
# FALLS BACK to the generic channel dir, whose token is not authorised for the
# group -- a token RESOLVES, and resolving is not the same as working. Probing
# the file for a token line cannot tell these apart; only a real send can.
#
# NOT `EnvironmentFile=bot.conf`. Every line there is `export KEY=value`, and
# systemd does not strip the keyword -- it would create a variable literally
# named `export TELEGRAM_GROUP_CHAT_ID`.
Environment=TELEGRAM_GROUP_CHAT_ID=<the operator group chat id>
Environment=TELEGRAM_STATE_DIR=<a channel dir whose token is authorised there>
ExecStart=/usr/bin/python3 %h/ops/storydump/scripts/posting_monitor.py \
  --url https://<api-host>/health/posting \
  --state-file %h/.local/state/storydump-posting-monitor.json \
  --notify-command %h/claudlobby/lib/tg-post.sh
SuccessExitStatus=0 10
```

```ini
# ~/.config/systemd/user/storydump-posting-monitor.timer
[Unit]
Description=poll storydump posting health every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
```

**A 15-minute cadence, not the sibling's 5.** The thresholds here are measured
in days rather than minutes, so a tighter poll adds load and buys no detection
time. It is offset from the sibling's boot delay so the two do not wake
together.

```bash
systemctl --user daemon-reload
systemctl --user enable --now storydump-posting-monitor.timer
systemctl --user list-timers storydump-posting-monitor.timer
```

### Verify the notify path by SENDING, before trusting the unit

The monitor cannot tell you its pager is broken until it already has something
urgent to say, which is the worst possible moment to find out — and failure (2)
above proves inspection is not enough, because a present token and an authorised
token look identical in the file.

```bash
# Exactly the environment systemd gives it. rc=0 means DELIVERED -- tg-post.sh
# parses `.ok` from the body and exits 0 only on a true, because Telegram
# answers a cross-wired token with HTTP 200 and {"ok":false}. Every other code
# is a NON-delivery, and they have different remedies:
#   2  TELEGRAM_GROUP_CHAT_ID unset          (checked FIRST, before the token)
#   1  no token anywhere -- TELEGRAM_BOT_TOKEN unset AND neither
#      TELEGRAM_STATE_DIR nor the generic channel dir holds one
#   3  the send was REJECTED by Telegram     (token wrong/unauthorised)
#
# Read exit 1 carefully: a missing .env under TELEGRAM_STATE_DIR does NOT give
# you 1. It silently falls back to the generic channel dir and you get 3, or a
# delivery to the wrong place. And an inherited TELEGRAM_BOT_TOKEN outranks
# TELEGRAM_STATE_DIR entirely -- a third way for a token to resolve and still be
# the wrong one. Only rc=0 from a real send proves the pager works.
# `%h` is a UNIT-FILE specifier and does NOT expand in systemd-run — using it
# here yields a wrong path and a confusing failure while following the doc.
systemd-run --user --wait --collect --pipe --quiet \
  -p Environment=TELEGRAM_GROUP_CHAT_ID=<id> \
  -p Environment=TELEGRAM_STATE_DIR=<dir> \
  "$HOME/claudlobby/lib/tg-post.sh" "posting-monitor notify probe"; echo "rc=$?"
```

### Expect it to alert on the first run

Production has never posted through the target tier — `daily_post_counts` holds
**zero rows, ever** — so the first poll will classify `never-posted-overdue` and
page. **That is the instrument working**, not a misconfiguration, and it is the
condition #1268 was filed about. It will keep repeating every 6h until a post
lands or the timer is stopped.

Confirm before enrolling, so the first page is not a surprise:

```bash
/usr/bin/python3 scripts/posting_monitor.py \
  --url https://<api-host>/health/posting \
  --state-file /tmp/posting-probe.json   # no --notify-command: prints, pages nobody
```

Exit codes: `0` nothing to say · `10` spoke · `11` had something to say and could
not deliver it. `11` is deliberately a failed unit — a monitor cannot page about
its own paging failure, but it can refuse to record that it spoke (so the next
poll retries) and let the supervisor log the failure.

`--status` prints the last recorded state without polling.

### Operational dependency: a dedicated checkout

Same requirement as the sibling, and the same reasons — the timer must point at
a **pinned checkout used by nothing else** (not a bot's working tree, which gets
`git checkout -b` constantly), something must keep it current or the script
drifts from the endpoint, and a missing checkout must fail loudly at enrollment
rather than leave a timer firing into nothing.

**Both units should point at the same checkout.** They are separate processes on
purpose; two checkouts would be two things to keep current, which is a
maintenance burden with no isolation benefit — a stale checkout breaks either
poller the same way.

## The state file is the grace clock — do not delete it

`first_seen_at` is the off-host half of the anchor. Deleting the state file
restarts it, which makes this poller speak **later** — the one direction a
monitor must not fail in quietly.

It is bounded rather than ignored: the two database-side rungs above are
supplied by the app on every poll, so an estate holding a destination or an
intent still trips the grace immediately and the message names which rung fired.
A genuinely empty tier — phase (a) — has only the local clock, so a lost state
file there really does buy the outage another 72 hours of quiet.

Writes are atomic (`os.replace`), so a kill mid-write cannot leave a file that
reads as a fresh run forever.

## Bounds

**No verdict here has been raised by real posting traffic.** `posting` and
`silent` are exercised against captured payloads and mutation checks — never a
real landing (see *Expect it to alert on the first run* for why). A later reader
must not read *tested* for *seen in production*; this file's own subject is that
the difference is easy to miss.

**Cross-tenant reach rests on a tracked gap.** Production connects as
`neondb_owner`, which owns these tables and bypasses RLS, so `p_tenant` is inert
(#751) — the same footing `scheduling_lag` documents. Under a role the policy
covers, a tenant-less read returns zero rows, and zero rows here reads as
*nothing has posted*: this fails toward the **alarm**, which is the survivable
direction, but it would be alarming for the wrong reason. Whoever closes #751
must give this a door.

**The `oldest_intent_age_seconds` anchor assumes intents are minted near their
slot.** `fn_clock_tick` selects `next_slot_at <= now()`, so `created_at` tracks
the slot and this holds today. A design that pre-minted intents far ahead of
their slots would make the anchor fire early. It would still require
`posted_ever = 0` across the estate's entire history to fire at all.

**It does not diagnose.** It asserts that a post landed and says which broad
shape the failure has. Why the approval loop strands is #854; why the tier was
empty is its own question. This check is deliberately blind to both, which is
what makes it survive causes nobody has thought of yet.
