# The waitlist

The marketing waitlist is `waitlist_entries`, created by `100_waitlist_entries.sql` and written
by the API alone (`POST /public/waitlist`, `src/services/target/waitlist.py`); `07` §43 is its
advertised DDL.

Until 100 the landing site owned a Drizzle-managed `waitlist_signups` table through its own
`DATABASE_URL`, and this note said no Python migration might touch it. That table never existed
in production; a hand-made, empty stopgap of it (and the NOLOGIN `waitlist_writer` role) was
created there on 2026-10-02 and is dropped by hand by the owner. No migration names it.
