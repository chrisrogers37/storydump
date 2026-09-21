---
title: "Re-home the worker's session factories so six cycle-breaking imports become ordinary ones, and delete the cycle comments that were never true"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:medium, worker, services]
links: []
---

# 05 — Imports and homes

| | |
|---|---|
| **PR title** | refactor(worker): the session factories move to the unit of work, and six cycle-breaking imports stop being necessary |
| **Risk** | Low — a pure move plus import rewrites; no SQL, no transaction boundary and no GUC changes |
| **Effort** | M (≈4h) |
| **Files modified** | `src/services/target/unit_of_work.py`, `work_loop.py`, `credential_lifecycle.py`, `drive_credentials.py`, `media_sync.py`, `offboarding.py`, `scheduler.py`, `reconciler.py`, `src/worker.py`, their tests, `CHANGELOG.md` |
| **Findings addressed** | TD-A12, TD-B8 |
| **Depends on** | 02 (it deletes modules and surfaces; this PR rewrites import lines and should not fight those deletions) |
| **Blocks** | 06 (`_run_job`'s decomposition reads cleaner once the factory import is ordinary) |

## Summary

`work_loop.py` owns two session factories — `make_session_for` and `poller_session_factory` — and
also imports `credential_lifecycle`, `media_sync`, `offboarding`, `outbox`, `prompts` and six
more service modules at the top of the file. Any of those modules that needs a factory therefore
*cannot* import `work_loop` at module level, and six call sites work around it with an
import inside the function. The cycle is real; the home is wrong. Both factories do one thing —
build an `async_sessionmaker` and apply the GUC invariant through `unit_of_work.apply_gucs` — and
`unit_of_work.py` imports nothing from the service layer (only `settings`, `vocabulary`,
`logger`, `exceptions`), so moving them there dissolves the cycle rather than routing around it.
Two further comments in `media_sync.py` claim a cycle that does not exist. What must not change:
which GUCs are applied, when the transaction opens and commits, and the actor each factory sets.

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-B8 | `work_loop.py:1036-1059` (`make_session_for`), `:1061-1079` (`poller_session_factory`) | Factories homed in a module that imports half the service layer, forcing 6 in-function imports |
| TD-B8 | `credential_lifecycle.py:159,:244,:321`, `drive_credentials.py:100`, `media_sync.py:325`, `offboarding.py:369` | The six forced imports |
| TD-B8 | `media_sync.py:273-275` | "these modules reach back into this one, so a module-level import is a cycle" — neither `outbox` nor `prompts` imports `media_sync`; the comment is false |
| TD-A12 | `scheduler.py:193`, `reconciler.py:365`, `media_sync.py:668` | Three `_json` helpers, each with `import json` inside the function — **narrowed**, see step 5 |
| TD-A12 (**withdrawn**) | "3 stdlib in-function imports" | Re-verified: `ruff --select PLC0415` finds no stdlib in-function import in `src/` beyond the three `import json` above, which step 5 covers |

## Dependencies

- **Depends on 02.** That PR deletes `identity_provisioning.py`, `workspace_provisioning.py`,
  `web_sessions.py` and `sync_tx.py`; editing import lines in files that are about to be deleted
  wastes the work and creates a conflict. Land 02 first, then re-run step 1's inventory.
- **Blocks 06** only softly: `_run_job` is easier to read once its neighbourhood's imports are
  ordinary, but 06 does not strictly require this PR.

## Implementation Plan

### Steps

1. **Record the inventory** in the PR — it is this PR's before/after evidence:

   ```bash
   ruff check src --select PLC0415 --output-format concise | wc -l          # 56 before
   grep -rn "from src.services.target.work_loop import poller_session_factory" src   # 6 before, 0 after
   grep -rn "make_session_for\|poller_session_factory" src tests --include='*.py'
   grep -rn "make_session_for\|poller_session_factory" tests/mutations/*.sh          # must be empty; if not, those scripts embed the call text and need the same edit
   ```

2. **Prove the destination is cycle-free before moving anything** — paste this too:

   ```bash
   grep -nE "^(import|from)" src/services/target/unit_of_work.py
   grep -c "work_loop" src/services/target/unit_of_work.py      # expect 0
   ```

   `unit_of_work.py` imports `contextvars`, `contextlib`, `typing`, `urllib.parse`, `sqlalchemy`,
   `src.config.settings`, `src.services.target.vocabulary`, `src.utils.logger`,
   `src.exceptions.base` — nothing from the service layer, so no service module importing it can
   create a cycle. It already imports `asynccontextmanager` (`:69`) and `async_sessionmaker`
   (`:74`), which are exactly what the two factories need, so the move adds no import.

3. **Move `make_session_for`** — from `work_loop.py:1036-1059` to `unit_of_work.py`, verbatim,
   placed after `create_engine`. Keep the docstring and update only the sentence that explains
   its location.

   Before (`work_loop.py:1036-1043`):
   ```python
   def make_session_for(engine):
       """Per-job transaction contexts with the GUC invariant applied once.

       Tenant scope comes from the claimed row (system singletons carry none and
       get an empty tenant id — fail-closed under any tenant policy); the actor
       is `system`, the `02` §4 worker actor. Lives here (phase 3b) because the
       sender executor takes its own short transactions through it.
       """
   ```
   After (in `unit_of_work.py`):
   ```python
   def make_session_for(engine):
       """Per-job transaction contexts with the GUC invariant applied once.

       Tenant scope comes from the claimed row (system singletons carry none and
       get an empty tenant id — fail-closed under any tenant policy); the actor
       is `system`, the `02` §4 worker actor. Lives here beside `apply_gucs`,
       which is the whole of its body: homed in `work_loop` (phase 3b) it forced
       six service modules to import the work loop from inside a function,
       because `work_loop` imports them at module level (#1216 audit, 2026-09-20).
       """
   ```
   The body is unchanged, except that `unit_of_work.apply_gucs(...)` becomes the local
   `apply_gucs(...)`.

4. **Move `poller_session_factory`** the same way — `work_loop.py:1061-1079` → `unit_of_work.py`,
   directly after `make_session_for`. Its docstring already explains the SET LOCAL/pool-reuse
   reason and needs no change beyond the same "lives here" sentence.

   Then rewrite the seven import sites:

   | File | Before | After |
   |---|---|---|
   | `src/worker.py:57` | `make_session_for,` (in the `from src.services.target.work_loop import (...)` block) | move the name into the `unit_of_work` import |
   | `work_loop.py:420` | `sessions = make_session_for(deps.engine)` | `sessions = unit_of_work.make_session_for(deps.engine)` (it already imports `unit_of_work` at `:44`) |
   | `credential_lifecycle.py:159,:244,:321` | `from src.services.target.work_loop import poller_session_factory` (inside the function) | delete the three lines; add `poller_session_factory` to the module-level `unit_of_work` import |
   | `drive_credentials.py:100` | same | same |
   | `media_sync.py:325` | same | same |
   | `offboarding.py:369` | same | same |

   Each of those four modules must be checked for how it already imports `unit_of_work` — some
   import the module, some import names from it. Follow the file's existing style.

5. **The three `_json` helpers — narrowed, and not into a shared helper.** Re-reading them shows
   two identical trivial wrappers and one that is not:

   ```python
   # src/services/target/scheduler.py:193          # src/services/target/reconciler.py:365
   def _json(value) -> str:                        def _json(payload) -> str:
       import json                                     import json
       return json.dumps(value)                        return json.dumps(payload)

   # src/services/target/media_sync.py:668  — NOT the same
   def _json(value) -> str:
       import json
       return json.dumps(value if value is not None else {"v": 2})
   ```

   Two identical one-line wrappers are a coincidence, not a rule-of-three case, and a shared
   module for `json.dumps` would be worse than the duplication. Do this instead:
   - Hoist `import json` to module level in all three files (stdlib; no cycle; this is the only
     thing actually wrong with them).
   - Delete `scheduler._json` and `reconciler._json`, calling `json.dumps(...)` directly at their
     call sites (`grep -n "_json(" src/services/target/scheduler.py src/services/target/reconciler.py`
     for the list).
   - **Keep `media_sync`'s** and rename it to say what it does — `_payload_json` — because the
     `{"v": 2}` default for `None` is behaviour, and a reader who sees `_json` in three files
     will assume all three are the same. Add one line to its docstring naming the default.
   - Leave `google_drive_adapter._json_body` and `ig_login_oauth._json_object` alone — they parse
     httpx responses, are a different concern, and belong to doc 03's B12 step.

6. **Delete the false cycle comment and hoist its imports** — `media_sync.py:273-275`:

   Before:
   ```python
       # Local imports, matching `_run_sync` below: these modules reach back into
       # this one, so a module-level import is a cycle.
       from src.services.target import outbox, prompts
   ```
   After: delete both comment lines and the import; add `outbox, prompts` to the module-level
   import block. Prove the claim false first and paste it:
   ```bash
   grep -nE "^(from|import).*media_sync" src/services/target/prompts.py src/services/target/outbox.py   # expect no output
   ```
   Do the same for `media_sync.py:324`'s `from src.services.target import prompts` inside
   `_run_sync`. **Note:** the `poller_session_factory` import on the next line (`:325`) is a
   *real* cycle break until step 4 lands — do step 4 first, then this one, so the file never has
   a module-level import of `work_loop`.

7. **Audit the remaining in-function imports rather than hoisting them blindly.** After steps 3–6
   the count drops from 56 by roughly 12. For each of the rest, the builder classifies it in the
   PR body as one of: *real cycle* (leave, and make sure the comment names the direction),
   *deliberate seam* (leave; a test monkeypatches the module at call time), *lazy-load for import
   cost* (leave; `src/worker.py`'s 11 are the candidates here — check each against
   `tests/src/test_worker_entrypoint.py`, which pins what the entrypoint imports eagerly), or
   *no reason* (hoist). **Do not hoist anything in `src/worker.py` without reading
   `src/main.py:12-16` first** — the eager-import discipline there is load-bearing and pinned.
   A one-line table in the PR is the deliverable; hoisting the "no reason" set is optional and
   may be deferred.

8. **CHANGELOG** — under `## [Unreleased]` → `### Changed`:

   > **The worker's session factories live with the unit of work (#1216).**
   > `make_session_for` and `poller_session_factory` build a sessionmaker and apply the GUC
   > invariant — they are unit-of-work concerns — but they lived in `work_loop`, which imports
   > eleven service modules at the top of the file. Six of those modules needed a factory and
   > could only reach it by importing the work loop from inside a function. Both factories now
   > sit beside `apply_gucs` in `unit_of_work.py`, which imports nothing from the service layer,
   > and the six cycle-breaking imports are ordinary module-level ones. Two comments in
   > `media_sync.py` asserting a cycle that never existed (neither `outbox` nor `prompts` imports
   > it) are gone with their imports, and the `import json` inside three helpers moved to the top
   > of those files; `media_sync`'s wrapper is now `_payload_json`, named for the `{"v": 2}`
   > default that made it different from the two it was mistaken for. No GUC, transaction
   > boundary or actor changed.

## Test Plan

- **Pre-change baseline:** `REQUIRE_TEST_DATABASE=1 pytest --no-cov` on the parent commit — green,
  pass count recorded. The count must be identical afterwards: this PR moves code, it does not
  add or remove a test.
- **The characterization tests** for the factories are the gates that run jobs through them —
  name these in the PR and run them after each step:
  `tests/src/services/target/test_work_loop.py`, `tests/src/test_worker.py`,
  `tests/scripts/test_w1_worker_gate.py`, `tests/scripts/test_w5de_credential_lifecycle.py`,
  `tests/scripts/test_w6_sync_gate.py`, `tests/scripts/test_outbox_sender_gate.py`. The GUC
  invariant itself is pinned by `tests/src/services/target/test_unit_of_work.py` and the RLS
  gates — run those too, since the factories now live in that module.
- **Targeted:**
  ```bash
  REQUIRE_TEST_DATABASE=1 pytest tests/src/services/target/test_unit_of_work.py \
      tests/src/services/target/test_work_loop.py tests/src/test_worker.py \
      tests/src/test_worker_entrypoint.py --no-cov
  REQUIRE_TEST_DATABASE=1 pytest tests/scripts/ --no-cov
  ```
- **New pins:** none. (A pin that the factories are importable without `work_loop` would be
  satisfied by any test that imports `unit_of_work` — which the suite already does.)
- **Post-change:** the same commands, plus `ruff check src --select PLC0415 | wc -l` showing the
  reduced count, and step 1's `grep` for the forced import returning `0`.

## Verification Checklist

- [ ] 02 has landed (or its deletions are confirmed not to touch these files)
- [ ] baseline `REQUIRE_TEST_DATABASE=1 pytest --no-cov` green on the parent commit, pass count recorded
- [ ] step 2's cycle-free proof for `unit_of_work.py` pasted in the PR (`grep -c work_loop` = 0)
- [ ] step 6's proof that `prompts`/`outbox` do not import `media_sync` pasted in the PR
- [ ] call-site audit: `grep -rn "make_session_for\|poller_session_factory" src tests --include='*.py'` shows the new home only; `tests/mutations/*.sh` checked and empty
- [ ] targeted gates green, then the full suite green with an **identical pass count**
- [ ] `ruff check . && ruff format --check .`; `PLC0415` count reduced and reported
- [ ] manual smoke: `python -c "import src.worker, src.services.target.work_loop, src.services.target.media_sync; print('ok')"` — proves no import cycle was introduced
- [ ] `CHANGELOG.md` entry under `[Unreleased]`
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

- **Do not hoist the six `poller_session_factory` imports without moving the function.** The cycle
  is real: `work_loop.py:33` imports `credential_lifecycle`, `email_sender` and `media_sync` at
  module level. Hoisting alone turns a working import into an `ImportError` at startup.
- **Do not change the GUC set, the actor, or where the transaction opens and commits.**
  `make_session_for` opens `session.begin()` and applies the GUCs inside it;
  `poller_session_factory` applies them without opening one, because the poller opens its own per
  tick. That difference is deliberate and documented — preserve both exactly.
- **Do not extract a shared `_json`.** Step 5 explains why two identical one-line wrappers are not
  a rule-of-three case, and `media_sync`'s is not identical.
- **Do not flatten `media_sync._json`'s `{"v": 2}` default** into a plain `json.dumps`. It is
  behaviour.
- **Do not hoist anything in `src/worker.py`** without reading `src/main.py:12-16` and
  `tests/src/test_worker_entrypoint.py`. The eager-import discipline is load-bearing (#979) and
  pinned.
- **Do not set `_IN_TRANSACTION` in either factory while you are in there.** That is audit finding
  B2 — flagged for an owner ruling, because switching it on may start raising on an existing
  violation. This PR moves the factories; it does not change what they guard.
- **Do not touch `google_drive_adapter._json_body` or `ig_login_oauth._json_object`** — doc 03.

## Related

- `00_TECH_DEBT.md` — findings TD-A12 and TD-B8.
- `02_dead-lane-and-surfaces.md` — lands first.
- `03_rule-of-three-services.md` — owns the httpx response-parsing helpers this doc leaves alone.
- `06_publish-pipeline-shape.md` — reads more cleanly after this.
- `.claude/rules/scheduler.md` — the worker's clock, jobs and publish pipeline.

## Origin

`/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`. Research:
`tech-debt-2026-09-20/research/pipeline-worker.md` (TD-A12) and `research/integrations-identity.md`
(TD-B8). The cycle direction, the destination's import list, the false `media_sync` comment and
the three `_json` bodies were each re-verified during planning; the "3 stdlib in-function imports"
sub-finding was withdrawn for lack of evidence.
