-- Migration: 059_rls_and_policies.sql
-- Description: F.2.7 — plan 02 §7-DDL's row-level security. Twenty-three
--   ALTER TABLE ... ENABLE ROW LEVEL SECURITY and fifty-three CREATE POLICY.
--   NO TABLES, NO INDEXES, NO GRANTS, NO FUNCTIONS.
--
--   THIS IS THE INCREMENT THAT CLOSES THE TENANCY GATE. Split §4 records the
--   red window as F.2.2 through F.2.6 and this file as the moment it ends:
--   23 tables / 23 RLS / 53 policies. Measured on the plan side before writing
--   a line, so the flip is evidence rather than a claim — `tenancy_violations`
--   over the stream's own prefix reports 17 violations at index 126 (through
--   F.2.6) and 0 at index 202 (through this file), with the tenant-keyed count
--   unchanged at 17. The window was red by the plan's own order, not by a
--   defect, and this is where the plan closes it.
--
--   A CONTIGUOUS PREFIX OF THE ADVERTISED STREAM, and that is the whole
--   contract this file is under. `04` §0.2 arm (b) diffs the concatenated
--   target lineage against the expanded 02+07 stream as an ORDERED PREFIX, so
--   this file is statements 126..201 of that stream in that order, continuing
--   057's 94..125. Nothing here is authored — the body is the plan's own SQL,
--   extracted from the stream rather than transcribed. Edit the plan and the
--   manifest ratchet, never this file alone.
--
--   SEVENTEEN OF THE 53 POLICIES ARE GENERATED, NOT LITERAL, and that matters
--   to anyone diffing this file against `02` by eye. `expand_policies` in
--   `scripts/advertised_ddl.py` materialises the two pattern lists the §7-DDL
--   block declares — `p_tenant` across 14 tenant-plane tables and
--   `p_user_plane` across 3 user-plane ones — so they appear in the stream,
--   and therefore here, without appearing verbatim in the plan's prose. The
--   manifest is their source of truth; `advertised_ddl_replay` asserts each
--   one exists on the replayed database by name.
--
--   STILL SOURCED FROM `02`, AND IT DOES NOT CROSS INTO `07`. Measured, not
--   assumed: `02-domain-model.md` contributes stream indices 0..240 and
--   `07-security-model.md` begins at 241, so 126..201 is entirely `02`
--   §7-DDL. The auth plane arrives at F.2.9.
--
--   NO TARGET MODELS, for the same structural reason as 057 rather than as an
--   exception. The rule since F.2.2 is migration PLUS models, because lane
--   parity compares the replayed `public` against `create_all` and a table on
--   one side only is drift. This increment creates ZERO relations: `ALTER
--   TABLE ... ENABLE ROW LEVEL SECURITY` and `CREATE POLICY` have no
--   declarative SQLAlchemy representation at all — they are not emitted by
--   `create_all` under any model definition — so there is nothing a model
--   could mirror. The 23 tables were modelled in F.2.2-F.2.5.
--
--   ENABLE WITHOUT FORCE, deliberately, and the plan's own note below says
--   why: `svc_migration` owns the tables and owner-bypass is what lets the
--   runner transform without blanket migration policies. The control is that
--   `svc_migration`'s credential exists only in the runner's deploy context.
--
--   DEPENDS ON 053-057. Every ALTER names a table one of 053-056 created; the
--   policies name the service roles 057 granted and the `04` window-bootstrap
--   provisioned. RLS narrows ROWS on top of the verbs 057 granted — the two
--   are complementary, which is why the gate stayed red after 057.
--
--   ABOVE THE LINEAGE BOUNDARY. 051 renames the legacy schema out of public;
--   this applies to the target schema built into the empty public it leaves
--   behind. The running application does not see it until the M.3 cutover.
--
-- Rollback: ALTER TABLE <each of the 23> DISABLE ROW LEVEL SECURITY;
--   DROP POLICY IF EXISTS <each of the 53> ON <its table>;
--   Dropping the policies alone is NOT a rollback: RLS enabled with no policy
--   denies all non-owner access, which is more restrictive than the state this
--   file changed, so the DISABLE must accompany it.
-- Created: 2026-08-19
-- Issue: #806
-- EVERY POSTCONDITION BELOW MUST STAY TRUE FOREVER, not merely at the end of
--   this file's run. `migration_runner.py` derives a file's permanent ADOPTION
--   PROBE from its postconditions when the adoption manifest carries no entry,
--   so each line answers two questions: "did this file do its job just now" and
--   "has this file ever been applied to this database". A postcondition
--   asserting the ABSENCE of state a LATER increment adds passes the first and
--   then fails the second. So every line below is scoped to state 058 itself
--   creates, and none asserts that something does not exist. The counts use
--   `>=` rather than `=` for the same reason: F.2.9 enables RLS on three more
--   tables and adds five more policies (split §4: 23/23/53 here, 26/26/58
--   there), and an equality here would read this
--   migration unapplied the moment that lands.
-- runner:postcondition SELECT count(*) >= 23 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity
-- runner:postcondition SELECT count(*) >= 53 FROM pg_policies WHERE schemaname = 'public'
-- runner:postcondition SELECT count(*) >= 14 FROM pg_policies WHERE schemaname = 'public' AND policyname = 'p_tenant'
-- runner:postcondition SELECT count(*) >= 3 FROM pg_policies WHERE schemaname = 'public' AND policyname = 'p_user_plane'
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'workspaces' AND policyname = 'p_tenant_workspaces')

-- ROW LEVEL SECURITY: enabled on every table in this file, at creation (FC-7 killed the staged
-- W-phase completion — there is no "later"). ENABLE without FORCE, deliberately: svc_migration
-- owns the tables and owner-bypass is what lets the runner transform without blanket migration
-- policies; the control is that svc_migration's credential exists only in the runner's deploy
-- context (F.4 gate asserts runtime env carries exactly svc_ingress and svc_worker).
ALTER TABLE users                   ENABLE ROW LEVEL SECURITY;

ALTER TABLE user_identities         ENABLE ROW LEVEL SECURITY;

ALTER TABLE onboarding_sessions     ENABLE ROW LEVEL SECURITY;

ALTER TABLE workspaces              ENABLE ROW LEVEL SECURITY;

ALTER TABLE workspace_members       ENABLE ROW LEVEL SECURITY;

ALTER TABLE workspace_invitations   ENABLE ROW LEVEL SECURITY;

ALTER TABLE channel_bindings        ENABLE ROW LEVEL SECURITY;

ALTER TABLE ig_accounts             ENABLE ROW LEVEL SECURITY;

ALTER TABLE provider_quarantine     ENABLE ROW LEVEL SECURITY;

ALTER TABLE media_sources           ENABLE ROW LEVEL SECURITY;

ALTER TABLE oauth_credentials       ENABLE ROW LEVEL SECURITY;

ALTER TABLE media_items             ENABLE ROW LEVEL SECURITY;

ALTER TABLE post_locks              ENABLE ROW LEVEL SECURITY;

ALTER TABLE category_post_case_mix  ENABLE ROW LEVEL SECURITY;

ALTER TABLE post_intents            ENABLE ROW LEVEL SECURITY;

ALTER TABLE post_intent_transitions ENABLE ROW LEVEL SECURITY;

ALTER TABLE audit_events            ENABLE ROW LEVEL SECURITY;

ALTER TABLE daily_post_counts       ENABLE ROW LEVEL SECURITY;

ALTER TABLE jobs                    ENABLE ROW LEVEL SECURITY;

ALTER TABLE channel_outbox          ENABLE ROW LEVEL SECURITY;

ALTER TABLE provider_operations     ENABLE ROW LEVEL SECURITY;

ALTER TABLE command_dedup           ENABLE ROW LEVEL SECURITY;

ALTER TABLE rate_counters           ENABLE ROW LEVEL SECURITY;

-- POLICIES, by class. The tenant predicate is NULL-safe by construction: an unset GUC yields
-- NULL, NULL matches no row, and "absent context reads nothing" is exactly the F.4 gate.

-- Class 1 — tenant-plane, the standard pair. workspaces keys on id (it IS the tenant); signup
-- pre-assigns the new id and sets the GUC before INSERT (the UoW factory requires tenant_id).
CREATE POLICY p_tenant_workspaces ON workspaces FOR ALL TO svc_ingress, svc_worker
  USING      (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON workspace_members FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- …the same two-line policy repeats, verbatim except the table name, on: workspace_invitations,
-- channel_bindings, ig_accounts, provider_quarantine, media_sources, oauth_credentials,
-- media_items, post_locks, category_post_case_mix, post_intents, daily_post_counts,
-- channel_outbox, provider_operations. The migration file spells all thirteen out; this document
-- states the pattern once and the replay fixture generates them from this list, so the plan text
-- cannot silently diverge from the file (the list IS the normative enumeration).

-- Class 2 — jobs: tenant rows in tenant context, system rows (workspace_id IS NULL) visible to
-- both logins — workers execute system kinds, and ingress produces the payload-complete
-- send_email system kind (§5 classing rule).
CREATE POLICY p_jobs ON jobs FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
              OR workspace_id IS NULL)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
              OR workspace_id IS NULL);

-- Class 3 — user-plane (users, user_identities, onboarding_sessions): no workspace key exists —
-- identity precedes tenancy (sign-in, identity upsert, onboarding all run before any
-- app.tenant_id can be set). Role-scoped row-open policies; the isolation authority for these
-- tables is the one central authorization gate (01 §Process roles), stated as a deliberate class.
CREATE POLICY p_user_plane ON users FOR ALL TO svc_ingress, svc_worker
  USING (true) WITH CHECK (true);

-- …repeated on user_identities and onboarding_sessions (same normative-list rule as class 1).

-- Class 4 — machinery counters and admission dedup.
CREATE POLICY p_rate ON rate_counters FOR ALL TO svc_ingress, svc_worker
  USING (true) WITH CHECK (true);

CREATE POLICY p_dedup ON command_dedup FOR ALL TO svc_ingress
  USING (true) WITH CHECK (true);

-- Class 5 — reference data: readable by everyone, writable by migrations only (owner bypass).
CREATE POLICY p_transitions_read ON post_intent_transitions FOR SELECT
  TO svc_ingress, svc_worker, svc_claim, svc_clock, svc_maintenance, svc_membership
  USING (true);

-- Class 6 — audit: tenant-scoped INSERT/SELECT for logins; row-open INSERT for door owners
-- (their bodies' triggers insert as the owner); retention's SELECT/DELETE for the sweep owner.
CREATE POLICY p_audit_ins ON audit_events FOR INSERT TO svc_ingress, svc_worker
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_audit_sel ON audit_events FOR SELECT TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_audit_sys ON audit_events FOR INSERT
  TO svc_claim, svc_clock, svc_maintenance, svc_membership WITH CHECK (true);

CREATE POLICY p_audit_retention_sel ON audit_events FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_audit_retention_del ON audit_events FOR DELETE TO svc_maintenance USING (true);

-- Class 7 — door-owner enumerated policies (each backs one body below; the complete list, per
-- §7's rule that every cross-tenant capability is a named reviewable object):
CREATE POLICY p_claim_jobs   ON jobs FOR ALL TO svc_claim USING (true) WITH CHECK (true);

CREATE POLICY p_claim_quar   ON provider_quarantine FOR SELECT TO svc_claim USING (true);

CREATE POLICY p_clock_ws     ON workspaces      FOR SELECT TO svc_clock USING (true);

CREATE POLICY p_clock_acct_s ON ig_accounts     FOR SELECT TO svc_clock USING (true);

CREATE POLICY p_clock_acct_u ON ig_accounts     FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);

CREATE POLICY p_clock_src_s  ON media_sources   FOR SELECT TO svc_clock USING (true);

CREATE POLICY p_clock_src_u  ON media_sources   FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);

CREATE POLICY p_clock_cred_s ON oauth_credentials FOR SELECT TO svc_clock USING (true);

CREATE POLICY p_clock_cred_u ON oauth_credentials FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);

CREATE POLICY p_clock_jobs   ON jobs FOR ALL TO svc_clock USING (true) WITH CHECK (true);

CREATE POLICY p_maint_intents ON post_intents FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_locks   ON post_locks   FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_invites ON workspace_invitations FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_onboard ON onboarding_sessions   FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_jobs    ON jobs FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_outbox  ON channel_outbox      FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_ops     ON provider_operations FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_dpc     ON daily_post_counts   FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_rate    ON rate_counters       FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_ws      ON workspaces FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_member_invites ON workspace_invitations FOR ALL TO svc_membership USING (true) WITH CHECK (true);

CREATE POLICY p_member_members ON workspace_members FOR ALL TO svc_membership USING (true) WITH CHECK (true);

CREATE POLICY p_member_binds   ON channel_bindings FOR SELECT TO svc_membership USING (true);

CREATE POLICY p_member_outbox  ON channel_outbox FOR INSERT TO svc_membership WITH CHECK (true);

CREATE POLICY p_member_ws      ON workspaces FOR SELECT TO svc_membership USING (true);

-- (command_dedup, session_tokens, oauth_states: the auth-plane policies for svc_ingress and the
-- sweep owner are printed with those tables — command_dedup above, the 07 pair in 07.)
CREATE POLICY p_maint_dedup ON command_dedup FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

-- [generated: §7-DDL policy-list expansion, 13 tenant + 2 user]
CREATE POLICY p_tenant ON workspace_invitations FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON channel_bindings FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON ig_accounts FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON provider_quarantine FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON media_sources FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON oauth_credentials FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON media_items FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON post_locks FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON category_post_case_mix FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON post_intents FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON daily_post_counts FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON channel_outbox FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON provider_operations FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_user_plane ON user_identities FOR ALL TO svc_ingress, svc_worker
  USING (true) WITH CHECK (true);

CREATE POLICY p_user_plane ON onboarding_sessions FOR ALL TO svc_ingress, svc_worker
  USING (true) WITH CHECK (true);
