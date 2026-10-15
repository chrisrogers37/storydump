-- Migration 133: an admission is used up by the account created at its address, durably (#1740,
-- the 2026-10-13 audit's A1). Appended to the advertised stream as `07` §73.
--
-- THE DEFECT. 129 made one spot open one account (F9 (a)): the first account through an
-- admission, by its link or as its address, uses it up. Only the link wrote that down. A direct
-- sign-up at an admitted address left claimed_at NULL, and the doors read the spot as used only
-- while an account held the address. A returning Google subject's verified email is refreshed at
-- every sign-in (#1579), so the person who signed up at the address could move off it, and the
-- admission opened again: its old link claimed a second account, the address admitted another
-- new account directly, fn_signup_admit answered admitted_before (an explicit resend emailed a new
-- link) and fn_signup_link stored that link.
--
-- THE FIX. tg_users_consume_admission, AFTER INSERT ON users FOR EACH ROW WHEN the new row has an
-- address, runs trg_users_consume_admission(): on the unconsumed admission whose address is
-- lower(NEW.primary_email), it stamps claimed_at and clears claim_token_hash, in the transaction
-- that creates the user. The doors already read claimed_at, so their signatures and predicates do
-- not change. claimed_at now means consumed: by its link, or by an account created at its address.
-- - Birth only. An existing account whose address changes to an admitted one consumes nothing:
--   that account did not come through the admission.
-- - No admission, no change. An invitation, open sign-up, an admission already consumed and a
--   Telegram-only user (no address, so the WHEN skips it) insert exactly as before.
-- - An error is never swallowed: a failure in the UPDATE fails the user's creation with it.
--
-- THE AUTHORITY. The function is SECURITY DEFINER, owned by svc_membership, which already reads
-- and updates signup_admissions (092's SELECT and p_member_admissions, 129's UPDATE and
-- p_member_admissions_upd): no grant and no policy is added, and the runtime roles still hold
-- nothing on the table. PostgreSQL checks EXECUTE on a trigger's function when the trigger is
-- created, never when an INSERT fires it, so svc_ingress and svc_worker create users without
-- holding it. PUBLIC's EXECUTE is revoked and no role is granted it: this is no callable door.
-- The trigger is created while this file's actor still owns the function (CREATE TRIGGER needs
-- EXECUTE on it), and then the function passes to svc_membership inside this file's own CREATE
-- bracket, of 129's shape: CREATE on public granted to svc_membership, then revoked.
--
-- THE LOCK. LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE is the first statement. It is the mode
-- CREATE TRIGGER takes anyway, and it blocks every INSERT, UPDATE and DELETE on users until this
-- file commits. The runner retries a lock-wait failure (55P03) by re-running the whole file, so
-- the lock comes before any work. A deadlock (40P01) is not retried: the apply fails whole, as any
-- failure does, and a later deploy runs it again.
--
-- THE BACKFILL, once. Every unconsumed admission whose address an account holds now, compared
-- with lower() as the doors compare it, is stamped consumed: a disabled account counts, since it
-- holds its spot under the doors too. So does an account that moved to the address rather than
-- being created at it: nothing records which it was, and stamping keeps a spot used where the
-- other reading would reopen one. claimed_at is this file's now(), because when the account
-- was created is not recorded; an admission consumed earlier keeps its claimed_at, and admitted_at,
-- emailed_at and note are left alone. An admission used directly and vacated before this file
-- has no record of the account that used it, so it cannot be told from an unused one and stays
-- open: that history is not repaired here (#1740).
--
-- THE OVERLAP LEFT. A direct sign-up asks fn_signup_admitted before it inserts its user, and a
-- read is not a lock. When a claim of the same admission commits between that read and the
-- insert, both accounts are created: the trigger's UPDATE waits for the claim, then finds the
-- admission consumed and does nothing. When the direct sign-up's insert comes first, the claim
-- waits on the row and is refused. Closing the first order needs serialising the two sign-ins,
-- which this file does not do.
--
-- DEPLOY ORDER: either. No code changes with this file: the user inserts made today fire the
-- trigger as they are.
--
-- Adoption evidence (#997): the function by name with no arguments, returning trigger, owned by
-- svc_membership as SECURITY DEFINER on the pinned path, its body naming what it writes, and the
-- trigger on users, enabled, row-level AFTER INSERT only (tgtype 5: the row bit 1 and the INSERT
-- bit 4, with no BEFORE or INSTEAD OF bit), bound to the function, under its WHEN: catalog state
-- this file creates. Each probe reads the catalogs by name, so it reads false, without raising,
-- on a database this file has not reached. EXECUTE held by no role but the owner, the closed
-- CREATE bracket and the backfill are absences and data, which the gates hold instead.
--
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname = 'trg_users_consume_admission' AND p.pronargs = 0 AND p.prorettype = 'trigger'::regtype AND p.prosecdef AND r.rolname = 'svc_membership' AND 'search_path=pg_catalog, public, pg_temp' = ANY (p.proconfig))
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'trg_users_consume_admission' AND position('signup_admissions' IN p.prosrc) > 0 AND position('claimed_at' IN p.prosrc) > 0 AND position('claim_token_hash' IN p.prosrc) > 0)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_trigger g JOIN pg_class t ON t.oid = g.tgrelid JOIN pg_namespace n ON n.oid = t.relnamespace JOIN pg_proc p ON p.oid = g.tgfoid WHERE n.nspname = 'public' AND t.relname = 'users' AND g.tgname = 'tg_users_consume_admission' AND NOT g.tgisinternal AND g.tgenabled = 'O' AND g.tgtype = 5 AND p.proname = 'trg_users_consume_admission' AND g.tgqual IS NOT NULL AND position('primary_email IS NOT NULL' IN pg_get_triggerdef(g.oid)) > 0)

-- [§73 an admission is used up by the account created at its address: the trigger, and the held admissions stamped once]
-- tg_users_consume_admission stamps claimed_at and clears the link's hash on the unconsumed
-- admission at a new user's address, in the user's own transaction, through a SECURITY DEFINER
-- function owned by svc_membership, with EXECUTE granted to no role. The lock comes first; the
-- backfill stamps the admissions an account holds now.

LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE;

GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION trg_users_consume_admission()
RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  UPDATE signup_admissions
     SET claimed_at = now(), claim_token_hash = NULL
   WHERE email = lower(NEW.primary_email) AND claimed_at IS NULL;
  RETURN NEW;
END $$;

COMMENT ON FUNCTION trg_users_consume_admission() IS
  'tg_users_consume_admission (133, #1740): when a user is created with an address, the '
  'unconsumed admission at that address, compared with lower(), is used up in the same '
  'transaction: claimed_at is stamped and its claim link''s hash cleared. One spot, one account, '
  'whether the account came through the link or as the address. Birth only: an address changed '
  'later consumes nothing. No admission, no change; an error fails the user''s creation. '
  'SECURITY DEFINER owned by svc_membership, under its grants and policies on '
  'signup_admissions. EXECUTE is granted to no role: PostgreSQL asks for it when a trigger is '
  'created, never when an INSERT fires one.';

CREATE TRIGGER tg_users_consume_admission AFTER INSERT ON users
  FOR EACH ROW WHEN (NEW.primary_email IS NOT NULL)
  EXECUTE FUNCTION trg_users_consume_admission();

ALTER FUNCTION trg_users_consume_admission() OWNER TO svc_membership;

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION trg_users_consume_admission() FROM PUBLIC;

-- The backfill comes last, after the trigger. Either order is the same: the lock taken first
-- holds every write to users off until this file commits, so the accounts read here are the
-- accounts at commit, and every account created after the commit fires the trigger.
UPDATE signup_admissions a
   SET claimed_at = now(), claim_token_hash = NULL
 WHERE a.claimed_at IS NULL
   AND EXISTS (SELECT 1 FROM users u WHERE lower(u.primary_email) = a.email);
