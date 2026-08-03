# Consolidated design plan — multi-tenant storydump (2026-08-02)

**Status:** proposed — awaiting human ratification. Documentation only; no runtime behavior changes in this PR. Second design pass applied (Codex reviews A/B on this PR: executable DDL + database-enforced invariants, multi-account semantics, effect/reconciliation contracts, corrected sequencing). **One fork is open for the product owner: PA-1 in `03` (provider-account identity across workspaces); implementers build its default (a) until ruled.**
**Supersedes:** the standalone adoption of either prior package (see Authority, below).
**Executable by:** an implementer working increment-by-increment from `04-execution-sequence.md` with zero design judgment calls — every shape, key, state, number, and gate is stated here or explicitly incorporated by path.

## What this is

Three design efforts targeted the same future system and disagreed: the data-model package (#721), the architecture package (#722, in-tree at `documentation/planning/2026-07-29-high-throughput-multi-tenant/`), and an independently-derived cold design (fleet-side). A cross-check adjudicated them (verdict: incompatible as written — 5 blocking conflicts), the ratified review findings (#730, `review-findings.md` in the same package dir) amended #722, and the product owner issued rulings (2026-08-02) that fix the tenancy model, the interaction-layer direction, the media-credential model, and the Instagram auth path.

This plan is the single consolidated output. Its spine is the cold design — the only input whose every default traces to a stated requirement — with #722's engineering and #721's domain-model direction pulled in where they earn their place, and the 2026-08-02 rulings applied as fixed constraints.

## Inputs and provenance

| Input | Where | What survives here |
|---|---|---|
| Product rulings 2026-08-02 | `00-fixed-constraints.md` (normative restatement) | All of it — FC-0..FC-4 are not revisable by implementers |
| Envelope ruling (prior) | #730 discussion | Thousands of provisioned tenants; sets C2 topology and all numbers |
| #722 architecture package | `../2026-07-29-high-throughput-multi-tenant/` (`epic.md`, `implementation-plan.md`, `self-evaluation.md`, `tiered-issue-triage.md`) | Jobs/leases/outbox semantics, webhook ingress, RLS harness discipline, P0 exit-gate rigor — as re-based by `04` |
| #730 ratified findings | `../2026-07-29-high-throughput-multi-tenant/review-findings.md` | R1–R5 all honored; mapping noted per decision in `03` |
| #721 data-model package | PR #721 (not in-tree) | Intent-ledger direction, workspace-rooted tenancy direction, six-stage migration machine, consumer-contract track — letter re-derived (24 recorded quality flags) |
| Cold design | fleet-side working set; its requirement set R1–R8 / T1–T4 / H1–H6 is restated in full in `01` §Requirements ledger (the only normative home) | The requirements ledger and the overall shape of `01`/`02` |
| IG platform reference | fleet vault: "Instagram Content Publishing API — limits, account requirements and App Review (2026)" + 2026-08-02 correction addendum | 25/rolling-24h publish cap, 200/user/hr, Instagram-Login-vs-Facebook-Login split, App Review lead times |

## File map

- `00-fixed-constraints.md` — product-owner rulings as normative requirements. Read first; nothing below may contradict them.
- `01-target-architecture.md` — the requirements ledger (R/T/H ids every other file cites), process roles, interaction-layer port, tenancy spine, job machinery.
- `02-domain-model.md` — full schema, state machines with complete transition matrices and failure terminals, uniqueness keys, RLS.
- `03-decision-record.md` — every contested decision: what was decided, the requirement it serves, what it supersedes, reversibility.
- `04-execution-sequence.md` — the single consolidated increment sequence (supersedes both packages' phase plans), with exit gates and traceability to #722's P0/P1 items and #730's Rs.
- `05-operational-numbers.md` — initial values for every operational setting (the package-level gap both priors left open), with derivations, retention/DR tables, and the revision rule.
- `06-product-lifecycles.md` — workspace/membership lifecycles, multi-account scheduling semantics, account movement, customer-visible failure behavior, named non-goals.
- `07-security-model.md` — web sign-in (OTP + sessions), OAuth state binding, credential encryption + key rotation, audit integrity, oracle/log hygiene, first-party API auth.

## Authority and supersession

Where this plan and the 2026-07-29 package disagree, **this plan wins**. The package remains in-tree as reference detail; `04-execution-sequence.md` states, per increment, which package items are incorporated (and at what amendment) and which are superseded. The ratified #730 findings are honored in full; each R is anchored to its consolidated home in `03-decision-record.md`. #721 is not in-tree; everything this plan takes from it is restated here in full, so no implementer needs to read #721.
