-- 075: the aged-ambiguous index for the sender's lost-answer resolution (07 §21; #1297).
-- Identical to the 07 §21 block; the advertised-DDL manifest pins the two together.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'ix_outbox_ambiguous_age')

-- #1297 (the one-edit tap): a live sender's lost answer is resolved after a backoff — the sender
-- sweep (every 3 s, fleet-wide) asks each binding for an `ambiguous` row older than it, and the
-- binding's sender lists its aged rows on every tick. Neither predicate had an index of its own:
-- `ix_outbox_due` is partial on `pending`, `ix_outbox_retire` is `(updated_at)` alone, so the sweep
-- became a full scan of an unbounded table. The partial index serves exactly both predicates.
CREATE INDEX ix_outbox_ambiguous_age ON channel_outbox (binding_id, updated_at) WHERE state = 'ambiguous';
