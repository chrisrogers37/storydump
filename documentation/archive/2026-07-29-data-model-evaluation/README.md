> **⛔ SUPERSEDED PACKAGE — DO NOT IMPLEMENT FROM ANY DOCUMENT IN THIS DIRECTORY.**
> The authoritative plan is [`../../planning/2026-08-02-consolidated-design-plan/`](../../planning/2026-08-02-consolidated-design-plan/README.md) — start at its README.
> This package (#721's data-model evaluation, merged in-tree 2026-08-10) is one of the three inputs that plan consolidated. Its intent-ledger and workspace-rooted tenancy direction survive there (`03-decision-record.md` §"#721 content carried forward"); its strangler/six-stage migration machine was struck by FC-7 (offline cutover) and its Alembic track by C6. Retained in-tree only as the evidence trail. Archived 2026-09-02.

# Data Model Evaluation Package — 2026-07-29

**Repository baseline:** `main` at `683f7cf` (2026-07-29)
**Scope:** Documentation and planning only. No production schema change, no application
behavior change, and no command in this package triggers Instagram or Telegram posting.

---

## Purpose

This package answers two separate questions, in a deliberately separated way:

1. **What system does Storydump implement today, and what data model supports it?**
   This must be independently answerable — a reader (human or agent) should be able to
   reconstruct the as-built system without being steered toward any redesign.
2. **If the same product were designed today, what data model should replace or evolve
   the current one, and how could the running application migrate safely?**

To keep the two questions separated:

- `01-fable-system-evaluation-prompt.md` is a **neutral, reusable prompt**. It does not
  contain, hint at, or pre-select this session's recommendation. It can be handed to any
  capable evaluator (another Fable session, another model, or a human architect) to
  produce an independent evaluation from repository evidence.
- `02-self-evaluation.md` is **this session's answer** to that same prompt, grounded in
  file-level evidence from the baseline commit.
- Documents `03`–`07` develop this session's recommendation and its delivery plan. They
  are downstream of the evaluation, not inputs to it.

## Documents and Reading Order

| # | Document | What it contains |
|---|----------|------------------|
| 0 | [README.md](README.md) | This index: scope, baseline, reading order |
| 1 | [01-fable-system-evaluation-prompt.md](01-fable-system-evaluation-prompt.md) | Neutral, reusable evaluation prompt (no recommendation leakage) |
| 2 | [02-self-evaluation.md](02-self-evaluation.md) | This session's evaluation: system reconstruction, schema inventory, path traces, liabilities, comparison of three target approaches, recommendation |
| 3 | [03-recommended-target-model.md](03-recommended-target-model.md) | Deep treatment of the recommended target model: entities, boundaries, invariants, non-goals |
| 4 | [04-epic.md](04-epic.md) | Implementation epic: outcomes, acceptance criteria, sequencing, dependencies, risks, exit conditions |
| 5 | [05-tiered-issue-triage.md](05-tiered-issue-triage.md) | Work split into P0–P3 tiers, reconciled with existing GitHub issues |
| 6 | [06-migration-and-consumer-plan.md](06-migration-and-consumer-plan.md) | Expand/backfill/dual-write/shadow-read/cutover/contract plan covering every consumer, plus rollback |
| 7 | [07-evidence-map.md](07-evidence-map.md) | Compact map from conclusions to files, migrations, tests, and issues |

**Recommended reading order:**

- To evaluate independently (or re-run the evaluation later): read `01` only, produce
  your own answer, then compare against `02`.
- To review this session's work: `02` → `03` → `04` → `05` → `06`, with `07` open
  alongside as the citation index.
- To act on the plan: `04` (epic) and `05` (triage) are the operational entry points;
  `06` is the migration playbook each increment must follow.

## Baseline Facts

- Baseline commit: `683f7cf` — `chore: gitignore fleet bot telemetry droppings (#720)`.
- Schema at baseline: 15 SQLAlchemy models (`src/models/`), a SQL-only
  `schema_version` table, and a Drizzle-managed `waitlist_signups` table owned by the
  landing site (`landing/src/lib/schema.ts`).
- Migrations at baseline: 49 hand-numbered SQL files,
  `scripts/migrations/001_add_category_column.sql` through
  `scripts/migrations/049_inv1_delivered_requires_stamp.sql`, applied manually with
  `psql` (no Alembic runner, no deploy-time migration step).
- Open GitHub issues at time of writing: 265 (numbered up to #719). Issue references in
  this package were checked against that snapshot.
- Prior related work this package builds on (and does not duplicate):
  - `documentation/archive/2026-07-system-review/` — the 2026-07-02 full-system
    review (91 findings organized under 5 epics, filed as 36 GitHub issues; see its
    `issues/README.md`).
  - Issue #692 — the enum-SSOT + queue delivery-state implementation plan, partially
    shipped as migrations `045`–`049`.
  - `documentation/archive/2026-05-18-instagram-credential-refactor.md` — the
    additive → dual-write → cutover credential migration (migrations `035`–`041`),
    which is the in-repo precedent for the migration style this package proposes.

## Safety Rules Honored by This Package

This system posts to Instagram and Telegram. Nothing in this package — including the
verification steps in each document and the evaluation prompt itself — runs, requires,
or recommends running:

- `storydump-cli` mutating commands (`reset-queue`, `instagram-auth`, `promote-user`,
  `dedup-media --apply`, etc.)
- `python -m src.main` (the worker/scheduler)
- Any Telegram Web interaction

Verification in this package is limited to reading code, running `pytest`, `ruff`, and
read-only `storydump-cli` inspection commands (see the safe list in `CLAUDE.md`).

## Status Conventions

Each analytical document tags its claims:

- **Observed** — verified directly in the baseline tree (file cited).
- **Inferred** — a conclusion that follows from observations but is not itself a single
  file fact (reasoning stated).
- **Recommended** — a design choice this session proposes; always downstream of the
  observed/inferred sections.

Last verified against baseline: 2026-07-29.
