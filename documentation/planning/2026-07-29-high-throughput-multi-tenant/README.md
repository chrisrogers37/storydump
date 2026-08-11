> **⛔ SUPERSEDED PACKAGE — DO NOT IMPLEMENT FROM ANY DOCUMENT IN THIS DIRECTORY.**
> The authoritative plan is [`../2026-08-02-consolidated-design-plan/`](../2026-08-02-consolidated-design-plan/README.md) — start at its README.
> This package (#722) is one of the historical inputs that plan consolidated; a formal cross-check found it incompatible with the #721 data-model package as written, and the consolidated plan adjudicated every conflict (its `03-decision-record.md` records what survived, what lost, and why). It is retained in-tree only because the design reviews cite it — the evidence trail matters; the content does not govern anything.
> One exception lives here: `review-findings.md` (#730, ratified) is honored — not superseded — and carries its own banner.

# High-Throughput Multi-Tenant Architecture

**Date:** 2026-07-29  
**Baseline:** `main` at `683f7cf`  
**Status:** SUPERSEDED by `../2026-08-02-consolidated-design-plan/` (2026-08-02) — historical input, retained as evidence
**Scope:** Architecture and migration design only

This session defines how Storydump can evolve from a single polling worker into a
durable, horizontally scalable system without changing the product workflow or
weakening its posting safety rules. It does not authorize production posting,
database migration, Redis or Railway provisioning, webhook cutover, or any other
infrastructure change.

## Decision summary

Use an evolutionary multi-service architecture:

- PostgreSQL remains authoritative for accepted commands, jobs, external-operation
  anchors, outcomes, and audit history.
- Redis provides shared admission budgets and low-latency work notification, but
  is never the only record of accepted work.
- Telegram webhook ingress and the existing API durably admit work in short
  transactions and return an operation identifier.
- Independently scalable command, publish, sync, and maintenance workers execute
  leased PostgreSQL jobs with bounded concurrency.
- Tenant ownership becomes mandatory in interfaces and schema, with PostgreSQL
  Row-Level Security as defense in depth.
- The current Meta claim-before-publish behavior remains the safety baseline:
  ambiguous publishes are reconciled or held for review, never blindly retried.

The review found the direction sound with eight required clarifications, now made
normative in the epic and plan:

1. A Redis message is a wake-up hint, not proof that a PostgreSQL job is delivered.
   A generation-based database sweeper must promote due waiting work and recover
   ready jobs even when a stream entry is lost or acknowledged incorrectly.
2. Telegram callback queries need a reserved fast acknowledgement path. Webhook
   HTTP success alone does not stop the user's button spinner.
3. Authentication and coarse abuse checks precede a fingerprint-checked
   idempotency lookup and durable command admission; PostgreSQL commit precedes
   `202 Accepted`; broker publication follows commit.
4. Cancellation is a durable request and state transition. Lease loss can fence
   new effects/finalization but cannot interrupt a third-party call already issued.
5. Priority and weighted tenant fairness are explicit dispatcher behavior; Redis
   Streams consumer groups alone do not provide tenant fairness.
6. RLS tenant context is set with `SET LOCAL` inside every transaction that
   touches tenant-owned data, so it remains compatible with Neon's
   transaction-mode PgBouncer.
7. Shadow work is immutably non-executable; enabling live dispatch can never make
   historical shadow rows repeat effects already performed by the legacy path.
8. Migration replay models non-transactional phases and verifies postconditions
   instead of assuming every version stamp is atomic.

## Documents

| Document | Purpose |
|---|---|
| [`epic.md`](epic.md) | Outcome, invariants, target architecture, data model, rollout, and acceptance criteria |
| [`self-evaluation.md`](self-evaluation.md) | Evidence-based review of the design against this repository |
| [`fable-evaluation-prompt.md`](fable-evaluation-prompt.md) | Portable prompt for an independent architecture review |
| [`tiered-issue-triage.md`](tiered-issue-triage.md) | P0–P3 work packages, dependencies, and exit gates |
| [`implementation-plan.md`](implementation-plan.md) | File-oriented, test-first execution sequence |
| [`review-findings.md`](review-findings.md) | Independent-review outcome: five required changes, the capacity-envelope correction, and the Instagram platform constraints |

Recommended review order: this index, `self-evaluation.md`, `epic.md`,
`tiered-issue-triage.md`, then `implementation-plan.md`.

## Non-negotiable invariants

- No command is reported as accepted until its authoritative PostgreSQL record
  commits.
- Every admitted operation reaches a terminal or operator-review state.
- Replays and concurrent claims cannot produce a second domain effect.
- A Meta response that may have published is never treated as safe to retry.
- Ordinary runtime access cannot reach tenant-owned data without an explicit
  tenant context; only named, separately authorized maintenance interfaces may
  perform deployment-global access.
- Redis loss may delay work but cannot erase accepted work.
- Bulk sync cannot consume capacity reserved for acknowledgement, cancellation, or
  reconciliation.
- Deployment scaling cannot silently increase provider concurrency or the global
  database connection budget.

## Repository evidence and prior work

- [`../2026-07-system-review/triage-tracker.md`](../2026-07-system-review/triage-tracker.md)
  documents optional tenant scoping, process-local coordination, schema drift, and
  session-lifetime issues.
- [`../per-request-session-isolation.md`](../per-request-session-isolation.md) is
  partly superseded: bounded Telegram concurrency and `ContextVar` session
  isolation have shipped, but explicit async unit-of-work scopes have not.
- [`../../operations/2026-05-telegram-delivery-burst-postmortem.md`](../../operations/2026-05-telegram-delivery-burst-postmortem.md)
  records the operational cost of unbounded or opaque Telegram delivery failure.
- The baseline implementation is pinned by `requirements.txt` to
  python-telegram-bot 22.7, SlowAPI 0.1.9, SQLAlchemy 2.0.49, and
  psycopg2-binary 2.9.12; these are migration inputs, not target constraints.

## Review outcome required

Final approval should confirm the architecture contracts and priority ordering,
not production rollout. Each implementation phase still requires its own schema,
dependency, deployment, and rollback review. Until then, this set remains
documentation only.
