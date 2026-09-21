---
title: "The API's copies past three — the 404, the web channel, the refusal-handler shape, and the four router seams that move to principal.py"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:medium, api]
links: []
---

# 04 — The API's copies past three

| | |
|---|---|
| **PR title** | The API's copies past three — the 404, the web channel, the refusal-handler shape, and the four router seams that move to `principal.py` |
| **Risk** | Medium — four steps are mechanical, but step 5 moves the seam every router's tenant transaction runs through and that `tests/src/api/conftest.py` monkeypatches, so a missed call site is a test that silently opens a real unit of work |
| **Effort** | M (≈ 4–6 hours) |
| **Files modified** | `src/api/app.py`, `src/api/principal.py`, `src/api/routes/v1.py`, `src/api/routes/tokens.py`, `src/api/routes/ops.py`, `src/api/routes/auth.py`, `tests/src/api/conftest.py`, `tests/mutations/cli_v2_01.sh`, `tests/mutations/cli_v2_02.sh`, `CHANGELOG.md` |
| **Findings addressed** | TD-C10, TD-C15, TD-C17, TD-C18, TD-C19 (partial — see step 3) |
| **Depends on** | `01_one-spelling.md` (it edits `src/exceptions/tenancy.py:73` `TokenRefused.REASONS`, which step 4's handler table is pinned against by `tests/src/api/test_app_factory.py:177`) |
| **Blocks** | `09_composition-roots.md` (it decomposes `create_app` and moves `_register_webhook` / `_sample_webhook_live` out of `app.py`; step 1 here resolves the `env` binding those moves inherit) |

## Summary

`src/api/` has the same shape of debt as the services tier, one layer up. Eight routes raise the
house-rule 404 by hand. Three routers reach into the biggest router for four helpers whose names
say `_private` while the call sites say everyone uses them — and the shared test seam patches one
of them by that private name. Five exception handlers in `app.py` repeat one seven-line shape.
`"web"` is spelled in four places. `os.environ if env is None else env` appears five times inside
one function, beside a duplicate import and an unnamed `60`.

This PR gives each one home. **Nothing changes behaviour**: every status, body, log line and
statement stays byte-identical, and the two parts of TD-C19 that would have changed something —
the second copy of the callback unit-of-work construction, and routing `actor_kind` through
`Principal` where no `Principal` exists — are withdrawn with their reasons. Step 5 is the only
structural move, and it carries the full call-site list including the two mutation battery
scripts that embed the old call text verbatim (which the research did not list, and which would
otherwise degrade to a NO-TEST-SELECTED non-kill).

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-C17 | `src/api/app.py:63,:65`, `:498`, `:566,:569,:588,:618,:620`, `:696` | A duplicate `webhooks` import, an unnamed `60`, `os.environ if env is None else env` ×5, a `getattr` for an attribute always set |
| TD-C18 | `v1.py:337,:437,:550,:576,:754,:1014`; `tokens.py:171,:245` | `HTTPException(status_code=404, detail="not found")` ×8 — the "no existence oracle" rule (`07` §5) spelled eight times |
| TD-C19 | `principal.py:91`, `v1.py:119`, `auth.py:365,:453` | `"web"` spelled four times (the 7-line UoW copy withdrawn — step 3) |
| TD-C15 | `app.py:288-296,:298-306,:314-326,:328-336,:349-357` | `TABLE.get(exc.reason) → _unmapped → log → JSONResponse` ×5 |
| TD-C10 | defs `v1.py:135,:154,:186,:596`; callers `tokens.py` ×8, `ops.py` ×2, `tests/src/api/conftest.py:171` | Three routers reach `v1._open_tenant` / `_member` / `_admin` / `_json_object` as private names |

### Withdrawn on re-reading

| ID (part) | Reason |
|---|---|
| TD-C19, the `_callback_uow(request, row)` helper | The seven-line construction appears at `auth.py:360-366` and `:448-454` — **twice**. Two is coincidence; the rule is three. The two bodies also diverge immediately after (`:367` opens the uow and stores a Drive credential; `:455` opens it inside a `try` and runs an admin re-check first), so a helper would return a `uow` and save four lines at two sites. Withdrawn; only the `channel="web"` literal inside them, which recurs four times, is folded (step 3). |
| TD-C19, `actor_kind` via `Principal` | `principal.py:107-110` derives `actor_kind` from a `Principal`. Neither OAuth callback has one — they have an `oauth_states` row and no session yet, which is the whole point of a callback. Passing `actor_kind="user"` there is the correct literal, not a copy of the property. No change. |

## Dependencies

- **`01_one-spelling.md` must merge first.** It edits `src/exceptions/tenancy.py:73`
  (`TokenRefused.REASONS`, TD-C9), and `tests/src/api/test_app_factory.py:177-183` asserts
  `set(app._TOKEN_STATUS) == set(TokenRefused.REASONS)`. Step 4 rewrites the handler that reads
  `_TOKEN_STATUS`; landing both at once makes a failure of that pin ambiguous. Rebase on 01's
  merge commit.
- `09_composition-roots.md` depends on this one and will move `_register_webhook`,
  `_sample_webhook_live` and `_telegram_transport` out of `app.py`. Step 1's single `env` binding
  is the thing that makes those moves a straight cut rather than five call-site edits; do not skip
  it.

## Implementation Plan

### Steps

1. **Four small repeats in `src/api/app.py`** — `:63`/`:65`, `:498`, `:566-620`, `:696`. Each is
   independent; all four are in one file and one step.

   **(a) The duplicate `webhooks` import.** `:63` imports the module and `:65` imports its router
   under a second name; the module binding is already used at `:619` and `:637`.
   Before:
   ```python
   from src.api.routes import webhooks
   from src.api.routes.meta import router as meta_router
   from src.api.routes.webhooks import router as webhooks_router
   ```
   After:
   ```python
   from src.api.routes import webhooks
   from src.api.routes.meta import router as meta_router
   ```
   and at `:673`, `app.include_router(webhooks_router, prefix="/webhooks")` →
   `app.include_router(webhooks.router, prefix="/webhooks")`.
   Grep: `grep -rn "webhooks_router" src storydump_cli scripts tests` — expected before: 2
   (`app.py:65`, `:673`). Expected after: **0**. If a test imports `app.webhooks_router`, the grep
   will show it; re-point it to `webhooks.router` in this step.

   **(b) The sampler's interval gets a name.** `:498` `await asyncio.sleep(60)` sits inside
   `_sample_webhook_live`, whose docstring at `:467` says "every minute". Add beside the other
   module constants near `:169`:
   ```python
   #: How often `_sample_webhook_live` re-reads what Telegram holds. The CLI's
   #: `storydump health` reads the cached sample and states this bound in its
   #: help; the number should be findable from both ends.
   WEBHOOK_LIVE_SAMPLE_SECONDS = 60
   ```
   and `:498` becomes `await asyncio.sleep(WEBHOOK_LIVE_SAMPLE_SECONDS)`.
   Grep: `grep -rn "WEBHOOK_LIVE_SAMPLE_SECONDS" src storydump_cli scripts tests` — expected
   before 0, after 2 (the constant and its one use). If a test sleeps or patches
   `asyncio.sleep` around this loop, it keeps working — the value is unchanged.

   **(c) One `env` binding.** `os.environ if env is None else env` appears at `:566`, `:569`,
   `:588`, `:618`, `:620`. Make it the **first statement of `create_app`**, before `_lifespan` is
   defined at `:556` — the closure then captures the resolved mapping:
   ```python
   def create_app(
       *, engine: Optional[AsyncEngine] = None, env: Optional[Mapping[str, str]] = None
   ) -> FastAPI:
       """…"""
       # Resolved once: `os.environ` is a live mapping, so binding it here reads
       # exactly what reading it per call site read — five spellings of one
       # decision, and `create_app(env=…)`'s seam is now a single place.
       env = os.environ if env is None else env
   ```
   Then the five sites become `env`. **Check first** that nothing later in `create_app` tests
   `env is None` to mean "no override was given":
   `grep -n "env is None" src/api/app.py` — expected: 5 lines before, all of them the expression
   being replaced; **0** after. If any sixth site exists, stop and report.

   **(d) The needless `getattr`.** `:696`
   `app.state.pool_watch.snapshot() if getattr(app.state, "pool_watch", None) is not None else None`
   — `:621` assigns `app.state.pool_watch` unconditionally (it may be `None`, but the attribute
   always exists). After:
   ```python
               "pool": (
                   app.state.pool_watch.snapshot()
                   if app.state.pool_watch is not None
                   else None
               ),
   ```
   Confirm the unconditional assignment with `grep -n "app.state.pool_watch" src/api/app.py` —
   expected: the assignment at `:621`, the `if` at `:623`, and the read at `:696`, with no branch
   between `create_app`'s entry and the health route that could skip `:621`.

   Test to update: none. `tests/src/api/test_app_factory.py` drives `create_app` with and without
   `env=` and must pass unchanged — it is the characterization test for all four sub-steps.

2. **One 404** — `v1.py:337,:437,:550,:576,:754,:1014`; `tokens.py:171,:245`.

   **Checked first, as instructed:** the only refusal→status machinery in `src/api/` is
   `app.py:_register_handlers` (`:272-374`), which maps *typed service refusals carrying a
   `reason`* to a status. A `None` row is not a typed refusal and reaches no handler — these eight
   sites raise `HTTPException` directly, which is the correct FastAPI shape. `src/api/principal.py`
   is where the cross-router gates that raise `HTTPException` already live (`require_engine:121`,
   `require_own_workspace:259`, `require_session:266`). That is the home; nothing needs inventing
   beyond the one function.

   Add to `principal.py`, beside `require_own_workspace`:
   ```python
   def not_found() -> HTTPException:
       """The house 404: a row the caller may not see and a row that is not
       there answer identically (`07` §5 — no existence oracle).

       A FUNCTION rather than a module constant, deliberately: a raised
       exception instance carries `__traceback__` and `__context__`, so a
       shared one would pin a request's frames until the next raise replaced
       them. The detail string is the thing worth having in one place.
       """
       return HTTPException(status_code=404, detail="not found")
   ```

   Before (`v1.py:336-337`):
   ```python
       if user is None:
           raise HTTPException(status_code=404, detail="not found")
   ```
   After:
   ```python
       if user is None:
           raise principal.not_found()
   ```
   The same two-line edit at all eight sites. `v1.py` imports names from `src.api.principal` at
   `:44-50` and imports modules with `from src.api import google_client, instagram_client` at
   `:51`; extend that line to `from src.api import google_client, instagram_client, principal`.
   `tokens.py:23-29` imports names from `src.api.principal`; add `from src.api import principal`
   beside its `from src.api.routes import v1` at `:30`.

   **Do not touch `v1.py:1039`** — `HTTPException(status_code=404, detail=f"unknown command
   {command!r}")` is a different body and a different answer.

   Call-site grep:
   ```
   grep -rn 'HTTPException(status_code=404\|HTTPException(404' src storydump_cli scripts tests
   ```
   Expected before: 9 lines in `src` (the 8 plus `v1.py:1039`). Expected after: **2** — `v1.py:1039`
   and the body of `principal.not_found`.
   ```
   grep -rn "not_found" src/api
   ```
   Expected after: 1 def + 8 raises.

   Tests: `tests/src/api/test_v1_routes.py` and `tests/src/api/test_token_routes.py` assert
   `404` and `{"detail": "not found"}` and must pass unchanged. Add one pin in
   `tests/src/api/test_token_principal.py` (or the nearest `principal` test — find it with
   `grep -rln "require_own_workspace" tests`) asserting
   `not_found().status_code == 404 and not_found().detail == "not found"` and that two calls
   return **distinct** objects, so the constant-vs-factory decision has a test.

3. **`"web"` is spelled once** — `principal.py:91`, `v1.py:119`, `auth.py:365`, `:453`.

   Add to `principal.py` beside `COOKIE` at `:52`:
   ```python
   #: The `command_dedup.channel` and `app.channel` GUC value for the web
   #: surface (`webhook_ingress.CHANNELS`). The browser's sessions, the two
   #: OAuth callbacks and the v1 command adapter are one channel; four
   #: spellings of it is how a dedup row lands under a name nothing reads.
   WEB_CHANNEL = "web"
   ```
   Then:
   - `principal.py:91` `channel: str = "web"` → `channel: str = WEB_CHANNEL` (the dataclass field
     default; the constant is defined above the class at `:52`, so this is valid at class-body
     evaluation time).
   - `v1.py:119` keeps its name and its comment, and reads the constant:
     ```python
     #: `command_dedup.channel` for this adapter (`webhook_ingress.CHANNELS`).
     CHANNEL = principal.WEB_CHANNEL
     ```
     (`v1.py:1058` reads `CHANNEL` and is unchanged; step 2 already added the `principal` module
     import.)
   - `auth.py:365` and `:453` `channel="web"` → `channel=principal.WEB_CHANNEL`. Confirm `auth.py`
     imports the module: `grep -n "principal" src/api/routes/auth.py` — it imports
     `require_engine` and others from `src.api.principal`; add `from src.api import principal` if
     the module itself is not bound.

   **Leave `actor_kind="user"` at `auth.py:363` and `:451` as written** — see the Withdrawn table.

   Call-site grep:
   ```
   grep -rn '"web"' src/api
   ```
   Expected before: 4 (`principal.py:91`, `auth.py:365`, `:453`, `v1.py:119`). Expected after:
   **1** — the `WEB_CHANNEL` definition.
   ```
   grep -rn "v1.CHANNEL\|from src.api.routes.v1 import CHANNEL\|WEB_CHANNEL" src storydump_cli scripts tests
   ```
   Run the first form **before** editing: if a test reads `v1.CHANNEL`, the name still resolves
   after this step (it is reassigned, not deleted), so nothing breaks — but record the result so
   the after-grep is comparable.

   Tests: `tests/src/api/test_auth_routes.py` (the Drive and Instagram callbacks) and the v1
   command tests must pass unchanged — the written value is identical.

4. **One refusal-handler shape** — `src/api/app.py:288-296`, `:298-306`, `:314-326`, `:328-336`,
   `:349-357`.

   All five do `status = TABLE.get(exc.reason)`, fall through to `_unmapped(request, exc)` on a
   miss, and answer with a `JSONResponse`. They differ on exactly two axes, and the factory must
   carry both:

   | Handler | Table | Logs? | Body |
   |---|---|---|---|
   | `TenantResolutionError` `:288` | `_TENANT_STATUS` | yes | `{"detail": _TENANT_DETAIL[status]}` — **no `reason` key**, and the detail is keyed by **status** |
   | `TokenRefused` `:298` | `_TOKEN_STATUS` | yes | `{"detail": str(exc), "reason": exc.reason}` |
   | `CommandRefused` `:314` | `_COMMAND_STATUS` | **no** | `{"detail": str(exc), "reason": exc.reason}`, or the `CommandNotBuilt` body |
   | `ProvisioningRefused` `:328` | `_PROVISIONING_STATUS` | yes | `{"detail": str(exc), "reason": exc.reason}` |
   | `InvitationRefused` `:349` | `_INVITATION_STATUS` | **no** | `{"detail": _INVITATION_DETAIL[exc.reason], "reason": exc.reason}` |

   Add above `_register_handlers`:
   ```python
   def _reason_detail(exc, status: int) -> dict:
       """The ordinary refusal body: the message, and the machine-routable
       reason the web's `target-api.ts::readError` matches on."""
       return {"detail": str(exc), "reason": exc.reason}


   def _mapped(table: dict, content=_reason_detail, *, log: bool = True):
       """A handler for a refusal whose `reason` this table maps to a status.

       The five handlers below had each written out the same decision — look
       the reason up, fall through to `_unmapped` when it is not there (which
       is the pin `TestRefusalMappingsAreTotal` exists to keep honest), then
       answer. What actually varies is the body and whether the refusal is
       logged, so those are the arguments; everything else is this function.
       """

       async def handler(request: Request, exc):
           status = table.get(exc.reason)
           if status is None:
               return _unmapped(request, exc)
           if log:
               logger.info("refused %s %s: %s", request.method, request.url.path, exc)
           return JSONResponse(status_code=status, content=content(exc, status))

       return handler
   ```

   Before (`app.py:288-296`):
   ```python
       @app.exception_handler(TenantResolutionError)
       async def _tenant(request: Request, exc: TenantResolutionError):
           status = _TENANT_STATUS.get(exc.reason)
           if status is None:
               return _unmapped(request, exc)
           logger.info("refused %s %s: %s", request.method, request.url.path, exc)
           return JSONResponse(
               status_code=status, content={"detail": _TENANT_DETAIL[status]}
           )
   ```
   After (inside `_register_handlers`, in the same position so the file's reading order is
   unchanged):
   ```python
       app.add_exception_handler(
           TenantResolutionError,
           # No `reason` on the wire: the web surface must not learn which of
           # "not a member", "no such workspace" and "disabled" it met.
           _mapped(_TENANT_STATUS, lambda exc, status: {"detail": _TENANT_DETAIL[status]}),
       )
       app.add_exception_handler(TokenRefused, _mapped(_TOKEN_STATUS))
       app.add_exception_handler(ProvisioningRefused, _mapped(_PROVISIONING_STATUS))
       app.add_exception_handler(
           InvitationRefused,
           _mapped(
               _INVITATION_STATUS,
               lambda exc, status: {
                   "detail": _INVITATION_DETAIL[exc.reason],
                   "reason": exc.reason,
               },
               log=False,
           ),
       )
       app.add_exception_handler(
           CommandRefused, _mapped(_COMMAND_STATUS, _command_body, log=False)
       )
   ```
   with the `CommandNotBuilt` branch lifted verbatim into a named body function beside
   `_reason_detail`:
   ```python
   def _command_body(exc, status: int) -> dict:
       if isinstance(exc, CommandNotBuilt):
           return {"command": exc.command, "detail": "not built", "reason": "not_built"}
       return _reason_detail(exc, status)
   ```

   **Do not touch** `_pool_saturated` (`:275`), `_token_args` (`:308`), `_mix` (`:339`),
   `_replayed` (`:358`) or `_conflict` (`:364`) — none of them looks a reason up in a table, and
   `_mix` deliberately answers 400 with no table at all.

   Notes the builder must honour:
   - `app.add_exception_handler(Cls, fn)` and `@app.exception_handler(Cls)` register the same way;
     the decorator form is sugar for it. The keys and order of `app.exception_handlers` are
     unchanged.
   - The comment blocks currently attached to each handler body (`_mix`'s three lines, the
     `CommandNotBuilt` branch's context) move with the code they explain, not to the registration.
   - The tables `_TENANT_STATUS`, `_TENANT_DETAIL`, `_TOKEN_STATUS`, `_COMMAND_STATUS`,
     `_PROVISIONING_STATUS`, `_INVITATION_STATUS`, `_INVITATION_DETAIL` (`:169-265`) stay exactly
     where they are with their comments — `TestRefusalMappingsAreTotal` imports them by name from
     the module.

   Call-site grep:
   ```
   grep -rn "_TENANT_STATUS\|_TOKEN_STATUS\|_COMMAND_STATUS\|_PROVISIONING_STATUS\|_INVITATION_STATUS\|_INVITATION_DETAIL\|_TENANT_DETAIL" src storydump_cli scripts tests
   ```
   Expected before: the 7 definitions, 7 reads in `_register_handlers`, and the 4 reads in
   `tests/src/api/test_app_factory.py:149,167,168,176,183-184`. Expected after: the same
   definitions, the same test reads, and the reads now inside the `add_exception_handler` calls —
   **no name disappears**.
   ```
   grep -rn "_unmapped" src storydump_cli scripts tests
   ```
   Expected before: 1 def + 5 uses + the test at `test_app_factory.py:186-200`. Expected after:
   1 def + **1** use (inside `_mapped`) + the test.

   Tests: `tests/src/api/test_app_factory.py:141-212` is the characterization test for this step
   and must pass unchanged — in particular
   `test_an_unmapped_reason_is_a_500_not_a_guessed_client_status`, which drives a real request
   through `_unmapped`. Add two cases to that class: a `TenantResolutionError` whose reason **is**
   mapped answers with a body carrying **no** `reason` key, and an `InvitationRefused` answers
   with `_INVITATION_DETAIL[reason]` rather than `str(exc)` — the two bodies the factory could
   most plausibly have flattened.

5. **The four shared router seams move to `src/api/principal.py`** — defs `v1.py:135`
   (`_open_tenant`), `:154` (`_member`), `:186` (`_json_object`), `:596` (`_admin`).

   `principal.py` already holds the cross-router seams (`require_engine`, `require_own_workspace`,
   `require_session`, `current_principal`) and imports only `src.config.settings`,
   `src.exceptions.tenancy`, `src.services.target.{service_tokens,sessions,vocabulary}` — so
   moving these four introduces no cycle (`principal.py` imports nothing from `src.api.routes`;
   confirm with `grep -n "src.api.routes" src/api/principal.py`, expected **0**).

   Move and rename, keeping every docstring and body verbatim:

   | From | To |
   |---|---|
   | `v1._open_tenant` | `principal.open_tenant` |
   | `v1._member` | `principal.member_session` |
   | `v1._admin` | `principal.admin_session` |
   | `v1._json_object` | `principal.json_object` |

   `principal.py` gains the imports these four need: `json`, `Any` (already imported? check
   `grep -n "^from typing\|^import json" src/api/principal.py`), `asynccontextmanager` from
   `contextlib`, `unit_of_work` (find its current import in `v1.py` with
   `grep -n "unit_of_work" src/api/routes/v1.py`) and `tenant_resolution` from
   `src.services.target`.

   **Inside `principal.py`, `member_session` and `admin_session` must call `open_tenant` as a bare
   module global** (exactly as `_member`/`_admin` call `_open_tenant` today at `v1.py:156` and
   `:598`). That is what keeps the conftest's monkeypatch effective through both gates.

   **Every caller outside `principal.py` must call through the module attribute**
   (`principal.open_tenant(...)`), never `from src.api.principal import open_tenant`. A
   from-import binds the original function at import time and the monkeypatch would not reach it —
   this is the one non-mechanical rule in the step and a violation of it produces tests that
   silently open a real unit of work against the test engine.

   Before (`v1.py:166`):
   ```python
       async with _member(request, str(ws), principal) as session:
   ```
   After:
   ```python
       async with principal_mod.member_session(request, str(ws), principal) as session:
   ```
   **Name collision to resolve first:** `v1.py`, `tokens.py` and `ops.py` all use `principal` as a
   *parameter* name on nearly every route (`principal: Principal = Depends(require_session)`), so
   the module cannot be bound as `principal`. Bind it as `principal_mod`:
   `from src.api import principal as principal_mod` in all three routers, and use
   `principal_mod.<name>(...)` at every call site. **This supersedes the shorter `principal.`
   spelling used in steps 2 and 3** — do step 5 last and, when it lands, convert the
   `principal.not_found()` and `principal.WEB_CHANNEL` references those steps added to
   `principal_mod.` in the same commit. (`auth.py` has no `principal` parameter — check with
   `grep -n "principal" src/api/routes/auth.py` — so it may keep `from src.api import principal`;
   use `principal_mod` there too for one spelling across the package.)

   Exact call-site list — run each grep, make the edit, re-run:

   ```
   grep -n "_open_tenant\|_member(\|_admin(\|_json_object" src/api/routes/v1.py
   ```
   Expected before: **32** lines (4 defs, 2 internal `_open_tenant` calls inside `_member`/`_admin`
   which move with them, 10 `_member(` route bodies, 11 `_admin(` route bodies, 4 `_json_object`
   route bodies, and the `_open_tenant` call at `:227`). Expected after: **0**.

   ```
   grep -n "v1\._open_tenant\|v1\._member\|v1\._admin\|v1\._json_object" src/api/routes/tokens.py src/api/routes/ops.py
   ```
   Expected before: 10 — `tokens.py:94, 127, 184, 185, 205, 210, 234, 240` and `ops.py:56, 59`.
   Expected after: **0**. If `tokens.py`'s and `ops.py`'s `from src.api.routes import v1` (at
   `:30` and `:28`) then has no other use, remove it — check with
   `grep -n "\bv1\b" src/api/routes/tokens.py src/api/routes/ops.py` after the edit.

   ```
   grep -rn "_open_tenant\|_json_object\|v1\._member\|v1\._admin" tests
   ```
   Expected before: `tests/src/api/conftest.py:171`
   (`monkeypatch.setattr(v1, "_open_tenant", open_tenant)`), plus
   `tests/mutations/cli_v2_01.sh:118,120` and `tests/mutations/cli_v2_02.sh:75`, which embed
   `async with v1._open_tenant(request, str(ws), principal) as session:` as literal mutation
   source and target text. (`src/services/target/ig_login_oauth.py:542,596,625,647` also match
   `_json_object` — that is an **unrelated** private helper in a different module; do not touch
   it.) Expected after: **0** matches on `_open_tenant`; the `ig_login_oauth` hits remain.

   The three test-side edits:
   - `tests/src/api/conftest.py:171` →
     `monkeypatch.setattr(principal, "open_tenant", open_tenant)`, with
     `from src.api import principal` added to the conftest's imports. The `v1` import at the top of
     the conftest stays if other fixtures use it — check with `grep -n "\bv1\b" tests/src/api/conftest.py`.
   - `tests/mutations/cli_v2_01.sh:118,120` and `cli_v2_02.sh:75` — update both the *from* and
     *to* strings to the new call text (`principal_mod.open_tenant(...)`) and the file each
     mutation targets if it changed. **Then re-run the two batteries on the committed tree**: a
     mutation whose source string no longer appears selects nothing and reports NO TEST SELECTED,
     which is not a kill. A battery that does not kill is a failed step, not a passing one.

   Finally, **delete the `# --- seams ---` section header at `v1.py:132`** only if nothing is left
   under it — `_collection` (`:163`) and `_idempotency_key` (`:173`) stay in `v1.py` (one caller
   each; below the rule of three), so the header stays with them.

   Tests: `tests/src/api/test_v1_routes.py`, `test_token_routes.py` and `test_ops_routes.py` must
   pass unchanged; they are the proof the seam still resolves. Add one pin in
   `tests/src/api/conftest.py`'s module or `test_v1_routes.py` asserting that the `tenant` fixture
   was actually exercised — e.g. a route test that asserts `tenant` recorded a `("uow", …)` entry
   — so a future from-import that bypasses the patch fails loudly instead of quietly hitting the
   engine.

6. **CHANGELOG** — add under `## [Unreleased]` → `### Changed`, one bullet in the house style:

   ```markdown
   - **The API's copies past three each have one home (#NNNN).** The house 404 — a row you may not see and a row that is not there answer identically (`07` §5) — was spelled at eight routes and is now `principal.not_found()`; `"web"`, the channel the browser's sessions, both OAuth callbacks and the v1 command adapter share, was spelled four times and is `principal.WEB_CHANNEL`; the five refusal handlers in `create_app` that each wrote out "look the reason up, fall through to `_unmapped`, answer" are one `_mapped` factory with the two things that actually varied — the body and whether the refusal is logged — as its arguments, the tables and their comments untouched where `TestRefusalMappingsAreTotal` reads them; and the four seams three routers had been reaching for as `v1._open_tenant`, `v1._member`, `v1._admin` and `v1._json_object` now live in `src/api/principal.py` beside the other cross-router gates, as public names, with the shared test seam patching `principal.open_tenant` and every caller reaching it through the module so the patch still lands. `src/api/app.py` also loses a duplicate `webhooks` import, an unnamed `60` (`WEBHOOK_LIVE_SAMPLE_SECONDS`), a `getattr` for an attribute `create_app` always sets, and five spellings of `os.environ if env is None else env` — one binding at the top of the factory, which is what makes the composition-root split a straight cut. Behaviour-preserving: every status, body, log line and written value is unchanged, and the callback unit-of-work construction was left alone because it appears twice, not three times.
   ```

   Replace `#NNNN` with the PR number once it exists.

## Test Plan

- **Pre-change baseline**, on the parent commit:
  ```
  REQUIRE_TEST_DATABASE=1 pytest --no-cov 2>&1 | tail -40 | tee /tmp/baseline-04.txt
  ruff check . && ruff format --check .
  ```
  The gates that characterize this change:
  ```
  REQUIRE_TEST_DATABASE=1 pytest --no-cov \
    tests/src/api/test_app_factory.py \
    tests/src/api/test_v1_routes.py \
    tests/src/api/test_token_routes.py \
    tests/src/api/test_ops_routes.py \
    tests/src/api/test_auth_routes.py \
    tests/src/api/test_token_principal.py \
    tests/scripts/test_service_tokens_gate.py \
    tests/scripts/test_ops_views_gate.py
  ```
  `test_app_factory.py:141-212` (`TestRefusalMappingsAreTotal`) is step 4's characterization test;
  the two `tests/scripts/` gates drive the real app as `svc_ingress` through the seams step 5
  moves, which is what proves the move did not quietly bypass a tenant gate.

- **Targeted**, per step:
  ```
  pytest tests/src/api/test_app_factory.py --no-cov                      # steps 1, 4
  pytest tests/src/api/test_v1_routes.py tests/src/api/test_token_routes.py --no-cov  # step 2
  pytest tests/src/api/test_auth_routes.py --no-cov                      # step 3
  pytest tests/src/api/ --no-cov                                         # step 5
  ```

- **New/updated pins:**
  1. `principal.not_found()` — status, detail, and two calls returning distinct objects.
  2. `test_app_factory.py` — a mapped `TenantResolutionError` body carries no `reason` key; an
     `InvitationRefused` body carries `_INVITATION_DETAIL[reason]`, not `str(exc)`.
  3. A route test asserting the `tenant` fixture recorded its `("uow", …)` entry, so a from-import
     that bypasses the monkeypatch fails.
  4. `tests/mutations/cli_v2_01.sh` and `cli_v2_02.sh` — updated mutation text, **re-run on the
     committed tree**, every mutant killed; a NO TEST SELECTED or KILLED BY ERROR line is a
     failure, not a pass.

- **Post-change**: the same two baseline commands, diffed against `/tmp/baseline-04.txt`. Same
  pass count plus the new pins; zero new failures or skips.

## Verification Checklist

- [ ] baseline green on the parent commit (`REQUIRE_TEST_DATABASE=1 pytest --no-cov`, recorded)
- [ ] call-site audit: every grep in steps 1–5 run, results exactly as stated — in particular
      `grep -rn "webhooks_router" …` → 0, `grep -n "env is None" src/api/app.py` → 0,
      `grep -rn 'HTTPException(status_code=404' src …` → 2, `grep -rn '"web"' src/api` → 1,
      `grep -rn "_unmapped" src` → 1 def + 1 use,
      `grep -n "_open_tenant\|_member(\|_admin(\|_json_object" src/api/routes/v1.py` → 0,
      `grep -n "v1\._" src/api/routes/tokens.py src/api/routes/ops.py` → 0,
      `grep -rn "_open_tenant" tests` → 0
- [ ] targeted tests + full suite green
- [ ] `ruff check . && ruff format --check .`
- [ ] manual smoke: `uvicorn`-free — build the app in a REPL with
      `from src.api.app import create_app; app = create_app(env={})` and confirm it constructs
      with no engine; then, with the test client, `GET /api/v1/me` for a signed-in principal with
      no user row (expect `404 {"detail": "not found"}`), `GET /api/v1/workspaces/<unknown>`
      (expect the same), and a `TenantResolutionError("unknown_binding")` path (expect
      `500 {"detail": "internal error"}` from `_unmapped`). Read `/health` once and confirm the
      `pool` block still renders. No production API, no `storydump` write verb, no
      `python -m src.main`.
- [ ] `tests/mutations/cli_v2_01.sh` and `cli_v2_02.sh` re-run on the committed tree, every mutant
      killed, no NO TEST SELECTED / KILLED BY ERROR lines
- [ ] `CHANGELOG.md` entry under `## [Unreleased]` → `### Changed`
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

- **Do not fold in any flagged defect.** The master audit's "Questions" list three that sit in the
  files this PR edits, and each changes behaviour:
  - **C5** `/health.version` is the hand-copied `"0.2.0"` at `src/api/app.py:87` while the package
    is `1.6.0`. Step 1 edits `app.py` four times. **Do not point it at `src.__version__`** — that
    changes what `/health` and `storydump doctor` report and wants its own PR.
  - **C1** `storydump health` renders blank tap counts because `/health` nests them under
    `taps.taps`. The tap counters are read at `app.py:700`, four lines from step 1(d)'s edit. Do
    not reshape the block.
  - **B16** the `category_mix` v1 compat shim is reached through `v1.py:776,:799`; step 2 edits
    `v1.py:754` and `:1014`. Do not remove the shim.
  - **C8** (the settings-vs-`env` split at `webhooks.py:162-165` / `v1.py:93` vs
    `app.py:413-414,:447,:536`) is plan 09's optional step and needs a ruling. Step 1(c) resolves
    `env` **inside `create_app` only**; do not extend it into the routes, and do not make
    `create_app(env=…)` arm the Telegram door.
- **Do not narrow or widen a status or a body.** `_TENANT_DETAIL` is keyed by **status** and its
  body carries **no `reason`** — that is the no-existence-oracle rule, not an oversight. The
  `InvitationRefused` and `CommandRefused` handlers do **not** log; adding a log line to them is a
  behaviour change. The `_mapped` factory's defaults must not quietly normalise any of this.
- **Do not extract the two-site copies.** The callback unit-of-work construction at
  `auth.py:360-366` and `:448-454` is two. `_collection` (`v1.py:163`) and `_idempotency_key`
  (`:173`) have one caller each. `_token_args`, `_mix`, `_replayed` and `_conflict` share no table
  lookup. Leave all of them.
- **Do not `from src.api.principal import open_tenant`** — anywhere. The module attribute is what
  the conftest patches; a from-import binds the real function and turns a unit test into a live
  database call that will pass for the wrong reason.
- **Do not touch `src/services/target/ig_login_oauth._json_object`.** It matches the step 5 grep
  and is a completely unrelated private helper (an `httpx.Response` → dict, raising
  `IgOAuthRefused`).
- **Do not decompose `create_app`, `doctor` or `for_chat.send`.** TD-C11, C12 and C13 are plan 09.
  Step 1 touches five lines inside `create_app`; it does not move it.
- **Do not rename `src/services/target/health.py`** (TD-C20) — that is plan 12.
- **Do not run** `python -m src.main`, any `storydump` write verb (`approve`, `cancel`, `resolve`,
  `tokens revoke`, `webhook register|deregister`), or `python -m scripts.migration_runner apply`.
  Step 1 edits the webhook registration task's neighbourhood; nothing in this PR registers a
  webhook, and `create_app` must never be run against production's environment from a laptop.

## Related

- `00_TECH_DEBT.md` — the audit this plan is row 04 of.
- `01_one-spelling.md` — lands `TokenRefused.REASONS` (TD-C9) and the Telegram vocabulary
  constants; must merge first.
- `03_rule-of-three-services.md` — the same axis one layer down. `src/api/routes/v1.py:266`'s
  direct `INSERT INTO audit_events` is the documented API-side exception to 03's `audit.record`
  and is deliberately left alone by both plans.
- `09_composition-roots.md` — decomposes `create_app` (TD-C11), `doctor` (TD-C12) and
  `for_chat.send` (TD-C13), and inherits step 1(c)'s `env` binding.
- `.claude/rules/development-patterns.md` (routes parse → authorize → call a service → map a
  status), `.claude/rules/testing.md`, `.claude/rules/changelog.md`.
- Research evidence: `research/api-channels-cli.md` (TD-C10, TD-C15, TD-C17, TD-C18, TD-C19, and
  its "Notes for the plan writer" call-site lists).

## Origin

`/artemis-skills:audit tech debt`, whole repo, `main` at `0966771` (2026-09-20). Row 04 of the
remediation matrix in `00_TECH_DEBT.md`. Every `path:line` above was re-read against the working
tree while this plan was written; two parts of TD-C19 were withdrawn on that re-reading, and two
call sites the research did not list — `tests/mutations/cli_v2_01.sh:118,120` and
`cli_v2_02.sh:75`, which embed the `v1._open_tenant` call text verbatim — were found and are
carried in step 5.
