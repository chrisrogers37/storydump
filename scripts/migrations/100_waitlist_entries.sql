-- Migration 100: the marketing waitlist moves into the ledger (owner decision, 2026-10-03: "one
-- system, one writer"). Appended to the advertised stream as `07` §43.
--
-- THE SITE WROTE TO THE DATABASE ITSELF. The landing site's POST /api/waitlist inserted into a
-- Drizzle-managed `waitlist_signups` table through its own DATABASE_URL, the one write in the
-- system that did not go through the API. Production never got that table (the form failed every
-- signup), and the only credential that could have served it reached every table. The API now
-- owns the write (`POST /public/waitlist`, `waitlist.join`), and the site holds no database
-- credential at all.
--
-- A NEW NAME, NOT AN ADOPTION. A hand-made, empty `waitlist_signups` (and an insert-only
-- `waitlist_writer` login) was created in production on 2026-10-02 as a stopgap and never
-- served a signup. This file leaves both alone, so its CREATE cannot meet an existing table at
-- the predeploy; the owner drops them by hand once the API serves the form.
--
-- waitlist_entries is global, not tenant-plane: a visitor joining the waitlist has no user and
-- no workspace. RLS is on and the one policy is svc_ingress's INSERT, the route's only statement:
-- the API can add an address and cannot read, change or remove one, so the public endpoint is no
-- oracle for who is on the list. The owner reads the list as the database owner, which
-- owner-bypass lets through, and admits people with `signup_admissions` (§35). The CHECK is
-- §35's address rule plus RFC 5321's 254-octet bound; `utm` is the campaign the visitor came
-- from, an object bounded at 2 KB.
--
-- Adoption evidence (#997): the table with RLS enabled, svc_ingress's INSERT and its policy —
-- catalog state this file creates.
--
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'waitlist_entries' AND c.relrowsecurity)
-- runner:postcondition SELECT has_table_privilege('svc_ingress', 'waitlist_entries', 'INSERT')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'waitlist_entries' AND policyname = 'p_ingress_waitlist')

CREATE TABLE waitlist_entries (
  email     text PRIMARY KEY CONSTRAINT ck_waitlist_entries_email CHECK (
              email = lower(email) AND length(email) <= 254
              AND email ~ '^[^[:space:]@]+@[^[:space:]@]+$'
              AND email !~ '[\u0080-\u00a0\u00ad\u180e\u2000-\u200f\u2028-\u202f\u205f-\u2064\u3000\ufeff]'),
  joined_at timestamptz NOT NULL DEFAULT now(),
  utm       jsonb CONSTRAINT ck_waitlist_entries_utm CHECK (
              utm IS NULL OR (jsonb_typeof(utm) = 'object' AND length(utm::text) <= 2048))
);

ALTER TABLE waitlist_entries ENABLE ROW LEVEL SECURITY;

GRANT INSERT ON waitlist_entries TO svc_ingress;

CREATE POLICY p_ingress_waitlist ON waitlist_entries FOR INSERT TO svc_ingress WITH CHECK (true);
