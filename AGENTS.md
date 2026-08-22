# AGENTS.md

General development guidance lives in `CLAUDE.md` (architecture, safety rules, CLI
commands, testing). Read it first — its **CRITICAL SAFETY RULES** apply to all agents.

## Cursor Cloud specific instructions

This VM is provisioned for local development of the Python backend (worker + FastAPI
web + `storydump-cli`) and the Next.js `landing/` site. The update script keeps the
Python `venv` and `landing/` npm deps fresh; the notes below cover the non-obvious
startup caveats it intentionally does NOT handle.

### Safety (read `CLAUDE.md` first)
- NEVER run the worker or any posting path: `python -m src.main`, `storydump-cli process-queue`, `create-schedule`, `reset-queue`, `instagram-auth`. These poll/post to Telegram/Instagram. Inspection commands (`list-*`, `check-health`, `index-media`, `validate-image`) are safe.

### PostgreSQL (must be started each session — not auto-started)
- Start the local cluster before anything that touches the DB:
  `sudo pg_ctlcluster 16 main start`
- Connection (already provisioned, matches `.env`): host `localhost`, db `storydump`, user `storydump_user`, password `storydump` (role has `CREATEDB`+`SUPERUSER` so the pytest suite can create/drop `storydump_test`).
- If the role/db are ever missing, recreate with `make setup-db` after setting the same creds, then apply `scripts/setup_database.sql` + everything in `scripts/migrations/*.sql` in numeric order.

### Environment files (gitignored — live only on the VM)
- `.env`, `.env.test`, and `landing/.env.local` are already created and are NOT committed.
- `src/config/settings.py` requires `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `ADMIN_TELEGRAM_CHAT_ID` even for the web service, CLI, and tests. Dummy values are set — only the worker (which we don't run) needs real Telegram credentials.
- `.env.test` must keep `LOG_LEVEL=DEBUG`; with `WARNING` the logger drops INFO lines and `tests/src/utils/test_logger.py::test_logger_includes_timestamp` fails.
- Tests need `ENCRYPTION_KEY` (a Fernet key) in `.env.test`; a generated one is present.

### Running the services (use the `./venv`)
- Lint (matches CI): `./venv/bin/ruff check .` and `./venv/bin/ruff format --check .`.
- Tests: `./venv/bin/pytest` (auto-creates/drops `storydump_test`). ~2240 tests; a handful skip when optional integrations are absent.
- FastAPI web: `./venv/bin/uvicorn src.api.app:app --host 0.0.0.0 --port 8000` → health at `GET /health`, OpenAPI at `/openapi.json`. Dashboard API under `/api/onboarding/*` requires Telegram WebApp `init_data` (HMAC-signed with `TELEGRAM_BOT_TOKEN`) + an active `UserChatMembership` for the `chat_id`.
- Next.js landing/dashboard: `npm --prefix landing run dev` → http://localhost:3000 (dashboard BFF proxies to `BACKEND_URL`, the FastAPI service).
