-- Run after manual 001 commits, in a separate owner transaction.
-- Validation scans rows without holding 001's ACCESS EXCLUSIVE lock.
ALTER TABLE public.post_intents VALIDATE CONSTRAINT ck_intent_via;
ALTER TABLE public.post_intents VALIDATE CONSTRAINT ck_posted_complete;
