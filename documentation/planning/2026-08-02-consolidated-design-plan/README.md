# Consolidated design plan — multi-tenant storydump (2026-08-02)

**Status:** proposed — awaiting human ratification. Documentation only; no runtime behavior changes in this PR. Second design pass applied (Codex reviews A/B on this PR: executable DDL + database-enforced invariants, multi-account semantics, effect/reconciliation contracts, corrected sequencing). **One fork is open for the product owner: PA-1 in `03` (provider-account identity across workspaces); implementers build its default (a) until ruled.**
**Supersedes:** the standalone adoption of either prior package (see Authority, below).
**Executable by:** an implementer working increment-by-increment from `04-execution-sequence.md` with zero design judgment calls — every shape, key, state, number, and gate is stated here or explicitly incorporated by path.

## Start here — authority, reading order, self-containment

**This directory is the authoritative plan.** Everything else under `documentation/planning/` that addresses multi-tenancy — in particular `../2026-07-29-high-throughput-multi-tenant/` (#722's package, of similar vintage) — is **historical input, superseded by this plan**; each of its files carries a banner saying so. Do not implement from anything outside this directory.

**Reading order:** `00` (fixed constraints — nothing below may contradict them) → `01` (requirements ledger + architecture) → `02` (schema + state machines, executable DDL) → `03` (decision record — why, and what lost) → `04` (the increment sequence you execute) → `05` (every operational number) → `06` (product lifecycles + multi-account semantics) → `07` (security model). An implementer works increment-by-increment from `04`, reaching into the others as it cites them.

**Self-containment guarantee:** these nine files plus this repository's own code and migrations are sufficient — no external document is required to execute this plan. Citations to #721, #722 (EP/IP/SE/TT), #730 (RF-*), the fleet-side cold design, and review comments are **provenance for auditors**: every requirement, shape, number, gate, and absorbed exit criterion is restated in full where it is used. #721 is not even in this repository, and does not need to be. The one open item an implementer must know: **fork PA-1 (`03`) awaits the product owner; build its default (a) until ruled.**

## What this is

Three design efforts targeted the same future system and disagreed: the data-model package (#721), the architecture package (#722, in-tree at `documentation/planning/2026-07-29-high-throughput-multi-tenant/`), and an independently-derived cold design (fleet-side). A cross-check adjudicated them (verdict: incompatible as written — 5 blocking conflicts at that check's grain; review A's A–P inventory is the same incompatibility counted finer, `03` count-grain note), the ratified review findings (#730, `review-findings.md` in the same package dir) amended #722, and the product owner issued rulings (2026-08-02) that fix the tenancy model, the interaction-layer direction, the media-credential model, and the Instagram auth path.

This plan is the single consolidated output. Its spine is the cold design — the only input whose every default traces to a stated requirement — with #722's engineering and #721's domain-model direction pulled in where they earn their place, and the 2026-08-02 rulings applied as fixed constraints.

## Inputs and provenance

| Input | Where | What survives here |
|---|---|---|
| Product rulings 2026-08-02 | `00-fixed-constraints.md` (normative restatement) | All of it — FC-0..FC-4 are not revisable by implementers |
| Envelope ruling (prior) | #730 discussion | Thousands of provisioned tenants; sets C2 topology and all numbers |
| #722 architecture package | `../2026-07-29-high-throughput-multi-tenant/` (`epic.md`, `implementation-plan.md`, `self-evaluation.md`, `tiered-issue-triage.md`) | Jobs/leases/outbox semantics, webhook ingress, RLS harness discipline, P0 exit-gate rigor — as re-based by `04` |
| #730 ratified findings | `../2026-07-29-high-throughput-multi-tenant/review-findings.md` | R1–R5 all honored; mapping noted per decision in `03` |
| #721 data-model package | PR #721 (not in-tree) | Intent-ledger direction, workspace-rooted tenancy direction, six-stage migration machine, consumer-contract track — letter re-derived (24 recorded quality flags) |
| Cold design | fleet-side working set (externally held, **not required reading**); its requirement set R1–R8 / T1–T4 / H1–H6 is restated in full in `01` §Requirements ledger (the only normative home) | The requirements ledger and the overall shape of `01`/`02` |
| IG platform reference | fleet vault doc (externally held, **not required reading** — every platform fact it contributed is restated in `05` and re-verified against Meta's primary documentation at `04` 0.4) | 25/rolling-24h publish cap, 200/user/hr, Instagram-Login-vs-Facebook-Login split, App Review lead times |

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

The 2026-07-29 package is **superseded in full** — where it and this plan say different things, this plan is not merely preferred, it is the only authority. The package remains in-tree solely as the evidence trail (the reviews cite it); every file in it carries a supersession banner. `04-execution-sequence.md` names, per increment, which package items were absorbed — as audit annotations, with the absorbed scope and gates restated in full in `04` itself. The ratified #730 findings are honored in full; each R is anchored to its consolidated home in `03-decision-record.md` (its file lives in the superseded directory but is itself *honored*, not superseded — its banner says exactly that). #721 is not in-tree and does not need to be: everything load-bearing was restated here (verified — its five load-bearing quality flags resolve to FLAG-1/2/3/7/10 fixes named in `02`/`03`; nothing else was depended on).
