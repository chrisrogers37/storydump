-- Migration: 055_accounts_sources_media_tables.sql
-- Description: F.2.3 — plan 02 §2's accounts, sources and media tables. Six of
--   them: ig_accounts, provider_quarantine, media_sources, oauth_credentials,
--   media_items, post_locks, with their six touch triggers and eleven
--   partial/plain indexes.
--
--   A CONTIGUOUS PREFIX OF THE ADVERTISED STREAM, and that is the whole
--   contract this file is under. `04` §0.2 arm (b) diffs the concatenated
--   target lineage against the expanded 02+07 stream as an ORDERED PREFIX, so
--   this file is statements 22..44 of that stream in that order, continuing
--   053's 2..21 and 052's 0..1. Nothing here is authored — the body is the
--   plan's own SQL, extracted from the stream rather than transcribed. Edit
--   the plan and the manifest ratchet, never this file alone.
--
--   NO POLICIES, AND NO RLS — deliberately, per the #806 Fork 1 ruling (a) and
--   the ratified split (`documentation/planning/2026-08-14-f2-increment-split`,
--   §3): F.2.2-F.2.5 land tables, triggers and indexes, and RLS plus policies
--   for all of them land together in F.2.7. The advertised stream creates all
--   26 tables before the first ENABLE ROW LEVEL SECURITY, so a table increment
--   carrying its own policy would NOT be a stream prefix and would fail arm (b)
--   at the first policy statement.
--
--   ALL SIX ARE TENANT-KEYED — every table here carries a workspace_id FK to
--   workspaces(id) ON DELETE CASCADE, verified against this file's own DDL
--   rather than assumed. They carry no policy until F.2.7, and the split doc
--   §4 records this file as moving the lineage from 7 tables to 13 with RLS
--   still at 0. That window is the ruling's stated cost, not an oversight;
--   `test_lineage_lane.py` compares the replayed schema against the tenancy
--   state the stream's prefix of the SAME LENGTH implies, so a tenant-keyed
--   table arriving here without RLS is CORRECT.
--
--   DEPENDS ON 052 AND 053. `ck_iga_tz_valid` and `ck_src_tz_valid` call
--   `fn_safe_tz` and every touch trigger calls `trg_touch_updated_at` (both
--   052); every table here references `workspaces(id)` and media_items
--   references `media_sources`, so 053's tenancy root must exist first.
--
--   ABOVE THE LINEAGE BOUNDARY. 051 renames the legacy schema out of public;
--   these tables are created into the empty public it leaves behind. The
--   running application does not see them until the M.3 cutover.
--
-- Rollback: DROP TABLE IF EXISTS post_locks, media_items, oauth_credentials,
--   media_sources, provider_quarantine, ig_accounts CASCADE;
--   Drops only this file's tables; 052's shared functions and 053's tables
--   survive, since later increments depend on them.
-- Created: 2026-08-19
-- Issue: #806
-- EVERY POSTCONDITION BELOW MUST STAY TRUE FOREVER, not merely at the end of
--   this file's run. `migration_runner.py` derives a file's permanent ADOPTION
--   PROBE from its postconditions when the adoption manifest carries no entry,
--   so each line answers two questions: "did this file do its job just now" and
--   "has this file ever been applied to this database". A postcondition
--   asserting the ABSENCE of state a LATER increment adds passes the first and
--   then fails the second. So every line below is scoped to an object 054
--   itself creates, and none asserts that something does not exist: F.2.7
--   enables RLS on these tables and attaches their policies, and that may not
--   retroactively un-apply this migration.
-- runner:postcondition SELECT count(*) = 6 FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('ig_accounts','provider_quarantine','media_sources','oauth_credentials','media_items','post_locks')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'uq_ig_account_live')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'uq_credential_per_account')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'uq_lock_ws_scope')
-- runner:postcondition SELECT count(*) = 6 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND NOT t.tgisinternal AND t.tgname IN ('tg_touch_ig_accounts','tg_touch_provider_quarantine','tg_touch_media_sources','tg_touch_oauth_credentials','tg_touch_media_items','tg_touch_post_locks')

-- [§2 accounts/sources/media tables]
CREATE TABLE ig_accounts (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id         UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  provider_account_ref TEXT NOT NULL,           -- Meta IG user id (the REAL account)
  handle               VARCHAR(50) NULL,
  display_name         VARCHAR(100) NULL,
  state                TEXT NOT NULL DEFAULT 'active'
                       CONSTRAINT ck_ig_accounts_state
                       CHECK (state IN ('active','reauth_required','disabled','moved')),
  -- per-account schedule overrides; NULL = inherit the workspace column (§1):
  posts_per_day        INTEGER NULL
                       CONSTRAINT ck_iga_ppd CHECK (posts_per_day BETWEEN 1 AND 50),
  posting_hours_start  INTEGER NULL CONSTRAINT ck_iga_hs CHECK (posting_hours_start BETWEEN 0 AND 23),
  posting_hours_end    INTEGER NULL CONSTRAINT ck_iga_he CHECK (posting_hours_end BETWEEN 0 AND 23),
  tz                   TEXT NULL
                       CONSTRAINT ck_iga_tz_valid CHECK (tz IS NULL OR fn_safe_tz(tz) = tz),
  -- scheduling state (the clock's O(due) columns, H3):
  next_slot_at         TIMESTAMPTZ NULL,
  last_posted_at       TIMESTAMPTZ NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_ig_accounts_ws_id UNIQUE (workspace_id, id)
);

CREATE TRIGGER tg_touch_ig_accounts BEFORE UPDATE ON ig_accounts
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE UNIQUE INDEX uq_ig_account_live ON ig_accounts (workspace_id, provider_account_ref)
  WHERE state <> 'moved';

CREATE INDEX ix_ig_accounts_due ON ig_accounts (next_slot_at)
  WHERE state = 'active' AND next_slot_at IS NOT NULL;

-- Workspace-owned (FC-1); the schedule is PER ACCOUNT (a workspace's 4 accounts each have their
-- own cadence, window, tz, slot cursor — workspace columns are the defaults; 06 §multi-account).
-- The same real account in two workspaces is two rows — fork PA-1, ruling with the product owner;
-- default (a) independent-connections is what this DDL implements, and option (b) is exactly one
-- migration (a global unique on provider_account_ref over live rows). 'moved' rows are terminal
-- tombstones excluded from uniqueness so an account can move away and later return (06 §movement).
-- Real-account concurrency and Meta budgets key on provider_account_ref, never our row id
-- (§3 key 4, §8). Account-level fault quarantine lives ONLY in provider_quarantine.
-- State transitions (complete): active → reauth_required (refresh failure / provider revocation
-- (07 §3) / definitive publish-time auth-rejection — the §2 credential liveness edge (D31), same
-- transaction as the credential flip) · reauth_required → active (reconnect swaps the credential
-- payload, 07 §2) · active ↔ disabled (user command, audited) ·
-- {active,reauth_required,disabled} → moved (06 §4, terminal). The dispatcher's due-scan
-- predicate reads state='active' only.

CREATE TABLE provider_quarantine (
  workspace_id      UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  provider          TEXT NOT NULL
                    CONSTRAINT ck_quarantine_provider
                    CHECK (provider IN ('ig','telegram','cloudinary','gdrive')),
  scope_ref         TEXT NOT NULL,              -- ALWAYS a §5 serialization key ('ig:…','tg:…',
                                                -- 'src:…','cred:…') — the exact work it defers;
                                                -- provider is observability metadata, matching
                                                -- never depends on it
  quarantined_until TIMESTAMPTZ NOT NULL,
  strike_count      INTEGER NOT NULL DEFAULT 1,
  last_strike_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  alerted_at        TIMESTAMPTZ NULL,           -- notification dedup: re-alert only if > 1h old (05)
  reason            TEXT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),  -- first entry (§0 stamps; the decay
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),  -- anchor is last_strike_at, above)
  PRIMARY KEY (workspace_id, scope_ref)
);

CREATE TRIGGER tg_touch_provider_quarantine BEFORE UPDATE ON provider_quarantine
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

-- THE generic T2 fault-quarantine mechanism. Grain = the serialization key (pass-2 fix: the
-- earlier '' = whole-(workspace,provider) wildcard could not be matched against prefixed
-- serialization keys and silently made the fine grain a no-op; per-key rows are what adapters
-- can actually write — they always know the key of the job that faulted — and workspace-provider-
-- wide faults quarantine organically, key by key, as each fails). Semantics:
--   entry: the adapter upserts the row; quarantined_until = now() + backoff(strike_count) over
--          1m/5m/30m/2h/24h (05 seam); repeat entry within the decay window increments
--          strike_count, else resets to 1 (decay: last_strike_at older than 24h). The entry
--          transaction also pushes matching ready jobs out of the claim scan's way:
--          UPDATE jobs SET run_at = GREATEST(run_at, :until)
--            WHERE state='ready' AND serialization_key = :key;
--          (rare event, small ready set — keeps every subsequent claim from re-walking
--          quarantined work at the front of the run_at order).
--   effect: the §5 claim check defers late-inserted matching jobs. That is the ONLY effect.
--   exit:   passive — quarantined_until passes and the next claim proceeds; that claim IS the
--           probe (success rewrites nothing; the row is upserted again only on a fresh fault).
--   manual: clear-quarantine operator command EXPIRES the row — UPDATE SET quarantined_until =
--           now(), strike_count = 0 — and re-readies deferred run_at (audited). Expiry-by-update,
--           not DELETE: no login holds DELETE (§7); the row is overwritten by the next fault.
--   NOT quarantine: credential revocation (state='revoked' → reauth flow, 07) and Meta cap
--   error 9 (a cap, §8) — neither writes here.

CREATE TABLE media_sources (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id         UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  provider             TEXT NOT NULL
                       CONSTRAINT ck_sources_provider CHECK (provider IN ('gdrive')),
                       -- FC-8/D37: a pluggable adapter surface behind a closed CHECK (§0/D15);
                       -- the open set is D37's stated add cost. v1 ships exactly 'gdrive'
  config               JSONB NOT NULL           -- adapter-defined, versioned; gdrive: {v:1,
                       -- folder_ref:text, root_name?:text}. Each provider's shape is a D37
                       -- contract-table row
                       CONSTRAINT ck_sources_config_v CHECK (jsonb_typeof(config->'v') = 'number'),
  sync_checkpoint      JSONB NULL,              -- adapter-defined, versioned; gdrive: {v:1,
                       -- page_token?:text}; cursor-style providers use their own keys
  next_sync_at         TIMESTAMPTZ NULL,
  state                TEXT NOT NULL DEFAULT 'active'
                       CONSTRAINT ck_sources_state CHECK (state IN ('active','paused','error')),
  last_sync_success_at TIMESTAMPTZ NULL,
  alerted_at           TIMESTAMPTZ NULL,        -- source-disconnect alert dedup (legacy gdrive_alerted_at)
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_sources_ws_id UNIQUE (workspace_id, id)
);

CREATE TRIGGER tg_touch_media_sources BEFORE UPDATE ON media_sources
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE INDEX ix_sources_sync_due ON media_sources (next_sync_at)
  WHERE state = 'active' AND next_sync_at IS NOT NULL;

-- Printed BEFORE oauth_credentials because that table's composite FK targets it (replay order,
-- §0 — the pass-2 file order had these two swapped and could not replay as printed).
-- State transitions (complete): active ↔ paused (user command) · active → error (sync failure
-- classified persistent, e.g. folder gone/credential dead — alert via alerted_at dedup) ·
-- error → active (successful sync after reconnect/repair — the pre-slot or on-demand sync
-- IS the probe). The due-scan predicate reads state='active' only.

CREATE TABLE oauth_credentials (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id      UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  ig_account_id     UUID NULL,
  media_source_id   UUID NULL,
  provider          TEXT NOT NULL
                    CONSTRAINT ck_credentials_provider
                    CHECK (provider IN ('ig_login','gdrive')),
                    -- no fb_login_legacy value exists (pass 5 — `00` FC-4 application note under
                    -- FC-7). A media-source credential's provider always equals its source's
                    -- provider (D37 invariant, service-enforced; the per-source unique below
                    -- already keys by provider). Media providers extend per D37's add cost
  encrypted_payload TEXT NOT NULL,              -- Fernet/MultiFernet ciphertext (07 §rotation);
                                                -- never plaintext, never in logs/traces
  expires_at        TIMESTAMPTZ NULL,
  next_refresh_at   TIMESTAMPTZ NULL,
  state             TEXT NOT NULL DEFAULT 'active'
                    CONSTRAINT ck_credentials_state CHECK (state IN ('active','expired','revoked')),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_credentials_one_owner CHECK (num_nonnulls(ig_account_id, media_source_id) = 1),
  CONSTRAINT fk_credentials_account FOREIGN KEY (workspace_id, ig_account_id)
    REFERENCES ig_accounts (workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_credentials_source  FOREIGN KEY (workspace_id, media_source_id)
    REFERENCES media_sources (workspace_id, id) ON DELETE CASCADE
);

CREATE TRIGGER tg_touch_oauth_credentials BEFORE UPDATE ON oauth_credentials
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE UNIQUE INDEX uq_credential_per_account ON oauth_credentials (workspace_id, ig_account_id, provider)
  WHERE ig_account_id IS NOT NULL;

CREATE UNIQUE INDEX uq_credential_per_source  ON oauth_credentials (workspace_id, media_source_id, provider)
  WHERE media_source_id IS NOT NULL;

CREATE INDEX ix_credentials_refresh_due ON oauth_credentials (next_refresh_at)
  WHERE state = 'active' AND next_refresh_at IS NOT NULL;

-- Typed XOR owner FKs (not polymorphic) so the composite-FK convention holds on the MOST
-- sensitive table (C4). The per-account unique keys by provider — one credential per (account,
-- provider); with the target ig-side set at exactly 'ig_login' this is one live IG credential
-- per account row in practice, and the key shape needs no change if a provider is ever added.
-- State transitions (complete): active → expired (a definitive provider auth-rejection observed
-- on ANY Meta call — scheduled refresh, publish pipeline, or the §8 pre-check if enabled — or the
-- 07 §3 decrypt failure; the liveness-edge paragraph below states the discrimination) ·
-- active → revoked (user disconnect / offboarding / account movement — the move transaction
-- revokes the source row as it copies the payload to the target workspace, 06 §4: a move, not a
-- fork; exactly one active row per grant, so refresh can never diverge two copies) ·
-- {active,expired,revoked} → active (reconnect, 07 §2: an UPSERT on the owner unique key —
-- payload swapped and state flipped on the existing row, same row id, no delete, no
-- zero-credential window; the swap is audited).
-- THE CREDENTIAL LIVENESS EDGE (D31): 'expired' is a PROVIDER-VERDICT state, not a calendar
-- state — "the provider definitively rejected this credential", from whichever call observed it.
-- Definitive = an auth-class rejection (Meta error 190 without a revocation subcode; the
-- unparseable-token class per the corrupt-phrase discrimination already proven on main), never a
-- transient network/5xx/429/timeout fault — those stay in the R8 retry taxonomy and
-- provider_quarantine, which this edge does not touch. The observing worker performs BOTH flips
-- in the SAME transaction as the failure it is recording: this row → 'expired' and the owning
-- ig_accounts row → 'reauth_required' (§2 above). The dispatcher then mints no further intents
-- for the account, the reauth machinery prompts (06 §5), and reconnect (07 §2) restores
-- 'active'. Publish-time provider rejections are the abundant, free liveness signal: a
-- dead-but-calendar-valid token costs exactly ONE wasted attempt before dispatch stops, bounded
-- otherwise by the 05 refresh cadence — the scheduled refresh doubles as the liveness probe.
-- Tenant-plane writes only, by the worker that already holds these UPDATEs (it executes
-- refresh_credential): no new state, no new job kind, no new grants.

CREATE TABLE media_items (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id      UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id         UUID NOT NULL,
  provider_file_ref TEXT NOT NULL,              -- the adapter's CANONICAL STABLE item identifier
                                                -- (legacy source_identifier), adapter-defined
                                                -- format, opaque to core. ADAPTER CONTRACT (D37,
                                                -- normative): stable across provider-side
                                                -- rename/move and unique within the source —
                                                -- gdrive: the file id; a path is never a legal
                                                -- ref (an unstable ref makes sync mint a fresh
                                                -- row on rename, resetting recency and evading
                                                -- the media_item_id-keyed recent locks, R4/D37)
  content_hash      TEXT NOT NULL,              -- SHA256 (legacy file_hash)
  media_kind        TEXT NOT NULL
                    CONSTRAINT ck_media_kind CHECK (media_kind IN ('image','video')),
  mime_type         VARCHAR(100) NULL,
  file_name         TEXT NOT NULL,
  file_size         BIGINT NULL,
  category          TEXT NULL,
  title             TEXT NULL,
  caption           TEXT NULL,
  generated_caption TEXT NULL,
  link_url          TEXT NULL,
  tags              TEXT[] NULL,
  custom_metadata   JSONB NULL,
  thumbnail_url     TEXT NULL,
  state             TEXT NOT NULL DEFAULT 'available'
                    CONSTRAINT ck_media_state CHECK (state IN ('available','unsupported','removed')),
  times_posted      INTEGER NOT NULL DEFAULT 0,
  last_posted_at    TIMESTAMPTZ NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_media_ws_id  UNIQUE (workspace_id, id),
  CONSTRAINT fk_media_source FOREIGN KEY (workspace_id, source_id)
    REFERENCES media_sources (workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT uq_media_dedup  UNIQUE (workspace_id, content_hash)
);

CREATE TRIGGER tg_touch_media_items BEFORE UPDATE ON media_items
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE INDEX ix_media_selection ON media_items (workspace_id, state, category);

CREATE INDEX ix_media_provider_ref ON media_items (workspace_id, source_id, provider_file_ref);

-- uq_media_dedup: dedup is per-workspace BY SCHEMA; a global hash namespace is inexpressible (R4).
-- Legacy file_hash is NOT unique today (duplicates exist in production) — the M window carries a
-- human-gated dedup remediation precondition (existing dedup-media tooling) with a
-- zero-duplicates gate BEFORE the transform runs (04 M.1), same pattern as 0.3's remediation.
-- Legacy columns not carried: cloud_* transit columns (transit state is per-attempt:
-- post_intents.transit_asset_ref), instagram_media_id/backfilled_at (posted evidence is per-intent:
-- §3 ig_media_id; legacy values ride the M.1 history transform), is_active (→ state), file_path
-- (Drive path context folds into file_name; identity is provider_file_ref). No legacy
-- "unsupported" flag exists (pass-4 anchor): today Meta 9004 writes a permanent_reject LOCK, not
-- a media_items column — those rows ride the M.1 lock mapping; the target's 'unsupported' media
-- state and lock kind formalize the class going forward.
-- times_posted and last_posted_at are workspace-level ADVISORY aggregates (display/selection
-- hints; authority is the terminal intents) — per-account recency = post_locks.

CREATE TABLE post_locks (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id         UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  media_item_id        UUID NOT NULL,
  ig_account_id        UUID NULL,               -- NULL = workspace-wide; set = account-scoped
  kind                 TEXT NOT NULL
                       CONSTRAINT ck_locks_kind
                       CHECK (kind IN ('recent','skip','reject','unsupported','seasonal','hold')),
  expires_at           TIMESTAMPTZ NULL,        -- NULL = permanent
  created_by_intent_id UUID NULL,               -- plain UUID ref, §0-exemption: the intent may be
                                                -- terminal-frozen; attribution only, never joined for
                                                -- authorization
  created_by_user_id   UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_locks_recent_scope CHECK ((kind = 'recent') = (ig_account_id IS NOT NULL)),
  CONSTRAINT fk_locks_media   FOREIGN KEY (workspace_id, media_item_id)
    REFERENCES media_items (workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_locks_account FOREIGN KEY (workspace_id, ig_account_id)
    REFERENCES ig_accounts (workspace_id, id) ON DELETE CASCADE
);

CREATE TRIGGER tg_touch_post_locks BEFORE UPDATE ON post_locks
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE UNIQUE INDEX uq_lock_ws_scope ON post_locks (workspace_id, media_item_id, kind)
  WHERE ig_account_id IS NULL;

CREATE UNIQUE INDEX uq_lock_acct_scope ON post_locks (workspace_id, media_item_id, kind, ig_account_id)
  WHERE ig_account_id IS NOT NULL;

CREATE INDEX ix_locks_expiry ON post_locks (expires_at) WHERE expires_at IS NOT NULL;
