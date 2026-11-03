-- rate_counters takes a sixth scope, ops_notice: the latch on a notice to the
-- deployment's operators (src/channels/telegram_ops_notice.py), claimed once
-- per key and window, so one kind of notice reaches them at most once a
-- window. The constraint is rebuilt with the list the model declares
-- (machinery.py, ck_rate_scope), in its order.
-- The runner wraps this file and its ledger row in one transaction.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_rate_scope' AND conrelid = to_regclass('public.rate_counters') AND pg_get_constraintdef(oid) LIKE '%ops_notice%')

SET LOCAL search_path = public, pg_temp;

ALTER TABLE rate_counters DROP CONSTRAINT ck_rate_scope;
ALTER TABLE rate_counters ADD CONSTRAINT ck_rate_scope CHECK (
  scope IN ('tg_chat','tg_global','ws_admission','preauth_ip','email_global','ops_notice')
);
