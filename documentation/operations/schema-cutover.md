# Schema cutover

Production was not converted in place. On 2026-10-17 it moved to a new database
built from schema code, and on 2026-10-19 the owner deleted the original
database. [The cutover as performed](#the-cutover-as-performed-2026-10-17)
records where production lives, what data moved and what was deleted.

## The cutover as performed (2026-10-17)

**Where production lives.**

- Database: Neon project `storydump` (named `storydump-v2` until
  2026-10-21), database
  `storydump_v2`, PostgreSQL 17. It was built empty from schema code on
  2026-10-16. Adoption recorded `000`, `001` and `002`, each confirmed by its
  probe, so the deployment pre-step applies nothing and owes nothing.
- Railway: the `storydump` service (the API, `api.storydump.app`) and the
  `worker` service deploy `chrisrogers37/storydump` `main`. The repository was
  named `storydump-v2` until 2026-10-21.
  `TARGET_DATABASE_URL` connects the API as `svc_ingress` and the worker as
  `svc_worker`. `DATABASE_URL` is the database owner, used only by the
  pre-deploy runner.
- Vercel: `storydump.app` builds `landing/` from the same repository's `main`.
- The switch: one Railway environment change repointed both services, which
  deployed `d25d10d` between 14:17 and 14:22 UTC. The original deployments were
  removed, and by 14:30 UTC the original database had no application sessions.

Schema code builds the
current schema directly and each adoption probe confirms it, so there is no
ledger to convert and no owner migration to apply. On 2026-10-16 a read-only
parity check ran on a temporary Neon branch of the original database, and a
local rehearsal ran on a dump of that branch. The rehearsal stopped while
restoring object ownership; neither was repeated.

**What data moved.** None. By the owner's decision production started empty:
no users, workspaces, Instagram accounts, credentials, media, schedules or
posting history were imported. Every row in production was created there after
the switch, starting with the owner's admission, account and workspace. The
exception is the 32 legal-edge rows of `post_intent_transitions` that every
schema build writes. At the 2026-10-16 rehearsal snapshot the original database
held 2 users, 2 workspaces, 2 Instagram accounts, 4,554 media items, 759 post
intents and 531 post locks, plus its 16 `archive.*_pre_cutover_20260917`
tables. None of it came across, and all of it was deleted with the original
database.

The original system never had a `PAST_POSTS_HMAC_KEYS` ring, and neither
database held a tombstone or deletion receipt keyed by one, so production's
ring started new. It was set on the API and the worker on 2026-10-19 under its
earlier name, `ARCHIVE_HMAC_KEYS`, and its value carried over to
`PAST_POSTS_HMAC_KEYS` when the feature was renamed Instagram past posts. Keep
every version it has held; see
[the key ring](instagram-past-posts-review.md#5-the-key-ring).

**The original database, deleted.** Neon project `storyline-ai-db`, database
`neondb`, was the rollback copy from the
switch until the owner deleted the project, with its branches, on 2026-10-19.
Nothing wrote to it after the switch. The full dump and the archive-snapshot
dump taken from its temporary branch on 2026-10-16 were deleted the same day.

Kept with the cutover record, outside Git: the Railway and Vercel settings from
before the switch, the password-free roles and source catalog taken on
2026-10-16, which hold no user data, and a bundle of every branch of the
original repository.

**No rollback to the original system.** It ended with the original database.
The owner deleted it once a rollback was no longer wanted and nothing more was
to come across.

The privacy policy said, in two places, that a one-time copy of the previous
system's data, taken on September 17, 2026, was kept and that deletion requests
were applied to it by hand. That copy was the original database's archive
snapshots and the dumps that contained them. Deleting them ended it, and the
policy no longer mentions it.
