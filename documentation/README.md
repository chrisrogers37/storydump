# Storydump Documentation

Welcome to the Storydump documentation hub. All project documentation is organized here by purpose.

**Last Updated**: 2026-07-06 (docs audit — see note below)
**Current Version**: v1.6.0 in `src/__init__.py`, but stale — see [PROJECT_MISSION.md](../PROJECT_MISSION.md) and [planning/phases/00_MASTER_ROADMAP.md](planning/phases/00_MASTER_ROADMAP.md) for what has actually shipped since
**Current Phase**: Phase 2 (Instagram API Automation) - COMPLETED | Multi-tenant/multi-instance rearchitecture - SHIPPED (post-v1.6.0, unplanned in this index until now)
**Next Phase**: Phase 3 (Shopify Integration) - PENDING
**Deployment**: Railway (worker + API) + Neon PostgreSQL + Vercel (`landing/` web dashboard)

> **2026-07-06 audit note**: This index had drifted significantly from the codebase — it linked to an `archive/` directory that was intentionally purged 2026-04-28 (#311), was missing ~15 documents that were never added to this index, and understated test counts. This pass fixes the structural issues; see the "Documentation Coverage" table at the bottom for what's now current vs. still worth a follow-up pass.

## Documentation Structure

```
documentation/
├── README.md (this file)          # Documentation index
├── ROADMAP.md                     # Product roadmap and version history
├── SECURITY_REVIEW.md             # Security audit findings
├── api/                            # API reference (added 2026-07-06)
├── planning/                       # Planning and design documents
│   ├── phases/                    # Phased implementation plans
│   │   ├── 00_MASTER_ROADMAP.md   # Vision, architecture, phase overview
│   │   ├── 02_shopify_integration.md        # PENDING
│   │   ├── 03_printify_integration.md       # PENDING
│   │   ├── 04_media_product_linking.md      # PENDING
│   │   ├── 05_llm_integration.md            # PENDING
│   │   ├── 06_order_email_automation.md     # PENDING
│   │   └── 07_dashboard_ui.md              # see status in index below
│   ├── feed-queue-features/       # Feed & queue research docs
│   └── investigations/            # Dated incident/bug investigations
├── guides/                         # How-to guides and tutorials
├── operations/                     # Operational runbooks + postmortems
└── updates/                        # Retired 2026-07-06 (historical only — see CHANGELOG.md)
```
*(There is no `archive/` directory — it was intentionally purged 2026-04-28 (#311); superseded docs live in git history instead. `documentation/README.md`'s "Archive" section below was not fully cleaned up after that purge until this audit.)*

---

## Planning & Architecture

### Master Roadmap
**[phases/00_MASTER_ROADMAP.md](planning/phases/00_MASTER_ROADMAP.md)** - see file for current per-phase status table (corrected 2026-07-06; the previous "8 phases total, 5 completed, 6 pending" summary here didn't add up and has been removed rather than replaced with another guess)
- Vision: E-commerce Optimization Hub for Social Media Marketing
- Architecture principles (strict separation of concerns)
- Service naming conventions and data model strategy
- Data flow diagrams (current Phase 2 and future Phase 5+)
- Note: this file references `01_settings_and_multitenancy.md` and `archive/01_instagram_api.md` — neither exists in the repo (flagged in-file 2026-07-06)

### Active Phase Planning Documents

**[phases/02_shopify_integration.md](planning/phases/02_shopify_integration.md)** - PENDING (verified 2026-07-06 — no Shopify code/tables/tests exist beyond an enum-style string mention)
- Shopify Admin API integration, product catalog sync (Type 2 SCD), order tracking

**[phases/03_printify_integration.md](planning/phases/03_printify_integration.md)** - PENDING (verified 2026-07-06)
- Printify API for print-on-demand, product/blueprint sync, fulfillment tracking

**[phases/04_media_product_linking.md](planning/phases/04_media_product_linking.md)** - PENDING (verified 2026-07-06 — `src/services/domain/` exists but is empty)
- Many-to-many media-product relationships, attribution tracking, performance analytics

**[phases/05_llm_integration.md](planning/phases/05_llm_integration.md)** - PENDING (verified 2026-07-06)
- LLM service abstraction (Claude/OpenAI), content suggestions, email drafting
- Note: `src/services/core/caption_service.py` already calls the Anthropic API in production for AI-generated captions (migration 026, `ANTHROPIC_API_KEY` + `enable_ai_captions`) — a real, shipped LLM feature, but architecturally unrelated to this plan's spec. Kept as PENDING since it isn't a stepping-stone toward this specific design, not because no LLM usage exists at all.

**[phases/06_order_email_automation.md](planning/phases/06_order_email_automation.md)** - PENDING (verified 2026-07-06)
- Order notifications via Telegram, Gmail API, LLM-drafted customer responses

**[phases/07_dashboard_ui.md](planning/phases/07_dashboard_ui.md)** - 🔧 IN PROGRESS (status corrected 2026-07-06, was PENDING)
- Next.js web dashboard, analytics visualizations, media-product management
- A real dashboard is live at `landing/src/app/(dashboard)/` (Overview, Media Library, Analytics, Settings, Setup Wizard) matching much of this doc's tech-stack section — but it shipped via the separate multi-tenant/onboarding initiative, not by executing this plan, and uses a different architecture (Next.js BFF + signed Telegram `init_data`, not a generic JWT REST API). Missing this doc's Product & Performance section entirely (depends on Shopify/Printify, which don't exist yet). See in-file notes for detail.

### Feed & Queue Features (Research)

*Note: this index previously linked `00_OVERVIEW.md` and `03_queue_enhancements.md` — neither file exists; removed 2026-07-06.*

- **[01: Live Story Visibility](planning/feed-queue-features/01_live_story_visibility.md)** — Fetch & display live stories in `/status` (Ready, not yet implemented — verified 2026-07-06; doc also flags a stale API host reference worth fixing before implementation)
- **[02: Feed Reset](planning/feed-queue-features/02_feed_reset.md)** — Clear live stories from Instagram (Blocked — confirmed 2026-07-06, still no Instagram DELETE API for stories)

### Other Planning Documents

*The following documents existed on disk but were never linked from this index — added 2026-07-06 after a full documentation audit:*

- **[2026-03-31-meta-app-launch-design.md](planning/2026-03-31-meta-app-launch-design.md)** - ✅ COMPLETED — Meta Graph API v21.0 bump + `InstagramLoginOAuthService`, live in production
- **[2026-05-18-instagram-credential-refactor.md](planning/2026-05-18-instagram-credential-refactor.md)** - ✅ COMPLETED — all 5 PRs landed (migrations 035-041); `instagram_accounts.instagram_account_id` legacy column intentionally kept, tracked as a separate follow-up
- **[instagram-deeplink-redirect.md](planning/instagram-deeplink-redirect.md)** - ⚠️ BUILT BUT NOT ACTIVATED — `docs/index.html` implements the redirect exactly as designed, but the bot's "Open Instagram" button still points at the plain `instagram.com` feed URL; the env var to wire it up was added then deliberately removed 2026-05-12. The page is live on GitHub Pages but nothing links to it.
- **[per-request-session-isolation.md](planning/per-request-session-isolation.md)** - 📋 PLANNING ONLY — confirmed nothing implemented yet, not currently scheduled
- **[web-app-migration-plan.md](planning/web-app-migration-plan.md)** - ✅ COMPLETED (substantially) — `landing/` is the full app this plan envisioned; several naming/implementation details diverged from the original sketch (see doc for specifics). Strong candidate for archiving/closing out.
- **[investigations/ig-oauth-cross-flow-reconnect_2026-05-25/](planning/investigations/ig-oauth-cross-flow-reconnect_2026-05-25/)** - ✅ RESOLVED — reconnect-loop fix shipped and verified live (#441)
- **[investigations/ig-host-routing_2026-06-02/](planning/investigations/ig-host-routing_2026-06-02/)** - ✅ RESOLVED — all 5 planned PRs merged 2026-06-02/03, no recurrence since

**Also see:** [planning/multi-account-dashboard.md](planning/multi-account-dashboard.md) — the multi-tenant/multi-instance design doc, ~88% verified-implemented as of 2026-07-06 (moved here from the repo root during this audit — it previously lived outside `documentation/`). See the [Multi-Tenant / Multi-Instance Rearchitecture](ROADMAP.md#multi-tenant--multi-instance-rearchitecture-shipped) entry in ROADMAP.md.

### Test Coverage Report
**[TEST_COVERAGE.md](guides/TEST_COVERAGE.md)** - Top-line counts refreshed 2026-07-06 (2,038 tests via `pytest --collect-only`, 103 files — per-layer breakdown further down the doc is older and unverified)
- Test suite summary by layer (103 test files)
- Phase 1.6 through Phase 2 test additions, plus substantial growth since (multi-tenant work)
- Coverage gaps and future work
- Test infrastructure documentation

---

## Getting Started Guides

### Quick Start (10 Minutes)
**[quickstart.md](guides/quickstart.md)**
- Fastest path to running the application
- Step-by-step setup: clone, venv, database, configure, index media, run
- Essential CLI and Telegram bot commands
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
- 2,038 tests collected as of 2026-07-06 (was 1,417 as of v1.6.0 — see [TEST_COVERAGE.md](guides/TEST_COVERAGE.md))
- Test fixtures and patterns (session-scoped DB, function-scoped transactions)
- CI/CD integration (GitHub Actions)

### Instagram API Setup
**[instagram-api-setup.md](guides/instagram-api-setup.md)**
- Meta Business Suite and developer app setup (12 steps)
- Instagram Graph API token generation and extension
- Cloudinary integration for media hosting
- Multi-account management via CLI commands
- Token bootstrapping (.env to DB), encryption, and troubleshooting

### Instagram Login Setup
**[instagram-login-setup.md](guides/instagram-login-setup.md)**
- Newer "Instagram Login" OAuth flow (distinct from the legacy Facebook Login flow above)
- Added as part of the 2026-05 credential refactor — see [planning/2026-05-18-instagram-credential-refactor.md](planning/2026-05-18-instagram-credential-refactor.md)

### Landing Site / Dashboard Deployment (Vercel)
**[landing-vercel-deployment.md](guides/landing-vercel-deployment.md)**
- Deploying the `landing/` Next.js web dashboard + marketing site to Vercel
- Environment variables, BFF proxy configuration, instance picker

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

### Troubleshooting
**[operations/troubleshooting.md](operations/troubleshooting.md)**
- Service won't start (common causes, log inspection)
- Posts not going out (queue, scheduling, dry-run check)
- Telegram bot not responding (token, webhook, permissions)
- Instagram API errors (rate limits, token expiry, account selection)
- Media indexing failures (paths, permissions, formats)
- Emergency procedures (service restart, queue reset)

### Worker Recovery
**[operations/worker-recovery.md](operations/worker-recovery.md)**
- Recovering the Railway worker process after a crash or stuck deploy

### Google OAuth Verification
**[operations/google-oauth-verification.md](operations/google-oauth-verification.md)**
- Google OAuth consent screen verification process for Drive access

### Postmortems
**[operations/2026-05-telegram-delivery-burst-postmortem.md](operations/2026-05-telegram-delivery-burst-postmortem.md)**
- 2026-05-17 → 2026-05-19 Telegram delivery failure burst (958 failures) — root cause and follow-up fixes (#467)

---

## Project Updates

**⚠️ Retired 2026-07-06.** This folder stopped being maintained after 2026-01-11 despite dozens of shipped PRs since — `CHANGELOG.md`'s `[Unreleased]` section is the de facto changelog and has been for months. Rather than resume a practice that had already lapsed, this is now the documented convention going forward: **log changes in `CHANGELOG.md`, not here.** The 3 files below are kept as historical record; no new ones should be added.

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

*Note: this folder historically contained dated documents for bug fixes, patches, and significant changes — retired 2026-07-06, see notice above.*
*Historical handoffs are no longer kept in-repo — `archive/` was purged 2026-04-28 (#311); use git history instead.*
*Line numbers in older updates reference pre-refactor code (v1.6.0 refactored TelegramService).*

---

## Security

**[SECURITY_REVIEW.md](SECURITY_REVIEW.md)** - Reviewed 2026-01-11, Updated 2026-02-15, **Addendum 2026-07-06**
- No hardcoded credentials found
- `.env` properly gitignored, all secrets via environment variables
- Collaborative bot design (intentional, private channel = security boundary)
- Token encryption (Fernet) for Instagram API credentials in database
- Cloning the repo exposes zero credentials
- Optional admin-only command pattern documented
- ⚠️ **See §11 addendum**: a critical cross-tenant data-isolation gap (onboarding/dashboard API + Telegram queue callbacks) was found and fixed 2026-06-28 (#511/#512, PR #519). Role-based command authorization for multi-tenant remains an open follow-up.

---

## API Documentation

**[api/README.md](api/README.md)** - Added 2026-07-06 (this previously said "Coming in Phase 5" — the dashboard API had already shipped with no reference doc)

- `src/api/routes/onboarding/` — Mini App onboarding/dashboard/settings REST endpoints (32 routes across setup/dashboard/settings), gated by `MembershipService` (see [SECURITY_REVIEW.md](SECURITY_REVIEW.md) §11)
- `src/api/routes/oauth.py` — Instagram/Google OAuth callback routes
- Auth is signed Telegram `initData` (Mini App) or a signed URL token, not a generic API-key/SDK model; the web dashboard reaches this API through a BFF proxy holding a JWT with `activeChatId`
- Rate limiting exists (SlowAPI, 30/min global default, 5-10/min on mutating settings endpoints) — no WebSocket layer

---

## Archive

There is no in-repo `archive/` directory. It existed through early 2026 but was **intentionally purged 2026-04-28** ("chore: purge stale archives and complete Storydump rebrand in docs", #311) as part of the Storydump rebrand cleanup. The follow-up commit that was supposed to scrub references to it (#312, same day) missed this section — that's why it listed 11 dead links until this audit.

Superseded/completed planning docs are no longer kept in-repo after completion. To find historical plans (the ones formerly listed here — Phase 1 completion checklist, v1.2.0 handoff, Phase 1.5/1.7 planning, the original `instagram_automation_plan.md`, TelegramService refactor plan, etc.), use git history, e.g.:
```bash
git log --all --diff-filter=D --summary -- 'documentation/archive/*'
```

---

## Quick Reference

### For New Developers
1. Start with **[quickstart.md](guides/quickstart.md)** (10 min setup)
2. Read **[testing-guide.md](guides/testing-guide.md)** (understand testing)
3. Review **[phases/00_MASTER_ROADMAP.md](planning/phases/00_MASTER_ROADMAP.md)** (architecture and roadmap)
4. Read root **[CLAUDE.md](../CLAUDE.md)** for detailed service/model reference and safety rules

### For Deploying to Production
1. Follow **[deployment.md](guides/deployment.md)** step-by-step
2. Complete Telegram bot setup (Section 1)
3. Configure Neon database (Section 2)
4. Deploy to Railway (Section 4)
5. Test and go live (Sections 5-11)
6. For Instagram API: **[instagram-api-setup.md](guides/instagram-api-setup.md)**
7. For cloud-specific details: **[cloud-deployment.md](guides/cloud-deployment.md)**

### For Understanding Architecture
1. Read root **[PROJECT_MISSION.md](../PROJECT_MISSION.md)** first — the current product mental model (User → Instances → accounts/media/queue). Root `README.md`'s Architecture section describes single-tenant Phase 1-2 history; this is the multi-tenant present.
2. Check **[phases/00_MASTER_ROADMAP.md](planning/phases/00_MASTER_ROADMAP.md)**
   - Architecture principles and data flow diagrams
   - Service naming conventions (core/, integrations/, domain/)
   - Phase progression and dependencies
3. Read root **[CLAUDE.md](../CLAUDE.md)** for service/model/table reference

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
   - Planning/design → `planning/` (inside `documentation/` — not the repo root; `multi-account-dashboard.md` was found misplaced at repo root and moved here during the 2026-07-06 audit)
   - How-to guides → `guides/`
   - Operations → `operations/`
   - Bug fixes/patches → `CHANGELOG.md` under `## [Unreleased]` (not `updates/` — that folder was retired 2026-07-06; see the Project Updates section above)
   - Completed plans → delete or leave in place with a status marker; there is no `archive/` directory anymore (purged 2026-04-28, #311) — rely on git history instead of moving files

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
| **Planning** | Current (as of 2026-07-06 audit) | 18 (7 phase plans + 2 feed-queue + 3 investigations + 5 dated top-level docs + `multi-account-dashboard.md`, relocated here from repo root this audit) | Phases 1-2 complete, 3+ pending — see phase docs for per-phase status |
| **Guides** | Current | 11 (10 guides + TEST_COVERAGE.md) | Setup, deployment (3 overlapping docs — see gap analysis), testing, Instagram API (2 flows), dev env, landing/Vercel |
| **Operations** | Current | 6 files | Monitoring, backup, troubleshooting, worker recovery, Google OAuth verification, 1 postmortem |
| **Updates** | Retired 2026-07-06 | 3 files (historical only) | Formally retired in favor of `CHANGELOG.md` `[Unreleased]` — see Project Updates section |
| **Security** | Current | 1 file + §11 addendum (2026-07-06) | Cross-tenant IDOR found & fixed 2026-06-28; role-based command auth still open |
| **Archive** | Removed | 0 (purged 2026-04-28, #311) | Use `git log --all --diff-filter=D -- 'documentation/archive/*'` for history |
| **API Docs** | Current | 1 file (added 2026-07-06) | `src/api/routes/` (REST + Mini App onboarding) — see [api/README.md](api/README.md) |

### Document Status Legend
- **COMPLETED** - Fully implemented and documented
- **IN PROGRESS** - Work actively underway
- **PENDING** - Planned for future implementation

---

## Need Help?

- **Setup issues?** → See [quickstart.md](guides/quickstart.md) troubleshooting
- **Deployment questions?** → Check [deployment.md](guides/deployment.md)
- **Test failures?** → Review [testing-guide.md](guides/testing-guide.md)
- **Architecture questions?** → Read [phases/00_MASTER_ROADMAP.md](planning/phases/00_MASTER_ROADMAP.md)
- **Version history?** → Check [ROADMAP.md](ROADMAP.md) or [../CHANGELOG.md](../CHANGELOG.md)
- **Instagram API setup?** → Follow [instagram-api-setup.md](guides/instagram-api-setup.md)
- **Security concerns?** → Review [SECURITY_REVIEW.md](SECURITY_REVIEW.md)

---

*Last updated: 2026-07-06*
