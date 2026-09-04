# Storydump Documentation

Welcome to the Storydump documentation hub. All project documentation is organized here by purpose.

**Last Updated**: 2026-09-02
**Current Version**: v1.6.0 (last tagged release; the 2026-08 multi-tenant refactor is under `[Unreleased]` in the CHANGELOG)
**Current program**: the [consolidated design plan](planning/2026-08-02-consolidated-design-plan/README.md) — Phases 0, F and L built; Phase M partially executed; **Phase X.3 (multi-workspace UX) in progress**; Phase S pending. Per-increment scoreboard in that README's *Live status*.
**Deployment**: Railway (worker + API) + Neon PostgreSQL; landing site and dashboard on Vercel

## Documentation Structure

```
documentation/
├── README.md (this file)          # Documentation index
├── ROADMAP.md                     # Product roadmap and version history
├── SECURITY_REVIEW.md             # Security audit findings
├── planning/                       # Live plans and increment specs
│   ├── 2026-08-02-consolidated-design-plan/   # THE authoritative plan (ratified, in execution)
│   ├── 2026-08-11-f1-ownership-inventory/     # F.1 spec — built (#846); burn-down on #841
│   ├── 2026-08-14-f2-increment-split/         # F.2 split — complete (migrations 052–060)
│   ├── 2026-08-17-m1-transform-spec/          # M.1 — abandoned (legacy data not migrated)
│   ├── 2026-08-17-m2-rehearsal-spec/          # M.2 — not executed
│   └── 2026-08-17-m3-parity-bar-mapping/      # M.3 parity bar — deferred
├── archive/                        # Completed, superseded and abandoned plans (see archive/README.md)
├── cloudinary/                     # Cloudinary usage analyses and enhancement proposals
├── guides/                         # How-to guides and tutorials
├── operations/                     # Operational runbooks
└── updates/                        # Project updates, bugfixes, patches
```

---

## Planning & Architecture

### Consolidated design plan (2026-08-02) — authoritative
**[2026-08-02-consolidated-design-plan/](planning/2026-08-02-consolidated-design-plan/README.md)** - RATIFIED, IN EXECUTION
- The single plan for the multi-tenant refactor: fixed constraints FC-0..FC-9, target architecture, executable domain model, decision record D1–D41, the increment sequence (Phases 0 → F → L → M → X → S), operational numbers, product lifecycles, security model
- Read `README.md` → `00` → `04`; the README's *Live status* carries the per-increment scoreboard (2026-09-02) and the three rulings applied in practice but not yet written into `00`/`03`
- Position: Phases 0, F and L built · the M.3 window applied by hand and not closed out (3f/3g and the stand-down owed) · X.3 in progress (#1172) · S pending
- Trackers: #746 (Phase 0), #806 (F.2), #841 (F.1), #751 (F.4), #790 (Phase M), #1172 (X.3), #1212 (state of play)

### Increment specs (live; each carries a status banner)
- **[2026-08-11-f1-ownership-inventory/](planning/2026-08-11-f1-ownership-inventory/README.md)** — F.1 ownership inventory and fail-closed interface spec — BUILT (`src/repositories/tenant_scope.py`); residual burn-down on #841
- **[2026-08-14-f2-increment-split/](planning/2026-08-14-f2-increment-split/README.md)** — F.2 migration split — COMPLETED (migrations 052–060)
- **[2026-08-17-m1-transform-spec/](planning/2026-08-17-m1-transform-spec/README.md)** — M.1 legacy → target transform — ABANDONED by owner ruling 2026-09-02 (legacy data not migrated; spec retained as the record)
- **[2026-08-17-m2-rehearsal-spec/](planning/2026-08-17-m2-rehearsal-spec/README.md)** — M.2 window rehearsal — NOT EXECUTED
- **[2026-08-17-m3-parity-bar-mapping/](planning/2026-08-17-m3-parity-bar-mapping/README.md)** — M.3 Telegram parity bar — DEFERRED (#854)

### Archive
**[archive/README.md](archive/README.md)** — index of completed, superseded and abandoned plans, moved out of `planning/` on 2026-09-02: the two 2026-07-29 design packages the consolidated plan adjudicated, the 2026-07 full-system review, the 2026-05/06 Instagram investigations, the pre-refactor product phases (Shopify, Printify, LLM, order email, dashboard) and roadmap, and the completed credential-refactor, session-isolation, web-app-migration and Meta-launch plans.

### Test Coverage Report
**[TEST_COVERAGE.md](guides/TEST_COVERAGE.md)** - CURRENT (1,417 tests)
- Test suite summary by layer (77 test files)
- Phase 1.6 through Phase 2 test additions
- Coverage gaps and future work
- Test infrastructure documentation

---

## Cloudinary

### Feature Gap Analysis & Enhancement Proposals
**[cloudinary/2026-07-14-feature-gap-analysis.md](cloudinary/2026-07-14-feature-gap-analysis.md)** - PROPOSED
- Current usage model (transient post-time hosting for the Instagram hop) mapped file:line, vs Cloudinary's July 2026 announcements (AI image generation add-on, self-service OAuth, VS Code extension GA)
- Eight sized proposals (P0–P7): call timeout/offload substrate, persistent storage for web uploads (#317), tag-scoped lifecycle (#450/#550), q_auto delivery, generative 9:16 framing toggle, video normalization, pHash perceptual dedup, exploratory AI content generation (#152/#189)
- Explicit non-proposals and unverified-claims sections; per-feature Cloudinary source citations

---

## Getting Started Guides

### Quick Start
**[quickstart.md](guides/quickstart.md)**
- Routes by intent: using the hosted product, working on it, or operating it
- Onboarding happens at storydump.app and in the Telegram bot — there is no instance to install
- Telegram bot commands for day-to-day use
- Troubleshooting common issues

### Deployment Guide (Railway + Neon)
**[deployment.md](guides/deployment.md)**
- 11-section deployment checklist for production
- Telegram bot setup (BotFather, channel, admin ID)
- Neon PostgreSQL database configuration
- Railway two-service deployment (worker + web)
- Media setup (Google Drive), team onboarding, backups, monitoring

### Cloud Deployment Guide (Railway + Neon)
**[cloud-deployment.md](guides/cloud-deployment.md)**
- Railway two-process deployment (worker + web)
- Neon PostgreSQL setup with SSL and pool sizing
- Full environment variable reference
- OAuth callback setup (Instagram + Google Drive)
- Onboarding Mini App HTTPS configuration
- Monitoring, costs, security checklist, troubleshooting

### Testing Guide
**[testing-guide.md](guides/testing-guide.md)**
- Automatic test database setup and fixture architecture
- Running tests: `make test`, `make test-unit`, `pytest` with markers
- 1,417 tests collected as of v1.6.0
- Test fixtures and patterns (session-scoped DB, function-scoped transactions)
- CI/CD integration (GitHub Actions)

### Instagram Login Setup
**[instagram-login-setup.md](guides/instagram-login-setup.md)**
- The current path: OAuth direct through Instagram, Instagram User access tokens
- Scopes, redirect configuration, and token refresh via `graph.instagram.com`
- Cloudinary integration for media hosting
- Multi-account management

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

---

## Operations & Maintenance

### Monitoring & Alerting
**[operations/monitoring.md](operations/monitoring.md)**
- Railway service health checks and log streaming
- SQL queries for queue health, posting rate, token expiry
- Alerting thresholds (stuck posts, token expiry, error rate)
- Health check script

### Backup & Restore
**[operations/backup-restore.md](operations/backup-restore.md)**
- Database backup procedures (manual via `pg_dump`, automated via cron)
- Media files backup (rsync to external storage)
- Configuration backup (.env, tokens)
- Disaster recovery steps

### Telegram Webhook
**[operations/telegram-webhook.md](operations/telegram-webhook.md)**
- Which bot, the two-act arming (secret on the API, then `setWebhook`), and `scripts/telegram_webhook.py` — status / register / deregister without a secret ever being pasted

### Runtime Database Roles (F.4 rollout)
**[operations/runtime-database-roles.md](operations/runtime-database-roles.md)**
- Moving the API and worker off the owner login onto `svc_ingress` / `svc_worker`, one service at a time
- Verified through `/health`'s `db_role` field and the worker's boot log line; rollback per step

### Troubleshooting
**[operations/troubleshooting.md](operations/troubleshooting.md)**
- Service won't start (common causes, log inspection)
- Posts not going out (queue, scheduling, dry-run check)
- Telegram bot not responding (token, webhook, permissions)
- Instagram API errors (rate limits, token expiry, account selection)
- Media indexing failures (paths, permissions, formats)
- Emergency procedures (service restart, queue reset)

---

## Project Updates

### Feature Updates
**[2026-01-11-force-posting-queue-shift.md](updates/2026-01-11-force-posting-queue-shift.md)** - COMPLETED
- Force posting via `/next` and `process-queue --force`
- Queue slot-shift logic (subsequent items inherit earlier time slots)

**[2026-01-10-category-scheduling.md](updates/2026-01-10-category-scheduling.md)** - COMPLETED
- Category-based media organization (folder to category extraction)
- Configurable posting ratios per category (Type 2 SCD)
- Scheduler integration with category-aware slot allocation
- New CLI commands: `list-categories`, `update-category-mix`, `category-mix-history`

### Bug Fixes & Patches
**[2026-01-04-bugfixes.md](updates/2026-01-04-bugfixes.md)** - COMPLETED
- 4 critical bugs fixed (service run metadata, scheduler date mutation, health check, lock creation)
- All bugs identified during code review and deployment testing

*Note: Updates folder contains dated documents for bug fixes, patches, and significant changes.*
*Line numbers in older updates reference pre-refactor code (v1.6.0 refactored TelegramService).*

---

## Security

**[archive/2026-07-system-review/triage-tracker.md](archive/2026-07-system-review/triage-tracker.md)** - Full-system review (2026-07-02) — ARCHIVED audit record
- 91 triaged findings (bugs, security, architecture, over-complication, tests) under 5 cross-cutting epics
- Filed as one GitHub issue per finding, not the clustered 36 the document planned: 5 epics (#560, #576, #577, #578, #579) + #580–#658, all under the `system-review` label — 72 open / 12 closed on 2026-09-02
- The live state is the label, not the document; the consolidated plan absorbed the tenancy, migration and multi-worker epics into Phases F, 0 and L

**[SECURITY_REVIEW.md](SECURITY_REVIEW.md)** - Reviewed 2026-01-11, Updated 2026-02-10
- No hardcoded credentials found
- `.env` properly gitignored, all secrets via environment variables
- Collaborative bot design (intentional, private channel = security boundary)
- Token encryption (Fernet) for Instagram API credentials in database
- Cloning the repo exposes zero credentials
- Optional admin-only command pattern documented

---

## API Documentation

Coming in Phase 5 (Dashboard UI):
- REST API endpoints (FastAPI)
- Authentication flows (JWT via Telegram Login Widget)
- Rate limiting
- WebSocket events
- SDK documentation

---

## Quick Reference

### For New Developers
1. Start with **[quickstart.md](guides/quickstart.md)**, then **[dev-environment-setup.md](guides/dev-environment-setup.md)** (local setup)
2. Read **[testing-guide.md](guides/testing-guide.md)** (understand testing)
3. Read **[../AGENTS.md](../AGENTS.md)** (architecture, the command port, setup, commands, testing) and the **[consolidated design plan](planning/2026-08-02-consolidated-design-plan/README.md)** (where the system is going)
4. Read root **[CLAUDE.md](../CLAUDE.md)** for the safety rules (it defers to `AGENTS.md` for everything else)

### For Deploying to Production
1. Follow **[deployment.md](guides/deployment.md)** step-by-step
2. Complete Telegram bot setup (Section 1)
3. Configure Neon database (Section 2)
4. Deploy to Railway (Section 4)
5. Test and go live (Sections 5-11)
6. For Instagram: **[instagram-login-setup.md](guides/instagram-login-setup.md)**
7. For cloud-specific details: **[cloud-deployment.md](guides/cloud-deployment.md)**

### For Understanding Architecture
1. Read **[../AGENTS.md](../AGENTS.md)** — layering rules, the command port, services, what is deliberately not wired
2. Read the consolidated plan's `01-target-architecture.md` and `02-domain-model.md` for the target tier (`src/services/target/`, `src/models/target/`)
3. The pre-refactor roadmap and its diagrams are archived at `archive/phases/00_MASTER_ROADMAP.md`

### For Contributing Code
1. Read root **[CLAUDE.md](../CLAUDE.md)** (development guidelines, pre-commit checklist)
2. Review **[testing-guide.md](guides/testing-guide.md)** (test requirements — every feature needs tests)
3. Check the consolidated plan's `04-execution-sequence.md` for the increment your change belongs to
4. Run pre-commit: `source venv/bin/activate && ruff check src/ tests/ && ruff format --check src/ tests/ && pytest`

---

## Additional Resources

### Root-Level Documentation
These critical files remain in the project root for visibility:

- **[../README.md](../README.md)** - Project overview and quick start
- **[../CHANGELOG.md](../CHANGELOG.md)** - Version history and release notes
- **[../CLAUDE.md](../CLAUDE.md)** - Developer guide for AI assistants (safety rules, architecture, patterns)

### External Documentation
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Instagram Graph API](https://developers.facebook.com/docs/instagram-api)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [Python Telegram Bot](https://python-telegram-bot.readthedocs.io/)
- [Cloudinary Documentation](https://cloudinary.com/documentation)

---

## Contributing Documentation

When adding new documentation:

1. **Choose the right location:**
   - Planning/design → `planning/`
   - How-to guides → `guides/`
   - Operations → `operations/`
   - Bug fixes/patches → `updates/` (use dated filenames: `YYYY-MM-DD-description.md`)
   - Completed, superseded or abandoned plans → `git mv` to `archive/` (same layout), add a
     one-line status banner at the top, and add a row to `archive/README.md`

2. **Update this index** (`documentation/README.md`)

3. **Follow naming conventions:**
   - Use lowercase with hyphens: `backup-restore.md`
   - Be descriptive: `telegram-bot-setup.md` not `setup.md`
   - Date updates: `2026-02-10-feature-name.md`

4. **Keep root-level clean:**
   - Only critical files in project root (README, CHANGELOG, CLAUDE.md)
   - Everything else goes in `documentation/`

---

## Documentation Coverage

| Area | Status | Files | Notes |
|------|--------|-------|-------|
| **Planning** | Current | 15 Markdown files: the 10-file consolidated plan + 5 increment specs | Plan ratified and in execution (Phase X.3 current); each spec carries a status banner |
| **Archive** | Historical | 37 files | Completed, superseded and abandoned plans, indexed in `archive/README.md` |
| **Guides** | Current | 10 guides | Setup, deployment, testing, Instagram Login, dev env, deployment options, CI/CD, landing deploy, test coverage (its test count is stale) |
| **Operations** | Current | 11 files | Monitoring, backup, troubleshooting, migration runner, scheduling monitor, worker recovery, Meta App Review + callbacks, Google OAuth verification, preview deployments, one postmortem |
| **Updates** | Current | 3 files | Bugfixes, category scheduling, force posting |
| **Security** | Current | 1 file | Security review (updated post-refactor) |
| **API Docs** | Future | 0 files | Planned for Phase 5 (Dashboard UI) |

### Document Status Legend
- **COMPLETED** - Fully implemented and documented
- **IN PROGRESS** - Work actively underway
- **PENDING** - Planned for future implementation
- **PROPOSED** - Design is documented but still requires review and phase-specific approval
- **ARCHIVED** - Completed, superseded or abandoned; kept under `archive/` as the record, with a status banner

---

## Need Help?

- **Setup issues?** → See [quickstart.md](guides/quickstart.md) troubleshooting, or [dev-environment-setup.md](guides/dev-environment-setup.md) for a local environment
- **Deployment questions?** → Check [deployment.md](guides/deployment.md)
- **Test failures?** → Review [testing-guide.md](guides/testing-guide.md)
- **Architecture questions?** → Read [../AGENTS.md](../AGENTS.md), then the [consolidated design plan](planning/2026-08-02-consolidated-design-plan/README.md)
- **Version history?** → Check [ROADMAP.md](ROADMAP.md) or [../CHANGELOG.md](../CHANGELOG.md)
- **Instagram setup?** → Follow [instagram-login-setup.md](guides/instagram-login-setup.md)
- **Security concerns?** → Review [SECURITY_REVIEW.md](SECURITY_REVIEW.md)

---

*Last updated: 2026-09-02*
