-- Migration 107: the outcomes indexed by slot, for the reads that ask for them newest first or a
-- day at a time (#1640, #1634; `07` §50). Statements appended to the advertised stream.
--
-- WHY. The calendar's month (`GET …/intents/days?state=posted`), the Overview's recent activity
-- (`?state=posted,skipped,rejected&order=desc`) and the history tab read a workspace's outcomes by
-- schedule_slot_at. No index served `workspace_id = $1 AND state IN (…)` in slot order:
-- uq_intent_slot leads with the account and is cadence-only, ix_intents_reap_slot holds scheduled
-- and prompt_pending rows only, uq_intent_live_subject holds no terminal state, and the primary key
-- is the id. post_intents is kept forever (055), so each of these reads walked a workspace's whole
-- history, and its cost grew with every story it posted.
--
-- THE PREDICATE IS THE HISTORY'S THREE OUTCOMES, NOT POSTED ALONE (#1640's open question). The
-- reads that ship ask for posted (the calendar's month) and for posted, skipped and rejected (the
-- Overview's recent activity and the history tab: HISTORY_STATES). A posted-only index would serve
-- the first and neither of the others. The two outcomes it adds are a small share of the rows, and
-- a read naming 'posted' alone still uses it: the planner proves state = 'posted' implies the list.
--
-- (workspace_id, schedule_slot_at): the tenant first, as every read names it, then the slot, so a
-- month or a day is one range of the index and newest first is a backward walk of it.
--
-- A plain CREATE INDEX inside the runner's transaction, as 089 created uq_intent_slot: the runner
-- bounds the lock wait and retries it.

-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'post_intents' AND indexname = 'ix_intents_history_slot' AND indexdef LIKE '%(workspace_id, schedule_slot_at)%' AND indexdef LIKE '%posted%' AND indexdef LIKE '%skipped%' AND indexdef LIKE '%rejected%')

-- [§50 the outcomes indexed by slot: the calendar's month and day and the Overview's recent activity read a workspace's posted, skipped and rejected stories by schedule_slot_at]
-- A partial index on post_intents (workspace_id, schedule_slot_at) holding the three outcomes the
-- history names, so the newest-first and day-at-a-time reads walk one range of it rather than the
-- workspace's whole history (#1640).
CREATE INDEX ix_intents_history_slot ON post_intents (workspace_id, schedule_slot_at)
  WHERE state IN ('posted','skipped','rejected');
