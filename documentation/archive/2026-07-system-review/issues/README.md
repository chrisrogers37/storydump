> **Archived 2026-09-02 — COMPLETED audit record.** The filing script's clustering was not used — every finding got its own issue (#580–#658). See [`documentation/archive/README.md`](../../README.md) for the index.

# System Review — Issue Backlog & Filing

This directory turns the [system review](../triage-tracker.md) into GitHub issues.

- **P0 / P1** findings are filed as **individual** issues (one per `TD-NNN`).
- **P2 / P3 / P4 / nice-to-have** findings are filed as **clustered** issues, grouped by theme.
- All issues carry the `auto-audit` label plus a priority + type label.

Total: **36 issues** — 6 × P0, 20 × P1, 10 × clusters — covering all 91 tracker findings.

## Filing the issues

The GitHub CLI in the cloud-agent environment is **read-only**, so the agent
cannot create issues directly. Run the bundled script from an authenticated
environment (local machine or CI with `gh` write scope):

```bash
# Preview what will be created (no writes):
documentation/archive/2026-07-system-review/issues/file-issues.sh

# Actually create the issues:
documentation/archive/2026-07-system-review/issues/file-issues.sh --confirm

# Optional: target a specific repo (defaults to the current repo):
documentation/archive/2026-07-system-review/issues/file-issues.sh --confirm --repo chrisrogers37/storydump
```

The script is **idempotent-friendly**: it prints each title in dry-run mode so
you can diff against existing issues before creating. It only uses labels that
already exist in the repo.

## Priority model

| Priority | Label | Meaning |
|----------|-------|---------|
| P0 | `priority:critical` | Actively exploitable security / cross-tenant exposure or mutation. Fix immediately. |
| P1 | `priority:high` | High-severity integrity / atomicity / credential / schema-foundation bugs. |
| P2 | `priority:medium` | Medium-severity correctness bugs + security hardening. |
| P3 | `priority:medium` | Medium architecture / tech-debt / over-complication. |
| P4 | `priority:low` | Low-severity issues. |
| Nice-to-have | `enhancement` | Enhancements / developer experience. |

## Index

### P0 — Critical (individual, 6)

| # | Title | TD | Labels |
|---|-------|----|--------|
| 1 | initData TTL bypass via future `auth_date` | TD-001 | priority:critical, security, auto-audit |
| 2 | Spoofable / ineffective Mini App rate limiting | TD-002 | priority:critical, security, auto-audit |
| 3 | Mini App bound-token path skips membership authorization | TD-004 | priority:critical, security, auto-audit |
| 4 | `remove-account` is deployment-wide (cross-tenant destructive) | TD-005 | priority:critical, security, auto-audit |
| 5 | Media repository write paths bypass tenant filter | TD-030 | priority:critical, security, bug, auto-audit |
| 6 | Drive factory falls back to global service account on any OAuth error | TD-056 | priority:critical, security, auto-audit |

### P1 — High (individual, 20)

| # | Title | TD | Labels |
|---|-------|----|--------|
| 7 | Autopost success recording is non-atomic | TD-010 | priority:high, bug, auto-audit |
| 8 | Autopost failure/cancel leaves queue row stuck in `processing` | TD-011 | priority:high, bug, auto-audit |
| 9 | Callback queue-completion monkey-patches `session.commit` | TD-012 | priority:high, bug, architecture, auto-audit |
| 10 | Autopost operation locks are process-local (double-post risk) | TD-014 | priority:high, architecture, auto-audit |
| 11 | Cross-tenant hash-lock exclusion in media eligibility | TD-020 | priority:high, bug, auto-audit |
| 12 | Cross-tenant duplicate-hash groups | TD-021 | priority:high, bug, auto-audit |
| 13 | Token UPSERT ignores `auth_method` (overwrites wrong credential) | TD-025 | priority:high, bug, auto-audit |
| 14 | Scheduler selection/preview/availability not tenant-scoped | TD-031 | priority:high, bug, auto-audit |
| 15 | Backfill downloader mutates ORM and commits directly | TD-040 | priority:high, bug, architecture, auto-audit |
| 16 | Backfill hashing uses SHA256 while the rest uses MD5 | TD-046 | priority:high, bug, auto-audit |
| 17 | Media-sync gate opens even when every tenant sync failed | TD-049 | priority:high, bug, auto-audit |
| 18 | Media locks created without `chat_settings_id` | TD-050 | priority:high, bug, auto-audit |
| 19 | Telegram notifications always target the env channel | TD-052 | priority:high, bug, telegram, auto-audit |
| 20 | Prefix lookups are global (cross-tenant queue item/account) | TD-060 | priority:high, bug, telegram, auto-audit |
| 21 | Global pause uses `TELEGRAM_CHANNEL_ID`, not the acting chat | TD-061 | priority:high, bug, telegram, auto-audit |
| 22 | Adopt a real migration tool (Alembic) | TD-070 | priority:high, tech-debt, auto-audit |
| 23 | Prod/test schema divergence (`create_all` vs SQL migrations) | TD-071 | priority:high, tech-debt, auto-audit |
| 24 | Failed token refresh leaves a `FOR UPDATE` lock open | TD-078 | priority:high, bug, auto-audit |
| 25 | No migration / schema-parity tests | TD-100 | priority:high, tech-debt, auto-audit |
| 26 | Repo tests are mock-only; no eligibility/concurrency coverage | TD-101 | priority:high, tech-debt, auto-audit |

### P2 — Medium correctness/hardening (clustered, 4)

| # | Cluster | TDs | Labels |
|---|---------|-----|--------|
| 27 | Security hardening | TD-003, 006, 007, 008, 009, 017, 018, 019, 022, 023, 024 | priority:medium, security, auto-audit |
| 28 | Multi-tenant & data-integrity correctness | TD-026, 051, 034, 035, 036, 037, 065, 072, 073, 031b | priority:medium, bug, auto-audit |
| 29 | Posting / scheduler / integration correctness | TD-042, 043, 044, 045, 047, 048, 090, 091, 083, 084, 013 | priority:medium, bug, auto-audit |
| 30 | DB session & transaction correctness | TD-075, 076, 077, 038, 039 | priority:medium, bug, auto-audit |

### P3 — Medium architecture/debt (clustered, 4)

| # | Cluster | TDs | Labels |
|---|---------|-----|--------|
| 31 | Architecture & layering | TD-032, 033, 053, 054, 055, 057, 058, 074, 079, 059 | priority:medium, architecture, tech-debt, auto-audit |
| 32 | Multi-worker / process-local state | TD-015, 016, 041, 062 | priority:medium, architecture, tech-debt, auto-audit |
| 33 | Telegram UX & duplication debt | TD-086, 087, 089, 092 | priority:medium, telegram, tech-debt, auto-audit |
| 34 | Test coverage gaps | TD-102, 103, 104 | priority:medium, tech-debt, auto-audit |

### P4 — Low + Nice-to-have (clustered, 2)

| # | Cluster | TDs | Labels |
|---|---------|-----|--------|
| 35 | Low-severity cleanup | TD-064, 063b | priority:low, tech-debt, auto-audit |
| 36 | Developer experience & enhancements | TD-080, 081, 082, 085, 088 | enhancement, auto-audit |
