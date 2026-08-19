-- Migration: 061_intent_self_transition_guard.sql
-- Description: #883 — refuse a same-state write to post_intents.state, so the
--   LOSER of a concurrent transition is told it lost instead of being told it
--   won. Statements 257-258 — the first two past the 257 F.2 landed.
--
--   THE DEFECT, MEASURED RATHER THAN REASONED. Two writers take the same edge
--   on the same intent. The loser blocks on the row lock; when the winner
--   commits, the loser's UPDATE re-evaluates against the winner's committed
--   row, so `trg_intent_guard` sees OLD.state = NEW.state, skips its legality
--   check (it only compares under IS DISTINCT FROM) and writes a no-op. Both
--   callers were told they had transitioned. One had.
--
--   `rowcount` DOES NOT SEPARATE THEM: the loser got 1, identical to the
--   winner, so no service-side row check can recover the distinction. The
--   caller's only success signal is that the statement did not raise.
--
--   READ COMMITTED ONLY, and that is the level we run at. At REPEATABLE READ
--   and above the loser already gets a serialization error; L.0's engine sets
--   no isolation_level and the server default here is read committed, so
--   every transition runs at the one level where the loser is not told.
--   Raising the level would also close this, and is deliberately NOT done
--   here: it is a fleet-wide change to every transaction L.0 opens, with its
--   own retry semantics, and it must not ride in on a trigger fix.
--
--   WHY IT CANNOT LIVE IN trg_intent_guard. A BEFORE UPDATE FOR EACH ROW
--   trigger cannot tell "SET state to the same value" from "did not touch
--   state" — both present as NEW.state = OLD.state. That is precisely why the
--   existing guard is written with IS DISTINCT FROM, and it is why this is a
--   second trigger rather than an edit to the first. `UPDATE OF state` fires
--   on the column being NAMED IN THE SET LIST whatever its value, which is
--   the discriminator plpgsql does not otherwise expose.
--
--   CHECKPOINT UPDATES ARE UNTOUCHED, which is the whole reason for the
--   `UPDATE OF` form: `UPDATE post_intents SET ig_permalink = ...` does not
--   name state, so this trigger never fires on it. Verified in the
--   postconditions below and by the gate suite's non-state-update cases.
--
--   FIRING ORDER IS LOAD-BEARING AND IS NOT AN ACCIDENT OF NAMING. Postgres
--   fires BEFORE row triggers in name order, and tg_intent_guard sorts before
--   tg_intent_no_self_transition, so a terminal row still reports "is
--   terminal" rather than this rule. Renaming either trigger reorders them.
--
--   ERRCODE IS check_violation DELIBERATELY, not a new SQLSTATE.
--   `intent_ledger.transition()` translates exactly RaiseError (P0001) and
--   CheckViolationError (23514) into IntentTransitionRefused; a custom code
--   would escape as a raw DBAPIError, which is a behaviour regression rather
--   than a finer distinction. The message carries the distinction instead. If
--   a retry path ever needs to BRANCH on "already in the target state" versus
--   "illegal edge", that wants a typed error in the service and a distinct
--   code here, together — not a code change alone.
--
--   NOT AN F.2 INCREMENT. 060 completed F.2 and made the target lineage EQUAL
--   the advertised stream. This is the first migration past that, and it
--   extends the stream by one statement pair rather than consuming a
--   remaining prefix. Arm (b) still holds — the lineage is an ordered prefix
--   of the stream — which is why the block below is appended at the END of
--   `07-security-model.md`: the last doc in stream order is the only place a
--   new statement can land and keep the lineage positionally aligned. The
--   placement is dictated by that ordering rule, not by a claim that this is
--   a security object; `02` §5 carries a pointer to it.
--
--   ABOVE THE LINEAGE BOUNDARY. 051 renames the legacy schema out of public;
--   this object is created into the empty public it leaves behind. The
--   running application does not see it until the M.3 cutover.
--
--   THE POSTCONDITIONS BELOW ARE EXECUTED, not decorative — proven rather
--   than assumed. `tests/scripts/test_lineage_lane.py` replays the whole
--   target lineage THROUGH THE RUNNER, which evaluates them; breaking the
--   first one deliberately reddens 8 tests in that file. The replay fixture
--   most suites use (`replay_advertised_stream`) drives the doc stream and
--   does NOT evaluate them, so the lane is the path that gives them teeth.
--
-- Rollback: DROP TRIGGER IF EXISTS tg_intent_no_self_transition ON post_intents;
--   DROP FUNCTION IF EXISTS trg_intent_no_self_transition();
-- Depends on: 055 (post_intents and trg_intent_guard).
-- Created: 2026-08-19
-- Issue: #883

CREATE FUNCTION trg_intent_no_self_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'same-state write to post_intent % (state %) — a transition that did not happen', OLD.id, OLD.state
    USING ERRCODE = 'check_violation';
END $$;

CREATE TRIGGER tg_intent_no_self_transition BEFORE UPDATE OF state ON post_intents
  FOR EACH ROW WHEN (NEW.state IS NOT DISTINCT FROM OLD.state)
  EXECUTE FUNCTION trg_intent_no_self_transition();

-- runner:postcondition SELECT count(*) = 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND NOT t.tgisinternal AND t.tgname = 'tg_intent_no_self_transition'
-- tgtype 19 = ROW(1) + BEFORE(2) + UPDATE(16), measured not assumed
-- runner:postcondition SELECT t.tgtype = 19 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid WHERE c.relname = 'post_intents' AND t.tgname = 'tg_intent_no_self_transition'
-- runner:postcondition SELECT count(*) = 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY (t.tgattr) WHERE c.relname = 'post_intents' AND t.tgname = 'tg_intent_no_self_transition' AND a.attname = 'state'
-- runner:postcondition SELECT (SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid WHERE c.relname = 'post_intents' AND NOT t.tgisinternal AND t.tgtype & 2 = 2 AND t.tgtype & 16 = 16 ORDER BY t.tgname LIMIT 1) = 'tg_intent_guard'
