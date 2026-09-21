---
title: "Docstrings that describe a system that no longer exists, and one module named for the wrong health"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:low, services, worker]
links: []
---

# 12 — Stale words and misleading names

| | |
|---|---|
| **PR title** | docs(src): the docstrings catch up with the tier that exists, and the worker's probe is named for the worker |
| **Risk** | Low — prose and one module rename with two importers; no logic changes at all |
| **Effort** | S (≈3h, most of it reading to get each replacement sentence right) |
| **Files modified** | ~15 modules' docstrings, `src/services/target/health.py` → `worker_health.py`, `src/worker.py`, `Makefile`, `setup.py`, `CHANGELOG.md` |
| **Findings addressed** | TD-A20, TD-B18, TD-C16 (docstring part), TD-C20, TD-O6 |
| **Depends on** | 02 (the deletions land first, so the words describe the end state) |
| **Blocks** | nothing |

## Summary

In this codebase the docstring is the spec — the modules carry long explanatory headers citing
PR numbers and rulings, and that is the house style, not clutter. Which is exactly why a header
asserting a present-tense fact that stopped being true is a defect: it is a wrong spec, stated
with the same authority as a right one. Six module headers say things the September tear-out and
the increments before it made false — that nothing reaches a real Meta endpoint, that connections
are "deliberately absent", that no composition root exists, that zero channel bindings have been
delivered. Separately, `src/services/target/health.py` is the *worker's* raw-socket liveness
probe, sitting in the services package beside the API's `/health`, the CLI's `health` verb and two
fleet monitors — and both of its importers already alias it `health_endpoint` to cope. **One
finding is moved out of this doc to the flagged list** (B19 — see below); nothing here changes
behaviour.

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-B18 | `meta_adapter.py:4-11` | "Nothing reaches a real Meta endpoint until M.3… The real Graph adapter is deliberately NOT here" — `instagram_graph.py::InstagramGraphAdapter` (`:89`) is that adapter and it exists |
| TD-B18 | `provisioning.py:26-31` | "Connections are milestone 2 and are deliberately absent" — connections are built (`ig_login_oauth`, `meta_callbacks`, the `connect_account` executor) |
| TD-B18 | `webhook_ingress.py:53-57` | "#903 stays open: no composition root exists yet, and this increment does not create one" — there are two (`src/worker.py`, `src/api/app.py`) |
| TD-B18 | `bindings.py:3-15` | "zero has been delivered… Everything downstream is already built and inert" — `channel_bind.py` writes bindings and the outbox runs |
| TD-B18 | `egress.py:125-127`, `email_sender.py:46-52` | Both say the host allowlist is "the load-bearing control **until #871 lands**" — verify #871's state before rewording (step 3) |
| TD-A20 | `transit.py:4,:72,:75,:212,:271`; `jobs.py:432`; `commands.py:3`; `publish_pipeline.py:58,:1583`; `publish_cap.py:302`; `provider_ops.py:84`; `vocabulary.py:451`; `prompts.py:230`; `worker.py:645,:673` | References to torn-out code; name clashes (`TERMINAL_STATES`, `window_start`); `text` shadowing; an undeclared `health_server` |
| TD-C16 | `encryption.py:126,:132,:139-166` | A docstring naming legacy tables |
| TD-C20 | `src/services/target/health.py`; importers `worker.py:53`, `tests/src/test_target_health_endpoint.py:15` | The worker's probe named `health.py`; both importers already alias it `health_endpoint` |
| TD-O6 | `Makefile` (6 targets), `setup.py:8` | `./venv/bin/pytest` vs the `.venv/` checkout; `author="Your Name"` |
| TD-B19 (**moved to flagged**) | `identity.py:65,:261` (`hashtext`) vs `provisioning.py:425,:495`, `category_mix.py:186` (`hashtextextended`) | Not a cleanup — see below |

**B19 belongs on the flagged list, not in this PR.** `provisioning.py:484-487` documents the wider
hash deliberately: "`hashtextextended` rather than `hashtext` because the 32-bit variant collides
often enough at estate scale to serialize unrelated folders now and then." Unifying the two is
defensible — but changing a lock's hash function changes the *key value*, so during a rolling
deploy the old and new processes would take **different** advisory locks for the same logical key,
and the lock would not hold across that window. That is a behaviour change with a deploy hazard;
it needs an owner ruling and a deploy plan, not a docstring PR. Add it to `00_TECH_DEBT.md`'s
Questions list.

## Dependencies

- **Depends on 02.** That PR deletes modules and surfaces; a docstring rewritten before the
  deletion describes a state that lasts one commit. Land 02 first, then write these sentences
  against the end state.

## Implementation Plan

### Steps

1. **Verify each claim before rewriting it.** For every header below, the builder runs the check
   and pastes the result in the PR. A docstring corrected on assumption is the same defect with a
   newer date.

   ```bash
   grep -n "class InstagramGraphAdapter" src/services/target/instagram_graph.py      # B18/meta_adapter
   grep -rn "connect_account\|reconnect_account" src/services/target/commands.py     # B18/provisioning
   grep -n "def main" src/worker.py src/api/app.py                                   # B18/webhook_ingress
   grep -rn "INSERT INTO channel_bindings" src/services/target/                      # B18/bindings
   gh issue view 871 --json state,title                                              # B18/egress + email_sender
   ```
   (`gh` needs the Bash sandbox disabled in this environment — run it outside, or ask the owner
   for #871's state.)

2. **Rewrite the four provably-stale headers.** Keep the house voice: say what is true now, cite
   what changed. Suggested replacements — adjust to what step 1 found:

   - `meta_adapter.py:4-11` → state that this module owns the *classification* seam and the stub
     used by the gates, and that the real Graph adapter is
     `instagram_graph.py::InstagramGraphAdapter`, built since; delete the "nothing reaches a real
     Meta endpoint until M.3" paragraph, which is now the opposite of the truth.
   - `provisioning.py:26-31` → keep the first sentence (the `api_publishing_enabled` /
     `approval_mode` defaults are still the closed-loop gate) and replace "Connections are
     milestone 2 and are deliberately absent" with where connections now live.
   - `webhook_ingress.py:53-57` → keep the ack-SLO paragraph; replace the "no composition root
     exists yet" clause with the two that do, and restate #903's status as it actually is.
   - `bindings.py:3-15` → the "What this closes" section measured zero bindings at `e057063`;
     replace it with what the module does now, keeping the `0..n`-per-workspace ratification note
     (that is still the design) and dropping the "zero has been delivered" measurement.

3. **The `#871` pair** — `egress.py:125-127` and the quotation of it at `email_sender.py:46-52`.
   If #871 is closed, both sentences need rewording **and they must agree** — `email_sender`
   quotes `egress`'s own comment, so fix `egress` first and make the quotation match. If #871 is
   open, both are correct and this step is a no-op; say so in the PR.

4. **The A20 sweep.** For each site, read it and either correct the sentence or record why it is
   already fine. These are individually small; the discipline is that each one is *read*, not
   pattern-replaced:
   - `transit.py:4,:72,:75,:212,:271`, `jobs.py:432`, `commands.py:3`,
     `publish_pipeline.py:58,:1583`, `publish_cap.py:302`, `provider_ops.py:84`,
     `vocabulary.py:451`, `prompts.py:230`, `worker.py:645,:673` — references to code the
     tear-out removed.
   - `publish_pipeline.py:1583` is `_await_ready`'s docstring, which names three return values
     where the code has four. **Doc 06 fixes this one** as part of moving its callers — check
     whether 06 has landed and skip it here if so.
   - The `TERMINAL_STATES` name clash resolves itself once doc 01 makes every site import the one
     in `intent_ledger`; confirm and note it rather than editing.
   - `window_start`, the `text` shadowing and the undeclared `health_server` are *naming*, not
     prose: fix the shadowing (`text` shadows the SQLAlchemy import) only if `ruff` is silent on
     it today and the rename is local to one function; otherwise leave them and say why.

5. **`encryption.py`'s legacy-table docstring** — `:126`, `:132`, `:139-166`. Replace the legacy
   table names with the target ones the module actually encrypts for. (The two f-string log calls
   the scan also flagged at `logger.py:74-87` are **out of scope** unless
   `.claude/rules/development-patterns.md` states a lazy-`%s` logging rule — check it; if it does
   not, leave them.)

6. **Rename the worker's probe** — `src/services/target/health.py` → `worker_health.py`. It is a
   raw-socket liveness endpoint for the worker process, and it sits in a package where "health"
   already means three other things (`/health` in the API, `storydump health`, and the
   `scheduling_health`/`posting_health` views beside it). Both importers already alias it:
   ```
   src/worker.py:53                          from src.services.target import health as health_endpoint
   tests/src/test_target_health_endpoint.py:15   from src.services.target import health as health_endpoint
   ```
   Use `git mv`, update the two imports (the alias can now go — `worker_health` reads correctly
   on its own), and grep the docs:
   ```bash
   git mv src/services/target/health.py src/services/target/worker_health.py
   grep -rn "target import health\b\|target\.health\b\|target/health\.py" src storydump_cli scripts tests documentation AGENTS.md CLAUDE.md README.md .claude
   grep -rn "target/health\|target import health" tests/mutations/*.sh
   ```
   `tests/test_agent_docs.py` fails when a LIVE page names a deleted module path, so any hit under
   `documentation/guides/`, `documentation/operations/`, `AGENTS.md`, `CLAUDE.md`, `README.md` or
   `.claude/` must be updated in this PR. Consider renaming the test file to match
   (`tests/src/test_worker_health_endpoint.py`) — optional, but it is the file the rename is for.

7. **The Makefile and `setup.py`** — `Makefile`'s `test`, `test-unit`, `test-integration`,
   `test-quick`, `test-failed`, `test-watch` call `./venv/bin/pytest` while this checkout uses
   `.venv/`. Add a variable with a fallback rather than switching the hardcoded path (a
   contributor may have either):
   ```make
   VENV ?= $(if $(wildcard .venv/bin/pytest),.venv,venv)
   ```
   and use `$(VENV)/bin/pytest` in the six targets. Also check whether anything still writes
   `logs/app.log` (`grep -rn "logs/app.log\|app\.log" src/utils/logger.py src/`); if nothing does,
   delete the `logs` target. And `setup.py:8`: `author="Your Name"` → the maintainer.
   (If doc 10 has landed, it already fixed `author` — check and skip.)

8. **CHANGELOG** — under `## [Unreleased]` → `### Changed`:

   > **The docstrings describe the tier that exists (#1216).** In this codebase the module header
   > is the spec, which is why six of them asserting things that stopped being true were a
   > problem and not a cosmetic one: that nothing reaches a real Meta endpoint (the Graph adapter
   > has been built), that connections are "deliberately absent" (they are wired), that no
   > composition root exists (there are two), that zero channel bindings have been delivered (the
   > outbox runs on them). Each was re-read against the code and rewritten to what is true, with
   > what changed cited. The worker's raw-socket liveness probe is now `worker_health.py`: it sat
   > in the services package as `health.py` beside the API's `/health`, the CLI's `health` verb
   > and the two health views, and both of its importers already aliased it `health_endpoint` to
   > cope. The Makefile's test targets find either `venv/` or `.venv/`.

## Test Plan

- **Pre-change baseline:** `REQUIRE_TEST_DATABASE=1 pytest --no-cov` on the parent commit — green,
  pass count recorded; identical after. Prose changes cannot move it, so any change means the
  rename broke an import.
- **The characterization for a docs PR is the doc guard**:
  `REQUIRE_TEST_DATABASE=1 pytest tests/test_agent_docs.py --no-cov` — it fails when a live page
  names a deleted module path, which is exactly the risk in step 6.
- **Targeted:**
  ```bash
  REQUIRE_TEST_DATABASE=1 pytest tests/test_agent_docs.py \
      tests/src/test_target_health_endpoint.py tests/src/test_worker.py --no-cov
  ```
- **New pins:** none.
- **Post-change:** the full suite; pass count compared. Plus `make test-quick` actually running,
  to prove step 7's variable resolves in this checkout.

## Verification Checklist

- [ ] 02 has landed
- [ ] baseline green on the parent commit, **pass count recorded**
- [ ] step 1's verification output in the PR — one line per claim, including #871's state
- [ ] every rewritten sentence says what is true **now** and cites what changed
- [ ] `git mv` used for the rename (history follows the file)
- [ ] no live page (`documentation/guides/`, `documentation/operations/`, `AGENTS.md`, `CLAUDE.md`, `README.md`, `.claude/`) names `target/health.py`; `tests/mutations/*.sh` checked
- [ ] `pytest tests/test_agent_docs.py --no-cov` green
- [ ] `make test-quick` runs in this checkout (step 7)
- [ ] full suite green, **pass count identical**
- [ ] `ruff check . && ruff format --check .`
- [ ] manual smoke: none — no runtime behaviour changes
- [ ] `CHANGELOG.md` entry under `[Unreleased]`
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

- **Do not unify the two advisory-lock hash functions.** Changing a lock's hash changes its key,
  so a rolling deploy would have old and new processes holding different locks for the same
  logical key. It is on the flagged list now, with a deploy plan owed.
- **Do not rewrite a docstring without reading the code it describes.** The whole finding is that
  someone previously wrote a true sentence and the code moved; repeating that with a 2026 date
  helps nobody.
- **Do not delete an explanatory header because it is long.** The length is the house style, and
  several of these headers record rulings that are still binding — only the false present-tense
  claims change.
- **Do not touch `email_sender.py`'s "parked by design" statement.** The inert sender is
  deliberate (`AGENTS.md`, "What is deliberately not wired"); only the `#871` clause is in scope.
- **Do not change any logic, constant or SQL in this PR.** If a docstring is wrong because the
  code is wrong, that is a different PR — note it and move on.
- **Do not rename `scheduling_health.py` or `posting_health.py`.** They are correctly named for
  the views they serve; only the worker's socket probe is misnamed.
- **Do not edit `documentation/planning/` or `documentation/archive/` pages** to match the new
  module name. Those pages are history and are supposed to name what existed then.

## Related

- `00_TECH_DEBT.md` — findings TD-A20, TD-B18, TD-C16, TD-C20, TD-O6; B19 moves to its Questions
  list.
- `02_dead-lane-and-surfaces.md` — lands first.
- `06_publish-pipeline-shape.md` — fixes `_await_ready`'s docstring as part of moving its callers.
- `08_integrations-shape.md` — fixes the two docstrings its own move makes stale.
- `10_dependencies-and-ci.md` — may already have fixed `setup.py`'s `author`.

## Origin

`/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`. Research:
`tech-debt-2026-09-20/research/pipeline-worker.md` (A20), `research/integrations-identity.md`
(B18, B19), `research/api-channels-cli.md` (C16, C20), `research/orchestrator.md` (O6). All six
docstring headers, both `health.py` importers and the documented reason for `hashtextextended`
were re-opened while writing this plan; B19 was moved to the flagged list as a result.
