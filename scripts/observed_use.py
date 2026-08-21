"""Observed-use measurement over ``user_interactions`` (`04` §2 / #790).

**This measures; it does not rule.** FC-7 clause 3 sets the M.3 parity bar at
"the commands *in production use*". Nobody had measured production use — the bar
was an inventory of what EXISTS, asserted against a definition about what is
USED. This is the query that turns the bar's own wording into data.

    GROUP BY interaction_type, interaction_name with counts and max(created_at)
    yields the owner's actual vocabulary with frequencies and recency.

Frequency and recency are reported together and neither is sufficient alone: a
command used 400 times whose last use was in March means something different
from one used 40 times last week.

## The two things a reader must not conclude from a zero

**1. A zero here is not evidence of disuse.** ``log_command`` / ``log_callback``
are hand-placed at each call site — there is no middleware — so the set of names
this table CAN hold is a property of the code, not of the product surface. Three
populations are therefore distinct and are reported separately:

    RECORDED     the code writes this name; a count is a real count
    SILENT       no code path writes this name; its zero means NOTHING
    RENAMED      written under a different string than its dispatch key

A reader who joins observed names to the dispatch surface by string equality
gets false zeros in the last two populations. ``--surface`` prints the split.

**2. The table's window is not the product's lifetime.** ``min(created_at)``
is reported next to every result. An interaction that predates the table, or
that happened while a call site was uninstrumented, is absent from it and
indistinguishable from one that never happened.

## The positive control

A zero is only interpretable if the instrument is known live. ``posting_history``
is written by a different code path than ``user_interactions``; a manual post
lands a row in both. Comparing the two per month says whether the interaction
recorder was actually running in that window — an independent instrument rather
than this table vouching for itself.

## What it never does

- **It never writes.** The session is set read-only client-side AND the server
  is given ``default_transaction_read_only=on``, and the script then READS THAT
  SETTING BACK and refuses to proceed unless the server confirms it. Intent is
  not enforcement; the assertion is what makes "did you only read?" answerable
  by something other than this script's own say-so.
- **It never prints the DSN.** Resolved, parsed, asserted, never echoed.
- **It does not rule on the parity bar.** It reports the vocabulary. Which items
  are pre-window is an owner decision this deliberately leaves open.

## Exit codes

    0  OK       — measurement ran
    2  ERROR    — could not connect, or identity/read-only assertion failed
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

import psycopg2

OK, ERROR = 0, 2

# The one production endpoint. The project has exactly one branch, so there is
# no staging host to mistake for it — which is why an equality assertion is
# sufficient here and a substring match would not be.
EXPECTED_HOST = "ep-hidden-shadow-aify76h5.c-4.us-east-1.aws.neon.tech"

# storydump's Railway project (personal workspace).
RAILWAY_PROJECT_ID = "33d1ccca-353c-4236-8d39-0d8fd916f054"

# Names the code can write, taken from the call sites. A name with a ':' suffix
# is parameterised (``switch_account:{account_id}``) and fragments under a naive
# GROUP BY, so the report also groups on the root.
RECORDED_COMMANDS = {
    "/start",
    "/status",
    "/next",
    "/help",
    "/cleanup",
    "/approveall",
    "/link",
    "/name",
    "/instances",
    "/new",
    "/settings",
    "/setup",
}
RECORDED_CALLBACK_ROOTS = {
    "posted",
    "skip",
    "reject",
    "cancel_reject",
    "confirm_reject",
    "autopost",
    "batch_approve",
    "instance_new",
    "select_account",
    "cycle_account",
    "switch_account",
    "remove_account",
    "switch_account_from_post",
    "settings_toggle",
    "schedule_action",
    "schedule_confirm",
    "resume",
    "clear",
}
# Dispatch keys with NO call site that writes a row. A zero for these is
# uninterpretable by construction.
SILENT_CALLBACKS = {
    "back",
    "regenerate_caption",
    "batch_approve_cancel",
    "instance_manage",
    "btp",
    "settings_refresh",
    "settings_edit",
    "settings_edit_cancel",
    "settings_close",
    "settings_accounts",
    "accounts_config",
}
# Registered slash commands routed to handle_removed_command, which replies
# with a redirect and does not log. Structurally silent.
SILENT_COMMANDS = {
    "/queue",
    "/pause",
    "/resume",
    "/history",
    "/sync",
    "/schedule",
    "/stats",
    "/locks",
    "/reset",
    "/dryrun",
    "/backfill",
    "/connect",
}
# Recorded under a string that is not the dispatch key.
# When each retired command stopped writing rows. `handle_removed_command` does
# not log, so the recorder went dark on these AT RETIREMENT — not when demand
# ended. A zero after these dates is an artifact of the instrument, not a signal.
RETIRED_ON = {
    "/schedule": "2026-02-19",
    "/stats": "2026-02-19",
    "/locks": "2026-02-19",
    "/reset": "2026-02-19",
    "/dryrun": "2026-02-19",
    "/backfill": "2026-02-19",
    "/connect": "2026-02-19",
    "/queue": "2026-02-23",
    "/pause": "2026-02-23",
    "/resume": "2026-02-23",
    "/history": "2026-02-23",
    "/sync": "2026-02-23",
}

RENAMED = {
    "sap": "switch_account_from_post",
    "account_remove": "remove_account:{id}",
    "account_remove_confirmed": "remove_account:{id}",
}

VOCAB_SQL = """
SELECT interaction_type,
       interaction_name,
       count(*)                                    AS n,
       min(created_at)                             AS first_used,
       max(created_at)                             AS last_used,
       count(DISTINCT user_id)                     AS users
  FROM user_interactions
 GROUP BY 1, 2
 ORDER BY 1, 3 DESC, 2
"""

ROOT_SQL = """
SELECT interaction_type,
       split_part(interaction_name, ':', 1)        AS name_root,
       count(*)                                    AS n,
       count(DISTINCT interaction_name)            AS variants,
       min(created_at)                             AS first_used,
       max(created_at)                             AS last_used
  FROM user_interactions
 GROUP BY 1, 2
 ORDER BY 1, 3 DESC, 2
"""

WINDOW_SQL = """
SELECT count(*)                AS rows_total,
       min(created_at)         AS first_row,
       max(created_at)         AS last_row,
       count(DISTINCT user_id) AS distinct_users
  FROM user_interactions
"""

CONTROL_SQL = """
WITH ph AS (
    SELECT date_trunc('month', posted_at) AS m, count(*) AS n
      FROM posting_history
     WHERE posting_method = 'telegram_manual'
       AND status IN ('posted', 'skipped', 'rejected')
     GROUP BY 1
), ui AS (
    SELECT date_trunc('month', created_at) AS m, count(*) AS n
      FROM user_interactions
     WHERE interaction_name IN ('posted', 'skip', 'confirm_reject')
     GROUP BY 1
)
SELECT to_char(COALESCE(ph.m, ui.m), 'YYYY-MM')            AS mon,
       COALESCE(ph.n, 0)                                   AS history_rows,
       COALESCE(ui.n, 0)                                   AS card_callbacks,
       round(100.0 * COALESCE(ui.n, 0) / NULLIF(ph.n, 0), 1) AS pct_agree
  FROM ph FULL OUTER JOIN ui ON ph.m = ui.m
 ORDER BY 1
"""

PAIR_SQL = """
SELECT status, posting_method, count(*) AS n,
       min(posted_at) AS first_at, max(posted_at) AS last_at
  FROM posting_history GROUP BY 1, 2 ORDER BY 3 DESC
"""

LIVE_SQL = """
SELECT interaction_type,
       split_part(interaction_name, ':', 1) AS name_root,
       count(*) AS n, max(created_at) AS last_used
  FROM user_interactions
 WHERE created_at > now() - interval '%s days'
 GROUP BY 1, 2 ORDER BY 3 DESC
"""

METHOD_SQL = """
SELECT COALESCE(posting_method, '(null)') AS posting_method,
       count(*) AS n, min(posted_at) AS first_at, max(posted_at) AS last_at
  FROM posting_history GROUP BY 1 ORDER BY 2 DESC
"""


def resolve_dsn(explicit: str | None) -> str:
    """Take the DSN from the consumer's own config, never reconstruct one.

    The Railway CLI has no ``--project`` flag on ``variables``, so the project is
    linked inside a throwaway directory first rather than by mutating the caller's
    cwd. storydump lives in the owner's PERSONAL workspace, so the personal token
    is preferred — the work token returns Unauthorized for this project.
    """
    if explicit:
        return explicit
    env = os.environ.copy()
    token = env.get("RAILWAY_PERSONAL_TOKEN") or env.get("RAILWAY_API_TOKEN")
    if token:
        env["RAILWAY_API_TOKEN"] = token
    # RAILWAY_TOKEN is PROJECT-scoped in the CLI; an account token placed there
    # is rejected as Unauthorized and shadows RAILWAY_API_TOKEN. Clear it.
    env.pop("RAILWAY_TOKEN", None)

    with tempfile.TemporaryDirectory(prefix="observed-use-link-") as scratch:
        link = subprocess.run(
            [
                "railway",
                "link",
                "--project",
                RAILWAY_PROJECT_ID,
                "--environment",
                "production",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=scratch,
            timeout=120,
        )
        if link.returncode != 0:
            raise RuntimeError(
                f"railway link failed rc={link.returncode}: "
                f"{link.stderr.strip()[:200] or link.stdout.strip()[:200]}"
            )
        out = subprocess.run(
            [
                "railway",
                "variables",
                "--service",
                "worker",
                "--environment",
                "production",
                "--json",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=scratch,
            timeout=120,
        )
    if out.returncode != 0:
        raise RuntimeError(
            f"railway variables failed rc={out.returncode}: {out.stderr.strip()[:200]}"
        )
    dsn = json.loads(out.stdout).get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("no DATABASE_URL in the worker service variables")
    return dsn


def assert_identity(dsn: str) -> str:
    host = urlparse(dsn).hostname or ""
    if host != EXPECTED_HOST:
        raise RuntimeError(
            f"refusing: resolved host is not the expected production endpoint "
            f"(got {host!r})"
        )
    return host


def q(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return [d[0] for d in cur.description], cur.fetchall()


def fmt(cols, rows) -> str:
    if not rows:
        return "  (no rows)"
    body = [[("" if v is None else str(v)) for v in r] for r in rows]
    w = [max(len(c), *(len(r[i]) for r in body)) for i, c in enumerate(cols)]
    line = "  " + "  ".join(c.ljust(w[i]) for i, c in enumerate(cols))
    rule = "  " + "  ".join("-" * w[i] for i in range(len(cols)))
    return "\n".join(
        [line, rule]
        + ["  " + "  ".join(r[i].ljust(w[i]) for i in range(len(cols))) for r in body]
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dsn", help="read-only DSN; resolved from Railway if omitted")
    p.add_argument(
        "--surface",
        action="store_true",
        help="also print the recorded/silent/renamed split",
    )
    args = p.parse_args(argv)

    try:
        dsn = resolve_dsn(args.dsn)
        host = assert_identity(dsn)
    except Exception as exc:  # noqa: BLE001
        print(f"observed_use ERROR: {exc}", file=sys.stderr)
        return ERROR

    try:
        conn = psycopg2.connect(dsn, options="-c default_transaction_read_only=on")
    except Exception as exc:  # noqa: BLE001
        print(
            f"observed_use ERROR: cannot connect: {type(exc).__name__}", file=sys.stderr
        )
        return ERROR

    try:
        conn.set_session(readonly=True)
        # Read the setting BACK. Setting it is intent; confirming it is enforcement.
        with conn.cursor() as cur:
            cur.execute("SHOW default_transaction_read_only")
            confirmed = cur.fetchone()[0]
            cur.execute("SELECT current_user, current_database(), version()")
            who, db, ver = cur.fetchone()
        if confirmed != "on":
            print(
                f"observed_use ERROR: server reports read_only={confirmed!r}",
                file=sys.stderr,
            )
            return ERROR

        print("=" * 78)
        print("OBSERVED USE — user_interactions (read-only production measurement)")
        print("=" * 78)
        print(f"  host              {host}")
        print(f"  database / role   {db} / {who}")
        print(f"  server            {ver.split(' on ')[0]}")
        print(
            f"  read_only         {confirmed}  (confirmed by the server, not asserted)"
        )

        for title, sql in (
            ("TABLE WINDOW (the bound on every count below)", WINDOW_SQL),
            ("VOCABULARY — raw (type, name, count, recency)", VOCAB_SQL),
            ("VOCABULARY — rooted (parameterised names collapsed on ':')", ROOT_SQL),
            (
                "POSITIVE CONTROL — pair check (posting_history status x method)",
                PAIR_SQL,
            ),
            (
                "POSITIVE CONTROL — monthly agreement, independent instrument",
                CONTROL_SQL,
            ),
            ("POSTING METHODS (control discriminator)", METHOD_SQL),
        ):
            print(f"\n{title}\n" + "-" * 78)
            cols, rows = q(conn, sql)
            print(fmt(cols, rows))

        for days in (90, 30):
            print(f"\nLIVE VOCABULARY — last {days} days\n" + "-" * 78)
            cols, rows = q(conn, LIVE_SQL % days)
            print(fmt(cols, rows))

        # A zero is only a signal for a name the code can actually write.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT split_part(interaction_name, ':', 1) "
                "FROM user_interactions"
            )
            seen = {r[0] for r in cur.fetchall()}
        writable = RECORDED_COMMANDS | RECORDED_CALLBACK_ROOTS
        print(
            "\nREAL ZEROS — the code writes this name; production never did\n"
            + "-" * 78
        )
        for n in sorted(writable - seen):
            print(f"  {n}")
        print(
            "\nHISTORICAL — present in data, not producible by current code\n"
            + "-" * 78
        )
        for n in sorted(seen - writable - {"photo_notification", "caption_update"}):
            note = f"  (recorder went dark {RETIRED_ON[n]})" if n in RETIRED_ON else ""
            print(f"  {n}{note}")

        if args.surface:
            print("\nDISPATCH SURFACE — what a zero can and cannot mean\n" + "-" * 78)
            print(f"  recorded commands   {len(RECORDED_COMMANDS)}")
            print(f"  recorded cb roots   {len(RECORDED_CALLBACK_ROOTS)}")
            print(
                f"  SILENT commands     {len(SILENT_COMMANDS)}  {sorted(SILENT_COMMANDS)}"
            )
            print(
                f"  SILENT callbacks    {len(SILENT_CALLBACKS)}  {sorted(SILENT_CALLBACKS)}"
            )
            print(f"  RENAMED             {RENAMED}")
            print(
                f"  retired-on dates    {len(RETIRED_ON)} commands, "
                f"dark since 2026-02-19/2026-02-23"
            )
    finally:
        conn.close()
    return OK


if __name__ == "__main__":
    sys.exit(main())
