-- Migration: 056_machinery_tables.sql
-- Description: F.2.5 — plan 02 §5 and §6's execution machinery. Five tables
--   (jobs, channel_outbox, provider_operations, command_dedup, rate_counters),
--   three touch triggers and nine indexes, one of which lands on an earlier
--   increment's table.
--
--   SPANS TWO PLAN SECTIONS, and the file keeps the plan's own markers rather
--   than inventing one: §5 is `jobs`, §6 is the outbound-effects trio plus
--   dedup and rate counters. Four `-- [§n ...]` markers survive in the body
--   below, exactly as the stream carries them.
--
--   A CONTIGUOUS PREFIX OF THE ADVERTISED STREAM, and that is the whole
--   contract this file is under. `04` §0.2 arm (b) diffs the concatenated
--   target lineage against the expanded 02+07 stream as an ORDERED PREFIX, so
--   this file is statements 77..93 of that stream in that order, continuing
--   055's 45..76, 054's 22..44, 053's 2..21 and 052's 0..1. Nothing here is
--   authored — the body is the plan's own SQL, extracted from the stream
--   rather than transcribed. Edit the plan and the manifest ratchet, never
--   this file alone.
--
--   NUMBERED 056. 054 is F.2.3 and 055 is F.2.4, both merged. PR #840 still
--   proposes a 054 for a LEGACY-lineage change and is blocked pending a
--   lineage ruling; it collides with the merged 054 independently of this file
--   and must renumber either way. Verified against the corpus and against
--   every open PR touching `scripts/migrations/`, not assumed from the highest
--   number on disk.
--
--   THE LAST OF THE `02` TABLE INCREMENTS. After this the lineage holds all 23
--   of `02`'s tables (split §4), and the remaining work changes character:
--   F.2.6 is the grant matrix, F.2.7 is RLS and policies — the increment that
--   finally closes the tenancy gate — and F.2.8 the SECURITY DEFINER doors.
--
--   NO POLICIES, AND NO RLS — deliberately, per the #806 Fork 1 ruling (a) and
--   the ratified split §3: F.2.2-F.2.5 land tables, triggers and indexes, and
--   RLS plus policies for all of them land together in F.2.7. This file is the
--   last increment that leaves the tenancy gate red by the plan's own order.
--
--   ONE INDEX LANDS ON AN EARLIER INCREMENT'S TABLE. `ix_intents_parked` is
--   created on `post_intents`, which 055 created. That is the stream's order
--   rather than a liberty taken here — the same shape as 055's five tg_audit_*
--   triggers landing on 053's and 054's tables — and it is why the reap index
--   is grouped under §6 with the outbox rather than with the intent ledger.
--
--   TWO OF THE FIVE TABLES CARRY NO TOUCH TRIGGER. `command_dedup` and
--   `rate_counters` have no `updated_at` at all: both are write-once-then-read
--   coordination records rather than mutable rows, so `timestamps()` is not
--   uniform across this increment's models either.
--
--   DEPENDS ON 052 AND 055. Every touch trigger calls `trg_touch_updated_at`
--   (052); `ix_intents_parked` requires `post_intents` to exist (055).
--
--   ABOVE THE LINEAGE BOUNDARY. 051 renames the legacy schema out of public;
--   these objects are created into the empty public it leaves behind. The
--   running application does not see them until the M.3 cutover.
--
-- Rollback: DROP TABLE IF EXISTS rate_counters, command_dedup,
--   provider_operations, channel_outbox, jobs CASCADE;
--   DROP INDEX IF EXISTS ix_intents_parked;
--   The index is dropped explicitly because its table belongs to 055 and so
--   survives this file's rollback.
-- Created: 2026-08-19
-- Issue: #806
-- EVERY POSTCONDITION BELOW MUST STAY TRUE FOREVER, not merely at the end of
--   this file's run. `migration_runner.py` derives a file's permanent ADOPTION
--   PROBE from its postconditions when the adoption manifest carries no entry,
--   so each line answers two questions: "did this file do its job just now" and
--   "has this file ever been applied to this database". A postcondition
--   asserting the ABSENCE of state a LATER increment adds passes the first and
--   then fails the second. So every line below is scoped to an object 056
--   itself creates, and none asserts that something does not exist: F.2.7
--   enables RLS on these tables and attaches their policies, and that may not
--   retroactively un-apply this migration.
-- runner:postcondition SELECT count(*) = 5 FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('jobs','channel_outbox','provider_operations','command_dedup','rate_counters')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'uq_jobs_serialized_lease')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'ix_intents_parked')
-- runner:postcondition SELECT count(*) = 3 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND NOT t.tgisinternal AND t.tgname IN ('tg_touch_jobs','tg_touch_channel_outbox','tg_touch_provider_operations')

-- [§5 jobs table + claim/lease/retire indexes]
-- (this table doubles as the 04 step-8 identity guards' target-only marker — it has no
-- legacy counterpart; renaming it is a change to those guards)
CREATE TABLE jobs (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind              TEXT NOT NULL CONSTRAINT ck_jobs_kind CHECK (kind IN (
                      -- tenant kinds (workspace_id NOT NULL):
                      'plan_slot','publish_pipeline','deliver_outbox','sync_media_source',
                      'first_ingest_chunk','refresh_credential','offboard_workspace',
                      'revoke_workspace_credentials','reauth_prompt',
                      -- system kinds (workspace_id NULL):
                      'reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
                      'reencrypt_credentials','send_email')),
  workspace_id      UUID NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  lane              TEXT NOT NULL CONSTRAINT ck_jobs_lane CHECK (lane IN ('interactive','bulk')),
  serialization_key TEXT NOT NULL,              -- every registry kind names its key (pass-3:
                                                -- nullability was vestigial, and a NULL key
                                                -- bypassed both serialization guards)
  run_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  state             TEXT NOT NULL DEFAULT 'ready' CONSTRAINT ck_jobs_state
                    CHECK (state IN ('ready','leased','succeeded','failed','review_required','cancelled')),
  cancel_requested  BOOLEAN NOT NULL DEFAULT false,
  attempts          INTEGER NOT NULL DEFAULT 0,
  max_attempts      INTEGER NOT NULL,
  deadline_at       TIMESTAMPTZ NULL,
  locked_by         TEXT NULL,
  locked_until      TIMESTAMPTZ NULL,
  lease_token       UUID NULL,
  payload           JSONB NOT NULL DEFAULT '{"v":1}'
                    CONSTRAINT ck_jobs_payload_v CHECK (jsonb_typeof(payload->'v') = 'number'),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_jobs_system_kinds CHECK (
    (workspace_id IS NULL) = (kind IN
      ('reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
       'reencrypt_credentials','send_email')))
);

CREATE TRIGGER tg_touch_jobs BEFORE UPDATE ON jobs
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE INDEX ix_jobs_claim ON jobs (lane, run_at) WHERE state = 'ready';

CREATE INDEX ix_jobs_lease_expiry ON jobs (locked_until) WHERE state = 'leased';

CREATE UNIQUE INDEX uq_jobs_serialized_lease ON jobs (serialization_key) WHERE state = 'leased';

CREATE INDEX ix_jobs_retire ON jobs (updated_at)
  WHERE state IN ('succeeded','cancelled','failed','review_required');

-- ix_jobs_retire gives the retention sweep an age-ordered walk over exactly the swept rows
-- (updated_at = terminalization time — §0 stamps); without it every sweep batch re-scans the
-- millions of terminal rows that accrue at envelope. Same pattern on channel_outbox and
-- provider_operations below.
-- uq_jobs_serialized_lease is THE serialization guard: two leased jobs with one key are
-- impossible by constraint, not by claim-query discipline. The claim query merely avoids
-- most conflicts; the index makes the race lose correctly. ck_jobs_system_kinds is an
-- EQUIVALENCE (pass 3): a system kind with a workspace, or a tenant kind without one, is a
-- constraint violation — the one-way form let malformed producer rows through in both
-- directions the registry never intended.

-- [§6 channel_outbox + provider_operations + ix_intents_parked]
CREATE TABLE channel_outbox (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id         UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  binding_id           UUID NOT NULL,
  kind                 TEXT NOT NULL CONSTRAINT ck_outbox_kind
                       CHECK (kind IN ('approval_prompt','prompt_supersede','notification','ack',
                                       'invitation')),
  intent_id            UUID NULL,               -- plain UUID ref (intent may be terminal-frozen)
  payload              JSONB NOT NULL           -- {v:1, ...} channel-NEUTRAL content
                       CONSTRAINT ck_outbox_payload_v CHECK (jsonb_typeof(payload->'v') = 'number'),
  state                TEXT NOT NULL DEFAULT 'pending' CONSTRAINT ck_outbox_state
                       CHECK (state IN ('pending','sending','sent','ambiguous','failed','superseded')),
  attempts             INTEGER NOT NULL DEFAULT 0,
  external_message_ref TEXT NULL,               -- tg message id after send
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT fk_outbox_binding FOREIGN KEY (workspace_id, binding_id)
    REFERENCES channel_bindings (workspace_id, id) ON DELETE CASCADE
);

CREATE TRIGGER tg_touch_channel_outbox BEFORE UPDATE ON channel_outbox
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE INDEX ix_outbox_due ON channel_outbox (binding_id, created_at) WHERE state = 'pending';

CREATE INDEX ix_outbox_retire ON channel_outbox (updated_at)
  WHERE state IN ('sent','superseded','failed','ambiguous');

-- retention walk (§5 pattern)

CREATE TABLE provider_operations (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id        UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  intent_id           UUID NOT NULL,            -- plain UUID ref (see fk note on post_intents)
  provider            TEXT NOT NULL CONSTRAINT ck_ops_provider CHECK (provider IN ('ig')),
  op_kind             TEXT NOT NULL CONSTRAINT ck_ops_kind
                      CHECK (op_kind IN ('container_create','publish')),
  business_key        TEXT NOT NULL,
  generation          INTEGER NOT NULL DEFAULT 1,
  state               TEXT NOT NULL DEFAULT 'permitted' CONSTRAINT ck_ops_state
                      CHECK (state IN ('permitted','succeeded','failed','ambiguous')),
  lease_token         UUID NOT NULL,            -- the lease that authorized this permit
  response_ref        JSONB NULL,               -- {v:1, container_id?|media_id?|status_code?|error?}
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_ops_business_key UNIQUE (business_key)
);

CREATE TRIGGER tg_touch_provider_operations BEFORE UPDATE ON provider_operations
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE INDEX ix_ops_retire ON provider_operations (updated_at)
  WHERE state IN ('succeeded','failed');

-- retention walk; ambiguous rows are excluded
                                                -- until the reconciler terminalizes them

-- The reconciler and the parked-intent alarm drive off post_intents, not this table:
CREATE INDEX ix_intents_parked ON post_intents (entered_state_at)
  WHERE state IN ('publishing_ambiguous','review_required');

-- (one partial index serves the 60 s reconciler sweep, the 15 min alarm, and the 06 §5 operator
-- list — without it those recurring scans would ride the live-subject index and filter the whole
-- live set forever.)

-- [§6 command_dedup]
CREATE TABLE command_dedup (
  channel      TEXT NOT NULL,                   -- 'telegram' | 'web' | 'cli'
  principal    TEXT NOT NULL,                   -- '' for telegram (update ids are issued
                                                -- bot-globally by Telegram, already unique);
                                                -- session id (web) / service-token id (cli),
                                                -- so a key collision across tenants is
                                                -- structurally impossible (pass 3)
  external_ref TEXT NOT NULL,                   -- telegram update_id; web/cli idempotency token
  fingerprint  TEXT NOT NULL,                   -- SHA256 over the normalized command payload
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),  -- §0 insert-only class: rows never update
  PRIMARY KEY (channel, principal, external_ref)
);

-- Idempotent admission: the adapter inserts before dispatching the command. A duplicate delivery
-- hits the PK; the adapter then compares fingerprints (pass 3 — request binding): SAME
-- fingerprint ⇒ a true replay, acknowledged without re-execution (200 replayed callbacks ⇒ one
-- command); DIFFERENT fingerprint ⇒ the key was reused for different content — rejected as a
-- 409 conflict, never silently swallowed as a replay (a reused web/cli idempotency token with a
-- new command body is a caller bug or an attack; treating it as a replay would silently drop
-- the second command). Rows age out via retention (`05`) — the replay window Telegram can
-- produce is hours, the retention class keeps days.

-- [§6 rate_counters + retire index]
CREATE TABLE rate_counters (
  scope          TEXT NOT NULL
                 CONSTRAINT ck_rate_scope CHECK (scope IN
                   ('tg_chat','tg_global','ws_admission','preauth_ip','email_global')),
  key            TEXT NOT NULL,                 -- per scope: binding id | '' (one global row) |
                                                -- workspace id | client ip
  window_start   TIMESTAMPTZ NOT NULL,          -- fixed window: now() truncated to the scope's
                                                -- window length (05 owns lengths and limits)
  count          INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_rate_nonneg CHECK (count >= 0),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),  -- §0 insert-only-class stamps: only count
  PRIMARY KEY (scope, key, window_start)              -- moves; age is immutable in window_start
);

CREATE INDEX ix_rate_retire ON rate_counters (window_start);
