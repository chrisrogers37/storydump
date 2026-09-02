-- Migration 067: the browser-binding nonce covers connect and reconnect, not
-- sign-in alone (`07` §2).
--
-- THE DEFECT THIS CLOSES. `060` wrote the nonce rule as a biconditional --
-- `(purpose = 'signin') = (cookie_nonce_hash IS NOT NULL)` -- which does not
-- merely permit an unbound connect state, it REQUIRES one: a connect row
-- carrying a nonce was rejected by the database. So the schema and the code
-- agreed, and both were wrong in the same direction. `consume_state` gated its
-- nonce check on `purpose == "signin"` and this constraint guaranteed there
-- would never be anything to check for the other purposes.
--
-- What that bought an attacker: `connect`/`reconnect` complete by writing a
-- publish-capable credential into the workspace the state names, and nothing
-- verified WHO completed the callback. `user_id` and `workspace_id` are
-- recorded at issue, but they attribute the write afterwards -- they do not
-- gate it. A legitimate admin of their own workspace could mint a real state,
-- send the genuine provider link to someone else, and receive that person's
-- credential. Every step looks legitimate to the recipient because every step
-- IS legitimate except the binding nobody checked.
--
-- THE CONSTRAINT IS RENAMED, and that is not tidying. `ck_oauth_state_signin_
-- nonce` states the defect in its name: it says the nonce is a sign-in
-- concern. Leaving the name while widening the predicate would leave the next
-- reader with a constraint whose name argues against its body, which is how
-- the rule got scoped this way in the first place.
--
-- `link` IS DELIBERATELY EXCLUDED. It completes as a Telegram `/start` tap
-- rather than a browser redirect, so there is no cookie jar to bind to and
-- requiring a nonce would refuse every legitimate link. It is bound instead by
-- the tapping Telegram identity against the row's pinned user (D35). Adding it
-- here would not harden anything; it would break the flow.
--
-- IN-FLIGHT UNBOUND ROWS ARE DELETED, NOT MIGRATED, and there is no way to
-- keep them. A live `connect` row with a NULL nonce is precisely the
-- exploitable object this migration exists to make impossible, and it cannot
-- be given a nonce retroactively -- the browser that would have to hold the
-- matching cookie never received one. Consuming them rather than deleting
-- would not satisfy the new constraint either, which applies to every row
-- regardless of `consumed_at`. The cost is bounded and small: these rows carry
-- a 900s TTL, so at most fifteen minutes of in-flight connect attempts are
-- affected, and the remedy for a user who hits it is to press connect again.
-- Leaving even one live is strictly worse.
--
-- Adoption evidence (`runner adopt`). TWO postconditions, because the rename is
-- half the change: the first proves the widened constraint landed, the second
-- proves the old one is GONE rather than sitting alongside it -- a state in
-- which the biconditional would still forbid a bound connect row and the flow
-- would be broken in the opposite direction.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_oauth_state_binding_nonce')
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_oauth_state_signin_nonce')

-- The unbound in-flight states. Scoped to the two purposes being widened:
-- `signin` rows already carry a nonce (the old biconditional required it) and
-- `link` rows are outside the new rule, so neither is touched.
DELETE FROM oauth_states
 WHERE purpose IN ('connect', 'reconnect')
   AND cookie_nonce_hash IS NULL;

ALTER TABLE oauth_states
  DROP CONSTRAINT ck_oauth_state_signin_nonce;

ALTER TABLE oauth_states
  ADD CONSTRAINT ck_oauth_state_binding_nonce CHECK (
    (purpose IN ('signin', 'connect', 'reconnect'))
      = (cookie_nonce_hash IS NOT NULL));
