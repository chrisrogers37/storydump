> **⛔ SUPERSEDED — DO NOT IMPLEMENT FROM THIS DOCUMENT.** The authoritative plan is [`../2026-08-02-consolidated-design-plan/`](../2026-08-02-consolidated-design-plan/README.md); every increment, gate, and shape this file proposed is restated or struck there. Retained only as historical input and evidence for the reviews that cite it.

# Independent Architecture Evaluation Prompt

**Purpose:** Portable prompt for an independent Fable review  
**Design date:** 2026-07-29  
**Repository baseline:** `main` at `683f7cf`  
**Status:** READY FOR INDEPENDENT REVIEW

Copy the prompt below into a new review session. Attach the repository and the
design documents, or paste the design in place of `<DESIGN>`.

---

## Prompt

You are an independent principal distributed-systems reviewer. Evaluate the
proposed high-throughput, multi-tenant Storydump architecture against the actual
repository. Do not implement it and do not mutate production systems,
infrastructure, schedules, queues, posting history, or provider state.

### Inputs

- Repository: `<REPOSITORY OR ARCHIVE>`
- Baseline commit: `<BASELINE COMMIT>` (expected: `683f7cf`)
- Design: `<DESIGN OR PATH TO epic.md>`
- Existing system review, if present:
  `documentation/planning/2026-07-system-review/`

Storydump is a Python/FastAPI application deployed on Railway with Neon
PostgreSQL and Telegram, Meta, Google Drive, and Cloudinary integrations. The
design selects PostgreSQL as the authoritative command/job/effect store and
Redis as a non-authoritative admission and work-notification layer.

### Review rules

1. Verify repository claims in source, tests, migrations, configuration, and
   dependency pins. Cite paths and symbols; add line ranges only when stable.
2. Clearly distinguish:
   - current behavior proven by code or tests;
   - current behavior asserted only by documentation;
   - target behavior proposed by the design;
   - external-provider behavior that must be re-verified.
3. Do not accept “exactly once” language without identifying the uniqueness,
   conditional transition, provider anchor, ambiguity policy, and crash window
   that make replay harmless.
4. Treat process memory, Redis, broker delivery, and network responses as
   fallible. PostgreSQL is the only authoritative accepted-work record in the
   proposed design.
5. Test tenant isolation from a missing-context and hostile cross-tenant caller
   perspective. Account for PostgreSQL owner and `BYPASSRLS` behavior.
6. Account for Neon's transaction-mode PgBouncer. Reject correctness that
   depends on session affinity, session advisory locks, `LISTEN/NOTIFY`, or
   session-local state surviving a transaction.
7. Review rate limits at each provider's actual scope. Do not add tenant IDs to
   globally shared budgets or combine genuinely independent tenant budgets.
8. Treat cancellation as cooperative once an external call may be in flight.
9. Check that Redis message loss after an outbox row is marked published is
   recoverable from PostgreSQL, including delayed jobs still in `waiting`.
10. Check that Telegram webhook receipt and callback-query acknowledgement are
    not conflated. A successful webhook response does not inherently stop a
    callback spinner unless `answerCallbackQuery` is invoked.
11. Challenge fairness claims: Redis Streams consumer groups distribute work but
    do not by themselves implement weighted tenant fairness or priority.
12. Challenge capacity claims with explicit bounds: process concurrency,
    tenant/provider concurrency, DB pool sum across replicas, thread offload,
    prefetch, retries, and temporary-file/memory use.
13. Re-verify volatile platform/provider claims against first-party sources
    current at review time. Mark anything that cannot be verified.
14. Prefer fail-safe ambiguity over availability for externally visible publish
    effects. Do not recommend blind retry.
15. Require idempotency keys to be provider-account/command-namespace scoped and
    fingerprint checked; key reuse with different semantics must conflict.
16. Require shadow work to be immutably non-executable. A later global dispatch
    flag must never activate a historical shadow job whose legacy effect already
    occurred.
17. Inspect non-transactional migration phases and postconditions; do not assume
    every migration can atomically update its version record.
18. Do not claim a database lease can fence a network request after its final
    pre-send check. Require a one-shot effect permit so successors reconcile
    rather than issue a second call.
19. Ensure poison outbox/job handling removes the parent job from every recovery
    scan and lease query; otherwise “recovery” can regenerate poison forever.

### Required analysis

#### A. Define the problem from first principles

State the actual user/system outcome, irreducible constraints, inherited
conventions, and assumptions introduced by the target architecture. Classify
each major assumption as:

- `fundamental`;
- `repository-proven`;
- `externally verified`;
- `plausible but unproven`;
- `unsafe/incorrect`.

#### B. Verify the current-system evidence

At minimum inspect:

- `src/services/core/telegram_operation_state.py`;
- `src/repositories/queue_repository.py`;
- `src/api/rate_limit.py` and `src/api/app.py`;
- `src/services/core/telegram_service.py`;
- scheduler and media-sync loops;
- `src/main.py`;
- repository session/unit-of-work patterns;
- tenant filter helpers and nullable tenant columns;
- posting history idempotency and Meta container persistence;
- tests for concurrent claims, ambiguity, and tenant scope;
- migrations versus ORM-created test schemas;
- current observability and health signals.

For each significant design evidence claim, report `verified`, `partially
verified`, `stale`, or `contradicted`, with evidence and consequence.

#### C. Stress the target architecture

Walk these boundaries one by one:

1. authenticate → rate-limit → transaction → response;
2. command/job/outbox commit → broker publish;
3. broker read → PostgreSQL lease → provider call;
4. provider success/timeout → durable finalization;
5. lease expiry versus slow/partitioned worker;
6. cancellation versus an in-flight provider effect;
7. worker/Redis/PostgreSQL rolling restart;
8. one-tenant flood versus peer latency;
9. RLS context under transaction pooling;
10. poison event/job and operator-review completion.

For every gap, give a concrete failure sequence rather than a generic warning.

#### D. Evaluate migration safety

Check phase ordering, dual-write/shadow semantics, feature flags, data backfills,
constraint validation, role changes, dependency upgrades, canary criteria, and
rollback. A rollback must stop new routing without deleting durable evidence or
blindly resuming ambiguous provider operations.

#### E. Evaluate tests and observability

Determine whether the proposed tests can prove the claims. Require migrated
PostgreSQL, separate processes/connections, runtime RLS roles, Redis restart/loss,
worker kills at state boundaries, provider fakes, deterministic time, and
bounded-cardinality telemetry. Identify SLOs that lack an unambiguous measurement
point.

### Required output

Use this exact structure:

1. **Executive verdict** — one of `approve`, `approve with required changes`, or
   `reject`, followed by the minimum rationale.
2. **Scorecard** — 0–5 for correctness, durability, tenant isolation, fairness,
   capacity control, security, operability, migration safety, and testability.
3. **Fundamentals and assumptions** — classification table.
4. **Repository evidence audit** — claim, verdict, evidence, consequence.
5. **Failure-window analysis** — ordered failure sequences and required invariant.
6. **Tenant/security analysis** — authorization, RLS, credentials, egress, audit.
7. **Capacity/fairness analysis** — explicit bounds and missing capacity math.
8. **Migration analysis** — ordering, cutover gates, and rollback hazards.
9. **Required design changes** — P0 correctness blockers, P1 pre-cutover
   requirements, P2 follow-ups. Do not inflate ordinary implementation tasks into
   P0.
10. **Acceptance-test matrix** — scenario, injection, expected state/effect, and
    proof signal.
11. **Open decisions** — only choices that materially alter correctness or schema.
12. **Final recommendation** — what can be approved now and what remains gated.

Be direct. Do not validate the proposal merely because it is detailed. Prefer a
smaller enforceable contract over broad aspirational language. If the evidence
does not support a claim, say so.

---

The repository-specific answer produced with this prompt is
[`self-evaluation.md`](self-evaluation.md).
