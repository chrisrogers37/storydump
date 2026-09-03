> **⛔ SUPERSEDED** — part of the 2026-07-29 data-model package; the authoritative plan is [`../../planning/2026-08-02-consolidated-design-plan/`](../../planning/2026-08-02-consolidated-design-plan/README.md). See this directory's README for what survived. Archived 2026-09-02.

# Prompt: Evaluate Storydump's System and Data Model

**Type:** Reusable evaluation prompt.
**Neutrality:** This prompt intentionally contains no preferred answer. It asks for a
reconstruction, an evaluation, a comparison, and only then a recommendation. If you
find language below that appears to pre-select a particular design, treat it as a
defect in the prompt and note it in your output.

---

You are evaluating the repository you have been given (Storydump). Work only from
repository evidence: source code, migrations, tests, configuration, documentation, and
the project's GitHub issues. Where you cannot establish a fact from the repository,
say so explicitly rather than guessing.

## Safety rules (non-negotiable)

This system posts to Instagram and to Telegram group chats. While evaluating:

- Do **not** run the worker (`python -m src.main`) or the API against production
  configuration.
- Do **not** run any mutating or posting-capable command. In particular never run
  `storydump-cli reset-queue`, `storydump-cli instagram-auth`, or any command the
  repository's `CLAUDE.md` marks unsafe.
- You **may** run: `pytest`, `ruff`, and the read-only `storydump-cli` inspection
  commands listed in `CLAUDE.md` (`list-queue`, `list-media`, `check-health`, etc.)
  if an environment is available. None of your validation steps may cause a post to
  be published or a Telegram message to be sent.
- Reading any file is always safe.

## Ground rules for your output

1. Label every material claim **Observed** (cite the file, and the migration or test
   where relevant), **Inferred** (state the reasoning), or **Recommended**.
2. Cite existing GitHub issues where they already describe a problem you found;
   do not present known issues as new discoveries.
3. Complete Parts 1–4 before drafting any redesign. Your recommendation in Part 6 must
   be justified by the comparison in Part 5, not asserted first and defended after.
4. State your confidence when evidence is thin (e.g. behavior only exercised in
   production, not covered by tests).

## Part 1 — Reconstruct the system as built

Answer, with citations, before evaluating anything:

1. **Product purpose.** What does this product do for whom? What is the core workflow
   from a piece of media entering the system to a story appearing on Instagram?
2. **Actors.** Who or what acts on the system (end users, team roles, operators,
   background processes, external services)? How is each authenticated?
3. **Tenant boundary.** What is the unit of isolation between one customer/team and
   another, as actually implemented? Which tables, code paths, and auth checks define
   it? Where is it enforced and where is it assumed?
4. **Workflows.** Describe the scheduling, approval, posting, media ingestion,
   onboarding, and account-connection workflows as state machines where applicable.
5. **External integrations.** Enumerate integrations (messaging, social publishing,
   file storage, media CDN, analytics, auth providers) and what state each one
   requires the system to hold.
6. **Runtime topology.** What processes exist, where do they run, what loops or
   servers does each contain, and how do they share the database?
7. **Operational constraints.** How are schema changes applied to production? What
   does CI test, against what kind of database? What are the deploy, rollback, and
   observability mechanisms?

## Part 2 — Inventory the as-built data model

1. List every persisted table (including any tables owned by other codebases in the
   repository) with its purpose, key columns, constraints, and indexes.
2. For each important **concept** (tenant, person, membership, social account,
   credential, media file, scheduled post, posting outcome, configuration value,
   audit record), identify the **source of truth**. Flag concepts with more than one
   source of truth, denormalized copies, or values duplicated between the database
   and environment configuration.
3. Identify where the schema enforces correctness (NOT NULL, FKs, uniques, CHECKs,
   partial indexes) versus where correctness depends on application code remembering
   to do the right thing.
4. Describe how schema definitions, migrations, and test databases relate: is there a
   single mechanism that produces the production schema, the fresh-install schema,
   and the test schema? If not, characterize the drift precisely.

## Part 3 — Trace the important paths

Trace end-to-end, with file citations, at minimum:

1. A scheduled slot becoming a queue item, an approval card in Telegram, and a
   terminal outcome in history — including every transaction boundary and every
   point where a crash would leave partial state.
2. An Instagram API auto-post, from button press to recorded outcome, including
   claim, external calls, and finalization.
3. Media ingestion from an external source to an eligible, categorized media item.
4. An OAuth connection (Instagram and Google Drive): where credentials land, how they
   are encrypted, refreshed, revoked, and which tenant owns them.
5. A read path from each surface: the Telegram bot, the FastAPI Mini App/API, the
   Next.js dashboard (through its BFF), and the CLI — noting how each derives the
   tenant and whether any surface bypasses the service layer.

## Part 4 — Evaluate

With Parts 1–3 as evidence, state:

1. **Strengths** — what the current model gets right and should be preserved.
2. **Liabilities** — structural weaknesses, each tied to evidence and, where
   applicable, to an existing GitHub issue.
3. **Failure modes** — concrete sequences (crash timing, concurrent actions, retry
   storms, partial deploys) that lose data, duplicate external side effects, leak
   data across tenants, or wedge the system.
4. **Scaling constraints** — what breaks first as tenants, media volume, posting
   volume, or worker replicas grow.

## Part 5 — Compare target approaches

Define evaluation criteria first (at minimum: integrity and tenant isolation,
idempotency of external side effects, operability of schema change, evolvability
toward the product roadmap, migration risk to the running system, and implementation
cost). Then compare **at least three** credible approaches:

- **A. Incremental repair** — keep the current shape; fix constraints, tooling, and
  the worst write paths in place.
- **B. A cleaner redesign** — a target model you would design today for this product,
  reached by an incremental migration while the current application keeps running.
- **C. At least one more** — e.g. a full rewrite-and-cutover, a different persistence
  architecture, or a variant of A or B with materially different boundaries.

For each: describe the end state, what it fixes, what it deliberately does not fix,
the migration path, and the principal risks. Score against your criteria.

## Part 6 — Recommend

Only now, recommend one approach. Justify it against the comparison, list the
decisions that are reversible versus one-way doors, and state explicit non-goals to
bound the scope.

## Part 7 — Migration and validation strategy

For your recommended approach, provide a migration strategy that keeps the current
application safe and continuously available:

- No big-bang cutover; the legacy and target structures must be able to coexist.
- Every stage must be observable (reconciliation reports, comparison metrics) and
  reversible without destructive down-migrations.
- Cover every consumer of the data model you identified in Part 1 (background worker,
  API, Telegram surface, CLI, web dashboard and its auth/session types, analytics,
  tests/CI, deployment and database operations).
- Define validation gates: what must be measured, and at what threshold, before each
  stage advances — including how you validate without ever triggering a live post.
- State the rollback procedure for each stage.

## Output format

Produce a single document with sections matching Parts 1–7, an executive summary at
the top (written last), and an appendix mapping each material conclusion to its
evidence (files, migrations, tests, issues).
