-- Bound early archive cards can lose selected content before approval.
-- runner:postcondition SELECT count(*) = 3 FROM post_intent_transitions WHERE from_state IN ('scheduled','prompt_pending','awaiting_approval') AND to_state='review_required'
INSERT INTO post_intent_transitions (from_state,to_state) VALUES
 ('scheduled','review_required'),
 ('prompt_pending','review_required'),
 ('awaiting_approval','review_required')
ON CONFLICT DO NOTHING;
