-- Migration 129: admitting people from the waitlist (#1723; the owner's rulings of 2026-10-12, F1
-- and F5 to F9). Appended to the advertised stream as `07` §72.
--
-- ADMITTING SOMEONE WAS A HAND-WRITTEN INSERT. The owner let an address in with one INSERT INTO
-- signup_admissions as the database owner (092), and nothing at runtime wrote the table. These
-- four doors make admission a runtime act: the API lists the waitlist and admits an address, the
-- worker stores the claim link it has just emailed, and the sign-in callback spends that link.
--
-- THE CLAIM LINK. An admission's email carries a one-time link, and whoever opens it and signs in
-- with Google gets in, under any Google address (F1). Only the SHA-256 of the link's token is
-- stored, in claim_token_hash, and only after the provider accepted the email, together with
-- emailed_at: the worker sends first and stores second, as 115's re-send does. A failed send
-- therefore leaves the last link working, and the raw token never rests in jobs.payload.
-- uq_signup_admissions_claim is the claim's lookup, as uq_invite_token is an invitation's, and
-- claimed_at records the claim. A link never expires (F6): a later email replaces it, and it dies
-- once its spot is used.
--
-- ONE SPOT, ONE ACCOUNT (F9 (a)). The first account through an admission, by its link or as its
-- address, uses it up. "Held" below means an account's primary_email is the address, compared
-- with lower(). fn_signup_link and fn_signup_claim refuse a claimed or held admission, and
-- fn_signup_admit answers signed_up for one. fn_signup_admitted (092, 098) stops admitting a
-- claimed admission's address: its admissions arm gains `AND a.claimed_at IS NULL`, and nothing
-- else in it changes. CREATE OR REPLACE keeps its owner and its grant.
--
-- fn_waitlist(p_limit, p_ref) lists every waitlist entry, and every admission that has none
-- (F5 (b)), from waitlist_entries FULL JOIN signup_admissions. Newest first by when the entry
-- joined or, off the list, when it was admitted, and at most p_limit rows. Each row is the
-- address, when it joined (NULL off the list), when it was admitted and emailed, and whether it
-- signed up: a claimed admission, or a held address. With p_ref, it is only the row whose
-- address's SHA-256 hex starts with p_ref, the 32 characters a Telegram button carries.
--
-- fn_signup_admit(p_email) admits any address the table's CHECK holds, on the waitlist or not
-- (F5 (b)). A held address or a claimed admission answers signed_up, and nothing is written.
-- Otherwise it answers admitted, or admitted_before when the row was already there. The CHECK's
-- refusal reaches the caller, which translates it.
--
-- fn_signup_link(p_email, p_token_hash) stores a link's hash and emailed_at together, replacing an
-- earlier link, and answers false when the spot was taken in the meantime.
--
-- fn_signup_claim(p_token_hash) spends a link once: it sets claimed_at, clears the hash, and
-- answers whether it did.
--
-- svc_membership, the doors' owner, gains INSERT and UPDATE on signup_admissions, SELECT on
-- waitlist_entries, and users' primary_email (a column grant under 098's p_member_users), with a
-- policy for each new command. svc_ingress executes fn_waitlist, fn_signup_admit and
-- fn_signup_claim, and svc_worker executes fn_signup_link. Neither runtime role gains anything on
-- either table. No operational number is in a door body: the list's bound arrives as p_limit. The
-- CREATE bracket is 062's, as 115's is.
--
-- Adoption evidence (#997): the three columns and the index, the four doors owned by
-- svc_membership as SECURITY DEFINER, each door's EXECUTE grant, svc_membership's table and column
-- grants, the three policies by name, and the replaced door's new predicate: catalog state this
-- file creates.
--
-- runner:postcondition SELECT count(*) = 3 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'signup_admissions' AND column_name IN ('claim_token_hash', 'emailed_at', 'claimed_at')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'signup_admissions' AND indexname = 'uq_signup_admissions_claim')
-- runner:postcondition SELECT count(DISTINCT p.proname) = 4 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname IN ('fn_waitlist', 'fn_signup_admit', 'fn_signup_link', 'fn_signup_claim') AND p.prosecdef AND r.rolname = 'svc_membership'
-- runner:postcondition SELECT count(DISTINCT p.proname) = 3 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND p.proname IN ('fn_waitlist', 'fn_signup_admit', 'fn_signup_claim') AND r.rolname = 'svc_ingress' AND a.privilege_type = 'EXECUTE'
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_signup_link' AND r.rolname = 'svc_worker' AND a.privilege_type = 'EXECUTE')
-- runner:postcondition SELECT has_table_privilege('svc_membership', 'signup_admissions', 'INSERT') AND has_table_privilege('svc_membership', 'signup_admissions', 'UPDATE')
-- runner:postcondition SELECT has_table_privilege('svc_membership', 'waitlist_entries', 'SELECT')
-- runner:postcondition SELECT has_column_privilege('svc_membership', 'users', 'primary_email', 'SELECT')
-- runner:postcondition SELECT count(*) = 3 FROM pg_policies WHERE schemaname = 'public' AND (tablename, policyname) IN (('signup_admissions', 'p_member_admissions_ins'), ('signup_admissions', 'p_member_admissions_upd'), ('waitlist_entries', 'p_member_waitlist'))
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_signup_admitted' AND position('claimed_at' IN p.prosrc) > 0)

-- [§72 admitting from the waitlist: the claim link's columns, the list, the admission, the link stored after its email went, and the claim]
-- Four definer doors owned by svc_membership: fn_waitlist, fn_signup_admit and fn_signup_claim
-- for svc_ingress, fn_signup_link for svc_worker. "Held" means an account's primary_email is the
-- address. fn_signup_admitted stops counting a claimed admission. The runtime roles gain nothing
-- on signup_admissions or waitlist_entries.

ALTER TABLE signup_admissions ADD COLUMN claim_token_hash text;

ALTER TABLE signup_admissions ADD COLUMN emailed_at timestamptz;

ALTER TABLE signup_admissions ADD COLUMN claimed_at timestamptz;

CREATE UNIQUE INDEX uq_signup_admissions_claim ON signup_admissions (claim_token_hash)
  WHERE claim_token_hash IS NOT NULL;

GRANT INSERT, UPDATE ON signup_admissions TO svc_membership;

CREATE POLICY p_member_admissions_ins ON signup_admissions FOR INSERT TO svc_membership
  WITH CHECK (true);

CREATE POLICY p_member_admissions_upd ON signup_admissions FOR UPDATE TO svc_membership
  USING (true) WITH CHECK (true);

GRANT SELECT ON waitlist_entries TO svc_membership;

CREATE POLICY p_member_waitlist ON waitlist_entries FOR SELECT TO svc_membership USING (true);

GRANT SELECT (primary_email) ON users TO svc_membership;

GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION fn_waitlist(p_limit integer, p_ref text DEFAULT NULL)
RETURNS TABLE (o_email text, o_joined_at timestamptz, o_admitted_at timestamptz,
               o_emailed_at timestamptz, o_signed_up boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT email, w.joined_at, a.admitted_at, a.emailed_at,
         a.claimed_at IS NOT NULL
           OR EXISTS (SELECT 1 FROM users u WHERE lower(u.primary_email) = email)
    FROM waitlist_entries w FULL JOIN signup_admissions a USING (email)
   WHERE p_ref IS NULL
      OR left(encode(sha256(convert_to(email, 'UTF8')), 'hex'), 32) = p_ref
   ORDER BY COALESCE(w.joined_at, a.admitted_at) DESC, email
   LIMIT GREATEST(p_limit, 0)
$$;

COMMENT ON FUNCTION fn_waitlist(integer, text) IS
  'The owner''s waitlist (129): every waitlist entry, and every admission that has none, newest '
  'first by when it joined or, off the list, when it was admitted, at most p_limit rows. Each row '
  'is the address, when it joined (NULL off the list), when it was admitted and emailed, and '
  'whether it signed up (a claimed admission, or an account holding the address). With p_ref, '
  'only the row whose address''s SHA-256 hex starts with p_ref''s 32 characters. Never a claim '
  'token''s hash. SECURITY DEFINER owned by svc_membership with EXECUTE granted to svc_ingress.';

ALTER FUNCTION fn_waitlist(integer, text) OWNER TO svc_membership;

CREATE FUNCTION fn_signup_admit(p_email text)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM users u WHERE lower(u.primary_email) = lower(p_email))
     OR EXISTS (SELECT 1 FROM signup_admissions a
                 WHERE a.email = lower(p_email) AND a.claimed_at IS NOT NULL) THEN
    RETURN 'signed_up';
  END IF;
  INSERT INTO signup_admissions (email) VALUES (lower(p_email)) ON CONFLICT (email) DO NOTHING;
  IF FOUND THEN
    RETURN 'admitted';
  END IF;
  RETURN 'admitted_before';
END $$;

COMMENT ON FUNCTION fn_signup_admit(text) IS
  'Admit an address (129), on the waitlist or not: signed_up when an account holds it or its '
  'admission was claimed, writing nothing; otherwise admitted, or admitted_before when it was '
  'already admitted. The table''s CHECK is the authority on what an address is, and its refusal '
  'reaches the caller. SECURITY DEFINER owned by svc_membership with EXECUTE granted to '
  'svc_ingress.';

ALTER FUNCTION fn_signup_admit(text) OWNER TO svc_membership;

CREATE FUNCTION fn_signup_link(p_email text, p_token_hash text)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH linked AS (
    UPDATE signup_admissions a SET claim_token_hash = p_token_hash, emailed_at = now()
     WHERE a.email = lower(p_email) AND a.claimed_at IS NULL
       AND NOT EXISTS (SELECT 1 FROM users u WHERE lower(u.primary_email) = a.email)
    RETURNING a.email
  )
  SELECT EXISTS (SELECT 1 FROM linked)
$$;

COMMENT ON FUNCTION fn_signup_link(text, text) IS
  'Store the claim link an admission email carried (129): its token''s hash and emailed_at '
  'together, replacing an earlier link, on an admission not yet claimed whose address no account '
  'holds; true when it did. The worker calls it only after the provider accepted the email, so a '
  'failed send leaves the last link working. SECURITY DEFINER owned by svc_membership with '
  'EXECUTE granted to svc_worker.';

ALTER FUNCTION fn_signup_link(text, text) OWNER TO svc_membership;

CREATE FUNCTION fn_signup_claim(p_token_hash text)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH claimed AS (
    UPDATE signup_admissions a SET claimed_at = now(), claim_token_hash = NULL
     WHERE a.claim_token_hash = p_token_hash AND a.claimed_at IS NULL
       AND NOT EXISTS (SELECT 1 FROM users u WHERE lower(u.primary_email) = a.email)
    RETURNING a.email
  )
  SELECT EXISTS (SELECT 1 FROM claimed)
$$;

COMMENT ON FUNCTION fn_signup_claim(text) IS
  'Spend a claim link (129): sets claimed_at and clears the hash on the admission whose link it '
  'is, while it is unclaimed and no account holds its address; true when it did. Single use, '
  'and dead once the address has an account: one spot, one account. SECURITY DEFINER owned by '
  'svc_membership with EXECUTE granted to svc_ingress.';

ALTER FUNCTION fn_signup_claim(text) OWNER TO svc_membership;

CREATE OR REPLACE FUNCTION fn_signup_admitted(p_email text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM signup_admissions a WHERE a.email = lower(p_email) AND a.claimed_at IS NULL)
      OR EXISTS (SELECT 1 FROM workspace_invitations i
                   JOIN workspaces w ON w.id = i.workspace_id AND w.state = 'active'
                   JOIN workspace_members m ON m.workspace_id = i.workspace_id
                                           AND m.user_id = i.invited_by_user_id
                                           AND m.role IN ('owner', 'admin')
                   JOIN users u ON u.id = i.invited_by_user_id AND u.state = 'active'
                  WHERE lower(i.email) = lower(p_email)
                    AND i.state = 'pending' AND i.expires_at > now())
$$;

COMMENT ON FUNCTION fn_signup_admitted(text) IS
  'May a new Google account with this verified email create its user (092)? True when the owner '
  'admitted the address (signup_admissions) and its admission was not claimed through its link '
  '(129), or a pending, unexpired invitation is addressed to it from an active workspace by '
  'someone still its owner or an admin, with an active account (098); false for NULL. Compares '
  'lower() on both sides. Answers one boolean, never which workspace invited the address. '
  'SECURITY DEFINER owned by svc_membership with EXECUTE granted to svc_ingress.';

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION fn_waitlist(integer, text) FROM PUBLIC;

REVOKE ALL ON FUNCTION fn_signup_admit(text) FROM PUBLIC;

REVOKE ALL ON FUNCTION fn_signup_link(text, text) FROM PUBLIC;

REVOKE ALL ON FUNCTION fn_signup_claim(text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_waitlist(integer, text) TO svc_ingress;

GRANT EXECUTE ON FUNCTION fn_signup_admit(text) TO svc_ingress;

GRANT EXECUTE ON FUNCTION fn_signup_link(text, text) TO svc_worker;

GRANT EXECUTE ON FUNCTION fn_signup_claim(text) TO svc_ingress;
