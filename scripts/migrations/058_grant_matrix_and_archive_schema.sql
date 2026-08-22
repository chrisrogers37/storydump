-- Migration: 058_grant_matrix_and_archive_schema.sql
-- Description: F.2.6 — plan 02 §7-DDL's grant matrix, plus the `archive`
--   schema and its grant. Thirty grants/revokes, one CREATE SCHEMA, one grant
--   on it. NO TABLES, NO INDEXES, NO TRIGGERS, NO FUNCTIONS.
--
--   A CONTIGUOUS PREFIX OF THE ADVERTISED STREAM, and that is the whole
--   contract this file is under. `04` §0.2 arm (b) diffs the concatenated
--   target lineage against the expanded 02+07 stream as an ORDERED PREFIX, so
--   this file is statements 94..125 of that stream in that order, continuing
--   056's 77..93. Nothing here is authored — the body is the plan's own SQL,
--   extracted from the stream rather than transcribed. Edit the plan and the
--   manifest ratchet, never this file alone.
--
--   FIRST INCREMENT WITH NO TARGET MODELS, and that is a consequence of the
--   parity rule rather than an exception to it. From F.2.2 an increment has
--   been migration PLUS models, because lane parity compares the replayed
--   `public` against `create_all` on `TargetBase` and a table on one side only
--   is drift. This increment creates ZERO relations, so there is nothing for
--   `create_all` to render and no model to write; the 23 tables it grants on
--   all landed in F.2.2-F.2.5. Verified by running rather than argued: the
--   lane parity test passes with no change to `src/models/target/`.
--
--   STILL SOURCED FROM `02`, despite being past that document's table
--   sections. Measured, not assumed: `02-domain-model.md` contributes stream
--   indices 0..240 and `07-security-model.md` begins at 241, so 94..125 is
--   `02` §7-DDL. `07`'s three auth-plane tables arrive at F.2.9.
--
--   THE PLAN'S BLOCK MARKER BELOW IS WIDER THAN THIS FILE. It reads "§7-DDL
--   grants + RLS enablement + policies", but only the grants are here:
--   RLS enablement and the 53 policies are statements 126..201, which is
--   F.2.7. The marker is kept verbatim because it is the plan's, not because
--   it describes this increment's boundary.
--
--   ROLES ARE NOT CREATED HERE, and the plan is emphatic about it (pass 7,
--   R6's P0; D40). This stream executes as `svc_migration`, which must not
--   hold role DDL's cluster privileges. The seven service roles these grants
--   name are provisioned by the `04` window-bootstrap artifact — the one place
--   guarded DDL is legal — so this file assumes them and would fail loudly
--   against a cluster where the bootstrap has not run.
--
--   THE TENANCY GATE IS STILL RED AFTER THIS FILE, by the plan's own order.
--   Split §4 records F.2.6 as 23 tables / 0 RLS / 0 policies — unchanged from
--   F.2.5, because grants narrow VERBS and RLS narrows ROWS. F.2.7 is the
--   increment that closes it.
--
--   DEPENDS ON 053-056. Every GRANT names a table one of them created; a grant
--   on a missing relation is a hard error, which is why the grant matrix could
--   not have landed earlier in the stream.
--
--   ABOVE THE LINEAGE BOUNDARY. 051 renames the legacy schema out of public;
--   these grants apply to the target schema built into the empty public it
--   leaves behind. The running application does not see them until M.3.
--
-- Rollback: DROP SCHEMA IF EXISTS archive CASCADE;
--   REVOKE ALL ON ALL TABLES IN SCHEMA public FROM svc_ingress, svc_worker,
--   svc_claim, svc_clock, svc_maintenance, svc_membership;
--   REVOKE USAGE ON SCHEMA public FROM svc_ingress, svc_worker, svc_claim,
--   svc_clock, svc_maintenance, svc_membership;
--   Column-level grants go with the table-level REVOKE ALL. The roles
--   themselves are the bootstrap's, not this file's, and are left alone.
-- Created: 2026-08-19
-- Issue: #806
-- EVERY POSTCONDITION BELOW MUST STAY TRUE FOREVER, not merely at the end of
--   this file's run. `migration_runner.py` derives a file's permanent ADOPTION
--   PROBE from its postconditions when the adoption manifest carries no entry,
--   so each line answers two questions: "did this file do its job just now" and
--   "has this file ever been applied to this database". A postcondition
--   asserting the ABSENCE of state a LATER increment adds passes the first and
--   then fails the second. So every line below is scoped to a grant or object
--   057 itself makes, and none asserts that something does not exist — F.2.7
--   enables RLS on these same tables, which narrows rows without touching any
--   verb granted here.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'archive')
-- runner:postcondition SELECT has_schema_privilege('svc_maintenance', 'archive', 'CREATE')
-- runner:postcondition SELECT has_table_privilege('svc_clock', 'jobs', 'INSERT')
-- runner:postcondition SELECT has_table_privilege('svc_membership', 'workspace_members', 'INSERT')
-- runner:postcondition SELECT has_column_privilege('svc_clock', 'ig_accounts', 'next_slot_at', 'UPDATE')

-- [§7-DDL grants + RLS enablement + policies (declares the 2 pattern lists)]
-- ROLES ARE NOT CREATED HERE (pass 7 — R6's P0; the split and its rejected alternatives: D40).
-- This stream executes as svc_migration, which must not hold role DDL's cluster privileges;
-- the seven service roles (inventory: §7 above) are provisioned by the 04 window-bootstrap
-- artifact, the one place guarded/conditional DDL is legal — the pass-6 "production adoption
-- skips roles that exist" prose escape is dead, and this stream stays byte-fixed AND
-- executable by its declared production actor.
-- Passwords and connection strings are deployment env, never DDL.

-- GRANT BASELINE: nothing by default, then narrow verb grants. RLS then narrows rows.
-- (Pass-5 simplification: the pass-3 payload-free VIEW is gone — svc_clock's column-level
-- SELECT grant below enforces the identical property with one column list instead of two,
-- and one fewer object. The clock reads oauth_credentials directly; encrypted_payload is
-- simply not in its grant, so selecting it fails at the privilege layer.)
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;

GRANT USAGE ON SCHEMA public
  TO svc_ingress, svc_worker, svc_claim, svc_clock, svc_maintenance, svc_membership;

-- Login verb grants. NO DELETE APPEARS ANYWHERE IN THIS BLOCK — §0's "runtime never deletes",
-- structural: deletes exist only inside door bodies below, as their NOLOGIN owners.
GRANT SELECT, INSERT, UPDATE ON
  workspaces, workspace_members, channel_bindings, workspace_invitations,
  ig_accounts, provider_quarantine, media_sources, oauth_credentials,
  media_items, post_locks, post_intents, daily_post_counts,
  channel_outbox, provider_operations, category_post_case_mix,
  jobs, users, user_identities, onboarding_sessions
  TO svc_ingress, svc_worker;

GRANT SELECT, INSERT, UPDATE ON rate_counters TO svc_ingress, svc_worker;

GRANT SELECT, INSERT ON command_dedup TO svc_ingress;

GRANT SELECT ON post_intent_transitions
  TO svc_ingress, svc_worker, svc_claim, svc_clock, svc_maintenance, svc_membership;

GRANT INSERT ON audit_events
  TO svc_ingress, svc_worker, svc_claim, svc_clock, svc_maintenance, svc_membership;

GRANT SELECT ON audit_events TO svc_ingress, svc_worker;

-- the web audit view (06 §7)

-- Door-owner verb grants (each enumerated privilege backs exactly one body below).
GRANT SELECT, UPDATE ON jobs TO svc_claim;

GRANT SELECT ON provider_quarantine TO svc_claim;

GRANT SELECT ON workspaces, ig_accounts, media_sources TO svc_clock;

GRANT UPDATE (next_slot_at)    ON ig_accounts   TO svc_clock;

-- column-grant: the tick advances
GRANT UPDATE (next_sync_at)    ON media_sources TO svc_clock;

-- machinery columns and nothing else
GRANT SELECT (id, workspace_id, ig_account_id, media_source_id, provider,
              expires_at, next_refresh_at, state) ON oauth_credentials TO svc_clock;

GRANT UPDATE (next_refresh_at) ON oauth_credentials TO svc_clock;

GRANT SELECT, INSERT ON jobs TO svc_clock;

GRANT SELECT, UPDATE ON post_intents TO svc_maintenance;

-- reaper edge flips
GRANT SELECT, DELETE ON post_locks TO svc_maintenance;

-- expired-lock purge
GRANT SELECT, UPDATE ON workspace_invitations TO svc_maintenance;

-- expiry state flips
GRANT SELECT, DELETE ON onboarding_sessions TO svc_maintenance;

-- stale-session purge
GRANT SELECT, UPDATE, DELETE ON jobs TO svc_maintenance;

-- lease re-ready + retention
GRANT SELECT, DELETE ON channel_outbox, provider_operations,
                        daily_post_counts, rate_counters TO svc_maintenance;

-- retention classes
GRANT SELECT, DELETE ON command_dedup TO svc_maintenance;

-- fn_auth_plane_sweep's third leg
                        -- (R5: the p_maint_dedup policy existed with no table grant behind it —
                        -- a policy narrows rows but cannot confer the verb; the sweep hit
                        -- permission denied at the GRANT layer)
GRANT SELECT, DELETE ON audit_events TO svc_maintenance;

-- export-then-delete only
GRANT SELECT, DELETE ON workspaces TO svc_maintenance;

-- fn_offboard_finalize
GRANT SELECT ON workspaces TO svc_membership;

GRANT SELECT, UPDATE ON workspace_invitations TO svc_membership;

GRANT SELECT, INSERT ON workspace_members TO svc_membership;

GRANT SELECT ON channel_bindings TO svc_membership;

GRANT INSERT ON channel_outbox TO svc_membership;

-- The archive schema: created empty here; M.3 snapshot migrations ALTER their tables' OWNER TO
-- svc_maintenance so retention can DROP them on expiry (05). No login role receives any grant.
CREATE SCHEMA archive;

GRANT USAGE, CREATE ON SCHEMA archive TO svc_maintenance;
