-- 072: ix_outbox_intent — the outbox rows of one intent, for the tap's supersede (07 §18; phase 1 of the 2026-09-09 tap plan).
-- Identical to the 07 §18 block; the advertised-DDL manifest pins the two together.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'ix_outbox_intent')

-- The outbox rows of one intent, for the tap's supersede-everywhere (phase 1 of the 2026-09-09 tap
-- plan): every flip retires the intent's cards in every binding with `WHERE … intent_id = :i`, and
-- the settled-card sweep joins live cards to their intents. Without this index each was a sequential
-- scan of the outbox on the tap's own transaction. Partial: notifications and acks carry no intent.
CREATE INDEX ix_outbox_intent ON channel_outbox (intent_id) WHERE intent_id IS NOT NULL;
