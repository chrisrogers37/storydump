# The Instagram source gate, run in isolation

The contributor recipe for `tests/scripts/test_instagram_source_gate.py`, the
full-composition gate of the Instagram source. It runs the real services over fake providers and a throwaway
PostgreSQL, from connecting an account to a posted story and to the deletion of
what the past-posts source imported. It is a test, not an operations procedure: nothing
here touches a deployment, and a green run is not evidence about Meta,
Instagram's CDN, Cloudinary or Telegram, because every one of them is a fake.

Three properties make it safe to run on a laptop or a build machine, and the
recipe below keeps them true:

- **Fake providers only.** Instagram's Graph and CDN, the transit store, the
  Telegram Bot API and the Meta publish stub are in-process fakes
  (`tests/fixtures/fake_providers.py`). No test connects to a live provider.
- **A database that holds nothing.** The fixtures create databases and the
  cluster's service roles, and drop them. Point the gate at a cluster you would
  not mind losing, which is what the next sections build.
- **No entrypoint.** The gate assembles the worker's composition and steps its
  loops itself. `src.main` and `src.worker.main` are never entered, no clock
  loop runs and nothing posts on its own; scene 9 pins that by reading the
  file's own syntax tree.

## What the gate drives

The past-posts source is armed the way a deployment arms it: the test hands each service an
environment holding a `PAST_POSTS_HMAC_KEYS` ring and that service's release, and
`past_posts_composition.build_worker` and `create_app(env=...)` read it. Nothing is read from the process environment and no
test hands a service a capability. The API runs as `svc_ingress` (sign-in, the
OAuth callback, the source-mix and source-policy routes, the command port, the
data-deletion callback). The worker runs as `svc_worker`: its clock ticks, its
loops claim and run one job at a time, and its prompt and sender sweepers make one
pass each. Each scene gets its own scratch database with the advertised migration
stream replayed under the service roles, because jobs are claimed from the whole
database and a shared one would let a scene run another's work.

The fakes sit at the seams the production objects are built over, so the real code
above them runs unchanged:

| Fake | Stands in for |
|---|---|
| `FakeInstagram` | Graph and the media CDN, one `httpx.MockTransport` serving the generated onboarding past-posts sources the way Meta pages them (by time window, newest first, a caption only to the read that asks for it, UTC offsets written `+0000`, the form Python 3.10's `datetime.fromisoformat` does not accept) |
| `FakeTransit` | the transit store: one asset registry behind the publish pipeline's upload and the deletion cleanup's typed lookup and destroy |
| `FakeTelegram` | the Bot API, recording every call, under the real `TelegramTransport` |
| `GateMeta` over `StubMetaAdapter` | the Meta publish calls, counted |
| `jpeg()` | a real, decodable image, so the real validator has bytes to judge |

Each scene indexes `onboarding.useful(10, ...)` trimmed to five posts: a recent
one, a credited one, one of unknown attribution (each an exclusion with its own
reason) and the past-posts source's single 2022 image, the one post that qualifies. What is
selected is therefore known in advance. Every token, id and URL is synthetic.

## What a run needs

- **Linux.** The selected-media validator refuses to run elsewhere
  (`media_validation.run_bounded` needs Linux resource limits). Off Linux, seven of
  the nine scenes fail by design, not skip, with the message
  ``the selected-media validator runs only on Linux (`media_validation.run_bounded`): run this gate on the Linux lane``.
  The two that pass on macOS are the OAuth interleaving scene (8) and the
  entry-point scan (9). A macOS result is neither a pass nor a defect.
- **Python 3.10**, as CI runs it, with the repository's requirements:
  `pip install -r requirements.txt && pip install -e '.[cli]'`.
- **PostgreSQL 15 in UTC, with password authentication over TCP, and a superuser
  test role** (the `POSTGRES_USER` of CI's `postgres:15` service is one). The
  fixtures create databases and the `svc_*` roles, which are cluster objects. A
  cluster in another time zone can fail the past-posts gates that store naive dates.
- **`psql` 15 on the `PATH`**, if you will run more than this gate. The gate's own
  fixtures bootstrap the roles and replay the migration stream through `psycopg2`,
  but the wider database-gated suites apply files through `psql`.
- **`ffprobe` and `ffmpeg` 7.1**, for the validator's own suites on the same
  machine. The video validator pins its evidence to 7.1 and answers another version
  with `tool_version_unproven`. This gate selects an image, which is decoded by
  Pillow in a resource-bounded subprocess, so none of its nine scenes calls them.
- **A checkout with no `.env`.** The guard refuses to start when the working
  directory or the rootdir holds one, because settings would load it on import.
- **One run at a time per cluster.** The suite takes a cluster-wide advisory lock;
  a second run waits (and says who holds the lock) rather than corrupting the first.

## Bootstrap a throwaway database

A disposable container is the shortest path. Bind it to loopback, on the
guard's port:

```bash
docker run -d --name storydump-gate-pg -p 127.0.0.1:65433:5432 \
  -e POSTGRES_USER=test_user -e POSTGRES_PASSWORD=test_password \
  -e POSTGRES_DB=storyline_test postgres:15 -c timezone=UTC
```

Or a scratch cluster of your own, in a directory you will delete: `initdb` with
`--username=test_user --auth=scram-sha-256` and a password file, started with
`-p 65433`, `listen_addresses=127.0.0.1` and `timezone=UTC`, then
`createdb storyline_test`.

The guard allows exactly one database port, **65433**, on a **loopback host**. If
your database listens somewhere else, put a loopback forwarder on 65433 in front of
it, or run the gate inside the database container's network namespace with
PostgreSQL on 65433. The guard checks what
the configuration names, not what is behind the port.

## Make a test-only key

```bash
export ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
```

A fresh key per session, never the deployed `ENCRYPTION_KEY` or `ENCRYPTION_KEYS`,
and never written to a file. Everything the gate encrypts dies with the database.

## Run it

```bash
env -i PATH="$PATH" PYTHONPATH="$PWD" PYTHON_DOTENV_DISABLED=1 \
  DB_HOST=localhost DB_PORT=65433 DB_USER=test_user DB_PASSWORD=test_password \
  DB_NAME=storyline_ai TEST_DB_NAME=storyline_test REQUIRE_TEST_DATABASE=1 \
  ENCRYPTION_KEY="$ENCRYPTION_KEY" \
  python -m pytest -p tests.isolation_guard --isolated-provider-gate \
    tests/scripts/test_instagram_source_gate.py --no-cov -q -p no:cacheprovider
```

- `env -i` starts from nothing, because the guard requires every variable named in
  `.env.example` (provider credentials included) to be unset, apart from the database,
  key and logging settings it checks by their own rules. Never load the production
  `.env` as a shortcut, and keep `PYTHON_DOTENV_DISABLED=1` so no dotenv file can
  change the configuration the guard judged.
- `REQUIRE_TEST_DATABASE=1` turns "no database answered" into a failure. Without
  it, an unreachable database skips the scenes and the run looks green.
- `-p tests.isolation_guard --isolated-provider-gate` arms the guard. The flag is
  on the command line because a dotenv file cannot unset it. Passing the flag
  without `-p` is pytest's own "unrecognized arguments".
- Add `-k <fragment>` to run one scene.

### What the guard refuses

It works in three layers, each in place before the next could matter.

1. **Before any conftest loads**: the plugin must come from this checkout, no `.env`
   may exist, and the deny guard is installed.
2. **After the `.env.test` override and before settings import**, the effective
   configuration is judged by variable name and never by value. `DB_HOST` must be
   `localhost`, `127.0.0.1` or `::1`; `DB_PORT` must be 65433; `DB_USER` must be
   `test_user`; `TEST_DB_NAME` must start with `storyline_test`; `DB_NAME` must be
   `storyline_ai` or start with `storyline_test`; `ENCRYPTION_KEY` must be a valid
   Fernet key; `TARGET_DATABASE_URL`, `DATABASE_URL`, `TEST_DATABASE_URL` and
   `SCHEMA_DRIFT_DSN` must be absent or loopback URLs on that port; `PGHOSTADDR`,
   `PGSERVICE` and `PGSERVICEFILE` must be unset. A refusal ends the run with
   pytest's usage-error exit code (4) and a list of `NAME: reason` lines, before a
   fixture connects.
3. **At run time**, a socket may reach only the disposable PostgreSQL (or a
   loopback fixture registered in-process), only `localhost` resolves, a `psycopg2`
   connection must match the disposable cluster, and a real `httpx` transport, the
   Cloudinary SDK's network modules and the worker's entrypoints are refused. Every
   refusal is recorded, and the session fails if any was, even when the code that met
   it swallowed the exception.

## What each scene proves

Statuses and values below are the ones the scenes assert. Ids are generated per run:
a source is one per account, an item is `media_items.id` (it survives a deletion and a
reconnect through its tombstone), and the card is the story's `post_intents.id`, which
the buttons carry as `v1:<verb>:<intent id>`.

### 1. A connected past-posts source runs from connect to a posted audit

`test_a_connected_past_posts_runs_from_connect_to_a_posted_audit`, the whole path:

1. **Armed by records.** The worker's capability is enabled, its evidence carries the
   record's run id, and `sync_media_source` is not parked.
2. **Source created On.** Connecting with History On gives outcome
   `connected_history_on`, one generation (`lifecycle = active`, `subject_id` the
   account's id), one verified mapping (`method = oauth_exchange`, `is_current`),
   and not one Graph request or job.
3. **Metadata-only walk.** `sync_now` runs to the start of history. Five parents are
   observed and represented; one is a candidate (the 2022 image, parsed from Graph's
   own time format); the exclusions are `too_recent`, `credited_excluded` and
   `attribution_unknown`; `eligible` and `verified_ready` are 0. **Zero CDN
   requests and zero validator calls**, every Graph request carries only that
   account's token, every index row is `validation_state = pending`, and no row holds
   a media URL.
4. **Plan.** The clock mints the slot and the story is `scheduled`, `origin = cadence`,
   bound to the 2022 post's item. Choosing it still reads nothing.
5. **Selected-media validation.** The prompt sweep fetches the bytes **once** (one
   CDN request) and the real validator judges them. The story is `awaiting_approval`,
   the item's index row reads `validation_state = supported`, and the story's
   provenance records `capture_epoch = 1`.
6. **Card.** `sendPhoto` carries the picture, a caption whose first line is the camera
   emoji and `@example_handle_a`, and the buttons `post`, `posted`, `skip`, `reject`,
   plus the notice that the account has no content left. Two CDN requests so far
   (prepare, send).
7. **Approve.** The command port answers 202 `enqueued` (state `approved`, job
   `publish_pipeline`). No container and no upload exist yet.
8. **Fake publish.** The story is `posted`, `publish_step = effect_confirmed`,
   `published_via = api`. The bytes were staged to transit once, the container was
   made over that copy, one publish call was made, and the staged copy was destroyed
   (the fake store holds no asset). Both operations, `container_create` and
   `publish`, are `succeeded`.
9. **Audit chain and repost lock.** Exactly five transitions,
   `scheduled → prompt_pending → awaiting_approval → approved → publishing → posted`.
   One `recent` lock on the item, expiring more than 29 days out, and `last_posted_at`
   set on that item alone.
10. **The card learns the outcome from the ledger**: two edits, "Approved by" then
    "Posted", with no further CDN request.

### 2. Posted myself closes a card without a provider call

`test_posted_myself_closes_a_past_posts_card_without_a_provider_call`. API publishing is
off. A Post now tap is refused with reason `manual_mode` and nothing moves.
`mark_posted` answers 200 `executed`, state `posted`, `published_via = manual`,
`publish_step = none`; three transitions end `awaiting_approval → posted`; one
`recent` lock; the card edit reads "Posted by". No Meta call, no transit upload and no
further Graph or CDN read.

### 3. Off then On holds a waiting card and resumes it

`test_off_then_on_holds_a_waiting_card_without_reading_and_resumes_it`. Switching the
source Off (mix revision 2) moves its epoch by one. While Off, `sync_now` is refused
with `source_read_reason = source_off`, the waiting card's approval is refused
(`media_unsuitable`, `source_reason = source_off`), and the sweeps and the sender touch
no provider. Switching On (revision 3) makes the same story approvable: the publish
renews the authorization (audit reason `past_posts_authorization_renewed`, at the new
epoch), stages the story once, and posts it once.

### 4. A source policy revision withdraws approval until it qualifies again

`test_a_source_policy_revision_withdraws_a_waiting_cards_approval_until_it_qualifies_again`.
Raising the minimum age to 36,500 days (policy revision 2) makes the 2022 post
`too_recent`: the approval is refused by name and nothing queues. Saving against the
revision it already left is a `policy_conflict` that names revision 2. Saving 90 days
again (revision 3) makes the same card approvable, and the publish records
`last_authorized_policy_revision = 3` and one renewal audit.

### 5. A permit refused at the boundary keeps the staged copy

`test_a_permit_refused_at_the_boundary_keeps_the_staged_copy_and_resumes_without_a_second_upload`.
The source goes Off the moment the container exists. The story stays `approved` at
`publish_step = container_ready` with `source_off` as its last error, one container
and no publish exist, the staged copy is still held, and the job waits with its attempt
given back. After On, the retry resumes at the container: it stages nothing, creates
nothing, reads nothing, and publishes once. The container's permit carries the old
epoch and the publish's carries the epoch the Off and On made.

### 6. A repeated deletion episode cleans each generation once

`test_a_repeated_deletion_episode_cleans_each_generation_once_and_spares_the_rest`,
with an Off Drive folder beside the past-posts source. After the story posts, the residue is five
index rows, one intent provenance, two operation provenance, one artifact, five live
items and no tombstone.

- **Episode one.** A signed deletion request is a durable receipt (`pending`, status
  `in_progress`) that freezes the one generation and deletes nothing. The first cleanup
  pass fences it (`deletion_pending`, reads refused with `generation_unavailable`, scope
  `cleaning`) and stops: a queued card edit still needs its routing. After the sender
  delivers it, the next pass redacts and the receipt is `completed`. Residue: no index,
  provenance or artifact rows, five items kept as redacted rows, five tombstones, the
  generation `deleted`. Nothing was read from Instagram to delete.
- **What survives**: the posted story and its `recent` lock, each item's identity (as a
  tombstone), the Drive item and the account's grant.
- **An exact retry** is the same receipt behind a second capability, already complete,
  with no new cleanup.
- **Reconnect** gives a new generation and keeps the saved choice
  (`reconnected_preserved`). Re-indexing restores each post's item from its tombstone,
  and the repost lock still holds the 2022 post (`locked` exclusion 1).
- **A request issued before the reconnect** deletes nothing of the new generation: its
  receipt is `completed` with no scope.
- **Episode two** (a request ordered after the reconnect) is a new receipt that freezes
  the new generation alone and cleans it. Both generations end `deleted`, and the
  first receipt stays complete.

### 7. Two accounts and two workspaces never see each other's past-posts sources

`test_two_accounts_and_two_workspaces_never_see_each_others_past_posts`. Two accounts
share one workspace, each with its own source at 0.5. Each index holds only its own
posts, and every Graph request carries the token of the account it names. Each slot's
story comes from its own past-posts source, the Off Drive folder is drawn by neither, and the
cards are captioned for their own accounts. Approving one story posts through that
account alone. Switching B Off moves only B's epoch and refuses B's approval and read.
A second workspace's person is refused the first workspace's sources and, as the API's
login under its own tenant, counts zero index rows, items, intents and jobs. Drive is
unchanged.

### 8. The OAuth callback interleaved with a mix save

`test_oauth_callback_interleaved_with_mix_save`. While Instagram's page is open,
another admin saves a custom mix. The callback lands: the connection succeeds and the
history does not (`connected_history_off_mix_conflict`, `next_action = review_mix`),
the source is Off, the other admin's custom mix is kept (the new source joins it at 0),
and no provider read was made or asked for. A reconnect keeps the saved choice (`reconnected_preserved`). Turning
History on later is the explicit choice: an equal split, then the first read goes out
as that account.

### 9. The gate enters no worker or API entrypoint

`test_the_gate_enters_no_worker_or_api_entrypoint`. A syntax-tree scan of the gate file
finds no `src.main` import and no `worker.main`, `run` or `_run` attribute.

## Reading a failure

- **The Linux message above**: the run is on the wrong platform. Nothing is wrong
  with the scene.
- **`isolated provider gate refused to start`**: the guard named a variable or file.
  Fix the environment, not the guard.
- **`effect(s) refused by the isolated provider gate`** at the end of a run: something
  tried a socket, a name or a transport the guard forbids. The list says which.
- **`waiting for the suite cluster lock`**: another run holds the cluster. Wait, or use
  a cluster of your own.

## Clean up

Nothing persists. Remove the container (`docker rm -f storydump-gate-pg`), or stop the
scratch cluster and delete its directory, and unset `ENCRYPTION_KEY`. The fixtures drop
their scratch databases and roles at teardown, and the next run on the same cluster
sweeps a crashed run's strays, which is why the cluster is dropped rather than reused.

## What a green run does not show

Every provider is a fake. The gate does not satisfy any activation gate: Meta's access
path for this use, the shared Graph version, the live field and pagination check,
mixed-video validator compatibility, or the deployment and drain evidence. Those are
listed in [`instagram-past-posts-review.md`](instagram-past-posts-review.md#2-what-is-still-unproven).

## See also

- [`instagram-past-posts-review.md`](instagram-past-posts-review.md) — testing, prerequisites, activation, permissions, the key ring and limits
- [`meta-callback-endpoints.md`](meta-callback-endpoints.md) — the deauthorize and data-deletion endpoints scene 6 drives
- `tests/isolation_guard.py`, `tests/test_isolation_guard.py` — the guard and its refusals
- `tests/fixtures/fake_providers.py`, `tests/fixtures/instagram_onboarding/` — the fakes and the generated past-posts sources
- [`testing-guide.md`](../guides/testing-guide.md) — the general recipe for the database-gated suites
