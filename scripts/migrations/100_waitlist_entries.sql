-- Migration 100: the marketing waitlist moves into the ledger (owner decision, 2026-10-03: "one
-- system, one writer"). Appended to the advertised stream as `07` §43; the history is
-- scripts/migrations/NOTE_waitlist_table.md.
--
-- The landing site wrote its waitlist to the database through a credential of its own; the API
-- now owns the write (`POST /public/waitlist`) and the site holds no database credential. A new
-- name, not an adoption: production's hand-made, empty `waitlist_signups` and its
-- NOLOGIN `waitlist_writer` role are left alone for the owner to drop by hand.
--
-- Global, not tenant-plane: a visitor joining the waitlist has no user and no workspace. RLS is on
-- and the one policy is svc_ingress's INSERT, so the API can add an address and cannot read,
-- change or remove one: the public endpoint is no oracle for who is on the list. The owner reads
-- it as the database owner. The CHECK is the authority on an address: §35's rule plus a dot in
-- the domain, no control characters, and at most 254 characters.
--
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'waitlist_entries' AND c.relrowsecurity)
-- runner:postcondition SELECT has_table_privilege('svc_ingress', 'waitlist_entries', 'INSERT')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'waitlist_entries' AND policyname = 'p_ingress_waitlist')

CREATE TABLE waitlist_entries (
  email     text PRIMARY KEY CONSTRAINT ck_waitlist_entries_email CHECK (
              email = lower(email) AND length(email) <= 254
              AND email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'
              AND email !~ '[[:cntrl:]]'
              AND email !~ '[\u0080-\u00a0\u00ad\u180e\u2000-\u200f\u2028-\u202f\u205f-\u2064\u3000\ufeff]'),
  joined_at timestamptz NOT NULL DEFAULT now(),
  utm       jsonb CONSTRAINT ck_waitlist_entries_utm CHECK (
              utm IS NULL OR (jsonb_typeof(utm) = 'object' AND length(utm::text) <= 2048))
);

ALTER TABLE waitlist_entries ENABLE ROW LEVEL SECURITY;

GRANT INSERT ON waitlist_entries TO svc_ingress;

CREATE POLICY p_ingress_waitlist ON waitlist_entries FOR INSERT TO svc_ingress WITH CHECK (true);
