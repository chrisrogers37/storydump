---
title: "The health routes get a router like every other route, doctor's six checks become six functions, and the transport's send closure splits"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:medium, api, cli]
links: []
---

# 09 — The composition roots

| | |
|---|---|
| **PR title** | refactor(roots): a health router like every other router, `doctor`'s checks as functions, and `for_chat.send` split |
| **Risk** | Medium — `create_app` wires the whole API and middleware order is load-bearing; bounded by the app-factory tests and by not touching the order |
| **Effort** | M (≈6h) |
| **Files modified** | `src/api/app.py`, new `src/api/routes/health.py`, `storydump_cli/commands/env.py`, `src/channels/telegram_transport.py`, `CHANGELOG.md` |
| **Findings addressed** | TD-C11, TD-C12, TD-C13; TD-C8 as an **optional** step (a ruling) |
| **Depends on** | 04 (moves the shared route helpers to `principal.py`) |
| **Blocks** | nothing |

## Summary

Three entry functions have grown past reading in one sitting for the same reason: they compose.
`create_app` (54 statements) builds the lifespan, wires eight pieces of `app.state`, installs
four middlewares, includes seven routers — and then defines three health routes inline, the only
routes in the API not in a router module. The CLI's `doctor` (215 lines) runs six try/except
ladders that each produce one row, then orders the rows and returns a verdict. And
`for_chat.send` is a 95-line closure that resolves a supersede, validates the media's workspace,
fetches the media, and sends. What must not change: the middleware order (Starlette prepends, and
the file says so), every `app.state` key, `doctor`'s rows, its `--json` envelope
(`{"v":1,"kind","data","error"}`) and its exit codes, and every sentence the transport sends.

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-C11 | `src/api/app.py:550-852` | `create_app` = lifespan + state wiring + middleware + routers + three inline health routes |
| TD-C12 | `storydump_cli/commands/env.py:511-738` | `doctor`: 215 lines, six try/except ladders, nesting 4, complexity 23 |
| TD-C13 | `src/channels/telegram_transport.py:480-574` | `for_chat.send`: a 95-line closure with four fallback `except` arms |
| TD-C8 | `routes/webhooks.py:162-165`, `routes/v1.py:93` vs `app.py:413-414,:447,:536` | Telegram variables read via `settings` in routes and raw `env` in the factory — **optional step 5, a ruling** |

## Dependencies

- **Depends on 04** — it moves `_open_tenant`/`_member`/`_admin`/`_json_object` from `v1.py` to
  `principal.py`. The new health router (step 2) should import from wherever those land.
- Blocks nothing.

## Implementation Plan

### Steps

Each step leaves the suite green on its own. Steps 2–4 are independent of each other; do them in
any order, but not in one commit.

1. **Inventory** — record in the PR:
   ```bash
   grep -n "app.state\." src/api/app.py | wc -l
   grep -n "add_middleware\|include_router" src/api/app.py
   grep -rn "create_app\|_register_webhook\|_sample_webhook_live" src tests --include='*.py' | grep -v "^src/api/app.py"
   grep -rn "create_app\|doctor\|for_chat" tests/mutations/*.sh     # must be empty
   ```

2. **A health router, like every other route in the API** — `src/api/app.py:681` onward defines
   `/health`, `/health/scheduling` and `/health/posting` inline inside the factory. Every other
   route in this app lives in a module under `src/api/routes/` and is included as a router
   (`auth_router`, `v1_router`, `tokens_router`, `ops_router`, `webhooks_router`, `meta_router`,
   `retired_router` — seven precedents at `:665-680`). Create `src/api/routes/health.py` with an
   `APIRouter`, move the three handlers verbatim, and include it beside the others:

   ```python
   app.include_router(health_router)
   ```

   The handlers read `request.app.state.*` rather than the factory's closure variables — check
   each one for a closure capture before moving it, and convert those reads to `request.app.state`
   (they are already state-backed: `db_role`, `webhook`, `webhook_live`, `tap_metrics`,
   `ingress_workers`, `pool_watch`).

   **Do not change any field of any health payload.** `storydump health` renders these, two fleet
   monitors poll them, and audit finding C1 already concerns a renderer/payload mismatch — a
   change here would collide with a flagged defect. The `/health.version` value is likewise
   flagged (C5): move it, do not fix it.

   Tests: `tests/src/api/test_app_factory.py`, `tests/src/test_target_health_endpoint.py`.

3. **Move the webhook registration and sampler into the channel they belong to** —
   `_register_webhook` (`app.py:401-466`) and `_sample_webhook_live` (`:467-505`) are Telegram
   channel logic living in the API's composition root. `src/channels/telegram_webhook_registration.py`
   already exists and already owns `autoregister_enabled` — which `_register_webhook` duplicates
   the off-words of (finding C7, doc 01). Move both functions there; `create_app` calls them.

   **Layer check before moving:** `AGENTS.md` says channels are imported only by the two
   composition roots. `app.py` is one of them, so it may import the channel module — that is the
   rule being followed, not broken. Do not move anything that reads `app.state` directly; pass it
   in.

   Tests: `tests/src/channels/test_telegram_webhook_registration.py`,
   `tests/src/api/test_app_factory.py`.

4. **`doctor`'s six ladders become six functions** — `storydump_cli/commands/env.py:511-738`. The
   body is already six independent blocks that each produce one row, followed by
   `ordered = [...]` (`:726`), the verdict (`:733`), and the return. Extract each block as
   `_check_<name>(...) -> dict` returning the same row it builds today:

   | Block | Lines | Row |
   |---|---|---|
   | the API | `:524-563` | `api` |
   | the token | `:563-626` | `token` |
   | the token store | `:627-630` | `store` |
   | the config | `:631-648` | `config` |
   | Railway | `:649-659` | `railway` |
   | the migration ledger | `:660-725` | `ledger` |

   (Confirm each block's exact bounds by reading — the line numbers above are from the audit and
   must be re-checked before the cut.) `doctor` becomes the six calls, the existing `ordered`
   list, the existing verdict line and the return — unchanged.

   **The contract is the rows.** Same keys, same `state` values (`ok`/`wrong`/`missing`/
   `skipped`), same one-line fixes, same order, same exit code. Characterization:
   `tests/storydump_cli/test_env.py` — the doctor tests are function-based, not classes:
   `test_doctor_reports_every_check_ok_and_exits_0` (`:938`),
   `test_doctor_human_output_is_one_line_per_check` (`:959`),
   `test_doctor_without_a_token_says_missing_with_the_login_fix_exit_3` (`:971`),
   `test_doctor_with_a_token_the_api_refuses_says_wrong` (`:982`),
   `test_doctor_blames_the_api_not_the_token_for_a_5xx` (`:1002`),
   `test_doctor_never_says_ok_over_an_unchecked_token` (`:1019`),
   `test_doctor_reads_a_5xx_health_as_wrong_not_missing` (`:1037`),
   `test_doctor_reads_a_4xx_health_as_wrong` (`:1047`). Run
   `pytest tests/storydump_cli/test_env.py -k doctor --no-cov` after the step.

5. **Split `for_chat.send`** — `src/channels/telegram_transport.py:480-574`. Its structure: a
   supersede short-circuit (`:481-482`), a workspace-ownership check on the media
   (`:486-494`), the media fetch with its fallback arms (`:541-563`), and the send with the
   receipt (`:563-574`). Extract `_fetch_media(...)` for the third and `_send_media_card(...)`
   for the fourth; leave the supersede short-circuit and the ownership check in `send` — they are
   guards, and a guard that lives elsewhere is a guard a reader misses.

   **Do not change which exception each arm catches or what it falls back to.** The four arms are
   the transport's degradation behaviour.

   Tests: `tests/src/channels/test_telegram_transport.py`,
   `tests/scripts/test_w2_transport_gate.py`.

6. **OPTIONAL — the settings-vs-env ruling (C8).** Not a required step; the builder decides and
   records the decision in the PR, or defers it to its own PR. The situation: routes read the
   Telegram variables through pydantic `settings` (`routes/webhooks.py:162-165`, `routes/v1.py:93`)
   while the factory reads raw `env` (`app.py:413-414,:447,:536`), so `create_app(env=…)` cannot
   arm the webhook door and the tests monkeypatch both surfaces.

   | End state | What changes | What it costs |
   |---|---|---|
   | **A — everything through `settings`** | The factory's three raw `env` reads become `settings` reads; `create_app(env=…)` either goes or becomes a settings override | `settings` is process-global, so a test that wants two configurations in one process loses the seam `env=` gives it; `.env` resolution order (`settings.py:145`) becomes the only path |
   | **B — everything through the injected `env`** | The two route reads take `env` from `request.app.state`, set once by the factory | Routes gain a dependency on app state for config; but `create_app(env=…)` becomes true end to end and the double monkeypatch in tests collapses to one |

   B is the smaller behavioural surface and matches the factory's existing intent; A is closer to
   how the rest of the codebase reads config. **Whichever is chosen, it is a separate commit with
   its own test run**, because it changes which source a running process reads.

7. **CHANGELOG** — under `## [Unreleased]` → `### Changed`:

   > **The API's health routes live in a router, `doctor`'s checks are functions, and the
   > transport's send splits (#1216).** `/health`, `/health/scheduling` and `/health/posting` were
   > the only routes in the app defined inline inside `create_app` rather than in a module under
   > `src/api/routes/`; they now have one, included beside the other seven, with every payload
   > field unchanged. The Telegram webhook registration and its live sampler move out of the API's
   > composition root into `src/channels/telegram_webhook_registration.py`, which already owned
   > the rule they duplicated. The CLI's `doctor` ran six try/except ladders in one 215-line body
   > and now calls six check functions that return the same rows in the same order, with the same
   > `--json` envelope and the same exit codes. And `for_chat.send` gives up its media fetch and
   > its card send while keeping its two guards and all four fallback arms exactly as they were.

## Test Plan

- **Pre-change baseline:** `REQUIRE_TEST_DATABASE=1 pytest --no-cov` on the parent commit — green,
  pass count recorded; identical after.
- **Characterization:** `tests/src/api/test_app_factory.py` (the factory and its wiring),
  `tests/src/test_target_health_endpoint.py` (the payloads),
  `tests/storydump_cli/test_env.py` (1,249 lines; the eight doctor tests named in step 4),
  `tests/src/channels/test_telegram_transport.py` + `tests/scripts/test_w2_transport_gate.py`.
- **Targeted, per step:**
  ```bash
  REQUIRE_TEST_DATABASE=1 pytest tests/src/api/test_app_factory.py \
      tests/src/test_target_health_endpoint.py --no-cov                      # steps 2–3
  pytest tests/storydump_cli/test_env.py -k doctor --no-cov                  # step 4
  REQUIRE_TEST_DATABASE=1 pytest tests/src/channels/ \
      tests/scripts/test_w2_transport_gate.py --no-cov                       # step 5
  ```
- **New pins:** none required. If step 2 tempts you to add a payload-shape test, note that audit
  finding C1 is exactly that missing pin and is flagged — mention it rather than fixing it here.
- **Post-change:** the full suite; pass count compared.

## Verification Checklist

- [ ] 04 has landed
- [ ] baseline green on the parent commit, **pass count recorded**
- [ ] **middleware order unchanged** — diff `app.add_middleware` lines before and after and say so in the PR
- [ ] every `app.state.*` key still set, and set in the same place relative to the engine
- [ ] no health payload field added, removed or renamed
- [ ] `doctor`'s rows, order, `--json` envelope and exit codes unchanged; the eight named tests green
- [ ] call-site audit incl. `tests/mutations/*.sh`
- [ ] full suite green, **pass count identical**
- [ ] `ruff check . && ruff format --check .`
- [ ] manual smoke: `storydump doctor` and `storydump health` against the deployed API — both are read-only verbs (`AGENTS.md`), and they are the two surfaces this PR reshapes. Paste both outputs in the PR
- [ ] `CHANGELOG.md` entry under `[Unreleased]`
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

- **Do not change the middleware order.** Starlette prepends, so the last added runs first — the
  comment at `app.py:649` says it and the security headers depend on it.
- **Do not fix `/health.version`'s `"0.2.0"`** while moving the health routes. It is audit
  finding C5, flagged.
- **Do not fix the `taps.executed`/`taps.replayed` mismatch** between `/health` and the CLI
  renderer. Finding C1, flagged, and it is the reason step 2 must not touch payload fields.
- **Do not register or deregister the production webhook** to test step 3. `AGENTS.md` lists both
  as never-run, and `TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER` must never be switched on locally with
  the production token.
- **Do not make step 6's ruling silently** by picking whichever end state makes a test easier.
  Record it, or defer it.
- **Do not move `doctor`'s ordering or verdict** into a helper. They are two lines and they are
  the function's whole remaining job.
- **Do not move the transport's guards** out of `send` (step 5).
- **Do not move service logic into `src/channels/`.** Only the webhook registration and its
  sampler move, and both are channel concerns already.

## Related

- `00_TECH_DEBT.md` — findings TD-C11, TD-C12, TD-C13, TD-C8.
- `01_one-spelling.md` — owns C7, the autoregister off-words `_register_webhook` duplicates.
- `04_rule-of-three-api-cli.md` — lands first.
- `AGENTS.md` §"Services" — the two deployed processes and what each health surface is for.

## Origin

`/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`. Research:
`tech-debt-2026-09-20/research/api-channels-cli.md`, findings TD-C11, C12, C13, C8. `create_app`'s
wiring order, the seven router precedents, `doctor`'s six blocks and its eight function-based
tests, and `for_chat.send`'s four arms were re-opened while writing this plan.
