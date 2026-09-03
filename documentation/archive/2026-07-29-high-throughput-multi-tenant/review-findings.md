> **📌 RATIFIED FINDINGS — honored in full, but implement from the consolidated plan.** These #730 findings amended the (now superseded) package in this directory; the findings themselves are NOT superseded — each R/G is anchored to its consolidated home in [`../../planning/2026-08-02-consolidated-design-plan/03-decision-record.md`](../../planning/2026-08-02-consolidated-design-plan/03-decision-record.md) (§#730 traceability). Do not implement from this directory.

# Review Findings and Envelope Correction

**Date:** 2026-08-02
**Baseline:** PR #722 at `4afd2fc`
**Status:** RATIFIED — required changes are gates on the packages named below
**Scope:** Records decisions already taken in review. No new analysis, no code, no schema.

This document records the outcome of the independent review of PR #722, so the findings
live with the design set rather than only in pull-request comments. It supersedes nothing;
it constrains `epic.md`, `tiered-issue-triage.md`, and `implementation-plan.md` at the
points named.

Review inputs: two independent reviews of the design ([architecture
review](https://github.com/chrisrogers37/storydump/pull/722#issuecomment-5159944879),
[complexity and transformation-path
review](https://github.com/chrisrogers37/storydump/pull/722#issuecomment-5159978715),
[envelope
analysis](https://github.com/chrisrogers37/storydump/pull/722#issuecomment-5160011876)), a
[cold first-principles
design](https://github.com/chrisrogers37/storydump/pull/722#issuecomment-5160009279)
produced without reading this plan, and a repository-accuracy pass that verified 29 of 30
claims the documents make about current source.

---

## 1. Required changes

In priority order. Each is a gate on the package named, not a suggestion.

### R1 — Bound the ready-job reconciler's aggregate emission — `P0-07`

**Top blocker.** The reconciler promotes due `waiting` work and re-emits wake-ups for
`ready` jobs with no live lease or recent wake-up. Its cooldown bounds re-emission **per
job**; the actual exposure is aggregate, and scales as `ready_depth / cooldown`.

The trigger predicate cannot distinguish *"the wake-up was lost"* from *"every worker is
busy."* Under saturation the second is true of nearly every ready job, so a healthy backlog
is read as mass wake-up loss: extra outbox rows, relay claims and stream entries, each
costing a no-op lease attempt against the database, arriving exactly when there is least
headroom. This also pushes the two signals the design uses to gate admission — oldest-ready
age and pool wait — the wrong way, from the inside.

Every drill specified for this package tests **loss**. None tests **saturation**.

**Required:**
- Add a per-pass aggregate emission bound, so recovery work is bounded by a configured
  constant rather than by backlog depth.
- Make the trigger account for worker availability, so a saturated-but-healthy backlog is
  not read as loss.
- Replace `wakeup_generation` and its uniqueness index with `last_wakeup_at` plus a
  conditional update (`UPDATE ... WHERE last_wakeup_at < now() - cooldown RETURNING`),
  which gives single-winner semantics among concurrent scanners in one column. Duplicate
  wake-ups are harmless by construction — the lease decides, a loser acks without effect —
  so the generation protocol defends an invariant that does not need defending, and does
  not bound the load that does matter.
- Add a saturation drill to the exit criteria alongside the two loss drills: deep ready
  backlog, zero message loss, workers pinned; assert emissions stay inside the stated bound.

Because ready depth scales with tenants, this is also the recovery path's scaling limit —
see §2.

### R2 — Choose a scale-free fair-dispatch algorithm before building one — `P1-04` / Open Decision 3

The relay *"[s]elects a bounded tenant quantum per pass and orders tenants by persisted
last dispatch"* — an `O(eligible tenants)` sort on every pass. `self-evaluation.md` §7
already scopes it: *"The weighted relay design is implementable at the initial
250-active-tenant envelope, but the exact selection query/algorithm needs an ADR and
deterministic tests."* It does not claim the design extends beyond that, and `P3-01` defers
the replacement to *"only if measurements show it is the bottleneck."*

At the real target (§2) that conditional no longer applies, and P3 is the wrong tier.

**The reason to act now is timing, not difficulty.** The algorithm is still unwritten —
Open Decision 3. Choosing a scale-free one today costs nothing: an indexed
virtual-finish or deficit-counter scan that touches only the head of the queue, or
sharding the relay by tenant hash across replicas. Building the relational weighted scan
first and replacing it at a few hundred tenants means rewriting the component every
dispatch flows through, under load, in production.

`P3-01` already requires the replacement to *"preserve the same observable fairness
contract"* — so the contract is scale-invariant and only the implementation is not. This
fix is contained: pick the algorithm with thousands in mind and keep everything else.

### R3 — Move RLS enforcement from Increment 18 to Increment 4

The plan designs RLS in test/audit mode at Increment 4 and does not **enforce** it until
Increment 18 — last, after webhook cutover (16) and the publish canary (17). Tenant
backfill is hedged the same way: Increment 3 measures legacy NULL behavior *"table by
table"* and backfills *"stop on ambiguous ownership."*

That risk model assumes a multi-tenant system performing a multi-tenant backfill.
This system is not that. `chat_settings` is documented as one record per deployment, so:

- The backfill is trivially safe now. Every NULL-owned row belongs to the single tenant;
  there is nothing for ownership to be ambiguous between.
- Enforcing RLS now cannot break cross-tenant behavior, because there is none to break.
  The blast radius of a wrong policy today is "one tenant sees nothing" — loud, immediate,
  trivially reversible.
- Both get monotonically harder from here. Every increment adds tables, writers, and rows
  before the ownership rules are enforced.

As written, the plan defers its cheapest-now/most-expensive-later work to the end and does
its most speculative work first. It also means RLS policies are validated only against
synthetic fixtures for fourteen increments and then go live underneath a system already
fully cut over — the worst possible moment to discover a policy bug.

**Required:** pull tenancy backfill, `NOT NULL`, and RLS *enforcement* forward to
Increments 3–5, as close behind the runtime-role work as it allows. Everything downstream
is then built on an enforced boundary rather than a promised one.

Two supporting constraints for the same package:

- Gate on **zero NULL-owned rows in every RLS-enabled table**, asserted immediately before
  policy enable and again after, and confirm each `NOT NULL` is `VALIDATE`d rather than
  left `NOT VALID`. Under the proposed policy a NULL-owned row is invisible to every
  tenant including its owner — the one RLS failure that fails *quiet* rather than *closed*,
  so it will not be caught by the denial tests.
- Keep RLS policies as **constant-expression comparisons**. Authorization binds to active
  membership; if membership resolution ever leaks into the policy as a subquery, per-row
  cost becomes a lookup and degrades non-linearly. Membership stays in the application
  layer.

### R4 — Justify the Redis tier and the command framework against a real number, or cut them — `P1-02` / `P1-04` / `P0-04`

Both are sized to the capacity envelope, and the envelope was never derived (§2). With a
real target now set, each needs to be justified against it or removed.

**The distinction that matters:** tenant count and request rate justify different things.

- **Fairness machinery and the relay** are justified by tenant count, and thousands of
  tenants strengthens that case — while making R2 acute.
- **Redis admission buckets** are justified by *sustained request rate*, not tenant count.
  The load-bearing figure is the envelope's **50 req/s sustained API**, which remains
  underived. The product's API surface is bursty-human (Mini-App dashboards, OAuth,
  commands), not sustained-machine. A larger tenant count does not by itself reach the rate
  that motivates Lua multi-bucket admission.

The plan's own text supports gating rather than sequencing: PostgreSQL-only coordination
*"remains the fallback correctness model"*, Increment 11's gate requires that
*"PostgreSQL tests still prove no accepted work depends solely on Redis"*, and every
Redis-loss drill must pass. The PostgreSQL-authoritative core must therefore be built and
proven regardless; the Redis tier is a latency and overload accelerator layered on top.
Increments 0–10 stand alone.

**Required:** derive the sustained request rate from product behavior or a growth plan and
record it. Then either justify Increments 11–12 against that number, or gate them on a
*measured* SLO breach rather than sequencing them as inevitable. The same test applies to
the eight-table generic command framework against roughly half a dozen real command types.

### R5 — Scope fairness machinery to the lanes where contention is physically possible — `P1-04`

Instagram caps API publishing at **25 posts per rolling 24 hours per account** (§4.2). That
cap changes what the publish lane can be asked to do, and it is a different reason to cut
than the envelope question in R4: not *"this load may not materialize"* but *"this load
cannot occur."*

The design already separates work into queue- and priority-specific streams with distinct
worker roles (command, publish, sync, maintenance). Apply the cap per lane:

- **Publish lane — fairness is unnecessary.** Weighted fair queuing exists to stop one
  tenant monopolizing a contended resource. A tenant cannot monopolize the publish lane,
  because the platform stops them at 25 per day. The acceptance test *"one tenant
  contributes 90% of work while peers remain within SLO"* is, for this lane, a test of a
  state the platform forbids. What the publish lane actually needs is the per-account
  serialization invariant the design already has (`Meta publish flows per Instagram
  account | 1`) plus deferral scheduling — not a weighted relay.
- **Sync and command lanes — fairness is load-bearing.** Neither is capped by Meta. A
  tenant with a large Drive library can genuinely flood sync, and command traffic is
  human-driven and bursty. This is where contention is real and where R2's algorithm choice
  matters.

**Required:** state explicitly which lanes the weighted-fairness machinery serves, and do
not build it for the publish lane. R2 still stands — the relay's `O(eligible tenants)` scan
applies across all lanes regardless — but the *semantics* it must implement narrow to the
uncapped lanes, which is a meaningful reduction in what has to be got right.

One interaction to design for rather than cut: because the cap is a **rolling** window,
publish jobs deferred by it sit in `waiting` for up to 24 hours, and their `available_at`
cannot be computed from a local clock (§4.2). The `waiting` → `ready` promotion path must
take its not-before time from the provider's reported usage, which the epic's SLO
definition already anticipates — *"`eligible_at` is the later of admission/`available_at`
and a durable external not-before time such as a provider reset."* Wire it to that endpoint
rather than to a counter.

---

## 2. Envelope correction

**The initial acceptance envelope — 1,000 provisioned tenants, 250 simultaneously active —
was never derived. The real target is thousands of tenants.**

`250` appears twice as a value in the capacity-contract table and every other occurrence in
the document set is that number echoed back as a load-test scenario. Nothing computes it,
and nothing imposes it: it is not a PostgreSQL connection limit, a Redis throughput figure,
or a Railway instance size backed into a tenant count. The design set concedes this —
capacity control scores 4/5 with the reason *"deployment-wide numbers are not yet
derived"*, and §7 opens *"the envelope is credible for an evolutionary design, but
implementation must derive these numbers before scaling."* The capacity arithmetic there
runs the other direction, constraining pool budget to fit under the Neon ceiling, and never
links it to a supportable tenant count.

**250 is therefore a sizing assumption, not a design ceiling.**

The `epic.md` wording *"[l]owering one requires an architecture decision record"* is
asymmetric — it guards against dropping the number but not against adopting one. Adopting a
capacity number should require the same justification as lowering it. Correct that line
when the envelope is restated.

### The architecture extends on hardware

The decisions that usually create a tenant ceiling were all made correctly, and this is the
substantive good news:

- **No connection-per-tenant.** Pools are per worker replica; tenants are rows.
- **No table-per-tenant or schema-per-tenant.** Row-level `tenant_id`; provisioning a new
  tenant is an INSERT.
- **Redis Streams are partitioned by queue and priority, not by tenant.** Stream-per-tenant
  with consumer groups and pending-entry lists would have been a wall at thousands.
- **RLS cost is per row scanned, not per tenant provisioned.** The policy expression is
  stable, folds to a constant comparison, and composes with the index on `tenant_id`.
  Provisioned tenant count does not affect query cost.
- **The schedule dispatcher pages with a persisted cursor**, and replicas are safe because
  uniqueness collapses duplicate discovery.
- `tenant_dispatch_state` is one row per tenant; Redis budget keys are per tenant with
  expiry. Both linear and cheap.

### The two exceptions that do not extend

1. **The fair relay's weighted tenant selection is `O(eligible tenants)` per pass**, and is
   explicitly self-scoped to the 250-tenant envelope. → **R2**.
2. **The ready-job reconciler is `O(ready jobs)`**, and ready depth scales with tenants ×
   jobs per tenant. At the real target the aggregate is an order of magnitude larger and
   arrives when the system is most saturated. → **R1**.

Nothing else in the coordination design is per-tenant in a way that requires rework.

### The ceiling that actually binds is not architectural

At thousands of tenants the binding constraint is **provider quota**, and the scope it is
measured at decides whether it scales. The epic's rule is right — budget labels *"match the
provider's actual shared scope rather than mechanically including a tenant"* — but the
consequence is unstated, and it differs by provider:

- **Instagram scales with tenants.** Both limits are per connected user or per account
  (§4), so each tenant brings its own quota rather than dividing a shared one. No ceiling
  here.
- **Google Drive and Cloudinary do not.** Drive quota is weighted units per Cloud project;
  Cloudinary caps per product environment. If every tenant runs through one project and one
  environment, per-tenant throughput is quota ÷ tenant count, and no amount of Railway or
  Neon hardware changes it.

So the shared-scope ceiling is real, but it applies to the *media pipeline*, not to
publishing. That is a product question — do tenants connect their own storage credentials,
or share ours? — and it is upstream of the sync-lane capacity numbers.

---

## 3. Independent convergence

**A cold first-principles design of the same system, derived without reading this plan,
converged on the correctness core. That is the strongest validation this design set has.**

Working from product requirements and current source alone, the independent design
independently produced: webhook ingress with a fast admission path and the callback
answered at ingress; **PostgreSQL as sole authority with conditional lease claims before
any effect**; at-least-once delivery plus idempotency keys and state machines, with
exactly-once explicitly refused as a transport claim; the Meta ambiguity discipline
(container persisted before publish, classify the response, never blind-retry, reconcile
from provider evidence, operator-review state); **per-Instagram-account publish
serialization enforced in PostgreSQL, with pacing explicitly not the serialization proof**;
RLS with transaction-local `set_config`, a non-owner runtime role, `NOT NULL` after
backfill, fail-closed repositories, and the PgBouncer caveat; a transactional outbox with a
reserved acknowledgement budget; fairness enforced before dispatch rather than assumed from
FIFO; and `O(due)` scheduling with unique business keys.

Because the two derivations were independent, this convergence is evidence that the
correctness core is **required by the product**, not stylistic preference. Increments 0–10
build essentially that core, and it should be treated as settled.

The divergences concentrated in exactly the two places §1 already flags — the Redis
coordination tier and the generic command platform — both sized to the undermined envelope.
That is consistent, and it is why R4 is framed as "justify against a real number or cut"
rather than as an objection.

### Two gaps the plan does not cover

Both are domain-level, both are demanded by the requirements, and neither is addressed by
the current package set.

**G1 — The queue/history terminal-record seam survives into the target architecture.**
Terminal outcome is *delete the queue row, insert a history row*, with `queue_item_id` as
an unconstrained UUID. `provider_operations` protects the *external* effect, which is the
more critical half, but the domain-level invariant "exactly one terminal record per
attempt" remains enforced by discipline rather than schema. The implementation plan retains
`posting_queue` and `posting_history` and states explicitly that `posting_queue` is not to
be reused as `jobs`, so the seam persists rather than being collapsed.

*Reconciliation:* `P0-03` does already require *"[d]efine the permanent business key for
posting finalization"* and *"[a]dd a validated unique constraint/index after duplicates are
resolved"*, gated behind remediating the known production duplicate groups. So the missing
uniqueness constraint is covered. What is **not** covered is the broader point: the
two-table terminal dance persists in the target design where a single durable intent state
machine would remove the seam entirely. Treat collapsing posting into one intent state
machine as the eventual simplification, and confirm `P0-03`'s business key closes the
uniqueness half in the meantime.

**G2 — `instagram_accounts` tenancy is unclassified.** The ownership contract says
*"[c]lassify every table as deployment-global, tenant-owned, or relationship/audit"*, but
the Increment-3 backfill inventory enumerates `media_items`, `posting_queue`,
`posting_history`, media locks, category mix, API tokens, memberships, and audit —
Instagram accounts are absent, and nothing else in the document set classifies them. Today
they are deployment-global identity with per-tenant selection, and the in-repo July system
review already tracks the consequences (global account listing; unenforced instance role).

If accounts stay global-with-selection, that boundary remains discipline-enforced while
everything around it becomes schema-enforced — precisely the asymmetry this work exists to
remove. **Required:** either classify Instagram accounts as tenant-owned in the Increment-3
inventory, or write down why global identity is load-bearing (Meta-side account reuse is the
plausible reason) and add the compensating constraint. This is a small change now and a
migration later.

---

## 4. Platform findings

Instagram imposes **two limits that point in opposite directions**, and holding both at once
is what determines the shape of the system. Call rate scales with users; publish volume is
hard-capped per account. Together they say: throughput comes from tenant *count*, never from
per-tenant rate.

### 4.1 Call rate scales with users — no structural ceiling

**This is what makes the rest of this work worth doing**, and it is the reason the platform
question was asked before the architecture question.

The full analysis — including why a per-user quota model and a fixed per-app cap lead to
opposite product conclusions — is recorded in the fleet vault at
`_shared/knowledge/platform-scaling-models-decide-product-viability-per-user-quota-vs-fixed-app-cap.md`
and is not duplicated here. In summary:

- Rate limits are **per connected user**, reported as roughly 200 calls per user per hour,
  with isolated pools and no documented cap on the number of connected users. Adding users
  adds quota. This is the opposite of a fixed per-app ceiling, where every new user divides
  a constant budget.
- **App Review is mandatory** and runs approximately 2–4 weeks per permission. This is a
  schedule dependency on the critical path, not a technical barrier — it needs to start
  early because it cannot be compressed later. *(Schedule status 2026-08-18: the mandatory
  half stands unchanged; the critical-path coupling was lifted by owner ruling — the M.3
  window no longer waits on approval, which now gates the post-cutover reconnect.
  `../../planning/2026-08-02-consolidated-design-plan/00-fixed-constraints.md` FC-7 §5, PR #838.)*
- **Only Professional accounts can be posted to.** A user on a personal account must
  convert to Professional and link a Facebook Page. This is onboarding friction that lands
  on the user, and it belongs in product planning rather than architecture.

### 4.2 Publish volume is hard-capped per account — a design constraint

**Instagram caps API publishing at 25 posts per rolling 24 hours, per account.** Details are
recorded in the fleet vault at
`_shared/knowledge/instagram-content-publishing-api-limits-account-requirements-and-app-review-2026.md`
and are not duplicated here. The properties that bear on the architecture:

- The window is **rolling and timestamp-based**, not a calendar-day reset. There is no
  midnight at which a tenant's budget refills.
- It is enforced on the **publish step** of the two-step create-container-then-publish flow,
  not on container creation. Error code 9 on exceed.
- It applies to **API publishing only** — not to the native app or Creator Studio. A user
  can post outside the API and the account's remaining budget still moves.

**What this means for the design.** "High throughput" on this platform cannot mean high
per-tenant rate. The publish path has a wall at 25/day/account that no amount of engineering
moves. Total publish throughput is `tenants × 25/day`, so the scarce resource is **tenants,
not capacity**, and a publish queue in this system never needs to sustain more than 25
publishes per day per tenant. At 1,000 tenants that is roughly 0.3 publishes per second
across the entire deployment.

This is a genuinely different reason to cut machinery than the envelope question in §2.
There, the argument is that a load may not materialize. Here, it cannot occur. Applied in
**R5**.

Two consequences worth stating explicitly:

- **Do not count posts locally.** Meta exposes an IG User Content Publishing Limit endpoint
  reporting current usage. A local counter drifts against a rolling window and, because the
  cap ignores non-API posts, cannot see activity the user performs in the app. Local
  counting is wrong in both directions. `epic.md` already reaches the right conclusion in
  its sources list — *"the live account response remains authoritative"* — but it is
  recorded there as a re-verification note rather than as a design requirement. Promote it:
  the provider's reported usage is the only admissible source of remaining publish budget.
- **The cap is per account, not per app**, so it scales with tenants rather than dividing
  among them. That is why §2 lists Instagram as extending on tenant count while Drive and
  Cloudinary do not.

> **Verification status.** The figures in §4.1 and §4.2 are **second-hand**, drawn from 2026
> developer guides, and are **pending confirmation against Meta primary documentation**.
> They are recorded as the current working understanding, not as verified fact. Confirm
> against first-party sources before any capacity contract, pricing model, or go-to-market
> commitment depends on them. `epic.md` already lists the Meta endpoints to re-verify at
> implementation; these checks belong with them.

The relationship to §2 is direct. Per-user quota removes the *platform* ceiling, and the
per-account publish cap means Instagram scales with tenant count rather than dividing a
shared budget — so neither Instagram limit is the thing that binds. What remains open is
provider quota at shared scope for **Drive and Cloudinary**, and that is settled by whether
tenants connect their own storage credentials.

---

## Status of the design set after this review

The architecture is sound and the direction is endorsed. The correctness core is
independently corroborated. The five required changes above are gates on their named
packages; none of them blocks this documentation set from being accepted, and none
requires a change of direction.

The one structural correction is R3: the plan's sequencing puts its cheapest and most
reversible safety work last. Doing tenancy and RLS while exactly one tenant exists is the
only moment a policy error is free to discover, and that moment is now.
