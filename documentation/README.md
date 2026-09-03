# Storydump Documentation

Welcome to the Storydump documentation hub. All project documentation is organized here by purpose.

**Last Updated**: 2026-07-30
**Current Version**: v1.6.0
**Current Phase**: Phase 2 (Instagram API Automation) - COMPLETED | Phase 1.8 (Telegram UX) - COMPLETED
**Next Phase**: Phase 3 (Shopify Integration) - PENDING
**Deployment**: Railway (worker + API) + Neon PostgreSQL

## Documentation Structure

```
documentation/
├── README.md (this file)          # Documentation index
├── ROADMAP.md                     # Product roadmap and version history
├── SECURITY_REVIEW.md             # Security audit findings
├── planning/                       # Planning and design documents
│   ├── 2026-07-system-review/     # Full-system review: triage + detailed findings + issue backlog
│   ├── 2026-07-29-data-model-evaluation/  # Data model evaluation: neutral prompt, self-evaluation, target model, epic, triage, migration plan
│   ├── 2026-07-29-high-throughput-multi-tenant/
│   │                               # Durable multi-tenant/worker architecture and migration plan
│   ├── phases/                    # Phased implementation plans
│   │   ├── 00_MASTER_ROADMAP.md   # Vision, architecture, phase overview
│   │   ├── 02_shopify_integration.md        # PENDING
│   │   ├── 03_printify_integration.md       # PENDING
│   │   ├── 04_media_product_linking.md      # PENDING
│   │   ├── 05_llm_integration.md            # PENDING
│   │   ├── 06_order_email_automation.md     # PENDING
│   │   └── 07_dashboard_ui.md              # PENDING
├── cloudinary/                     # Cloudinary usage analyses and enhancement proposals
├── guides/                         # How-to guides and tutorials
├── operations/                     # Operational runbooks
└── updates/                        # Project updates, bugfixes, patches
```

---

## Planning & Architecture

### High-Throughput Multi-Tenant Architecture
**[2026-07-29-high-throughput-multi-tenant/](planning/2026-07-29-high-throughput-multi-tenant/README.md)** - PROPOSED
- PostgreSQL-authoritative commands, leased jobs, provider operations, and transactional outbox
- Redis-based shared admission and work wake-ups with independent ready-job recovery
- Webhook ingress, fair worker pools, mandatory tenant context, and RLS defense in depth
- Independent review prompt, repository self-evaluation, P0-P3 triage, and file-oriented implementation plan

### Master Roadmap
**[phases/00_MASTER_ROADMAP.md](planning/phases/00_MASTER_ROADMAP.md)** - Phases 1-2.5 COMPLETED
- Vision: E-commerce Optimization Hub for Social Media Marketing
- Architecture principles (strict separation of concerns)
- Service naming conventions and data model strategy
- Data flow diagrams (current Phase 2 and future Phase 5+)
- Phase overview with status markers (8 phases total, 5 completed, 6 pending)

### Active Phase Planning Documents

**[phases/02_shopify_integration.md](planning/phases/02_shopify_integration.md)** - PENDING
- Shopify Admin API integration, product catalog sync (Type 2 SCD), order tracking

**[phases/03_printify_integration.md](planning/phases/03_printify_integration.md)** - PENDING
- Printify API for print-on-demand, product/blueprint sync, fulfillment tracking

**[phases/04_media_product_linking.md](planning/phases/04_media_product_linking.md)** - PENDING
- Many-to-many media-product relationships, attribution tracking, performance analytics

**[phases/05_llm_integration.md](planning/phases/05_llm_integration.md)** - PENDING
- LLM service abstraction (Claude/OpenAI), content suggestions, email drafting

**[phases/06_order_email_automation.md](planning/phases/06_order_email_automation.md)** - PENDING
- Order notifications via Telegram, Gmail API, LLM-drafted customer responses

**[phases/07_dashboard_ui.md](planning/phases/07_dashboard_ui.md)** - PENDING
- Next.js web dashboard, analytics visualizations, media-product management

### Data Model Evaluation (2026-07-29)

**[2026-07-29-data-model-evaluation/](planning/2026-07-29-data-model-evaluation/README.md)** - PROPOSED
- Neutral, reusable prompt for evaluating the system and its data model (no recommendation leakage)
- Repository-grounded self-evaluation: schema inventory, path traces, liabilities, comparison of three target approaches
- Recommended workspace-rooted target model, implementation epic, P0–P3 issue triage, and an
  expand/backfill/dual-write/shadow-read/cutover/contract migration plan with per-consumer coverage and rollback

### Feed & Queue Features (Research)

- **[01: Live Story Visibility](planning/feed-queue-features/01_live_story_visibility.md)** — Fetch & display live stories in `/status` (Ready)
- **[02: Feed Reset](planning/feed-queue-features/02_feed_reset.md)** — Clear live stories from Instagram (Blocked — no DELETE API)

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

**[planning/2026-07-system-review/triage-tracker.md](planning/2026-07-system-review/triage-tracker.md)** - Full-system review (2026-07-02)
- 91 triaged findings (bugs, security, architecture, over-complication, tests)
- Organized under 5 cross-cutting epics; filed as 36 GitHub issues (26 individual P0/P1 + 10 clusters)
- Prioritizes data-integrity, security, and multi-tenant isolation work first
- Includes durable `triage-tracker.md`, `detailed-findings.md`, and a `file-issues.sh` filing script

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
3. Review **[phases/00_MASTER_ROADMAP.md](planning/phases/00_MASTER_ROADMAP.md)** (architecture and roadmap)
4. Read root **[CLAUDE.md](../CLAUDE.md)** for detailed service/model reference and safety rules

### For Deploying to Production
1. Follow **[deployment.md](guides/deployment.md)** step-by-step
2. Complete Telegram bot setup (Section 1)
3. Configure Neon database (Section 2)
4. Deploy to Railway (Section 4)
5. Test and go live (Sections 5-11)
6. For Instagram: **[instagram-login-setup.md](guides/instagram-login-setup.md)**
7. For cloud-specific details: **[cloud-deployment.md](guides/cloud-deployment.md)**

### For Understanding Architecture
1. Check **[phases/00_MASTER_ROADMAP.md](planning/phases/00_MASTER_ROADMAP.md)**
   - Architecture principles and data flow diagrams
   - Service naming conventions (core/, integrations/, domain/)
   - Phase progression and dependencies
2. Read root **[CLAUDE.md](../CLAUDE.md)** for service/model/table reference

### For Contributing Code
1. Read root **[CLAUDE.md](../CLAUDE.md)** (development guidelines, pre-commit checklist)
2. Review **[testing-guide.md](guides/testing-guide.md)** (test requirements — every feature needs tests)
3. Check **[phases/00_MASTER_ROADMAP.md](planning/phases/00_MASTER_ROADMAP.md)** for context
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
   - Completed plans → keep in their current documentation area with a clear
     `COMPLETED` or `Superseded` status

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
| **Planning** | Current | 26 Markdown files, including the 6-document throughput/tenancy set | Phases 1-2 complete; high-throughput design proposed; later product phases pending |
| **Guides** | Current | 9 guides | Setup, deployment, testing, Instagram API, dev env, deployment options, Tailscale, test coverage |
| **Operations** | Current | 3 files | Monitoring, backup, troubleshooting |
| **Updates** | Current | 3 files | Bugfixes, category scheduling, force posting |
| **Security** | Current | 1 file | Security review (updated post-refactor) |
| **API Docs** | Future | 0 files | Planned for Phase 5 (Dashboard UI) |

### Document Status Legend
- **COMPLETED** - Fully implemented and documented
- **IN PROGRESS** - Work actively underway
- **PENDING** - Planned for future implementation
- **PROPOSED** - Design is documented but still requires review and phase-specific approval

---

## Need Help?

- **Setup issues?** → See [quickstart.md](guides/quickstart.md) troubleshooting, or [dev-environment-setup.md](guides/dev-environment-setup.md) for a local environment
- **Deployment questions?** → Check [deployment.md](guides/deployment.md)
- **Test failures?** → Review [testing-guide.md](guides/testing-guide.md)
- **Architecture questions?** → Read [phases/00_MASTER_ROADMAP.md](planning/phases/00_MASTER_ROADMAP.md)
- **Version history?** → Check [ROADMAP.md](ROADMAP.md) or [../CHANGELOG.md](../CHANGELOG.md)
- **Instagram setup?** → Follow [instagram-login-setup.md](guides/instagram-login-setup.md)
- **Security concerns?** → Review [SECURITY_REVIEW.md](SECURITY_REVIEW.md)

---

*Last updated: 2026-07-30*
