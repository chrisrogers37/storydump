-- Migration 066: the "no media available" notice marker (D3 of the acceptance
-- checklist #1090; `06` §5's slot-missed row, `05`'s "no media available notice
-- dedup 24 h").
--
-- `06` §5 names the mechanism as "slot planner + notification dedup". The slot
-- planner existed; the dedup had nowhere to live, so neither did the notice --
-- a grep for a no-media notification returned nothing anywhere in the tree.
-- This file is the second half only: the marker. The producer that stamps it
-- ships in the same change set (`scheduler.execute_plan_slot`).
--
-- KEYED PER ACCOUNT, NOT PER WORKSPACE. A slot is minted per (workspace,
-- ig_account) -- `plan_slot`'s payload names the account and `ig_accounts`
-- carries the cadence -- so two accounts in one workspace starve
-- independently. A workspace-keyed marker would let the first account's notice
-- silence the second account's FIRST notice, which is the failure D3 exists to
-- rule out ("you are told once", not "one of your accounts is told once").
--
-- NO INDEX, deliberately, and the contrast with 062 is the reason. 062 added
-- `ix_ig_accounts_reauth_due` because the clock SCANS ig_accounts for accounts
-- whose prompt is due -- a predicate over the whole table. This marker is only
-- ever read for the one account a `plan_slot` job already names, by primary
-- key, so an index on it would be dead weight on every write and would serve
-- no query that exists.
--
-- NO GRANT, deliberately, same shape of contrast. 057 (:100-105) gives
-- svc_worker table-level SELECT/INSERT/UPDATE on ig_accounts, so a new column
-- is already reachable by the role that stamps it. 062 needed an explicit
-- `GRANT UPDATE (last_reauth_prompt_at) ... TO svc_clock` because svc_clock's
-- grant is COLUMN-scoped (057 :129 names next_slot_at and nothing else); the
-- clock does not touch this marker, so no column grant is added. Adding one
-- would widen svc_clock's reach for no caller.
--
-- Adoption evidence + post-apply verification (#997): one catalog read for the
-- one structural thing this file does. Catalog-only, matching 062 -- a
-- has_*_privilege probe RAISES when its role is absent and the runner reads a
-- raising probe as a hard failure.
-- runner:postcondition SELECT count(*) = 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'ig_accounts' AND column_name = 'last_no_media_notice_at'

ALTER TABLE ig_accounts
  ADD COLUMN last_no_media_notice_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN ig_accounts.last_no_media_notice_at IS
  'When this account last told its workspace that a slot found no media. '
  'NULL = never told. Stamped by the slot planner at NOTICE time, in the same '
  'transaction as the outbox row, so a rolled-back plan takes its notice with '
  'it. The dedup window is 05 (24 h) and lives in WorkerConfig, not here: the '
  'column records WHEN, never HOW OFTEN.';
