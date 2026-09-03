# M.2 rehearsal spec — the window, proven on a branch

> **Status (2026-09-02): NOT EXECUTED.** The window's steps 3a–3d were applied to production by hand as `neondb_owner` across 2026-08-24 and 2026-08-26 (#1195, #1014) without this rehearsal (`00` FC-7 §7); 3e is abandoned (M.1 spec), and 3f, 3g and the step-8 stand-down have not run. The success/abandon stand-down legs (§4–§5) still govern 3g when it is scheduled, which is why this spec is retained.

**Status:** draft for review (#790 M.2). Planning artifact only — no implementation, no branch created, nothing executed. Same discipline as the M.1 spec (merged, #827/#830): everything ruled is made executable; every open input is **marked, with per-option deltas, and picked by nobody here**.

**Scope:** `04` Phase M **M.2** (L91) made executable as a rehearsal protocol: branch production via PITR → the step-0 bootstrap under the **real Neon project-owner role** → the full M.3 step-3 sequence (3a–3g, file order, all postconditions green, every step as `svc_migration`) → the step-8 **success** stand-down with its identity-gated gate → the target smoke battery against the branch → timed, logged, repeated until clean — plus the three separate legs: **rollback-and-re-entry**, **abandon**, and **guard-refusal**. It is also, by `05` §DR's own row, **the first DR drill** ("the M.2 rehearsal IS the first drill"), which adds two obligations a reading of `04` alone misses (§4 L5).

**What this spec does NOT decide:** Fork A (inherited at 3e — §5); the plan-author-ruling destination (unresolved, M.1 spec §3.8 — inherited by this spec's escalation routes); the Neon managed-role **verdict itself** (§3 defines the measurement; the outcome is the branch's to give); the parity-bar doc's two open forks (adjacent, gate M.3's opening, not M.2's gate — §6); FC-7's window budget (days-scale by owner ruling, no ratified number exists — §7 records against it, does not invent one).

**Sources of truth** (at `origin/main` `65f134e`): `04-execution-sequence.md` L91 (M.2 verbatim + gate), L93–142 (bootstrap, printed in full), L145–151 (3a–3g), L152–159 (rollback lever, D41), L160–260 (step-8 both variants + gates, printed in full), L262 (M.3 parity bar — adjacent) · `03-decision-record.md` D39/D40 (+ pass-8/9 amendments: both-exits closure, subject-identity guards, viewer-independence, version-aware membership invariant)/D41 · `05-operational-numbers.md` §Backup/DR (PITR floor, RTO, drill row) · `00-fixed-constraints.md` FC-7 (days-scale budget, owner ruling near-verbatim) · `scripts/window/step0_bootstrap.sql` · `scripts/migration_runner.py` + `documentation/operations/migration-runner.md` (adopt/apply/ledger semantics) · the M.1 transform spec (`documentation/planning/2026-08-17-m1-transform-spec/`, §6 postcondition index) · `documentation/planning/2026-08-17-m3-parity-bar-mapping/` (#825) · #787 (alex's production measurements: no runner ledger; 050 adopts at first contact; **PG 17.10**) · #793 (census zero, open) · ari's access measurement 2026-08-17 (§1 P1).

---

## §1. Preconditions — each stated, none assumed

**P1 — ACCESS: the fleet cannot reach production today, measured, so the branch is owner-provisioned.** Measured 2026-08-17: `neon` CLI credentials dated 2026-03-30 falling back to browser OAuth; `railway` returns Unauthorized; no `DATABASE_URL` in any env tier. M.2 as written therefore depends on artifacts **nobody here can currently mint**, and this spec treats them as a named provisioning request, not an assumption:

| Ask | Exactly what | Why |
|---|---|---|
| P1.a | A **PITR branch of the production branch** (`br-square-frog-ai37r0qg`, project `ancient-grass-50759240` — identity per alex's #787 provenance table), created at a recorded LSN/timestamp | The rehearsal substrate. Branch creation needs Neon project access the fleet lacks |
| P1.b | A **psql session as the database-owner role** (`neondb_owner`) against the **branch** endpoint — obtained per the acquisition note below, **never by resetting or rotating this role's credential** | `04` L93: the bootstrap runs "by the database-owner actor — on Neon, the project's database owner." This is the leg where Neon's role layer gets its verdict, so a lesser substitute measures nothing |
| P1.c | The branch endpoint's connection string, handed to the harness | §2's isolation gate pins to it |
| P1.d | Repeat-until-clean authority: **branch deletion + re-branch per attempt** (branches are copy-on-write; cost is cents) | §2's fresh-substrate rule |

**P1.b acquisition — outage-shaped if done wrong, so the mechanism is named rather than left to the console.** `neondb_owner` is the role **production services authenticate as**. Whether a Neon credential reset is branch-local or project-visible is **not verified from here** (per the #835 review: nobody on this side can test it), and an instruction that is only safe under one unverified model is not an instruction — so:

- **Prohibited outright: obtaining the session by resetting or rotating `neondb_owner` anywhere** — most especially any console flow whose branch selector defaults to the production branch. If this path were ever the only one left, the rehearsal stops and the ask goes back to the provisioner; it is never taken under time pressure.
- **Preferred path (zero mutation): the existing stored credential.** Production's `DATABASE_URL` (Railway env; custodian: the owner) already carries the `neondb_owner` credential, and a branch is expected to accept the parent's credentials at its **own** endpoint (branches copy cluster state at branch point). The custodian constructs the branch DSN by swapping only the host, and hands that over — the secret never changes value or custody. **Expected, then verified, never assumed:** first contact is read-only — connect to the branch endpoint and, before any mutating statement, assert `current_user = 'neondb_owner'`, the §2.1 provenance gate, **and read `pg_roles` for `svc\_%` rows** (one SELECT in the session already open). The read is a **measurement of the catalog this session reaches** — which is what SD-M2-3's P5 actually needs — not a read of production's current catalog, and it imports no position on the Neon scoping question this bullet declines to settle. Three readings, three routes: **zero rows** — the fresh world; SD-M2-3's basis is measured; P5 may run. **`svc_migration` alone** — the documented 0.2-Login world, inherited at branch point (`04` §0.2 prints its creation pre-window, before any window opens; the N3-shape creator-ADMIN row corroborates that provenance): **record the reading; P5 proceeds** — its `ALTER ROLE` is then the documented rehearsal password set on the inherited role, exactly as branch-local as every other mutation the rehearsal performs (§2.2's structural isolation is the warrant for all of them alike), and §2.3's load-bearing 0.2-Login contract gets the very creator-ADMIN row N3 probes, rather than a halt on a documented-correct state. **Any other `svc_*` present** — a bootstrap has run somewhere upstream of this branch: stop, record, §3's divergence route (that reading falsifies SD-M2-3's fresh-world basis; contact with *this* branch is only reachable if the fresh-branch rule (SD-M2-1) was not honored — named last because the spec makes it the least available cause). If the credential does not authenticate, that is a recorded observation, not a license to reset.
- **Ruled out as a substitute: a stand-in owner-equivalent role created for the rehearsal.** The bootstrap's executor identity is part of what §3 measures (N3's creator-ADMIN semantics, N9's implicit `pg_database_owner` membership) — a stand-in runs the right statements as the wrong subject, which is the R8 class exactly: green lines over an unverified subject.
- **Fallback, only if the inherited credential fails:** a credential set scoped **explicitly by branch id via the Neon API**, executed by the provisioner only after confirming from Neon's own documentation/behavior that the operation is branch-local — the safety proof is the provisioner's, made before the call, never this spec's assumption. Console default-branch flows remain prohibited even here.

**P2 — CODE: the full file order must exist before any leg can run, and that chain is fork-gated upstream.** 3d needs F.2.2–F.2.9 complete (today: F.2.2 only). 3e needs the M.1 transform files — which the M.1 spec's file-contract rule 8 forbids landing before **Fork A** is ruled, and whose M1-04/M1-06 cells additionally wait on **Fork B** and **Fork E**. So M.2's executability is transitively gated on A (hard), B and E (cell-level) — stated here as the chain it is, not rediscovered leg by leg.

**P3 — the branch census.** Run `scripts/fc8_gate.py` (#792) against the **branch** at leg start: CLEAR required; `UNCLASSIFIED` and `is_active IS NULL` buckets recorded. This re-derives the 2026-08-13 zeros on the actual substrate (the backfill corpus is operator-triggered — zero is frozen, not guaranteed) and rehearses the gate itself.

**P4 — PITR floor observed, not assumed.** `05` §DR: "Neon PITR window ≥ 7 days — verified at 0.2's gate and re-checked when the plan changes." The rehearsal records the project's **actual configured retention** at branch time (Neon API/console value, P1 provisioning captures it) and asserts ≥ 7 days. This is DR-box 1 of the gate.

**P5 — `svc_migration` credential on the branch, out-of-band.** `04` L96–98: passwords are deployment env, set out-of-band, never DDL. After the bootstrap creates the roles, the owner-actor session sets the rehearsal password (`ALTER ROLE svc_migration PASSWORD …` from the P1.b session — environment provisioning, not stream content) [SD-M2-3]. The runner then connects as `svc_migration` for every step-3 file, as the gate requires.

---

## §2. Environment and identity discipline

Every rule below exists because a measurement taken from the wrong subject, or the wrong moment, reads green while being true of nothing.

1. **Branch provenance gate, before anything else.** The harness records and asserts: project id, branch id, parent branch id == production's, the PITR LSN/timestamp, and that the connected host == the branch endpoint from P1.c. **Refuse-if-mismatch** — the harness will not execute against a host it did not expect (the same refuse-shape alex's #787 script used against production, pointed the other way).
2. **Isolation is structural, not disciplinary.** The harness carries **only** the branch connection string; the production endpoint appears nowhere in its inputs. A rehearsal that *could* reach production is a defect before it has done anything.
3. **PG major assert.** The branch must report the production major (**17.x**, measured 17.10). Consequence stated once: **the PG16+ arms of every version-aware gate are the live arms** — the step-8 membership assertions run their shape form (creator-pinned OID compare), never the PG15 pinned form, and the 0.2 Login contract (same-actor creation confers ADMIN) is load-bearing in every leg.
4. **Fresh substrate per attempt** [SD-M2-1]. "Repeated until clean" means: each attempt runs on a **fresh PITR branch**; a failed or interrupted attempt's branch is retired (kept briefly for forensics, then deleted), never re-run in place. A partially-run branch is the mid-rebase state generalized — numbers taken from it are true of nothing. The **only** deliberate exceptions are Leg 2 (rollback-and-re-entry, whose subject *is* the interrupted state) and Leg 4 (guard-refusal, whose subjects are deliberately-staged wrong states) — in both, the state is **constructed on purpose and named in the log**, not inherited from an accident.
5. **Measurements from settled states only.** Wall-clock stamps land at step boundaries (after the runner returns, after a gate completes); no number is read mid-step. The parity report runs only after the leg's final step has returned.
6. **Every gate line runs as printed and viewer-independent** (D40/R8): catalog predicates read `pg_catalog` directly; nothing is inferred from `information_schema` views that filter to the viewer's enabled roles.

---

## §3. The Neon managed-role verdict — the checklist that IS the measurement

`03` D40, closing line: *"proven on stock PostgreSQL by the 0.2 gate; … Neon's managed-role layer gets its verdict at M.2's bootstrap leg (R6 explicitly did not test it, R7 did not either)."* This section makes that verdict a **checklist of executable probes**, each with its stock-PG expectation, run in bootstrap order inside Leg 1. A divergence is not a rehearsal failure — it is **the finding this leg exists to produce**, on a branch, where it is cheap.

| # | Assumption (source) | Probe | Expected (stock PG) | If it diverges |
|---|---|---|---|---|
| N1 | The project-owner actor can `CREATE ROLE … LOGIN/NOLOGIN` (bootstrap §1) | run the guarded DO block's first loops | 7 roles exist | Bootstrap dies at its first statement — the whole-window shape is at stake; halt, record verbatim error |
| N2 | Owner can grant the four transient memberships to `svc_migration` (bootstrap §2) | `GRANT … TO svc_migration` | succeeds | same |
| N3 | **The self-grant**: `GRANT svc_migration TO current_user` is legal because on PG16+ the creator holds ADMIN from creation (0.2 Login contract; R7 executed the failure on 17.7) | the bootstrap statement + `SELECT admin_option FROM pg_auth_members WHERE roleid='svc_migration'::regrole AND member=current_user::regrole` | succeeds; `admin_option = t`, grantor = the owner actor | If Neon's layer records a different grantor or withholds ADMIN, the self-grant and BOTH step-8 `REVOKE … FROM current_user` legs break — a load-bearing divergence |
| N4 | **The public-owner precondition**: observed owner ∈ {`pg_database_owner`, `svc_migration`} (bootstrap's RAISE, R7/R8 shape) | the bootstrap's own SELECT | on a virgin branch: `pg_database_owner` | The bootstrap **halts by design** ("record the real owner and adapt before opening the window"). On Neon this is a live possibility — the halt firing on the branch is the mechanism working; record the observed owner verbatim |
| N5 | Owner can `ALTER SCHEMA public OWNER TO svc_migration` | bootstrap statement | succeeds | halt, record |
| N6 | Owner can `GRANT CREATE ON DATABASE … TO svc_migration` | bootstrap statement | succeeds | halt, record |
| N7 | `GRANT SELECT ON ALL TABLES IN SCHEMA public TO svc_migration` covers the legacy inventory | after grant: the abandon-gate's `aclexplode` predicate inverted (count of legacy tables WHERE grantee=svc_migration = full count) | full inventory | partial grant = R6's "schema ownership confers no table access" one layer up; halt |
| N8 | **The pass-9 auto-grant shape** holds: `roleid`-side `svc_%` rows match exactly (member = creator, `admin_option`, no `set`, no `inherit`) | the step-8 PG16+ gate line, run read-only right after bootstrap | zero rows outside the exception | A Neon-managed grantor or looser row **fails the printed gate by design** ("any foreign or looser grant still fails") — the gate is doing its job; the divergence is the verdict |
| N9 | The owner actor is implicitly a member of `pg_database_owner` (abandon leg's `ALTER … OWNER TO pg_database_owner` legality, `04` L221–225) | attempted only in Leg 3 as printed | succeeds | abandon path broken on Neon — load-bearing for the abandon exit |
| N10 | No superuser is exposed, and creator auto-grant rows are **unrevocable by the owner** (pass-9's measured mechanism: grantor recorded as the cluster role) | attempt `REVOKE svc_migration FROM current_user` at step 8 (as printed) succeeds while the *auto-grant* rows persist | printed revokes succeed; auto-grant rows remain, matching N8's shape | Divergence either direction gets recorded — both step-8 gates depend on this split |

**Divergence route:** halt the leg; capture the probe, the verbatim server response, `pg_catalog` state; file the finding on #790 (the Phase M tracker). Where the *ruling* on a divergence lives is the plan-author question whose destination is **unresolved** (M.1 spec §3.8) — this spec inherits that honestly rather than inventing an address. The branch is retained until the finding is filed.

---

## §4. The legs

Each leg: entry state → sequence → assertions → exit gate → evidence artifacts (§7 templates). All runner steps as `svc_migration`; bootstrap and step-8 as the owner actor (P1.b); every gate line as printed in `04`.

### Leg 1 — clean end-to-end (the gate's first clause)

1. **Prep:** provenance gate (§2.1) → PG-major assert → P3 census (CLEAR) → P4 floor recorded.
2. **Bootstrap** as owner actor, §3 probes interleaved in bootstrap order. N4's halt firing here is a finding, not a failure.
3. **P5 credential**, then **one runner invocation** as `svc_migration`, advisory-locked, file order:
   - **3a `runner adopt`** — expected on this branch (which inherits production's measured world): **no prior ledger exists**; adopt seeds `runner.schema_migrations` with `001`–`049` per the manifest **and `050` as `adopted` via its own postcondition probes** (alex's measured adopt-at-first-contact, #787). Assert: ledger rows exactly {001..050}, all `status='adopted'`; **zero DDL statements sent** (the recording-connection technique from #787's arm A/B is the instrument). This leg **records #787-inert as a rehearsed fact**, not an inference.
   - **3b** — assert **no-op**: 050 already ledgered, `pending` list empty of it, zero statements sent. (A 3b that *executes* here means the branch was not in production's world — provenance failure, not a privilege question.)
   - **3c** (051) — postconditions as printed: `public` zero relations; `legacy` holds the full inventory by count and name.
   - **3d** — the F.2 files into empty `public`; assert the prefix gate's report (`f2_prefix_report` ok at the final boundary) and every file's own postconditions.
   - **3e** — the M.1 transform files; assert **every postcondition in the M.1 spec §6 index green**. Quarantine arms: per Fork A's ruling — see §5 for what a fire means mid-rehearsal.
   - **3f** — snapshots into `archive`, every legacy table; assert count parity per table.
   - **3g** — drop `legacy`; assert `pg_namespace` no longer holds it.
4. **Step-8 success variant** as owner actor: subject-identity guard (target marker present AND legacy absent) → revokes → the printed gate lines, **PG16+ arms** (§2.3), all `pg_catalog`-direct. Assert `public` owner = `svc_migration` (steady-state by design).
5. **Smoke battery** (§6) against the branch.
6. **Wall-clock table** (§7): per-step and total. **Total vs FC-7's budget:** FC-7's ruling is *days-scale* ("we can take a few days" — `00`, near-verbatim; no ratified number exists). The rehearsal's job is to put a **measured number** next to that qualitative budget for the owner to judge — this spec deliberately does not invent a threshold.
7. **Branch parity report** (§7): ledger == expectation — on this branch the expectation is **fully enumerable**: {001..050 adopted} ∪ {051, the F.2 files, the M.1 files, 3f, 3g: applied} — plus the M.1 postcondition checklist, all green.

**Repeat until clean** under §2.4: any red → forensics → retire branch → fresh branch → full re-run. The gate's clause is **one fully clean end-to-end run**; partial greens accumulate learning, never evidence.

### Leg 2 — rollback-and-re-entry (own branch)

Run Leg 1 to a **declared interruption point**, then the D41 lever, then retry to completion.

- **Interruption points** [SD-M2-2]: primary = **after 3e completes, before 3f** (maximum transformed data at risk — the richest test of leg 1's `DROP SCHEMA public CASCADE` discarding 3d *and* 3e work). Secondary (optional, cheap on a second branch) = mid-3d (target schema partially built). Chosen as spec-tier parameters; neither is a fork — any point between 3c and 3g exercises the same four legs.
- **The four legs**, exactly as printed (L152–159), as `svc_migration`, in order. Assertions after each:
  1. after leg 1: `public` gone from `pg_namespace`;
  2. after leg 2: `public` holds the legacy inventory (count+name — the 3c postcondition inverted);
  3. after leg 3: `archive` gone;
  4. after leg 4: **the D41 ledger-truth assertion** — rows deleted for the move file and everything ≥ it; **3a's adopt rows and 050 remain** ("the ledger is truthful to the database at every point": every surviving row's effects are present in the restored schema; 050's effects rode the un-rename back).
- **Retry path:** bootstrap grants still armed (D40's both-exits rule — this is the exit where objects survive); re-invoke the runner; assert it **re-enters at the move file** (deterministic, leg 3 cleared the archive collision, leg 4 cleared the no-op) and completes 3c–3g + step-8 success + smoke, all Leg-1 exit gates green.
- Wall-clock recorded for the lever itself (the "seconds, in place" claim of D41 gets a number).

### Leg 3 — abandon (own branch)

Run Leg 1 to the same declared interruption point → four legs (as Leg 2) → **step-8 abandon variant** as owner actor, exactly as printed (L207–259): subject-identity guard first (legacy marker present AND target marker absent), then the order-load-bearing sequence (ownership restore and legacy-grant revoke **before** the membership revokes — the first two legs consume the still-live self-grant), then the pre-window gate: capabilities, not mechanisms (D40/R7) — ownership = `pg_database_owner`, no `svc_migration` CREATE anywhere, the `aclexplode` zero on legacy tables, membership invariant in its **PG16+ shape arm**. Exit assertion: **pre-window shape restored**, per the gate's own lines, plus the ledger-truth check (as Leg 2.4).

### Leg 4 — guard-refusal (two staged branches, both must HALT with schema untouched)

The pass-9 mis-runs, executed deliberately:

- **4a — abandon variant against a COMPLETED branch** (a Leg-1-finished state): must die at the subject-identity guard (`posting_queue` absent / `jobs` present). Assert: exception raised naming both markers; **schema untouched** — full `pg_catalog` snapshot (namespaces, class list, owners, ACLs, `pg_auth_members`) taken before and after is **byte-identical**.
- **4b — success variant against a MID-WINDOW branch** (a Leg-2-style interrupted state, before 3g): must die at its guard (`jobs` absent OR `legacy` present). Same snapshot-diff-empty assertion.

"Both mis-runs halt, schema untouched" is the gate's clause verbatim; the snapshot diff is what makes "untouched" an observation instead of a claim.

### Leg 5 — the DR-drill obligations `05` attaches to this rehearsal

`05` §DR names M.2 as **the first restore drill**, which adds two items beyond `04`'s text:

1. **RTO observation:** the drill's stated sequence (PITR branch → runner parity check → smoke suite) is exactly Leg 1 — record its wall-clock **as the first RTO data point** against the 1 h target (branch creation time included, from P1 provisioning logs).
2. **Tenant-level recovery, exercised once** ("PITR branch + selective per-workspace copy … exercised once in the first drill"): on the completed Leg-1 branch, extract one workspace's rows by `workspace_id` WHERE-clause across the tenant-scoped tables and verify counts against the parity report's per-table numbers. Read-only; evidence in the run report.

---

## §5. Fork A inheritance — what a quarantine fire means mid-rehearsal

Fork A (quarantine home/shape/**actor** — mason's menu, unresolved) lives at 3e, and M.2 rehearses 3e **against real branched data — exactly where mason predicted it first fires expensively** ("a missing adjudication actor reads as a rehearsal failure rather than as a design gap"). This spec inverts that: the branch makes the firing **cheap**, provided it is read correctly.

**The protocol:** a quarantine-arm postcondition failing (or, under A2, a quarantine table going nonempty) during Leg 1/2/3 is **Fork A surfacing, not a rehearsal defect**. The leg halts (the runner's own behavior); the harness exports the affected row set read-only as the fork's sizing evidence; the branch is retained; the finding is filed on #790. **What the rehearsal must NOT do:** hand-repair `legacy.*` on the branch and re-run — that rehearses option A3 as if it were ruled, needs exactly the undeclared write privilege A3's ratifier (the owner) has not granted, and converts a design signal into a silent default. File-contract rule 8's logic, applied to execution.

Per-option deltas for this spec:

| A ruling | What M.2 rehearses at 3e |
|---|---|
| **A1** (pre-window adjudication) | Quarantine arms are hard zeros; the pre-window detection battery (M.1 spec §5.1) runs against the branch during prep; a fire in-leg = detection gap → fix detection, fresh branch, re-run |
| **A2** (quarantine table + resolution file) | The rehearsal gains a step: 3e completes green with quarantine rows; the **adjudication + resolution file** is rehearsed between 3e and 3f — including its write grant, which the bootstrap/step-8 rehearsal must then also cover (both-exits closure applies to the new grant too) |
| **A3** (halt and retry) | The halt IS the mechanism: rehearse owner-actor repair on `legacy.*` + the D41 lever + re-entry — and the rehearsal log must record that this exercises the privilege A3's ruling grants, so the step-8 gates' expectations change accordingly |

Forks **B** and **E** do not add rehearsal branches of their own: they gate M.1 file *authoring* (P2's chain), so by the time M.2 can run, their cells are concrete. Listed to keep the chain visible, not as new work here.

---

## §6. The smoke battery — what "the target suite's smoke tests" can mean at M.2, measured

The parity-bar doc (#825) establishes, test-pinned: **the target has no app plane at all today** — "no target services, repositories, adapters or command handlers exist anywhere"; `TargetBase` has zero subclasses. The M.3 **parity bar** (`04` L262: command vocabulary, prompts, notifications, sync, scheduling, manual mode, API publish path) is an **M.3-opening precondition served by application code that deploys at M.3 step 4** — it is not, and cannot be, M.2's smoke. The parity doc's own two open forks (bar-`approve` vs `autopost_now`; C2/C3 scope) therefore gate M.3's bar, **not this gate** — adjacent, cited, not inherited.

M.2's smoke battery is the **DB-plane instrument suite the repo already owns**, pointed at the branch [SD-M2-4]:

1. `scripts/schema_parity.py` — branch `public` vs the advertised stream (the 3d byte-parity claim, verified on the rehearsal substrate);
2. `scripts/tenancy_gate.py` — tenancy signature over the full landed lineage;
3. the RLS harness assertions (`tests/scripts/test_rls_harness.py`'s checks, branch-pointed) — policies present and enforcing per §7-DDL;
4. the M.1 §6 postcondition index, re-run read-only (idempotent SELECTs — a second green read from a settled state, distinct from the in-transaction green at 3e);
5. the step-8 gate lines re-run read-only (steady-state shape holds *after* everything, not just at stand-down);
6. `scripts/fc8_gate.py` as a **fails-loudly positive control**: post-3g its subject tables no longer exist, so the correct assertion is exit 2 (ERROR, relation absent) — never a CLEAR. A gate that could report CLEAR against a world missing its subject would be a silent-empty instrument; asserting the loud failure proves it cannot green on the wrong world.

Anything app-shaped (bot commands, publish path) is out of M.2 by construction and stated so.

---

## §7. Evidence — formats, homes, provenance

- **Run report per attempt:** `documentation/planning/2026-08-17-m2-rehearsal-spec/runs/<date>-<leg>-<n>.md` (tracked files, never comments): branch identity block (§2.1 values), per-step wall-clock table, gate outcomes, §3 checklist results, anomalies verbatim.
- **Wall-clock table:** one row per step (prep, bootstrap, 3a…3g individually, step-8, smoke), start/end/duration; totals per leg; Leg 1's total stated next to FC-7's **days-scale** budget and `05`'s 1 h RTO target — numbers beside their bars, no verdicts invented.
- **Parity report:** the enumerated expected ledger (Leg 1.7) vs `runner.schema_migrations` actual, row-for-row; the M.1 postcondition checklist with each SELECT's result; per-table row counts (feeds Leg 5.2).
- **Provenance rules:** every number carries the branch id + LSN it was measured on; nothing is reported from a retired or interrupted branch except as forensics labeled so; the §2.5 settled-state rule governs all of it. The gate's "timed, logged, repeated until clean" is satisfied by the run-report series ending in one report that is green on every line.

---

## §8. Gate mapping (`04` L91, verbatim clauses → where satisfied)

| Gate clause | Where |
|---|---|
| "one fully clean rehearsal end-to-end, with the wall-clock recorded (it sizes the real window against FC-7's days budget)" | Leg 1 + §7 wall-clock (budget is days-scale by ruling — recorded against, not thresholded) |
| "the branch parity report (runner ledger == expectation; every M.1 postcondition green)" | Leg 1.7 + §7 parity report (expectation fully enumerated) |
| "the rollback-and-re-entry leg green" | Leg 2 (four legs + D41 ledger-truth + deterministic re-entry to full green) |
| "the abandon leg green (pre-window shape restored, per its gate)" | Leg 3 (printed abandon gate, capabilities-not-mechanisms, PG16+ arms) |
| "the guard-refusal leg green (both mis-runs halt, schema untouched)" | Leg 4 (both guards + byte-identical catalog snapshots) |
| "the DR-drill boxes ticked (PITR branch → runner parity → smoke suite)" + `05`'s floor | P4 (≥ 7-day floor observed) + Leg 1 (the drill sequence) + Leg 5 (RTO datum, tenant-level extraction) |

**Honest state of this gate today:** every clause is *specified*; none is *executable* until P1 (access — owner-provisioned) and P2 (F.2 complete + M.1 files landed, itself Fork-A-gated) clear. That is the same shape as M.1's gate item 3 was: stated short rather than papered over — with the difference that here the short items are preconditions outside this spec's control, each named with its owner.

---

## §9. Spec-tier decisions and the fork register

**[SD-M2-n] register** (same contract as M.1 §3.7: no privilege/actor gap, no cross-artifact contradiction, reversible, obvious-but-unratified; this PR's review is their approval):

| id | Decision | Alternative |
|---|---|---|
| SD-M2-1 | Fresh PITR branch per attempt; interrupted/staged states only ever constructed deliberately (§2.4) | re-run in place (evidence true of nothing) |
| SD-M2-2 | Interruption points: primary after-3e, optional mid-3d (Leg 2/3) | a single fixed point (loses the partial-3d case) |
| SD-M2-3 | `svc_migration` rehearsal password via `ALTER ROLE` from the owner session, post-bootstrap (P5). Not outage-shaped, unlike P1.b — **provided the catalog this session reaches holds no pre-existing `svc_*` role for the ALTER to collide with**. Basis at its true strength, an **inference from program state, not a measurement**: no window has ever opened, and the step-0 bootstrap is the only printed creator of the **six service roles other than `svc_migration`** — `svc_migration` itself is printed as a **0.2 pre-window creation** (`04` §0.2 Login; `migration-runner.md` rollout step 1; the bootstrap's own guard expects it and passes it through untouched), and whether that step has run against production is **open from here** (#787 enumerated **schemas and tables, not roles**, and bounded itself out of role topology). The inference is **verified before P5 ever runs** by P1.b's first-contact read of `pg_roles` — a measurement of the session's own catalog, deliberately not framed as a read of production's — whose three readings and routes live at P1.b: zero rows (fresh world; P5 runs), `svc_migration` alone (the documented 0.2-Login world; recorded, P5 proceeds on the inherited role), any other `svc_*` (halt — a bootstrap ran upstream) | Neon API-managed role credential — deliberately NOT used for `svc_migration`: routing the runner's login through the managed layer would blur exactly the layer §3 is measuring |
| SD-M2-4 | Smoke battery composition (§6, six instruments incl. the FC-8 fails-loudly positive control) | narrower battery (parity only) |
| SD-M2-5 | Run reports as tracked files under `runs/` beside this spec (§7) | issue comments (no diffable history; the M.1 §3.8 lesson one door over) |

**Fork register:** no new forks surfaced by this spec. The open items it runs under, all pre-existing and marked at their sites: **Fork A** (§5 — the one item that blocks M.1 file-landing and therefore M.2 execution outright), **B/E** (P2 chain), **plan-author routing** (§3 divergence route, M.1 §3.8), **#793** (P3 census could move it), the **parity doc's Fork 1/2** (M.3-adjacent, §6), and the **Neon verdict** (§3 — an outcome, not a choice). If executing this spec surfaces a genuine new fork — the likeliest place is §3's N4/N8 on real Neon — it gets the M.1 treatment: menu, per-option deltas, no pick.
