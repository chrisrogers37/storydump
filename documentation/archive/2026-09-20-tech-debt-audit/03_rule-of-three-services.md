---
title: "One spelling for the copies past three inside src/services/target — the fan-out, the refusal constructor, the retire statement, the grant predicate, the evidence latch and the audit row"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:medium, services]
links: []
---

# 03 — One spelling for the copies past three inside `src/services/target`

| | |
|---|---|
| **PR title** | One spelling for the copies past three inside `src/services/target` — the fan-out, the refusal constructor, the retire statement, the grant predicate, the evidence latch and the audit row |
| **Risk** | Medium — every change is a like-for-like swap onto a door that already exists, but four of them edit SQL inside the worker's terminal transaction (the evidence latch and the audit rows), so the gates, not a reading, are what proves it |
| **Effort** | M–L (≈ 6–9 hours) |
| **Files modified** | `src/services/target/outbox.py`, `media_sync.py`, `credential_lifecycle.py`, `google_oidc.py`, `ig_login_oauth.py`, `bindings.py`, `category_mix.py`, `invitations.py`, `identity_link.py`, `channel_bind.py`, `provisioning.py`, `google_drive_oauth.py`, `drive_credentials.py`, `workspaces.py`, `publish_pipeline.py`, `reconciler.py`, `transit.py`, `command_executors.py`, `work_loop.py`, `email_sender.py`, `intent_ledger.py`, `_dbapi.py`, `offboarding.py`, `src/services/target/audit.py` (new), `src/utils/datetime_utils.py`, `CHANGELOG.md` |
| **Findings addressed** | TD-A8, TD-A9, TD-A10/TD-B13, TD-A19 (partial — see step 6), TD-B7 (partial — see step 1), TD-B10, TD-B11, TD-B12 (partial — see step 2), TD-B14 |
| **Depends on** | `01_one-spelling.md` (it lands `vocabulary` additions and `prompts.PUSH_BINDING_WHERE`; step 8 here splices a sibling fragment and must not race it) |
| **Blocks** | `06_publish-pipeline-shape.md`, `07_executors-and-tap.md`, `08_integrations-shape.md` |

## Summary

`src/services/target/` grew by copying. Nine ideas in this tier are spelled at three to thirteen
sites each *beside a module that already owns them*: `outbox.fanout_notification` (whose own
docstring names the three callers still to move), `RefusalError.__init__` (whose docstring says
"the constructor is not a fourth place to re-derive the same three lines"), the
`UPDATE oauth_states SET consumed_at = now()` retire, the ownerless-`gdrive`-credential predicate
that IS migration 069's ownership rule, the `customer_notified` evidence merge the reconciler's
own prose says must never be a rebuild, and the direct `INSERT INTO audit_events` shape.

This PR gives each one home and points every site at it. **Nothing changes behaviour.** Where a
copy could not be folded without changing an observable — `ProvisioningRefused`'s message reaching
the API as `detail`, `credential_lifecycle.ig_refresh`'s unguarded `resp.json()`, the three
mutually incompatible ZoneInfo degrades — the step names the exact difference and excludes the
site rather than quietly normalising it. Three sub-findings are **withdrawn** on re-reading with
their reasons recorded (steps 2, 6 and the Withdrawn table).

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-B7 | `google_oidc.py:60`, `ig_login_oauth.py:516`, `bindings.py:94`, `category_mix.py:50`, `invitations.py:59` | Five refusal classes hand-roll the `RefusalError.__init__` the base exists to own (`ProvisioningRefused` excluded — step 1) |
| TD-B12 | `google_drive_oauth.py:216,:271`, `ig_login_oauth.py:631` | `expires_in` → `expires_at` re-derived three times; the `3600` at `:275` is unnamed (`json_body` half withdrawn — step 2) |
| TD-B10 | `invitations.py:88,:198`, `workspaces.py:125`, `_dbapi.py:21` | Three hand-rolled `for cause in driver_candidates(exc): isinstance(...)` loops beside `_dbapi`, whose docstring claims a migration that never happened |
| TD-A10/B13 | `media_sync.py:300,:482`, `credential_lifecycle.py:365` vs `outbox.py:223` | The "tell every push binding" loop `fanout_notification` was written to replace, still written out three times |
| TD-A19 | `_utcnow` ×4, `elapsed_ms` ×4, `or "UTC"` ×13 | Four trivial ideas spelled twenty ways; `src/utils/datetime_utils.py` is the existing home (ZoneInfo degrade withdrawn — step 6) |
| TD-B11 | `identity_link.py:60`, `channel_bind.py:57`, `ig_login_oauth.py:169`, `provisioning.py:837`; `drive_credentials.py:115,:260,:292`, `google_drive_oauth.py:329,:360`, `workspaces.py:307` | The last-issued-wins retire ×4 and migration 069's ownership predicate ×6 |
| TD-A8 | `publish_pipeline.py:972,:1678,:2093` vs `outbox.py:767` | Three per-binding `restate_cards` loops where `restate_everywhere` is one statement |
| TD-A9 | `publish_pipeline.py:821,:1876`; `reconciler.py:194,:306` | The evidence-merge SET clause written four times, in two cast spellings |
| TD-B14 | `credential_lifecycle.py:220`, `offboarding.py:278`, `publish_pipeline.py:479,:932` | Four hand-written `INSERT INTO audit_events`, two actor spellings |

### Withdrawn on re-reading

| ID (part) | Reason |
|---|---|
| TD-B7, the `ProvisioningRefused` case | `RefusalError.__init__` builds `f"{self._prefix}: {reason}"` with no way to render a bare reason. `ProvisioningRefused` renders `f"{reason}"` (`provisioning.py:136`) and `src/api/app.py:335` returns `{"detail": str(exc), "reason": exc.reason}` — so the wire body would change from `{"detail": "handle_required", …}` to `{"detail": "provisioning refused: handle_required", …}` on every 4xx from the provisioning routes. That is a contract change, not a cleanup. Excluded; see "What NOT To Do". |
| TD-B12, the `json_body` half | The finding counts five "JSON body or None" sites. On re-reading only **two** are that shape (`google_oidc.py:136-139` and `:165-168`). `ig_login_oauth._json_object` raises `IgOAuthRefused`, `google_drive_adapter._json_body` raises `DriveRetryableError`, and `email_sender.py:240-243` extracts `.get("id")` inside the `try`. Two is coincidence — rule of three not met. Withdrawn. |
| TD-B12, `credential_lifecycle.ig_refresh` | `:110-114` tests `body.get("expires_in") is not None` and calls `int(body["expires_in"])`; the other three guard `isinstance(x, (int, float)) and not isinstance(x, bool)`. A body carrying `"expires_in": true` yields one second today and `None` under the helper; `"expires_in": "3600"` yields an hour today and `None` under the helper. The `resp.json()` at `:105` is likewise unguarded and a non-JSON 200 raises `ValueError` out of the executor. Both are reachable (the endpoint is Meta's, not ours). Excluded — a bug-fix PR, not this one. |
| TD-A19, the ZoneInfo degrade | The three copies are not one idea. `prompts.stamp:97-107` catches `(ZoneInfoNotFoundError, ValueError)`, warns once per zone and **relabels** `tz` to `"UTC"` so the rendered string changes; `prompts.waiting_line:147-150` catches `Exception` and is silent; `publish_pipeline._local_date:446-450` catches `Exception`, is silent and returns `timezone.utc` rather than a `ZoneInfo`. Any single helper either widens `stamp`'s except clause or adds a log line to two silent sites. Which degrade is the house rule is an owner ruling; withdrawn from this behaviour-preserving PR. |

## Dependencies

- **`01_one-spelling.md` must merge first.** It adds `prompts.PUSH_BINDING_WHERE` and the
  `vocabulary` provider constants. Step 4 below reads `prompts.push_bindings` and step 8 adds a
  sibling SQL-fragment constant in `google_drive_oauth`; both conflict textually with 01 if run in
  parallel. Rebase on 01's merge commit before starting.
- Nothing here depends on `02_dead-lane-and-surfaces.md`. Step 5 touches
  `src/utils/datetime_utils.py`, which 02 also edits (it removes the test-only `naive_utc`); if 02
  has merged, rebase — the two edits are in different functions.

## Implementation Plan

### Steps

1. **Five refusal classes subclass `RefusalError`** — `src/services/target/google_oidc.py:60-70`,
   `ig_login_oauth.py:516-527`, `bindings.py:94-102`, `category_mix.py:50-58`,
   `invitations.py:59-64`. The base is `src/exceptions/base.py:15-37`; the in-repo precedents for
   the two shapes are `DriveOAuthRefused` (`google_drive_oauth.py:119-131`, prefix + vocabulary
   guard + `super()`) and `TokenRefused` (`src/exceptions/tenancy.py:56-79`).

   Before (`google_oidc.py:60-70`):
   ```python
   class OidcRefused(StorydumpError):
       """A sign-in the flow will not complete. ``reason`` is a closed set so the
       route maps it without parsing prose: ``exchange_failed`` · ``no_id_token``
       · ``malformed_id_token`` · ``issuer`` · ``audience`` · ``expired`` ·
       ``future`` · ``nonce`` · ``subject``."""

       def __init__(self, reason: str, detail: str = ""):
           self.reason = reason
           super().__init__(
               f"sign-in refused: {reason}" + (f" — {detail}" if detail else "")
           )
   ```
   After:
   ```python
   class OidcRefused(RefusalError):
       """A sign-in the flow will not complete. ``reason`` is a closed set so the
       route maps it without parsing prose: ``exchange_failed`` · ``no_id_token``
       · ``malformed_id_token`` · ``issuer`` · ``audience`` · ``expired`` ·
       ``future`` · ``nonce`` · ``subject``."""

       _prefix = "sign-in refused"
   ```

   The remaining four, each keeping its message verbatim by setting `_prefix` to the exact string
   that precedes `": {reason}"` today:

   | Class | File:line | `_prefix` | Keeps |
   |---|---|---|---|
   | `IgOAuthRefused` | `ig_login_oauth.py:516-527` | `"instagram grant refused"` | its `REASONS` guard **and** `self.detail = detail` (the base does not set `detail`) |
   | `BindingRefused` | `bindings.py:94-102` | `"binding refused"` | — |
   | `MixInvalid` | `category_mix.py:50-58` | `"mix invalid"` | — |
   | `InvitationRefused` | `invitations.py:59-64` | `"invitation refused"` | — |

   `IgOAuthRefused` after (the guard-then-`super()` shape of `tenancy.TokenRefused:74-78`):
   ```python
   class IgOAuthRefused(RefusalError):
       """The grant could not be completed. ``reason`` is one of :data:`REASONS`,
       and no token ever rides in the message."""

       _prefix = "instagram grant refused"

       def __init__(self, reason: str, detail: str = ""):
           if reason not in REASONS:
               raise ValueError(f"unknown IgOAuthRefused reason {reason!r}")
           self.detail = detail
           super().__init__(reason, detail)
   ```

   Each edited module swaps its import: `from src.exceptions.base import StorydumpError` becomes
   `from src.exceptions.base import RefusalError, StorydumpError` where `StorydumpError` is still
   used elsewhere in the file, else `RefusalError` alone. Check per file with
   `grep -n "StorydumpError" src/services/target/<file>.py`.

   Call-site grep — the five names are raised and caught, never constructed positionally beyond
   `(reason, detail)`, so no call site changes:
   ```
   grep -rn "OidcRefused\|IgOAuthRefused\|BindingRefused\|MixInvalid\|InvitationRefused" src storydump_cli scripts tests
   ```
   Expected before and after: the **same** set of lines (definitions, `raise` sites, `except`
   arms, the `app.py` handler registrations at `:328-357`, and the tests). If the two outputs
   differ by anything other than the five class-body lines, stop.

   Verify no test asserts the messages (they must be byte-identical anyway, so this is a
   confirmation, not a licence):
   ```
   grep -rn "sign-in refused\|instagram grant refused\|binding refused\|mix invalid\|invitation refused" tests
   ```

   Test to update: none — the messages are unchanged by construction. Add one pin instead:
   in `tests/src/exceptions/test_base.py` (create it if absent — precedent:
   `tests/src/exceptions/` already holds the tenancy tests; confirm with `ls tests/src/exceptions`)
   a parametrised test asserting `str(Cls("r", "d")) == "<prefix>: r — d"` for all five classes
   plus `DriveOAuthRefused` and `EmailRefused`, so the next class that hand-rolls the constructor
   fails a test rather than a review.

2. **`expires_in` → `expires_at` once, and the hour is named** —
   `src/services/target/google_drive_oauth.py:216-219`, `:271-278`, `ig_login_oauth.py:631-634`.

   The three sites are the same guarded conversion; only the fallback differs (`None`, `3600`,
   `None`). Add to `src/services/target/egress.py` — the floor both modules already import for
   `egress.request`; confirm with
   `grep -n "^from src.services.target import egress\|import egress" src/services/target/google_drive_oauth.py src/services/target/ig_login_oauth.py`:

   ```python
   def expires_at_from(
       expires_in: Any, *, default_seconds: Optional[int] = None
   ) -> Optional[datetime]:
       """A token response's ``expires_in`` as an absolute UTC instant.

       Three provider legs derived this independently (`07` §2's grants and the
       Drive refresh), and each re-decided the same two guards: a JSON body is
       untrusted, so a non-numeric value is no expiry, and ``bool`` is an ``int``
       in Python — ``"expires_in": true`` must not read as one second.

       *default_seconds* is what an ABSENT or unusable value means. ``None``
       means "no known expiry"; the Drive refresh passes Google's hour, because
       writing NULL there would read as "never refresh this again".
       """
       if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
           return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
       if default_seconds is None:
           return None
       return datetime.now(timezone.utc) + timedelta(seconds=default_seconds)
   ```

   Before (`google_drive_oauth.py:216-219`):
   ```python
       expires_in = body.get("expires_in")
       expires_at = None
       if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
           expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
   ```
   After:
   ```python
       expires_at = egress.expires_at_from(body.get("expires_in"))
   ```

   Before (`google_drive_oauth.py:271-278`):
   ```python
       expires_in = body.get("expires_in")
       # No `expires_in` (Google always sends one; a proxy might not): assume
       # Google's hour rather than write NULL, which would read as "no known
       # expiry" and never be refreshed again.
       seconds = 3600
       if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
           seconds = int(expires_in)
       expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
   ```
   After (the comment moves to the constant, where the number is):
   ```python
       expires_at = egress.expires_at_from(
           body.get("expires_in"), default_seconds=GOOGLE_ACCESS_TOKEN_SECONDS
       )
   ```
   with, beside `SCOPE` near the top of `google_drive_oauth.py`:
   ```python
   #: What an access token is worth when the refresh response does not say.
   #: Google always sends `expires_in`; a proxy might not, and writing NULL
   #: there would read as "no known expiry" and never be refreshed again.
   GOOGLE_ACCESS_TOKEN_SECONDS = 3600
   ```

   `ig_login_oauth.py:631-634` takes the same one-line After as the first site.

   Call-site grep:
   ```
   grep -rn "expires_at_from\|GOOGLE_ACCESS_TOKEN_SECONDS" src storydump_cli scripts tests
   ```
   Expected before: 0 lines. Expected after: 2 (the def and the constant) + 3 call sites + the new
   unit test's imports.
   ```
   grep -rn "expires_in" src/services/target
   ```
   Expected before: 8 lines across `google_drive_oauth.py` (×3), `ig_login_oauth.py` (×2),
   `credential_lifecycle.py` (×3). Expected after: 3 lines in `credential_lifecycle.py` only
   (**deliberately untouched** — see Withdrawn) plus the three `body.get("expires_in")` arguments.

   Test to add: `tests/src/services/target/test_egress.py` (the file exists — confirm with
   `ls tests/src/services/target/test_egress.py`) gains a table-driven test for
   `expires_at_from`: `3600` → now+1h, `3600.0` → now+1h, `True` → the default,
   `"3600"` → the default, `None` → the default, `None` with `default_seconds=3600` → now+1h.
   Update `tests/src/services/target/test_google_drive_oauth.py` only if it asserts on
   `expires_at` — it must keep passing unchanged.

3. **One question about an asyncpg error, asked one way** — `src/services/target/_dbapi.py:11-29`,
   `invitations.py:88-94`, `:198-204`, `workspaces.py:125-130`.

   Add beside `driver_candidates`:
   ```python
   def driver_error_is(exc: BaseException, *classes: type) -> Optional[BaseException]:
       """The first buried driver exception that is one of *classes*, else None.

       :func:`constraint_violated` answers "which constraint" and is the better
       question where the constraint has a name. This answers "which class",
       which is what three siblings ask when the DDL names no constraint they
       can rely on (`fn_invitation_accept` raises `no_data_found`; a
       check-violation's name is read off the exception itself).
       """
       for candidate in driver_candidates(exc):
           if isinstance(candidate, classes):
               return candidate
       return None
   ```

   Before (`invitations.py:88-94`):
   ```python
       except DBAPIError as exc:
           for cause in driver_candidates(exc):
               if isinstance(cause, NoDataFoundError):
                   raise InvitationRefused("not_acceptable", str(cause)) from exc
               if isinstance(cause, CheckViolationError):
                   raise InvitationRefused("identity_mismatch", str(cause)) from exc
           raise
   ```
   After:
   ```python
       except DBAPIError as exc:
           if (cause := driver_error_is(exc, NoDataFoundError)) is not None:
               raise InvitationRefused("not_acceptable", str(cause)) from exc
           if (cause := driver_error_is(exc, CheckViolationError)) is not None:
               raise InvitationRefused("identity_mismatch", str(cause)) from exc
           raise
   ```
   **Behaviour note the builder must preserve:** the loop today checks both classes against each
   candidate in turn, so a chain whose `orig` is a `CheckViolationError` and whose `__cause__` is a
   `NoDataFoundError` raises `identity_mismatch`; the rewrite raises `not_acceptable`. asyncpg
   never produces such a chain (`orig` and `orig.__cause__` are the same driver exception wrapped
   once) — if the builder cannot satisfy themselves of that, keep the single-pass loop and call
   `driver_error_is` only at the two single-class sites.

   `invitations.py:198-204` and `workspaces.py:125-130` are single-class and convert directly.
   `workspaces.py:127` keeps its `getattr(cause, "constraint_name", None) or "check"` —
   **do not** switch it to `constraint_violated`, which asks a different (narrower) question.
   Likewise `invitations.py:199` keeps `UniqueViolationError`: narrowing it to
   `constraint_violated(exc, "uq_invite_live")` would stop refusing on any other unique violation
   the statement can raise, which is a behaviour change.

   Same step, the words: `_dbapi.py:24-27` says `constraint_violated` is "THE FOURTH READER OF THE
   SAME QUESTION, AND THE LAST ONE" and names `jobs`, `publish_cap` and `provider_ops` as migrated
   siblings. Confirm with `grep -rn "driver_candidates" src` (expected: `_dbapi.py` def +
   `jobs.py`, `publish_cap.py`, `provider_ops.py`, `invitations.py` ×2, `workspaces.py`) and
   rewrite the paragraph to say what is true: that the hoist happened, that three siblings still
   ask by class through `driver_error_is`, and which question each door answers.

   Call-site grep:
   ```
   grep -rn "driver_candidates\|driver_error_is\|constraint_violated" src storydump_cli scripts tests
   ```
   Expected after: `driver_candidates` has exactly two callers left, both inside `_dbapi.py`
   (`constraint_violated` and `driver_error_is`); `driver_error_is` has four call sites
   (`invitations.py` ×3, `workspaces.py` ×1).

   Tests: `tests/scripts/test_invitation_create_gate.py` and
   `tests/scripts/test_command_executors_gate.py` (`invalid_args`) must pass unchanged. Add a unit
   test for `driver_error_is` beside the existing `_dbapi` tests — find them with
   `grep -rln "constraint_violated" tests`.

4. **The fan-out loop goes through the door written to replace it** —
   `src/services/target/media_sync.py:298-315`, `:476-497`,
   `credential_lifecycle.py:365-372`, against `outbox.py:223-252`.

   First, find every copy (the finding counts three in scope, ten in the tier):
   ```
   grep -rn "push" src/services/target | grep -i binding
   ```
   Expected: the owner (`prompts.py:339` `push_bindings` def, its docstring), and the callers.
   Then narrow to the loops:
   ```
   grep -rn -A3 "for binding_id in\|for b in" src/services/target | grep -n "kind=\"notification\""
   ```
   **Swap only a site whose loop body is exactly `outbox.enqueue(..., kind="notification",
   payload={"v": 1, "text": …})` with no other statement in the loop.** Sites that loop to call
   `outbox.restate_cards` are step 9's, not this one's; `publish_pipeline.py:1805-1814` loops for
   `restate_cards` and *then* calls `fanout_notification` at `:1815` — leave it exactly as it is
   (it needs the binding list for its `return len(bindings)`).

   Before (`media_sync.py:298-315`):
   ```python
       for row in rows:
           workspace_id = str(row["workspace_id"])
           bindings = await prompts.push_bindings(session, workspace_id)
           for binding_id in bindings:
               await outbox.enqueue(
                   session,
                   workspace_id=workspace_id,
                   binding_id=binding_id,
                   kind="notification",
                   payload={
                       "v": 1,
                       "text": (
                           "⚠️ This workspace's Drive source is still disconnected"
                           " and has not synced since it failed. Reconnect it to"
                           " resume syncing, or pause it if this is intended."
                       ),
                   },
               )
   ```
   After:
   ```python
       for row in rows:
           workspace_id = str(row["workspace_id"])
           await outbox.fanout_notification(
               session,
               workspace_id=workspace_id,
               bindings=await prompts.push_bindings(session, workspace_id),
               text=(
                   "⚠️ This workspace's Drive source is still disconnected"
                   " and has not synced since it failed. Reconnect it to"
                   " resume syncing, or pause it if this is intended."
               ),
           )
   ```

   `media_sync.py:476-497` is the same swap and additionally **deletes the in-function
   `from src.services.target import outbox` at `:484`**, which sits inside the loop body; the
   module-level import is already present (confirm with
   `grep -n "^from src.services.target import\|^import" src/services/target/media_sync.py`). Its
   `bindings` expression is conditional and must be kept as written:
   ```python
           bindings = (
               await prompts.push_bindings(s, workspace_id) if flipped == "error" else []
           )
           await outbox.fanout_notification(
               s,
               workspace_id=workspace_id,
               bindings=bindings,
               text=(
                   "⚠️ Media sync failed for this workspace's Drive"
                   f" source: {exc}. Syncing is paused until the"
                   " source is reconnected or repaired."
               ),
           )
   ```
   (`fanout_notification` over an empty list writes nothing and returns 0 — the same as the
   zero-iteration loop today.)

   `credential_lifecycle.py:365-372` keeps its `no-surface` early return at `:351-359` and swaps
   only the loop:
   ```python
           await outbox.fanout_notification(
               s, workspace_id=str(row["workspace_id"]), bindings=bindings, text=body
           )
   ```
   Note `fanout_notification` calls `str(workspace_id)` itself; passing the already-`str` value is
   harmless and keeps the diff minimal.

   Same step, the words: delete the stale paragraph at `outbox.py:240-241` —
   "Remaining callers to move: `media_sync` (two sites) and `credential_lifecycle` (one). They
   belong to #1090 D1/D2 rather than here." — and replace it with one sentence recording that the
   move happened in this PR.

   Call-site grep:
   ```
   grep -rn "fanout_notification" src storydump_cli scripts tests
   ```
   Expected before: the def at `outbox.py:223`, four callers (`reconciler.py:329`,
   `work_loop.py:753`, `scheduler.py:290`, `publish_pipeline.py:1815`), plus test references.
   Expected after: the def and **seven** callers.
   ```
   grep -rn "kind=\"notification\"" src
   ```
   Expected before: 4 (the door + the three copies). Expected after: 1 (`outbox.py` only).

   Tests: `tests/scripts/test_w6_sync_gate.py`, `tests/scripts/test_w5de_credential_lifecycle.py`
   and `tests/scripts/test_customer_notice_gate.py` must pass unchanged — they assert on the
   `channel_outbox` rows, which are byte-identical. Add an assertion to
   `tests/src/services/target/test_credential_lifecycle.py` that the reauth prompt's enqueued
   payload is exactly `{"v": 1, "text": <body>}`, so the envelope has a pin outside the door.

5. **`utcnow()` and `ms_since()` live in `src/utils/datetime_utils.py`** — the module `transit.py`
   already imports from (`transit.py:68`, `ensure_utc`). Four copies of each.

   Append to `src/utils/datetime_utils.py`:
   ```python
   def utcnow() -> datetime:
       """Now, timezone-aware, in UTC.

       Four modules had defined this privately (`publish_pipeline`, `work_loop`,
       `command_executors`, `email_sender`). It lives beside :func:`ensure_utc`
       and :func:`naive_utc` because "stored values are UTC" is one convention,
       not four.
       """
       return datetime.now(timezone.utc)


   def ms_since(started: float) -> int:
       """Milliseconds elapsed since a ``time.perf_counter()`` reading.

       The expression `publish_pipeline._ms_since` already named, written out at
       three more sites in `transit`. Every log line and audit `elapsed_ms` in
       the tier rounds the same way; one home is what keeps that true.
       """
       return int((time.perf_counter() - started) * 1000)
   ```
   (add `import time` at the top of the file).

   Then, mechanically:
   - Delete `def _utcnow()` at `publish_pipeline.py:185`, `work_loop.py:50`,
     `command_executors.py:144`, `email_sender.py:295`. In each module add
     `from src.utils.datetime_utils import utcnow` and rename every `_utcnow()` call to
     `utcnow()`. **Do not** touch the `datetime.now(timezone.utc)` expressions that are *not* in a
     `_utcnow` body — the sites that spell it inline are step 6's territory only where listed, and
     several are inside SQL parameter dicts where the diff buys nothing.
   - Delete `def _ms_since(started)` at `publish_pipeline.py:1528-1529`; import `ms_since` and
     rename its five call sites (`:1157`, `:1242`, `:1263`, `:1283`, `:1287`).
   - Replace the three inline expressions at `transit.py:459`, `:465`, `:538` —
     `elapsed_ms=int((time.perf_counter() - started) * 1000)` → `elapsed_ms=ms_since(started)` —
     extending the existing `from src.utils.datetime_utils import ensure_utc` import. If `time` is
     then unused in `transit.py`, ruff's `F401` will say so; remove the import in that case.

   Call-site greps:
   ```
   grep -rn "_utcnow" src storydump_cli scripts tests
   ```
   Expected before: 4 defs + their call sites + any test that monkeypatches one. Expected after:
   **0 lines** — if a test patches `module._utcnow`, it must be repointed to
   `module.utcnow` (the name the module now binds) in the same step; find them first with
   `grep -rn "_utcnow" tests`.
   ```
   grep -rn "perf_counter() - " src
   ```
   Expected before: 4 (`publish_pipeline.py:1529`, `transit.py:459,:465,:538`).
   Expected after: 1 — inside `datetime_utils.ms_since`.
   ```
   grep -rn "utcnow\|ms_since" src/utils/datetime_utils.py
   ```
   Expected after: the two defs.

   Test to add: `tests/src/utils/test_datetime_utils.py` (the file exists — confirm with
   `ls tests/src/utils/`) gains a pin that `ms_since` truncates rather than rounds
   (`ms_since(time.perf_counter())` is `0`, not negative) and that `utcnow().tzinfo is
   timezone.utc`.

6. **The workspace timezone is read one way per module** — `publish_pipeline.py:446`, `:968`,
   `:1676`, `:1787`, `:2091`; `command_executors.py:194` (the existing `_tz`), `:552`, `:939`.

   `_Ctx` (`publish_pipeline.py:196-222`) already carries `intent_id` and `workspace_id` as
   properties; add a third beside them:
   ```python
       @property
       def tz(self) -> str:
           """The story's effective zone. `_load`'s SELECT always projects
           `eff_tz` (`COALESCE(a.tz, w.tz)`, :400), so the fallback is for a NULL
           column, not a missing key."""
           return str(self.intent.get("eff_tz") or "UTC")
   ```
   Then replace the five reads. Four are `str(ctx.intent.get("eff_tz") or "UTC")` → `ctx.tz`. The
   fifth, `_local_date` at `:446`, is `ctx.intent["eff_tz"] or "UTC"` → `ctx.tz`; the two spellings
   are equivalent because `eff_tz` is projected unconditionally at `:400` (verify with
   `grep -n "eff_tz" src/services/target/publish_pipeline.py`, expected: `:400` is the only
   `AS eff_tz`).

   `command_executors.py` already has the helper at `:194`:
   ```python
   def _tz(intent: dict[str, Any]) -> str:
       return str(intent.get("eff_tz") or "UTC")
   ```
   Point the two open copies at it: `:552` `"tz": intent["eff_tz"] or "UTC"` → `"tz": _tz(intent)`,
   and `:939` `tz=str(row["eff_tz"] or "UTC")` → `tz=_tz(row)`.

   **Leave alone** the four single-copy sites — `reconciler.py:511`, `prompts.py:227`, `:420`,
   `outbox.py:1281`. One occurrence in a module is not a copy, and hoisting them to a shared helper
   would pull four modules into a dependency they do not need.

   Call-site grep:
   ```
   grep -rn 'or "UTC"' src storydump_cli scripts
   ```
   Expected before: 13 lines. Expected after: **6** — `_Ctx.tz`, `command_executors._tz`, and the
   four deliberately untouched single-copy sites listed above. Any other survivor means a site was
   missed.

   Tests: `tests/scripts/test_l5_pipeline_gate.py` (card lines carry the zone) and
   `tests/src/services/target/test_prompts.py` must pass unchanged.

7. **One statement retires a target's live `oauth_states`** — `identity_link.py:60-67`,
   `channel_bind.py:57-64`, `ig_login_oauth.py:169-176`, `provisioning.py:837-844`.

   The four differ only in which owner column selects the rows. `ig_login_oauth.py` already owns
   the `oauth_states` machinery (`issue_state`, `new_state`, `consume`) and both
   `identity_link` and `channel_bind` already import it (they call `ig_login_oauth.issue_state`
   two lines below their copy), so the home is there. Add above `issue_state`:

   ```python
   async def retire_live_states(
       conn,
       *,
       provider: str,
       purpose: Optional[str] = None,
       user_id=None,
       workspace_id=None,
       reconnect_target=None,
   ) -> int:
       """Consume every live state this selector matches. Returns the count.

       "Last issued wins" (`07` §2) is one security rule with four writers: a
       link mint, a bind mint, `issue_state`'s own reconnect-target retire and
       `disable_destination`'s. Four spellings is how one of them keeps a state
       tappable after the next is minted. Exactly one selector kwarg is given
       besides *provider*; the statement is built from named fragments, never
       from a caller's string.
       """
       where = ["provider = :provider", "consumed_at IS NULL"]
       params: dict[str, Any] = {"provider": provider}
       if purpose is not None:
           where.append("purpose = :purpose")
           params["purpose"] = purpose
       if user_id is not None:
           where.append("user_id = :uid")
           params["uid"] = str(user_id)
       if workspace_id is not None:
           where.append("workspace_id = :ws")
           params["ws"] = str(workspace_id)
       if reconnect_target is not None:
           where.append("reconnect_target = :target")
           params["target"] = str(reconnect_target)
       result = await conn.execute(
           text(
               "UPDATE oauth_states SET consumed_at = now()"
               " WHERE " + " AND ".join(where)
           ),
           params,
       )
       return result.rowcount
   ```

   Before (`identity_link.py:60-67`):
   ```python
       await conn.execute(
           text(
               "UPDATE oauth_states SET consumed_at = now()"
               " WHERE purpose = :purpose AND provider = :provider"
               "   AND user_id = :uid AND consumed_at IS NULL"
           ),
           {"purpose": PURPOSE, "provider": PROVIDER, "uid": str(user_id)},
       )
   ```
   After:
   ```python
       await ig_login_oauth.retire_live_states(
           conn, provider=PROVIDER, purpose=PURPOSE, user_id=user_id
       )
   ```

   The other three:
   - `channel_bind.py:57-64` → `retire_live_states(conn, provider=PROVIDER, purpose=PURPOSE,
     workspace_id=workspace_id)`.
   - `ig_login_oauth.py:169-176` (inside `issue_state`, guarded by
     `if reconnect_target is not None:`) → `await retire_live_states(conn, provider=provider,
     reconnect_target=reconnect_target)` — a local call, no module prefix. **Keep the nine-line
     comment at `:164-168` where it is**; it explains the rule, not the statement.
   - `provisioning.py:837-844` → `retired = await ig_login_oauth.retire_live_states(executor,
     provider=IG_LOGIN_PROVIDER, reconnect_target=ig_account_id)`, and the return dict at `:851`
     becomes `"states_retired": retired` (it reads `retired.rowcount` today — the helper already
     returns the int). Confirm `provisioning.py` imports `ig_login_oauth` with
     `grep -n "ig_login_oauth" src/services/target/provisioning.py`; add the import if it does not.

   **Ordering note:** the rendered `WHERE` clause reorders the predicates relative to today's text
   (`provider` first, `consumed_at IS NULL` second). `AND` is commutative and the planner is free
   to reorder anyway; no index or partial-index predicate depends on the written order. Confirm no
   test asserts the statement text: `grep -rn "SET consumed_at = now()" tests` — expected 0.

   Call-site grep:
   ```
   grep -rn "retire_live_states" src storydump_cli scripts tests
   ```
   Expected before: 0. Expected after: 1 def + 4 call sites.
   ```
   grep -rn "UPDATE oauth_states SET consumed_at" src
   ```
   Expected before: 4. Expected after: 1 — inside `retire_live_states`.

   Tests: `tests/src/services/target/test_identity_link.py`, `test_channel_bind.py`,
   `test_ig_login_oauth_connect.py` and `tests/src/services/target/test_provisioning.py` must pass
   unchanged. Add a unit test for `retire_live_states` covering each selector and the returned
   count.

8. **Migration 069's ownership predicate is written once** — `drive_credentials.py:115-116`,
   `:260-262`, `:292-294`; `google_drive_oauth.py:329-330`, `:360-361`; `workspaces.py:307-308`.

   The writer owns the shape, so the constant lives in `google_drive_oauth.py`, beside
   `store_credential` — the statement whose `ON CONFLICT … WHERE` clause IS the rule. The
   precedent for a spliced SQL-fragment constant in this tier is `outbox._TOUCHED_FLAG`
   (`outbox.py:571`, `.format()`-spliced at `:647` and `:837`); plan 01 lands the sibling
   `prompts.PUSH_BINDING_WHERE` on the same pattern.

   ```python
   #: A `gdrive` credential names no owner column — the WORKSPACE is its owner
   #: (069, #1165), and this predicate is that ownership rule. Six readers had
   #: written it out; one of them omitting `media_source_id IS NULL` the day a
   #: source-owned row returns is exactly how a workspace reads another owner's
   #: grant. A fragment, spliced — there is no caller input in it.
   WORKSPACE_GRANT_WHERE = (
       "workspace_id = :ws AND provider = :provider"
       "   AND ig_account_id IS NULL AND media_source_id IS NULL"
   )
   ```

   Before (`drive_credentials.py:113-116`):
   ```python
                       "SELECT encrypted_payload, state, expires_at"
                       " FROM oauth_credentials"
                       " WHERE workspace_id = :ws AND provider = :provider"
                       "   AND ig_account_id IS NULL AND media_source_id IS NULL"
   ```
   After:
   ```python
                       "SELECT encrypted_payload, state, expires_at"
                       " FROM oauth_credentials"
                       " WHERE " + google_drive_oauth.WORKSPACE_GRANT_WHERE
   ```

   Per-site mechanics — each keeps its own additional predicates and its own params:
   - `drive_credentials.py:260-262` (`_store_refreshed`): `" WHERE " + WORKSPACE_GRANT_WHERE +
     "   AND state = 'active' AND encrypted_payload = :seen"`.
   - `drive_credentials.py:292-294` (`_mark_expired`): the same tail.
   - `google_drive_oauth.py:329-330` (`connect_purpose`): the columns are **aliased `c.`** there
     (`c.workspace_id = :ws AND c.provider = :provider AND c.ig_account_id IS NULL AND
     c.media_source_id IS NULL`). Two options, and the builder must pick the first: change the
     statement to drop the `c` alias (`FROM oauth_credentials` with no alias, since no other
     column in that `EXISTS` is qualified) and splice the constant. Do **not** make the constant a
     `.format(alias=…)` template for one site.
   - `google_drive_oauth.py:360-361` (`store_credential`'s `ON CONFLICT … WHERE`): this clause is
     `WHERE ig_account_id IS NULL AND media_source_id IS NULL` — the *index* predicate, which
     carries no `workspace_id`/`provider` (they are the conflict target). **It is a different
     fragment and must not be spliced.** Leave it exactly as written; add a one-line comment
     pointing at `WORKSPACE_GRANT_WHERE` so a reader sees the relationship.
   - `workspaces.py:307-308` (`drive_status`): aliased `c.` again, and the statement also selects
     `c.state`, `c.updated_at`. Drop the alias throughout that one statement, then splice. Its
     params are already `ws=` and `provider=` (`GDRIVE_PROVIDER`), matching the fragment's bind
     names. Confirm `workspaces.py` imports `google_drive_oauth` —
     `grep -n "google_drive_oauth" src/services/target/workspaces.py`; if it does not, import it at
     module level (there is no cycle: `google_drive_oauth` does not import `workspaces` — verify
     with `grep -n "workspaces" src/services/target/google_drive_oauth.py`, expected 0).

   Call-site grep:
   ```
   grep -rn "WORKSPACE_GRANT_WHERE" src storydump_cli scripts tests
   ```
   Expected before: 0. Expected after: 1 def + 5 call sites.
   ```
   grep -rn "ig_account_id IS NULL AND media_source_id IS NULL" src
   ```
   Expected before: 6 (one is the `ON CONFLICT` index predicate). Expected after: **2** — the
   constant and the `ON CONFLICT` clause.

   Tests: `tests/src/services/target/test_drive_workspace_grant.py`,
   `test_google_drive_oauth.py`, `tests/scripts/test_gdrive_oauth_gate.py` must pass unchanged.

9. **Three `restate_cards` loops become one statement** — `publish_pipeline.py:972-979`
   (`_say_waiting`), `:1678-1685` (`_confirm_dry_run`), `:2093-2100` (`_confirm`), against
   `outbox.restate_everywhere` (`outbox.py:767-785`).

   `restate_everywhere_touched` (`:788-852`) selects the same bindings
   (`state = 'active' AND channel LIKE 'telegram%'`, the same predicate `prompts.push_bindings`
   uses at `:349-352`) and the same rows (`kind = 'approval_prompt'`,
   `state IN ('sent','superseded','ambiguous')`, `external_message_ref IS NOT NULL`), in one round
   trip.

   Before (`publish_pipeline.py:971-979`):
   ```python
       for binding_id in await prompts.push_bindings(session, ctx.workspace_id):
           await outbox.restate_cards(
               session,
               workspace_id=ctx.workspace_id,
               binding_id=binding_id,
               intent_id=ctx.intent_id,
               outcome_text=line,
           )
   ```
   After:
   ```python
       await outbox.restate_everywhere(
           session,
           workspace_id=ctx.workspace_id,
           intent_id=ctx.intent_id,
           outcome_text=line,
       )
   ```
   The same swap at `:1678-1685` and `:2093-2100` (both also use `ctx.workspace_id` /
   `ctx.intent_id`). None of the three passes `reply_markup`; `restate_everywhere` defaults it to
   `None` and the statement wraps it in `jsonb_strip_nulls`, so the enqueued payload is identical
   to `_supersede_payload`'s with no markup.

   **`publish_pipeline.py:1805-1814` is deliberately NOT changed** — `_restate_and_notify` needs
   the binding list for `fanout_notification` at `:1815` and for `return len(bindings)` at `:1821`,
   and it passes `reply_markup=prompts.review_keyboard(...)`. Leave it.

   Call-site grep:
   ```
   grep -rn "restate_cards\|restate_everywhere" src storydump_cli scripts tests
   ```
   Expected before: `restate_cards` — the def at `outbox.py:854` plus 4 callers in
   `publish_pipeline.py`; `restate_everywhere` — the def, `restate_everywhere_touched`, and its
   existing callers. Expected after: `restate_cards` has **1** caller
   (`publish_pipeline.py:1806`); `restate_everywhere` gains 3.

   Tests: `tests/src/services/target/test_outbox_restate.py` and
   `tests/scripts/test_l5_pipeline_gate.py` (card lines at posted / waiting / dry-run) must pass
   unchanged. Add to `test_l5_pipeline_gate.py` — or the nearest pipeline gate that already seeds
   two bindings; find it with `grep -rln "push_bindings" tests` — an assertion that a workspace
   with **two** active Telegram bindings gets a `prompt_supersede` row on both after a posted
   confirm, which is the invariant the loop→statement swap must preserve.

10. **The evidence merge is one fragment** — `publish_pipeline.py:821-829`, `:1876-1884`;
    `reconciler.py:194-214`, `:306-324`.

    The four statements share one SET clause and differ in three ways the fragment must keep: the
    seed (`'{}'` in the pipeline, `'{"v": 1}'` in the reconciler), the stamped key and value
    (`customer_notified`/`true` vs `notify_attempted_at`/`now()`), and the guards and `RETURNING`
    that only the reconciler's two carry. So the extraction is the **clause**, not the statement.

    Add to `src/services/target/intent_ledger.py` (the module that owns writes to `post_intents`),
    on the `outbox._TOUCHED_FLAG` pattern:
    ```python
    #: The `last_error->'evidence'` MERGE, as a fragment. `reconciler`'s own
    #: prose (:254-262) says this must never be a rebuild: `evidence` carries
    #: `checks`, `last_checked_at` and the trail, which is the operator's entire
    #: inheritance on a parked intent, and a `jsonb_build_object` rewrite of it
    #: notifies the customer by destroying the evidence. Four writers spelled it
    #: out; `{seed}` is what an absent `last_error` becomes, `{key}` and
    #: `{value}` are SQL literals chosen by the writer — never caller input.
    EVIDENCE_MERGE = (
        "last_error = COALESCE(last_error, CAST('{seed}' AS jsonb))"
        " || jsonb_build_object('evidence',"
        "      COALESCE(last_error->'evidence', CAST('{{}}' AS jsonb))"
        "      || jsonb_build_object('{key}', {value}))"
    )
    ```

    Before (`publish_pipeline.py:821-829`):
    ```python
                   await session.execute(
                       text(
                           "UPDATE post_intents SET last_error = COALESCE(last_error, '{}'::jsonb)"
                           " || jsonb_build_object('evidence', COALESCE(last_error->'evidence', '{}'::jsonb)"
                           "    || jsonb_build_object('customer_notified', true))"
                           " WHERE id = :intent"
                       ),
                       {"intent": ctx.intent_id},
                   )
    ```
    After:
    ```python
                   await session.execute(
                       text(
                           "UPDATE post_intents SET "
                           + intent_ledger.EVIDENCE_MERGE.format(
                               seed="{}", key="customer_notified", value="true"
                           )
                           + " WHERE id = :intent"
                       ),
                       {"intent": ctx.intent_id},
                   )
    ```
    `publish_pipeline.py:1876-1884` takes the identical After with `{"intent": intent_id}`.

    `reconciler.py:306-324` uses `seed='{"v": 1}'`, `key="customer_notified"`, `value="true"` and
    keeps its `WHERE id = :intent AND state = 'review_required' AND NOT COALESCE(...)` claim guard
    and `RETURNING id` verbatim. `reconciler.py:194-214` uses the same seed with
    `key="notify_attempted_at"`, `value="now()"` and keeps its age guard and `RETURNING id`.

    **Cast-spelling note:** the two pipeline sites write `'{}'::jsonb` and the two reconciler sites
    write `CAST('{}' AS jsonb)`. The fragment uses `CAST(...)` for all four. These are the same
    expression to Postgres; the only change is the statement's text. Confirm nothing asserts it:
    `grep -rn "customer_notified" tests | grep -i "jsonb\|COALESCE"` — expected 0 (tests read the
    column, not the SQL).

    Call-site grep:
    ```
    grep -rn "EVIDENCE_MERGE" src storydump_cli scripts tests
    ```
    Expected before: 0. Expected after: 1 def + 4 call sites.
    ```
    grep -rn "jsonb_build_object('evidence'" src
    ```
    Expected before: 4. Expected after: **1** — inside `EVIDENCE_MERGE`.

    Confirm the import direction is safe before writing it:
    `grep -n "intent_ledger" src/services/target/publish_pipeline.py src/services/target/reconciler.py`
    — both already import it (module level or in-function); use the existing binding rather than
    adding a second.

    Tests: `tests/scripts/test_customer_notice_gate.py`,
    `tests/scripts/test_l5_pipeline_gate.py` and
    `tests/src/services/target/test_reconciler_sweep.py` must pass unchanged — they are the four
    sites' behavioural pins and they are what proves this step. Add one unit test rendering
    `EVIDENCE_MERGE.format(...)` for both seeds and asserting the exact strings, so a future edit
    to the fragment shows as a diff.

11. **One direct `INSERT INTO audit_events`** — `credential_lifecycle.py:220-239`,
    `offboarding.py:278-303`, `publish_pipeline.py:479-487`, `:932-940`.

    All four write the same nine columns in the same order. Three read the actor triple from the
    GUCs identically; `offboarding` writes `current_setting('app.actor_kind'), NULL, 'system'`
    because `poller_session_factory` sets no `app.channel` and a NULL channel there would lose the
    only thing the row says about who ran the drain.

    New file `src/services/target/audit.py`. **Precedent for a module of this scope:**
    `src/services/target/_dbapi.py` (two functions, hoisted the moment a fourth reader asked the
    same question) and `readers.py` (two functions, "so a read model is one `return`"). It cannot
    live in `intent_ledger` — two of the four rows are about an `oauth_credential` and a
    `workspace`, not an intent.

    ```python
    """The one place a service writes an `audit_events` row directly.

    The triggers own state-change audit (`trg_governance_audit`). Four writers
    need a row the triggers cannot produce — a `cap_deferred`, a float wait, a
    `revoke_failed`, a parked drain — and each wrote the nine-column INSERT out
    by hand, two of them disagreeing on how the actor is recorded. The columns
    are one statement here; the actor spelling is a named fragment, because the
    two spellings are a real difference and not a drift.
    """

    #: Read the actor from the transaction's GUCs, exactly as the triggers do.
    ACTOR_FROM_GUCS = (
        "current_setting('app.actor_kind'),"
        " NULLIF(current_setting('app.actor_user_id', true), '')::uuid,"
        " NULLIF(current_setting('app.channel', true), '')"
    )
    #: A worker session that sets no `app.channel` (`poller_session_factory`),
    #: naming the channel itself so the row still says where the write came
    #: from. `offboarding`'s parked-drain record is the one writer of this shape.
    ACTOR_SYSTEM_CHANNEL = "current_setting('app.actor_kind'), NULL, 'system'"


    async def record(
        executor,
        *,
        workspace_id: str,
        entity_kind: str,
        entity_id_sql: str,
        from_state: str,
        to_state: str,
        detail: dict,
        actor_sql: str = ACTOR_FROM_GUCS,
        **params,
    ) -> None:
        """One `audit_events` row. *entity_id_sql* is a bind name or a cast of
        one (`:intent`, `CAST(:ws AS uuid)`), supplied by the writer — never by
        a caller of the writer."""
        await executor.execute(
            text(
                "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
                " from_state, to_state, actor_kind, actor_user_id, channel, detail)"
                f" VALUES (:ws, :entity_kind, {entity_id_sql},"
                f"         :from_state, :to_state, {actor_sql},"
                "         CAST(:detail AS jsonb))"
            ),
            {
                "ws": workspace_id,
                "entity_kind": entity_kind,
                "from_state": from_state,
                "to_state": to_state,
                "detail": json.dumps(detail),
                **params,
            },
        )
    ```

    **`entity_kind`, `from_state` and `to_state` become bind parameters** where they were SQL
    literals at three of the sites (`'oauth_credential'`, `'revoked'`, `'post_intent'`,
    `'approved'`, `'workspace'`, `'offboarding'`). The written value is identical; the statement
    text is not. `publish_pipeline.py:479` already binds `:state` for both state columns, so it is
    the precedent. If `audit_events.entity_kind` is an enum column rather than text, a bind will
    need an explicit cast — check with
    `grep -rn "entity_kind" src/models/target/` before writing, and add `CAST(:entity_kind AS …)`
    if so.

    Before (`offboarding.py:289-302`):
    ```python
           await session.execute(
               text(
                   "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
                   " from_state, to_state, actor_kind, actor_user_id, channel, detail)"
                   " VALUES (:ws, 'workspace', CAST(:ws AS uuid), 'offboarding',"
                   "         'offboarding', current_setting('app.actor_kind'),"
                   "         NULL, 'system', CAST(:detail AS jsonb))"
               ),
               {
                   "ws": workspace_id,
                   "detail": json.dumps({"v": 1, "event": event, **detail}),
               },
           )
    ```
    After:
    ```python
           await audit.record(
               session,
               workspace_id=workspace_id,
               entity_kind="workspace",
               entity_id_sql="CAST(:ws AS uuid)",
               from_state="offboarding",
               to_state="offboarding",
               detail={"v": 1, "event": event, **detail},
               actor_sql=audit.ACTOR_SYSTEM_CHANNEL,
           )
    ```
    The other three keep the default `actor_sql`:
    - `credential_lifecycle.py:224-239` → `entity_kind="oauth_credential"`,
      `entity_id_sql=":cid"`, `from_state="revoked"`, `to_state="revoked"`,
      `detail={"event": "revoke_failed", "reason": reason}`, `cid=credential_id`.
    - `publish_pipeline.py:477-487` → `entity_kind="post_intent"`, `entity_id_sql=":intent"`,
      `from_state=state`, `to_state=state`, plus its existing `intent=` param and detail dict.
    - `publish_pipeline.py:930-940` → the same with `from_state="approved"`,
      `to_state="approved"`.

    Both `_audit_revoke_failed` and `offboarding._audit` keep their own-transaction wrapper
    (`async with factory() as s: … await s.commit()`) exactly as written — the helper takes the
    executor and writes one statement; it does not commit. That is the whole point of both
    docstrings and must not move.

    `v1.py:266` (the documented API-side exception) is **out of scope** — it is another auditor's
    file and plan 04's area; leave it.

    Call-site grep:
    ```
    grep -rn "INSERT INTO audit_events" src storydump_cli scripts tests
    ```
    Expected before: 5 in `src` (`credential_lifecycle.py`, `offboarding.py`,
    `publish_pipeline.py` ×2, `src/api/routes/v1.py:266`) plus any test fixture. Expected after:
    **2** — `audit.record` and `v1.py:266`.
    ```
    grep -rn "audit.record\|ACTOR_FROM_GUCS\|ACTOR_SYSTEM_CHANNEL" src tests
    ```
    Expected after: the defs plus 4 call sites.

    Tests: `tests/scripts/test_offboard_gate.py` and
    `tests/src/services/target/test_credential_lifecycle.py` must pass unchanged — the first reads
    the parked-drain row's `channel`, which is the one column this step could have broken. Add a
    gate assertion in `test_offboard_gate.py` (if it does not already have one) that the row's
    `channel` is exactly `'system'` and `actor_user_id` is NULL, so the two actor spellings each
    have a pin.

12. **CHANGELOG** — add under `## [Unreleased]` → `### Changed` (create the heading only if it is
    absent; it is present today), as one bullet in the house style:

    ```markdown
    - **One spelling for the copies past three inside `src/services/target` (#NNNN).** Nine ideas the tier had written out three to thirteen times now have the one home each already had: `outbox.fanout_notification` gets the three "tell every push binding" loops its own docstring had been listing as owed since #1090; five refusal classes subclass the `RefusalError` whose constructor exists to stop a fourth copy of the same three lines; the last-issued-wins retire of `oauth_states` (`07` §2) is one `retire_live_states` with four callers instead of four statements; migration 069's ownership predicate — the workspace's ownerless `gdrive` credential — is `google_drive_oauth.WORKSPACE_GRANT_WHERE`, spliced by its five readers, so the day a source-owned row returns it cannot be one writer's omission; the `last_error->'evidence'` merge the reconciler's prose says must never be a rebuild is one fragment with four writers; the nine-column direct `audit_events` INSERT is `audit.record`, with the two actor spellings NAMED (`ACTOR_FROM_GUCS`, and the parked drain's `ACTOR_SYSTEM_CHANNEL`) rather than diverging silently; three per-binding `restate_cards` loops in the publish pipeline are `outbox.restate_everywhere`'s one statement; `_utcnow` and the elapsed-milliseconds expression live in `src/utils/datetime_utils.py` beside `ensure_utc`; and `expires_in` → `expires_at` is one guarded conversion with Google's hour named. Behaviour-preserving throughout, and where a fold would not have been it was left alone and said so: `ProvisioningRefused` keeps its bare message because `/v1` returns it as `detail`, `credential_lifecycle.ig_refresh` keeps its own `expires_in` guards because they differ, and the three incompatible ZoneInfo degrades (warn-once versus silent, two exception breadths, two return types) want a ruling on which is the house rule before they become one.
    ```

    Replace `#NNNN` with the PR number once it exists.

## Test Plan

- **Pre-change baseline**, on the parent commit, recorded to a file so the post-change run is a
  diff and not a memory:
  ```
  REQUIRE_TEST_DATABASE=1 pytest --no-cov 2>&1 | tail -40 | tee /tmp/baseline.txt
  ruff check . && ruff format --check .
  ```
  The gates that characterize this change, run first and individually so a failure is attributable:
  ```
  REQUIRE_TEST_DATABASE=1 pytest --no-cov \
    tests/scripts/test_customer_notice_gate.py \
    tests/scripts/test_l5_pipeline_gate.py \
    tests/scripts/test_w6_sync_gate.py \
    tests/scripts/test_w5de_credential_lifecycle.py \
    tests/scripts/test_offboard_gate.py \
    tests/scripts/test_gdrive_oauth_gate.py \
    tests/scripts/test_invitation_create_gate.py
  ```
  These are the seven that read the rows this PR's SQL writes: the evidence latch, the card lines,
  the sync and reauth notifications, the parked-drain audit row, the Drive grant predicate and the
  invitation refusals.

- **Targeted**, per step, run as each step lands:
  ```
  pytest tests/src/exceptions/ --no-cov                                   # step 1
  pytest tests/src/services/target/test_egress.py --no-cov                # step 2
  pytest tests/src/services/target/test_invitations.py --no-cov           # step 3
  pytest tests/src/services/target/test_credential_lifecycle.py --no-cov  # steps 4, 11
  pytest tests/src/utils/ --no-cov                                        # step 5
  pytest tests/src/services/target/test_prompts.py --no-cov               # step 6
  pytest tests/src/services/target/test_identity_link.py \
         tests/src/services/target/test_channel_bind.py \
         tests/src/services/target/test_provisioning.py --no-cov          # step 7
  pytest tests/src/services/target/test_drive_workspace_grant.py \
         tests/src/services/target/test_google_drive_oauth.py --no-cov    # step 8
  pytest tests/src/services/target/test_outbox_restate.py --no-cov        # step 9
  pytest tests/src/services/target/test_reconciler_sweep.py --no-cov      # step 10
  ```
  (If a named file does not exist, find the real one with
  `grep -rln "<symbol>" tests` before inventing a path.)

- **New/updated pins**, one per extraction so the next copy fails a test rather than a review:
  1. `tests/src/exceptions/test_base.py` — the seven `RefusalError` subclasses' messages.
  2. `tests/src/services/target/test_egress.py` — `expires_at_from`'s six cases, `True` and
     `"3600"` among them.
  3. `_dbapi.driver_error_is` — a unit test beside the existing `constraint_violated` tests.
  4. `tests/src/services/target/test_credential_lifecycle.py` — the reauth prompt's payload is
     exactly `{"v": 1, "text": …}`.
  5. `tests/src/utils/test_datetime_utils.py` — `utcnow().tzinfo`, `ms_since` truncation.
  6. `tests/src/services/target/test_ig_login_oauth_connect.py` — `retire_live_states` per selector
     and its returned count.
  7. The pipeline gate — two active bindings both get a `prompt_supersede` row after a confirm.
  8. `intent_ledger.EVIDENCE_MERGE.format(...)` renders the exact string for both seeds.
  9. `tests/scripts/test_offboard_gate.py` — the parked-drain row's `channel = 'system'`,
     `actor_user_id IS NULL`.

- **Post-change**: the same two baseline commands, diffed against `/tmp/baseline.txt`. The full
  suite must have the same pass count plus the new pins, and zero new failures or skips.

## Verification Checklist

- [ ] baseline green on the parent commit (`REQUIRE_TEST_DATABASE=1 pytest --no-cov`, recorded)
- [ ] call-site audit: every grep in steps 1–11 run, results exactly as stated — in particular
      `grep -rn "kind=\"notification\"" src` → 1, `grep -rn 'or "UTC"' src storydump_cli scripts`
      → 6, `grep -rn "UPDATE oauth_states SET consumed_at" src` → 1,
      `grep -rn "ig_account_id IS NULL AND media_source_id IS NULL" src` → 2,
      `grep -rn "jsonb_build_object('evidence'" src` → 1,
      `grep -rn "INSERT INTO audit_events" src` → 2, `grep -rn "_utcnow" src ... tests` → 0
- [ ] targeted tests + full suite green
- [ ] `ruff check . && ruff format --check .`
- [ ] manual smoke: against a local test database, drive one story to `posted` through the L5
      pipeline gate's fixture with **two** active Telegram bindings and confirm two
      `prompt_supersede` rows and one outcome line each (step 9's invariant); then run the
      offboard gate's parked-drain path and read the `audit_events` row back with
      `SELECT actor_kind, actor_user_id, channel FROM audit_events WHERE entity_kind = 'workspace'`
      — it must be `('system', NULL, 'system')` exactly as before (step 11's invariant).
      No production database, no `storydump` write verb, no `python -m src.main`.
- [ ] `CHANGELOG.md` entry under `## [Unreleased]` → `### Changed`
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

- **Do not fold in any flagged defect.** The master audit's "Questions" list eight that touch this
  tier, and every one of them changes behaviour to fix:
  - **A1** the 7-vs-30-day repost-lock fallback (`publish_pipeline.py:263`) — an owner ruling;
    step 9 and step 10 edit the same functions, so the temptation is real. Do not change the
    default.
  - **B3** the two hand-written `INSERT INTO jobs` (`media_sync.py:619`, `offboarding.py:306`) —
    routing them through `jobs.enqueue` would start writing the `deadline_at` #1288 added. Step 4
    and step 11 both edit those two files; leave the INSERTs alone.
  - **B4** `bindings.repoint` swallowing a unique violation (`bindings.py:227-245`) — step 1 edits
    `BindingRefused` in that file. Do not touch `repoint`.
  - **B2** the `_IN_TRANSACTION` tripwire — step 11 edits two own-transaction helpers whose
    docstrings claim a floor that does not enforce. Correct nothing about that claim here.
  - **B16** the `category_mix` v1 compat shim (`category_mix.py:266-307`) — step 1 edits
    `MixInvalid` in that file. Do not remove the shim.
  - **B17** the tenant-less credential read (`ig_credentials.py:63-80`) — not in this PR's file
    list at all; keep it that way.
  - **C1** the blank tap counts and **C5** the `/health.version` drift — the API's, plan 04's area.
- **Do not add a Python pre-check that duplicates a database rule.** Step 8's predicate is migration
  069's; step 7's retire is `07` §2's. Both are SQL and stay SQL. Do not "helpfully" add a
  `SELECT … EXISTS` before an `ON CONFLICT`, and do not add a Python guard for
  `uq_credential_per_workspace` — the database is the authority.
- **Do not extract the two-site copies.** The rule is three. `google_oidc.py:136-139` and
  `:165-168` are two; `publish_pipeline.py:821` and `:1876` are two identical statements but they
  join the reconciler's two through a *fragment*, not a shared function that would have to
  parameterise the guards. Do not invent `stamp_evidence(session, intent_id, **keys, claim=False)`
  — the four sites' guards, seeds and `RETURNING` clauses do not collapse into one signature
  without changing at least one row.
- **Do not normalise the excluded sites.** `ProvisioningRefused`'s bare message,
  `credential_lifecycle.ig_refresh`'s `expires_in` handling and `resp.json()`, the three ZoneInfo
  degrades, `invitations.py:199`'s class-based unique match, `workspaces.py:127`'s
  `constraint_name` read, `publish_pipeline.py:1805-1814`'s loop, `google_drive_oauth.py:360-361`'s
  `ON CONFLICT` predicate, and the four single-copy `or "UTC"` sites are all named above with the
  exact reason. Each is a deliberate non-change.
- **Do not decompose anything.** `_ladder`, `_run_job`, `_retry_or_poison`, `_run_sync` and
  `list_changes` are plans 06 and 08. This PR touches lines inside some of them; it does not move
  them.
- **Do not run** `python -m src.main`, any `storydump` write verb (`approve`, `cancel`, `resolve`,
  `tokens revoke`, `webhook register|deregister`), or `python -m scripts.migration_runner apply`.
  Nothing in this PR needs a migration, a deploy or a posting action.

## Related

- `00_TECH_DEBT.md` — the audit this plan is row 03 of.
- `01_one-spelling.md` — lands `prompts.PUSH_BINDING_WHERE` and the vocabulary constants this
  plan's steps 4 and 8 sit beside. Must merge first.
- `04_rule-of-three-api-cli.md` — the same axis in `src/api` and the CLI; `v1.py:266`'s
  `audit_events` INSERT belongs to that side, not this one.
- `06_publish-pipeline-shape.md`, `07_executors-and-tap.md`, `08_integrations-shape.md` — the
  decompositions that wait on these extractions.
- `.claude/rules/development-patterns.md` (services are modules of functions taking the executor
  first), `.claude/rules/database.md` (hand-written SQL under the unit of work; the DB is the
  authority), `.claude/rules/testing.md`, `.claude/rules/changelog.md`.
- Research evidence: `research/pipeline-worker.md` (TD-A8, TD-A9, TD-A10, TD-A19),
  `research/integrations-identity.md` (TD-B7, TD-B10, TD-B11, TD-B12, TD-B13, TD-B14).

## Origin

`/artemis-skills:audit tech debt`, whole repo, `main` at `0966771` (2026-09-20). Row 03 of the
remediation matrix in `00_TECH_DEBT.md`. Every `path:line` above was re-read against the working
tree while this plan was written; three sub-findings were withdrawn on that re-reading and are
recorded in the "Withdrawn on re-reading" table rather than planned against.
