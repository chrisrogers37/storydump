-- The outbox sender sweep mints a sender for an ambiguous row once it has
-- waited p_age times its attempts, the schedule outbox.resolve_aged_ambiguous
-- resolves it on, so an edit resent until confirmed waits longer after each
-- lost answer and a row still waiting mints no sender. Same signature, owner
-- and grants.
-- The runner wraps this file and its ledger row in one transaction.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc WHERE oid = to_regprocedure('public.fn_sender_sweep(text,integer,numeric,numeric,integer)') AND prosrc LIKE '%p_age * GREATEST(o.attempts, 1)%')

SET LOCAL search_path = public, pg_temp;

CREATE OR REPLACE FUNCTION fn_sender_sweep(p_prefix text, p_attempts int, p_deadline numeric, p_age numeric, p_limit int)
RETURNS int
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH minted AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key,
                      run_at, max_attempts, deadline_at, payload)
    SELECT 'deliver_outbox', b.workspace_id, 'interactive',
           p_prefix || b.id, now(), p_attempts,
           now() + make_interval(secs => p_deadline),
           jsonb_build_object('v', 1, 'binding_id', b.id)
      FROM channel_bindings b
     WHERE b.state = 'active' AND b.channel LIKE 'telegram%'
       AND (EXISTS (SELECT 1 FROM channel_outbox o
                     WHERE o.binding_id = b.id AND o.state = 'pending')
            OR EXISTS (SELECT 1 FROM channel_outbox o
                        WHERE o.binding_id = b.id AND o.state = 'ambiguous'
                          AND o.updated_at <= now() - make_interval(secs => p_age)
                          AND o.updated_at <= now() - make_interval(
                                secs => p_age * GREATEST(o.attempts, 1))))
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.serialization_key = p_prefix || b.id
                          AND j.state IN ('ready', 'leased'))
     LIMIT p_limit
    RETURNING 1)
  SELECT count(*)::int FROM minted
$$;
