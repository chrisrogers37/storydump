---
paths:
  - "src/**/*.py"
  - "storydump_cli/**/*.py"
---

# Development Patterns

One tier: the API (`src/api`), the worker (`src/worker.py`) and the CLI
(`storydump_cli/`) over the services in `src/services/target/`. There is no
repository layer to call. The database side of
every rule below is `.claude/rules/database.md`.

## Layers

- **Adapters call services; services own the SQL.** A route in
  `src/api/routes/` parses, authorizes, calls a function in
  `src/services/target/`, and maps the result to a status. The routes hold one
  SQL statement between them — the `cli_command` audit row a token's write
  leaves (`v1.py:242`); everything else is a service's.
- **`storydump_cli/` is an HTTP client of the API** — never a database
  connection. Its one `src` import is `src/services/target/vocabulary.py`
  (`tests/storydump_cli/test_import_boundary.py` runs a fresh interpreter to
  prove it), so `vocabulary.py` stays dependency-free: no driver, no framework,
  no other service module.
- **Writes go through the command port.** `commands.VOCABULARY` is closed and
  pinned to the architecture row
  (`tests/fixtures/design_rows/01-target-architecture.md`) by
  `tests/src/services/target/test_commands.py`; a new write is a vocabulary
  name, an executor in `command_executors.py`, and a floor in
  `commands.ROLE_FLOOR`. A name without an executor answers `CommandNotBuilt`
  (501) — it is not dropped from the vocabulary.
- **Reads are workspace-keyed and pass one gate**,
  `tenant_resolution.authorize_member`. A workspace the caller cannot see
  answers 404, never 403 — the same 404 a workspace that does not exist gets.

## Service modules

- A service is a module of functions, not a class hierarchy. Functions take the
  caller's executor first (`session` or `conn`) and run in the caller's
  transaction. Follow the module you are extending — the executors
  (`command_executors.py`), the read views (`readers.py`, `ops_views.py`), the
  lanes (`work_loop.py`).
- **The database is the authority.** Where a trigger, a CHECK or a unique index
  enforces a rule, the service issues the statement and translates the refusal
  (`_dbapi.driver_candidates` unwraps the asyncpg error); it does not keep a
  Python copy of the rule to check first.
- **Refusals are typed, with a closed `reason`**: subclass `StorydumpError`
  (`src/exceptions/base.py`), or `RefusalError` when callers route on the
  reason. Callers route on `reason` and never parse the message. Do not log and
  swallow: an unexpected exception propagates, so a crashed job looks crashed
  and its lease expires.
- **Seams are injected, and an absent seam parks.** Provider adapters (Meta,
  Cloudinary transit, Drive, email, the Telegram transport) arrive through
  `WorkerDeps`; a deployment without one parks the kinds that need it, with the
  reason named. Do not substitute a fake in a composition root.
- **Provider HTTP goes through the egress floor**:
  `egress.request(client, method, url, policy=…)` supplies the timeout class,
  one absolute retry budget, the response byte cap and SSRF-safe resolution. A
  new provider host is a deliberate addition: to `DEFAULT_ALLOWED_HOSTS`
  (`egress.py:132`) when every adapter may reach it, otherwise to the one
  module's own policy (`email_sender`, `transit`, the Drive adapter's thumbnail
  fetch). Not inside an open transaction.
- **Numbers are parameters.** Operational numbers live in `WorkerConfig` and
  arrive as arguments; the pinned ones are named seam constants
  (`POOL_SIZE_SEAM`). A literal in a service or a door body is a review blocker.
- An unservable input is a NAMED outcome with a log line — not a silent drop
  and not a raise that strands an admitted delivery (`telegram_dispatch.py`).

## Logging

```python
import logging

logger = logging.getLogger(__name__)

logger.info("plan_slot: no media for account %s — notified %d binding(s)", acct, n)
logger.warning("reconcile poll for intent %s: %s", intent_id, type(exc).__name__)
logger.exception("sender-job sweep failed; retrying on cadence")
```

- A module logger and `%`-style arguments, not f-strings: 1 of the 287
  `logger.<level>(…)` calls under `src/` and `storydump_cli/` passes an
  f-string (`src/api/app.py:689`, measured 2026-10-24).
  `src/utils/logger.py`'s shared `logger` is what the API routes import; either logger is in use, the
  argument style is the convention.
- A deliberate swallow logs with `logger.exception` every time, naming what was
  NOT done ("NO JOBS WERE MINTED"). A counter says something is failing; only
  the log says what.
- Never log a token, a secret, a connection string or `encrypted_payload`. A URL
  that embeds a token is redacted before it reaches an exception or a log line
  (`TelegramTransport.redact`); a provider's error body is summarised by status,
  not quoted.
- uvicorn's access line and the app's own refusal lines print no query string
  and no invitation token. `src/utils/logger.py` puts `AccessLogRedactor` on
  `uvicorn.access` and on the trace-level `uvicorn.asgi`, which print
  `?<redacted>` and mask a credential path segment (`CREDENTIAL_PATH_PREFIXES`).
  An application line names a request by `loggable_path(request.scope)`: its
  route template, with only `LOGGABLE_PATH_PARAMS` values filled in. Never name
  it by `request.url`, `scope["path"]` or the query.
  `tests/src/api/test_log_redaction.py` fails on a log line or an exception
  message that does. A new secret travels in a header or the body; a route that
  must carry one in its path joins `CREDENTIAL_PATH_PREFIXES`. Even redacted,
  do not run the API at `--log-level trace` (or `UVICORN_LOG_LEVEL=trace`) on
  real traffic: it logs every ASGI message of every request.
- httpx and httpcore log at WARNING (`LIBRARY_LOG_LEVELS`, set at import):
  httpx's INFO line prints the whole outbound URL, and a provider URL carries a
  secret. Never lower them to see requests; log the call's outcome yourself.

## Media

- A file's kind comes from its MIME type, not its name: `image/*` → `image`,
  `video/*` → `video` (`google_drive_adapter._kind_for`, `:315`). A Drive name
  need not carry an extension at all.
- Byte caps are per use: a story on its way to Meta is 8 MiB for an image and
  40 MB for a video (`vocabulary.PUBLISH_MAX_BYTES`; the video cap is Cloudinary's
  synchronous-transform limit), a Telegram card's preview 10 MiB and 50 MiB
  (`media_adapters.MEDIA_CARD_MAX_BYTES`). Oversize is refused from
  metadata, before a download.
- Framing is done at delivery, not on upload: `transit.story_transformation`
  (`transit.py:218`) scales to 1080 × 1920 and pads to 9:16 over a blurred copy,
  delivered as JPG or MP4. Nothing in `src/` resizes an image.

## Security patterns

- CORS admits ONE origin, `settings.web_app_origin`, with credentials; with no
  origin configured none is admitted. Never `"*"` (`_cors_origins`, `src/api/app.py:803`).
- `TRUSTED_PROXY_HOSTS` is a network list and never `"*"`: the wildcard makes
  every IP-keyed control caller-partitionable (`src/config/settings.py:300`).
- No existence oracle: a non-member's workspace is 404; a rejected webhook
  secret answers the same 403 whether the secret is wrong or unset.
- Compare secrets in constant time (`hmac.compare_digest`,
  `webhook_ingress.verify_secret_token`). Store a hash of a bearer token, never
  the token (`service_tokens.token_hash`, `session_tokens.token_hash`).
- Bound every numeric API input at the route: `Query(default, ge=1, le=MAX)`
  (`v1.py:638`, `ops.py:93`), and clamp again in the service so a direct
  caller is bounded too (`ops_views.floating`).
- An agent-facing or operator-facing tool prints no secret: `storydump webhook`
  reads the deployment's variables and echoes none of them.
