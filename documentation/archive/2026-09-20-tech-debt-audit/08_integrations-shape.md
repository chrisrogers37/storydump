---
title: "Split the sync and the Drive walk along the phases they already name, and give the tier's OAuth-state machinery its own module"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:medium, services]
links: []
---

# 08 — The integrations' shape

| | |
|---|---|
| **PR title** | refactor(integrations): `_run_sync` and `list_changes` split along their own phases; the OAuth-state door leaves "Instagram Login" |
| **Risk** | Medium — `_run_sync`'s phases are transaction boundaries, so the split must land exactly on them; bounded by two large gates and by an ordering that keeps the suite green per step |
| **Effort** | L (≈1.5 days) |
| **Files modified** | `src/services/target/media_sync.py`, `google_drive_adapter.py`, `ig_login_oauth.py`, new `oauth_states.py`, plus the eight importers, `CHANGELOG.md` |
| **Findings addressed** | TD-B9, TD-B15 |
| **Depends on** | 03 (service helpers), 05 (re-homes the `poller_session_factory` import at `media_sync.py:325`) |
| **Blocks** | nothing |

## Summary

`_run_sync` is 343 lines and `list_changes` 237, and both are long for the same reason: a
sequence of distinct phases written as one body. `_run_sync` is the easier of the two because it
already names its phases in comments — *"Phase 1 — read the source, own transaction, committed
before the door"*, *"Phase 2 — the provider door, outside any transaction"*, *"Phase 3 —
checkpoint CAS + upsert + chain-or-rearm, one transaction"* — and those comments are also the
module's transaction boundaries, so splitting on them preserves the rule that a transaction never
spans a provider call, structurally rather than by care. Separately, the tier's generic
`oauth_states` machinery lives in a module called "Instagram Login OAuth": Google identity
linking, Google Drive OAuth and Telegram channel binding all reach into it for `issue_state`,
`consume_state`, `OAuthStateRefused` and `ring`. **One sub-finding is withdrawn** — the scan's
"cap-warning block pasted twice" is not in this file (see below).

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-B9 | `media_sync.py:323-665` | `_run_sync`, 343 lines, nesting 5 — but with its three phases already named in comments at `:332`, `:441`, `:508` |
| TD-B9 | `google_drive_adapter.py:292-528` | `list_changes`, 237 lines, 87 statements, complexity 28 |
| TD-B15 | `ig_login_oauth.py:117-319` (`oauth_states` machinery), `:341-349` (`ring`) | Eight modules import it; four of them are not Instagram |
| TD-B9 (**withdrawn**) | "the cap-warning block pasted twice" in `media_sync.py` | `grep -n "cap\|CAP" src/services/target/media_sync.py` returns **nothing**. `FOLDER_WALK_CAP` is in `google_drive_adapter.py:390`, and it appears once. The claim was attributed to the wrong file |

## Dependencies

- **Depends on 03** — it extracts the fan-out loop that `media_sync.py:300` and `:482` both run.
- **Depends on 05** — it makes `media_sync.py:325`'s in-function `poller_session_factory` import
  an ordinary one, and deletes the false cycle comment at `:273-275`. Doing 08 first means
  editing those lines twice.
- Blocks nothing.

## Implementation Plan

### Steps

Each step leaves the suite green on its own.

1. **Inventory and the withdrawal** — record both in the PR:
   ```bash
   grep -n "cap\|CAP" src/services/target/media_sync.py          # empty — the withdrawn sub-finding
   grep -n "FOLDER_WALK_CAP" src/services/target/google_drive_adapter.py   # one definition, one use
   grep -rn "ig_login_oauth" src --include='*.py' | grep -v "^src/services/target/ig_login_oauth.py"
   grep -rn "_run_sync\|list_changes\|issue_state\|consume_state\|OAuthStateRefused" tests/mutations/*.sh   # must be empty
   ```

2. **Split `_run_sync` on its own phase comments** — `media_sync.py:323-665`. The three phases
   and their exact boundaries:

   | Phase | Lines | Comment at | Transaction |
   |---|---|---|---|
   | 1 — read the source | `:332-441` | `:332` | its own, committed before the door |
   | 2 — the provider door | `:441-508` | `:441` | **none** — outside any transaction |
   | 3 — checkpoint CAS + upsert + chain-or-rearm | `:508-665` | `:508` | one |

   Extract each as a module-level function taking the factory and the job, returning what the
   next phase needs:
   ```python
   async def _read_source(factory, job, *, source_id) -> Optional[dict]:
       """Phase 1 — the source row and the walk token, in a transaction of its
       own that commits before the provider door. Split out of `_run_sync`
       (the tech-debt audit, 2026-09-20): the phase comments were already the
       seams, and they are transaction boundaries, so the split makes the
       `02` §5 rule — a transaction never spans a provider call — structural
       rather than a thing each reader has to check."""
   ```
   `_run_sync` becomes the three calls plus the early returns it already has (`:347` no row,
   `:350` paused, `:377` chunk-with-in-flight).

   **The boundaries are not negotiable.** If a phase's code seems to want a value from the next,
   pass it explicitly; do not widen a transaction to make the split tidy. If a split would move
   a line across a commit, stop — that line is in the wrong phase today and fixing it is a
   behaviour change, not this PR.

   Keep every comment with the code it explains — especially the eleven-line note at `:359-370`
   about the stored checkpoint being the cursor of record, which is the module's hardest-won
   fact (owner ruling 2026-09-08).

   Tests: `tests/scripts/test_w6_sync_gate.py` (1,674 lines) — run the whole file after this
   step; `tests/src/services/target/test_drive_workspace_grant.py`.

3. **Split `list_changes`** — `google_drive_adapter.py:292-528`. Its seams, by structure rather
   than by comment: checkpoint validation (`:339-343`), the folder walk incl. the
   `FOLDER_WALK_CAP` guard (`:390-404`), the page fetch (`:476-515`), and the entry mapping
   (`:516-528`). Extract the page fetch and the entry mapping first — they are the most
   self-contained — then reassess whether the folder walk still needs splitting; a 237-line
   function that loses 90 lines may be fine, and over-splitting an adapter into six one-use
   helpers is the KISS failure the audit's own constraints warn about. **State in the PR where
   you stopped and why.**

   Tests: `tests/src/services/target/test_google_drive_adapter.py` (1,495 lines).

4. **Give the OAuth-state machinery its own module** — `ig_login_oauth.py:117-319` and `:341-349`.
   The evidence that it is not Instagram's: of the eight modules importing this file, four use
   only the generic parts —

   | Importer | What it takes | Its actual subject |
   |---|---|---|
   | `identity_link.py:32,68,82,88` | `issue_state`, `consume_state`, `OAuthStateRefused` | Google identity linking |
   | `channel_bind.py:28,65,98,104` | `issue_state`, `consume_state`, `OAuthStateRefused` | Telegram channel binding |
   | `google_drive_oauth.py:83` | `ring` | Google Drive |
   | `drive_credentials.py:58` | `ring` | Google Drive |
   | `ig_credentials.py:36` | `PROVIDER`, `ring` | Instagram (fine either way) |
   | `credential_lifecycle.py:53` | the module, as `oauth` | mixed |
   | `command_executors.py:81` | `issue_state` | the command port |
   | `google_oidc.py:6` | docstring reference only | — |

   Create `src/services/target/oauth_states.py` holding `issue_state`, `consume_state`,
   `OAuthStateRefused` and `ring`, moved verbatim. **Precedent for a module at this scope** is
   ample in the same package — `callback_tokens.py` (72 lines), `rate_counters.py` (96),
   `identity_link.py` (122), `sync_tx.py` (128), `backpressure.py` (138) — cite one in the PR.
   `ig_login_oauth.py` imports from the new module and keeps everything Instagram-specific
   (`PROVIDER`, the token exchange, `store_credential`, `load_credential`, `ig_refresh`).

   Leave a one-line re-export in `ig_login_oauth.py` **only if** the eight call sites cannot all
   be updated in this PR — they can, so do not. Update all of them.

   Call-site audit:
   ```bash
   grep -rn "ig_login_oauth import\|ig_login_oauth\." src tests --include='*.py'
   ```
   before and after; after, no non-Instagram module should name `ig_login_oauth`.

   Tests: `tests/scripts/test_l6_ig_login_oauth.py`, `tests/scripts/test_gdrive_oauth_gate.py`,
   `tests/scripts/test_channel_bindings_writer.py`, `tests/scripts/test_identity_writers.py`
   (note: doc 02 re-homes that last gate — check whether it has landed and use its successor).

5. **Do not rename `ig_login_oauth.py` itself.** After step 4 it is honestly named: what is left
   is Instagram Login OAuth. A rename would touch every remaining importer for no gain.

6. **Fix the two docstrings the move makes wrong** — `google_oidc.py:6` describes the state row
   as living "via `ig_login_oauth.issue_state`", and `google_drive_oauth.py:11` says the module's
   pattern is "(:mod:`ig_login_oauth`)". Point both at `oauth_states`. (Doc 12 owns stale
   docstrings generally; these two are made stale *by this PR*, so they are fixed here.)

7. **CHANGELOG** — under `## [Unreleased]` → `### Changed`:

   > **The sync and the Drive walk are split along the phases they already named, and the OAuth
   > state door leaves "Instagram Login" (#1216).** `_run_sync`'s three phases — read the source
   > in its own transaction, call the provider outside any, then checkpoint and chain in one —
   > were written in comments and are now three functions, so the `02` §5 rule that a transaction
   > never spans a provider call is structural rather than something each reader checks.
   > `list_changes` gives up its page fetch and entry mapping the same way. And the generic
   > `oauth_states` machinery — `issue_state`, `consume_state`, `OAuthStateRefused` and the
   > `ring` — moves to `oauth_states.py`: Google identity linking, Google Drive OAuth and
   > Telegram channel binding were all importing it from a module named for Instagram Login, and
   > now import it from the door it is. No checkpoint, cursor, transaction boundary or refusal
   > changed.

## Test Plan

- **Pre-change baseline:** `REQUIRE_TEST_DATABASE=1 pytest --no-cov` on the parent commit — green,
  pass count recorded; identical after.
- **Characterization:** `tests/scripts/test_w6_sync_gate.py` (1,674 lines) for `_run_sync`;
  `tests/src/services/target/test_google_drive_adapter.py` (1,495) for `list_changes`;
  `tests/scripts/test_l6_ig_login_oauth.py` and `tests/scripts/test_gdrive_oauth_gate.py` for the
  OAuth-state move. Name the classes you rely on in the PR (`grep -n "^class Test" <file>`).
- **Targeted, after each step:**
  ```bash
  REQUIRE_TEST_DATABASE=1 pytest tests/scripts/test_w6_sync_gate.py \
      tests/src/services/target/test_google_drive_adapter.py \
      tests/src/services/target/test_drive_workspace_grant.py --no-cov      # steps 2–3
  REQUIRE_TEST_DATABASE=1 pytest tests/scripts/test_l6_ig_login_oauth.py \
      tests/scripts/test_gdrive_oauth_gate.py tests/scripts/test_channel_bindings_writer.py --no-cov   # step 4
  ```
- **New pins:** none.
- **Post-change:** the full suite; pass count compared.

## Verification Checklist

- [ ] 03 and 05 have landed
- [ ] baseline green on the parent commit, **pass count recorded**
- [ ] step 1's withdrawal evidence in the PR (`grep "cap" media_sync.py` empty)
- [ ] **every `_run_sync` split lands on a transaction boundary** — state this explicitly in the PR, phase by phase
- [ ] step 3 says where the `list_changes` split stopped and why
- [ ] call-site audit: no non-Instagram module names `ig_login_oauth` after step 4; `tests/mutations/*.sh` empty
- [ ] each step's gates green before the next step starts
- [ ] full suite green, **pass count identical**
- [ ] `ruff check . && ruff format --check .`; report the new `C901` for `_run_sync` and `list_changes`
- [ ] manual smoke: none — a sync reaches Google Drive and a credential path reaches Meta; the gates are the evidence. Do not run a sync by hand
- [ ] `CHANGELOG.md` entry under `[Unreleased]`
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

- **Do not move a line across a commit boundary** to make a phase split tidy. The phases are
  transaction boundaries; that is the whole reason the split is safe.
- **Do not widen a transaction to span the provider door.** `02` §5 — checkpoint, commit, then
  call.
- **Do not drop the checkpoint-cursor comment** (`media_sync.py:359-370`). It records an owner
  ruling and it is the fact the module is hardest to get right without.
- **Do not over-split `list_changes`** (step 3). Six one-use helpers is worse than one long
  adapter function.
- **Do not guard `ig_refresh`'s `resp.json()`** while you are in `ig_login_oauth.py` — doc 03
  examined it and excluded it, because guarding changes behaviour on a non-JSON body.
- **Do not leave a compatibility re-export** in `ig_login_oauth.py` (step 4). All eight call
  sites are in this repo and can be updated now; a shim would be the next audit's finding.
- **Do not rename `ig_login_oauth.py`** (step 5).
- **Do not fix `provisioning.py:758` or `workspaces.py:241`** (the hand-spelled `PROVIDER`) here
  — doc 01 owns them.

## Related

- `00_TECH_DEBT.md` — findings TD-B9, TD-B15, and the Corrections table this doc adds a row to.
- `03_rule-of-three-services.md`, `05_imports-and-homes.md` — land first.
- `12_stale-words-and-names.md` — owns stale docstrings generally; step 6 handles only the two
  this PR makes stale.
- `.claude/rules/database.md` — SQL under the unit of work, the tenancy gate.

## Origin

`/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`. Research:
`tech-debt-2026-09-20/research/integrations-identity.md`, findings TD-B9 and TD-B15. The phase
comments, the `FOLDER_WALK_CAP` attribution and all eight `ig_login_oauth` importers were
re-verified while writing this plan; the "cap-warning pasted twice" sub-finding was withdrawn as a
result.
