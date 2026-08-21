"""Fork E (#943): what does dropping the legacy `recent_post` locks cost?

E4 dies regardless of what happens to history — `ck_locks_recent_scope` forces
a recent lock to name an account, and there is no attribution to give it. So
the locks go. The question is the price, and legacy locks EXPIRE, so the price
is bounded: at most one TTL of media being re-offered early, every re-offer
behind an approval prompt.

## The measurement is NOT the TTL setting, and that matters

`chat_settings.repost_ttl_days` is nullable with no DB default, and the
dispatch asked for it. It is reported. But it is a **proxy**, and on this
schema the thing it proxies is already recorded: `media_posting_locks.
locked_until` is a STORED column, written as `locked_at + ttl` when the lock
is created. So:

- the TTL setting governs locks created **from now on**;
- `locked_until` governs the locks that **already exist**, which is the
  population Fork E is about.

Changing the setting today would not move a single existing lock's expiry.
So the cost of dropping them is read off `locked_until` directly, and that
answer holds whatever the setting says. Both are reported, and where they
disagree the stored column is the one that describes the cost.

## The NULL question, resolved from code before the query runs

Nullable-with-no-default means "unset" is live, and the dispatch is right that
it changes the answer rather than being a missing datapoint. Traced:

    MediaLockService._resolve_ttl (src/services/core/media_lock.py:26-49)
      no telegram_chat_id            -> code default
      chat row not found             -> code default
      chat.repost_ttl_days IS NULL   -> code default
      otherwise                      -> the per-chat value

with ``code default = defaults.DEFAULT_REPOST_TTL_DAYS = 30`` (hardcoded, not
env-overridable). **A NULL setting is therefore BOUNDED at 30 days, never
permanent.** The unbounded case exists but is a different lock: a
``permanent_reject`` is created with an explicit ``ttl_days=None``, which
stores ``locked_until = NULL``, and the model documents NULL as permanent.
Those are counted separately below rather than folded in.

## What it never does

- **It never writes.** The read-only + identity contract is imported from
  ``observed_use.py``, not restated, so there is one audited copy.
- **It does not rule on Fork E.** It prices it.

## Running it

    python -m scripts.fork_e_lock_cost

Package-imported like ``m1_preflight``, so ``python scripts/fork_e_lock_cost.py``
does NOT work from a bare shell — a direct path invocation puts ``scripts/`` on
``sys.path`` rather than the repo root. Stated because the obvious invocation is
the one that fails.

## Exit codes

    0  OK       — measurement ran
    2  ERROR    — could not connect, or identity/read-only assertion failed
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import psycopg2

from scripts.observed_use import assert_identity, fmt, q, resolve_dsn

OK, ERROR = 0, 2

# The code fallback, restated here ONLY to print it next to the measured
# values. Asserted against the source at import so it cannot drift silently.
CODE_DEFAULT_REPOST_TTL_DAYS = 30

SETTINGS_SQL = """
SELECT telegram_chat_id, display_name, is_paused, onboarding_step,
       repost_ttl_days, skip_ttl_days,
       (repost_ttl_days IS NULL) AS repost_ttl_is_null,
       created_at
  FROM chat_settings
 ORDER BY created_at
"""

# Every lock, split by reason and by whether it is STILL IN FORCE. The prior
# version of this count (in fork_a_attribution.py) had no liveness filter and
# was labelled `live_locks` -- it was a count of all rows. Corrected here, and
# both numbers are printed so the correction is visible rather than quiet.
LOCKS_BY_REASON_SQL = """
SELECT COALESCE(lock_reason, '(null)') AS lock_reason,
       count(*) AS all_rows,
       count(*) FILTER (WHERE locked_until IS NULL) AS permanent,
       count(*) FILTER (WHERE locked_until > now()) AS in_force,
       count(*) FILTER (WHERE locked_until <= now()) AS expired
  FROM media_posting_locks
 GROUP BY 1 ORDER BY 2 DESC
"""

# THE COST. How much longer would each still-in-force recent_post lock have
# held? Everything past the last bucket is media re-offered early -- behind an
# approval prompt, never auto-posted.
REMAINING_SQL = """
SELECT count(*) AS in_force,
       count(*) FILTER (WHERE locked_until <= now() + interval '1 day') AS within_1d,
       count(*) FILTER (WHERE locked_until <= now() + interval '7 days') AS within_7d,
       count(*) FILTER (WHERE locked_until <= now() + interval '14 days') AS within_14d,
       count(*) FILTER (WHERE locked_until <= now() + interval '30 days') AS within_30d,
       max(locked_until) AS last_to_expire,
       max(EXTRACT(EPOCH FROM (locked_until - now())) / 86400)::numeric(10, 1)
         AS max_days_remaining
  FROM media_posting_locks
 WHERE lock_reason = 'recent_post' AND locked_until > now()
"""

# The TTL these locks were ACTUALLY created with, derived from the rows rather
# than from config -- the two can disagree if the setting changed mid-life, and
# the rows are what Fork E is dropping.
OBSERVED_TTL_SQL = """
SELECT round(EXTRACT(EPOCH FROM (locked_until - locked_at)) / 86400)::int AS ttl_days,
       count(*) AS locks,
       min(locked_at) AS earliest, max(locked_at) AS latest
  FROM media_posting_locks
 WHERE lock_reason = 'recent_post' AND locked_until IS NOT NULL
 GROUP BY 1 ORDER BY 2 DESC
"""


def assert_code_default_matches_source() -> None:
    """The printed fallback must be the one the app would actually use.

    A constant copied into a report is a constant that can go stale, and this
    one is load-bearing: it is the whole answer to "is a NULL setting bounded?".
    So it is read back out of the source rather than trusted.
    """
    src = (
        pathlib.Path(__file__).resolve().parent.parent
        / "src"
        / "config"
        / "defaults.py"
    ).read_text()
    for line in src.splitlines():
        if line.startswith("DEFAULT_REPOST_TTL_DAYS"):
            actual = int(line.split("=")[1].strip())
            if actual != CODE_DEFAULT_REPOST_TTL_DAYS:
                raise RuntimeError(
                    f"fallback drifted: this script says "
                    f"{CODE_DEFAULT_REPOST_TTL_DAYS}, defaults.py says {actual}"
                )
            return
    raise RuntimeError("DEFAULT_REPOST_TTL_DAYS not found in src/config/defaults.py")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dsn", help="override the resolved DSN (never printed)")
    args = p.parse_args(argv)

    try:
        assert_code_default_matches_source()
        dsn = resolve_dsn(args.dsn)
        host = assert_identity(dsn)
    except Exception as exc:  # noqa: BLE001
        print(f"fork_e ERROR: {exc}", file=sys.stderr)
        return ERROR

    try:
        conn = psycopg2.connect(dsn, options="-c default_transaction_read_only=on")
    except Exception as exc:  # noqa: BLE001
        print(f"fork_e ERROR: cannot connect: {type(exc).__name__}", file=sys.stderr)
        return ERROR

    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("SHOW default_transaction_read_only")
            confirmed = cur.fetchone()[0]
            cur.execute("SELECT current_user, current_database(), now()")
            who, db, now = cur.fetchone()
        if confirmed != "on":
            print(
                f"fork_e ERROR: server reports read_only={confirmed!r}", file=sys.stderr
            )
            return ERROR

        print("=" * 78)
        print("FORK E — the cost of dropping legacy locks (read-only production)")
        print("=" * 78)
        print(f"  host              {host}")
        print(f"  database / role   {db} / {who}")
        print(f"  read_only         {confirmed}  (confirmed by the server)")
        print(f"  server now()      {now}")
        print(
            f"  code fallback     DEFAULT_REPOST_TTL_DAYS = "
            f"{CODE_DEFAULT_REPOST_TTL_DAYS}  (asserted against source)"
        )

        for title, sql in (
            ("THE SETTING — repost_ttl_days per tenant", SETTINGS_SQL),
            ("LOCKS BY REASON — all rows vs still in force", LOCKS_BY_REASON_SQL),
            ("OBSERVED TTL — what the rows were actually built with", OBSERVED_TTL_SQL),
        ):
            print(f"\n{title}\n" + "-" * 78)
            print(fmt(*q(conn, sql)))

        print("\nTHE COST — recent_post locks still in force\n" + "-" * 78)
        cols, rows = q(conn, REMAINING_SQL)
        print(fmt(cols, rows))
        (in_force, d1, d7, d14, d30, last, max_days) = rows[0]

        print("\nREAD\n" + "-" * 78)
        if in_force == 0:
            print(
                "  0 recent_post locks are still in force. Dropping them costs "
                "nothing —\n  every one has already expired on its own."
            )
        else:
            print(f"  {in_force} recent_post lock(s) still in force.")
            print(
                f"  Of those, {d7} ({d7 / in_force:.0%}) would have expired "
                f"within 7 days anyway,"
            )
            print(f"  and {d30} ({d30 / in_force:.0%}) within 30 days.")
            print(
                f"  The last one expires {last} — {max_days} day(s) out. That is "
                f"the CEILING on\n  how long any media could be re-offered early, "
                f"and every re-offer is behind an\n  approval prompt."
            )
        print("\n  This prices the fork. It does not rule on it.")
        print("=" * 78)
        return OK
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
