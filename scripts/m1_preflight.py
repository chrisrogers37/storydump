"""The M.1 pre-window profile battery, assembled (`04` L89 / #790).

**This is not a new instrument.** The M.1 transform spec
(`documentation/planning/2026-08-17-m1-transform-spec/README.md`) §5.1 already
specifies it, names its queries, and says where it goes:

    Each is a plain SELECT over §4's stated predicates; the assembled script
    belongs beside `fc8_gate.py` and lands with the M1 files.

This is that script. Every check carries the `spec` section it comes from, so
the battery and the spec can be diffed rather than compared by memory — a second
enumeration of the same predicates is exactly the fork that would drift.

## What it answers

G-PROFILE (spec §2.7) pairs every "this should be impossible" edge with two
artifacts: a **pre-window profile query** (read-only, live legacy, run at window
prep) and an **in-file assertion** in the 3e transform. This is the first half.
The second half lands with the M1 files.

    A zero measured at prep can drift by 3e (legacy is live until step 1), so
    the in-file arm re-checks; a nonzero at prep routes to the owner *before*
    the window opens instead of detonating inside it.

That is why it exists at all: there is no shadow phase (`04` Phase M), so the
first production traffic the target ever sees is the M.3 step-6 smoke canary
with the legacy system already stood down.

## What it does NOT do

- **It never writes.** Not to legacy, not anywhere. `--dsn` is a read-only DSN
  and the session is set read-only besides.
- **It does not rule.** Where the spec marks a fork open, this reports the
  measurement the fork is conditional on and names the fork. It picks none of
  them, and it does not decide the SD-n proposals either.
- **It does not re-implement FC-8.** `scripts/fc8_gate.py` (#792) owns those
  two counts and is imported.

## Exit codes

    0  CLEAR       — every check ran, no blocker is nonzero
    1  HALT        — at least one blocker is nonzero; route per its remedy
    2  ERROR       — a check could not be answered (missing table, bad DSN)
    3  INCOMPLETE  — a legacy table present in the database has no check

`ERROR` and `INCOMPLETE` are separate codes rather than folded into `HALT`
because *could not ask* and *asked and found trouble* have different remedies,
and collapsing them fails toward "everything is fine" — the direction nobody
audits.

**The coverage denominator comes from the database, never from a list in this
module.** Every base table found in the legacy schema must be claimed by the
registry, so a table that appears in production after this was written surfaces
as `INCOMPLETE` rather than as silence. That is not hypothetical: it is how
`posting_history_dedup_archive` (#941) — created in production two days after
the spec was written, and absent from `02` §9's fourteen — was found on the
first production run.
"""

from __future__ import annotations

import argparse
import datetime
import decimal
import json
import sys
import textwrap
from dataclasses import dataclass, field

import psycopg2

from scripts.fc8_gate import census as fc8_census
from scripts.m1_ladder import (
    LADDER_DISAGREEMENT_SQL,
    LADDER_SQL_STRICT,
    RUNG_NAMES,
    RUNG_QUARANTINE,
)

CLEAR, HALT, ERROR, INCOMPLETE = 0, 1, 2, 3

BLOCKER, COUNT, DISCLOSURE = "BLOCKER", "COUNT", "DISCLOSURE"


@dataclass(frozen=True)
class Check:
    """One profile query from the spec's §5.1 battery.

    `kind` decides what a nonzero result MEANS, which is the only thing
    separating a blocker from a census row — the SQL is the same shape.
    `spec` is the section it comes from; a check with no section is a check
    nobody ratified.
    """

    table: str
    name: str
    kind: str
    sql: str
    spec: str = ""
    why: str = ""
    remedy: str = ""
    fork: str = ""  # names the open fork this measurement feeds
    optional_table: bool = False
    """The check's table may legitimately be absent.

    PostgreSQL parses a whole statement before executing it, so a
    ``CASE WHEN to_regclass(...) IS NULL`` guard inside the SQL does not help —
    the reference to a missing table fails at parse time regardless of which
    branch would run. The existence test has to happen out here.

    Set ONLY where absence is a real state rather than a broken instrument."""


# ---------------------------------------------------------------------------
# THE BATTERY — spec §5.1, in the spec's own order (M1-01 .. M1-11), because
# that is the order a blocker actually bites: a chat that cannot become a
# workspace takes everything workspace-rooted with it.
# ---------------------------------------------------------------------------

CHECKS: tuple[Check, ...] = (
    # ===== the two collapse counts every conditional fork keys on ==========
    Check(
        "chat_settings",
        "fork_c_and_d_collapse_counts",
        DISCLOSURE,
        "SELECT count(*) AS chats,"
        "       count(*) FILTER (WHERE telegram_chat_id < 0) AS group_chats"
        "  FROM chat_settings",
        spec="§3.3",
        why="THE conditional-fork input, printed by the spec verbatim. Fork C"
        " collapses at group_chats = 1; Fork D collapses only at chats = 1"
        " TOTAL. The two are deliberately different counts — a single DM-rooted"
        " row makes minting rule W mint two workspaces where a collapse shape"
        " mints one, so C can collapse while D does not.",
    ),
    Check(
        "chat_settings",
        "dm_rooted_tenants",
        COUNT,
        "SELECT count(*) FROM chat_settings WHERE telegram_chat_id > 0",
        spec="§3.4 addendum / §4.2",
        why="A DM-rooted tenant is product-shaped and is Fork D's territory."
        " It is also why D's collapse keys on chats rather than group_chats.",
        fork="D",
    ),
    # ===== M1-01  users -> users, user_identities =========================
    Check("users", "total", COUNT, "SELECT count(*) FROM users", spec="§4.1"),
    Check(
        "users",
        "null_is_active",
        COUNT,
        "SELECT count(*) FROM users WHERE is_active IS NULL",
        spec="§4.1",
        why="Ruled: false -> 'disabled', else (true/NULL) -> 'active'. The NULL"
        " count is profiled so the ruling's third case is sized, not assumed.",
    ),
    # ===== M1-02  chat_settings + memberships -> workspaces, members ======
    Check(
        "chat_settings",
        "total",
        COUNT,
        "SELECT count(*) FROM chat_settings",
        spec="§4.2",
    ),
    Check(
        "chat_settings",
        "name_defaulted_by_sd12",
        COUNT,
        "SELECT count(*) FROM chat_settings"
        " WHERE display_name IS NULL OR btrim(display_name) = ''",
        spec="SD-12",
        why="workspaces.name is NOT NULL against a nullable source. SD-12 rules"
        " COALESCE(display_name, 'Workspace ' || left(id::text,8)) plus a G-LOG"
        " row when defaulted. Counted, NOT blocked — the rule handles it; this"
        " predicts how many G-LOG rows the transform will write.",
    ),
    Check(
        "chat_settings",
        "tz_defaulted_by_g_tz",
        COUNT,
        "SELECT count(*) FROM chat_settings WHERE posting_timezone IS NULL",
        spec="§2.2 G-TZ",
        why="G-TZ rules NULL -> 'UTC' outright (legacy NULL meant UTC), with NO"
        " G-LOG row — the log names DISCARDED values, and an absent value was"
        " never set. Counted to separate this population from the next check.",
    ),
    Check(
        "chat_settings",
        "tz_discarded_by_g_tz",
        BLOCKER,
        "SELECT count(*) FROM chat_settings"
        " WHERE posting_timezone IS NOT NULL"
        "   AND posting_timezone NOT IN (SELECT name FROM pg_timezone_names)"
        "   AND upper(posting_timezone) NOT IN"
        "       (SELECT upper(abbrev) FROM pg_timezone_abbrevs)",
        spec="§2.2 G-TZ",
        why="A value fn_safe_tz does not recognize maps to 'UTC' plus a G-LOG"
        " row naming the discarded value. That is the rule WORKING, not a"
        " defect — blocked so the discarded values are reviewed before the"
        " window rather than found in the log after it.",
        remedy="Confirm each value is genuinely junk, then let G-TZ map it."
        " APPROXIMATE: fn_safe_tz is a target function (migration 052) and"
        " cannot be called against a legacy database, so the catalog stands in."
        " It can only OVER-report — a value in neither view may still be"
        " accepted by AT TIME ZONE — so a hit is a prompt to look, never proof.",
    ),
    Check(
        "user_chat_memberships",
        "total",
        COUNT,
        "SELECT count(*) FROM user_chat_memberships",
        spec="§4.2",
    ),
    Check(
        "user_chat_memberships",
        "rung4_quarantine_feed",
        BLOCKER,
        f"SELECT count(*) FROM ({LADDER_SQL_STRICT}) l"
        f" WHERE l.rung = {RUNG_QUARANTINE}",
        spec="§5.2 / §3.1",
        why="Owner-derivation rung 4: a chat resolving to no owner."
        " ct_workspaces_owner_at_insert is DEFERRABLE INITIALLY DEFERRED, so a"
        " workspaces INSERT without an owner member row in the SAME transaction"
        " is refused at commit — an unresolved chat produces NO workspace and"
        " nothing workspace-rooted beneath it.",
        remedy="Fork A's adjudication procedure, which does not exist yet — no"
        " printed actor can perform it (svc_migration holds SELECT only on"
        " legacy). Note the count is Fork-D-conditional: under a collapse shape"
        " a DM-rooted chat mints no workspace and this feed may be empty.",
        fork="A (volume conditional on D)",
    ),
    Check(
        "user_chat_memberships",
        "ladder_reading_disagreement",
        BLOCKER,
        f"SELECT count(*) FROM ({LADDER_DISAGREEMENT_SQL}) d",
        spec="m1_ladder",
        why="`04` L80 qualifies only rung 3 as 'earliest ACTIVE', so rungs 1/2"
        " read literally see inactive rows — while the next bullet drops"
        " is_active=false rows and the derived owner must land as a"
        " workspace_members row. The two readings pick a different owner for"
        " these chats.",
        remedy="Rule the activity qualifier, then delete the losing reading"
        " from scripts/m1_ladder.py.",
    ),
    Check(
        "user_chat_memberships",
        "inactive_rows_dropped",
        COUNT,
        "SELECT count(*) FROM user_chat_memberships WHERE NOT is_active",
        spec="§4.2",
        why="Dropped with an audit record. Counted so the audit_events row"
        " count is predicted rather than discovered.",
    ),
    Check(
        "user_chat_memberships",
        "legacy_owner_rows_demoted_to_admin",
        COUNT,
        "SELECT count(*) FROM user_chat_memberships m"
        " WHERE m.instance_role = 'owner' AND m.user_id IS DISTINCT FROM ("
        f"   SELECT l.owner_user_id FROM ({LADDER_SQL_STRICT}) l"
        "    WHERE l.chat_settings_id = m.chat_settings_id)",
        spec="§4.2",
        why="uq_members_one_owner is a partial UNIQUE INDEX on (workspace_id)"
        " WHERE role='owner'; a 1:1 copy of several owner rows violates it.",
    ),
    # ===== M1-03  media_sources ===========================================
    Check(
        "chat_settings",
        "gdrive_chats_with_null_root",
        BLOCKER,
        "SELECT count(*) FROM chat_settings"
        " WHERE media_source_type = 'google_drive' AND media_source_root IS NULL",
        spec="§4.3",
        why="config := jsonb_build_object(...,'folder_ref', media_source_root)"
        " and folder_ref is required — the source row would be invalid.",
        remedy="Nonzero routes to the owner at prep.",
    ),
    # ===== M1-04  ig_accounts (the fan-out) ===============================
    Check(
        "instagram_accounts",
        "total",
        COUNT,
        "SELECT count(*) FROM instagram_accounts",
        spec="§4.4",
    ),
    Check(
        "api_tokens",
        "fanout_pair_set_size",
        COUNT,
        "SELECT count(*) FROM (SELECT DISTINCT t.chat_settings_id,"
        "   t.instagram_account_id FROM api_tokens t"
        "  WHERE t.instagram_account_id IS NOT NULL"
        "    AND t.chat_settings_id IS NOT NULL) pairs",
        spec="§4.4",
        why="The ruled fan-out source, literally. One ig_accounts row per pair.",
    ),
    Check(
        "instagram_accounts",
        "accounts_outside_the_pair_set",
        BLOCKER,
        "SELECT count(*) FROM instagram_accounts ia WHERE NOT EXISTS ("
        "  SELECT 1 FROM api_tokens t"
        "   WHERE t.instagram_account_id = ia.id"
        "     AND t.chat_settings_id IS NOT NULL)",
        spec="§4.4 profile (a)",
        why="Legacy accounts referenced by ZERO token pairs mint no row, and"
        " any attributed history pointing at them quarantines downstream."
        " ig_accounts.workspace_id is NOT NULL.",
        remedy="Fork C conditional: at group_chats = 1 the NULL-chat tokens"
        " resolve to the sole group tenant and these become reachable, so read"
        " this beside fork_c_and_d_collapse_counts. Widening the ruled fan-out"
        " source is a plan change, not the spec's call.",
        fork="C",
    ),
    Check(
        "chat_settings",
        "active_pointer_orphans",
        BLOCKER,
        "SELECT count(*) FROM chat_settings cs"
        " WHERE cs.active_instagram_account_id IS NOT NULL"
        "   AND NOT EXISTS (SELECT 1 FROM api_tokens t"
        "        WHERE t.instagram_account_id = cs.active_instagram_account_id"
        "          AND t.chat_settings_id IS NOT NULL)",
        spec="§4.4 profile (b)",
        why="active_instagram_account_id values outside the pair set — the"
        " column dissolves, but it is consumed by this profile.",
        remedy="Same class as accounts_outside_the_pair_set; Fork C.",
        fork="C",
    ),
    Check(
        "api_tokens",
        "total_snapshotted_never_transformed",
        COUNT,
        "SELECT count(*) FROM api_tokens",
        spec="§4.4",
        why="FC-7.2: no ciphertext carried; ownership FACTS only. Counted so"
        " the 3f snapshot is verified non-empty, not to predict a target row.",
    ),
    Check(
        "api_tokens",
        "ownership_edges_by_service",
        DISCLOSURE,
        "SELECT service_name,"
        "       count(*) FILTER (WHERE chat_settings_id IS NOT NULL) AS with_chat,"
        "       count(*) FILTER (WHERE instagram_account_id IS NOT NULL) AS with_acct,"
        "       count(*) FILTER (WHERE chat_settings_id IS NOT NULL"
        "                          AND instagram_account_id IS NOT NULL) AS with_both,"
        "       count(*) AS total"
        "  FROM api_tokens GROUP BY service_name ORDER BY service_name",
        spec="§4.4",
        why="with_both is the population the fan-out joins on. Broken out"
        " because a per-column census reads healthy when neither column is"
        " null-heavy on its own.",
    ),
    # ===== M1-05  media_items =============================================
    Check(
        "media_items", "total", COUNT, "SELECT count(*) FROM media_items", spec="§4.5"
    ),
    Check(
        "media_items",
        "non_gdrive_source_types",
        BLOCKER,
        "SELECT count(*) FROM media_items WHERE source_type <> 'google_drive'",
        spec="§4.5 / #793",
        why="ck_sources_provider admits 'gdrive' only, so M1-05 admits"
        " google_drive rows alone and asserts zero others. Covers the FC-8 pair"
        " AND instagram_backfill, which #793 leaves open.",
        remedy="FC-8's two counts route per `04` L87. An instagram_backfill row"
        " needs #793 ruled. The corpus is operator-triggered, so zero is frozen"
        " rather than guaranteed — which is why the assert exists in-file too.",
        fork="#793",
    ),
    Check(
        "media_items",
        "null_source_identifier_on_gdrive",
        BLOCKER,
        "SELECT count(*) FROM media_items"
        " WHERE source_type = 'google_drive' AND source_identifier IS NULL",
        spec="§4.5",
        why="provider_file_ref is NOT NULL in the target (the D37 canonical"
        " stable ref).",
        remedy="Nonzero routes to the owner at prep.",
    ),
    Check(
        "media_items",
        "unmappable_media_kind",
        BLOCKER,
        "SELECT count(*) FROM media_items"
        " WHERE source_type = 'google_drive'"
        "   AND (mime_type IS NULL"
        "        OR (mime_type NOT LIKE 'image/%%' AND mime_type NOT LIKE 'video/%%'))",
        spec="SD-7",
        why="media_kind is image iff mime_type LIKE 'image/%', video iff"
        " 'video/%'; SD-7 asserts zero matching neither. Guessing from the file"
        " extension was considered and rejected as unverifiable.",
        remedy="Nonzero needs the rows inspected before SD-7 is approved.",
    ),
    Check(
        "media_items",
        "null_tenant",
        BLOCKER,
        "SELECT count(*) FROM media_items WHERE chat_settings_id IS NULL",
        spec="§4.5 / 044",
        why="Ruled for this table by the 044 sole-tenant rule — which assigns"
        " rows to THE tenant and therefore needs exactly one to exist. Read"
        " beside fork_c_and_d_collapse_counts.",
        remedy="044's own header requires explicit owner sign-off for this"
        " class of write; never via CI or on merge.",
    ),
    Check(
        "media_items",
        "null_is_active",
        COUNT,
        "SELECT count(*) FROM media_items WHERE is_active IS NULL",
        spec="§5.1",
        why="Neither live nor dead; counted rather than assigned to a side.",
    ),
    # ===== M1-06  post_locks ==============================================
    Check(
        "media_posting_locks",
        "total",
        COUNT,
        "SELECT count(*) FROM media_posting_locks",
        spec="§4.6",
    ),
    Check(
        "media_posting_locks",
        "live_recent_locks_fork_e_sizer",
        COUNT,
        "SELECT count(*) FROM media_posting_locks"
        " WHERE lock_reason = 'recent_post'"
        "   AND (locked_until IS NULL OR locked_until > (now() AT TIME ZONE 'UTC'))",
        spec="§3.5 / §4.6",
        why="THE Fork E sizer. ck_locks_recent_scope requires an ig_account_id"
        " on a 'recent' lock and legacy locks carry no account column, so every"
        " LIVE recent_post row is an E case. Zero moots E at execution; the"
        " spec sizes it as 'plausible but few'.",
        remedy="E1 (switch-timeline attribution) is only available if the"
        " timeline has rows — see attribution_timeline_source_rows. Otherwise"
        " E2 (quarantine) or E3 (drop, losing an anti-repeat hold for up to one"
        " TTL) are what remain.",
        fork="E",
    ),
    Check(
        "media_posting_locks",
        "workspace_mismatch_vs_media",
        BLOCKER,
        "SELECT count(*) FROM media_posting_locks l"
        "  JOIN media_items m ON m.id = l.media_item_id"
        " WHERE l.chat_settings_id IS NOT NULL"
        "   AND m.chat_settings_id IS NOT NULL"
        "   AND l.chat_settings_id <> m.chat_settings_id",
        spec="§3.3 narrowing / §4.6",
        why="composite fk_locks_media (workspace_id, media_item_id) FORCES a"
        " lock's workspace to equal its media item's, so the lock's own"
        " chat_settings_id is a CROSS-CHECK, not a mapping input. A mismatch is"
        " a discrepancy class, which is why the NULL-tenant count is absent"
        " here: Fork C is structurally closed for this table.",
        remedy="Quarantine feed.",
    ),
    Check(
        "media_posting_locks",
        "expired_rows_not_carried",
        COUNT,
        "SELECT count(*) FROM media_posting_locks"
        " WHERE locked_until IS NOT NULL"
        "   AND locked_until <= (now() AT TIME ZONE 'UTC')",
        spec="SD-14",
        why="Rows expired at the transform instant are not carried (equivalent"
        " to the runtime reap sweep's steady state; 3f preserves them).",
    ),
    # ===== M1-07  category_post_case_mix ==================================
    Check(
        "category_post_case_mix",
        "total",
        COUNT,
        "SELECT count(*) FROM category_post_case_mix",
        spec="§4.7",
    ),
    Check(
        "category_post_case_mix",
        "null_tenant",
        BLOCKER,
        "SELECT count(*) FROM category_post_case_mix WHERE chat_settings_id IS NULL",
        spec="§3.3 Fork C",
        why="Re-keyed to workspace_id (NOT NULL). This table names no"
        " media_item, so unlike the locks there is nothing to derive a tenant"
        " FROM — Fork C stands here.",
        remedy="Fork C. Collapses at group_chats = 1; ratifier is the product"
        " owner per 044's header.",
        fork="C",
    ),
    Check(
        "category_post_case_mix",
        "is_current_vs_effective_to_disagreement",
        COUNT,
        "SELECT count(*) FROM category_post_case_mix"
        " WHERE is_current IS DISTINCT FROM (effective_to IS NULL)",
        spec="SD-15",
        why="effective_to is authoritative; is_current is ruled redundant and"
        " not carried. SD-15 profiles the disagreement before trusting either.",
    ),
    # ===== M1-08  post_intents ============================================
    Check(
        "posting_history",
        "total",
        COUNT,
        "SELECT count(*) FROM posting_history",
        spec="§4.8",
    ),
    Check(
        "posting_history",
        "null_tenant",
        BLOCKER,
        "SELECT count(*) FROM posting_history WHERE chat_settings_id IS NULL",
        spec="§3.3 Fork C (C3)",
        why="post_intents.workspace_id is NOT NULL. The composite-FK argument"
        " that closed Fork C for locks fixes media_item CONSISTENCY here but"
        " not workspace_id itself — C stands.",
        remedy="Fork C; quarantined rows join Fork A's list.",
        fork="C",
    ),
    Check(
        "posting_history",
        "workspace_mismatch_vs_media",
        BLOCKER,
        "SELECT count(*) FROM posting_history h"
        "  JOIN media_items m ON m.id = h.media_item_id"
        " WHERE h.chat_settings_id IS NOT NULL"
        "   AND m.chat_settings_id IS NOT NULL"
        "   AND h.chat_settings_id <> m.chat_settings_id",
        spec="§4.8",
        why="Media in a different workspace than the history row claiming it —"
        " composite-FK consistency, profiled.",
        remedy="Quarantine feed.",
    ),
    Check(
        "posting_history",
        "status_outside_the_1to1_map",
        BLOCKER,
        "SELECT count(*) FROM posting_history WHERE status NOT IN"
        " ('posted','failed','skipped','rejected','expired')",
        spec="§4.8",
        why="Five legacy statuses map 1:1 onto post_intents terminal states."
        " A sixth value has no mapping and would be dropped silently.",
        remedy="Extend the map, or quarantine the rows.",
    ),
    Check(
        "posting_history",
        "status_success_disagreement",
        COUNT,
        "SELECT count(*) FROM posting_history WHERE (status = 'posted') <> success",
        spec="SD-19",
        why="status is authoritative; success rides verbatim into the companion"
        " detail row. SD-19 profiles the disagreement rather than quarantining"
        " a cosmetic legacy inconsistency.",
    ),
    Check(
        "posting_history",
        "by_status_and_method",
        DISCLOSURE,
        "SELECT status, COALESCE(posting_method,'(null)'), count(*)"
        "  FROM posting_history GROUP BY 1,2 ORDER BY 3 DESC",
        spec="§6",
        why="The per-state target row counts the transform must reproduce.",
    ),
    Check(
        "service_runs",
        "attribution_timeline_source_rows",
        COUNT,
        "SELECT count(*) FROM service_runs"
        " WHERE service_name = 'InstagramAccountService'"
        "   AND method_name IN ('switch_account','add_account')",
        spec="§4.8",
        why="The rows the ruled ±6h attribution timeline is reconstructed FROM."
        " Zero here means the mechanism resolves nothing and every API-posted"
        " row falls into a timeline gap — which also removes E1 as a Fork E"
        " option, since E1 reuses this mechanism.",
        remedy="The spec names a degenerate short-circuit: a workspace whose"
        " pair-set holds exactly one account attributes trivially, no timeline"
        " consulted. See attribution_rows_needing_the_timeline.",
    ),
    Check(
        "posting_history",
        "attribution_rows_needing_the_timeline",
        COUNT,
        "SELECT count(*) FROM posting_history WHERE posting_method = 'instagram_api'",
        spec="§4.8",
        why="The population the timeline must attribute. Reported as rows"
        " AT STAKE rather than as missing timeline rows, because a"
        " missing-source count of zero reads like good news.",
        fork="E (shares the mechanism)",
    ),
    Check(
        "service_runs",
        "total_consumed_never_landed",
        COUNT,
        "SELECT count(*) FROM service_runs",
        spec="§4.8",
        why="Consumed by the attribution transform, snapshotted at 3f, never"
        " lands in the target (ruled).",
    ),
    Check(
        "posting_queue",
        "rows_the_queue_clause_drops",
        COUNT,
        "SELECT count(*) FROM posting_queue",
        spec="§4.8 / FC-7.4",
        why="No intents are minted for these; the archive snapshot preserves"
        " them; the scheduler re-plans from cadence. Every row stops working at"
        " cutover — the owner-facing number.",
    ),
    Check(
        "posting_queue",
        "by_status",
        DISCLOSURE,
        "SELECT status, count(*),"
        "       round((avg(EXTRACT(epoch FROM (now() AT TIME ZONE 'UTC')"
        "                          - scheduled_for)) / 3600.0)::numeric, 1)"
        "         AS avg_age_hours"
        "  FROM posting_queue GROUP BY status ORDER BY 3 DESC NULLS LAST",
        spec="FC-7.4",
        why="`04` L86 words the clause as 'pending posting_queue items'. The"
        " spec's own postcondition is status-blind (no intents for ANY surviving"
        " legacy id), so a corpus with zero 'pending' rows makes the PROSE and"
        " the ASSERTION disagree. The spread is what makes that checkable.",
    ),
    # ===== M1-09  audit merge =============================================
    Check(
        "audit_log",
        "total_to_audit_events",
        COUNT,
        "SELECT count(*) FROM audit_log",
        spec="§4.9",
    ),
    Check(
        "audit_log",
        "null_tenant",
        BLOCKER,
        "SELECT count(*) FROM audit_log WHERE chat_settings_id IS NULL",
        spec="§3.3 Fork C",
        why="Third open Fork C table.",
        remedy="Fork C.",
        fork="C",
    ),
    Check(
        "user_interactions",
        "total_to_audit_events",
        COUNT,
        "SELECT count(*) FROM user_interactions",
        spec="§4.9",
    ),
    Check(
        "user_interactions",
        "unresolvable_chat_ids",
        BLOCKER,
        "SELECT count(*) FROM user_interactions ui"
        " WHERE ui.telegram_chat_id IS NULL"
        "    OR NOT EXISTS (SELECT 1 FROM chat_settings cs"
        "         WHERE cs.telegram_chat_id = ui.telegram_chat_id)",
        spec="§3.4 addendum",
        why="This table has NO tenant column — only a raw nullable"
        " telegram_chat_id resolved via chat_settings. Rows with NULL or"
        " unresolvable ids have no workspace under any Fork C option as"
        " written. Same class, one table further out.",
        remedy="Named for the Fork C ruling to say whether it covers them;"
        " until then a §5.2 quarantine feed.",
        fork="C (addendum)",
    ),
    Check(
        "user_interactions",
        "interaction_type_outside_the_check_set",
        COUNT,
        "SELECT count(*) FROM user_interactions WHERE interaction_type NOT IN"
        " ('command','callback','message','bot_response')",
        spec="§4.9",
        why="A known writer implies drift is possible. The mapping carries any"
        " value (detail is typed open), so this is a census, not a gate.",
    ),
    # ===== M1-10  onboarding (proposed drop) ==============================
    Check(
        "onboarding_sessions",
        "live_sessions_blocking_the_drop",
        BLOCKER,
        "SELECT count(*) FROM onboarding_sessions"
        " WHERE expires_at > (now() AT TIME ZONE 'UTC')",
        spec="§4.10",
        why="THE drop-check, and it is NOT a raw row count: the default branch"
        " asserts zero NON-EXPIRED sessions. Expired rows are what the drop"
        " discards by design, and 3f preserves them.",
        remedy="Nonzero routes to the owner at prep (finish or void the"
        " session) — the two step vocabularies cannot honestly map.",
    ),
    Check(
        "chat_settings",
        "incomplete_onboarding_blocking_the_drop",
        BLOCKER,
        "SELECT count(*) FROM chat_settings WHERE onboarding_completed IS NOT TRUE",
        spec="§4.10",
        why="The drop-check's second half. Both halves must be zero.",
        remedy="Same as above: owner at prep.",
    ),
    Check(
        "onboarding_sessions",
        "total_rows_snapshot_only",
        COUNT,
        "SELECT count(*) FROM onboarding_sessions",
        spec="§4.10",
        why="Total rows, for the 3f snapshot. Deliberately NOT the drop-check —"
        " confusing the two is what made an expired-session table look like a"
        " failed gate.",
    ),
    # ===== ledger + the undispositioned table =============================
    Check(
        "posting_history_dedup_archive",
        "undispositioned_rows",
        BLOCKER,
        "SELECT count(*) FROM posting_history_dedup_archive",
        spec="(none — #941)",
        why="A 15th legacy table `02` §9 does not account for, created in"
        " production 2026-08-19 — two days AFTER the M.1 spec was written, so"
        " its absence from the mapping is a gap the spec could not have had."
        " It holds the only surviving record that a story published more than"
        " once, and 3g is DROP SCHEMA legacy CASCADE.",
        remedy="Write the disposition (#941). Expected: snapshot at 3f, never"
        " transformed, dropped with the rest of legacy at 3g.",
        optional_table=True,
    ),
    Check(
        "schema_version",
        "head_version",
        DISCLOSURE,
        "SELECT COALESCE(max(version), -1) FROM schema_version",
        spec="§9 / #941",
        why="Superseded by the runner ledger via `runner adopt`; never trusted."
        " Disclosed because a head above the corpus means production carries a"
        " migration the corpus does not describe (#840, #941).",
    ),
)


@dataclass
class Result:
    check: Check
    value: int | None = None
    rows: list = field(default_factory=list)
    error: str | None = None
    absent: bool = False  # the optional table is not in this database

    @property
    def halts(self) -> bool:
        return self.check.kind == BLOCKER and bool(self.value)

    @property
    def reportable(self) -> bool:
        """The check produced a value. False for BOTH ways it can fail to —
        which is the distinction the exit codes exist to keep, so it is named
        once here rather than re-spelled at every call site."""
        return not self.error and not self.absent


@dataclass
class Report:
    results: list = field(default_factory=list)
    unclaimed_tables: list = field(default_factory=list)
    fc8: dict = field(default_factory=dict)
    rungs: dict = field(default_factory=dict)

    @property
    def errored(self) -> list:
        return [r for r in self.results if r.error]

    @property
    def absent(self) -> list:
        return [r for r in self.results if r.absent]

    @property
    def blockers(self) -> list:
        return [r for r in self.results if r.halts]

    def exit_code(self) -> int:
        if self.errored:
            return ERROR
        if self.unclaimed_tables:
            return INCOMPLETE
        return HALT if self.blockers else CLEAR


def _jsonable(value):
    """Coerce a psycopg2 scalar to something `json.dumps` accepts.

    `numeric` comes back as `Decimal` and dates come back as `date`/`datetime`;
    the JSON encoder refuses all three. The TEXT
    renderer never noticed because it `str()`s everything — so `--json` crashed
    while `--text` was fine, on the same run, and the agreement test missed it
    by running against an EMPTY corpus where no aggregate produces a numeric at
    all. Coercing here rather than passing `default=str` to `json.dumps` keeps
    the two renderers looking at the same values instead of at two encodings of
    them.

    No disclosure returns a date TODAY. It is handled anyway because that is
    the identical defect one type over, and leaving a known second instance
    open after writing the paragraph above would be its own kind of dishonest.
    """
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value


def _legacy_base_tables(conn, schema: str) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = %s AND table_type = 'BASE TABLE'"
            " ORDER BY table_name",
            (schema,),
        )
        return [r[0] for r in cur.fetchall()]


def run(conn, schema: str = "public") -> Report:
    """Run every check. A check that raises is recorded, never skipped."""
    report = Report()

    with conn.cursor() as cur:
        # search_path takes an IDENTIFIER, so it cannot be a bind parameter;
        # quote_ident is psycopg2's door for exactly that and returns a
        # complete quoted identifier.
        cur.execute(
            "SET search_path TO " + psycopg2.extensions.quote_ident(schema, conn)
        )

    present_tables = None
    try:
        present_tables = set(_legacy_base_tables(conn, schema))
    except psycopg2.Error:
        conn.rollback()

    for check in CHECKS:
        result = Result(check=check)
        if (
            check.optional_table
            and present_tables is not None
            and check.table not in present_tables
        ):
            result.absent = True
            report.results.append(result)
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(check.sql)
                fetched = cur.fetchall()
            if check.kind == DISCLOSURE:
                result.rows = [[_jsonable(v) for v in row] for row in fetched]
            else:
                result.value = fetched[0][0] if fetched else 0
        except psycopg2.Error as exc:
            conn.rollback()
            result.error = f"{type(exc).__name__}: {(exc.pgerror or '').strip()[:120]}"
        report.results.append(result)

    # Coverage: the denominator comes from the DATABASE, never from a list here.
    claimed = {c.table for c in CHECKS}
    if present_tables is None:
        report.results.append(
            Result(
                check=Check(
                    table="(coverage)",
                    name="table_inventory",
                    kind=BLOCKER,
                    sql="",
                    spec="§5.1",
                ),
                error="could not read the table inventory — coverage unknown",
            )
        )
    else:
        report.unclaimed_tables = sorted(present_tables - claimed)

    # FC-8 is imported, never re-implemented.
    #
    # A FAILURE HERE MUST REACH THE EXIT CODE. An earlier version recorded the
    # error into `report.fc8` only — where it renders as a line of text and
    # contributes to no verdict, so a run whose FC-8 census could not be
    # evaluated could still exit CLEAR. That is the unreachable-vs-empty
    # collapse this module's own docstring is about, inside the module.
    try:
        report.fc8 = fc8_census(conn).to_json()
    except psycopg2.Error as exc:
        conn.rollback()
        report.fc8 = {"error": type(exc).__name__}
        report.results.append(
            Result(
                check=Check(
                    table="(fc8_gate)",
                    name="census",
                    kind=BLOCKER,
                    sql="",
                    spec="§7 / #792",
                ),
                error=f"{type(exc).__name__}: FC-8 census could not be evaluated",
            )
        )

    # Which rungs the corpus actually exercises — coverage honesty for the
    # ladder. A rung with no rows here is a rung production cannot validate.
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT rung, count(*) FROM ({LADDER_SQL_STRICT}) l"
                " GROUP BY rung ORDER BY rung"
            )
            report.rungs = {int(r[0]): int(r[1]) for r in cur.fetchall()}
    except psycopg2.Error as exc:
        conn.rollback()
        report.rungs = {"error": type(exc).__name__}
        report.results.append(
            Result(
                check=Check(
                    table="(ladder)",
                    name="rung_coverage",
                    kind=BLOCKER,
                    sql="",
                    spec="m1_ladder",
                ),
                error=f"{type(exc).__name__}: rung coverage could not be evaluated",
            )
        )

    return report


def _wrap(text: str, indent: str = "      ", width: int = 80) -> list:
    return textwrap.wrap(
        " ".join(text.split()),
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
    )


def render(report: Report) -> str:
    out = ["M.1 transform preflight", "=" * 74, ""]

    # --- blockers, loudest first ------------------------------------------
    if report.blockers:
        out += [f"  BLOCKERS ({len(report.blockers)}) — the transform cannot run", ""]
        for r in report.blockers:
            out.append(f"  [{r.check.table}.{r.check.name}]  {r.value}")
            out += _wrap(r.check.why)
            if r.check.remedy:
                out += _wrap(f"REMEDY: {r.check.remedy}")
            if r.check.fork:
                out.append(f"      FORK: {r.check.fork}")
            out.append("")
    else:
        out += ["  BLOCKERS: none", ""]

    # --- errors are NOT blockers, and never fold into 'clear' -------------
    if report.errored:
        out += [f"  COULD NOT ANSWER ({len(report.errored)}) — not a pass", ""]
        for r in report.errored:
            out.append(f"  [{r.check.table}.{r.check.name}]  {r.error}")
        out.append("")

    if report.unclaimed_tables:
        out += ["  UNCLAIMED TABLES — present in the database, no check here:", ""]
        for t in report.unclaimed_tables:
            out.append(f"    {t}")
        out += _wrap(
            "A table with no check produces no findings, and no findings"
            " renders the same as clean. Add a check or record the disposition"
            " explicitly.",
            indent="    ",
        )
        out.append("")

    # --- reconciliation counts --------------------------------------------
    out += ["  RECONCILIATION COUNTS", ""]
    for r in report.results:
        if r.check.kind == COUNT and not r.error and not r.absent:
            out.append(f"    {r.check.table + '.' + r.check.name:58} {r.value}")
    out.append("")

    # --- ladder coverage ---------------------------------------------------
    out += ["  OWNER-LADDER RUNG COVERAGE (which rungs this corpus exercises)", ""]
    if isinstance(report.rungs, dict) and "error" not in report.rungs:
        for rung in sorted(RUNG_NAMES):
            n = report.rungs.get(rung, 0)
            mark = "" if n else "   <-- NOT EXERCISED by this corpus"
            out.append(f"    {RUNG_NAMES[rung]:26} {n}{mark}")
        out += _wrap(
            "A rung with no rows here cannot be validated against this"
            " database. It is covered by synthetic fixtures in"
            " tests/scripts/test_m1_ladder.py or it is not covered at all.",
            indent="    ",
        )
    else:
        out.append(f"    could not answer: {report.rungs}")
    out.append("")

    # --- disclosures -------------------------------------------------------
    out += ["  DISCLOSURES", ""]
    for r in report.results:
        if r.check.kind != DISCLOSURE or r.error or r.absent:
            continue
        out.append(f"    {r.check.table}.{r.check.name}:")
        for row in r.rows:
            out.append("      " + "  ".join(str(c) for c in row))
        out.append("")

    if report.fc8:
        out += [
            "    fc8_gate (imported, not re-implemented):",
            f"      verdict: {report.fc8.get('verdict', report.fc8)}",
        ]
        if "live_local_upload_media_items" in report.fc8:
            out += [
                f"      live local/upload media_items:  "
                f"{report.fc8['live_local_upload_media_items']}",
                f"      history on local/upload media:  "
                f"{report.fc8['posting_history_rows_on_local_upload_media']}",
            ]
        out.append("")

    # --- open forks ------------------------------------------------------
    forks = sorted({r.check.fork for r in report.results if r.check.fork})
    if forks:
        out += ["  OPEN FORKS this run has measurements for:", ""]
        for name in forks:
            hit = [
                r
                for r in report.results
                if r.check.fork == name and (r.value or r.rows)
            ]
            state = "live on this data" if hit else "not live on this data"
            out.append(f"    {name:34} {state}")
        out.append("")

    # --- coverage statement, always ---------------------------------------
    ran = len([r for r in report.results if not r.error and not r.absent])
    out += [
        "  COVERAGE",
        f"    {ran} of {len(report.results)} checks ran across"
        f" {len({c.table for c in CHECKS})} legacy tables.",
    ]
    if report.errored:
        out.append(f"    {len(report.errored)} could not be answered (see above).")
    if report.unclaimed_tables:
        out.append(
            f"    {len(report.unclaimed_tables)} table(s) in the database are"
            " unclaimed by any check."
        )
    if report.absent:
        out.append(
            f"    {len(report.absent)} check(s) skipped — their table is not in"
            " this database: "
            + ", ".join(sorted({r.check.table for r in report.absent}))
            + ". Skipped is not passed."
        )
    code = report.exit_code()
    verdict = {
        CLEAR: "CLEAR — no blocker; the transform has a path for every row",
        HALT: "HALT — the transform cannot run as specified",
        ERROR: "ERROR — a check could not be answered; this is not a pass",
        INCOMPLETE: "INCOMPLETE — a table has no check; this is not a pass",
    }[code]
    out += ["", f"  VERDICT: {verdict}", ""]
    return "\n".join(out)


def to_json(report: Report) -> dict:
    return {
        "verdict": {
            CLEAR: "CLEAR",
            HALT: "HALT",
            ERROR: "ERROR",
            INCOMPLETE: "INCOMPLETE",
        }[report.exit_code()],
        "blockers": [
            {
                "table": r.check.table,
                "check": r.check.name,
                "value": r.value,
                "why": r.check.why,
                "remedy": r.check.remedy,
                "fork": r.check.fork or None,
            }
            for r in report.blockers
        ],
        "counts": {
            f"{r.check.table}.{r.check.name}": r.value
            for r in report.results
            if r.check.kind == COUNT and not r.error and not r.absent
        },
        "disclosures": {
            f"{r.check.table}.{r.check.name}": r.rows
            for r in report.results
            if r.check.kind == DISCLOSURE and not r.error and not r.absent
        },
        "errors": {f"{r.check.table}.{r.check.name}": r.error for r in report.errored},
        "unclaimed_tables": report.unclaimed_tables,
        "skipped_absent_tables": sorted({r.check.table for r in report.absent}),
        "ladder_rung_coverage": {
            RUNG_NAMES[k]: v for k, v in report.rungs.items() if isinstance(k, int)
        },
        "fc8": report.fc8,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dsn", required=True, help="legacy database DSN (read-only)")
    p.add_argument(
        "--schema",
        default="public",
        help="schema holding the legacy tables — 'public' before the M.3 step-3c"
        " move, 'legacy' after it",
    )
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = p.parse_args(argv)

    try:
        conn = psycopg2.connect(args.dsn)
    except Exception as exc:  # noqa: BLE001
        print(
            f"m1_preflight ERROR: cannot connect: {type(exc).__name__}",
            file=sys.stderr,
        )
        return ERROR

    conn.set_session(readonly=True)
    try:
        report = run(conn, args.schema)
    finally:
        conn.close()

    print(json.dumps(to_json(report), indent=2) if args.json else render(report))
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
