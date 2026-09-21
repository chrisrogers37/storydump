# Storydump Documentation

Welcome to the Storydump documentation hub. All project documentation is organized here by purpose.

**Last Updated**: 2026-09-20
**Current Version**: 1.6.0 (`src/__init__.py`, and the CHANGELOG's last release heading, 2026-02-09); everything since — the multi-tenant rebuild and the legacy tear-out included — is under `[Unreleased]`
**Current program**: the [consolidated design plan](planning/2026-08-02-consolidated-design-plan/README.md) — Phases 0, F and L built; the legacy tier retired (the tear-out, #1216), with M.3's last two steps run in the owner's window on 2026-09-19 as the gated migrations 079 and 080; **Phase X.3 (multi-workspace UX) in progress**; Phase S partly built. Per-increment scoreboard in that README's *Live status*.
**One tier**: the system these pages describe is the tier the plan calls *target* (`src/services/target/`). The legacy tier was retired in the tear-out (#1216, September 2026); its data survives as the `archive.*_pre_cutover_20260917` snapshots. Pages under `archive/`, `planning/` and `updates/` are history and keep the legacy names; `guides/` and `operations/` are live, and `tests/test_agent_docs.py` fails when one of them names a legacy-only table, a deleted module path or a retired variable.
**Deployment**: Railway (worker + API) + Neon PostgreSQL; landing site and dashboard on Vercel

## Documentation Structure

```
documentation/
├── README.md (this file)          # Documentation index
├── ROADMAP.md                     # Historical: the v1.0.0 – v1.6.0 product roadmap and version history
├── planning/                       # Live plans and increment specs
│   ├── 2026-08-02-consolidated-design-plan/   # THE authoritative plan (ratified, in execution)
│   ├── 2026-09-09-telegram-interaction-at-throughput/  # the Telegram tap, built for throughput
│   ├── 2026-09-15-cli-v2/                     # the `storydump` CLI — completed (spec: 2026-09-15-cli-v2-spec.md)
│   ├── 2026-09-16-cli-v2-audit/               # the system review of the CLI surface
│   ├── 2026-09-16-legacy-tear-out/            # retiring the legacy tier (#1216): five phases and the RUN_LOG
│   ├── 2026-09-21-worker-login-doors/         # #751 part 2: doors for the worker's four tenant-less sweeps, then its switch
│   └── investigations/                        # production investigations, one folder per incident (resolved ones move to archive/investigations/)
├── archive/                        # Completed, superseded and abandoned plans, and the legacy tier's pages (see archive/README.md)
├── guides/                         # How-to guides and tutorials
└── operations/                     # Operational runbooks
```

---

## Planning & Architecture

- `planning/investigations/` — production investigations (`/investigate-app`): one folder per incident, the record plus its fix plans — [2026-09-11 approved but never posted: the first fetch](planning/investigations/publish-first-fetch_2026-09-11/00_INVESTIGATION.md) (with its three follow-ups: the failure reporting, the fetch path, the float). Resolved: [2026-09-04 the sign-in bounce and the Instagram redirect](archive/investigations/2026-09-04-signin-bounce-and-instagram-redirect/00_INVESTIGATION.md) and [2026-09-06 the empty library after the first sync](archive/investigations/2026-09-06-empty-library-after-first-sync/00_INVESTIGATION.md), archived 2026-09-20.

### Consolidated design plan (2026-08-02) — authoritative
**[2026-08-02-consolidated-design-plan/](planning/2026-08-02-consolidated-design-plan/README.md)** - RATIFIED, IN EXECUTION
- The single plan for the multi-tenant refactor: fixed constraints FC-0..FC-9, target architecture, executable domain model, decision record D1–D41, the increment sequence (Phases 0 → F → L → M → X → S), operational numbers, product lifecycles, security model
- Read `README.md` → `00` → `04`; the README's *Live status* carries the position (2026-09-19: the legacy tier is retired) and the per-increment scoreboard with one tracker per increment
- Position: Phases 0, F and L built · the M.3 window applied by hand; 3f ran as migration 078; 3g and the stand-down ran as the gated 079 and 080 in the owner's window on 2026-09-19 ([operations/legacy-window-close.md](operations/legacy-window-close.md)) · X.3 in progress (#1172) · S partly built

### Increment specs (live; each carries a status banner)
- **[2026-09-09-telegram-interaction-at-throughput/](planning/2026-09-09-telegram-interaction-at-throughput/00_EPIC.md)** — the Telegram tap (W4) built for throughput: the tap, the API under load, worker throughput, tenant fairness (flagged) — RATIFIED 2026-09-09 (forks F1–F12 locked); phases 1, 2, 3a and 3b BUILT (2026-09-10/11, per the consolidated plan's *Live status*); phase 4 (tenant fairness) stays evidence-gated
- **[2026-09-15-cli-v2/](planning/2026-09-15-cli-v2/00_EPIC.md)** — the `storydump` v2 CLI: API tokens, the eight read views, the write verbs, `health`/`deploys`/`webhook`/`doctor`, and the deletion of the legacy `cli/` — COMPLETED 2026-09-15 (spec: [2026-09-15-cli-v2-spec.md](planning/2026-09-15-cli-v2-spec.md), approved; the production probes the read views were built from are under `probes/`)
- **[2026-09-16-cli-v2-audit/](planning/2026-09-16-cli-v2-audit/00_AUDIT.md)** — the system review of the v2 CLI surface and the fold of its findings
- **[2026-09-16-legacy-tear-out/](planning/2026-09-16-legacy-tear-out/00_EPIC.md)** — retiring the legacy tier (#1216) in five PRs: delete the code, retire the settings and entry points, snapshot every legacy table into `archive` (078), ship the gated drop and stand-down (079, 080), bring the documentation to the end state — RATIFIED; all five phases shipped (#1316, #1319, #1318, #1321, #1322) and the owner's window ran on 2026-09-19: the `legacy` schema is dropped (079) and the migration window stood down (080). [RUN_LOG.md](planning/2026-09-16-legacy-tear-out/RUN_LOG.md) is the ledger
- **[2026-09-21-worker-login-doors/](planning/2026-09-21-worker-login-doors/00_PLAN.md)** — #751 part 2: the worker measured statement by statement — four tenant-less sweeps of the class that blinded the health surfaces (the sender sweep, the prompt sweep, the stranded alert, the reconciler's poll) get doors for their reads and tenant claims for their writes; then the worker's switch.

### Archive
**[archive/README.md](archive/README.md)** — index of completed, superseded and abandoned plans, moved out of `planning/` on 2026-09-02: the two 2026-07-29 design packages the consolidated plan adjudicated, the 2026-07 full-system review, the 2026-05/06 Instagram investigations, the pre-refactor product phases (Shopify, Printify, LLM, order email, dashboard) and roadmap, and the completed credential-refactor, session-isolation, web-app-migration and Meta-launch plans.

Moved on 2026-09-20 (this audit), the five window-era increment specs — each with an archived banner:
- **[2026-08-11-f1-ownership-inventory/](archive/2026-08-11-f1-ownership-inventory/README.md)** — F.1 ownership inventory and fail-closed interface spec — COMPLETED as legacy, then SUPERSEDED by `unit_of_work.py` / `tenant_resolution.py`; BUILT (#846) as the legacy repository layer's fail-closed interface, and RETIRED with that layer in the tear-out (phase 01, #1316); the target's equivalent is the unit of work, unconstructible without a tenant (`src/services/target/unit_of_work.py`)
- **[2026-08-14-f2-increment-split/](archive/2026-08-14-f2-increment-split/README.md)** — F.2 migration split — COMPLETED (migrations 052–060)
- **[2026-08-17-m1-transform-spec/](archive/2026-08-17-m1-transform-spec/README.md)** — M.1 legacy → target transform — ABANDONED by owner ruling 2026-09-02 (legacy data not migrated; spec retained as the record)
- **[2026-08-17-m2-rehearsal-spec/](archive/2026-08-17-m2-rehearsal-spec/README.md)** — M.2 window rehearsal — EXECUTED DIFFERENTLY THAN WRITTEN (its banner, 2026-09-19): 3a–3d by hand on 2026-08-24/26, 3f/3g/step 8 as migrations 078–080, rehearsed on a PITR branch first; the window's runbook is [operations/legacy-window-close.md](operations/legacy-window-close.md)
- **[2026-08-17-m3-parity-bar-mapping/](archive/2026-08-17-m3-parity-bar-mapping/README.md)** — M.3 Telegram parity bar — SUPERSEDED (its forks ruled 2026-08-21; every bar item served on a target surface; chat-inbound commands #854 remain owed)

Moved there on 2026-09-18 (the tear-out's phase 05), because each describes the legacy tier:
- **[archive/updates/](archive/updates/)** — the three update notes of January 2026: [bug fixes](archive/updates/2026-01-04-bugfixes.md), [category scheduling](archive/updates/2026-01-10-category-scheduling.md), [force posting and the queue shift](archive/updates/2026-01-11-force-posting-queue-shift.md) (from `updates/`)
- **[archive/2026-05-telegram-delivery-burst-postmortem.md](archive/2026-05-telegram-delivery-burst-postmortem.md)** — the May 2026 postmortem of the legacy Telegram delivery path (from `operations/`); the target delivers through `channel_outbox`
- **[archive/2026-07-14-cloudinary-feature-gap-analysis.md](archive/2026-07-14-cloudinary-feature-gap-analysis.md)** — the July 2026 Cloudinary proposals, measured against the legacy tier's use (from `cloudinary/`); the target's path is `src/services/target/transit.py`
- **[archive/multi-account-dashboard.md](archive/multi-account-dashboard.md)** — the legacy dashboard's instance picker (from the top-level `planning/`), superseded by workspaces and their members

### Test Coverage
**[TEST_COVERAGE.md](guides/TEST_COVERAGE.md)** - CURRENT (rewritten 2026-09-18)
- The suite's shape by directory (about 3,700 tests: the DB gates under `tests/scripts/`, the services and the API under `tests/src/`, `tests/storydump_cli/`), with the command that re-measures it
- How coverage is measured, the integration-coverage policy (a green run must not claim coverage it never took), and what is not covered
- The per-file table of the legacy tier's tests it used to be went with those tests in the tear-out (#1216)

---

## Getting Started Guides

### Quick Start
**[quickstart.md](guides/quickstart.md)**
- Routes by intent: using the hosted product, working on it, or operating it
- Onboarding happens at storydump.app — there is no instance to install
- Troubleshooting common issues

### Deployment Guide (Railway + Neon)
**[deployment.md](guides/deployment.md)**
- 11-section deployment checklist for production: what has to be done outside of code
- The bot, the Neon database (step 0, the by-hand base, then the migration runner), the provider apps (Meta, Google, Cloudinary), Railway's two services (worker + API) and their variables
- The first workspace on the web (link Telegram, add a group, connect Drive), team onboarding, backups, monitoring, the testing phase and launch

### Cloud Deployment Guide (Railway + Neon)
**[cloud-deployment.md](guides/cloud-deployment.md)**
- The operator's reference for how the hosted deployment is put together, and for standing up a second environment shaped like it
- Two Railway services (worker + API), the Neon database, the variables each process reads (`.env.example` is the authoritative list: a test holds it in exact agreement with what the code reads)
- The providers around them: the bot, Instagram OAuth, Google OAuth, Cloudinary
- Monitoring and operations, security checklist, troubleshooting

### Testing Guide
**[testing-guide.md](guides/testing-guide.md)**
- Running tests: `make test`, `make test-unit`, `make test-quick`, `pytest` with markers
- The test database: created per session and dropped afterwards, `.env.test`, the fixture architecture in `tests/conftest.py`
- CI/CD integration (GitHub Actions); the size and shape of the suite are in [TEST_COVERAGE.md](guides/TEST_COVERAGE.md)

### Instagram Login Setup
**[instagram-login-setup.md](guides/instagram-login-setup.md)**
- The one flow a workspace uses to connect an Instagram account (Settings › Accounts › Connect Instagram): OAuth direct through Instagram, no Facebook Page
- The external setup behind it: the Meta developer app, the redirect URI, the variables the API reads, test users, verification and troubleshooting
- Going live: App Review (the runbook is [operations/meta-app-review.md](operations/meta-app-review.md))

The legacy Facebook-Login setup guide was removed: the design plan's FC-4 rules
out the Facebook Page path ("never make a user auth a Facebook Page again"), so
a guide walking a reader through it contradicted a fixed constraint.

### Development Environment Setup
**[dev-environment-setup.md](guides/dev-environment-setup.md)**
- Local development with cloud deployment (Railway + Neon)
- Shell aliases for development and production
- Database options (local PostgreSQL or Neon)
- Railway CLI deployment workflow
- Quick reference command card

### Deployment Options
**[deployment-options.md](guides/deployment-options.md)**
- Railway auto-deploy from GitHub (public repo safe)
- GitHub Actions CI (lint, test, security scan on cloud runners)
- Why not self-hosted runners (public repo security)

### CI/CD Pipeline
**[ci-cd-pipeline.md](guides/ci-cd-pipeline.md)**
- GitHub Actions CI pipeline (lint, test, security, changelog check)
- Railway auto-deploy CD pipeline
- Security for public repos (cloud runners only)

### Landing Site on Vercel
**[landing-vercel-deployment.md](guides/landing-vercel-deployment.md)**
- The Next.js site and dashboard in `landing/`: its Vercel environment variables (client vs server), common issues, project settings

---

## Operations & Maintenance

### Monitoring & Alerting
**[operations/monitoring.md](operations/monitoring.md)**
- The operator's instruments: the `storydump` CLI (`health`, `deploys`, the read verbs), the fleet monitors under `scripts/`, Railway's dashboard and logs — nothing reads the database directly
- What to watch: the float, jobs and the outbox, a posting burst, the deployment's posture, the worker's status line, the error rate
- Alerting and restart procedures

### Scheduling-Outage and Posting-Outage Monitors
**[operations/scheduling-monitor.md](operations/scheduling-monitor.md)** · **[operations/posting-monitor.md](operations/posting-monitor.md)**
- `scripts/scheduling_monitor.py` polls `GET /health/scheduling` and alerts when the schedule cursor stops advancing; `scripts/posting_monitor.py` polls `GET /health/posting` and alerts when no post has landed
- Why each runs outside the app, the verdicts, the thresholds, and how they are deployed

### Worker Recovery
**[operations/worker-recovery.md](operations/worker-recovery.md)**
- What the worker is (job leases and their heartbeat, the clock election, its own `/health`), how a dead or stuck one is recognised from outside, and how it is brought back on Railway
- Restarting the production worker is a production action: approved stories publish, so an agent asks first

### Backup & Restore
**[operations/backup-restore.md](operations/backup-restore.md)**
- What holds state and what backs it up: the database on Neon (point-in-time restore, a marker before a risky change, restoring in place), media (the tenant's own Drive), configuration and secrets
- The legacy tier's backup: the `archive.*_pre_cutover_20260917` snapshots and their lifetime
- Disaster recovery for the whole deployment

### Migration Runner
**[operations/migration-runner.md](operations/migration-runner.md)**
- `scripts/migration_runner.py`: `apply`, `adopt`, `status`, `repair`, `parity`; the ledger (`runner.schema_migrations`); the file markers, `-- runner:manual` included; the production rollout (every deploy's predeploy runs `apply`)

### Closing the Legacy Window (the owner's runbook)
**[operations/legacy-window-close.md](operations/legacy-window-close.md)**
- The last two M.3 steps, shipped as gated migrations the deploy owes and never runs: 079 drops the `legacy` schema behind an in-file precondition, 080 stands the window down
- Irreversible, and the owner's to run: `apply --manual` is on the never-run list for agents; run by the owner on 2026-09-19 (the tear-out's RUN_LOG records it) — kept as the record, and for a fresh database, where both files are still owed

### Telegram Webhook
**[operations/telegram-webhook.md](operations/telegram-webhook.md)**
- Which bot, the two-act arming (secret on the API, then `setWebhook`), and `storydump webhook` — status / register / deregister without a secret ever being pasted

### Runtime Database Roles (F.4 rollout)
**[operations/runtime-database-roles.md](operations/runtime-database-roles.md)**
- Moving the API and worker off the owner login onto `svc_ingress` / `svc_worker`, one service at a time
- Verified through `/health`'s `db_role` field and the worker's boot log line; rollback per step

### Reading the Ledger
**[operations/reading-the-ledger.md](operations/reading-the-ledger.md)**
- The eight `storydump` read verbs (story, cards, floating, account, jobs, outbox, burst, posture) that replaced the one-off SQL probes: what each answers, `--since`, `--watch`, and what they never read
- `psql` through Railway as the read-only escape hatch

### Troubleshooting
**[operations/troubleshooting.md](operations/troubleshooting.md)**
- Quick diagnostics: `storydump doctor`, `storydump health`, the logs
- By symptom: a service that won't start, stories not going out, the bot not responding, a refused token, media not syncing, database connections
- Emergency procedures: stop all posting, a story that must not post, a forced restart

### Meta App Review and Meta's Callbacks
**[operations/meta-app-review.md](operations/meta-app-review.md)** · **[operations/meta-callback-endpoints.md](operations/meta-callback-endpoints.md)**
- The App Review submission runbook (#410): which app, which fields, the wait times, the demo videos
- The deauthorize and data-deletion endpoints under `/webhooks/meta`: what they do, what they deliberately do not, and their bounds

### Google OAuth Verification
**[operations/google-oauth-verification.md](operations/google-oauth-verification.md)**
- Clearing the "Google hasn't verified this app" warning on the Drive consent screen (#333)

### Fetching a Preview Deployment
**[operations/fetching-preview-deployments.md](operations/fetching-preview-deployments.md)**
- Reading a Vercel preview of `landing/` headlessly, past deployment protection, so a front-end change can be looked at rather than inferred

---

## Project Updates

There is no live update note. The running record of changes is [../CHANGELOG.md](../CHANGELOG.md); an incident gets a folder under `planning/investigations/`.

The three notes of January 2026 — [bug fixes](archive/updates/2026-01-04-bugfixes.md), [category scheduling](archive/updates/2026-01-10-category-scheduling.md), [force posting and the queue shift](archive/updates/2026-01-11-force-posting-queue-shift.md) — describe the legacy scheduler, queue, bot and CLI, all deleted in the tear-out (#1216), and moved to `archive/updates/` on 2026-09-18. `updates/` no longer exists; the changelog and an investigation folder are where a change or an incident is recorded.

---

## Security

**[archive/2026-07-system-review/triage-tracker.md](archive/2026-07-system-review/triage-tracker.md)** - Full-system review (2026-07-02) — ARCHIVED audit record
- 91 triaged findings (bugs, security, architecture, over-complication, tests) under 5 cross-cutting epics
- Filed as one GitHub issue per finding, not the clustered 36 the document planned: 5 epics (#560, #576, #577, #578, #579) + #580–#658, all under the `system-review` label — 72 open / 12 closed on 2026-09-02
- The live state is the label, not the document; the consolidated plan absorbed the tenancy, migration and multi-worker epics into Phases F, 0 and L

**The security model the system is built to**: the consolidated plan's [07-security-model.md](planning/2026-08-02-consolidated-design-plan/07-security-model.md) (Google sign-in and sessions, the OAuth state flows, credential encryption and key rotation, audit integrity, the existence-oracle rule, first-party API auth for the CLI) and [02-domain-model.md](planning/2026-08-02-consolidated-design-plan/02-domain-model.md) §7 (the service roles, row-level security, the `SECURITY DEFINER` doors). [operations/runtime-database-roles.md](operations/runtime-database-roles.md) says where the deployed posture stands against it; `storydump posture` reads it live.

**[archive/2026-01-11-security-review.md](archive/2026-01-11-security-review.md)** - Reviewed 2026-01-11, updated 2026-02-15 — ARCHIVED (2026-09-18): a review of the legacy tier
- Its repository-hygiene findings (no hardcoded credentials, `.env` gitignored, every secret from the environment) are the review's own, as of 2026-02; nothing has re-measured them since
- Its bot sections (the collaborative channel as the security boundary, the admin-only command pattern) describe the legacy polling bot, deleted in the tear-out (#1216); the bot that exists executes a card tap as the linked member who tapped it, through the command port

---

## API Documentation

The API describes itself: a running API serves its schema at `GET /openapi.json` (FastAPI builds it from the routes; `uvicorn src.api.app:app`). What it mounts (`src/api/app.py`):
- `/auth` — sign-in with Google, sign-out, and the OAuth callbacks (Google Drive, Instagram Login)
- `/api/v1` — reads as resources; state changes as the command port, `POST /api/v1/workspaces/{ws}/commands/{command}`; API tokens; the `/ops` read views the `storydump` CLI reads
- `/webhooks/telegram` and `/webhooks/meta` — the providers' deliveries
- `/health`, `/health/scheduling`, `/health/posting` — the probe and the two surfaces the fleet monitors poll

The design is the consolidated plan's `01-target-architecture.md` (the interaction-layer port) and `07-security-model.md` §6 (first-party API auth); [../AGENTS.md](../AGENTS.md) › The command port is the short form. There is no SDK: the `storydump` CLI is the first-party client.

---

## Quick Reference

### For New Developers
1. Start with **[quickstart.md](guides/quickstart.md)**, then **[dev-environment-setup.md](guides/dev-environment-setup.md)** (local setup)
2. Read **[testing-guide.md](guides/testing-guide.md)** (understand testing)
3. Read **[../AGENTS.md](../AGENTS.md)** (architecture, the command port, setup, commands, testing) and the **[consolidated design plan](planning/2026-08-02-consolidated-design-plan/README.md)** (where the system is going)
4. Read root **[CLAUDE.md](../CLAUDE.md)** for the safety rules (it defers to `AGENTS.md` for everything else)

### For Deploying to Production
1. Follow **[deployment.md](guides/deployment.md)** step-by-step
2. Complete Telegram bot setup (Section 1); the bot's webhook is **[operations/telegram-webhook.md](operations/telegram-webhook.md)**
3. Configure Neon database (Section 2); the schema is the migration runner's — **[operations/migration-runner.md](operations/migration-runner.md)**
4. Deploy to Railway (Section 4)
5. Test and go live (Sections 5-11)
6. For Instagram: **[instagram-login-setup.md](guides/instagram-login-setup.md)**
7. For cloud-specific details: **[cloud-deployment.md](guides/cloud-deployment.md)**

### For Understanding Architecture
1. Read **[../AGENTS.md](../AGENTS.md)** — layering rules, the command port, services, what is deliberately not wired
2. Read the consolidated plan's `01-target-architecture.md` and `02-domain-model.md` for the design behind the tier (`src/services/target/`, `src/models/target/`)
3. The pre-refactor roadmap and its diagrams are archived at `archive/phases/00_MASTER_ROADMAP.md`

### For Contributing Code
1. Read **[../AGENTS.md](../AGENTS.md)** (the layer boundaries, testing, pre-commit and CI) and the safety rules, repeated in root **[CLAUDE.md](../CLAUDE.md)**
2. Review **[testing-guide.md](guides/testing-guide.md)** (test requirements — every feature needs tests)
3. Check the consolidated plan's `04-execution-sequence.md` for the increment your change belongs to
4. Run pre-commit: `source venv/bin/activate && ruff check . && ruff format --check . && pytest` (the two `ruff` commands are CI's Lint job over the whole tree; `pytest` is its Test job, run against a PostgreSQL service)

---

## Additional Resources

### Root-Level Documentation
These critical files remain in the project root for visibility:

- **[../README.md](../README.md)** - Project overview and quick start
- **[../CHANGELOG.md](../CHANGELOG.md)** - Version history and release notes
- **[../AGENTS.md](../AGENTS.md)** - The canonical developer and agent guide (safety rules, architecture, the command port, setup, commands, testing, services)
- **[../CLAUDE.md](../CLAUDE.md)** - Claude Code specifics plus the safety rules, repeated; it defers to `AGENTS.md` for everything else

### External Documentation
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Instagram Graph API](https://developers.facebook.com/docs/instagram-api)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [Cloudinary Documentation](https://cloudinary.com/documentation)
- [Google Drive API](https://developers.google.com/drive/api)

---

## Contributing Documentation

When adding new documentation:

1. **Choose the right location:**
   - Planning/design → `planning/`
   - How-to guides → `guides/`
   - Operations → `operations/`
   - Bug fixes/patches → `../CHANGELOG.md`; a production incident → a folder under `planning/investigations/`
   - Completed, superseded or abandoned plans, and any page that describes something the
     tree no longer holds → `git mv` to `archive/` (same layout), add a one-line status
     banner at the top, and add a row to `archive/README.md`
   - A page under `guides/` or `operations/` is LIVE: describe what the code does, cite
     `path:line` where a reader would check, and name no legacy-only table, deleted module
     path or retired variable — `tests/test_agent_docs.py` fails on one

2. **Update this index** (`documentation/README.md`)

3. **Follow naming conventions:**
   - Use lowercase with hyphens: `backup-restore.md`
   - Be descriptive: `telegram-bot-setup.md` not `setup.md`
   - Date updates: `2026-02-10-feature-name.md`

4. **Keep root-level clean:**
   - Only critical files in project root (README, CHANGELOG, AGENTS.md, CLAUDE.md)
   - Everything else goes in `documentation/`

---

## Documentation Coverage

| Area | Status | Files | Notes |
|------|--------|-------|-------|
| **Planning** | Current | 34 Markdown files: the 10-file consolidated plan, the Telegram tap plan, the CLI plan with its spec and audit, the legacy tear-out plan, 3 investigations | Plan ratified and in execution (Phase X.3 current); each spec carries a status banner |
| **Archive** | Historical | 56 Markdown files | Completed, superseded and abandoned plans and the legacy tier's pages, indexed in `archive/README.md` |
| **Guides** | Live | 10 guides | Quick start, deployment, cloud deployment, testing, test coverage, Instagram Login, dev env, deployment options, CI/CD, landing deploy |
| **Operations** | Live | 15 files | Monitoring, the two outage monitors, worker recovery, backup, the migration runner, the legacy window's close, the Telegram webhook, runtime database roles, reading the ledger, troubleshooting, Meta App Review + callbacks, Google OAuth verification, preview deployments |
| **API Docs** | Served | — | `GET /openapi.json` on a running API |

Counted on 2026-09-18 (`find documentation/<area> -name '*.md'`).

### Document Status Legend
- **COMPLETED** - Fully implemented and documented
- **IN PROGRESS** - Work actively underway
- **PENDING** - Planned for future implementation
- **PROPOSED** - Design is documented but still requires review and phase-specific approval
- **ARCHIVED** - Completed, superseded or abandoned; kept under `archive/` as the record, with a status banner
- **CURRENT** / **LIVE** - Describes the system that exists; a page under `guides/` or `operations/` is held to that by `tests/test_agent_docs.py`
- **HISTORICAL** - Describes the legacy tier or an earlier product phase; kept where it is as the record, and said to be history where it is indexed

---

## Need Help?

- **Setup issues?** → See [quickstart.md](guides/quickstart.md) troubleshooting, or [dev-environment-setup.md](guides/dev-environment-setup.md) for a local environment
- **Deployment questions?** → Check [deployment.md](guides/deployment.md)
- **Test failures?** → Review [testing-guide.md](guides/testing-guide.md)
- **Architecture questions?** → Read [../AGENTS.md](../AGENTS.md), then the [consolidated design plan](planning/2026-08-02-consolidated-design-plan/README.md)
- **Version history?** → Check [../CHANGELOG.md](../CHANGELOG.md); [ROADMAP.md](ROADMAP.md) is the historical record of v1.0.0 – v1.6.0
- **Something wrong in production?** → [operations/troubleshooting.md](operations/troubleshooting.md), then [operations/reading-the-ledger.md](operations/reading-the-ledger.md)
- **Instagram setup?** → Follow [instagram-login-setup.md](guides/instagram-login-setup.md)
- **Security concerns?** → The plan's [07-security-model.md](planning/2026-08-02-consolidated-design-plan/07-security-model.md); [archive/2026-01-11-security-review.md](archive/2026-01-11-security-review.md) is the legacy tier's review, archived

---

*Last updated: 2026-09-20*
