-- Migration 065: `alert_stranded_sources` joins the job-kind vocabulary (#1061).
--
-- WHY A MIGRATION AT ALL, WHEN THE RECURRING SEAM IS DATA-DRIVEN. `fn_clock_tick`'s leg 1
-- loops over `jsonb_each(p_recurring)` and mints whatever keys it finds, so adding a
-- recurring kind LOOKS like configuration. It is not: the loop's body INSERTs into `jobs`,
-- and `jobs.kind` is a closed CHECK. One layer being open says nothing about the layer that
-- gates the write. That mistake was made on this very change and caught by CI, not by
-- inspection — see below for why that matters more than usual here.
--
-- TWO CONSTRAINTS GATE THIS INSERT, NOT ONE, AND FIXING ONLY THE OBVIOUS ONE STILL ABORTS.
-- `ck_jobs_kind` is the vocabulary. `ck_jobs_system_kinds` is a BICONDITIONAL:
-- `(workspace_id IS NULL) = (kind IN (<system kinds>))`. Leg 1 inserts system singletons with
-- `workspace_id = NULL`, so a kind added to the vocabulary alone makes that biconditional read
-- `true = false` and the row is still refused. Both are widened here, and the new kind goes in
-- the SYSTEM group in both, because this beat is one row that sweeps every workspace — the
-- `reap_expired` shape, not the `sync_media_source` shape.
--
-- THE FAILURE MODE THIS AVOIDS IS TOTAL, NOT PARTIAL, AND IT FAILS FIRST. The recurring mint
-- is leg 1 of five inside one `fn_clock_tick` body. The function has a single BEGIN…END with
-- NO EXCEPTION section, so it opens no subtransaction: an unhandled error anywhere aborts the
-- caller's transaction and every leg rolls back with it. A kind absent from these constraints
-- therefore does not break its own beat — it stops slot minting, credential refreshes, source
-- syncs and reauth prompts, for every workspace and every provider, on the FIRST leg, before
-- anything useful has happened. Measured rather than reasoned: with the kind armed and these
-- constraints unwidened, `test_clock_mints_loops_process_parked_parks_and_nothing_strands`
-- fails on `assert bulk.processed >= 1` with `0 >= 1` — `plan_slot`, leg 2, never minted.
--
-- The abort-everything property is fn_clock_tick's, not this change's, and it is filed
-- separately. Note for whoever picks that up: REORDERING THE LEGS DOES NOT FIX IT. All five
-- share one transaction, so a failure at leg 5 rolls back legs 1-4 exactly as a failure at
-- leg 1 does. Only a per-leg EXCEPTION block (a real subtransaction) isolates them. The
-- current order is deliberate for a different reason — the legs share one budget and each
-- draws on what the earlier ones left, so leg 1 has first claim on `p_max`.
--
-- WIDENING A CHECK IS SAFE ON EXISTING ROWS by construction: every row that satisfied the
-- narrower predicate satisfies the wider one, so the validating scan cannot fail. The
-- drop-and-add is the repo's established shape for a CHECK edit (042, 045, 046, 049).
--
-- NOT IN THIS FILE, DELIBERATELY: the clock's due-scan. Fork F4 rejected option (b) — widening
-- what `fn_clock_tick` SELECTS so it re-polls `error` sources — because it re-polls sources
-- that are dead for good reasons and turns a visible error into recurring noise. Adding an
-- allowed VALUE to a constraint is a different act from changing which rows the tick selects.
-- `fn_clock_tick` is not touched here.

BEGIN;

ALTER TABLE jobs DROP CONSTRAINT ck_jobs_kind;
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN (
  -- tenant kinds (workspace_id NOT NULL):
  'plan_slot','publish_pipeline','deliver_outbox','sync_media_source',
  'first_ingest_chunk','refresh_credential','offboard_workspace',
  'revoke_workspace_credentials','reauth_prompt',
  -- system kinds (workspace_id NULL):
  'reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
  'reencrypt_credentials','send_email','alert_stranded_sources'));

ALTER TABLE jobs DROP CONSTRAINT ck_jobs_system_kinds;
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_system_kinds CHECK (
  (workspace_id IS NULL) = (kind IN
    ('reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
     'reencrypt_credentials','send_email','alert_stranded_sources')));

COMMIT;

-- Both constraints are asserted, not just the one the defect surfaced through: fixing
-- `ck_jobs_kind` alone leaves the insert refused by the other, and a postcondition that
-- checked only the first would report this migration successful over a clock that still
-- aborts.
-- runner:postcondition SELECT pg_get_constraintdef(oid) LIKE '%alert_stranded_sources%' FROM pg_constraint WHERE conname = 'ck_jobs_kind'
-- runner:postcondition SELECT pg_get_constraintdef(oid) LIKE '%alert_stranded_sources%' FROM pg_constraint WHERE conname = 'ck_jobs_system_kinds'
