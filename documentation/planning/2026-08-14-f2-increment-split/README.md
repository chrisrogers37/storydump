# F.2 — the increment split, reshaped under Fork 1 ruling (a)

**Status:** ratified. Fork 1 locked to option (a) on
[#806](https://github.com/chrisrogers37/storydump/issues/806#issuecomment-5287041579).
This document supersedes the split filed in a #746 comment.

**Why it exists at all.** The split lived in a comment. That is the defect #806 was right
about: a scan of every open issue for `F.2` found three mentions and zero owners, because the
issue that owned the work did not carry the label. Re-filing was going to happen anyway under
ruling (a), so it is re-filed *here* rather than into another comment.

Increment content is **derived from the advertised stream**, using the repo's own
`advertised_ddl` module against `origin/main`, not read off the plan prose or hand-counted.

---

## 1. What the ruling changed

`04` §0.2 arm (b) holds the concatenated F.2 migration files equal to the advertised stream —
as an **ordered positional prefix**. The filed split instead had every table increment carry its
tables *with* their RLS enablement and policies, which the stream does not do: it creates all 23
of `02`'s tables before the first `ENABLE ROW LEVEL SECURITY` (index 126) or the first
`CREATE POLICY` (index 149).

Measured against the real gate, the filed shape diverges at index 22 — the stream wants
`CREATE TABLE ig_accounts`, the split wants `CREATE POLICY p_tenant_workspaces`.

Ruling (a) resolves this by reshaping the split rather than the stream or the gate: **every
increment becomes a contiguous segment of the advertised stream.** The gate keeps its positional
guarantee unchanged, and no plan document is edited.

**The property is positional, not merely dependency-ordered.** Once the target lineage is
non-empty nothing can land ahead of its predecessor — F.2.3 before F.2.2 gives
`ok=False, first_divergence=2`, because the stream's next statement is `CREATE TABLE users` and
no other file satisfies that index. So the order below is binding, not advisory.

## 2. The split

257 statements, cut into contiguous segments. `052` has shipped; the rest are in order.

| Increment | Stream | Stmts | Tables | Contents |
|---|---|---|---|---|
| **052** — shared trigger functions | `0..1` | 2 | 0 | `trg_touch_updated_at`, `fn_safe_tz` — **shipped** (#813) |
| **F.2.2** — identity + tenancy | `2..21` | 20 | 7 | `users`, `user_identities`, `workspaces`, `workspace_members`, `workspace_invitations`, `channel_bindings`, `onboarding_sessions` |
| **F.2.3** — accounts, sources, media | `22..44` | 23 | 6 | `ig_accounts`, `provider_quarantine`, `media_sources`, `oauth_credentials`, `media_items`, `post_locks` |
| **F.2.4** — intent ledger | `45..76` | 32 | 5 | `category_post_case_mix`, `post_intents`, **`audit_events`**, `daily_post_counts`, `post_intent_transitions` |
| **F.2.5** — machinery | `77..93` | 17 | 5 | `jobs`, `channel_outbox`, `provider_operations`, **`command_dedup`**, `rate_counters` |
| **F.2.6** — grant matrix + `archive` | `94..125` | 32 | 0 | the §7-DDL grants, `CREATE SCHEMA archive` and its grant |
| **F.2.7** — RLS + policies | `126..201` | 76 | 0 | 23 `ENABLE ROW LEVEL SECURITY`, 53 `CREATE POLICY` — **the increment that closes the tenancy gate** |
| **F.2.8** — `SECURITY DEFINER` doors | `202..240` | 39 | 0 | the nine doors with their `ALTER`/`REVOKE`/`GRANT` cycles |
| **F.2.9** — auth plane (`07`) | `241..256` | 16 | 3 | `session_tokens`, `oauth_states`, `service_tokens`, **with** their RLS and policies |

**26 tables, matching the extractor's independent count.** Segments are contiguous, cover every
statement, and leave no gaps — asserted mechanically, not by inspection.

**Every cumulative prefix passes the real gate.** Driven through `f2_prefix_report` at each of
the nine boundaries: `ok=True` at all nine. That is the property option (a) was chosen for, and
it is measured rather than argued from construction.

## 3. What moved, relative to the filed split

**F.2.2 and F.2.3 keep their exact table membership.** The reshape bites from F.2.4 onward.

- **Policies leave every `02` table increment.** F.2.2–F.2.5 land tables, triggers and indexes;
  RLS and policies for all of them land together in F.2.7. This is §4's cost.
- **`audit_events` joins F.2.4 and `command_dedup` joins F.2.5.** The filed split assigned both
  to F.2.6, which is what made the last three increments non-contiguous.
- **§7-DDL splits three ways** (F.2.6 / F.2.7 / F.2.8) where the filed split had one tail
  increment. It has to: `07`'s three tables sit *after* the doors in stream order, so a single
  §7-DDL increment could not be contiguous. Splitting it also makes F.2.7 — the moment the
  tenancy invariant becomes true — its own reviewable unit.
- **`07`'s auth plane keeps #746's original property for free.** F.2.9's tables, RLS and
  policies are one contiguous run in the stream, so those three tables *do* land with their
  policies. Only `02`'s 23 are separated.

## 4. What it costs, and where the cost lands

Ruling (a) trades a **structural** guarantee — a table and its policy in one PR — for a
**detected** one. The exposure is now bounded and named rather than open-ended:

| after | tables | RLS | policies | tenancy gate |
|---|---|---|---|---|
| 052 | 0 | 0 | 0 | green (vacuous) |
| F.2.2 | 7 | 0 | 0 | **red** |
| F.2.3 | 13 | 0 | 0 | **red** |
| F.2.4 | 18 | 0 | 0 | **red** |
| F.2.5 | 23 | 0 | 0 | **red** |
| F.2.6 | 23 | 0 | 0 | **red** |
| F.2.7 | 23 | 23 | 53 | green |
| F.2.8 | 23 | 23 | 53 | green |
| F.2.9 | 26 | 26 | 58 | green |

**The red window is F.2.2 through F.2.6, and it closes at F.2.7.** It is red by the *plan's own
order*, not by a defect — the ratified stream itself creates tables long before it enables RLS.

Two things follow, and both are obligations rather than observations:

1. **The window must not be closed by deleting the check.** That silences the gate for exactly
   the 26-table stretch it exists to cover. The decision when the first table lands is between
   making the check prefix-aware — compare against the tenancy state the stream's own prefix of
   the same length implies — and bounding it to a complete lineage. The disclosure test in
   `test_lineage_lane.py` states this at the point of failure so it arrives as a decision.
2. **Production is unaffected under every option.** `04` F.2: *"No production execution —
   production first runs these files inside M.3."* Production never observes a partial F.2
   lineage, so the exposure is to CI and review discipline over the lane's lifetime, never to
   live data.

## 5. Fork 2 — dissolved, not deferred

Fork 2 was that F.2.4/F.2.5/F.2.6 are non-contiguous, caused by exactly two assignments:
`audit_events` and `command_dedup` sitting inside another increment's run.

Contiguous segmentation removes the cause. There is no assignment left that produces a
non-contiguous increment, so **Fork 2 needs no ruling** — it was a consequence of the shape (a)
replaced. The two tables move as recorded in §3.

## 6. The paired obligation

Ruling (a) was conditional: **wire `tenancy_gate` to the replayed target lineage.** Without it,
(a) rests on a detector that is not pointed at the thing it is meant to protect.

That gap was structural rather than partial, and the enumeration is what shows it — a single
file would not. Every caller of `tenancy_gate` before this change, complete rather than
sampled:

| caller | what it points the gate at |
|---|---|
| `test_tenancy_gate.py` — synthetic arm | hand-built signature dicts; no database |
| `test_tenancy_gate.py` — real-replay arm | a replay bounded to `LEGACY_LINEAGE_MAX`, which stops *below* the 051 move |
| `test_tenancy_gate.py` — Postgres arms | `gate_probe` / `posture_probe`, tables built by hand on a scratch database |
| `test_rls_harness.py` (#751) | the `confined` fixture — one hand-built `rls_probe` table, as a deliberate proof that the gate *cannot* see owner-bypass |

**Not one of them replays the target lineage.** Every F.2 file is numbered above the move, so
the only arm that replays anything real is bounded to a lineage that can never contain one.

**And an enumeration of callers is the wrong shape to stop at**, which is worth stating because
it nearly hid the better half of the fix: `test_advertised_ddl_replay.py` replays the *entire*
advertised stream into a live database — all 26 tables, RLS enabled, policies attached — and was
simply never a caller. A reader auditing the caller list would pass straight over a populated
target schema sitting one file away. The list answers "where is the gate pointed"; the question
that mattered was "what replayed schemas exist that nobody pointed it at".

Measured in an exported tree: a tenant-keyed table with RLS off, landed as `053`, left the
whole tenancy suite **green (2 passed)** while the lane's own replay observed it in `public`.

The wiring lands in **two** places, because the obligation has two halves and only one of them
was in the ruling's line of sight.

**The file-lineage half** — `test_lineage_lane.py`, which aims the gate at the migration files
as the ruling asked. It carries an in-test probe because the replayed `public` holds no tables
today, so the check alone would pass on an empty set — and would pass just as green pointed at
the wrong database, which is the same failure one level down.

**The completed-schema half** — `test_advertised_ddl_replay.py`, which already replayed the
*full* advertised stream into a live database under the declared actors and simply never ran the
gate on the result. Two lines fix that, and the difference is large: **the lineage half is
vacuous until F.2.2 and does not reach full strength until F.2.9, while this one is at full
strength now.** Measured: 19 tenant-keyed tables of the 26, zero violations. The other 7 carry
no workspace key by design (`02` §7-DDL Class 3/4).

**Three checks, three distinct claims, which is why none of them is redundant.** Arm (b) says
the migrations reproduce the plan. The lineage half says what the migrations landed is
tenant-scoped. The completed-schema half says **the plan itself is correct** — a tenant-keyed
table whose policy `02` forgot would leave arm (b) green, since the migrations would faithfully
reproduce the omission, and would redden only this one.

Its count assertion is a positive control rather than a fact about the schema: `tenancy_violations`
returns `[]` just as readily for a database it never saw. The floor is deliberately loose (15,
against an observed 19) because the exact number is the plan's to change.

## 7. Bounds

- **Stream indices, statement counts and table membership are derived**, by running
  `advertised_ddl` over the committed manifest and plan docs on `origin/main` at `3a59df9`.
  Re-derive rather than quote them: they move if the manifest or `build_stream` changes.
- **The prefix property is measured at all nine boundaries**, with a deliberate-failure control
  (swapping the first two statements gives `first_divergence=0`) so the gate is visibly
  discriminating rather than accepting everything.
- **§4's table is a stream-position derivation, not a violation count.** It counts every table,
  while `tenancy_gate` only demands tenancy of *tenant-keyed* ones — `02`'s Class 3/4 tables
  carry no workspace key by design. The red/green verdict is unaffected (`workspaces` alone is
  created at index 6 and not RLS-enabled until 126), but the numbers are positions, not
  violations.
- **Increment boundaries within §7-DDL are a judgement**, unlike the table increments, which
  follow the plan's own section structure. F.2.6/F.2.7/F.2.8 could be merged into one 147-
  statement increment and still satisfy the gate; they are split for reviewability and to make
  F.2.7 a named milestone.
- **Not re-derived here:** rajan's currency finding on the filed split, and mason's original
  object counts. Both stand.
- **`tenancy_gate` has no operator door, and that half is still open.** `schema_parity` has one
  — `runner parity --against <dsn>` — while `tenancy_gate` is a predicate library with no entry
  point, despite a docstring claiming it "must be runnable in the same predeploy context." So at
  M.3 step 3d, when the F.2 files first run against production, nobody can ask "did all 26 tables
  land born tenant-scoped" without hand-writing Python. A sibling `runner tenancy` subcommand is
  the shape. This is a pre-existing gap from #746 rather than one the reshape introduces, and it
  is named here because this document is what claims the detector is now aimed properly: it is
  aimed in **CI**, at both the file lineage and the completed schema, and not yet at production.
- **Option B is not foreclosed.** It remains the best end-state — the only option that makes
  born-RLS-enabled machine-checked through adjacency — and (a) forecloses nothing. Its price is
  still unmeasured: nobody has executed the reorder, so the position-independence of the
  manifest ratchet is read from `by_hash`/`_ratchet`, not run.
