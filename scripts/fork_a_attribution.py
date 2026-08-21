"""Fork A (#943): can `posting_history` rows be attributed to an Instagram
account by TIME ALONE?

The quarantine question is *which account* a history row went out through. The
tenant is not in question — every row is the one owner's. `posting_history`
carries no `instagram_account_id`, which is why the question exists at all; the
proposed mechanical answer is an epoch gate keyed on when each account was
created. This script measures whether that gate has anything to bite on.

## What it reports, and what that can support

It reports a SHAPE, not a verdict:

- both accounts' ``created_at``;
- the ``posting_history`` span and its month-by-month distribution, not just
  endpoints, because two rows and a five-month gap are different populations
  with the same min and max;
- whether the creation times partition that span CLEANLY, PARTIALLY, or NOT AT
  ALL, by the definition stated at ``classify_partition``;
- the row count on each side and in the ambiguous region.

**What a clean partition would and would not prove.** A row posted before an
account existed cannot have gone out through it — that direction is sound, and
it is the only direction this measurement establishes. The converse is not:
a row postdating account 2 could still have gone out through account 1, because
both may be live at once. So a partition is *consistent with* recoverable
attribution and is not proof of it. Every count below is labelled with which of
the two it is.

**It also checks whether the question is already answered elsewhere** — the
column inventory of ``posting_history`` is printed, so "no direct attribution
column exists" is a measured claim rather than an assumption inherited from the
issue.

**Scope bound: attribution is only owed on rows that went through Instagram at
all.** A ``telegram`` row never touched an account, so it is not in Fork A's
population and counting it would inflate the problem. The Instagram-bearing
subset is reported separately, by two INDEPENDENT predicates (declared method,
and the presence of a returned Instagram id), because they can disagree and the
disagreement is itself informative.

## What it never does

- **It never writes.** Read-only is enforced the way ``observed_use.py``
  enforces it, by importing that module rather than restating it: session
  read-only client-side, ``default_transaction_read_only=on`` server-side, and
  the setting READ BACK before any query runs.
- **It never prints the DSN**, and it never echoes account credentials — only
  identity fields (display name, username, created_at) that the ruling needs.
- **It does not rule on Fork A.** That is the owner's.

## Exit codes

    0  OK       — measurement ran
    2  ERROR    — could not connect, or identity/read-only assertion failed
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import psycopg2

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# The read-only + identity contract is defined once, in the script that
# established it. Restating it here would be a second copy that can drift
# from the one the reviewer already audited.
from observed_use import assert_identity, fmt, q, resolve_dsn  # noqa: E402

OK, ERROR = 0, 2

ACCOUNTS_SQL = """
SELECT display_name, instagram_username, instagram_account_id,
       is_active, created_at, updated_at
  FROM instagram_accounts
 ORDER BY created_at
"""

# Does anything already carry attribution? Printed so "no such column" is
# measured rather than assumed from the issue text.
COLUMNS_SQL = """
SELECT column_name, data_type
  FROM information_schema.columns
 WHERE table_name = 'posting_history'
 ORDER BY ordinal_position
"""

SPAN_SQL = """
SELECT count(*) AS rows, min(posted_at) AS first_at, max(posted_at) AS last_at
  FROM posting_history
"""

# Distribution, not endpoints: a span with a five-month hole in the middle
# partitions very differently from a dense one.
MONTHLY_SQL = """
SELECT to_char(date_trunc('month', posted_at), 'YYYY-MM') AS month,
       count(*) AS rows,
       count(*) FILTER (WHERE posting_method = 'instagram_api') AS via_api,
       count(*) FILTER (WHERE instagram_media_id IS NOT NULL
                           OR instagram_story_id IS NOT NULL) AS bears_ig_id
  FROM posting_history
 GROUP BY 1 ORDER BY 1
"""

# The two independent Instagram predicates, crossed. They can disagree, and a
# disagreement bounds how well either one defines Fork A's population.
IG_POPULATION_SQL = """
SELECT COALESCE(posting_method, '(null)') AS posting_method,
       (instagram_media_id IS NOT NULL OR instagram_story_id IS NOT NULL)
         AS bears_ig_id,
       count(*) AS rows,
       min(posted_at) AS first_at, max(posted_at) AS last_at
  FROM posting_history
 GROUP BY 1, 2 ORDER BY 3 DESC
"""

# Rows on each side of a cut. Parameterised so the same query answers the
# question for whichever account boundary the accounts turn out to define.
SIDES_SQL = """
SELECT count(*) FILTER (WHERE posted_at <  %(cut)s) AS before_cut,
       count(*) FILTER (WHERE posted_at >= %(cut)s) AS at_or_after_cut,
       count(*) FILTER (WHERE posted_at <  %(cut)s
                          AND (posting_method = 'instagram_api'
                               OR instagram_media_id IS NOT NULL
                               OR instagram_story_id IS NOT NULL)) AS before_ig,
       count(*) FILTER (WHERE posted_at >= %(cut)s
                          AND (posting_method = 'instagram_api'
                               OR instagram_media_id IS NOT NULL
                               OR instagram_story_id IS NOT NULL)) AS after_ig
  FROM posting_history
"""

# THE PRIOR QUESTION, and it has to be asked before the cut means anything:
# does any history PREDATE THE FIRST account row? If it does, `created_at` is
# not the account's epoch — it is when the row was written into THIS schema —
# and an epoch gate keyed on it has no signal for those rows at all.
PREDATES_SQL = """
SELECT count(*) FILTER (WHERE posted_at < %(first)s) AS before_first_account,
       count(*) FILTER (WHERE posted_at < %(first)s
                          AND (posting_method = 'instagram_api'
                               OR instagram_media_id IS NOT NULL
                               OR instagram_story_id IS NOT NULL))
         AS before_first_account_ig,
       min(posted_at) FILTER (WHERE posting_method = 'instagram_api')
         AS first_api_post
  FROM posting_history
"""

# If created_at is a migration artefact rather than an epoch, the schema_version
# row for the multi-account migration will sit at the same instant. Measured,
# because "these look like migration timestamps" is a story until it is checked.
SCHEMA_VERSION_SQL = """
SELECT version, description, applied_at
  FROM schema_version
 WHERE version IN (23, 41, 44)
 ORDER BY version
"""

# A column-NAME inventory cannot see attribution hiding in column CONTENT.
# Instagram media ids are commonly "<media>_<owner-user-id>", so if the account
# id is embedded, attribution is directly recoverable and no epoch gate is
# needed. Probed by shape and by joining against the real account ids, never by
# dumping ids. A null result here is as informative as a hit — it is what makes
# "no other signal" a measured claim rather than an inherited assumption.
ID_SHAPE_SQL = """
SELECT count(*) AS ig_rows,
       count(*) FILTER (WHERE instagram_media_id LIKE %(u1)s
                           OR instagram_story_id LIKE %(u1)s) AS matches_acct_1,
       count(*) FILTER (WHERE instagram_media_id LIKE %(u2)s
                           OR instagram_story_id LIKE %(u2)s) AS matches_acct_2,
       count(*) FILTER (WHERE instagram_media_id LIKE '%%_%%')
         AS media_id_has_underscore,
       count(*) FILTER (WHERE instagram_permalink IS NOT NULL) AS has_permalink
  FROM posting_history
 WHERE posting_method = 'instagram_api'
    OR instagram_media_id IS NOT NULL
    OR instagram_story_id IS NOT NULL
"""

# Fork E's stake, reported because mason's re-posing turned on it: E4 attributes
# live locks VIA history rows, so "drop history" is not free.
LOCKS_SQL = """
SELECT count(*) AS live_locks,
       count(*) FILTER (WHERE l.media_item_id IN
              (SELECT media_item_id FROM posting_history)) AS backed_by_history
  FROM media_posting_locks l
"""


def classify_partition(no_signal, separable, ambiguous):
    """CLEAN / PARTIAL / NOT-AT-ALL over THREE buckets, not two.

    The dispatch framed this as one cut — the second account's creation. That
    framing assumes both accounts predate the history, and on this data they do
    not, so a two-bucket verdict would read PARTIAL while the gate separates
    almost nothing. The buckets are:

    - ``no_signal``  — posted before the FIRST account row existed. The gate
      says nothing about these in either direction.
    - ``separable``  — posted between the two account rows. Attributable to the
      first account by the sound direction (a row cannot have gone out through
      an account that did not yet exist).
    - ``ambiguous``  — posted after both existed. Both accounts may be live, so
      time alone does not separate them.

    Only ``separable`` is what the gate buys. The verdict is keyed on it, and
    the label is deliberately quotable on its own: a reader who takes only the
    word must not be misled by it.
    """
    total = no_signal + separable + ambiguous
    if total == 0:
        return "NONE", "no rows to classify"
    if separable == 0:
        return "NOT-AT-ALL", (
            f"the account creation times separate 0 of {total} row(s); "
            f"{no_signal} predate both accounts and {ambiguous} postdate both"
        )
    if no_signal == 0 and ambiguous == 0:
        return "CLEAN", f"all {total} row(s) fall in the separable window"
    return "PARTIAL", (
        f"the gate separates {separable} of {total} row(s) "
        f"({separable / total:.1%}); {no_signal} predate both accounts "
        f"(no signal) and {ambiguous} postdate both (ambiguous)"
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dsn", help="override the resolved DSN (never printed)")
    args = p.parse_args(argv)

    try:
        dsn = resolve_dsn(args.dsn)
        host = assert_identity(dsn)
    except Exception as exc:  # noqa: BLE001
        print(f"fork_a ERROR: {exc}", file=sys.stderr)
        return ERROR

    try:
        conn = psycopg2.connect(dsn, options="-c default_transaction_read_only=on")
    except Exception as exc:  # noqa: BLE001
        print(f"fork_a ERROR: cannot connect: {type(exc).__name__}", file=sys.stderr)
        return ERROR

    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("SHOW default_transaction_read_only")
            confirmed = cur.fetchone()[0]
            cur.execute("SELECT current_user, current_database(), version()")
            who, db, ver = cur.fetchone()
        if confirmed != "on":
            print(
                f"fork_a ERROR: server reports read_only={confirmed!r}", file=sys.stderr
            )
            return ERROR

        print("=" * 78)
        print("FORK A — account attribution by time (read-only production)")
        print("=" * 78)
        print(f"  host              {host}")
        print(f"  database / role   {db} / {who}")
        print(f"  server            {ver.split(' on ')[0]}")
        print(f"  read_only         {confirmed}  (confirmed by the server)")

        for title, sql in (
            ("INSTAGRAM ACCOUNTS", ACCOUNTS_SQL),
            ("posting_history COLUMNS — is attribution already recorded?", COLUMNS_SQL),
            ("posting_history SPAN", SPAN_SQL),
            ("DISTRIBUTION BY MONTH (not just endpoints)", MONTHLY_SQL),
            (
                "INSTAGRAM POPULATION — two independent predicates, crossed",
                IG_POPULATION_SQL,
            ),
            ("FORK E STAKE — locks that 'drop history' would strand", LOCKS_SQL),
            (
                "MIGRATION TIMESTAMPS — is created_at an epoch or an artefact?",
                SCHEMA_VERSION_SQL,
            ),
        ):
            print(f"\n{title}\n" + "-" * 78)
            print(fmt(*q(conn, sql)))

        # Content probe — needs the account ids, so it runs after the roster.
        _, roster = q(conn, ACCOUNTS_SQL)
        if len(roster) >= 2:
            print(
                "\nID CONTENT PROBE — is the account id embedded in the ids?\n"
                + "-" * 78
            )
            print(
                fmt(
                    *q(
                        conn,
                        ID_SHAPE_SQL,
                        {"u1": f"%{roster[0][2]}%", "u2": f"%{roster[1][2]}%"},
                    )
                )
            )

        # --- the partition question ---------------------------------------
        _, acct_rows = q(conn, ACCOUNTS_SQL)
        _, span_rows = q(conn, SPAN_SQL)
        total, first_at, last_at = span_rows[0]

        print("\nPARTITION\n" + "-" * 78)
        if len(acct_rows) < 2:
            print(
                f"  {len(acct_rows)} account(s) present — the dispatch assumed "
                f"two. No cut to test; reporting the discrepancy rather than "
                f"inventing a boundary."
            )
        else:
            first_created = acct_rows[0][4]
            _, pre = q(conn, PREDATES_SQL, {"first": first_created})
            before_first, before_first_ig, first_api = pre[0]
            print(f"  1st account created_at         {first_created}")
            print(f"  earliest instagram_api post    {first_api}")
            print(
                f"  rows predating the FIRST acct  {before_first}   "
                f"(Instagram-bearing: {before_first_ig})"
            )
            if before_first_ig:
                print(
                    f"\n  *** {before_first_ig} Instagram-bearing row(s) predate BOTH "
                    f"account rows. For those, `created_at` cannot be the\n"
                    f"      account's epoch — it is when the row entered THIS schema. "
                    f"An epoch gate keyed on it has NO signal there,\n"
                    f"      in either direction. This is prior to the cut question "
                    f"below and dominates it. ***\n"
                )
            cut = acct_rows[1][4]  # second account by created_at
            cols, sides = q(conn, SIDES_SQL, {"cut": cut})
            before, after, before_ig, after_ig = sides[0]
            # Three buckets, on the Instagram-bearing population only — a
            # telegram row never touched an account and is not Fork A's to own.
            separable_ig = before_ig - before_first_ig
            verdict, why = classify_partition(before_first_ig, separable_ig, after_ig)
            print(f"  cut (2nd account created_at)   {cut}")
            print(f"  history span                   {first_at}  ..  {last_at}")
            print(
                f"  rows before cut                {before}   "
                f"(Instagram-bearing: {before_ig})"
            )
            print(
                f"  rows at/after cut              {after}   "
                f"(Instagram-bearing: {after_ig})"
            )
            print(
                f"\n  Instagram-bearing buckets: "
                f"no-signal={before_first_ig}  separable={separable_ig}  "
                f"ambiguous={after_ig}"
            )
            print(f"\n  PARTITION: {verdict}")
            print(f"  {why}")
            print(
                f"\n  SOUND DIRECTION ONLY: the {before_ig} Instagram-bearing "
                f"row(s) before the cut cannot have gone out through the second\n"
                f"  account. The {after_ig} at/after the cut are NOT attributed "
                f"by this measurement — both accounts may be live there."
            )

        print("\n" + "=" * 78)
        print("This reports a shape. The Fork A ruling is the owner's.")
        print("=" * 78)
        return OK
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
