---
title: "CLI v2 — phase 02: the eight read views and the read verbs (PR 2)"
type: plan
status: draft
owner: chris
created: 2026-09-15
tags: [plan, cli, api, ledger, devx]
links: []
---

## Summary

The diagnostics that took fifty-five SQL files become eight tenant-scoped API views and eight CLI
verbs with `--json` and `--watch`: `story`, `cards`, `floating`, `account`, `jobs`, `outbox`,
`burst`, `posture`. Each view is one bounded query run under the principal's workspace, reading
only tables under row-level security; a person-bound token loops the person's memberships.
Proven as the login role, so the F.4 switch cannot break them.

## Evidence

- The queries were validated against production on 2026-09-13..15 and are committed beside this
  plan as `probes/float_burst_read.sql`, `probes/burst_account.sql`, `probes/burst_order.sql`,
  `probes/pre_burst.sql`, `probes/double_serve.sql`, `probes/heal_read.sql`,
  `probes/float_ddl_probe.sql`. Their tables: `post_intents` (`055_intent_ledger_tables.sql`:
  `state`, `publish_step`, `cap_consumed_on`, `attempts_by_step`, `entered_state_at`),
  `audit_events` (`:199-203`; `detail->>'event'` in `float_wait`, `cap_deferred`, `cli_command`),
  `provider_operations` (`056:170`: `op_kind`, `generation`, `state`,
  `response_ref->>'url_variant'`), `channel_outbox` (`056:140-160`), `jobs`, `daily_post_counts`,
  `ig_accounts` (`next_slot_at`, `posts_per_day`, `tz`).
- **Outside RLS, not read:** `rate_counters` has no `workspace_id` and a `USING (true)` policy
  (`056:229-240`, `058:175-176`); `p_jobs` admits `workspace_id IS NULL` system rows
  (`058:159-162`).
- `src/api/routes/v1.py:398` `GET /workspaces/{ws}/intents` and `:467` `/intents/{intent_id}`:
  the read-route pattern (`_open_tenant`, a service function, JSON).
- `src/api/app.py:636` `/health`, `:663` `/health/scheduling`, `:740` `/health/posting`.
- Phase 01: the `Principal`, the token route allowlist (which admits `/ops/*`), the envelope and
  error shapes in the vocabulary module, the runner's ledger grant (F7).

## Implementation Plan

### Dependencies
Phase 01 merged.

### Blocks
Phase 03 (`doctor` reads `posture`; `burst --watch` is the post-deploy read).

### Steps

1. **`src/services/target/ops_views.py`** — one async function per view, each a single bounded
   statement `(conn, *, workspace_id, …)` returning rows that all carry `workspace_id`, written to
   run under `p_tenant` with an explicit `workspace_id = :ws` predicate and no cross-workspace
   join:
   - `story(conn, workspace_id, intent_id)`: the intent; its `audit_events` in order (from/to,
     actor kind, channel, `detail` — including the paired `cli_command` row); `provider_operations`
     (kind, generation, state, `url_variant`, error/subcode, `elapsed_ms`); `channel_outbox` by
     binding (kind, state, ref, attempts, `outcome_text`, `supersedes_ref`).
   - `cards(conn, workspace_id, intent_id)`: `channel_outbox` joined to `channel_bindings`
     (channel, external ref), adopted twins included, in send order.
   - `floating(conn, workspace_id, limit=100)`: `state='approved' AND cap_consumed_on IS NOT NULL`
     with its `ready|leased` job and the last `float_wait` row's class and rung.
   - `account(conn, workspace_id, key)`: cap per day, today's `daily_post_counts` in the
     account's zone, `next_slot_at`, the last twenty intents' outcomes.
   - `jobs(conn, workspace_id, since)`: `workspace_id = :ws` only (system singletons are not a
     workspace's business); counts by kind × lane × state; the oldest runnable age; `failed`
     rows with their last error.
   - `outbox(conn, workspace_id, since)`: pending, sending, ambiguous, failed rows by binding. No
     paced holds in v1 (`rate_counters` is outside RLS; a `pacing` view with an explicit key
     filter derived from the tenant's bindings is a later slice).
   - `burst(conn, workspace_id, since)`: taps and outcomes (transitions out of
     `awaiting_approval`), container permits with variant and outcome, refusals per story,
     `float_wait` rows, siblings posting past a waiter, review cards raised — `probes/`'s
     `float_burst_read.sql` and `burst_account.sql` as one result.
   - `posture(conn)`: the runner's ledger (F7), `current_user` and `rolbypassrls`, RLS enabled
     and forced per tenant table, the SECURITY DEFINER census; not tenant data, any role.
   Every query is `LIMIT`ed or windowed (`since`, default 3 hours).
2. **`src/api/routes/ops.py`** (F6) — `GET /ops/workspaces/{ws}/story/{id}`, `/cards/{id}`,
   `/floating`, `/account/{key}`, `/jobs`, `/outbox`, `/burst`; `GET /ops/posture`. Each
   `_open_tenant`s the workspace under the principal (any role, the allowlist admits tokens),
   calls the view and answers the phase-01 envelope: `{"v": 1, "kind": <view>, "data": {"workspace_id":
   …, "rows": […]}, "error": null}`. Registered in `create_app` beside the v1 router.
3. **CLI verbs** in `storydump_cli/commands/reads.py`: `story`, `cards`, `floating`, `account`,
   `jobs`, `outbox`, `burst [--since 3h|<timestamp>]`, `posture`. `--workspace <id|slug>` selects
   one; without it the principal's readable workspaces are looped. `data` is always
   `{"workspaces": [{"workspace_id", "rows"}]}` — one shape whether one workspace or several;
   `posture`'s `data` is the view's object. Human output is a `rich` table per workspace.
4. **`--watch [--every 30]`** in `storydump_cli/watch.py`: re-read; key rows by their id column
   (intent, job, outbox id); print added and changed rows only; `--json` emits one envelope per
   read with `data.changes`; exit 0 on the verb's terminal condition (`floating`: empty for two
   consecutive reads; `burst`: no story mid-flight), exit 6 on a failure condition the verb
   defines, and Ctrl-C ends with exit 0.
5. **Exit codes**: `1` when a `story`/`cards`/`account` key resolves to nothing; `3` on 401/403
   and on `not_a_member`; `4` when the API is unreachable.
6. **Docs**: `AGENTS.md`'s CLI section lists the views with one example each;
   `documentation/operations/reading-the-ledger.md` replaces the SQL recipes; CHANGELOG.

## Test Plan

Written red first; one named mutation per behaviour in `tests/mutations/cli_v2_02.sh`.

- **View gates** `tests/scripts/test_ops_views_gate.py` (replayed schema, the API in-process,
  connected as `svc_ingress` with the tenant GUC): seed two workspaces; for every view the rows
  for workspace A are complete and correct against a seeded story (a refused-then-accepted
  container, one float wait, a twin card, a cap deferral, a `cli_command` row) and every row
  carries A's id — none of B's appear; every view respects its bound; a system job with
  `workspace_id NULL` never appears in `jobs`; `posture` lists the applied migrations and the
  role.
- **Route gates**: each `/ops` route answers the envelope for a member's token, 403 for a
  service identity of another workspace, 401 without a token; `readonly` is admitted; a session
  is admitted.
- **CLI tests** (`tests/src/cli/`): each verb against the in-process app with a token fixture:
  the envelope and its error variant on a 404/403/connection error, the loop over two
  memberships, `--workspace`, exit codes 1/3/4, `--watch` prints only changed rows and its
  `--json` framing (a fake clock and a scripted API), redaction of an `sdt_` value in a row.

## Verification Checklist

- [ ] `storydump burst --since 2026-09-15T14:50:00Z --json` against production reproduces the
      first live burst's read (13 posted, 8 refusals, one float) from the ledger.
- [ ] `storydump floating --watch` during a burst prints a story stepping out and back in, and
      exits 0 when nothing is floating.
- [ ] `storydump posture` shows the applied migrations through 077 and the connected role.
- [ ] The view gate is green as `svc_ingress`; a second workspace's rows are never returned; no
      view reads `rate_counters`.

## What NOT To Do

- No cross-workspace join; a view is one workspace's rows, the CLI does the looping.
- No read of a table outside row-level security; no SECURITY DEFINER door; a view that cannot
  be written under `p_tenant` is redesigned.
- No unbounded query; every view has a `LIMIT` or a window.
- No raw SQL in the CLI package; no Telegram assumption in a shape.

## Build notes (2026-09-15)

Built as PR `implement/cli-v2-02-reads`. Deviations from the steps above, each deliberate:

- `floating` joins the story's retry job in any of `ready`, `leased` **or `failed`** (a live one
  preferred): the step named the live job only, but a float whose retry died is exactly the
  failure `--watch` exits 6 on, and the CLI builder found the condition could never fire as
  written.
- The gate has a third arm: the same reads as a role that bypasses row-level security
  (production's posture today), so the explicit `workspace_id` predicates are proven, not
  shadowed by the policies. The predicate mutations in `tests/mutations/cli_v2_02.sh` are killed
  by that arm only.
- `posture` reports the ledger as `present`, `absent` (a replayed gate database has no runner
  schema) or `unreadable` (present, no grant) rather than failing; `migrations` is empty unless
  present and readable.
- `burst` is one flat timeline with a `section` discriminator (`tap`, `permit`, `float_wait`,
  `sibling`, `review`, `outcome`) rather than the step's several result sets — one row shape per
  view holds, and the CLI groups on the section. Refusals are `permit` rows in state `failed`.
- `jobs` rows are groups by kind × lane × state with up to five samples for failed and
  review-required groups; a publish job's sample carries its story's `last_error` (jobs record no
  error of their own).
- `account` matches a handle with or without the `@`, or the account id.
- The CLI: `--workspace` takes an id or an exact name (a name is resolved through
  `/me/principal`; every workspace of that name is read; a bad name exits 1); `posture` has no
  `--workspace`/`--watch`; under `--watch` a missing key waits for the row rather than exiting 1;
  the first read prints every row as `added`, removals are printed too, and `burst`'s mid-flight
  test is a `permitted` permit or a story in `publishing` (taps are not counted, or a fixed
  window would never drain).
- After the review round: one window grammar (`vocabulary.window_start`) serves the API's `since`
  and the CLI's `--since` — a span, an ISO-8601 timestamp (naive = UTC) or a bare date, at most
  thirty days, never in the future, an overflow refused (422 / usage 64, not a 500 or a
  traceback); `burst` rows carry the table's own `workspace_id` (a stamp from the parameter hid a
  leaked row from the bypass arm); the sibling join's waits side is bounded to the window less a
  day; `jobs` and `outbox` list owed rows at any age and window the finished ones; handles match
  case-insensitively; cards carry `supersedes_ref`; `posture` lists every tenant-plane table with
  its RLS state (a dropped policy shows as `enabled: false`), reports the role's `rolsuper` as
  bypassing too, and probes the ledger by catalog oid (a name lookup in a schema without USAGE
  is a permission error — found by the gate); the CLI's `--watch` judges failure on what ARRIVES
  after the baseline read, keys burst rows on generation and waiter too, refuses `--every`
  below a second and `--limit` above 500 as usage, validates story ids as UUIDs, prints the
  ledger state under `posture`.

## Context

area: API operator surface, ledger reads, CLI · effort: L · risk: low-medium (RLS-correct
queries) · priority: P1
