---
title: "One spelling: hand-copied constants go back to the module that owns them"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:high, services]
links: []
---

# 01 — One spelling: hand-copied constants go back to the module that owns them

| | |
|---|---|
| **PR title** | One spelling: hand-copied constants go back to the module that owns them |
| **Risk** | Low — every edit replaces a literal with a reference to the constant it already equals; no statement's result set, no signature a caller relies on, and no wire value changes |
| **Effort** | M (≈ 5–7 hours) |
| **Files modified** | `src/services/target/{command_executors,scheduler,prompts,publish_pipeline,work_loop,jobs,outbox,bindings,tenant_resolution,channel_bind,membership_sync,invitations,service_tokens,workspaces,provisioning,ig_login_oauth,google_drive_oauth,drive_adapter,media_sync,vocabulary}.py`, `src/worker.py`, `src/channels/{telegram_transport,telegram_webhook_registration}.py`, `src/api/routes/webhooks.py`, `src/api/app.py`, `src/exceptions/tenancy.py`, `storydump_cli/{main,webhook}.py`, `storydump_cli/commands/{__init__,env}.py`, plus the pins under `tests/` named per step |
| **Findings addressed** | TD-A2, TD-A4, TD-A5, TD-A11, TD-A15, TD-B6, TD-B20, TD-C2, TD-C6, TD-C7, TD-C9, TD-C14 |
| **Depends on** | — (opens on `main` at `0966771`) |
| **Blocks** | 02, 03, 04 |

## Summary

Twelve findings are one debt wearing twelve hats: a constant has a declared
owner, and a second hand-written copy of it lives somewhere else. Some of the
copies carry a comment explaining why they had to be copies — and two of those
explanations are measurably false. The cost is always the same shape: the day
somebody retunes the number, adds a state or renames the variable, they edit
the home and the grep for the home's *name* does not find the copies, because a
copy is a string literal.

This PR returns each copy to its owner by reference. It is the audit's first
doc because docs 02, 03 and 04 all extract code that reads these constants, and
an extraction that carries a duplicate forward cements it.

**Nothing may change behaviour.** Every constant in scope already equals its
owner's value today — the verification is that each edit is provably a no-op on
values, and the test suite is the proof. Where a copy has drifted (there is one
candidate: the repost TTL, TD-A1) it is **not** in this PR; see "What NOT To Do".

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-A2 | `intent_ledger.py:85`; copies at `command_executors.py:933`, `scheduler.py:381`, `prompts.py:385`, `publish_pipeline.py:284`, `:618` | The terminal-state tuple is hand-spelled in three SQL statements and two Python tuples beside "the ONE Python home". |
| TD-A4 | `work_loop.py:1099`, `:1114`; owner `jobs.py:127-130` | The sender-job mint writes the interactive lane's `3` attempts, `'10 minutes'` deadline and `LIMIT 200` as SQL literals, and spells `tg:` three times. |
| TD-A5 | `publish_pipeline.py:124`; owner `jobs.py:138` | `DEFAULT_BACKOFF_SECONDS` is a second tuple for `BACKOFF_SECONDS["bulk"]`; the `backoff_seconds=` parameter around it has no caller. |
| TD-A11 | owner `prompts.py:339-360`; copies `outbox.py:619`, `:806`, `work_loop.py:1101` | "Which bindings can carry a push" is four spellings of one SQL predicate. |
| TD-A15 | `worker.py:537`, `:678`; `outbox.py:958`, `:1182`, `:1196`; `work_loop.py:1114`; `publish_pipeline.py:1906`, `:1959` | Operational numbers as literals in door bodies, where `WorkerConfig`/the module constants are the declared home. (`work_loop.py:83 retry_backoff_seconds` is *named* here and deleted in doc 02.) |
| TD-B6 | `ig_login_oauth.py:92`, `provisioning.py:91`, `:760`, `workspaces.py:244`, `:284`, `drive_adapter.py:107`, `google_drive_oauth.py:86`, + inline SQL `command_executors.py:119`, `:1012`, `media_sync.py:145`, `provisioning.py:870` | `"ig_login"` and `"gdrive"` as seven module constants and four SQL literals; one copy's import-cycle justification is false (verified below). |
| TD-B20 | `channel_bind.py:37`, `:39`, `membership_sync.py:29`, `bindings.py:76`, `:114`, `tenant_resolution.py:59`, `invitations.py:82`, `:139`, `service_tokens.py:188`, `workspaces.py:195-197` | Small closed vocabularies spelled two or three times; two hand-rolled `set_config` statements outside `apply_gucs`. |
| TD-B20 (part) | `workspaces.py:713-718`, `command_executors.py:886-892` | `remove_member` raises untyped `LookupError`/`ValueError` and the caller parses `str(exc)`. **Withdrawn from this PR** — see step 7c for the check that was run and why. |
| TD-C2 | `routes/webhooks.py:92` vs `vocabulary.py:389` | The webhook secret header is spelled twice — the route reads one, `storydump webhook status` sends the other — and no test pins them equal. |
| TD-C6 | `telegram_transport.py:50`, `:602`, `telegram_webhook_registration.py:63`, `:75`, `storydump_cli/webhook.py:58` | `RAILWAY_ENVIRONMENT_NAME`, `"production"` and the Bot API base are spelled outside the vocabulary the two sides share. |
| TD-C7 | `app.py:418-423` copies `telegram_webhook_registration.py:71` | `create_app` re-derives `autoregister_enabled`'s private off-words to word its own skip reason. |
| TD-C9 | `exceptions/tenancy.py:44`, `:73` vs `vocabulary.py:133-146` | `TokenRefused.REASONS` and thirteen of `TenantResolutionError.REASONS` are hand copies of the vocabulary's tuples. |
| TD-C14 | `commands/env.py:551`, `:611`, `:645` vs `main.py:56`, `:60`, `:237` | Three CLI fix sentences are hand-copied because `env.py` cannot import `main.py`. |

## Dependencies

- Opens on `main` at `0966771`. Nothing precedes it.
- Blocks **02** (`02_dead-lane-and-surfaces.md`), **03** and **04**: those extract or delete code that reads these constants, and an extraction carrying a duplicate forward makes the duplicate permanent.
- Step 6 (TD-B6) adds names to `src/services/target/vocabulary.py`. That module is the CLI's one `src` import and **must stay stdlib-only** — `tests/storydump_cli/test_import_boundary.py` runs a fresh interpreter to prove it. The names added are plain `str` constants; add nothing else.

## Implementation Plan

### Steps

Each step is one commit and is independently revertible. Steps 1–5 are the
worker's services, 6–7 the integrations/identity services, 8–11 the API and the
channels, 12 the CLI, 13 the CHANGELOG.

---

1. **`TERMINAL_STATES` is bound, not spelled (TD-A2)** — `src/services/target/command_executors.py:926-936`, `scheduler.py:375-388`, `prompts.py:370-397`, `publish_pipeline.py:283-292`, `:618-625`.

   The home is `intent_ledger.py:78-92` and its comment records the exact drift
   this closes. The precedent for binding a Python tuple into SQL as an array is
   `reconciler.py:149-156` (`= ANY(CAST(:ids AS uuid[]))`).

   1a. `command_executors.py` — the module already re-exports the tuple at `:88`.

   Before (`:932-936`):
   ```python
           " WHERE i.workspace_id = :ws AND i.ig_account_id = :acct"
           "   AND i.cancel_requested AND i.state NOT IN"
           "   ('posted','skipped','rejected','expired','failed','cancelled')",
           ws=command.workspace_id,
           acct=account_id,
   ```
   After:
   ```python
           " WHERE i.workspace_id = :ws AND i.ig_account_id = :acct"
           "   AND i.cancel_requested"
           "   AND i.state <> ALL(CAST(:terminal AS text[]))",
           ws=command.workspace_id,
           acct=account_id,
           terminal=list(TERMINAL_STATES),
   ```

   1b. `scheduler.py:379-382` — the `eligible` fragment, used twice (`:419`, `:466`); both call sites already pass `ws` and `acct`, so add `terminal` to both parameter dicts.

   Before:
   ```python
           "                     AND p.ig_account_id = :acct"
           "                     AND p.state NOT IN ('posted','skipped','rejected',"
           "                                         'expired','failed','cancelled'))"
   ```
   After:
   ```python
           "                     AND p.ig_account_id = :acct"
           "                     AND p.state <> ALL(CAST(:terminal AS text[])))"
   ```
   and at each of the two executions add `"terminal": list(intent_ledger.TERMINAL_STATES)` to the bound dict. `scheduler.py` must import `intent_ledger` if it does not already — check with `grep -n "intent_ledger" src/services/target/scheduler.py` and add `from src.services.target import intent_ledger` beside the existing service imports if the grep is empty.

   1c. `prompts.py:385-386` — `prompts.py:49` already imports `intent_ledger`.

   Before:
   ```python
                       "   AND i.state IN ('posted','skipped','rejected','expired',"
                       "                   'failed','cancelled')"
   ```
   After:
   ```python
                       "   AND i.state = ANY(CAST(:terminal AS text[]))"
   ```
   and the bound dict at `:391` becomes
   `{"lim": int(limit), "terminal": list(intent_ledger.TERMINAL_STATES)}`.

   1d. `publish_pipeline.py:283-292` — the routing set is the six terminals plus `review_required`.

   Before:
   ```python
       if state in (
           "posted",
           "failed",
           "cancelled",
           "expired",
           "skipped",
           "rejected",
           "review_required",
       ):
   ```
   After:
   ```python
       if state in (*intent_ledger.TERMINAL_STATES, "review_required"):
   ```

   1e. `publish_pipeline.py:618-625`.

   Before:
   ```python
               if row.state in (
                   "posted",
                   "failed",
                   "cancelled",
                   "expired",
                   "skipped",
                   "rejected",
               ):
   ```
   After:
   ```python
               if row.state in intent_ledger.TERMINAL_STATES:
   ```
   Confirm `publish_pipeline.py` imports `intent_ledger` (`grep -n "intent_ledger" src/services/target/publish_pipeline.py`); add the import if the grep is empty.

   **Call-site audit.** `grep -rn "TERMINAL_STATES" src storydump_cli scripts tests`
   — before: the definition (`intent_ledger.py:85`), the re-export
   (`command_executors.py:88`) and the tests that already name it. After: the
   same, **plus** one reader in each of `command_executors.py`, `scheduler.py`,
   `prompts.py`, `publish_pipeline.py` (×2). Then
   `grep -rn "'posted','skipped','rejected'" src` and
   `grep -rn "\"posted\",$" src/services/target/publish_pipeline.py` — both must
   return **zero** rows in `src/services/target/` after this step.

   **Tests.** Existing gates characterize this: `tests/scripts/test_offboard_gate.py`,
   `tests/scripts/test_w3_prompt_gate.py`, `tests/scripts/test_scheduler_clock_gate.py`,
   `tests/src/services/target/test_command_executors.py`. Add no new test — the
   gates already execute all five statements against real PostgreSQL, which is
   where a mis-bound array would fail. **Run the gates**; a scripted executor
   will not catch a bad `CAST(:terminal AS text[])`.

---

2. **The bulk backoff ladder has one tuple (TD-A5)** — `src/services/target/publish_pipeline.py:123-124`.

   `jobs` is already imported at `:108`.

   Before:
   ```python
   #: `05` row 8, bulk lane: backoff 1/5/15/60 min for retryable failures.
   DEFAULT_BACKOFF_SECONDS = (60.0, 300.0, 900.0, 3600.0)
   ```
   After:
   ```python
   #: `05` row 8, bulk lane: backoff 1/5/15/60 min for retryable failures — the
   #: ladder `jobs.BACKOFF_SECONDS` owns, as floats because this pipeline's rungs
   #: go straight into `timedelta(seconds=…)` (#1325 audit, TD-A5).
   DEFAULT_BACKOFF_SECONDS = tuple(float(s) for s in jobs.BACKOFF_SECONDS["bulk"])
   ```

   Use the `float()` conversion, not a bare alias: the pipeline's tuple is floats
   today and `jobs`' is ints. The conversion makes the values byte-identical to
   what ships, so the step is a provable no-op. Do **not** touch the unused
   `backoff_seconds=` parameter at `:262` — deleting a parameter is a signature
   change and belongs with doc 06's pipeline work.

   **Call-site audit.** `grep -rn "DEFAULT_BACKOFF_SECONDS\|BACKOFF_SECONDS" src storydump_cli scripts tests`
   — before: `publish_pipeline.py:124`, `:262`; `jobs.py:138`, `:151`. After: the
   same four, with `publish_pipeline.py:124` now reading `jobs.BACKOFF_SECONDS`.
   No test names either constant (verified), so nothing else moves.

   **Tests.** `tests/scripts/test_l5_pipeline_gate.py` (the `RETRY_SCHEDULED`
   rungs) characterizes the values. No new test.

---

3. **The sender-job mint reads the lane budget and names its key prefix (TD-A4, and the `LIMIT 200` half of TD-A15)** — `src/services/target/work_loop.py:1094-1118`, `:434`; `jobs.py:127-130`.

   3a. Add the prefix constant beside the lane budgets in `jobs.py`:
   ```python
   #: The serialization key of a binding's sender job: one live sender per
   #: binding, and `work_loop` parses the binding id back off it (#1325, TD-A4).
   SENDER_KEY_PREFIX = "tg:"
   ```

   3b. Add the mint bound to `WorkerConfig` (`work_loop.py:61-129`), beside
   `sender_sweep_seconds`:
   ```python
       #: H5: a sweep is bounded; the next one takes the rest.
       sender_mint_limit: int = 200
   ```

   3c. `work_loop.py:1094-1118`. Note `WorkerConfig` is defined *above*
   `ensure_sender_jobs` in the same module, so the class attribute is a valid
   def-time default — one home, no `or` fallback.

   Before (`:1093-1118`, abridged to the changed lines):
   ```python
   async def ensure_sender_jobs(session) -> int:
       result = await session.execute(
           text(
               "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
               " run_at, max_attempts, deadline_at, payload)"
               " SELECT 'deliver_outbox', b.workspace_id, 'interactive',"
               "        'tg:' || b.id, now(), 3, now() + interval '10 minutes',"
               ...
               "    AND NOT EXISTS (SELECT 1 FROM jobs j"
               "                     WHERE j.serialization_key = 'tg:' || b.id"
               "                       AND j.state IN ('ready', 'leased'))"
               # H5: a sweep is bounded; the next one takes the rest.
               "  LIMIT 200"
           ),
           {"age": outbox.AMBIGUOUS_RESOLVE_AFTER_SECONDS},
       )
       return result.rowcount
   ```
   After:
   ```python
   async def ensure_sender_jobs(
       session, *, limit: int = WorkerConfig.sender_mint_limit
   ) -> int:
       attempts, deadline_seconds = jobs.LANE_BUDGETS["interactive"]
       result = await session.execute(
           text(
               "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
               " run_at, max_attempts, deadline_at, payload)"
               " SELECT 'deliver_outbox', b.workspace_id, 'interactive',"
               "        :prefix || b.id, now(), :attempts,"
               "        now() + make_interval(secs => :deadline),"
               ...
               "    AND NOT EXISTS (SELECT 1 FROM jobs j"
               "                     WHERE j.serialization_key = :prefix || b.id"
               "                       AND j.state IN ('ready', 'leased'))"
               # H5: a sweep is bounded; the next one takes the rest.
               "  LIMIT :lim"
           ),
           {
               "age": outbox.AMBIGUOUS_RESOLVE_AFTER_SECONDS,
               "prefix": jobs.SENDER_KEY_PREFIX,
               "attempts": attempts,
               "deadline": deadline_seconds,
               "lim": int(limit),
           },
       )
       return result.rowcount
   ```
   `make_interval(secs => 600)` is `interval '10 minutes'` — the same
   `make_interval` idiom this statement already uses for `:age` two lines up, so
   it is the module's own spelling.

   3d. `work_loop.py:434` — the parse-back side:
   ```python
           binding_id = str(
               payload.get("binding_id") or job["serialization_key"].split(":", 1)[1]
           )
   ```
   becomes
   ```python
           binding_id = str(
               payload.get("binding_id")
               or job["serialization_key"].removeprefix(jobs.SENDER_KEY_PREFIX)
           )
   ```
   `str.removeprefix` is 3.9+; CI runs 3.10. For a key that carries the prefix
   the two expressions are identical; for one that does not, `split(":", 1)[1]`
   raises `IndexError` where `removeprefix` returns the key unchanged — that
   path is unreachable (the mint is the only writer of this kind's key), so
   keep the behaviour comparable by asserting nothing new. If the builder
   prefers zero risk, use `job["serialization_key"].split(":", 1)[1]` unchanged
   and only replace the two mint-side literals; note the choice in the PR body.

   3e. `worker.py:507` — pass the config's bound:
   ```python
                               self.mints += await ensure_sender_jobs(session)
   ```
   becomes
   ```python
                               self.mints += await ensure_sender_jobs(
                                   session, limit=self._app.config.sender_mint_limit
                               )
   ```

   **Call-site audit.** `grep -rn "ensure_sender_jobs" src storydump_cli scripts tests`
   — before: `work_loop.py:1082` (def), `worker.py:507`,
   `tests/scripts/test_channel_bindings_writer.py:334`,
   `tests/scripts/test_invitation_cards.py:257`,
   `tests/scripts/test_w2_transport_gate.py:98`. After: the same five; the three
   test callers pass no `limit` and keep the default, so **no test file changes**.
   `grep -rn "SENDER_KEY_PREFIX" src storydump_cli scripts tests` — before: zero;
   after: the definition plus three readers in `work_loop.py`.
   `grep -rn "'tg:'" src` — after: **zero**.
   `grep -rn "sender_mint_limit" src tests` — after: the field plus `worker.py:507`.

   **Tests.** Update `tests/src/test_worker.py` where it asserts `WorkerConfig`'s
   field set, if such an assertion exists (`grep -n "WorkerConfig(" tests/src/test_worker.py`).
   Add one unit pin in `tests/src/services/target/test_work_loop.py`: the SQL the
   scripted executor receives binds `attempts` and `deadline` equal to
   `jobs.LANE_BUDGETS["interactive"]` — that is the assertion that reddens the
   day the lane budget is retuned and the sweep is missed.

---

4. **The push-binding predicate has one home (TD-A11)** — owner moves to `src/services/target/bindings.py`; readers `prompts.py:346-360`, `outbox.py:619-622`, `:806-809`, `work_loop.py:1101-1102`.

   **The research file proposes `prompts.py` as the home; that is wrong and must
   not be followed.** `prompts.py:49` imports `outbox`, so `outbox` importing
   `prompts` at module level is a cycle (the two in-function imports at
   `outbox.py:927` and `:1252` carry the `# noqa: PLC0415 — cycle` comment and
   are the proof). `bindings.py` imports only `_dbapi` and `src.exceptions.base`
   — it is a leaf, and it already owns `CHANNELS` ("`ck_bindings_channel`,
   verbatim"), so the predicate over `channel_bindings` belongs there.

   4a. In `bindings.py`, beside `CHANNELS` at `:76`:
   ```python
   #: "Where can we say this": the bindings a push may go to. ONE owner for the
   #: predicate — the W3 sweep, the two one-statement outbox doors and
   #: `prompts.push_bindings` all route on it, and four spellings is how they
   #: drift apart the day a second push channel lands
   #: (`invitation_cards.py:126-131` names that exact risk). A fragment, not a
   #: bound parameter: it is SQL, and no user input reaches it.
   PUSH_BINDING_WHERE = "state = 'active' AND channel LIKE 'telegram%'"
   ```

   4b. `prompts.py:346-360` — add `bindings` to the `from src.services.target import …` line at `:49`.

   Before:
   ```python
                       "SELECT id FROM channel_bindings"
                       " WHERE workspace_id = :ws AND state = 'active'"
                       "   AND channel LIKE 'telegram%'"
   ```
   After:
   ```python
                       "SELECT id FROM channel_bindings"
                       f" WHERE workspace_id = :ws AND {bindings.PUSH_BINDING_WHERE}"
   ```

   4c. `outbox.py:618-622` and `:806-809` — add `from src.services.target import bindings` beside the `rate_counters` import at `:106`.

   Before (both sites, identically):
   ```python
                   "WITH b AS ("
                   "  SELECT id FROM channel_bindings"
                   "   WHERE workspace_id = :ws AND state = 'active'"
                   "     AND channel LIKE 'telegram%'"
                   "), sup AS ("
   ```
   After:
   ```python
                   "WITH b AS ("
                   "  SELECT id FROM channel_bindings"
                   f"   WHERE workspace_id = :ws AND {bindings.PUSH_BINDING_WHERE}"
                   "), sup AS ("
   ```
   (the second site's next line is `"), upd AS ("` — keep it.)

   4d. `work_loop.py:1101-1102`:
   ```python
               "   FROM channel_bindings b"
               "  WHERE b.state = 'active' AND b.channel LIKE 'telegram%'"
   ```
   becomes
   ```python
               "   FROM channel_bindings b"
               f"  WHERE {bindings.PUSH_BINDING_WHERE.replace('state', 'b.state').replace('channel', 'b.channel')}"
   ```
   **Do not do that.** The alias makes the fragment unusable as written. Instead
   give the constant an alias-aware form in `bindings.py` and use it at all four
   sites:
   ```python
   def push_binding_where(alias: str = "") -> str:
       """The predicate, optionally qualified for a table alias."""
       p = f"{alias}." if alias else ""
       return f"{p}state = 'active' AND {p}channel LIKE 'telegram%'"

   #: The unqualified form, for a statement with one `channel_bindings`.
   PUSH_BINDING_WHERE = push_binding_where()
   ```
   Then `work_loop.py:1102` reads
   `f"  WHERE {bindings.push_binding_where('b')}"`, and 4b/4c use
   `bindings.PUSH_BINDING_WHERE` as written above.

   **Call-site audit.** `grep -rn "channel LIKE 'telegram%'" src storydump_cli scripts tests`
   — before: four rows under `src/` (`prompts.py:348`, `outbox.py:621`, `:808`,
   `work_loop.py:1101`) plus whatever the gates spell in their own fixtures.
   After: **zero** under `src/`; the gates' own SQL is untouched.
   `grep -rn "PUSH_BINDING_WHERE\|push_binding_where" src tests` — after: the two
   definitions plus four readers.

   **Tests.** Existing: `tests/scripts/test_customer_notice_gate.py`,
   `tests/scripts/test_channel_bindings_writer.py`,
   `tests/src/services/target/test_work_loop.py`,
   `tests/src/services/target/test_outbox_restate.py`. Add one unit pin in
   `tests/src/services/target/test_work_loop.py` (or a new
   `tests/src/services/target/test_bindings_predicate.py`): assert the four
   statements each contain `bindings.PUSH_BINDING_WHERE` or
   `bindings.push_binding_where("b")` — read the module source with
   `inspect.getsource` rather than executing, modelled on the source-reading pin
   in `tests/src/services/target/test_vocabulary.py:296-305`.

---

5. **Operational numbers get names (TD-A15)** — `src/worker.py:537`, `:678`; `src/services/target/outbox.py:958`, `:1182`, `:1196`; `src/services/target/publish_pipeline.py:1906`, `:1959`.

   `WorkerConfig` (`work_loop.py:61-129`) is the declared home for the worker's
   numbers (`.claude/rules/scheduler.md`: "The numbers are `WorkerConfig`'s
   defaults, passed as parameters to the doors. Do not hardcode one in a service
   or a door body"). A door whose number is not the worker's gets a module
   constant beside its siblings instead — that is the same rule applied to the
   module that owns the door.

   5a. `WorkerConfig` gains two fields (beside `prompt_sweep_seconds` at `:89`):
   ```python
       prompt_sweep_limit: int = 50  # W3 sweep batch (`prompts.sweep_due_prompts`)
       status_interval_seconds: float = 60.0  # cadence of the status line
   ```
   (`sender_mint_limit` was added in step 3.)

   5b. `worker.py:537`:
   ```python
                           counts = await prompts_mod.sweep_due_prompts(session, limit=50)
   ```
   →
   ```python
                           counts = await prompts_mod.sweep_due_prompts(
                               session, limit=self._app.config.prompt_sweep_limit
                           )
   ```
   (`PromptSweeper` already reads `self._app.config.prompt_sweep_seconds` at `:542`, so the attribute is in scope.)

   5c. `worker.py:678`:
   ```python
           _status_reporter(app, stop, 60.0), name="status-reporter"
   ```
   →
   ```python
           _status_reporter(app, stop, app.config.status_interval_seconds),
           name="status-reporter",
   ```

   5d. `outbox.py` — a module constant beside `AMBIGUOUS_RESOLVE_AFTER_SECONDS`
   (find it with `grep -n "AMBIGUOUS_RESOLVE_AFTER_SECONDS" src/services/target/outbox.py`):
   ```python
   #: How many of a binding's aged `ambiguous` rows one pass resolves. Bounded
   #: like every sweep (H5); the next pass takes the rest.
   AMBIGUOUS_RESOLVE_BATCH = 20
   ```
   `:958` becomes `" ORDER BY created_at LIMIT :lim"` with `"lim": AMBIGUOUS_RESOLVE_BATCH` added to the bound dict at `:960`.

   **Do not add a `limit` parameter to `resolve_aged_ambiguous`.** Four test
   modules monkeypatch it with the exact signature
   `async def resolve_aged_ambiguous(session, *, binding_id)`
   (`tests/src/services/target/test_outbox_paced.py:23`, `:359`,
   `test_outbox_destination_gone.py:21`, `test_outbox_restate.py:206`); a new
   keyword would leave those fakes silently non-conforming. The module constant
   names the literal, which is what the rule asks.

   5e. `outbox.py:1182` and `:1196` — name the two fallbacks rather than remove
   them. Beside the other pacing constants:
   ```python
   #: The pacing windows `settle` assumes when a caller passes none (only the
   #: unit seam does; production always passes its budgets).
   DEFAULT_CHAT_WINDOW_SECONDS = 60
   DEFAULT_GLOBAL_WINDOW_SECONDS = 1
   ```
   `:1182` `window_seconds=chat_window_seconds or 60,` →
   `window_seconds=chat_window_seconds or DEFAULT_CHAT_WINDOW_SECONDS,`
   `:1196` `window_seconds=global_window_seconds or 1,` →
   `window_seconds=global_window_seconds or DEFAULT_GLOBAL_WINDOW_SECONDS,`

   The research file proposes making `settle`'s four budget parameters
   **required**. That is a signature change with a test caller
   (`tests/src/services/target/test_outbox_paced.py`) relying on the optional
   form; it is deferred, and the deferral is recorded in "What NOT To Do".

   5f. `publish_pipeline.py:1906`:
   ```python
       tries = int(job.get("attempts") or job.get("max_attempts") or 0) or 5
   ```
   →
   ```python
       tries = (
           int(job.get("attempts") or job.get("max_attempts") or 0)
           or jobs.LANE_BUDGETS["bulk"][0]
       )
   ```
   `jobs.LANE_BUDGETS["bulk"]` is `(5, 6 * 3600)`, so `[0]` is `5` — the same
   value, now named as what it is (this pipeline is the bulk lane).

   5g. `publish_pipeline.py:1959` — beside `DEFAULT_BACKOFF_SECONDS` at `:124`:
   ```python
   #: For rendering a retention window in days.
   SECONDS_PER_DAY = 86400
   ```
   and `:1959` `days = max(1, round(int(older_than_seconds) / 86400))` →
   `days = max(1, round(int(older_than_seconds) / SECONDS_PER_DAY))`.

   5h. **Name only.** `work_loop.py:83 retry_backoff_seconds: float = 60.0` has
   zero readers anywhere in `src` and is **deleted in doc 02**, not here. Leave
   it exactly as it is; a deletion in this PR would make the PR's "no behaviour
   changed" claim harder to verify and would collide with doc 02's dead-surface
   sweep.

   **Call-site audit.** `grep -rn "prompt_sweep_limit\|status_interval_seconds\|AMBIGUOUS_RESOLVE_BATCH\|DEFAULT_CHAT_WINDOW_SECONDS\|DEFAULT_GLOBAL_WINDOW_SECONDS\|SECONDS_PER_DAY" src storydump_cli scripts tests`
   — before: **zero**. After: each name has exactly one definition and one or two
   readers, all under `src/`.
   `grep -rn "limit=50" src/worker.py` and `grep -rn "86400" src/services/target/publish_pipeline.py`
   — after: **zero**.

   **Tests.** `tests/src/test_worker.py` (the sweeper cadences),
   `tests/src/services/target/test_outbox_paced.py`,
   `tests/scripts/test_w3_prompt_gate.py`. If `tests/src/test_worker.py` asserts
   a complete `WorkerConfig` field list, extend it with the two new fields.

---

6. **The provider names live in the vocabulary (TD-B6)** — `src/services/target/vocabulary.py`; readers `ig_login_oauth.py:92`, `provisioning.py:91`, `:760`, `workspaces.py:244`, `:284`, `drive_adapter.py:107`, `google_drive_oauth.py:86`, `identity.py:25-26`; SQL literals `command_executors.py:119`, `:1012`, `media_sync.py:145`, `provisioning.py:870`.

   **The cycle justification at `provisioning.py:758-759` is false.** The comment
   reads "`ig_login_oauth.PROVIDER`, spelled here rather than imported: that
   module imports this one for `attach_connected_identity`." Verified:
   `grep -n "provisioning" src/services/target/ig_login_oauth.py` returns
   **nothing** — `ig_login_oauth.py`'s imports are
   `src.config.constants`, `src.exceptions`, `src.services.target.egress` and
   stdlib/httpx/sqlalchemy. There is no cycle to avoid, and the comment must be
   deleted, not carried forward. (`workspaces.py:241-243` makes the same claim
   about its own import graph; it is moot once both read the vocabulary.)

   6a. In `vocabulary.py`, a new block (place it beside the other closed sets,
   above the CLI's exit codes at `:148`):
   ```python
   #: `credentials.provider` / `media_sources.provider` / `oauth_states.provider`
   #: — the values `ck_credentials_provider`, `ck_sources_provider` and
   #: `ck_oauth_state_provider` admit. Spelled here, the one dependency-free
   #: module, because seven modules had a hand copy each (#1325 audit, TD-B6).
   PROVIDER_IG_LOGIN = "ig_login"
   PROVIDER_GDRIVE = "gdrive"
   #: `identities.provider` — who verified the person.
   PROVIDER_GOOGLE = "google"
   PROVIDER_TELEGRAM = "telegram"
   ```
   Plain `str` constants only: `vocabulary.py` is the CLI's one `src` import and
   `tests/storydump_cli/test_import_boundary.py` runs a fresh interpreter over it.

   6b. Each existing module keeps its own name and derives it, so **no call site
   changes**:
   - `ig_login_oauth.py:92` — `PROVIDER = "ig_login"` → `PROVIDER = vocabulary.PROVIDER_IG_LOGIN`
   - `google_drive_oauth.py:86` — `PROVIDER = "gdrive"` → `PROVIDER = vocabulary.PROVIDER_GDRIVE`
   - `drive_adapter.py:107` — `PROVIDER = "gdrive"` → `PROVIDER = vocabulary.PROVIDER_GDRIVE`
   - `provisioning.py:91` — `GDRIVE_PROVIDER = "gdrive"` → `GDRIVE_PROVIDER = vocabulary.PROVIDER_GDRIVE`
   - `provisioning.py:758-760` — **delete the two comment lines** and
     `IG_LOGIN_PROVIDER = "ig_login"` → `IG_LOGIN_PROVIDER = vocabulary.PROVIDER_IG_LOGIN`
   - `workspaces.py:241-244` — delete the "a local name rather than an import"
     sentences; `IG_LOGIN_PROVIDER = vocabulary.PROVIDER_IG_LOGIN`
   - `workspaces.py:282-284` — same; `GDRIVE_PROVIDER = vocabulary.PROVIDER_GDRIVE`
   - `identity.py:25-26` — `PROVIDER_GOOGLE = vocabulary.PROVIDER_GOOGLE`,
     `PROVIDER_TELEGRAM = vocabulary.PROVIDER_TELEGRAM`

   Add `from src.services.target import vocabulary` to each module that lacks it
   (check with `grep -n "vocabulary" <file>`). `vocabulary` imports nothing from
   `src`, so no module can gain a cycle from this.

   6c. The four SQL literals become bound parameters:
   - `command_executors.py:119` `"                  AND c.provider = 'ig_login' AND c.state = 'active')"`
     → `"                  AND c.provider = :ig_provider AND c.state = 'active')"`, with
     `ig_provider=vocabulary.PROVIDER_IG_LOGIN` added to that statement's bound arguments.
   - `command_executors.py:1012` `" WHERE workspace_id = :ws AND provider = 'gdrive' AND state <> 'paused'"`
     → `… provider = :provider …` with `{"ws": command.workspace_id, "provider": vocabulary.PROVIDER_GDRIVE}`.
   - `media_sync.py:145` `" WHERE workspace_id = :ws AND provider = 'gdrive'"`
     → `… provider = :provider` with `"provider": vocabulary.PROVIDER_GDRIVE` added at `:148`.
   - `provisioning.py:870` `" WHERE id = :s AND workspace_id = :ws AND provider = 'gdrive'"`
     → `… provider = :provider …` with `"provider": vocabulary.PROVIDER_GDRIVE` added at `:873`.

   **Call-site audit.** `grep -rn "'ig_login'\|\"ig_login\"" src` — before:
   `ig_login_oauth.py:92`, `provisioning.py:760`, `workspaces.py:244`,
   `command_executors.py:119`. After: **only** `vocabulary.py`.
   `grep -rn "'gdrive'\|\"gdrive\"" src` — before: `provisioning.py:91`, `:870`,
   `workspaces.py:284`, `drive_adapter.py:107`, `google_drive_oauth.py:86`,
   `command_executors.py:1012`, `media_sync.py:145`. After: **only**
   `vocabulary.py`.
   `grep -rn "PROVIDER_IG_LOGIN\|PROVIDER_GDRIVE\|IG_LOGIN_PROVIDER\|GDRIVE_PROVIDER" src storydump_cli scripts tests`
   — every existing reader of `IG_LOGIN_PROVIDER`/`GDRIVE_PROVIDER`/`PROVIDER`
   is unchanged (the names survive), so the diff in `tests/` is zero for this step
   except the new pin below.

   **Tests.** `tests/src/services/target/test_vocabulary.py` gains a pin in the
   class that already checks the CHECK-constraint lists against the migrations:
   assert `vocabulary.PROVIDER_IG_LOGIN == "ig_login"`,
   `vocabulary.PROVIDER_GDRIVE == "gdrive"`, and that each appears in the
   `ck_credentials_provider` / `ck_sources_provider` / `ck_oauth_state_provider`
   text of the migration files the class already reads. Find the existing
   migration-reading helper with
   `grep -n "ck_\|migrations" tests/src/services/target/test_vocabulary.py | head -30`
   and reuse it — do not write a second file walker.

---

7. **Small vocabularies to their owners (TD-B20)** — `channel_bind.py:37`, `:39`, `membership_sync.py:29`, `bindings.py:76`, `:105-118`, `tenant_resolution.py:59`, `invitations.py:82`, `:139`, `service_tokens.py:188`.

   7a. **The group chat types.** `bindings.py:105-118` is the declared owner
   (`_CHAT_TYPES`, with the comment "**It lives here, and that placement is the
   point**"). Two modules re-derive the group half:
   `channel_bind.py:39 GROUP_CHAT_TYPES = ("group", "supergroup")` and
   `membership_sync.py:29` (identical). In `bindings.py`, beside `_CHAT_TYPES`:
   ```python
   #: The chat types that map to `telegram_group` — derived from the mapping
   #: above so a new group-shaped type is added once (#1325, TD-B20).
   GROUP_CHAT_TYPES: tuple[str, ...] = tuple(
       t for t, channel in _CHAT_TYPES.items() if channel == "telegram_group"
   )
   ```
   The comprehension preserves insertion order, so the value is exactly
   `("group", "supergroup")` today. Then
   `channel_bind.py:39` → `GROUP_CHAT_TYPES = bindings.GROUP_CHAT_TYPES` and
   `membership_sync.py:29` → `GROUP_CHAT_TYPES = bindings.GROUP_CHAT_TYPES`
   (`membership_sync.py:23` already imports `bindings`; `channel_bind.py` needs
   the import added — verify it does not create a cycle with
   `grep -n "channel_bind" src/services/target/bindings.py`, expected zero).

   7b. **The channel set.** `tenant_resolution.py:59 CHAT_CHANNELS = ("telegram_group", "telegram_dm")`
   is a second spelling of `bindings.py:76 CHANNELS`, and both comments say "must
   match `ck_bindings_channel`". Replace with
   `CHAT_CHANNELS = bindings.CHANNELS` (keep the name — `grep -rn "CHAT_CHANNELS" src storydump_cli scripts tests`
   first and list every reader; expected: `tenant_resolution.py` itself and its
   tests). Add `from src.services.target import bindings` to
   `tenant_resolution.py`; `bindings.py` is a leaf, so no cycle.

   7c. **`channel_bind.py:37 PROVIDER = "telegram"`** → `PROVIDER = identity.PROVIDER_TELEGRAM`,
   which is how `identity_link.py:38` and `membership_sync.py:28` already spell
   it. After step 6b, `identity.PROVIDER_TELEGRAM` is
   `vocabulary.PROVIDER_TELEGRAM`, so this reaches the one home.

   7d. **`invitations.py:82`** — the literal `'google'` inside the
   `fn_invitation_accept(...)` call. Bind it:
   `"  FROM fn_invitation_accept(:h, :u, :provider,"` with
   `"provider": identity.PROVIDER_GOOGLE` added at `:85`. Check
   `grep -n "identity" src/services/target/invitations.py` and add the import if absent.

   7e. **`invitations.py:139 ("admin", "member")`** — beside
   `tenant_resolution.ROLE_ORDER = ("member", "admin", "owner")`. These are not
   the same tuple (`ROLE_ORDER` includes `owner` and is ordered least-to-greatest),
   so **do not alias them**. Name the invitable subset in `tenant_resolution.py`
   beside `ROLE_ORDER`:
   ```python
   #: The roles an invitation may grant: `owner` is `transfer_ownership`'s edge.
   INVITABLE_ROLES: tuple[str, ...] = tuple(r for r in ROLE_ORDER if r != "owner")
   ```
   That evaluates to `("member", "admin")` — a **different order** from the
   literal at `:139`. The literal is used only in `if role not in (...)`, a
   membership test, so order is immaterial; but the refusal message below it
   reads "role must be admin or member" and is a separate string. Change `:139`
   to `if role not in tenant_resolution.INVITABLE_ROLES:` and leave the message
   text exactly as it is.

   7f. **The hand-rolled `set_config`.** `service_tokens.py:188`
   `_CLAIM_TENANT = text("SELECT set_config('app.tenant_id', :ws, true)")`
   expresses `apply_gucs(executor, tenant_id=ws)`. Replace the constant's one use
   with the door (`grep -n "_CLAIM_TENANT" src/services/target/service_tokens.py`
   to find it) and delete the constant. **Read the surrounding docstring at
   `:180-187` first**: it explains that the `set_config` is a *separate statement*
   because a `set_config` in the same `WHERE` would run after the policy's quals.
   `apply_gucs` issues its own statement, so that reasoning is preserved — but
   verify by reading `unit_of_work.apply_gucs` (`grep -n "def apply_gucs" -A 25 src/services/target/unit_of_work.py`)
   that it sets `app.tenant_id` transaction-locally and sets nothing else a
   resolver transaction must not have. **If `apply_gucs` also sets
   `app.actor_kind` or any other GUC unconditionally, skip 7f entirely** and
   leave a one-line comment at `:188` saying why — a wider GUC in the token
   resolver's transaction is a behaviour change.

   `workspaces.py:195-197` (`app.actor_user_id` alone) **stays**: `apply_gucs`
   takes `tenant_id` as mandatory and this door deliberately claims the actor
   without a tenant. Add one comment line above it naming that as the exception:
   ```python
       # Not `apply_gucs`: that door's `tenant_id` is mandatory and this is the
       # user-plane read, which has no tenant yet (#1325 audit, TD-B20).
   ```

   7g. **`remove_member`'s untyped refusals — WITHDRAWN from this PR.** The task
   allows typing them only if the caller's behaviour is identical. It is not,
   without care: `command_executors.py:886-892` maps `LookupError` → `not_found`
   and `ValueError` → `illegal_transition` with the message chosen by
   `str(exc) == "owner"`. `workspaces.py:713-718` raises `ValueError(outcome)`
   where `outcome` is whatever `fn_member_remove` returned other than `removed`
   and `not_found` — a set this plan cannot enumerate without reading the
   function's DDL, and any outcome not equal to `"owner"` currently produces the
   sentence "you cannot remove yourself" whether or not that is what happened.
   Typing the refusal faithfully means first establishing `fn_member_remove`'s
   complete outcome vocabulary from its migration, which is a finding in its own
   right. **Do not touch `workspaces.py:713-718` or `command_executors.py:886-892`
   in this PR.** Record it for a follow-up.

   **Call-site audit for step 7.**
   `grep -rn "GROUP_CHAT_TYPES" src storydump_cli scripts tests` — before: two
   definitions plus readers; after: one definition in `bindings.py`, two aliases,
   the same readers.
   `grep -rn "CHAT_CHANNELS" src storydump_cli scripts tests` — the name survives;
   list every reader before and confirm the same list after.
   `grep -rn '("group", "supergroup")' src` — after: **zero**.
   `grep -rn '("telegram_group", "telegram_dm")' src` — after: **only** `bindings.py:76`.
   `grep -rn "INVITABLE_ROLES" src tests` — after: one definition, one reader.
   `grep -rn "_CLAIM_TENANT" src tests` — after: **zero** (or unchanged if 7f was skipped).

   **Tests.** `tests/scripts/test_channel_bindings_writer.py:374` asserts on
   `bindings.CHANNELS` — unchanged. `tests/src/services/target/test_membership_sync.py`,
   `tests/scripts/test_command_executors_gate.py`, and the service-token gate
   (`grep -rn "service_tokens" tests/scripts/*.py | head`) characterize 7f; run
   them. Add one unit pin in `tests/src/services/target/test_bindings.py` (create
   it if absent): `bindings.GROUP_CHAT_TYPES == ("group", "supergroup")` and every
   member maps to `"telegram_group"` through `bindings.channel_for_chat_type`.

---

8. **The webhook secret header is one string, pinned (TD-C2)** — `src/api/routes/webhooks.py:92`.

   Before:
   ```python
   #: The header Telegram echoes back the registered secret in. Named once so the
   #: route and its tests cannot drift apart on the spelling.
   SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
   ```
   After:
   ```python
   #: The header Telegram echoes back the registered secret in. The vocabulary
   #: owns the spelling — the CLI's `webhook status` SENDS it and this route
   #: READS it, so a second literal here is how `storydump webhook status`
   #: starts reporting a refusal the live deliveries never see (#1325, TD-C2).
   #: The name stays, so the route's tests' import is unchanged. Precedent:
   #: `v1.py:124`, `IDEMPOTENCY_HEADER = vocabulary.IDEMPOTENCY_HEADER`.
   SECRET_HEADER = vocabulary.WEBHOOK_SECRET_HEADER
   ```
   `webhooks.py:79` already imports from `src.services.target.webhook_ingress`;
   add `from src.services.target import vocabulary` beside it.

   **Call-site audit.** `grep -rn "SECRET_HEADER" src storydump_cli scripts tests`
   — before: `webhooks.py:92` (def), `:163` (reader),
   `vocabulary.py:389` (def), `storydump_cli/webhook.py:50`, `:240`,
   `tests/storydump_cli/test_webhook.py:30`, `:242`,
   `tests/src/api/test_webhook_ingress_route.py:15`, `:90`, `:712`, `:787`,
   `tests/src/services/target/test_vocabulary.py:432`. After: **identical** —
   only `webhooks.py:92`'s right-hand side changed.

   **New pin (required by the task).** Add to
   `tests/src/services/target/test_commands.py` — the module that already pins
   one tier's vocabulary against another's — a test in the style of its existing
   closed-set pins:
   ```python
   def test_the_webhook_route_reads_the_vocabularys_header(self):
       """The route READS the header the CLI SENDS. Two literals and
       `storydump webhook status` reports a refusal Telegram never sees."""
       from src.api.routes import webhooks
       from src.services.target import vocabulary

       assert webhooks.SECRET_HEADER is vocabulary.WEBHOOK_SECRET_HEADER
   ```
   `is`, not `==`: the point of the pin is that there is one object, so a future
   hand copy with the same value still reddens. Confirm the file's class layout
   first with `grep -n "^class \|def test_" tests/src/services/target/test_commands.py | head -20`
   and place the test in the class whose docstring covers cross-module spellings.

   **Tests.** `tests/src/api/test_webhook_ingress_route.py`,
   `tests/storydump_cli/test_webhook.py`, `tests/src/services/target/test_vocabulary.py`.

---

9. **The Telegram deployment literals join the vocabulary (TD-C6)** — `src/services/target/vocabulary.py:380-399`; readers `telegram_transport.py:50`, `:602`, `telegram_webhook_registration.py:63`, `:75`, `storydump_cli/webhook.py:58`.

   9a. In `vocabulary.py`, in the Telegram block that begins at `:380` ("One
   spelling of the deployment's names, shared by the API's startup
   self-registration … and the CLI's `webhook` verb"):
   ```python
   #: Railway names the deployment's environment here. The API's autoregister
   #: guard and the transport's production guard ask the same question of it,
   #: and two literals is how a renamed environment is edited in one of them.
   RAILWAY_ENVIRONMENT_VAR = "RAILWAY_ENVIRONMENT_NAME"
   #: The one environment that owns the bot's webhook.
   PRODUCTION_ENVIRONMENT = "production"
   #: Telegram's Bot API. Spelled here because the CLI's `webhook` verb and the
   #: worker's transport both speak to it, and the CLI reaches `src` only here.
   TELEGRAM_BOT_API_BASE = "https://api.telegram.org"
   ```

   9b. `telegram_webhook_registration.py:63`:
   `ENVIRONMENT_VAR = "RAILWAY_ENVIRONMENT_NAME"` →
   `ENVIRONMENT_VAR = vocabulary.RAILWAY_ENVIRONMENT_VAR`. The module already
   imports named constants from the vocabulary at `:25-37`; follow that style
   (`from src.services.target.vocabulary import RAILWAY_ENVIRONMENT_VAR as ENVIRONMENT_VAR`
   matches the existing `WEBHOOK_URL_VAR as URL_VAR` line exactly — prefer it).
   `:75` (`return (environment or "").strip().lower() == "production"`) →
   `== vocabulary.PRODUCTION_ENVIRONMENT`. Import the name and add
   `"PRODUCTION_ENVIRONMENT"` to `__all__` (`:39-55`) only if a caller needs it;
   step 10 does not.

   9c. `telegram_transport.py:50`:
   `_API_BASE = "https://api.telegram.org"` →
   `_API_BASE = vocabulary.TELEGRAM_BOT_API_BASE`. The private name stays —
   `tests/src/channels/test_telegram_transport.py:985,988` imports it and asserts
   `t._api_base == _API_BASE`, and that assertion is unchanged.
   `telegram_transport.py:602`:
   ```python
       if (env.get("RAILWAY_ENVIRONMENT_NAME") or "").strip().lower() == "production":
   ```
   →
   ```python
       if (env.get(vocabulary.RAILWAY_ENVIRONMENT_VAR) or "").strip().lower() == (
           vocabulary.PRODUCTION_ENVIRONMENT
       ):
   ```
   Add `from src.services.target import vocabulary` to `telegram_transport.py`
   (it already imports `src.services.target.outbox` at `:46`, so the package is
   reachable).

   9d. `storydump_cli/webhook.py:58`:
   `BOT_API = "https://api.telegram.org"` → `BOT_API = TELEGRAM_BOT_API_BASE`,
   with `TELEGRAM_BOT_API_BASE` added to the existing
   `from src.services.target.vocabulary import (...)` block at `:48-55`. **This
   is the only way the CLI may learn the value**: `storydump_cli` imports
   `vocabulary.py` and nothing else from `src`
   (`tests/storydump_cli/test_import_boundary.py` runs a fresh interpreter to
   prove it), and `vocabulary.py` must stay stdlib-only — the three constants
   added in 9a are plain strings, so the boundary holds.

   **Call-site audit.** `grep -rn "https://api.telegram.org" src storydump_cli scripts tests`
   — before: `telegram_transport.py:50`, `storydump_cli/webhook.py:58`, plus test
   fixtures. After: **only** `vocabulary.py` under `src/` and `storydump_cli/`.
   `grep -rn "\"RAILWAY_ENVIRONMENT_NAME\"" src storydump_cli` — before:
   `telegram_transport.py:602`, `telegram_webhook_registration.py:63`. After:
   **only** `vocabulary.py`.
   `grep -rn "== \"production\"" src storydump_cli` — after: **zero**.
   `grep -rn "ENVIRONMENT_VAR\|_API_BASE\|BOT_API" src storydump_cli scripts tests`
   — every existing reader is unchanged; the names survive.

   Note `tests/src/services/target/test_vocabulary.py:307-336`
   (`TestOneSpellingOfTheDeployment`) scans `src/` for literal
   `"TARGET_TELEGRAM_*"` reads only, so this step neither trips it nor needs an
   `OWN_SPELLINGS` entry.

   **Tests.** Extend `test_the_variables_are_the_deployments`
   (`tests/src/services/target/test_vocabulary.py:424-436`) with three
   assertions:
   ```python
       assert vocabulary.RAILWAY_ENVIRONMENT_VAR == "RAILWAY_ENVIRONMENT_NAME"
       assert vocabulary.PRODUCTION_ENVIRONMENT == "production"
       assert vocabulary.TELEGRAM_BOT_API_BASE == "https://api.telegram.org"
   ```
   Run `tests/src/channels/test_telegram_transport.py`,
   `tests/src/channels/test_telegram_webhook_registration.py`,
   `tests/storydump_cli/test_webhook.py`,
   `tests/storydump_cli/test_import_boundary.py`.

---

10. **`autoregister_enabled` says why it said no (TD-C7)** — `src/channels/telegram_webhook_registration.py:71-77`, `src/api/app.py:414-428`.

    Today `app.py` asks the function, then copies the function's private
    off-words tuple to word its own `skipped` sentence. Give the tuple a name
    the caller can read — a constant, not a changed return type (changing
    `autoregister_enabled` to return `Optional[str]` alters a public function's
    contract; a constant does not).

    10a. `telegram_webhook_registration.py`, above `autoregister_enabled`:
    ```python
    #: An explicit "no". The API's skip reason names WHICH no it was, so the
    #: words live here rather than being re-derived by the caller (#1325, TD-C7).
    OFF_WORDS: tuple[str, ...] = ("0", "false", "no", "off")
    ON_WORDS: tuple[str, ...] = ("1", "true", "yes", "on")
    ```
    and the body's two `if value in (...)` lines read `OFF_WORDS` / `ON_WORDS`.
    Add both names to `__all__` (`:39-55`).

    10b. `app.py:418-423`:
    ```python
            switched_off = (env.get(reg.AUTOREGISTER_VAR) or "").strip().lower() in (
                "0",
                "false",
                "no",
                "off",
            )
    ```
    →
    ```python
            switched_off = (
                env.get(reg.AUTOREGISTER_VAR) or ""
            ).strip().lower() in reg.OFF_WORDS
    ```

    **Call-site audit.** `grep -rn "OFF_WORDS\|ON_WORDS" src storydump_cli scripts tests`
    — before: **zero**. After: two definitions, two readers in the registration
    module, one reader in `app.py`.
    `grep -rn '"false", *$' src/api/app.py` — after: **zero**.

    **Tests.** `tests/src/api/test_app_factory.py:584-705` (the skip-reason
    cases) and `tests/src/channels/test_telegram_webhook_registration.py`
    characterize both halves. Add one assertion to the registration test:
    every member of `OFF_WORDS` makes `autoregister_enabled(word, environment="production")`
    return `False`, and every member of `ON_WORDS` makes it return `True` with
    `environment=None` — that is the pin that reddens if the tuples and the body
    ever separate again.

    **Do not** move `_register_webhook` / `_sample_webhook_live` out of `app.py`
    in this PR. That relocation is doc 09's (`09_composition-roots.md`, TD-C11)
    and needs the monkeypatch re-pointing at `tests/src/api/test_app_factory.py:584-705`.

---

11. **The refusal tuples derive from the vocabulary (TD-C9)** — `src/exceptions/tenancy.py:34-48`, `:73`.

    `vocabulary.py` imports nothing from `src`, so `src/exceptions/tenancy.py`
    may import it with no cycle. The repo's own precedent is `commands.py:148`
    (`REASONS: tuple[str, ...] = vocabulary.REASONS`).

    11a. `:73`:
    ```python
        REASONS = ("session_required", "readonly_token", "wrong_workspace")
    ```
    →
    ```python
        REASONS: tuple[str, ...] = vocabulary.TOKEN_REFUSALS
    ```
    `tests/src/api/test_token_principal.py:257` already asserts
    `tuple(TokenRefused.REASONS) == vocabulary.TOKEN_REFUSALS` — it stays green
    and becomes true by construction.

    11b. `:34-48`:
    ```python
        REASONS = (
            "unknown_binding",
            "revoked_binding",
            "invalid_session",
            "expired_session",
            "revoked_session",
            "disabled_user",
            "not_a_member",
            "insufficient_role",
            "unknown_channel",
            "unprovisioned_channel",
            "invalid_token",
            "expired_token",
            "revoked_token",
        )
    ```
    →
    ```python
        #: The chat- and session-plane reasons this module owns, then the
        #: resolver's token reasons from the vocabulary — `disabled_user` is in
        #: both lists and is spelled once, here (#1325 audit, TD-C9).
        REASONS: tuple[str, ...] = (
            "unknown_binding",
            "revoked_binding",
            "invalid_session",
            "expired_session",
            "revoked_session",
            "disabled_user",
            "not_a_member",
            "insufficient_role",
            "unknown_channel",
            "unprovisioned_channel",
        ) + tuple(
            r for r in vocabulary.TOKEN_RESOLUTION_REASONS if r != "disabled_user"
        )
    ```
    This evaluates to **exactly** the thirteen-element tuple in the same order as
    today (`vocabulary.TOKEN_RESOLUTION_REASONS` is
    `("invalid_token", "expired_token", "revoked_token", "disabled_user")` and
    the filter drops the duplicate), so `test_tenant_reasons_are_a_subset_of_the_closed_vocabulary`
    (`tests/src/api/test_app_factory.py:177-183`) is unaffected.

    11c. **`"unprovisioned_channel"` stays.** `grep -rn "unprovisioned_channel" src storydump_cli scripts tests`
    returns two rows, both in `src/exceptions/tenancy.py` (`:19` the docstring,
    `:44` the tuple) — **no raiser anywhere**. Removing a member of a closed
    refusal vocabulary is a contract change against the web adapter's status
    table, so it is **noted, not done**: add `— no raiser in src as of 2026-09-20 (#1325 audit, TD-C9)`
    to the docstring's existing "legacy-era" parenthesis at `:19` and leave the
    tuple member alone.

    **Call-site audit.** `grep -rn "TokenRefused.REASONS\|TenantResolutionError.REASONS" src storydump_cli scripts tests`
    — before: `app.py:186` (a comment), `tests/src/api/test_app_factory.py:175`,
    `:181`, `tests/src/api/test_token_principal.py:257`. After: identical.
    `grep -rn "session_required\", \"readonly_token" src` — after: **only**
    `vocabulary.py:133-137`.

    **Tests.** `tests/src/api/test_token_principal.py`,
    `tests/src/api/test_app_factory.py`,
    `tests/src/services/target/test_service_tokens.py:334`.

---

12. **The CLI's fix sentences have one home (TD-C14)** — move `FIXES`, `UNREACHABLE_FIX`, `DEFAULT_FIX`, `INTERRUPTED_FIX` from `storydump_cli/main.py:56-91` to `storydump_cli/commands/__init__.py`.

    `main.py` imports `env.COMMANDS`, so `env.py` cannot import `main`; both can
    import `storydump_cli/commands/__init__.py`, whose docstring already declares
    it the home of "the two conventions they share". It imports only `uuid`,
    `typing`, `click` and `storydump_cli.config` today — adding these four
    constants keeps it a leaf.

    12a. Cut `main.py:56` (`UNREACHABLE_FIX`), `:59-83` (`FIXES`), `:84-88`
    (`INTERRUPTED_FIX`) and `:91` (`DEFAULT_FIX`) **verbatim, comments included**,
    and paste them into `storydump_cli/commands/__init__.py` below the
    `from storydump_cli.config import ...` line. Add `Mapping` to that module's
    `typing` import.

    12b. In `main.py`, replace the cut block with a re-export:
    ```python
    from storydump_cli.commands import (
        DEFAULT_FIX,
        FIXES,
        INTERRUPTED_FIX,
        UNREACHABLE_FIX,
    )
    ```
    **The re-export is load-bearing**: `tests/src/services/target/test_vocabulary.py:290`
    does `from storydump_cli.main import FIXES`, and that import must keep
    working. Confirm the import does not create a cycle — `main.py` already
    imports `storydump_cli.commands` for `begin`/`global_options` via the verb
    modules; check with
    `grep -n "from storydump_cli" storydump_cli/main.py` and place the new import
    beside the existing ones.

    12c. `commands/env.py` — add the names to its existing
    `from storydump_cli.commands import begin, global_options` line at `:35`:
    ```python
    from storydump_cli.commands import FIXES, UNREACHABLE_FIX, begin, global_options
    ```
    then replace the three hand-copied sentences:
    - `:551` `"check STORYDUMP_API and the network",` → `UNREACHABLE_FIX,`
    - `:611` `"run storydump login with a token minted on the web under Settings › API tokens",` → `FIXES["not_authorized"],`
    - `:645` `"fix the config file, or delete it and run storydump login again",` → `DEFAULT_CONFIG_FIX,`

    For `:645`: the sentence at `main.py:237` is a **fallback inside
    `_config_error`** (`getattr(exc, "fix", None) or (...)`), not a named
    constant. Name it in `commands/__init__.py` as part of 12a:
    ```python
    #: What fixes a config file the CLI cannot use, when the error names no fix.
    DEFAULT_CONFIG_FIX = "fix the config file, or delete it and run storydump login again"
    ```
    and change `main.py:236-238` to
    ```python
        fix = getattr(exc, "fix", None) or DEFAULT_CONFIG_FIX
    ```
    Add `DEFAULT_CONFIG_FIX` to the re-export in 12b.

    **Call-site audit.** `grep -rn "FIXES\|UNREACHABLE_FIX\|DEFAULT_FIX\|INTERRUPTED_FIX\|DEFAULT_CONFIG_FIX" src storydump_cli scripts tests`
    — before: `main.py:56`, `:59`, `:84`, `:91`, `:320`, `:331`, `:340`, `:286`,
    `tests/src/services/target/test_vocabulary.py:290`, `:293`. After: the
    definitions in `commands/__init__.py`, the re-export in `main.py`, the same
    four readers in `main.py`, three new readers in `commands/env.py`, and the
    unchanged test import.
    `grep -rn "check STORYDUMP_API and the network" storydump_cli` — after: **one** row.
    `grep -rn "run storydump login with a token minted" storydump_cli` — after: **one** row.
    `grep -rn "fix the config file, or delete it" storydump_cli` — after: **one** row.

    **Tests.** `tests/storydump_cli/test_main.py`,
    `tests/storydump_cli/test_env.py` (both assert on the sentences; the strings
    are unchanged, so both should pass untouched — if either imports from
    `storydump_cli.main`, the re-export keeps it valid),
    `tests/src/services/target/test_vocabulary.py:289-293`,
    `tests/storydump_cli/test_import_boundary.py`.

---

13. **CHANGELOG** — add under `## [Unreleased]` → `### Changed`, as the first bullet of that section:

    ```markdown
    - **One spelling: every hand-copied constant reads the module that owns it (the tech-debt audit, doc 01; #1325).** Twelve places kept a second, hand-written copy of a value another module declares, and a copy is a string literal no grep for the owner's name finds. `intent_ledger.TERMINAL_STATES` is now bound as a `text[]` parameter by the three SQL statements that spelled the six states by hand and read as a tuple by the pipeline's two Python copies — the drift the module's own comment records as having once made an offboard mint successors forever. The sender-job sweep reads `jobs.LANE_BUDGETS["interactive"]` for the attempts and the deadline it wrote as `3` and `interval '10 minutes'`, and `jobs.SENDER_KEY_PREFIX` for the `tg:` it spelled three times; the publish pipeline's bulk backoff ladder is `jobs.BACKOFF_SECONDS["bulk"]`; the prompt-sweep batch, the status cadence, the sender-mint bound, the ambiguous-resolution batch, the pacing-window fallbacks, the bulk lane's attempt floor and a day in seconds are named rather than typed into a door body. "Where can we say this" — `state = 'active' AND channel LIKE 'telegram%'` — is one fragment in `bindings.py`, read by the sweep, the two one-statement outbox doors and `prompts.push_bindings`, so the day a second push channel lands they cannot disagree about who gets a card. `"ig_login"` and `"gdrive"` move to `vocabulary.py` — the dependency-free module the CLI's import boundary already pins — from the seven module constants and four inline SQL literals that each had to agree with `ck_credentials_provider`; one of those copies carried an import-cycle justification that was false (`ig_login_oauth` imports nothing from `provisioning`). The Telegram group chat types derive from `bindings._CHAT_TYPES`, the channel set from `bindings.CHANNELS`, the invitable roles from `tenant_resolution.ROLE_ORDER`. The API route that READS Telegram's secret header now reads the same `vocabulary.WEBHOOK_SECRET_HEADER` the CLI SENDS, pinned by identity so a future hand copy with the right value still reddens — before, `storydump webhook status` and the live deliveries could disagree with no test noticing. `RAILWAY_ENVIRONMENT_NAME`, `"production"` and the Bot API base join the vocabulary both sides share; `create_app` asks `telegram_webhook_registration.OFF_WORDS` instead of re-deriving the function's private tuple to word its own skip reason; `TokenRefused.REASONS` and the token half of `TenantResolutionError.REASONS` derive from `vocabulary.TOKEN_REFUSALS` / `TOKEN_RESOLUTION_REASONS`; and the CLI's fix sentences live in `storydump_cli/commands/__init__.py`, which `main.py` and `commands/env.py` can both import — they were copied because `env.py` cannot import `main.py`. No value changed, no signature a caller relies on changed, and no wire shape changed; `tests/src/services/target/test_commands.py` gains the header pin and `test_vocabulary.py` the provider and deployment pins.
    ```

    Then run `grep -n "## \[Unreleased\]" -A 3 CHANGELOG.md` to confirm the entry
    sits under the right heading. CI's `changelog-check` (`.github/workflows/ci.yml`)
    fails without it.

## Test Plan

- **Pre-change baseline, on the parent commit (`0966771`)**, with the Docker test
  PostgreSQL up (`AGENTS.md` › Testing has the `docker run`):
  ```bash
  PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" \
  DB_HOST=localhost DB_PORT=65433 DB_USER=test_user DB_PASSWORD=test_password \
  DB_NAME=storyline_ai TEST_DB_NAME=storyline_test REQUIRE_TEST_DATABASE=1 \
    pytest --no-cov
  ```
  Record the pass/fail/skip counts. This is the number the post-change run must match.
- **Which existing gate characterizes this change.** The five SQL edits of step 1
  and the SQL edits of steps 3, 4 and 6c are only provable against real
  PostgreSQL: `tests/scripts/test_offboard_gate.py`,
  `tests/scripts/test_w3_prompt_gate.py`,
  `tests/scripts/test_scheduler_clock_gate.py`,
  `tests/scripts/test_l5_pipeline_gate.py`,
  `tests/scripts/test_w2_transport_gate.py`,
  `tests/scripts/test_channel_bindings_writer.py`,
  `tests/scripts/test_invitation_cards.py`,
  `tests/scripts/test_customer_notice_gate.py`,
  `tests/scripts/test_command_executors_gate.py`. A scripted executor cannot fail
  the way a bad `CAST(:terminal AS text[])` or an unbound `:provider` fails.
- **Targeted, per step** (fast, no database needed for the unit files):
  ```bash
  REQUIRE_TEST_DATABASE=1 pytest --no-cov \
    tests/scripts/test_offboard_gate.py \
    tests/scripts/test_w3_prompt_gate.py \
    tests/scripts/test_scheduler_clock_gate.py \
    tests/scripts/test_l5_pipeline_gate.py \
    tests/scripts/test_w2_transport_gate.py \
    tests/scripts/test_channel_bindings_writer.py \
    tests/scripts/test_invitation_cards.py \
    tests/scripts/test_customer_notice_gate.py \
    tests/scripts/test_command_executors_gate.py
  REQUIRE_TEST_DATABASE=1 pytest --no-cov \
    tests/src/test_worker.py \
    tests/src/services/target/test_work_loop.py \
    tests/src/services/target/test_outbox_paced.py \
    tests/src/services/target/test_outbox_restate.py \
    tests/src/services/target/test_outbox_destination_gone.py \
    tests/src/services/target/test_command_executors.py \
    tests/src/services/target/test_vocabulary.py \
    tests/src/services/target/test_commands.py \
    tests/src/services/target/test_membership_sync.py \
    tests/src/api/test_webhook_ingress_route.py \
    tests/src/api/test_app_factory.py \
    tests/src/api/test_token_principal.py \
    tests/src/channels/test_telegram_transport.py \
    tests/src/channels/test_telegram_webhook_registration.py \
    tests/storydump_cli/test_webhook.py \
    tests/storydump_cli/test_main.py \
    tests/storydump_cli/test_env.py \
    tests/storydump_cli/test_import_boundary.py
  ```
- **New/updated pins**:
  - `tests/src/services/target/test_commands.py` — **new**: `webhooks.SECRET_HEADER is vocabulary.WEBHOOK_SECRET_HEADER` (step 8).
  - `tests/src/services/target/test_vocabulary.py` — **updated**: the two provider constants and their presence in the three CHECK lists (step 6); the three deployment constants in `test_the_variables_are_the_deployments` (step 9).
  - `tests/src/services/target/test_work_loop.py` — **updated**: the sender mint binds `jobs.LANE_BUDGETS["interactive"]`, and the four push-binding statements read the `bindings` fragment (steps 3, 4).
  - `tests/src/services/target/test_bindings.py` — **new or updated**: `GROUP_CHAT_TYPES` derives from `_CHAT_TYPES` (step 7a).
  - `tests/src/channels/test_telegram_webhook_registration.py` — **updated**: every `OFF_WORDS` member is off, every `ON_WORDS` member is on (step 10).
  - `tests/src/test_worker.py` — **updated** only if it enumerates `WorkerConfig`'s fields (steps 3, 5).
- **Post-change**: the same full-suite command, same counts (plus the new tests);
  then `ruff check . && ruff format --check .`.

## Verification Checklist

- [ ] baseline green on the parent commit (`0966771`), counts recorded
- [ ] call-site audit: every `grep -rn` in steps 1–12 run, results as stated (including the ones whose expected result is **zero**)
- [ ] targeted tests + full suite green, counts match the baseline plus the new tests
- [ ] `ruff check . && ruff format --check .`
- [ ] manual smoke: `storydump webhook status` prints its four lines and no secret (step 8/9 touch its header and its Bot API base); `storydump doctor` prints the same six checks with the same fix sentences as before the change (step 12)
- [ ] `CHANGELOG.md` entry under `## [Unreleased]` → `### Changed`
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

**No flagged defect rides in this PR.** Each of these changes behaviour and is
recorded in `00_TECH_DEBT.md` › Questions for its own ruling or bug-fix PR:

- **TD-A1, the repost-lock TTL (7 vs 30).** `run_publish_pipeline(repost_ttl_days_default=7)`
  beside `DEFAULT_REPOST_TTL_DAYS = 30`. It looks exactly like the duplicates this
  PR folds, and it is not one: the two copies have **already drifted**, so folding
  them picks a number. Do not touch `publish_pipeline.py:263` or any of its
  forwarding sites (`:325-327`, `:336-340`, `:377`, `:1510`, `:1670`, `:2079`).
- **TD-C1, the health tap keys.** `output.py:650` reads `taps.executed`/`taps.replayed`
  where `/health` emits `taps.taps`; the fixture encodes the wrong shape so the test
  passes. Fixing it changes rendered output.
- **TD-B4, `bindings.repoint`.** This PR edits `bindings.py` (steps 4, 7a, 7b) —
  edit only the constants named there. Do not touch `repoint`'s swallowed unique
  violation.
- **TD-B3, the two hand-written `INSERT INTO jobs`.** `media_sync.py:619` and
  `offboarding.py:306` bypass `jobs.enqueue`. Routing them through it gives them a
  `deadline_at` they do not have today — a behaviour change. Step 3 edits the
  *sweep's* mint (`work_loop.py:1094`) and nothing else.
- **TD-C5, `/health.version`.** `app.py:87` says `"0.2.0"` while `src.__version__`
  is `1.6.0`. Step 10 edits `app.py:418-423` only.
- **TD-B2, the `_IN_TRANSACTION` tripwire.** Do not arm it in `make_session_for`
  or `poller_session_factory`; it may start raising on an existing violation.
- **TD-B17, the tenant-less credential read** (`ig_credentials.py:63-80`). Not in
  scope; step 6 does not touch that module.
- **TD-B16, the `category_mix` v1 compat shim** (`v1.py:776`, `:799`). Removing it
  is a contract change.

**Also not in this PR:**

- **No deletions.** `work_loop.py:83 retry_backoff_seconds` (zero readers) and the
  unused `backoff_seconds=` parameter at `publish_pipeline.py:262` are *named* here
  and removed in doc 02 and doc 06 respectively. A PR that both folds constants and
  deletes surfaces cannot claim "no behaviour changed" from its diff alone.
- **No signature changes.** `settle`'s four optional budget parameters
  (`outbox.py:1143-1153`) stay optional; `resolve_aged_ambiguous` gains no `limit`
  keyword (four test modules monkeypatch its exact signature);
  `autoregister_enabled` keeps returning `bool`.
- **No Python pre-check duplicating the database.** The provider names and the
  terminal states are *bound into SQL the database still enforces* — nothing here
  adds a Python validation in front of `ck_credentials_provider`,
  `ck_bindings_channel` or `trg_intent_guard`. The database is the authority.
- **No extraction below the rule of three.** `bindings.push_binding_where` has four
  call sites and `jobs.SENDER_KEY_PREFIX` three; everything else in this PR is a
  *reference to an existing owner*, not a new abstraction.
- **`workspaces.remove_member`'s untyped refusals (step 7g) stay untyped**, and
  `"unprovisioned_channel"` (step 11c) stays in the tuple. Both are noted, neither
  is done.
- **Do not move `_register_webhook` / `_sample_webhook_live` out of `app.py`** —
  that is doc 09 (TD-C11) and it re-points monkeypatches at
  `tests/src/api/test_app_factory.py:584-705`.

## Related

- `00_TECH_DEBT.md` — the inventory, the severity scoring and the remediation order (row 01).
- `02_dead-lane-and-surfaces.md` — depends on this doc; deletes `retry_backoff_seconds` and the dead surfaces this doc only names.
- `03_rule-of-three-services.md`, `04_rule-of-three-api-cli.md` — both depend on this doc.
- `.claude/rules/development-patterns.md` › Service modules ("Numbers are parameters… a literal in a service or a door body is a review blocker"; "the CLI's one `src` import is `vocabulary.py`").
- `.claude/rules/scheduler.md` ("The numbers are `WorkerConfig`'s defaults, passed as parameters to the doors").
- `.claude/rules/testing.md` › Two kinds of test (why the SQL edits need the gates, not the unit files).

## Origin

- `/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`.
- Research: `tech-debt-2026-09-20/research/pipeline-worker.md` (TD-A2, A4, A5, A11, A15), `research/integrations-identity.md` (TD-B6, B20), `research/api-channels-cli.md` (TD-C2, C6, C7, C9, C14).
