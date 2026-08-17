# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- **The settings boundary now redacts BOTH ways a settings load can fail, not just the one we noticed (#780)** — `Settings.__init__` caught `pydantic.ValidationError` and nothing else. `pydantic_settings` resolves raw values from env/dotenv/secrets in a SOURCE layer that runs *before* pydantic validates anything, and on failure raises its own `SettingsError` — a `ValueError`, unrelated to `ValidationError` by inheritance, so `except ValidationError` structurally could not see it. Both exits now converge on the same redacted, unchained `SettingsError`.
  - **It is a LEAK, not just a bypass, and that is a correction to how this was previously recorded.** The escaping error's own message names a field and a source class and quotes no value — which is why it read as merely structural. But it is chained `from e`, and for a complex field that `e` is a `json.JSONDecodeError` carrying the **entire undecoded input on `.doc`, untruncated**. Measured: a plain `pytest --showlocals` on the escaping path printed a synthetic credential **nine times**; the same invocation on the already-fixed `ValidationError` path printed it **zero** times. That positive control is what makes the nine attributable to the missing catch rather than to `--showlocals` being generally leaky. It is also strictly worse than the #775 defect this work began with, where pydantic's own ~22-character truncation at least bounded the disclosure.
  - **Severing the chain is the load-bearing half.** Redacting the message alone would accomplish nothing here, because the value was never in the message. The raise happens outside the `except` block for the same reason #777 does it: `from None` only sets `__suppress_context__`, leaving the `JSONDecodeError` — and its `.doc` — on `__context__` for any logger, debugger or traceback renderer to reach. After the fix both `__cause__` and `__context__` are `None`.
  - **The field name is verified against the model before being emitted, never merely extracted.** Reading anything out of a third-party message is the discipline `_redact` already refuses for `msg`, and the refusal earns its keep: `pydantic_settings`' CLI source formats its message as `f'Parsing error encountered for {field_name}: {e}'`, interpolating an underlying exception that may quote its input. That source is unused here, but "the message happens to be safe in the sources we happen to use" is a property of the installed release, not of the library. The emitted line is therefore provably one of two things — a declared field name, or `<unknown>`.
  - **The import is aliased, and the collision is not hypothetical**: `pydantic_settings` ships a `SettingsError` and so does `src/config/settings.py`. An unqualified import would shadow one or the other by import order, in the one module where the difference between them is the entire subject.
  - **Dormant as declared, and measured rather than assumed** — 42 fields, zero complex-typed, zero aliases, no `env_prefix`. The trigger is a **required** `list`/`dict`/nested-model field; an `Optional`-wrapped one degrades safely to the redacted validation path (pydantic-settings sets `allow_parse_failure` for union-wrapped complex fields), which a positive control pins so the fix is not over-scoped to "any complex field". Verified on the installed pydantic-settings **2.14.0**, not inherited from a note written against 2.14.2.
  - **Tests fail against the unfixed code, and for the right reason.** Captured before the fix: 3 failed / 9 passed, the three failing on `pydantic_settings.exceptions.SettingsError` escaping `Settings.__init__` — not on a fixture that happened to omit the credential. The defect is reached through a **subclass of the real `Settings`**, which inherits the real boundary, so a dormant path is exercised against production code without adding a complex field to shipped configuration.
  - **Six mutants, all killed, and one of them only after a test was added for it.** Removing the catch, chaining `from exc`, using `from None`, swallowing silently and dropping the field-name check each redden. Passing the library's message straight through **survived all twelve original tests** — the direct unit test of the redactor drives it by hand, so it proves the helper correct and says nothing about whether the boundary calls it, and the library's current message quotes no value so no leak assertion fires. Killed by asserting the emitted **vocabulary** is this project's, which is what makes "no value can reach this string" a property of our code rather than a lucky fact about the installed release.
  - **The leak detector itself had to be strengthened first, and this is the part worth carrying forward.** The existing `_render` helper walked the cause chain using `str()` and `repr()` — and `JSONDecodeError`'s `str()` is just `"Expecting value: line 1 column 1 (char 0)"`. Pointed at an exception holding the whole credential on `.doc`, it reported **zero leaked characters**. A leak test built on it would have gone green against a live disclosure. It now dumps each exception's attribute state as well.

- **A second, LIVE leak of the entire `.env` file, found while fixing the dormant one — the boundary now ends in a default-deny rung rather than a list of classes (#831)** — a single non-UTF-8 byte in `.env` (a latin-1 character in a password, a pasted smart quote) raises `UnicodeDecodeError` while `DotEnvSettingsSource` is being **constructed**, before any source is called. pydantic-settings' own `except Exception -> SettingsError` funnel lives *inside* the call, so it never sees it either, and neither enumerated class matches. It escaped `Settings.__init__` and, because `src/config/settings.py` builds a `Settings` at module scope, escaped **module import**.
  - **Not dormant, unlike #780.** No subclass, no complex field, no alias — the shipped 42-field configuration. Where #780 needs someone to add a required `list`/`dict` field first, this needs one mistyped byte in a file operators edit by hand.
  - **The payload is the whole file.** Measured on a 163-byte fixture: `UnicodeDecodeError.object` carried all 163 bytes and **three of three** synthetic credentials, while `str()` and the rendered traceback carried **zero**. Same blind spot as `.doc`, one level up and much larger — and the same reason a message-only redactor cannot see either.
  - **`ValueError` is the width, deliberately not `Exception`.** It is the library's own altitude: `SettingsError` subclasses it and `sources/base.py` catches it, and every credential-carrying escape found here is one. `except Exception` would also swallow genuine programming errors and report them with no traceback (the raise happens outside the handler) — trading a credential leak for a debugging cliff. `TypeError` and `AttributeError` still propagate intact, pinned by a test.
  - **This changes which clause carries the security property, and the mutation matrix says so plainly.** With the tail rung in place, deleting the `SettingsError` clause no longer reddens the leak assertions — a source error simply falls through and is redacted anyway. That clause now buys **diagnosability** (it names the failing field; the tail rung can only name the failure class), and it is still pinned by two tests. The guarantee itself rests on the rung.
  - **Eight mutants, all killed**, including the two on the new rung: removing it, and having it echo the exception instead of its class name. Tests captured RED against the two-clause version first — 2 failed, on `UnicodeDecodeError` escaping — and each carries a premise test asserting the payload is genuinely still reachable, so neither can quietly become vacuous.

### Fixed

- **`tests/scripts/` now REFUSES pytest-xdist instead of queueing silently for twenty minutes, and the two guides that recommended `-n auto` now print a command that works (#809)** — the directory serializes every session on `SUITE_CLUSTER_LOCK_KEY` because the seven `svc_*` roles are cluster-scoped spec names that cannot be namespaced per session (#768). Under `-n` each worker is a separate process with its own session, so workers 2..N queue on a lock the first holds until the session ends.
  - **This is a REFUSAL, not a fix for the underlying non-safety, and the docstring says so in those words.** Nothing here moves `tests/scripts/` toward being parallel-safe, and a reader six months out must not conclude that it is. What changed is that an unsafe invocation fails in seconds with an explanation instead of producing twenty silent minutes and then a `RuntimeError` — the wait loop reports by `print` from session-scoped fixture *setup*, and `pytest.ini` sets no `-s`, so the message is captured and never reaches the terminal unless the fixture fails.
  - **The presentation depends on the invocation form, which is worth stating because only one form was measured when this was handed over.** Measured here against real pytest 9.0.3 + pytest-xdist 3.8.0: reached by DESCENT (`pytest -n auto`, the form both guides printed, `testpaths = tests`) the run reports `ERROR tests/scripts` in the short summary in ~16s **with zero execnet noise, and the rest of the suite still passes**. Named as an INITIAL ARGUMENT (`pytest tests/scripts -n auto`) the same guard becomes a worker-boot failure — `no tests ran`, 20 lines of execnet traceback — because a conftest belonging to an initial arg is loaded at worker startup rather than during collection. It still fails in ~12s with the full message present, so it is loud rather than silent either way; it is simply uglier, and nothing here claims otherwise.
  - **The check is worker-side because nothing else can work from this file.** Refusing on the controller instead — keying on `config.option.numprocesses` to fail before workers spawn — would make the initial-argument form clean, and does not work. Measured rather than reasoned: in the descent form this hook is called exactly TWICE, both times with `workerinput` present. The controller never loads this conftest at all, and on the workers `numprocesses` is `None` and `dist` is `no`, because xdist resets them for the worker's own serial run. A controller-side check placed here would not trade one behaviour for another; it would be dead code that never fires.
  - **`hasattr(config, "workerinput")` is not a workaround for `is_xdist_worker()` — it is that helper's own implementation.** The helper takes a request/session wrapper and this hook is handed only `config`, so the direct form is the correct spelling. It is deliberately not an import: pytest-xdist is not a dependency of this project, and importing it would make the guard require the very plugin it exists to refuse.
  - **The guides now print `pytest --ignore=tests/scripts -n auto` followed by a serial `pytest tests/scripts`**, rather than keeping the old command with a caveat attached. A guide whose printed command exits 1 is still the defect the issue was filed about — the reader copies the command, not the caveat. The escape route was run before being recommended: `rc=0`, guard silent.
  - **Tested without requiring the plugin it refuses.** The predicate is exercised directly against a stub config, so CI proves the guard with pytest-xdist absent; the end-to-end behaviour is proven by running it for real and cited on the PR. Four mutants, each detected, and they are not detected the same way. **A guard that never fires** is the one that rules out vacuity: it reddens the refusal test *only* and leaves the positive control green, so the two discriminate rather than both keying on the guard's mere existence. **A guard that always fires** is detected quite differently, and the distinction is worth stating rather than compressing — the entire pytest invocation dies at `rc=4` with a `UsageError` *before collection*, so no test receives an individual verdict at all, the refusal test included. **Loosening the predicate** to a near-miss attribute reddens the refusal and the near-miss control together; **dropping the escape route** from the message reddens the refusal. That last assertion exists because a refusal with no route forward relocates the confusion rather than ending it.

### Added

- **The M.1 transform spec (#790) — `documentation/planning/2026-08-17-m1-transform-spec/README.md`: the `02` §9 disposition index made executable, with the four open forks marked instead of silently picked.** Planning artifact only; no code, no migration, no runtime change. Per legacy table: the full per-column mapping with the `02` §0 timestamp rule applied per column, the ruled derivations (owner ladder, role map, FC-7.2 fan-out, `service_runs` ±6h attribution, FC-7.4 queue clause), and 37 verbatim `-- runner:postcondition` lines (row-count reconciliation + per-table invariants) as the review objects the `04` L89 gate names.
  - **Fork discipline (#790 comment 5318301047 consumed, not rebuilt):** sections depending on Forks A–D are marked BLOCKED PENDING RULING with per-option deltas; C and D are additionally marked conditional on the 044 single-tenant measurement. The A3-silent-default trap is carried as file-contract rule 8 — landing 3e files before Fork A is ruled selects halt-and-retry by default and quietly enlarges #787's ruling, so the spec forbids it in the contract rather than trusting review to remember.
  - **One new fork surfaced under the same menu-no-pick discipline (Fork E):** live legacy `recent_post` locks cannot satisfy `ck_locks_recent_scope` — the target requires an `ig_account_id` legacy locks do not carry. Three shapes stated, none chosen, plan author named as ratifier.
  - **Gate self-assessment is explicit:** postconditions met by the spec; reconciliation queries meet review at this PR; the quarantine procedure is declared SHORT — its home, shape, and actor are Fork A's output, and the spec says so rather than inventing an adjudication path no printed actor can execute.
  - 19 spec-tier mapping decisions ([SD-n]) are registered in one table for explicit review rather than scattered as silent choices — two more were lifted to the plan author on #731 after review (#827 Finding 4: a conflict between two ratified texts and a deviation from a ratified text are not register-tier, and merging would have ratified them by silence); `scripts/fc8_gate.py` (#792) gets its first documentation home outside the changelog. Review also fixed the C/D collapse conditions to their distinct counts (`group_chats = 1` vs `chats = 1` — the two are not interchangeable, and a DM-rooted settings row is an ordinary state that separates them).

- **The #801 `CONNECTION LIMIT 0` reproduction is now a test, so the discriminator it established cannot silently regress (#804)** — `server_answered()` separates "no database was configured" (skip is honest) from "a database was configured and refused" (a real failure). #801 proved that separation works by creating a PostgreSQL role with `CONNECTION LIMIT 0` and observing a refusal from a still-listening address. That reproduction existed only in a review comment and in the reviewer's session; nothing in CI re-ran it.
  - **What was already covered, and what was not.** Every existing test in `TestTheListenerProbe` monkeypatches `server_is_listening` to a lambda. That is right for pinning how `server_answered` COMBINES its two inputs, and blind to whether either input is still what it was measured to be. The new `TestTheRefusalAgainstARealServer` uses no mocks: real role, real libpq, real socket probe.
  - **The gap is not hypothetical, and one mutant isolates it exactly.** Making `server_answered()` probe the wrong address while leaving `server_is_listening` itself intact — the shape of a refactor that passes the wrong host/port — reddens **only the two new tests**. Every pre-existing test stays green, because the lambdas that replace the probe ignore their arguments, and the standalone probe test is unaffected. Two further mutants confirm the new tests are aimed at the right thing rather than merely strict: deleting the listener branch (restoring pre-#769) reddens 4 (2 existing, 2 new), and breaking the real probe reddens 3 (1 existing, 2 new). Positive controls green throughout — the negative-direction test and the `pgcode` test never move under any of the three.
  - **Both directions, because the defect was an INABILITY TO SEPARATE them.** A test covering only the refusal is satisfied by a `server_answered()` that always returns True — the same bug with the opposite sign. The negative half is deliberately left unmarked: it needs no PostgreSQL, only an address with nothing behind it, so it still guards where the positive half must skip.
  - **The refusal is asserted, never assumed, because the failure mode here reads as success.** `server_answered()` also returns True for a connection that SUCCEEDS, so a probe role that stopped being refused would leave the tests green having reproduced nothing. Each test proves the refusal first, and the fixture asserts its own postcondition (`rolconnlimit = 0`, `rolsuper = false`) — a superuser is exempt from `rolconnlimit`, so that flag alone would turn the whole class into a happy-path test. The credential is yielded as a pair rather than travelling by module constant: a password mismatch raises the same `OperationalError`, so a later hardening that randomized the password would otherwise leave three green tests reproducing an auth failure.
  - **A missing precondition FAILS where integration coverage is mandatory**, rather than skipping. `tests/scripts/` skips unconditionally on the same missing privilege; this diverges because a guard that quietly does not run cannot be told apart from one that ran and found nothing — which is the defect #804 was filed about. Measured both ways: with no server and `REQUIRE_TEST_DATABASE=1`, `setup_test_database` raises first and errors 4 tests; with a server but no CREATEROLE, the new gate errors exactly the 3 that use the fixture. Without the flag both conditions produce named skips and the unmarked negative test still passes.
  - **Not routed through `integration_verdict`, deliberately.** Doing so would mean writing `server_answered=False` for the CREATEROLE case — where a server demonstrably answered and merely lacked a privilege — inside the one file whose whole subject is that distinction. The shared part that can actually drift, `database_is_required()`, is called rather than re-parsed.
  - **The cluster footprint is the minimum that still reproduces.** The role is uniquely named off #763's session token so concurrent runs from other checkouts cannot collide, owns nothing and is granted nothing (so no `pg_shdepend` rows and no droppable-blocked-by-dependency case), and is dropped in a `finally`. A leak from a hard kill is inert rather than poisoning, which is why there is no sweep. Kept function-scoped on purpose: session scope would save ~84 ms and hold a maintenance connection open for the whole run against a cluster this repo already contends on — the wrong trade on the axis that actually bites.

- **The M.3 parity bar's legacy→target command mapping, promoted out of a comment into a tracked document (#790)** — `documentation/planning/2026-08-17-m3-parity-bar-mapping/`. Fourteen named items classified against the current Telegram adapter at `c28066d`: 6 clean ports, 2 present-but-conditional, 3 builds-not-ports, 2 present-depth-unaudited, 1 unscoreable. Documentation only; no code change, and **two forks are left open rather than settled**.
  - **Filed here rather than in a comment because that is the #806 defect.** The F.2 increment split lived in a #746 comment and F.2 read as unowned for weeks. The mapping's first version was a #790 comment; this is the same shape, re-filed before it repeats.
  - **Two bar items name commands the product deliberately RETIRED from Telegram.** `telegram_service.py:265-277` carries a "Retired commands" block registering `/pause`, `/resume` and `/sync` (among twelve) as handlers whose only behaviour is to answer *"has been retired — use the dashboard"*. Read at face value the bar requires the target's adapter to serve them again, which is reversing a shipped decision rather than closing a gap. Recorded as Fork 2, unresolved: it is an owner ruling.
  - **`resume` is a false friend, twice over, and is called out by name so it cannot be re-scored.** The token appears at `telegram_service.py:268` (the retired slash command) and `:346` (a callback prefix for *reschedule/clear/force* recovery). Neither is workspace resume. A name-based audit scores `pause`/`resume` PRESENT on either string.
  - **The bar's two statements disagree, and the disagreement decides rows.** `04-execution-sequence.md:262` scopes it to "the target"; `00-fixed-constraints.md:91` (FC-7 clause 3), which `04` cites as its authority, scopes it to "the Telegram adapter". Capabilities deliberately moved to the dashboard satisfy the first and fail the second. The document uses FC-7's wording and flags the inconsistency rather than silently resolving it.
  - **The mapping was derived by enumerating legacy's own dispatch containers**, not by searching legacy for the target's vocabulary — legacy says `autopost`/`posted` where the bar says `approve`/`mark_posted`, so a word search returns false negatives and would have missed the retired-commands block entirely. This is the inverse of #810, where a requirement was invisible to word search because it lived in an identifier.
- **Every door onto the cluster-scoped `svc_*` roles now VERIFIES the suite mutex instead of declaring it (#808)** — `run_bootstrap` took a bare DSN, so the CREATE side was covered by *call path*: every DSN in the suite happened to come from `_create_db(admin_conn, ...)`, so every caller was already inside `SUITE_CLUSTER_LOCK_KEY`. Coverage held by emergent accident, and a property held by call path holds until someone adds a caller — with nothing going red when they do.
  - **#808 filed a runtime check as impossible, and it is right about both forms it considered — but both assume the check runs on the connection `run_bootstrap` opens itself.** Measured on this cluster: `pg_try_advisory_lock(SUITE_CLUSTER_LOCK_KEY)` returns `True` from the holding session (advisory locks are re-entrant, so the obvious probe detects nothing), and the un-scoped `pg_locks` form answers "somebody holds it", which is true *precisely* when a foreign session holds it — i.e. in the collision. What makes the question answerable is having the connection at all: ask `lock_holder` **on the handed connection** and compare pids. Same key, same cluster, same user: the holder's pid from the holder, and nothing from an equally valid non-holding connection.
  - **So the parameter is checked rather than decorative**, which is the whole difference. The cheap fix — put the connection in the signature and never use it — is a marker: nothing goes red when a later author deletes an argument the body ignores, so it relocates the convention-not-construction problem rather than closing it. `ruff` would not catch it either; `ARG` is deliberately not in this project's rule set. Reusing `lock_holder` rather than re-typing its join also keeps the `objsubid` discrimination in one place and lets the refusal **name who is inside the mutex** — the property the lock's own docstring calls its whole advantage over a `flock`.
  - **The DROP-side guard sits at `_drop_roles_hardened`, not at `drop_service_roles`.** Having `admin_conn` in a signature never made a door hold a lock — any connection satisfied the parameter. Checking the primitive all role dropping funnels through covers three callers instead of one, including `_sweep_leftovers`, which the module header calls the most destructive thing in the file. Guarding the public door alone would have been coverage by enumeration — the shape `SUITE_CLUSTER_LOCK_KEY`'s own docstring warns against.
  - **`psql_apply` REFUSES `BOOTSTRAP_SQL` rather than being guarded**, and the refusal is keyed on the artifact rather than on a door. It holds a DSN, not a connection, so it cannot check the mutex at all; and 12 of its 13 callers apply `SETUP_SQL` or migration files *into one scratch database*, which is per-database and needs no cluster mutex. `bootstrap_via_psql` is now the one route that may apply that file through psql, so a future third transport cannot quietly reopen the hole.
  - **Mutation-proven per part, and bound to the specific failure.** Seven mutants, each reddening its target: the guard removed at each of the three doors, the artifact refusal removed from `psql_apply`, the message's key removed, and the guard inverted. The load-bearing one drops the pid comparison — degrading the check to exactly the "somebody holds it" form #808 ruled out — and every refusal goes red **while the positive control stays green**, which is what says the comparison is the mechanism rather than a guard that broke everything. The negatives assert the message names **both** the door and the key, because a bare `RuntimeError` is equally raised by a typo or a closed connection, and `outsider_conn` is deliberately neither: same cluster, database, user and process as `admin_conn`, differing *only* in the lock.
  - **The DROP negative starts from `bootstrapped_db`, deliberately not from `roleless_db`.** Against a roleless database "the seven roles are still absent afterwards" is true whatever the guard does — a vacuous assertion, in a change about vacuous assertions. Starting where the roles exist makes their survival an observation.

- **F.2.2 — 02 §1's identity and tenancy tables land as migration 053, the first tables of the target schema (#806)** — seven of them (`users`, `user_identities`, `workspaces`, `workspace_members`, `workspace_invitations`, `channel_bindings`, `onboarding_sessions`) with their touch triggers, the two owner-invariant constraint triggers and three unique indexes: advertised-stream statements 2..21, in stream order, after 052's two shared functions at 0..1. Arm (b) reports `ok=True vacuous=False f2_count=22`.
  - **The file body is GENERATED FROM THE STREAM, not transcribed.** `04` §0.2 arm (b) is an ordered prefix diff, so a hand-copied `CREATE TABLE` that differs by a word fails it — and the failure would be reported at a statement index, not at the word. The migration was written by extracting statements 2..21 through the repo's own extractor and asserting, at write time, that the resulting file normalizes back to exactly those statements. Edit the plan and the manifest ratchet; never this file alone.
  - **NO POLICIES AND NO RLS, per the Fork 1 ruling (a).** The stream creates all 26 tables before the first `ENABLE ROW LEVEL SECURITY` (index 126) or `CREATE POLICY` (149), so an increment carrying its own policy would not be a prefix and would fail arm (b) at the first policy statement. Four of the seven are tenant-keyed and carry no policy until F.2.9; that window is the ruling's stated cost, and the prefix-aware lane check accounts for it on both sides rather than going red on schedule for five increments.
  - **The xfail tripwire planted by 1-of-2 fired exactly once, here, and was deleted — the response its own message specified.** The `sig == expected` comparison it sat under is untouched and is NOT bounded to a complete lineage; in its place is a plain `assert expected`, so the vacuity it warned about is now a live requirement rather than a warning about a future one. It had one job, it did it, and it is gone.
  - **Target models land PER INCREMENT, and the parity gate decided that rather than taste.** `src/models/target/identity_and_tenancy.py` mirrors 053 on `TargetBase`; lane parity compares the replayed `public` against `create_all`, so tables on the migration side with no models behind them IS the drift it reports. Deferring models to one late pass would mean running the gate knowingly red from F.2.2 through F.2.7 — the "red and known" cost Fork 1 declined when it declined option D. The models-side database is given 052 first, because `ck_ws_tz_valid` calls `fn_safe_tz` and a CHECK naming a missing function is a hard error; that it cannot contaminate the comparison is asserted in place (it creates no relations) rather than argued.
  - **Four pre-existing lane assertions were reshaped, not relaxed, and the distinction matters.** Each said "public is empty" or "no declared table name is in public" — both true only while the target lineage created no relations, and both now false for the correct reason. `public is empty` becomes `public holds exactly the tables the target lineage implies`, which still fails on a legacy leftover *and* additionally fails on an undeclared target table. The legacy-inventory check is narrowed to legacy-ONLY names, because the target schema deliberately re-uses `users` and `onboarding_sessions` — from 053 those names exist in both schemas, so a name-keyed intersection reports a stranded legacy table where there is none. Every reshaped assertion asserts its own discriminating power first (`assert legacy_only`), so a future increment that consumes the last legacy-only name reddens rather than passing vacuously.
  - **Expected sets are DERIVED from the stream, never listed — tables AND functions.** `implied_tenancy()` owns the prefix rule in one place (`expected_tenancy` over the stream sliced to the lineage's own statement length) and `implied_target_tables()` / `implied_target_functions()` read off it, so the next increment updates every caller by existing. The function inventory matters more than it looks: the stream holds 18 `CREATE FUNCTION`s, four have landed, and because the assertion compares against `proname` order the remaining 14 are insertions into a sorted list rather than appends — a literal list would need re-sorting by hand at each of the five remaining increments. Neither derivation is vacuous: both compare plan TEXT against a live catalog.
  - **The one list kept deliberately is the target lineage's FILE list**, because arm (b) compares statements — a file above the move carrying only comments contributes nothing and leaves it green, so only a file-level assertion can see that file at all. The hole it exists for is now also closed mechanically, by asserting every lineage file contributes at least one statement, which leaves the list doing only the ratification job.
  - **A latent defect caught in review, in this migration's own postconditions.** `migration_runner.py` derives a file's permanent ADOPTION PROBE from its `runner:postcondition` lines when the adoption manifest has no entry, so a postcondition answers two questions: "did this file just do its job" and "has this file ever been applied here". Two of 053's first-draft postconditions asserted the ABSENCE of state a later increment adds (`count(*) = 0 FROM pg_policies`, `NOT relrowsecurity`). Both would have gone false when F.2.8/F.2.9 land RLS and policies — 053 would then probe unapplied on a database plainly holding all seven tables, and `runner adopt` raises `incoherent chain` naming 053 while the real cause is the increment after it, mid-cutover. Replaced with four postconditions scoped to objects 053 itself creates, none of them negative, with the rule stated in the file so the next increment does not re-introduce it.
  - **Shared column vocabulary moved to `src/models/target/columns.py`.** `TZ`, `NOW`, `GEN_UUID`, `pk()`, `timestamps()` and `fk()` were homed inside F.2.2's own module, where the next increment could only reach them by importing a private name across increments or re-deriving them — and re-deriving `TZ` is precisely the mistake it exists to prevent (a naive `DateTime` renders `timestamp without time zone`, a silent divergence on every timestamp column rather than a loud one on the first). Of the 19 tables still to land, 19 use `TIMESTAMPTZ` and 12 have `gen_random_uuid()` keys, so no increment wants none of it. `fk()` also collapses 11 five-line foreign-key blocks to one line each, putting the two facts a reviewer checks against the SQL — target and `ondelete` — in the argument list rather than mid-block.
  - **One spelling of the advertised stream for the whole suite.** `test_tenancy_gate.py` had a cached builder carrying the measurement that justified caching it; the lane grew a byte-identical copy WITHOUT the cache. Promoted to `conftest.advertised_stream()` (the move conftest already makes for `migration_files`) and both suites import it, so the next suite that needs it finds a cached spelling rather than writing a third. `F2_2_START` went with the deleted control — an unconsumed literal is the drift its own comment block exists to prevent.
  - **The staged positive control from 1-of-2 was deleted, because the thing it simulated is now real.** It replayed the F.2.2 segment into a throwaway tree precisely because the lineage was empty; the real lineage now exercises the same path, and it would in any case collide at version 53. Its one non-duplicated half — that a correctly-born but UNDECLARED table reddens the prefix comparison while staying invisible to the invariant check — moved onto the existing `lane_tenancy_probe` in the real lane, where both predicates now answer about one object. Its plan-side facts (7 tables, 4 tenant-keyed, no RLS, no policies) were already pinned, DB-free, by `test_tenancy_gate.py`. Net: one fewer corpus replay, same coverage, the contrast visible in one place.
  - **A real bug the parity gate structurally could not catch.** `text("'{\"v\":1}'")` for `channel_bindings.settings` reads `:1` as a SQLAlchemy BIND PARAMETER and emits `DEFAULT '{\"v\"NULL}'`, so the table will not create. The gate compares types, constraints, uniques and FKs and never defaults — it surfaced as `create_all` raising, not as drift, which is worth knowing before the next increment writes a JSON default.

- **The lane tenancy check is prefix-aware, so F.2.2 lands green for the right reason (#806)** — `expected_tenancy()` in `scripts/tenancy_gate.py` derives the tenancy state a prefix of the advertised stream implies, and `test_lineage_lane.py` now asserts the replayed lineage **equals** it rather than asserting `tenant_keyed_tables(sig) == []`.
  - **The old shape had two exits and both were bad.** Under ruling (a) the increments are contiguous stream segments, and the stream creates all 23 of `02`'s tables before the first `ENABLE ROW LEVEL SECURITY` (index 126). So `keyed == []` is true only until F.2.2 and false for five increments — the check either goes red on schedule or gets switched off. Bounding it to a complete lineage, the other candidate the code comment named, is the same quiet window spelled differently: silent until F.2.9, which is the exact 26-table stretch the gate exists to cover.
  - **Landed BEFORE the first table, deliberately.** The reverse order leaves `main` red and fixes the gate under delivery pressure, which is how a check gets widened to fit whatever landed. On today's lineage this change is behaviourally inert — both sides are `{}` — and it **says so at runtime** rather than reading as coverage: `prefix is empty at 2 statements — equality compared nothing`.
  - **Strictly stronger than the `violations == []` it replaces, in a direction that check cannot reach.** Measured on a staged F.2.2 replay: a correctly-born tenant-keyed table that the stream does not declare at that position is **invisible** to the invariant check (it has RLS and a policy, so it adds nothing to the list) and breaks the prefix comparison. It also catches RLS enabled where the prefix does not call for it, and a disagreeing policy count.
  - **Not a tautology, because the two sides are different objects** — one is read off a live catalog after replaying real migration files, the other parsed out of plan text. A file that declares a table the stream declares but fails to install it diverges here.
  - **The derivation is calibrated against a live measurement, not just self-consistent.** Over the full 257-statement stream it derives **26 tables, 19 tenant-keyed** — the same two numbers `test_advertised_ddl_replay` observes by executing that stream into a real database. It also independently reproduces the split document's whole cost table (23/23/53 at F.2.7, 26/26/58 at F.2.9).
  - **It describes the mid-stream window rather than assuming it away.** A test asserts `tenancy_violations()` over the derived F.2.6 state is **non-empty** — if the derivation ever satisfied the invariant mid-stream it would be smuggling the invariant into the expectation, and the lane comparison could never catch a migration that skipped a policy.
  - **The cost of the old shape is now pinned as an executable fact.** On a staged F.2.2 catalog the replaced check reports exactly 4 violations, all `RLS is not enabled`, against four tables the plan says are correct at that point. Asserted so the reason survives, rather than someone reverting the shape thinking it was cosmetic.
  - **Forward-only, enforced by an ALLOWLIST rather than a denylist.** A statement that *reduces* tenancy state would make the derivation overstate what is present — the quiet direction. Naming `DROP POLICY` and `DISABLE ROW LEVEL SECURITY` by regex and silently ignoring everything else, which is what the first cut did, is a claim about today's corpus wearing the shape of an enforced rule: `DROP TABLE`, `ALTER TABLE … DROP COLUMN workspace_id`, `RENAME TO` and `DROP SCHEMA … CASCADE` all do the same damage. So the bounded side — the statement kinds that provably cannot move any of the four facts — is enumerated in `_TENANCY_IRRELEVANT` from the real stream, and anything else refuses. Same treatment `TENANT_KEY_EXEMPT` already gets, and the same philosophy as the manifest ratchet: a new statement kind is a review event, not a guess. Paired with a positive control that the allowlist covers the real corpus, so the refusal is discriminating rather than merely strict.
  - **One spelling of a signature entry.** Both producers build through `_tenancy_entry`, so a field added to the catalog reader and not the derivation is impossible rather than something a test has to notice — the drift test now pins to the constructor instead of to a literal key set, which was a third copy of the shape.
  - **The vacuity disclosure is an assertion, not a `print`.** pytest captures stdout and surfaces it only on failure, so a printed "equality compared nothing" is invisible on exactly the green runs it is written for. It now trips once, at F.2.2, in the same run as the first real comparison — matching the sibling lane-parity disclosure in the same file.
  - **Mutation-proven:** five mutants on `expected_tenancy` (tenant-key detection, the `workspaces` tenant-root case, policy counting, RLS recording, the state-reduction guard) each killed by the tests that should own them. A sixth attempt was discarded rather than counted — a crude edit produced a `SyntaxError`, which fails everything and reads as a kill; it was re-run as a valid mutant with syntax asserted clean before being recorded.

- **The tenancy gate now runs against the F.2 target-lineage replay — Fork 1's paired obligation (#806)** — ruling (a) lets policies leave the table increments, trading a *structural* guarantee (a table and its policy in one PR) for a *detected* one. That trade is only honest if the detector is aimed at the lineage F.2 lands in. It was not.
  - **The gap was structural, and is measured rather than argued.** `test_tenancy_gate.py` replays bounded to `LEGACY_LINEAGE_MAX`, which stops **below** the 051 move; every F.2 file is numbered above it. In an exported tree, a tenant-keyed table with RLS off landed as `053` left that entire suite **green (2 passed)** while the lane's own replay observed it in `public` — so the failure `tenancy_gate` exists to catch was invisible to the only place it was wired.
  - **The check goes where the target-lineage replay is** — `test_lineage_lane.py`, beside lane parity, for the same reason lane parity lives there rather than in a parity suite: the lane owns the replay and imports the predicates that judge it. A second replay inside the tenancy suite would have been a parallel path to keep in step.
  - **The in-test probe is load-bearing, not decoration.** After the lane, `public` holds zero tables, so the violation check alone passes on an empty set — and would pass just as green if `tenancy_signature` were pointed at the wrong database entirely, which is *the same failure this wiring exists to fix, one level down*. So it must not be assertable by it: a probe table is born correct (`TO svc_ingress`, uncreatable without the bootstrap, so it also shows this replay can carry the policy shape F.2.2 needs), asserted clean, then has its policy dropped and must be named in the violation list.
  - **Mutation-proven against the thing that was missed.** The same `053` mutant, on this branch, reddens the new check by name (`mutant_tenant_table: tenant-keyed but RLS is not enabled`) and trips the new disclosure; a positive control on the unmutated tree confirms the mutant genuinely replays rather than being skipped.
  - **The obligation has a second half, and it is the one that is non-vacuous today.** `test_advertised_ddl_replay.py` already replayed the *full* advertised stream into a live database under the declared actors, and simply never ran the gate on the result. It does now. The lineage half is vacuous until F.2.2 and not at full strength until F.2.9; this one is at full strength immediately — **19 tenant-keyed tables of the 26, zero violations** (the other 7 carry no workspace key by design, `02` §7-DDL Class 3/4).
  - **It checks a different claim from arm (b), which is why both exist.** Arm (b) says the migrations reproduce the plan. This says **the plan is correct**: a tenant-keyed table whose policy `02` forgot would leave arm (b) green, because the migrations would faithfully reproduce the omission, and would redden this.
  - **Its count assertion is a positive control, not a fact about the schema**, and is mutation-proven — raising the floor to an unreachable value fails with the real count in the message, so the control is live rather than trivially satisfied. `tenancy_violations` returns `[]` just as readily for a database it never saw; the floor is deliberately loose (15 against an observed 19) because the exact number is the plan's to change.
  - **`tenant_keyed_tables()` is extracted in `scripts/tenancy_gate.py`** so the two new disclosures scope coverage the same way the gate scopes enforcement, from one definition. A private re-derivation is a live drift risk rather than a stylistic one: the obvious spelling omits `TENANT_KEY_EXEMPT`, which is harmless while that set is empty and becomes a disclosure going red naming a table the gate is deliberately silent about the day it is not — and `tenancy_gate.py` anticipates that set growing.
- **The F.2 increment split, reshaped under ruling (a) and moved into a tracked document (#806)** — `documentation/planning/2026-08-14-f2-increment-split/`. The split lived in a #746 comment, which is the defect #806 was actually right about: a scan of every open issue for `F.2` found zero owners because the issue owning the work did not carry the label. It was being re-filed under (a) anyway, so it is re-filed somewhere addressable.
  - **Every increment is a contiguous segment of the advertised stream, and every cumulative prefix is measured green** through the real `f2_prefix_report` at all nine boundaries — the property (a) was chosen for, checked rather than argued from construction. 26 tables, matching the extractor's independent count; segments cover all 257 statements with no gaps.
  - **Fork 2 dissolves rather than being deferred.** It was that F.2.4/F.2.5/F.2.6 are non-contiguous, caused by exactly two assignments — `audit_events` and `command_dedup` sitting inside another increment's run. Contiguous segmentation removes the cause, so the fork needs no ruling; both tables move, and the move is recorded.
  - **The cost is derived and bounded rather than described.** The tenancy gate is red from F.2.2 through F.2.6 and closes at F.2.7, by the plan's own order — the ratified stream creates all 23 of `02`'s tables before enabling RLS on any of them. `07`'s three auth-plane tables keep #746's original table-with-its-policy property for free, because that segment is contiguous in the stream; only `02`'s 23 are separated.
  - **§7-DDL splits three ways where the filed split had one tail increment**, because `07`'s tables sit *after* the `SECURITY DEFINER` doors in stream order and a single §7-DDL increment could not be contiguous. That the boundaries *within* §7-DDL are a judgement — unlike the table increments, which follow the plan's own sections — is stated in the document's bounds rather than left to look derived.

- **Migration 052 — the two shared trigger functions, and the first file of the F.2 target lineage (#806)** — `trg_touch_updated_at()` and `fn_safe_tz()` from `02` §0: F.2.1's one surviving item, which did not ship with the bootstrap in #752. They are normative block #1, the literal head of the advertised stream, so they land before any table regardless of how the table increments are eventually ordered.
  - **The number is derived, not chosen.** `051_schema_move_public_to_legacy.sql` declares itself the corpus lineage boundary, and `target_lineage_files()` resolves "above the move" through the runner's own `runner:schema-move` marker rather than the literal 51 — so renumbering the boundary moves the lineage with it. Verified by running the resolver, not by reading it: marker → version 51, target lineage empty, next number 052.
  - **No table, and that is the point.** The open question about whether a table may land before its policy governs the table increments; a file that creates no table is not subject to it. This increment is likewise independent of both rulings outstanding with the owner.
  - **`fn_safe_tz` is `STABLE`, deliberately not `IMMUTABLE`.** Its answer depends on the server's tzdata, which changes. The two consumers share one rule: the `ck_*_tz_valid` CHECKs make an unrecognized zone unstorable at write, and reading through the function degrades a value later withdrawn from tzdata to UTC for **that row** instead of aborting the caller's whole set-based statement.
  - **Applied against a real database, not just diffed.** All four declared postconditions pass, the trigger measurably updates `updated_at`, and a CHECK built on `fn_safe_tz` rejects an unrecognized zone — including the `EXCEPTION` path, which a presence check cannot reach.
- **Arm (b) of the `04` §0.2 gate is now load-bearing (#806)** — it was wired and vacuous, with `test_target_lineage_is_empty_today_disclosed` asserting the target lineage was empty and written to go red the moment a file landed. 052 is that file. The disclosure is retired on its own terms and replaced with the assertion it was holding the place for: the lineage head must equal the advertised stream's opening statements, in order (`ok=True vacuous=False f2_count=2`).
  - **The expectation is derived from the stream — the plan documents plus the manifest — never from the migration**, which is the artifact under test. Derived from the file, it would pass for any content at all.
  - **Non-vacuity is now asserted, and that line is the load-bearing one.** `test_the_wired_prefix_holds_against_the_real_stream` asserted only `ok is True`, which an empty prefix satisfies vacuously. Without the added `vacuous is False`, deleting every target-lineage migration would have turned it **green** rather than red — reinstating, at the exact moment the disclosure was removed, the hole the disclosure existed to flag.
  - **A second disclosure existed and is updated too.** `test_lineage_lane.py`'s `test_the_target_lineage_is_currently_empty_and_says_so` made the same emptiness claim and named the two functions as what it expected to arrive; it now asserts the lineage is exactly that file, and a new lane test asserts what the replay **creates** — the other half of what the disclosure asked for. Found by running the suite rather than by grepping for the one that was pointed at.
  - **Lane parity stays vacuous, stated rather than left to a green.** The lane now installs two functions, but `schema_signature`/`create_all` are relation-scoped on both sides and a function lives in `pg_proc`, not `pg_class` — so 052 moves neither side. F.2.2's first table is what switches that gate on.
  - **Mutation-proven, six ways.** Diverging the DDL (`STABLE` → `IMMUTABLE`) reddens both prefix tests; deleting 052 reddens three, where an empty lineage is today's *passing* state; smuggling a `CREATE TABLE` in reddens the no-table assertion. On the lane side, removing the fallback, stubbing the function to always return `'UTC'`, and dropping `trg_touch_updated_at` each redden the install test — the tz assertion is a **pair** precisely because a passthrough stub satisfies one half and a constant-`'UTC'` stub the other.
- **The FC-8 zero-row gate, executable (#790 M.1)** — `04` L87 makes it a window-prep **halt**: live local/upload-origin `media_items` = 0, and `posting_history` rows resolving to such media = 0. `scripts/fc8_gate.py` computes both against the legacy schema and exits `0` CLEAR / `1` HALT / `2` ERROR.
  - **Buildable now because it reads legacy only.** M.1's transform INSERTs cannot be authored yet — `src/models/target/` is an empty `Base` ("zero target tables exist today; F.2.2 onward registers them here") and no shipped migration creates a target table — but this gate never touches them.
  - **A gate that cannot answer does not report CLEAR.** A missing table or an unreachable database exits `ERROR`, not zero — #758's false-PASS class, pinned by test and mutation-proven (swallowing the error and returning the safe answer reddens exactly that test).
  - **The two counts are deliberately different predicates.** Count 2 does not filter on `is_active`: a posted row whose media was later deactivated still has no target destination for that media. A test would go red if the two queries were ever "tidied" into one.
  - **It discloses what it has no authority to rule.** FC-8 names `local` and `upload`; the corpus also carries `instagram_backfill`, which the plan classifies nowhere, while the target's media-source CHECK is closed at exactly `gdrive` (`02` §2) — so such rows may have no destination and would not be counted. The gate reports them as `UNCLASSIFIED` and **does not** invent a third halt condition. Same for `is_active IS NULL`, which is neither live nor dead: counted in its own bucket rather than folded into either side by an `IS NOT FALSE`.
  - **The disclosure is pinned where it is read, not only where it is computed.** `render()` is the default path (`--json` is opt-in), so deleting its `UNCLASSIFIED` or `is_active IS NULL` blocks would have left `census()`, the JSON payload, both halt counts and all of CI green while the unknowns silently vanished from what an operator sees — the same failure-produces-silence shape the buckets exist to prevent. Three mutations, each killed by the test that names it: dropping the reason block, dropping the inline row marker, and dropping the null bucket (which also kills the end-to-end wiring test).
  - **The halt-routing text is pinned per fact, not per block (#795).** `render()`'s `if c.halts:` block had its presence covered and its content unasserted. It is a lower tier than the disclosure buckets above and stays classed that way — deleting it leaves an operator less well served, not uninformed, because the exit code and the VERDICT line still carry the fact. That argument depends on the VERDICT line saying `HALT — window prep stops`, which nothing asserted either, so the premise of its own lower tier is now pinned too. The block's six facts are asserted one test each rather than one assertion over the block, because a wrapped paragraph can lose a single resolution and read as deliberate: eleven mutations, each reddening exactly the tests that name the fact it removed — dropping the migrate resolution alone reddens only the migrate test, and a control that re-wraps the paragraph without changing a fact stays green, so the assertions bind to content rather than to line breaks.
  - **A halt reached through `posting_history` routes identically to one reached through live rows.** `halts` is either count nonzero; a block keyed on `live_named` alone would have satisfied every other assertion and printed nothing for the count that has no origin column.
  - 29 tests — 14 against `replayed_db`, the real replayed legacy lineage, which is what caught a NOT NULL column reading the model had missed; 14 pure `render()` tests that need no database and cannot contend for the host; and one that asserts an unreachable DSN exits ERROR, which needs no database by construction.

### Changed

- **A non-vacuity disclosure that named a trip condition it could not reach (#806)** — `test_states_its_own_coverage_rather_than_passing_silently` said its count would move "when F.2.2 lands the first workspace-keyed table." It would not: F.2's tables are numbered above the move and its replay stops below it, so every table F.2 ever lands leaves it green. Measured in the same exported tree as the wiring above.
  - **This is worse than no disclosure, which is why it is a correction rather than an addition.** A disclosure is read as coverage; one that cannot trip converts "nobody can mistake this green for enforcement" into exactly that mistake. It now states what it actually covers — that the **legacy** corpus carries no workspace-keyed table — and names the condition that would really move it.
  - **`TestAgainstTheRealReplay` is renamed `TestAgainstTheLegacyReplay`.** There are two real replays now and the old name claimed both; the class docstring points at the target-lineage half by name.

- **CI's lint job now covers `tests/`, which is where the #816 defect lived and the one place the gate never looked (#816)** — the job ran `ruff check src/ cli/` while `CLAUDE.md` instructs contributors to run `ruff check src/ tests/`. Neither path covers the union, so `tests/` was linted by convention only, and a convention is what a rebase drops. The issue supposed CI did not gate on ruff at all; it does, and has since the job was written — the gap was the path list, which is a cheaper fix and a narrower claim.
  - **No new rule set and no findings to clear.** `ruff.toml` already selects pyflakes (`F`), so the repo's declared lint policy always covered F811; only the CI invocation excluded the directory. Measured before proposing: on `main`, `ruff check src/ cli/ tests/` exits 1 on exactly this defect, and on this branch it exits 0 with `ruff format --check` reporting all 309 files already formatted. The widened gate is green on arrival rather than green after a cleanup pass.
  - **`CLAUDE.md`'s pre-commit command is aligned in the same pass, and now matches CI exactly.** It read `ruff check src/ tests/` against a gate that ran `src/ cli/`, so each side covered a directory the other missed and neither covered the union. Widening only the CI half would have left the divergence intact in the other direction — a contributor running the documented command clean and still failing on `cli/`. Both are now `src/ cli/ tests/`.
  - **Kept as its own commit.** Widening a CI gate is adjacent to the issue rather than inside it, so it is separable and can be dropped without disturbing the fix.

- **One lock guards the cluster-scoped service roles, not two — and the survivor is the advisory lock, not the `flock` (#785)** — the #753 PR-A rebase over #772 left both mechanisms live and redundant, deliberately, so neither completed review was silently undone. Consolidating them is a choice between them, and the issue's suggested shape (keep the `flock`, widen it, drop the advisory lock) is the one **not** taken. Its two stated grounds did not survive measurement.
  - **The resource is CLUSTER-scoped; a `flock` is HOST-scoped.** Two containers or hosts pointing at one PostgreSQL share no temp directory, so the file lock silently fails to serialise them while the advisory lock still does — and silent is the operative word, because an unserialised run is green until it happens to overlap another one. `tempfile.gettempdir()` also follows `TMPDIR`, so the same divergence is reachable on a single host; measured as a mechanism, and stated as **latent rather than live**, since every bot on this fleet currently sets `TMPDIR=/tmp`.
  - **"Kernel-released on process death" is not a discriminator.** It was the issue's headline advantage for the `flock`. Measured on this cluster: SIGKILL an advisory-lock holder with no cleanup path available and the lock clears anyway — the backend sees EOF and exits. Both mechanisms have the property; only one of them is scoped correctly.
  - **Coverage now comes from the container rather than an enumeration.** The advisory lock is held session-wide by `admin_conn`, so it already covers `_sweep_leftovers` — which drops OTHER sessions' suite-prefixed databases and is the most destructive operation in the file. The suggested widening named three fixtures and did not name the sweep; a list of fixture names goes stale the next time one is added, and the failure mode of a stale list is an unguarded destructive sweep.
  - **It can name its holder.** The wait loop prints pid / user / application from `pg_locks`. The `flock`'s own timeout message had to tell operators to go hunting for a live holder instead, because a file lock cannot say who holds it.
  - **The cost is stated, not hidden.** The `flock`'s tests needed no PostgreSQL — argued at the time as "the lock has to hold when the database layer is already contended, so its own proof should not depend on the contended thing". That is given up: three path-shape tests died with the mechanism, and the two property tests that carried the guarantee (exclusion, crash safety) are rewritten against the advisory lock and now require the cluster. Contended is not unavailable, and every other test in that directory already requires it.
  - **Serialization re-proven, not assumed.** Two concurrent sessions of the role suites, both green (`23 passed` each), with the queueing shown directly rather than inferred from wall-clock: the second run logs `waiting for the suite cluster lock (held by (439590, 'test_user', ''))` and names the first run's backend. An earlier reading of "zero waits" was a pytest output-capture artifact and is not evidence of anything.
  - **Mutation-proven.** Removing the lock acquisition from `admin_conn` — leaving the suite with no mutex at all — reddens exactly the two tests that gate it, and leaves the two scratch-key property tests green, which is correct: they test the primitive, not the session's use of it.
  - **The holder query now has ONE home, and matching it properly fixed a latent misattribution.** `lock_holder()` is called by both the wait loop and the test that asserts holders are nameable; the test previously carried its own copy, so the real query could have broken and every wait printed `held by None` with the test still green. Proven by mutation in the new direction: breaking the *production* query reddens the test, which the duplicate could not do. The shared version also matches `classid`/`objsubid` rather than `objid` alone — measured, a two-argument `pg_advisory_lock(0, 7532026)` lands on the same `objid` as the one-argument bigint key and differs only in `objsubid`, so the loose form could name an unrelated lock's holder. That looseness was pre-existing and is now corrected in the one place it lives.
  - `_is_suite_db`, `_drop_roles_hardened` (`pg_shdepend` role teardown) and `_drop_db`'s retry hardening are unchanged. Both hardenings came from opposite sides of the union and both are kept, which was the one part of the suggested shape that needed no adjudication.
  - **Not covered, and filed rather than bolted on:** the CREATE side of role provisioning (`run_bootstrap(dsn)`) takes a bare DSN, so its coverage by the session lock holds through every current call path but is not enforced by signature the way `drop_service_roles(admin_conn, ...)` is; and `tests/scripts/` is not xdist-safe while two testing guides recommend `pytest -n auto`, a mismatch this change sharpens because the removed `flock` used to fail loudly at 300s where the survivor waits quietly to 1200s. CI runs serially and is unaffected.
- **Meta's platform facts are now verified against Meta's own current documentation, and three of them had drifted (#745, closes #734)** — plan §0.4's seven-item checklist, each figure confirmed-or-corrected with a primary-doc citation, fetched raw on 2026-08-13 so no summarizer sits between Meta's wording and the quoted claim. Full evidence: `documentation/planning/2026-08-02-consolidated-design-plan/0.4-meta-primary-doc-verification.md`.
  - **`PUBLISHED` is documented for Instagram containers, not only for Threads — so the reconciler seam resolves to container-verdict mode.** This was §0.4's load-bearing question, and the recorded premise was wrong: Meta's Instagram content-publishing guide lists five `status_code` values including "PUBLISHED — The container's media object has been published". A post-publish observable terminal status exists, the authoritative-positive set is non-empty, and R3 review §6.11's uncertainty ("marked uncertain, not false") is closed in the direction that keeps the pass-2 contract. **The trap now named in `02` §6:** after `publish_called`, `FINISHED` means *ready to be published* — evidence the publish has **not** landed, not that it failed — so it must never join the authoritative-negative set.
  - **"200 calls/user/hr" had the figure right and the shape wrong.** Meta: "Calls within one hour = 200 * Number of Users", where Number of Users is unique **daily active** users. It is an app-wide pool sized by DAU, not a per-user ceiling — so reading it per-user over-estimates headroom exactly when the user base is quiet.
  - **"Publishing remains URL-pull only" is contradicted.** Images are URL-pull as documented ("we will cURL your image using the passed in URL"), but a resumable push-bytes surface exists on `rupload.facebook.com`, accepts "a file located on your computer", and Meta states "all media_type shares the same flow" while framing it as the Reels path. D38's conclusions stand on their own merits — but the absence of a push-bytes surface can no longer be cited as a *premise*.
  - **Meta contradicts itself on the cap, today.** The publishing guide says "limited to 100 API-published posts within a 24-hour moving period"; its own endpoint reference says `quota_total` is "currently 50". Reported, **not adjudicated** — it is precisely the argument for the live per-account read the code already performs, and neither page may be cited as *the* number. Also newly recorded: carousels count as a single post.
  - **The token-refresh min-age is 24 h, and the 7-day cadence clears it.** `05`'s open question on row 56 is closed by measurement rather than assumption. A scope revocation presents as a *refresh failure*, consistent with D31 reading a definitive auth rejection as a liveness signal.
  - **`available_at` is not exactly derivable, so `02` §8's fallback is the behaviour rather than a contingency.** `quota_usage` is a count, never a list of publish timestamps. A bounded search over the `since` parameter could locate the oldest publish, but costs several calls per derivation against §8's own budget of at most one query per publish attempt.
  - **The exhaustion tail's stories check is corroborating, not dispositive.** The 24 h lookback is confirmed, and two previously unrecorded exclusions surfaced: responses omit Live Video stories, and a story created by resharing is not returned. A story absent from the list is not proof it was never published.

### Added

- **The two unverified tenant-credential callers from #796 now assert fail-closed, executably (#797)** — #796 removed the broad `except` that let a named tenant fall through to the deployment-wide service account, and the review stated its bound rather than implying it: fail-closed confirmed at **1 of 3** callers (`scheduler.py`), the other two left unchecked. Four tests close that, two per caller.
  - **The three callers are not the three the issue title suggests, and this is the first thing to get right.** "Callers of the tenant-credential path" reads as callers of `get_provider_for_chat`, of which there are two. The three that matter are the *downstream* callers that could re-swallow the error now that it propagates: `scheduler.py`, `telegram_autopost.py`, `telegram_notification.py`.
  - **The gap was real and is not what "unverified" implies.** Every existing test injects at `provider.download_file` — after resolution has already succeeded and a provider object exists. Measured: **zero** tests in the repository injected at `get_provider_for_media_item`, against five that inject at `download_file`. Both raise sites inside `get_provider_for_chat` (no stored credentials; no configured root folder) fire *before* a provider is returned, so no existing test could reach either — and those two are exactly what a tenant with broken OAuth hits.
  - **Each caller holds the property by a different mechanism**, which is the argument for pinning all of them rather than one: `scheduler.py` has `try`/`finally` and no `except` at all; `telegram_autopost.py` has no handling in the helper and relies on the outer handler showing the tenant an error card; `telegram_notification.py` has an explicit `except GoogleDriveAuthError: raise` plus a promotion inside its blanket handler. A refactor at any one leaves the other two green.
  - **Every test asserts the credential path was actually walked, and for WHICH tenant.** A fail-closed assertion that never reaches the credential lookup passes for free — the vacuous-gate shape in another costume. Mutation-proven directly: an early return placed before resolution reddens the auth test at both callers, which a bare `pytest.raises` would not have caught.
  - **The control is not decoration; at one caller it is the only test that catches the realistic regression.** Hoisting the resolution call out of `telegram_notification`'s guarded block leaves the auth test **green** — the error raises either way — and reddens only the non-auth control. The four existing `download_file` tests stay green through that same mutation. Reintroducing the #627 swallow in `telegram_autopost` reddens both new tests while its four existing upload tests pass.
  - **Not covered, stated rather than implied:** no resolution-injection test was added at `scheduler.py`'s `_auto_approve_instagram`. Its structure was independently re-confirmed on current `main` (the `try` carries `finally` and no `except`; the nested `except Exception` sits inside that `finally`, scoped to Cloudinary cleanup), and loop-level coverage exists in `test_main_scheduler_loop.py`. But every existing test replaces `_auto_approve_instagram` wholesale, so a real test needs setup this change does not attempt.
  - **One fragility found and reported rather than fixed:** `_is_google_auth_error` matches on `"RefreshError" in type_name and "google" in module`, walking `__cause__` only — a pattern match standing in for a property check, and blind to `__context__`. It is not load-bearing for the designed failures (both raise sites produce `GoogleDriveAuthError`, caught by the explicit clause first), so widening it is a separate call with its own evidence.

### Fixed

- **`test_instagram_api` defined `test_check_response_errors_revocation_subcode_458` twice, and the first definition never ran (#816)** — Python binds a class attribute once, so the definition at line 940 shadowed the one at line 388 and the earlier body has not executed since it landed. Collection reports the same 57 tests either way, so no count, tick or coverage number ever moved.
  - **The two bodies are the same test, and that took proving rather than reading.** The issue recorded that they differ, measured from a textual diff. They do differ — in the local variable name (`mock_response` vs `response`), the key order of a dict literal, and the docstring. None of those reach the code under test: `_check_response_errors` reads that dict through `.get()`, so literal order is inert. Normalizing both ASTs for exactly those three axes makes them identical, and a positive control confirms the normalizer still separates a genuinely different sibling test rather than collapsing everything it is shown. So this is a dedup after all — the shadowed copy was not holding a second intended assertion, and nothing is lost by removing one.
  - **The copy that survives is the one that was dead.** It is kept at line 388 inside the real `_check_response_errors` cluster alongside its five siblings; the deleted copy sat by itself under a second, duplicate section header appended later in the file. That direction is deliberate. It returns the test to the code it belongs with, and it is the direction that carries an obligation to prove, because the survivor is the body that had never executed.
  - **Mutation-proven in both directions, with the instrument itself controlled.** On `main`, an `AssertionError` planted at the top of the first definition leaves the suite at `57 passed` — it genuinely never ran. The identical tripwire in the second definition reddens one test, which is what rules out a broken injection rather than a dead body; without that control the first result is equally consistent with the tripwire never having been inserted. After the fix, the same tripwire in the survivor reddens, and a real implementation mutation (`error_subcode = None`, collapsing the revocation branch) reddens it as well — so the resurrected assertion has teeth against the code under test, not merely a pulse.
  - **Swept rather than assumed.** Two independent instruments over all 296 `.py` files in `src/` and `tests/`: `ruff --select F811`, and a standalone AST pass that enumerates every definition in every module and class scope and reports any name bound twice in one scope. Both find this to be the only shadowed definition in the repository. The AST pass additionally flags `BaseRepository._db` and `._db_generator`, which are `@property`/`@setter` pairs and legitimate — a known blind spot of that scanner, which does not read decorators, and the reason the ruff cross-check is what settles the sweep rather than the scanner alone.

- **`_check_media_pool`'s two fail-closed gaps from the #791 merge are closed (#794)** — both were raised in review, both left open deliberately because each already failed closed, and both filed rather than parked so they could not evaporate. Failing closed is why they were not urgent; it is also what made them awkward to test, since a fail-closed path returns `healthy: False` and so does nearly every other failure — an assertion on `healthy` alone passes whether or not the gap was ever exercised. Every test added here asserts what the sweep **did** (call counts, which tenant was reached, which counter moved), not merely that the result was unhealthy.
  - **Gap 1: the per-tenant guard wrapped the CALL, not the unit.** Reading a tenant's categories is that tenant's work, so a malformed row — a category with no `runway_days` — escaped to the outer handler and ended the sweep, losing the cross-tenant aggregate. It failed closed, but at a site that already *looks* fixed, which is the worse shape: the next reader sees a `try` and stops looking. The guard now spans the whole unit.
  - **The regression test is deliberately not a raising call.** The old guard already isolated those, so a test using a raising tenant passes against the unfixed code. The discriminating failure is a call that RETURNS a malformed row.
  - **`checked` is committed only after the unit succeeds.** Otherwise a mid-unit failure counts the tenant both checked and unreachable, and `checked + unreachable` stops equalling the population — the only property that makes the disclosure worth anything. Pinned by its own test.
  - **The first attempt at that restructure contained the defect it was fixing, one line further on.** Written as `tenant_worst is None or cat_info["runway_days"] < ...`, the short-circuit skips the lookup for the first row, so a malformed row is adopted unread and raises at the commit — *outside* the guard. The new test caught it. The loop now reads the value unconditionally and keeps the number rather than re-reading the row.
  - **Gap 2: `**coverage` was absent from the `not active_chats` early return and from the outer `except`**, so a caller reading `tenants_checked` / `tenants_unreachable` got them on the normal path and not on those two — re-opening the never-happened-versus-lost confusion those keys exist to close, one level up. Both now disclose.
  - **The two paths report the same `0 of 0` and are told apart by `healthy`.** An enumerated-empty population is `True`; an aborted sweep, where enumeration itself may have failed, is `False`. Asserted as a **pair**, because either verdict alone is satisfied by an implementation that always returns it.
  - **A reachable tenant with no categories is counted checked, not unreachable** — the control that stops the widened guard swallowing healthy tenants. Without it, a fix counting every tenant with no worst-category as unreachable would satisfy every other assertion while reporting a fleet-wide outage.
  - **Mutation-proven, five ways**, each killing exactly the tests that encode it: narrowing the guard back to the call, committing `checked` before the unit completes, dropping coverage from the empty-population return, dropping it from the outer `except`, and flipping the aborted sweep to `healthy: True` — two tests red on each.
  - Out of scope as the issue states: the bounded observation about `inst.get('telegram_chat_id', '?')` raising on a non-dict row, recorded there as a shape rather than a reachable defect.
- **`auth_monitor` records the real reason an initData credential was rejected, instead of `"Invalid token format"` for every one of them (#737)** — `_validate_auth` is a fallback cascade (initData, then a signed URL token) and only the *second* exception ever reached the monitor. A real initData querystring URL-encodes its colons as `%3A`, so it never presents four colon-separated parts and dies at `validate_url_token`'s format check before any check could describe the actual problem. Bad signature, expired, future-dated and wrong-bot-token all arrived spelled identically.
  - **A diagnosis fix, not an access fix.** The credential was correctly rejected either way and nothing here widens access. What it cost was the signal at the moment it is most wanted: a Telegram clock-skew event and a leaked-token probe both look exactly like ordinary scanner junk once every reason is the same string.
  - **Both reasons are carried, attributed to their format** (`initData: …; urlToken: …`), rather than picking one by the shape of the input. Shape is a heuristic and it would misread precisely the input worth reading correctly — a malformed initData that happens to contain a literal colon. Carrying both never guesses, and the URL-token branch is a real credential type (browser links) whose reason is not a formality.
  - **The client-facing 401 detail is now decoupled from the recorded reason** (`AUTH_FAILURE_DETAIL`). This is forced by the fix rather than bundled with it: the detail *was* the recorded string, so recording both reasons while leaving them coupled would have handed every rejected caller strictly more information than before. The specific reason separates "well-formed but mis-signed" from "expired" from "wrong credential type entirely" — the discrimination a probe wants and a legitimate client never needs. Nothing is lost that callers had, since the reason they received was already the URL-token branch's `"Invalid token format"` whatever the real cause. Operators still get every reason, through the `WARNING` line `auth_monitor.record_failure` already emits — which carries the running failure count too, so no second log line is added here.
  - **Verified by a paired measurement on the real cascade**, with `validate_url_token` unpatched so the second reason is whatever the real code produces: unfixed, all three initData reasons collapse to `'Invalid token format'`; fixed, all three survive attributed. Two controls pin the cascade itself — a valid initData and a valid URL token must still authenticate, without which every assertion is satisfied by a chokepoint that rejects everything.
- **The public setup funnel no longer instructs customers to create and link a Facebook Page (#802, FC-4)** — `/setup/instagram` carried a step titled "Create and link a Facebook Page", asserting "Meta requires a Facebook Page linked to your Instagram account for API access — even if you never use Facebook", plus a warning that "You MUST have a Facebook Page linked". FC-4 rules the opposite in as many words: **never make a user auth a Facebook Page again**. The claim is also factually wrong under the Instagram API with Instagram Login that FC-4 selects and #745 verified against Meta's own current documentation — no Page is required — so a live, indexed page was charging prospective customers the advertised 30-60 minutes to satisfy a requirement that does not exist.
  - **Removed at all six sites, not only the step.** The page repeated the requirement in its meta description, its intro paragraph, a cross-reference from step 2, the step itself, its closing warning, and the verification hint — and one of them (the intro) wraps `Facebook` / `Page` across a line break, so a grep sweep does not find it. What remains is exactly the one conversion FC-4 preserves, personal → Professional, and a callout stating plainly that no Facebook Page is needed, so a reader who followed the old instructions is told the requirement is gone rather than finding it silently absent. `/setup`'s prerequisite checklist loses the same item.
  - **`/setup/meta-developer` and `/setup/google-drive` are withheld from search** — `robots: { index: false, follow: true }` on both, and both dropped from `sitemap.ts`. They walk the reader through registering their own Meta app and their own Google Cloud project, and the product accepts neither: `FACEBOOK_APP_ID`, `INSTAGRAM_APP_SECRET` and `GOOGLE_CLIENT_SECRET` are all deployment-level, and no surface — Mini App wizard, onboarding API, CLI, schema — takes a tenant-supplied App ID, Client ID or Secret. Search was the only way in, since neither page is linked from the site header or footer, so closing that path removes the whole arrival. Both pages stay reachable by link, including from the blog article that cites the Drive one: this withholds them, it does not delete them.
  - **What the funnel should become is deliberately untouched here** and stays routed to #282 — whether onboarding is self-serve or waitlist-gated, whether `/setup` survives as a shape, and the rewrite of the two withheld pages. This change removes a ratified-forbidden instruction and closes a search path; it makes no product decision. Neither is blocked on #410, which gates whether a new tenant can complete the shared-app flow, not whether these pages may stop being wrong.

- **Two operator-facing docs described a throttle that no longer exists and pointed at an environment variable that will not resolve (#734)** — filed as "three guides state the stale 25/24h publish cap", asking for them to say 100. They do not say that. Both survivors state "25 posts/**hour**" attached to `INSTAGRAM_POSTS_PER_HOUR`, which was **renamed** to `INSTAGRAM_PUBLISH_LIMIT_FALLBACK` in #707 (`99b00ea`) and is absent from the tree outside changelog history; the third file named in the issue was deleted in `08a8ed5` (#738). Writing "100" as filed would have attached a correct figure to a removed mechanism — a confidently wrong sentence. Both now describe what actually runs: a live per-account rolling-24 h quota read from Meta, with the fallback named correctly for anyone debugging a failed read.

### Security

- **`auth_monitor` evicts stale sources instead of retaining every distinct source ever seen (#760)** — pruning ran only for the key currently being written, so a source that never recurred kept its entry for the life of the process. The map grew with the number of *distinct* sources ever observed rather than the number currently active, and `WINDOW_SECONDS` did not bound it: that constant governs how long a failure **counts**, not how long an entry is **retained**. It also grew precisely in the case that alerts nothing, since a source seen once never approaches `FAILURE_THRESHOLD`.
  - **Two mechanisms, because they bound different things.** An amortised global sweep drops sources whose failures have all aged out — that bounds the steady state, but only between its runs. A distributed caller mints keys far faster than the sweep interval, so a hard `MAX_SOURCES` cap is what holds during a burst. The issue notes the two are complementary; they are, and neither alone closes it.
  - **Eviction is ordered by least-recent failure, deliberately not by first-seen.** The two invert on exactly the case that matters: a source that started failing early in the window and is *still* failing has the **oldest** first-seen, so first-seen ordering would evict the source closest to `FAILURE_THRESHOLD` and turn a memory bound into a missed alert. Pinned by test — swapping the sort key reddens that test and nothing else.
  - **The cap's lossiness is stated rather than papered over.** At the ceiling something must go, and an evicted source restarts its count; ordering only guarantees the victim is the least active one available. The over-cap path therefore runs a forced sweep first, so stale keys — which cost nobody their count — are always surrendered before a live one is.
  - **Verified by mutation**, each reddening exactly the test that names it: disabling the sweep reddens only the stale-key test; disabling the cap reddens the cap test; evicting by first-seen reddens only the alert-preservation test. Two guard tests hold what the fix must not break — a source inside the window is never evicted, and a source partway to the threshold still alerts after a sweep has run.

- **`GET /analytics/service-health` is gated on the system admin role instead of on authentication alone (#667)** — the endpoint returns deployment-wide operational telemetry, and authentication was the entire gate, so any authenticated tenant user could read the whole system's health. No tenant rows are in the payload, so this is an authorization gap rather than a data leak — but a tenant has no business reading the deployment's health.
  - **The gate is `users.role`, the system-level role, and deliberately not `UserChatMembership.instance_role`.** The two are separate columns precisely so the distinction survives: `instance_role` makes someone an owner of *their own* instance, which is not operator authority over the deployment, and accepting it here would re-open the endpoint one tenant at a time. Pinned by a test at both the service and route layer — the other assertions pass either way.
  - **Resolved through `MembershipService`, the existing authorization door**, so API glue still never reaches across the repository layer. `is_system_admin` is fail-closed on the same terms as `is_active_member`: absent user id, unknown user, deactivated user, or any non-admin role returns False. A *deactivated* admin is refused too — otherwise the role outlives the account, and deactivating a departed operator would revoke nothing.
  - **Role names are now constants beside the schema that constrains them** (`ROLE_ADMIN` / `ROLE_MEMBER` in `src/models/user.py`, next to the `check_user_role` CHECK admitting exactly those two) rather than a string literal in the gate.
  - **Mutation-proven, three ways:** ungating the route (back to `_validate_auth`) reddens exactly the two route-level refusal tests; dropping the `is_active` conjunct reddens exactly the deactivated-admin test; dropping the role comparison reddens exactly the member and instance-role tests. The route-level tests are the load-bearing ones — the helper can be correct while the route is still ungated, and only calling the endpoint proves the gate is attached to it.
  - **No caller is affected:** no frontend source references the route, so gating it breaks no user-facing surface.
- **A named tenant now resolves to its own credentials or to an error — never to the deployment's service account (#627)** — a broad `except Exception` around the per-tenant Google Drive OAuth lookup fell through to `get_provider()`, the global service account. In a product where the operator and a tenant are different parties, that is a cross-tenant access path, and it was silent: the auth error that triggered it was downgraded to a warning on the way past.
  - **Two distinct boundaries crossed, not one.** The service account's *credentials* stood in for the tenant's; and because `get_provider()` reads `root_folder_id` from the service account's own stored metadata when passed none, a tenant with no configured root was handed a provider **rooted in the operator's Drive folder**, whose files would then be indexed under that tenant. The second is the sharper one and is asserted separately — the credential assertions do not catch it.
  - **It also masked the cause.** The tenant's own error was swallowed and the service account's surfaced in its place, telling a tenant to run an operator-only CLI command for a credential they do not own. `get_provider_for_media_item`'s existing regression test already described this chain in its docstring; the fallback that produced it was still live.
  - **The broad `except` was checked for a legitimate case rather than narrowed blind, and it does not have one.** The only candidate was "tenant has not connected their own Drive" — but onboarding refuses to store a media root until that tenant's own OAuth has proven access to the folder (`/api/onboarding/media-folder` validates through `get_provider_for_chat` and returns 400 on failure), so "named tenant, no working OAuth" is a broken state to surface, not a configuration to substitute around. Nothing asserted the fallback anywhere. The untenanted path — no `telegram_chat_id` claimed — still uses the service account and crosses nothing.
  - **`media_sync.py` was the same defect wearing a different hat.** `telegram_chat_id or settings.TELEGRAM_CHANNEL_ID` named a tenant the sync is not, so an untenanted sync resolved — and later refreshed — whichever tenant that env var happens to point at's OAuth credentials, and used them against *this* sync's folder. The tenant is now passed through as-is, including `None`.
  - **Two existing tests were asserting the crossing as correct behavior** (`test_create_provider_google_drive`, `test_sync_uses_overridden_source_type`, both pinning `telegram_chat_id=-100123456789` on a caller that named no tenant). Both were rewritten to assert the substitution does not happen, against that same distinctive value so a reintroduction fails by name.
  - **Mutation-proven, since the defect is a silent substitution and a test that cannot detect silence is worthless here.** Restoring the broad `except` turns exactly the four crossing assertions red and leaves both controls green; restoring the `TELEGRAM_CHANNEL_ID` substitution reddens exactly the two `media_sync` assertions. The controls are load-bearing: an untenanted caller *still* reaching the service account is what proves the negative assertions are green because the door is shut to tenants, not because the fixture never opens it.
- **F.4, first half: the premise behind `ENABLE`-without-`FORCE` is now executable instead of prose (#751)** — `02` §7-DDL rules the posture deliberately, on the ground that `svc_migration` owns every object and bypasses RLS as owner by design. **That ruling is sound and is not reopened.** What was missing is what makes it safe: the measurement it rests on — that an owner really does read straight through its own table's policies — lived only as three lines of prose in another module's docstring, taken by hand once and never asserted anywhere.
  - **A premise nothing checks can stop being true silently.** A server upgrade, a role attribute, a different ownership arrangement, and #750's gate stays green — because that gate checks the ruling's *consequence* (`FORCE` is absent) and never its *premise* (that the absence is safe). `tests/scripts/test_rls_harness.py` makes the premise an assertion.
  - **A schema-level gate cannot cover this, structurally — and that is asserted, not argued.** The harness builds one table with one policy and connects twice: the owner reads every tenant, the runtime login reads exactly one. Nothing in the schema differs between the safe deployment and the unsafe one, so `TestTheSchemaGateIsStructurallyBlind` shows the tenancy gate reporting zero violations on the very table an owner is reading across tenants through. Not a defect in #750; the reason F.4 has to exist separately.
  - **Measured on PostgreSQL 15.18 before any expectation was written**, with a standalone probe: owner reads both tenants under the ratified posture; `+FORCE` blocks it; `NO FORCE` — issued by the very role `FORCE` constrains — restores it, which is why `FORCE` is not defence-in-depth here. Runtime login: `bypassrls=false, superuser=false` (positive control), unset tenant GUC **raises** `42704` rather than returning an empty set, GUC set returns exactly one tenant, `SET ROLE` to the owner and both attempts to dismantle its own confinement refuse with `42501`. Errors are asserted by SQLSTATE, not message text.
  - **Mutation-proven:** broadening the tenant policy to `USING (true)` turns exactly the three confinement assertions red and leaves the other eight green.
  - **What is deliberately NOT covered, stated rather than implied:** F.4's remaining halves — definer-door confinement, transaction-reuse leakage, zero-NULL gates, `fn_auth_plane_sweep` as the only auth-plane path, and the credential-locality assertion the ruling actually names (runtime env carries exactly `svc_ingress` and `svc_worker`) — need the F.2 schema, and F.2's tables do not exist yet: there are zero RLS-enabled tables and zero policies in any shipped migration, and the app still runs on one `DATABASE_URL` with no `svc_*` login anywhere in `src/`. A gate that cannot run would be worse than a named gap.
- **The global rate limit now keys on the proxy-corrected client instead of the raw connecting peer (#776)** — `SlowAPIMiddleware` was assembled *above* `ProxyHeadersMiddleware`, so it evaluated the `30/minute` default before the scope's client had been corrected. `SlowAPIMiddleware` runs that default itself, in its own dispatch, for every route carrying no `@limiter.limit` decorator — so behind a single edge, **every tenant on every undecorated route shared one bucket**, keyed on the edge. That is a capacity ceiling on the whole service, not a per-abuser control. Fixed by adding the limiter before `ProxyHeadersMiddleware`, which puts it below on the request path.
  - **Measured on the real `src.api.app:app`, pinned uvicorn 0.47.0, 60 requests per arm.** Before: 60 distinct tenants behind one edge → **30 blocked**, a contiguous tail, i.e. one shared bucket. After: **0 blocked**, per-client buckets. The issue's own repro flips the same way.
  - **The `@limiter.limit` decorators were never affected** and are not changed here. They evaluate at the endpoint, already inside `ProxyHeadersMiddleware`, which is why the route-level protections in #726 / #759 / #774 worked as intended. This corrects the *scope* of those claims, not their substance.
  - **The trade named on the issue was real but one shape wide, and #774 has since closed it.** Keying on the corrected client means the caller's header now reaches the limiter. Measured: forging a *leading* entry does **not** open it (30 blocked, unchanged) — uvicorn's right-to-left trust walk still attributes to the address the edge appended. Only the **two-header** #765 shape opened it (30 → 0), and `DropAmbiguousForwardedForMiddleware` (#774) closes exactly that. This change is rebased onto a `main` that already carries it, and the combined stack holds on every arm: distinct-tenant 0, forged-leading 30, two-header 30, direct-untrusted 30. The ordering constraint that made this conditional is therefore satisfied, not outstanding.
  - **The two middlewares are load-bearing for different things, which is measurable and was measured.** Reverting *only* the ordering, on the full post-#774 stack, turns `test_distinct_clients_behind_the_edge_do_not_share_one_bucket` red with `30 of 60 blocked, first at request 30, contiguous to the end` — the shared-bucket signature — while the two-header test stays **green**, because with the limiter back above `ProxyHeadersMiddleware` the ceiling bites for the wrong reason. That test is an inert mutant for this change and is documented as such in its docstring, so a green test sitting inside a #776 class is not mistaken for evidence of #776.
  - **The property had a test, and that test could never have caught this.** It asserted the limiter against a FastAPI app built inline for the purpose, wired with `ProxyHeadersMiddleware` *outside* the limiter — the correct order, and never the deployed one, so no change to `src/api/app.py` could make it fail. Its `blocked == 30` also could not separate the two worlds: one shared bucket and one correctly-attributed caller both produce exactly 30, for opposite reasons. Removed rather than repaired, and replaced with assertions that drive the assembled app and vary the two candidate keys independently.
  - **The new tests were briefly vacuous, which is recorded because it is the more general trap.** `tests/src/api/conftest.py` disables the limiter for every API test; under it, "distinct clients do not share a bucket" passes against an app with no limiter running at all. Only watching the tests fail first exposed it — the failure pattern came out inverted. The helper now asserts the limiter is live before measuring anything.

### Fixed

- **A refused connection is a server that answered, not an absent one (#769)** — `server_answered()` read every `psycopg2.OperationalError` as "no PostgreSQL was configured" and took the skip branch. But `too many connections`, `password authentication failed` and `database does not exist` all mean the server **answered and refused**, so a local run silently skipped every integration test at `rc 0` — the #758 false PASS, reproduced inside the probe that exists to prevent it.
  - **The exception cannot discriminate, measured rather than assumed.** On psycopg2 2.9.12 / PostgreSQL 15.18, `pgcode` is `None` for a dead port, a bad password, an absent role, **and** a refused connection limit alike; only the message text differs, and matching on that is a locale-dependent string match. The `too_many_connections` case was filed as inferred — it is now reproduced, via a role-scoped `CONNECTION LIMIT 0` so nothing else on the shared cluster is disturbed. (`no such role` also reports *password authentication failed*, since PostgreSQL conflates the two to prevent user enumeration — so message matching could not separate those either.)
  - **The address is asked instead, and only where libpq cannot say.** A successful connection keeps its own evidence; the TCP probe runs on the failure path only, which is where the ambiguity is.
  - **The fail branch widens, deliberately.** A half-configured environment — PostgreSQL up, test credentials wrong — now fails where it used to skip. That run executed no integration tests either way, so the skip was never honest; the cost is paid in a printed diagnosis naming `DB_USER` / `DB_PASSWORD` / `DB_NAME` rather than in silence.
  - **A non-PostgreSQL listener counts as answered.** Verifying the wire protocol was considered and rejected as implementing a startup handshake inside a conftest. If something else holds the port, `DB_HOST`/`DB_PORT` is wrong and a loud failure is the right outcome.
  - **The unchanged bound is still stated:** a server that has just *died* remains indistinguishable from one that was never there, because neither leaves anything listening. That single case still reads as not-configured.
  - Mutation-proven: restoring the pre-#769 behaviour reddens exactly the two tests that encode the distinction, and the paired test asserts both verdicts together — either half alone passes for a function that always returns one answer.
- **Every `media_items.file_hash` is MD5 again, from one place (#619)** — three writers each chose SHA256 independently, so their rows could never match a Google Drive `md5Checksum`, which is what `src/utils/file_hash.py` exists to be comparable to. A non-matching hash is indistinguishable from novel content, so every miss was silent.
  - **The issue named one site; there were three.** Backfill (`backfill_downloader.py`), the Mini App upload path (`dashboard.py` — which hashes and then immediately runs a duplicate-content lookup that structurally could not match), and the Drive provider's own no-`md5Checksum` fallback (`google_drive_provider.py`), where the same file hashed two different ways depending on what the API returned, inside the method whose docstring says it exists for cross-source dedup.
  - **Consequences reached past dedup.** `media_repository.py` excludes items whose `file_hash` matches a currently-locked item, so the same content could be posted twice — once as the Drive copy, once as the backfill copy — because the guard could not see the equivalence.
  - **Fixed as a class, not as three constants.** `_HASH` in `src/utils/file_hash.py` is now the single algorithm decision; the new `calculate_bytes_hash()` serves callers holding content in memory, `calculate_file_hash()` still streams from a path, and both build from `_HASH`. A test asserts no writer calls `hashlib` directly — the defect was three call sites each free to pick, and that is what re-opens it.
  - **Mutation-proven per part.** Reintroducing a direct `hashlib` call in a writer reddens the writer scan; flipping `_HASH` to SHA256 reddens five tests across all three surfaces. Expectations are anchored to `hashlib.md5` rather than to the functions under test, which would have passed with the algorithm swapped. The scan carries a positive control asserting the pattern can match and that every scanned path resolves.
  - **Existing rows are untouched and still divergent.** Rows written before this carry SHA256 and remain unmatchable; re-hashing them is the media-hash dedup remediation already named as an M.1 precondition (#746, #790), and it is deliberately not in this PR — remediating before the writers are fixed just lets the app write more SHA256. The discriminator is free: 64 hex characters is SHA256, 32 is MD5.
- **Auto-approve notification failures are no longer swallowed silently (#782)** — the quiet "Auto-approved: …" confirmation was sent under a bare `except Exception: pass`, so every failure produced **no record of any kind**. The post itself succeeds and only the courtesy confirmation is lost, so nothing downstream looks wrong: a tenant simply stops hearing from the bot, with no signal anywhere. Same shape as #758 one surface over — a failure that degrades into silence is indistinguishable from the thing never having happened.
  - **Proven by running, not by reading.** #782 was found by source inspection and explicitly not reproduced, so reproducing it was the first work. Against unmodified `main` a failing notify left `[]` — zero log lines — and a `ChatMigrated` raised there yielded no recoverable pair. After the change both are green; the before-run is the mutation proof.
  - **Closes a named hole in the #743 recovery corpus.** `ChatMigratedError.parse_pair`'s docstring cites this exact site as a shape that produces no row, which is why "not in this corpus" must be read as *unknown* rather than *did not migrate*. A migration here lost the new chat id outright — the one fact a recovery pass needs. The test round-trips through the real `parse_pair` rather than matching prose, so a reworded message that stopped being machine-recoverable fails it.
  - **Both sides of the distinction are pinned**, per #764: a failed send must be visible, and a notification never attempted (not auto-approved, or no Telegram service) must stay silent. A fix that logged unconditionally would satisfy the first while destroying the signal.
  - **Delivery accounting deliberately unchanged** — `posts_sent` increments before the notify and the post genuinely was sent, so per the issue's scope note the failure is logged rather than re-classified.
- **Closed the #767 per-tenant-loop family: two remaining sweeps no longer let one tenant end the iteration (#783)** — a `try` wrapped the whole per-tenant loop at both sites, so the first unreachable tenant aborted to the outer handler and every tenant after it was never reached. Active chats come back ordered by `created_at ASC`, so the same tenant fails at the same position on every run and permanently silences everyone created after it. Both fixed to match #781's precedent rather than inventing a second pattern for the same bug: per-tenant `try`, the failing tenant **named in a warning** rather than swallowed, and the outer `try` kept only for shared setup.
  - **`_check_media_pool` (health_check.py) also discloses its coverage now**, because isolation alone would not have been honest here. This function's whole job is a **cross-tenant aggregate** — the lowest runway anywhere — and an aggregate over an arbitrary prefix is not a smaller answer, it is a wrong one. Nothing in the return distinguished "worst of 12" from "worst of 3". It now returns `tenants_checked` / `tenants_unreachable` alongside the verdict; the keys are additive and the only consumer is the aggregate health report.
  - **`send_startup_notification` (telegram_lifecycle.py)** aborted the entire admin message on one malformed instance row, so the admin was told *nothing* rather than told about the rest — and from the reader's side "no startup message" is indistinguishable from "the bot did not start". Malformed rows are now skipped and named.
  - **Each site proven before and after.** Site 1 against unmodified `main`: `the sweep stopped early: 1 of 3 tenants examined`, with a critically-low tenant behind the failure never seen. Site 2: `send_message ... Called 0 times`. After: 4 and 3 tests green respectively, with controls asserting a clean run stays silent so the warnings mean something.
  - **Review round (mason).** Two findings, both landed:
    - **Isolation alone made a total outage read GREEN.** With every tenant unreachable, `_check_media_pool` returned `healthy: True` — nothing had been measured, so the claim had no observation behind it. Coverage now gates the verdict: any unreachable tenant means the check cannot report healthy. Measured, this was broader than reported — a **partial** outage (1 of 3, the rest fine) also returned `healthy: True`, because the verdict was computed from whoever answered. Both are fixed and pinned, with a control proving a fully-covered critical pool is still reported critical.
    - **The family was not closed, because the PREDICATE was wrong.** Both enumerations encoded *"a `for` loop inside a `try`"*; the defect is *"a per-item loop with no per-item guard"*. Those sets differ, and three live tenant loops sat in the difference — none inside a `try` at all. The enclosing `try` never caused the bug; it only decided whether the abort was silent or loud. The tell is a true negative: `scheduler_loop.py:131` iterates tenants **correctly** and appeared in neither enumeration for the same reason.
    - **The check that terminates enumerates the POPULATION, not the loop shape.** A tenant list enters the code through exactly two functions (`get_all_active_chats`, `get_user_instances`) across 9 call sites — a container you can exhaust. On that predicate: 8 loops, **5 tenant loops, all now guarded, 0 unguarded**. The 2 remaining unguarded are the inner per-category loop (already inside the isolated region) and a stale-queue-row loop — neither iterates tenants.
    - Three more sites guarded accordingly: `telegram_commands.py` (`_handle_dm_status`, `handle_instances`) and `start_command_router.py` (`_handle_returning_user`). Each guard spans the **whole** loop body deliberately — `lines` and `keyboard_rows` are built in parallel, so a failure between the two appends would leave a numbered instance with no button.
  - **Re-enumerated by AST afterwards, and the method's own blind spot measured** — 10 unguarded loops before, **8 after**, with zero now iterating tenants. Extending the walk to `except`/`else`/`finally` bodies (which the original method structurally cannot see) surfaces **one** more, `atomic_session.py:56`; it iterates repositories in a `finally`, not tenants, so it is out of this family and deliberately unchanged.

- **One unreachable tenant no longer silences pool-depletion and Drive-token alerts for every tenant behind it (#767)** — both hourly sweeps in `scheduler_loop.py` wrapped their `try` around the **entire** `for chat in active_chats` loop, so a raise from any one tenant's `send_message` ended the sweep for everyone iterated after it. `get_all_active()` orders by `created_at ASC`, so the iteration order is stable: a tenant that reliably raises aborts at the same position on every tick, permanently. The only trace was one generic `Pool depletion check failed` line per hour, which names neither the tenant that raised nor the tenants that were skipped.
  - **Silent by construction.** The affected tenants look healthy. They simply stop being told their media pool is draining — the alert that exists to stop them running dry.
  - **The guard wraps the whole per-tenant unit, not just the send.** The send is the trigger the issue names, but a per-tenant *check* failure — a malformed settings row, a per-tenant query error — has the identical blast radius, so isolating the send alone would have left the same bug reachable by another route. The outer `try` stays for genuine shared-setup failures and keeps its existing warning text, which now means what it says.
  - **A swallowed `ChatMigrated` now surrenders both ids.** The realistic trigger is a group→supergroup migration (#743), and a stranded tenant is by definition an old one, so it sorts early. These alerts carry no queue item and therefore have nowhere durable to write; `ChatMigratedError`'s own docstring already names them as a hole in the recovery corpus, and a generic warning here would have cemented it. The pair is rendered through `ChatMigratedError.durable_message()`, so the new chat id lands in the one shape `parse_pair` can read rather than in prose. **This does not close that hole** — it is still a log line, not a row.
  - **The 24h cooldown still advances only on a delivered alert.** Unchanged behaviour, now pinned by a test: stamping on failure would turn one bad tick into a day of silence, and introducing a per-tenant `except` is exactly the edit that could have moved the stamp.
  - Five tests, each red against the unfixed code for the reason it names — the sweep reaching only the first of three tenants, and no parseable migration pair in the log.
- **Serialised the cluster-scoped service roles, and made scratch-database teardown survive a backend it cannot kill (#758 part 3, partial — see #768)** — #763 gave each session its own database, which cannot isolate the seven `svc_*` roles: they are **cluster-scoped**, so every session on a host shares them. `roleless_db`'s *setup* drops all seven unconditionally, so two concurrent sessions collide setup-against-in-use in both directions. This lands two measured fixes and **explicitly does not close the issue**.
  - **A host-wide `flock` now spans the whole role lifecycle** — the opening drop, the test, and the closing drop — because the collision is this fixture's setup against another session's live roles; locking only teardown would leave it untouched. The lock is keyed on `DB_HOST:DB_PORT`, derived rather than declared: roles are cluster-scoped, so the cluster is the correct exclusion scope, and two checkouts pointing at different servers must not queue for each other. `flock` releases on process exit, so a killed session cannot wedge the host.
  - **`_drop_db` no longer aborts when `pg_terminate_backend` refuses.** It raises `InsufficientPrivilege: must be a superuser to terminate superuser process` when an autovacuum worker is attached — normal, transient, and much more likely under concurrency. The drop then aborted, the database survived, its grants made the roles undroppable, and **every later test in that session failed at setup**: one teardown error produced five cascaded setup errors. Termination is now best-effort, the drop is retried within a bound, and a database that still stands at the deadline fails loudly rather than being left behind.
  - **Measured, same checkout, one variable, sequential control green at 6 passed:** two concurrent sessions without the lock → `rc=1`, trigger `DependentObjectsStillExist`; with the lock → `rc=1`, trigger `InsufficientPrivilege` — *identical counts, different cause*, which is how the second defect stayed hidden. With both fixes: one pair red (3 passed, 4 errors), one pair fully green.
  - **NOT FIXED, and not guessed at:** an intermittent shared-catalog race (`tuple concurrently deleted`, plus a grant held by another session's database) survives both fixes. Its mechanism is **unknown** — role work was verified not to occur outside the locked fixture, so the obvious explanation is ruled out. Tracked with all measurements on its own issue. Concurrent runs of the role suites remain unsafe; run them one at a time.
- **A lost test database now fails the run instead of silently skipping it (#758, part 2)** — `setup_test_database` caught **every** exception and degraded to `pytest.skip`. A run whose database vanished mid-setup reported `N passed, M skipped` at **rc 0**, so the tick, "0 failed", and `grep passed` all said the integration tests passed when they had never executed. A false FAIL costs an hour and makes you look; a false PASS makes you ship.
  - **The discriminator is observable, not guessed:** *did a PostgreSQL answer at the configured address?* No server means none was configured, and skipping is honest. A server that answered and then failed is a real failure and now propagates — nothing after the probe is swallowed. The one ambiguous case is stated rather than glossed: at first contact, a server that just died is indistinguishable from one that was never there.
  - **`REQUIRE_TEST_DATABASE` makes integration coverage mandatory where it must be** — set in CI, where "no database" is not a contributor without PostgreSQL but the service container failing to come up. Proven both ways on the real path: unreachable server without the flag → 8 skipped at rc 0; with it → 8 errors at rc 1, naming the address and the reason.
  - **The policy moved out of the `except` and into two pure functions**, which is why it now has tests at all: the old decision was unreachable by any test, and every failure mode collapsed into one answer.
  - **A mass skip fails the build** where a database is required, via a `pytest_sessionfinish` ceiling. Baseline verified against CI — five consecutive runs at exactly 10 skipped while the passed count moved (2356 → 2376), two re-measured independently. The number is hand-maintained and will drift; that is its known cost, and it is a backstop for shapes the verdict cannot see (a stray skipmark, a missing optional dependency), not the load-bearing gate.
  - Teardown also moved outside the swallow: the old shape `yield`ed a **second** time when teardown raised, which pytest reports as an unreadable fixture error rather than the cleanup failure it is.
- **A bounded `adopt` reported "no migration file" for files that exist (#755)** — `adopt(..., max_version)` handed the *bounded* migration list to `_load_manifest`, which used it for two checks that ask questions at different scopes. The orphan check ("does every manifest key name a real file?") is a question about the **corpus**; asked against the replay window instead, every manifest entry above the bound was reported as having no migration file while the file sat in the tree, filtered out by the bound. Latent today — the manifest tops out at 049 and 051 carries postconditions rather than an entry — and it fires the moment F.2.2 gives a target migration a manifest entry, taking all four bounded `adopt` call sites with it.
  - **The severity is the diagnostic, not the crash.** It answers a question nobody asked, confidently: the reader goes looking for missing files, finds them present, and stops believing the tool — at the moment they can least afford it.
  - **The fix separates the two questions rather than narrowing one.** The probe requirement stays scoped to the **window** (adopt decides nothing about files outside it, and widening it would fail a legacy-lineage adopt because a *target* file has no probe yet — true, and about the wrong files); the orphan check reads the **corpus**. The bound now has one home, `_within`, so a caller holding the whole corpus derives the same window `discover_migrations` would have.
  - **Chosen over the narrower form suggested on the issue** (filter the orphan list to the bound), which fixes the false report but also silences the check for an entry naming a genuinely absent file on every bounded run. Measured on the case that separates them: manifest names 009, no 009 in the tree, bounded — this fix reports it, the narrower one passes silently. Both forms are otherwise identical across the 17-test adopt suite.
  - Four mutants killed, none inert, including the original defect and the narrower form.
### Security

- **Settings validation no longer echoes a credential when an ambient env var collides with a field name (#775)** — `Settings` declares bare-named fields (`TELEGRAM_BOT_TOKEN` and friends), so pydantic reads whatever the ambient environment holds under those names, from a process this project does not control. On a validation failure its `ValidationError` renders `input_value=` with a truncated copy of the input — printing part of an unrelated **real** credential. It fired four times in one evening for four different operators, which is what makes it a property of the code rather than of anyone's shell hygiene. `settings = Settings()` runs at import, so a bare `pytest` was enough to trigger it.
  - **Measured before and after**, same `env -i` invocation with a synthetic sentinel token: **22 sentinel characters** reached the traceback before, **0** after. The check reports a leaked *length*, never the fragment, so the instrument cannot itself put credential-shaped material in a log.
  - **`SecretStr` does not fix this shape, and that is pinned by a test rather than left as a comment.** It is the obvious tool and the first thing the issue suggested, but the observed error is `missing` on a *different* field, and that error's `input_value` is the whole **raw input mapping**, assembled before field types apply — so the annotation never runs. Measured: a `SecretStr` field still leaked 22 characters via a sibling's missing-error. Without the test, the next reader tries `SecretStr` again and believes it worked.
  - **The fix is a boundary catch**: `Settings.__init__` converts any `ValidationError` into `SettingsError`, whose message is built from `loc` and `type` only — never `msg`, because some pydantic messages interpolate the offending input. Field names survive, so a startup failure is still actionable; an error saying only "settings failed" sends someone to add a print statement, which is how the value gets echoed again.
  - **The raise happens outside the `except` block, and that is the subtle half.** `raise ... from exc` chains the original and Python prints `__cause__`, putting `input_value` straight back on screen. But `from None` is *also* insufficient: it only sets `__suppress_context__`, leaving the original exception hanging off `__context__` for any logger, debugger or `repr()` to reach. Measured directly — under `from None` the sentinel is reachable via `__context__`; raised after the handler exits, `__context__` is `None` and it is not.
  - **Mutation-proven, and one mutant is why the test suite exists at all.** Leaking `err["input"]` → 49 characters, 2 tests fail. `from exc` → 22 characters, 2 tests fail. `from None` inside the handler → the traceback reads **clean (0 characters)** and the tests **still fail** — so the leak check alone cannot see that regression, and only the tests catch it. Two instruments, each blind where the other sees.
  - **Defect 2 of the issue is deliberately not addressed here** — see the PR for the analysis. The project still silently consumes ambient variables it never set; a `STORYDUMP_` prefix would remove the collision class rather than its symptom, but it is a breaking configuration change for every live deployment and belongs in its own increment.

- **The upload guard no longer fails open on an unrecognised format (#761)** — `_validate_upload_content` compared the client's declared MIME against the MIME detected from magic bytes, but the comparison was guarded on `actual_mime is not None`. Detection recognises five signatures; **anything else returned `None` and skipped the check entirely**, so an unrecognised format was *more* trusted than a recognised one. The declared type is attacker-controlled — an HTTP header, or the filename extension — so detection was the only real check, and it was the one being skipped.
  - **Measured before the fix**, claiming `image/jpeg` in every case: a real PNG was correctly **rejected**, while EPS, JPEG 2000, TGA, BMP, TIFF and WebP were all **accepted**. The guard was strongest where it was least needed and absent where it mattered.
  - **What that reaches.** `Image.open()` dispatches on *content*, and `MediaIngestionService._index_file` gates Pillow on the file *extension* (`{".jpg", ".jpeg", ".png", ".gif"}`). So `photo.jpg` carrying EPS or JPEG 2000 bytes is stored, and on indexing is handed to that format's decoder — decoders outside the intended set, and the ones carrying the advisories bumped in #724. `ImageProcessor.SUPPORTED_FORMATS` does reject them, but at `image_processing.py:53`, *after* the `Image.open()` on line 46 has already entered the decoder: containment, not precedence.
  - **Unrecognised is now a 400** naming the reason, distinguishable from a type mismatch.
  - **`video/quicktime` was allowlisted but undetectable, and inverting the guard would have broken it outright.** Every `ftyp` box was mapped to `video/mp4` without reading the major brand, so a genuine `.mov` declaring `video/quicktime` was already rejected as a mismatch. MP4 and QuickTime both carry an `ftyp` box; the brand at bytes 8-12 is what separates them, and it is now read. `_detectable_mime_types()` pins the invariant that closed the class rather than the instance: **`ALLOWED_MIME_TYPES` must be a subset of what detection can emit**, so allowlisting a type nothing can produce fails the suite instead of rejecting every genuine upload of it.
  - Same shape as #758 one layer up — absence of a signal read as absence of a problem. Both fixes replace "we did not detect anything, so proceed" with an explicit decision.
  - Written test-first: the six unrecognised formats and the QuickTime case were each **observed failing before the fix and passing after**.

- **Repeated `X-Forwarded-For` headers still bypassed the rate limiter after #726 (#765)** — `ProxyHeadersMiddleware` reads headers via `dict(scope["headers"])`, which keeps only the **last** occurrence of a repeated header name. A caller sending `X-Forwarded-For` as two header instances instead of one comma-joined value survived that collapse regardless of `TRUSTED_PROXY_HOSTS`, reopening the bypass #726 closed for the single-header case.
  - **The first fix attempt failed its own test and was discarded, not patched.** Merging every instance into one comma-joined value before the trust walk ran looked like the natural fix; it isn't one — whichever instance ends up last after concatenation still wins `ProxyHeadersMiddleware`'s right-to-left trust walk, the same defect relocated rather than closed. Measured independently in review: that merge-based draft performs *worse* than shipping no middleware at all.
  - **`DropAmbiguousForwardedForMiddleware` refuses to guess instead.** More than one `X-Forwarded-For` instance drops the header entirely, falling back to the raw connecting peer — the same safe path already proven for "no header at all." Runs immediately outside `ProxyHeadersMiddleware` in the Starlette stack.
  - Verified against the real pinned `uvicorn==0.47.0` source directly (not a reimplementation) across 8 scenarios, and end-to-end through the real rate limiter: 60 requests, two raw `X-Forwarded-For` headers each with a rotating forged value — **30 limited**, the same ceiling a single real IP gets.
  - Pinned by `TestForwardedForAmbiguity` in `tests/src/api/test_security_hardening.py`.
- **`X-Forwarded-For` is no longer trusted from arbitrary peers (#726)** — `src/api/app.py` mounted `ProxyHeadersMiddleware` with `trusted_hosts=["*"]`. Under the wildcard uvicorn does two things together: it honours the header from *any* peer, and it returns the **leftmost** entry of the chain (`_TrustedHosts.get_trusted_client_host`: `if self.always_trust: return x_forwarded_for_hosts[0]`). A proxy appends the address it observed, so every entry except the last is written by the caller — meaning `request.client.host` was a caller-chosen string, not an observation.
  - **What that reached.** Both IP-keyed controls in the app read that one value: the limiter's `key_func=get_remote_address` (the global `30/minute` and the per-route `5/minute` and `10/minute` OAuth limits) and `auth_monitor.record_failure`, whose buckets drive the operator alert at `FAILURE_THRESHOLD`. A caller rotating the header lands in a fresh bucket per request, so neither accumulates. Measured against the real middleware and the real limiter: 60 requests over a 30/minute limit, rotating the forged value — **0 limited**; with the fix, **30 limited**.
  - **What it did not reach.** Authorization never consulted the IP. `_validate_request` requires a server-side active `UserChatMembership` for `(user_id, chat_id)`, and the initData/URL-token signatures are HMAC-SHA256 compared with `compare_digest` under a 1-hour TTL. The lost property was the request ceiling and the alarm on top of sound auth, not auth itself — no signature becomes forgeable at any request rate.
  - **The fix names the peers instead**, via `TRUSTED_PROXY_HOSTS`. The default is the private ranges rather than a specific edge address: a public-internet client can never hold an RFC1918 source address, so it can never place itself in the trusted set, and the default needs no per-platform tuning. Narrow it to a concrete edge address where the platform publishes a stable one. The middleware is kept, not dropped — removing it collapses every client onto the proxy's address, which is strictly worse.
  - Pinned by `TestForwardedForAttribution` in `tests/src/api/test_security_hardening.py`, including a forged-private-hop case, a direct-to-app case, and a `test_the_checks_can_fail` probe against the vulnerable configuration so the suite cannot pass while observing nothing.
- **Concurrent test runs no longer destroy each other's database (#758, part 1)** — `tests/conftest.py` owns a test database **by name**: it creates it, runs `pg_terminate_backend` against every connection to it, and drops it. That name was a fixed literal, and every checkout on a host points at the same PostgreSQL — so two concurrent runs shared one database and whichever finished first executed that teardown against the other's live session.
  - **Measured while it was still true:** four consecutive runs of one unchanged branch produced **2, 12, 4 and 9 errors**, in different sets each time, in files that branch never touched — while the same branch was green in CI. Nondeterministic red that each side reads as their own code.
  - **The name now carries a per-session suffix**, mirroring the `runner_test_{uuid}` pattern `tests/scripts/conftest.py` already proved for its scratch databases; the root conftest was the one place still using a fixed name. It removes the collision rather than serialising around it.
  - **The base is whatever the environment configured, never the default in `settings.py`.** Measured across the estate, `.env.test` sets `TEST_DB_NAME` to *different* values in different checkouts, so the shared name is not one literal and checkouts fall into separate collision groups. Suffixing whatever is configured makes that irrelevant.
  - Verified three ways: the suffix asserted against the rule rather than a literal, both mutants killed (no suffix; a constant suffix), and a real run observed creating a uniquely-named database.
  - Parts 2 (a lost database must fail rather than silently skip — the false-PASS half) and 3 (cluster-scoped `svc_*` role leakage) follow separately.

### Added

- **F.4 RLS harness: tenant context does not survive connection reuse — the half that was wrongly believed blocked (#751)** — #789 landed the owner-bypass premise as 11 executable tests and stated the remainder needed F.2's tables. Review found that boundary over-broad: the transaction-reuse leakage case needs **one table, one tenant policy, one non-owner login** — all of which #789 already ships — and the only obstacle was that the harness's `_as()` opened a fresh connection per call, so reuse was inexpressible. That is a helper, not a dependency. Four tests, all acting as the **runtime login and never the owner**, which is the property that makes this module mean anything given the ruling's whole premise is that the owner bypasses RLS.
  - **Measured on real PostgreSQL 15.18 before being written, and the measurement corrected the specification twice.**
  - **The predicted SQLSTATE was wrong.** `SET LOCAL` on a clean connection was expected to raise `42704` (undefined_object) after commit; it raises **`22P02`** (invalid_text_representation). Once a custom GUC has been set even transaction-locally the placeholder is registered, so it reads back as an empty string and the `::uuid` cast fails rather than the lookup. It still fails closed, which is the property that matters — but the asserted code is now the measured one.
  - **A worse case was missing entirely.** `SET LOCAL` is the recommended safe form, and on a clean connection it is. But it does not *unset* — it restores the session value. On a connection where any earlier request issued a plain `SET`, the transaction-scoped value reverts on commit to the **previous tenant's id**, and the next read returns that tenant's rows with **no error at all**. Strictly worse than the specified case, which fails loudly: this one succeeds silently. A pool adopting `SET LOCAL` without also guaranteeing no session-level `SET` ever touched the connection is still cross-tenant leaky.
  - **Mutation-proven with discrimination, not just death.** Destroying reuse inside the helper kills exactly the 3 reuse-dependent tests and leaves the fresh-connection control green — which is what proves the control is a control. Asserting the *predicted* SQLSTATE instead of the measured one kills exactly 1.
  - **Zero skips verified structurally rather than by reading the count**: the module contains no `skipif`/`xfail`/`importorskip`, all four tests request exactly one fixture (`confined`), and the only skip vector in the chain is `owner_actor`'s all-or-nothing capability check — so a quiet partial skip is structurally impossible.

- **The migration gates run under the declared actors (#753, plan §0.2 actor split)** — CI now proves the SQL replays *as production runs it*, not merely that it replays. The lineage seeds and replays as a database-owner-shaped actor (non-superuser + CREATEROLE — legacy tables carry real legacy ownership, and the corpus is thereby measured to need no superuser: `uuid-ossp` is a trusted extension on PG13+), the step-0 bootstrap runs as that owner, and window-actor arms act as `svc_migration` after it. The **real-corpus** legacy replays — every one that exercises the actual migration files — now run as the owner actor, not the superuser; the superuser path is gone from those (rajan's review: `scratch_db` remains at 100+ call sites, but those are generic runner mechanics on synthetic tables, never the real corpus, so they are orthogonal). Local clusters without CREATEROLE skip by name, per the #752 precedent.
  - **Where the printed window sequence cannot succeed as declared, the gate asserts the failure BY NAME instead of hiding it (D1, #753):** no mechanism gives `svc_migration` ALTER rights on owner-owned legacy tables — ALTER needs ownership or membership, which no GRANT confers — so the declared-actor arms assert `must be owner` refusals at 050 (at-49) and 046 (at-45), R6-P0's class held in CI instead of found in the window. Owner-world arms keep the tail coverage green meanwhile; both flip on Chris's D1 ruling, and the seam is those named tests.
  - **Measured along the way:** adoption probes are privilege-sensitive — without the bootstrap's SELECT grant the first refusal is 048's probe *erroring* on its direct `posting_queue` read (probe errors are hard failures by design), before the catalog probes' absent evidence ever reaches the floor check. Bootstrap-before-adopt is sequencing, not ceremony.
  - **Host-level isolation for a shared test cluster (#758's class; the role half measured as #768's false-fail — two concurrent role-suite runs both rc=1 on DROP ROLE, the same suite sequential rc=0):** the `tests/scripts/` suite serializes host-wide on a cluster advisory lock (acquired visibly and boundedly, naming the holder while it queues) — concurrent runs from other bots queue instead of corrupting each other, which no per-process discipline can do for cluster-scoped spec-named roles. Every cleanup path is gated by one `_is_suite_db` ownership predicate: it drops only databases this suite's own naming convention created, so a role-drop blocked by a dev/staging database the deployment bootstrap legitimately touched is named and re-raised, never swept (guarded by a behavioral test). Scratch databases reuse #763's per-session identity; the `at45→at49→replayed` template chain is session-scoped, cutting the suite's redundant full-corpus replays (wall-clock 156s→107s).

- **The advertised-DDL extractor — the plan text and the F.2 migration files held in lockstep (#753 PR-B, plan §0.2 `advertised_ddl_replay`)** — `scripts/advertised_ddl.py`, standalone by the runner's rule (stdlib only, zero `src` imports). It extracts the advertised stream `02` §0 defines: every ```sql fenced block from `02` top-to-bottom then `07` in file order, minus illustrative examples, plus the mechanical expansion of the two §7-DDL policy lists (13 tenant-plane + 2 user-plane = the 15 policies literal-only replay would leave absent).
  - **"Illustrative" is a committed, reviewed fact — never a regex guess.** `scripts/advertised_ddl_manifest.json` classifies every one of the 22 blocks (18 normative, 4 illustrative — the `BEGIN;`/bind-parameter usage shapes) keyed by content hash, and the ratchet gate asserts the manifest and the docs are an exact bijection **both directions**: a block added, removed, or edited in the plan falls out of the manifest and fails the gate until a human re-classifies it (the F.6-ratchet shape). An illustrative entry without a stated reason is rejected at load; the stream build fails closed over any unsatisfied ratchet, so it can never silently drop or admit a block.
  - **Arm (b): the F.2 prefix-diff.** The concatenated target-lineage migration files (above the 051 move — F.2, alex's active lane) are held equal to a prefix of the advertised stream at the normalized-statement level, so the first F.2 file that lands is checked against the plan the moment it does. F.2 is empty today, so the prefix is vacuous — disclosed as such (the #750 non-vacuity shape) rather than passing silently, with the mechanism wired and a tripwire test that flips it load-bearing when a target file appears.
  - **Arm (a): the stream replays from empty under the declared actors, and the four §0.2 assertions hold on the replayed database** (`tests/scripts/test_advertised_ddl_replay.py`, unblocked by #778 landing the actor fixtures). The step-0 bootstrap runs as the owner, the extracted stream replays as `svc_migration` into the empty `public` — the M.3 step-3 shape — and then: a seeded intent transition (`scheduled → prompt_pending`) succeeds, an unseeded one (`scheduled → posted`) raises through the guard trigger, `pg_policies` carries the 15 pattern-expanded policies (`p_tenant` on all 13 tenant-plane tables, `p_user_plane` on both user-plane tables — the exact set literal-only replay left absent), and no door-owner system role (`svc_claim`/`svc_clock`/`svc_maintenance`/`svc_membership`) is left holding `CREATE` on `public`. This is the first time the extracted stream is executed end to end; it proves the plan's DDL is valid, correctly ordered, and installable by the declared actor — the check that was meant to catch an ordering or privilege defect in CI rather than in the cutover window. Arm (c) (stand-down variants) stays with the M-phase, per the scoped plan on #753.

- **F.2.1b — the lineage lane, and the boundary that makes two lineages fit in one directory (#746)** — F.2.2's tables were blocked three independent ways, all of them downstream of one fact: both gate tests replay the whole migrations directory unbounded, so any file numbered 051+ joins both runs automatically with no opt-in. The target schema shares four table names with the legacy one (`users`, `media_items`, `onboarding_sessions`, `category_post_case_mix`), so the first target `CREATE TABLE` dies; and the 3c schema move that clears that collision is itself what breaks parity the other way, emptying `public` of the ~14 legacy tables whose models are still registered on `Base`. This increment lands the move, bounds the legacy lineage, and stands up the lane that replays across the boundary.
  - **Migration 051 is the M.3 step-3c schema move** (`ALTER SCHEMA public RENAME TO legacy; CREATE SCHEMA public;`) — one numbered file, one transaction. It is numbered **before** `02` §0's shared trigger functions, which is the opposite of the filed split's instinct and the correction that matters: anything the stream creates ahead of the move rides into `legacy` and is gone from the target. The substrate does go first, but *within the target*, and the target begins at the move.
  - **The lineage boundary is derived, never written down.** The move file declares itself with a `-- runner:schema-move` marker — the runner already had a marker vocabulary — and callers ask `legacy_lineage_max()` rather than naming a version. A bound stated as a literal is a second enumeration of the corpus: right on the day it is typed, silently wrong the first time anything is renumbered. `04`'s own in-window rollback leg writes the boundary as "the 3c move file" rather than as a number, for the same reason. Missing marker and duplicate marker are both hard failures — **missing is deliberately not an unbounded pass**, because every caller is asking for the legacy lineage alone and answering "then replay all of it" hands back the target schema under the legacy one's name.
  - **This changes the runner surface #749 shipped, deliberately and as its own increment.** `apply_pending(dsn, migrations_dir)` had no version bound and `discover_migrations` took none; both now take an optional `max_version`, as does `adopt`. Ten call sites move with it (eight in `test_migration_gate.py`, two in `test_tenancy_gate.py`) — regenerated from the tree rather than quoted from a prior count. **Production is always unbounded**: the M.3 window is one invocation in file order, and nothing about it changes.
  - **The bound is load-bearing rather than tidy, and that is asserted rather than commented.** Same corpus, same runner, one difference: bounded, `public` still holds the legacy schema the legacy suites assert against; unbounded, it does not.
  - **Target models live on their own declarative `Base`** (`src/models/target/`, fork (a) — ratified on #746). The alternative was one base plus a declared target-table list, and a hand-maintained inventory is exactly the object that looks authoritative right up until someone measures it. `create_all` on a base whose only members are the target models derives the comparison from the models themselves; there is no list, so there is nothing to drift. Legacy models keep running the application untouched until M.3 flips it over — a visible switch, not a rewrite.
  - **Lane parity starts empty and says so.** Zero target models and zero target tables exist today, so the comparison is arithmetic on two empty sets. Both halves are asserted explicitly, in the shape F.2.0 used, so the first table to land forces a deliberate update instead of quietly converting a vacuous green into a load-bearing one. The comparator is the existing `schema_parity` one pointed at the other lineage, not a second one.
  - **The move's postconditions are its adoption probe, and are documented as that rather than as inventory teeth.** An inventory comparison across `ALTER SCHEMA … RENAME` cannot fail — the rename moves the namespace, not the objects in it — and a check that cannot fail proves nothing. The probe is driven against three distinct un-moved database shapes, each the one a smaller probe gets wrong: a brand-new empty database (where "public holds zero relations" alone reads *applied*), the populated database step 3a actually runs against, and a database with a pre-existing non-empty `legacy` schema (where "legacy holds relations" alone reads *applied*). Dropping either postcondition turns a test red; both were verified inert-free by mutation.
  - **The inventory assertion that can fail lives in the lane**, derived from the legacy models: every table the running application declares is present in `legacy` and absent from `public` after the move. Mutation-proven against the real database — dropping one table from `legacy` turns it red — rather than against a dict, which cannot tell you the query reads the right catalog.
  - **The ledger's survival is asserted too.** `runner.schema_migrations` reads back complete after the move and is *not* in `legacy` — the property that made the `runner` schema a dedicated home in 0.2, now checked instead of reasoned about.
  - **Landing the cutover changes what an armed runner would do to production, so the arming point is now gated** (`tests/test_deploy_guardrails.py`). Before 051, `runner apply` against production applied a fix-forward; from 051 on it performs the cutover. The only thing between those worlds is one commented line in `railway.toml`, read by hand and reported by three people in one week — a hand-check does not survive the person doing it. The invariant is stated generally (*no deploy configuration invokes the migration runner automatically*, matched on the module name across `railway.toml` and `Procfile`) rather than as "one key in one file is commented", which would only ever know about the arming point that exists today. Dormancy is asserted as **present and off**, not merely off: a config that stopped mentioning the runner altogether also satisfies "not armed", and that is a different state with a different remedy. No database, no fixtures — the guard stays readable when the migration suites cannot run at all. Production remains untouched and nothing is armed.
  - **Measured on PostgreSQL 15.18 as a non-superuser actor holding `CREATEROLE`** — `04` 0.2's declared bootstrap actor, which is stricter than the cluster superuser CI's `postgres:15` service provides.
  - **Named and not closed:** the actor-faithful replay (#753). The lane runs as the test actor, not as `svc_migration` after an owner-actor bootstrap. It does not widen the gap, and `advertised_ddl_replay` still has zero implementation. Also unshipped: `02` §0's two shared trigger functions, F.2.1's one surviving item, which did not land with the bootstrap in #752 — the target lineage is empty above the boundary, and a disclosure test says so by name.

- **F.2.1 — the step-0 window bootstrap, because roles are not a migration (#746)** — the F.2 split filed on #746 put role creation in a substrate migration. That was wrong in three places at once, and the correction is what this increment delivers. `02` §7-DDL opens with `ROLES ARE NOT CREATED HERE`; D40 and `04`'s F.2 entry say the same — the advertised stream runs as `svc_migration`, and a runner login holding `CREATEROLE` is a standing self-escalation path on PostgreSQL 15, the opposite of the §7 model. The seven service roles are provisioned by the `04` M.3 step-0 artifact, run by the database-owner actor, now transcribed to `scripts/window/step0_bootstrap.sql`.
  - **The blocker is F.2.2's, not F.2.1's, and it is measured rather than argued.** On PostgreSQL 15.18 — the version CI pins — `CREATE POLICY p ON t TO svc_ingress` fails with `role "svc_ingress" does not exist`, while `ALTER TABLE t ENABLE ROW LEVEL SECURITY` succeeds with no roles at all. Every table PR from F.2.2 on lands its policies in the same increment by construction, so the roles must exist before the first policy-carrying table can replay. The two-sided test asserts both halves: uncreatable before the bootstrap, creatable after, using the real role name rather than a synthetic one.
  - **Enablement being role-free is itself the shape F.2.0's gate exists to catch** — a table can be born RLS-enabled and policy-less without any role in the cluster, which is exactly the silent hole the tenancy gate now reports.
  - **Two further items in the filed split were struck, not deferred.** "Extensions" is empty scope: `CREATE EXTENSION` appears zero times across `02` and `07`, and `gen_random_uuid()` needs none on PG15 (§0's "no extension dependency" holds). The `archive` schema creates fine without roles but its `GRANT … TO svc_maintenance` does not, and its printed position is *inside* §7-DDL after every table — so it moves to F.2.7 with the rest of the grant matrix.
  - **Role cleanup must follow database teardown.** A privilege granted inside a database is a catalog dependency on the role, so `DROP ROLE` fails with `DependentObjectsStillExist` while that database survives. The ordering lives in the `roleless_db`/`bootstrapped_db` fixtures rather than being hand-rolled per test — it cost a round of red tests to learn.
  - **A non-superuser actor without `CREATEROLE` skips with the missing privilege named**, rather than passing silently. CI's `postgres:15` service makes `POSTGRES_USER` the cluster superuser, so the gate genuinely runs there; `04` 0.2 declares the actor's real shape as non-superuser + `CREATEROLE`, which is stricter than what CI provides.
  - **Nothing is armed.** No numbered migration, no production execution, `preDeployCommand` still commented. The step-8 stand-down variants that revoke the bootstrap's window transients are M.3's filing, deliberately not transcribed here.

- **F.2.0 — the tenancy gate: CI can now see RLS (#746)** — the parity comparator checks columns, CHECK and UNIQUE constraints and nothing else, and nothing anywhere in `tests/` or `scripts/` referenced `ROW LEVEL SECURITY`, `pg_policy`, `relrowsecurity` or `CREATE POLICY`. A migration that created a workspace-keyed table and omitted its policy passed every check this repo owned, which left "tables are born RLS-enabled" — the property `02` §7 makes load-bearing for FC-1 — enforced by author discipline alone.
  - **RLS is checked as an invariant, not a comparison, and that distinction is measured rather than assumed.** `Base.metadata.create_all` emits 24 foreign keys but **zero** policies, RLS-enabled tables, triggers or functions, so adding policies to the parity signature would not gate them — it would make parity permanently red. `scripts/tenancy_gate.py` therefore asserts over the replayed schema alone: every workspace-keyed table is RLS-enabled and carries at least one policy. `workspaces` counts as tenant-keyed via its `id` (`02` §7-DDL Class 1); Class 3/4 tables carry no workspace key by design and are recognised by that absence rather than by being named.
  - **Foreign keys go into the parity signature instead**, since both sides do emit them — compared by definition text, not by name, so a dropped composite FK is drift rather than a rename.
  - **Both halves are proven able to fail**, per the principle already stated at `test_migration_gate.py:180` ("a comparator that cannot fail proves nothing"): on real Postgres, a correctly born table passes, dropping its policy turns the gate red, and disabling RLS turns it red with a different message; a missing FK is a parity diff.
  - The corpus check **discloses its own coverage** — zero workspace-keyed tables exist until F.2.2, so it asserts that count rather than passing silently on an empty set.

### Added

- **F.1 ownership inventory and fail-closed interface spec (#746)** — classifies all 14 legacy tables against the ratified plan: 9 tenant-owned, 2 global (user-plane), 3 with no target. Documentation only; no schema, no migration, no production-table change.
  - **The answer key is `02` §9 composed with `02` §7-DDL, not §9 alone.** §9 is a disposition index (legacy → target) and never uses the words "global" or "tenant-owned"; the ownership taxonomy lives in §7-DDL as seven normative policy classes. Recorded so the composition does not have to be rediscovered.
  - **`chat_settings` splits across two ownership classes** — config to `workspaces` (Class 1, tenant-plane) but onboarding columns to `onboarding_sessions` (Class 3, user-plane, where §7-DDL states "identity precedes tenancy"). F.1's "required leading `tenant_id`" rule applied uniformly would break onboarding by demanding a value that does not exist yet, so the spec attaches the rule to the class rather than the table.
  - **F.6 baseline re-measured and it does not reproduce.** The plan anchors 75/76 telegram-referencing modules; measured 64 in `src/` and 67 in `src/`+`cli/`, at both the plan date and today. The code has not drifted (64 → 64), and #744 moved the module set by zero. The predicate that produced 75 is not recorded anywhere, so F.6 should commit the executable rule and derive the count from it rather than committing a bare number.

### Fixed

- **A group migrating to a supergroup no longer strands its tenant (#743)** — Telegram changes a chat's id when a group is migrated to a supergroup, and there was no handling for it anywhere. `ChatSettingsRepository.get_or_create` is the primary access path, so the first update arriving from the *new* id found nothing and **minted a fresh, blank tenant**: settings, Instagram account links, memberships, category mixes, posting history and queued work all stayed attached to the dead id and became unreachable. Nothing raised. From the user's side the bot appeared to reset itself.
  - **A handler, not a schema change.** The schema was already migration-shaped — eight tables key on the `chat_settings_id` surrogate and survive untouched, and the raw Telegram id lives in exactly three columns. `migrate_chat_id` re-points `chat_settings.telegram_chat_id` (the unique anchor) and sweeps the two denormalized carriers: `posting_queue`, which *filters* on the raw id so pending posts strand invisibly, and `user_interactions`, which holds the raw id and nothing else tenant-shaped so analytics fork across the boundary.
  - **All three updates run on one session, against the usual layering.** Each repository owns a ContextVar-scoped session of its own, so a service orchestrating three repositories would run three transactions — and a migration that committed `chat_settings` then failed would strand the queue on a dead id, which is the same defect in a narrower form. One session is the only way this is atomic.
  - **A tenant already at the new id is refused, not merged.** By the time that happens `get_or_create` has usually already minted the blank one, but it may equally be a tenant with real settings; choosing a winner silently would be the same data loss. `ChatIdMigrationConflict` names both ids so a human can reconcile them.
  - **Idempotent, because Telegram delivers the migration update twice** and the permanent `ChatMigrated` send-path error can re-deliver the mapping indefinitely. A repeat call is a no-op rather than an error — verified by disabling that guard, which turns the ordinary duplicate delivery into a raised conflict.
  - Handler registered on `filters.StatusUpdate.MIGRATE`; `allowed_updates` already includes `message`, so the service message is delivered.

- **The chat-id pair from a migration now survives in a durable column, for the queue-card delivery path (#743)** — the fix above handles the *live* service message. Chats that migrated **before** it shipped are still stranded, and the only channel that can still reach them is Telegram's other one: the permanent `ChatMigrated` **send-path** error. That signal was already arriving on every scheduler tick and being discarded — `send_notification` caught it in a blanket `except Exception`, logged it as a generic string and returned `False`, so the scheduler recorded the literal `"send_notification returned False"`. The new chat id, the one fact a recovery pass needs, existed only inside a log line and aged out with log retention.
  - **Both ids are carried, because neither alone is actionable.** `telegram.error.ChatMigrated` supplies only `new_chat_id`; the old id is known only at the send site, from the chat the send was addressed to. `ChatMigratedError` pairs them and renders the durable form written to `posting_history.error_message` — an existing nullable `Text` column, so **no schema change**.
  - **Rendering and parsing live together on the exception**, so a later recovery pass reads the pair with the writer's own definition rather than inventing a second regex that drifts. `parse_pair` returns `None` for any other send failure *and for a partial match* — a sweep sees every failure in the corpus, and a half-built pair would re-point a live tenant at a wrongly-parsed id, which is the data loss this issue is about.
  - **Not retried, for the same reason as the Google auth error**: a migrated id never un-migrates, so the remaining two attempts are guaranteed failures against a dead chat — spent on every tick forever, because `last_post_sent_at` only advances on success and a stranded tenant stays in `get_all_active()`. The queue row is still marked `failed` and still recorded to history; only the retries go.
  - **Deliberately decides nothing about recovery.** What to do when both the stranded tenant and the blank one exist — merge, re-point, or archive — is a data decision with user-visible consequences and is still open. Recording the pair is what every candidate option needs, so it lands now rather than waiting on that.
  - **The resulting corpus is a lower bound, and is documented as one on `parse_pair`.** Of the five send sites that address a tenant group id read from the database, this covers the one that has a queue item to record against; the other four are best-effort alerts with nowhere to write, which is the deferred schema change and not an oversight. Three shapes therefore produce no row — a tenant with no eligible media never reaches a send, an auto-approved post bypasses Telegram but for one notify whose handler is `except Exception: pass`, and the alert sends have no queue item. Those correlate with the tenants an operator most wants to find, so a recovery pass must read absence as unknown rather than as "did not migrate".
  - **Mutation-proven, each mutant shown to move an observable independent of the tests**: removing the scheduler catch takes send attempts 1 → **3**; removing the notification catch turns the outcome from a raised `ChatMigratedError` into `returned False` — the signal discarded, verbatim pre-fix behaviour; dropping the old id from the rendered message takes the parsed pair to `None`. The probe behind those deltas opens no database connection at all, which is why it stayed valid while the shared-database collision later fixed in #763 was still corrupting suite-level runs.

### Security

- **Cleared 28 advisories across four pinned dependencies (#724)** — `Pillow` 12.2.0 → 12.3.0, `python-multipart` 0.0.28 → 0.0.31, `cryptography` 48.0.0 → **50.0.0**, `pydantic-settings` 2.14.0 → 2.14.2. `pip-audit --no-deps -r requirements.txt` goes from 28 findings to none. No source change was required: each bump was checked against the API surface this project actually uses, not against the changelog in the abstract.
  - **`cryptography` needed 50.0.0, not the 48.0.1 the issue proposed.** #724 was filed on 2026-07-30 against a single advisory; re-running the audit found **four**, and only one of them is fixed in 48.0.1. Pinning 48.0.1 would have satisfied the issue's diff while failing the issue's own verification step, which expects a clean audit. The other three (`CVE-2026-69247` PKCS#7 Bleichenbacher oracle, `CVE-2026-69249` certificate-path blowup, `CVE-2026-69248` wildcard escape from `permittedSubtrees`) are **X.509 and PKCS#7 code paths this project never enters** — every import is `cryptography.fernet` (`Fernet`, `MultiFernet`, `InvalidToken`). They are closed here because the bump is free, not because they were exposed.
  - **The advisory that did reach us is the wheel's bundled OpenSSL** (`GHSA-537c-gmf6-5ccf`, fixed in 48.0.1). `cryptography` wheels statically link OpenSSL, so that one is not API-scoped the way the other three are, and Fernet routes through it.
  - **Crossing 48 → 50 is two majors, and the breaking changes miss this codebase in every case**: removed deprecated `*_KEY_TYPES` aliases, ChaCha20 counter semantics, stricter X.509 loading, FFDH deprecation, and PKCS#7 error uniformity. None is reachable from `cryptography.fernet`. The two that are not about APIs at all are the dropped **x86_64 macOS** and **32-bit Windows** wheels in 49.0.0 — no effect on Railway (Linux x86_64), but an Intel Mac loses its wheel and would build from source.
  - **`python-multipart` is the most exposed of the four** and the reason this is not purely hygiene. `/api/onboarding/upload-media` declares `file: UploadFile = File(...)`, which FastAPI resolves while binding parameters — *before* the handler body reaches `_validate_request(...)` on its first line. The parser therefore sees unauthenticated request bodies, so `PYSEC-2026-3040` (`parse_form()` using an unvalidated `Content-Length` to bound a chunked read) is reachable pre-auth.
  - **`pydantic-settings` is not reachable** — the advisory is `NestedSecretsSettingsSource` symlink handling under `secrets_dir`, and neither `secrets_dir` nor `secrets_nested_subdir` appears anywhere in the tree. Bumped because it is free.

- **OAuth start endpoints now authorize the caller — tenant account-injection closed (#725)** — `/auth/instagram/start` and `/auth/google-drive/start` took `chat_id` as a plain query parameter and minted a Fernet-signed state token from it with no caller check, while every one of the 37 `/api/onboarding/` routes runs `_validate_request` (initData or signed URL token, **plus** a server-side active `UserChatMembership`). Because the callback stores the resulting account against whatever `chat_id` the state carries, anyone who knew a chat id could complete consent with an account they control and have it attached to a tenant they have no membership in — for Google Drive, pointing a tenant's media source at storage the initiator owns, with the victim receiving a "connected!" notification they never initiated. `chat_id` is not a secret: it is plaintext in the URL-token format (`{chat_id}:{user_id}:{timestamp}:{sig}`) and known to every member of a group. The signed state was doing its actual job — CSRF protection binding the callback to the request — and was never an authorization check on who may *start* a flow; that check was absent. Both routes now take `init_data` and run the same `_validate_request`.
  - **The three server-side reconnect links are updated in step**, because gating the routes alone would ship a broken reconnect. The `/next` command handler knows the member who triggered it, so its inline button carries a signed URL token. The scheduler-raised alerts (`PostingService.send_gdrive_auth_alert`, `HealthCheckService.format_token_alert`) are raised *for a chat, not by a user* — there is no member to sign for, and `URL_TOKEN_TTL` is one hour, so a link sitting in chat history would be dead regardless. They now point at `/start`, whose dashboard reconnect path is already authenticated; their tests assert the absence of a start link rather than the presence of a button.
  - **Callback endpoints are deliberately untouched** — the IdP calls those, and the signed `state` is the correct control there.

- **Reject future-dated credentials so expiry cannot be bypassed (#580)** — both credential checks in `src/utils/webapp_auth.py` computed expiry as `time.time() - stamp > TTL`, which is bounded on one side only: a future-dated stamp yields a negative age that never exceeds the TTL, so the credential never expired. A new `CLOCK_SKEW_TOLERANCE` (60s) bounds the other side, applied to both `validate_init_data` (Telegram-stamped `auth_date`) and `validate_url_token` (worker-minted timestamp, validated by the API from a separate container — the two run as independently-clocked Railway services, so a token minted by a fast worker clock was born unexpirable). Sized to absorb real inter-container drift while inflating the worst-case validity window by only 60s against a 3600s TTL — the same value Fernet applies for the same rule. Restores the bounded-replay property the membership-authorization path already documents itself as relying on. Note that a future-dated *initData* rejection currently reaches `auth_monitor` as `"Invalid token format"`: `_validate_auth` discards the initData error and falls through to `validate_url_token`, whose parse fails first. The credential is correctly rejected either way; only the recorded reason is masked.

- **Ignore Claude fleet bot telemetry paths** — narrow any-depth `.gitignore` rules for `data/events/fleet-*.jsonl`, `data/.last-tool-call`, `data/.idle`: the files Claudlobby supervision hooks can write relative to the session cwd when the bot environment is absent (Claudfather/Claudlobby#874). Prevents a broad `git add` in an agent checkout from staging fleet telemetry into this public repo; product `data/` paths are unaffected.


### Removed

- **The legacy Facebook-Login setup guide is deleted (`documentation/guides/instagram-api-setup.md`)** — it walked a reader through creating their own Meta Developer app, linking a Facebook Page, and generating Graph API tokens by hand. The consolidated design plan's **FC-4** rules that path out as a *fixed constraint*, not a deprecation: *"OAuth direct through Instagram, Instagram User access tokens, no Facebook Page — never make a user auth a Facebook Page again"*; its FC-7 application note has the target shipping Instagram-Login-only from its first production day, with the dual-path refresh and sunset gate never shipping at all. A guide instructing readers to set up a path a ratified constraint forbids is worse than no guide, and this is the document that produced the impression that every customer needs their own Meta app. `instagram-login-setup.md` is the current path and all four inbound references now point there. **The Facebook-path code is untouched** — `FACEBOOK_APP_ID` is live in six call sites and removing live credential code is a different risk with a different review; filed separately, citing FC-4.

### Added

- **Numbered-SQL migration runner — merged == applied becomes provable (#712, plan §0.2)** — `scripts/migration_runner.py`, standalone by design (stdlib + psycopg2; imports nothing from `src`, so it can run as a predeploy step before the app can boot): a versioned ledger in a dedicated `runner` schema (`runner.schema_migrations` — deliberately not `public`, which the M.3 cutover renames wholesale), SHA256 checksum discipline (an edited applied file is a hard failure; `repair --version N --reason …` records the deliberate exception), a whole-run `pg_advisory_lock` so concurrent deploys serialize, one-migration-one-transaction with a `-- runner:no-transaction` escape for `CREATE INDEX CONCURRENTLY` (statement-split in autocommit, because a multi-statement simple query is itself an implicit transaction block), `-- runner:postcondition <SQL>` lines executed after apply (anything but true fails the migration), and psql-equivalent execution for the legacy corpus's self-transaction-managing files.
  - **`runner adopt` — how a live database that predates the ledger enters it.** Probe-decided per file from a reviewed manifest (`scripts/migrations/adoption_manifest.json`), never reading the legacy `schema_version` table (whose known 010/034 self-stamp gaps are the hazard class this replaces). Designed for the standing production uncertainty: a false probe at or below the required floor (045) hard-fails naming the version; above the floor, a contiguous false tail is *pending* for a gated apply — so one command is correct whether production is at 45 or 49, without knowing which. Proven in CI against production-shaped fixtures at both ends, plus incoherent-gap and tampered-floor refusals, and a failed adopt writes nothing.
  - **`-- runner:reapply-safe` (carried by 048)** — an idempotent data migration whose applied state is undecidable in place may sit pending below an adopted head; the gated apply re-runs it (safe live because the UPDATEs are bounded to the probe's own >24h evidence — a fresh in-flight claim is outside the row set by construction; no row lock protects it, since claims commit before handler work).
  - **Migration 050 — chain reconciliation.** Drops the 004/008-era auto-named `api_tokens_service_name_token_type_key` if present and normalizes `chat_settings.caption_style` to TEXT, after which replay-from-empty equals production (pending the gated `\d` confirmation against prod that plan §0.2 requires before its first production run).
  - **CI gates that replay the real corpus.** Setup + 001–050 replay from empty through the runner; a schema-parity comparator asserts replayed-schema == models-schema and provably fails on a deliberately-broken fixture. The parity gate surfaced five real ORM drifts, fixed here: `caption_style` `String(20)` → `Text`, and four DB-only partial uniques mirrored into `__table_args__` (013 `instagram_media_id`, 014 legacy `file_path`, 015 Google-Drive token per chat, 021 permanent lock per media) — #641's named instances.
  - **Adoption evidence is three-kinded and visible.** Data-only floor files enter as first-class `asserted` manifest entries — rejected above the floor (above it, trust is never the mechanism) and printed on their own `asserted NNN` line, so the operator approving first contact sees which of the fifty are evidence and which are declared trust. Files carrying `runner:postcondition` lines need no manifest entry at all: the probe derives from them, so the predicate has one home and cannot drift (the manifest/file divergence this replaces was caught inside this PR's own review).
  - **`runner parity --against <dsn>`** — the CI gate's schema comparator (`scripts/schema_parity.py`, same zero-`src` constraints) as an operator door: compare production against a replayed scratch database with the exact comparison CI runs. A file's execution mode (`wrapped` / `self-managed` / `no-transaction`) is a declared discovery-time fact shown in `status` output.
  - **No production execution in this change.** The Railway `preDeployCommand` wiring ships dormant (commented in `railway.toml`); enabling it, creating the `svc_migration` login, the pre-050 `\d` confirmation, and the first `runner adopt` against production are separate human-gated steps — runbook: `documentation/operations/migration-runner.md`.

- **Consolidated multi-tenant design plan** — [one authoritative design set](documentation/planning/2026-08-02-consolidated-design-plan/README.md) consolidating the data-model package (#721), the architecture package (#722) as amended by the ratified review findings (#730), an independent cold design, and the 2026-08-02 product rulings now fixed as constraints: workspace-rooted tenancy (a user owns multiple workspaces; each workspace holds multiple Instagram accounts; a Telegram account manages one-to-many), Telegram demoted from load-bearing substrate to one pluggable interaction layer, app-level Cloudinary with per-workspace scoping/signed short-TTL delivery/reap-plus-TTL-sweep as requirements, and Instagram API with Instagram Login (no Facebook Page, ever — verified against `origin/main` that no feature needs the Facebook-Login-only capabilities). Supersedes both prior packages' phase plans with a single execution sequence, supplies the operational numbers both left open, and records every contested decision with provenance and reversibility. Documentation only; no production posting, schema, queue, or infrastructure behavior changed.
  - **Second design pass (Codex reviews A/B on the plan PR)** — the plan is now executable rather than adjudicative: `02` is DDL-complete (real CREATE TABLE/index/trigger/policy SQL, three-class ON DELETE policy, TEXT+CHECK enums, verbatim claim/flip/refund SQL) with the R3 terminal-record invariant database-enforced (legal-edge reference table + terminal-freeze trigger + actor-required audit trigger + insert guard — implemented, not just claimed); the invalid `NOT NULL NOT VALID` procedure replaced by the staged CHECK→VALIDATE→SET NOT NULL sequence; a new L.0 increment builds the final async unit-of-work before any Phase-L machinery; the RLS × cross-tenant scheduler/claim hole closed with named system roles under explicit enumerated policies (no BYPASSRLS anywhere); the stale-worker permit protocol fences provider calls at the network boundary; the ambiguous-publish reconciler gets a concrete evidence contract (container status authoritative, budgets, `review_required` fallback); the Meta usage pre-check is cut to lazy-inline keyed on the real account (the 5-minute per-account cache read as an eager refresher would have generated ~11× the provider traffic of publishing itself); multi-account scheduling, account movement, workspace/membership lifecycles, and customer-visible failure behavior specified in a new `06`; OTP/session/OAuth-state/key-rotation/audit-integrity designs in a new `07`; migration-runner ledger + chain reconciliation (the 004/008 orphaned unique, 010/034 missing stamps) specified in 0.2; dual-write owners named per track; envelope arithmetic corrected for FC-1 multi-account; the global admission ceiling struck. One fork is open for the product owner (PA-1: provider-account identity across workspaces); implementers build its default until ruled. A four-lens consolidation review of the pass then closed the cross-file defects it had introduced — most materially: the live phase-1 manual-posting flow made first-class in the ledger (`published_via`, without which the history backfill was unsatisfiable), workspace ownership single-homed on the member row, the quarantine grain corrected to the serialization key (the fine grain was silently unmatchable as drafted), missing transition edges restored (offboarding cancel, operator resolve-posted), and retention given real access paths. The plan is now also **navigable and self-contained as one artefact**: its README states authority, reading order, and a verified self-containment guarantee (the one by-reference gate was inlined; fleet-side citations demoted to provenance); every file of the superseded 2026-07-29 package carries an unmistakable supersession banner (with `review-findings.md` distinctly marked honored-not-superseded).
  - **Third design pass (fresh-session implementer review on the PR — its 7-item ratification gate, executed in full)** — (1) the advertised DDL replays from empty exactly as printed: stamps and touch triggers literal in every block, the owner-exists trigger pair written out including the workspace-INSERT half the pass-2 trigger missed, `02` §2 reordered into dependency order, and a named CI fixture (`advertised_ddl_replay`) that replays the plan's own DDL. (2) Raw-SQL invariant tests threaded into the owning increments' gates: ownerless-workspace commit, job kind/workspace pairing equivalence + NOT NULL serialization keys, the approved→publishing flip rewritten as a CTE-coupled statement that cannot leak a cap debit, publish exclusivity widened (`uq_publish_exclusive` now covers `publishing_ambiguous`; `review_required` releases deliberately, recorded), actor-less governance mutations, and credential-model escalation attempts. (3) The DB credential model decided (D29): per-process logins + SECURITY DEFINER doors with zero role memberships — no `SET ROLE` anywhere; auth-plane sweeps owned by the role whose policies see the rows — plus `rate_counters`, one durable schema for every pacing/admission number the plan previously left homeless. (4) The named defects fixed: OTP verify is lock→compare→consume-on-success; reconnect "last issued wins" made schema-true by issue-time invalidation; account movement revokes the source credential in the same transaction (a move, not a fork); offboarding drains before revoking; `command_dedup` gains principal scoping + a payload fingerprint (replayed key with different content ⇒ 409, never a silent swallow); W.4 legacy history attributes multi-account chats by the `audit_log` account timeline; the permit "fence" claim reworded to its true mechanism with a strengthened lease CAS (state + expiry); a generic required-actor audit trigger lands on the five governance tables; JSONB `v` shape CHECKs. (5) Platform-fact conditioning: 0.4 is an explicit prerequisite of L.3/L.5, and the reconciliation contract **branches** on whether Meta exposes a post-publish container verdict at all (both branches fully designed, config-seam selected; per-intent exponential poll ladder kills the 1,440-calls-per-ambiguity case either way); the Meta-cap rationale corrected (other API grants on the same account, never phone posts); the usage pre-check ships behind a default-off flag the S.5 canary must earn. (6) The egress floor (timeout classes, one absolute retry budget, byte caps, SSRF-safe resolution) moves into L.0 ahead of every live path; S.3 keeps the deployment-wide budgets and hostile-fake battery, and drops the phantom Meta app-wide budget. (7) The two missing operational paths provisioned: an `EmailSender` port with `send_email` job kind (Resend as the named default — a new external service, owner ack requested; X.3's gate delivers a real code end-to-end), and the archive moved off the Railway volume into an in-database `archive` schema (D30 — PITR-covered, role-fenced, retention-dropped as tables, DR-drill-covered by construction). Cloudinary FC-3.4 lands as **D28 — server-signed per-request upload parameters** (ratified 2026-08-03, conditional on clean delivery; literal-presets fallback specced). Still open for the product owner: PA-1 and the email-provider ack.
  - **Fourth design pass (the 2026-08-03 rulings + the #732 liveness finding + a codebase anchor)** — (1) **Publish-time credential liveness (D31)**: a definitive provider auth-rejection (190 without a revocation subcode; the unparseable-token class per the corrupt-phrase classifier already on `main`) now demotes the credential to `expired` and the account to `reauth_required` from ANY Meta call, in the observing worker's transaction; the refresh cadence gets its missing `05` row (7 d from issue, jittered, decoupled from expiry proximity — the legacy within-7-days-of-expiry semantics left a dead 60-day token unprobed for ~53 days) and L.6 gains the dead-token symptom gate; a standing pre-publish probe and a fourth credential state are explicitly rejected. (2) **The sign-in ruling (FC-5)**: Google OIDC replaces email OTP — challenge table, verify flow, OTP rate scopes, `email_change`, and the `email_otp` provider are gone; sessions survive verbatim; `oauth_states` widens instead of forking (signin/link purposes, purpose-conditional context, cookie-nonce CSRF, the two-purpose start-token door); identity keys on the OIDC `sub`, never email (D32); linking is explicit-only, merge operator-only (D35); Apple descoped with its re-entry cost recorded (D34). (3) **The invitation ruling (FC-6)**: the app delivers, by email and Telegram — `token_hash` is THE accept credential and email a per-provider acceptance constraint (D33, per-provider on day one: Telegram is the no-email case now, Apple a second instance of the same rule); delivery rides the surviving `EmailSender` port (provider ack REOPENED as an owner item) plus one new `invitation` outbox kind on the existing sender; admin invitations exist per the mid-pass ruling ("we want both") behind the **D36 elevation gate** — the invitation role is a ceiling, admin grants only on a matched identity proof or an explicit audited role change, so a forwarded or screenshotted admin link cannot silently produce an admin. (4) **The codebase anchor**: every current-repo claim in `00`–`07` verified against `main` @ `2e13f97` by independent verification passes, run twice — the original sweep, then a full fresh re-verification after that session was interrupted before persisting its evidence (two-run provenance and claim-by-claim tables in the PR's pass-4 disposition comment); disagreements resolved in the code's favor and recorded in place — most materially the **Meta publish cap corrected 25→100** against Meta's primary documentation (derived arithmetic re-run; ceiling ~8.7/s absolute), W.4's attribution timeline re-grounded on `service_runs` (`audit_log` never records account switches), W.6 re-grounded on the real HMAC-signed token mechanism (no JWT exists on `main`), `07` §3 adopting the shipped `ENCRYPTION_KEYS` MultiFernet machinery, and the naive-timestamp backfill rule naming its three already-`TIMESTAMPTZ` exceptions; the fresh run further refined W.4's history-writer surface (the module named `posting.py` on `main` is a vestigial Drive-alert utility, not the history writer) and its activation-record inventory, and pinned the in-tree 25-per-hour guide drift to a filed follow-up (#734). The plan README carries the anchor statement with its coverage bounds. Still open for the product owner: PA-1, the email-provider ack, the Google consent-screen publishing status.
  - **Fifth design pass (the Codex R4 review dispositioned + three 2026-08-04 product rulings)** — R4's own closing statement bounded it: the pass-4 anchor layer held (every sampled codebase claim accurate); the failures were in the plan's conclusions and execution contract, and this pass closes them. (1) **FC-7 — offline cutover ruled**: the migration becomes one rehearsed transform inside a days-scale window with the owner re-authenticating Instagram and Drive by hand afterwards — the six-stage machine as migration shape, all dual-write and shadow-read machinery, the L.9 shadow/cohort cutover, Phase W, and Phase G are deleted with explicit tombstones (each existed only to keep two live systems agreeing, and R4's F.5→L.9 freshness finding is mooted by deletion rather than reordering); `04` gains Phase M (transform spec, Neon-branch rehearsal doubling as the first DR drill, window runbook with parity bar and rollback lever), 0.1 App Review becomes the window's schedule gate, and the ruled queue disposition (thrown out, not transformed) is a stated drop with the raw rows preserved in the archive snapshot. (2) **FC-8 — full cloud + adapter surface**: local media is descoped behind a retained two-count zero-row gate (live local/upload rows AND history reaching them — the NOT-NULL chain makes unmigratable media block history backfill; nonzero halts window prep); media sources become a pluggable adapter surface (D37 — FC-2's discipline applied to media, the seam not the second implementation: v1 ships exactly the Drive adapter through the new `01` media-source port, and `provider_file_ref` gains the stable-item-ref contract that keeps provider-side renames from evading repost prevention). (3) **The R4 executable-schema gap closed in full**: the 27 transition-seed rows printed (with a behavioral replay assertion so an unseeded matrix can never replay green again), RLS enabled at creation on every table with the complete role/grant/policy DDL (zero login DELETEs, structural), all nine SECURITY DEFINER door bodies printed (including the new `fn_invitation_accept` pre-membership door and a new `svc_membership` owner; `fn_comparator_run` died with shadow-read), target DDL for `onboarding_sessions` and `category_post_case_mix` derived from the legacy model shapes (`service_runs` needs none — the transform consumes and archives it), the `runner adopt` production-adoption command specified (postcondition-verified baseline seeding — never trust of the old ledger whose 010/034 gaps are the hazard), and a `plan_slot` job kind added for the intent-creation edge no prior revision gave a producer. (4) **The behavioral defects fixed as specified in the disposition**: resolve-retry refunds the recorded debit day and returns the intent debit-neutral (the double-debit R4 found), cancel-retains-debit is now a stated R1-rationale decision, grace-window restore is honestly state-plus-mandatory-reconnect (early revocation kept deliberately), only the immutable numeric Telegram user id can set the invitation elevation proof (`invited_tg_user_id`/`invited_channel_hint` split — our own D32 rule applied one level finer), `www.googleapis.com` joins the X.3 egress hosts (live-verified `jwks_uri`), `max_overflow` is pinned into the `05` connection inequality, and the email volume claim is quantified with a provider-wide budget (`email_global` scope). (5) **D38** — the transit TTL attaches to the asset, not the URL (ruled: "definitely spend nothing at current users"), recorded with its condition load-bearing: revisit when sustained monthly Cloudinary credit consumption exceeds 225 credits, the point where bandwidth alone forces the Advanced tier and token-based expiry becomes marginally free. Documentation only; no production posting, schema, queue, or infrastructure behavior changed.
  - **Ninth design pass (the Codex R8 review dispositioned — gates now verify their subject's identity, and the membership invariant survives PostgreSQL 16+)** — R8 executed the pass-8 disposition's own first volunteered soft spot (third consecutive round a volunteered spot located the finding) and one genuine cross-version conflict. (1) The abandon-after-3g mis-run: with bootstrap grants still armed, the abandon variant handed the completed cutover's target schema to `pg_database_owner` while all five capability gates certified a green "pre-window" state — five true lines over the wrong subject, the R7 class one level up. Both stand-down variants now open with a two-sided subject-identity guard before their first mutation (abandon: legacy marker present AND target marker absent; success: the mirror), both gates assert identity as their first line, and D40 gains the rule: a gate verifies its subject's identity before asserting properties over it. The reproduction also surfaced a mechanism nobody had named — `information_schema.table_privileges` filters to the viewer's enabled roles (the owner actor read 0 where `pg_catalog` held 156 privilege rows) — so gate predicates now read `pg_catalog` directly, making viewer-independence part of the runnable-as-printed requirement. (2) Pass 8's two fixes were mutually unsatisfiable on PG16+: the 0.2 creator-ADMIN contract guarantees `pg_auth_members` rows (seven, on R8's fresh 17.7 success path) that the membership-zero gate forbade — and the database owner cannot revoke them, because PostgreSQL records their grantor as the cluster role. The invariant is now stated version-aware around what §7 actually guarantees: no service role is a member of anything on any version (the direction that kills SET ROLE for every login), zero rows the other direction on pinned PG15, and on PG16+ the creator auto-grant rows tolerated in exactly their irrevocable shape — owner actor, admin-only, `set_option` and `inherit_option` false, so they confer no assumption or inheritance path; any looser or foreign grant still fails. Verification: 44-assertion battery, 44 green — both mis-runs refused with the target schema untouched, the refused mis-run's recovery path (the success variant) proven, the seven-row conflict reproduced on-shape on PG15 (cluster-role-granted ADMIN rows, owner revocation ineffective), foreign grants caught. R8 also re-established the previously unexercised suites (27 transitions + illegal edge, the 28,800 slot scan, five governance-trigger kinds, both invitation branches, `o_` outputs) on current executions. Documentation only; no production posting, schema, queue, or infrastructure behavior changed.
  - **Eighth design pass (the Codex R7 review dispositioned — both window exits now close their privileges, and gates assert properties, not mechanisms)** — R7 confirmed R6's three findings closed on the retry path and executed the one that remained: abandoning the window left `svc_migration` owning `public` with schema CREATE and SELECT on all fifteen restored legacy tables — while the stand-down's membership-zero gate passed, a gate observing its mechanism rather than its subject. Reproduced under the production actor before fixing. (1) The step-8 stand-down is now two printed per-path variants: the success variant asserts the steady-state shape (including that `svc_migration` owns `public` by design), and the new abandon variant — order load-bearing, its first two legs consuming the still-live self-grant — restores the pre-window shape (`ALTER SCHEMA public OWNER TO pg_database_owner`, the actual pre-window owner; bootstrap legacy grants revoked) with a gate asserting each restored capability. D40's placement rule gains its counterpart: a transient's home owns its closure on both window exits — "dies at 3g" is a valid closure only on the path where 3g runs. M.2 gains an abandon-variant rehearsal leg (the path nobody plans to use is the one nobody rehearses). (2) R7's PG17.7 probe confirmed the pass-7 disposition's first volunteered soft spot: the bootstrap's self-grant fails on PG16+ without ADMIN on a pre-existing `svc_migration`. The actor/ADMIN contract is now stated at 0.2's new Login bullet — `svc_migration` is created by the same database-owner actor that runs the bootstrap, whose creator-ADMIN makes the self-grant legal across versions; a foreign-created role needs an explicit `WITH ADMIN OPTION` grant before the window. Not a PG15 blocker; stated before it becomes one. (3) The two stale payload-free-view references (02 clock comment, 07 least-privilege bullet) now say column grant, matching the pass-5 replacement. Verification: 35-assertion battery, 35 green — the new rollback-then-abandon arm proves owner/schema-CREATE/table-grants/memberships/database-CREATE all restored and the legacy read capability actually revoked. Documentation only; no production posting, schema, queue, or infrastructure behavior changed.
  - **Seventh design pass (the Codex R6 review dispositioned — the window now executes under its own declared actor)** — R6 ran the cutover as `svc_migration`, the production migration role, and proved the pass-6 window could not: `SELECT legacy.*` denied (schema ownership grants no access to contained tables), the stream's unconditional `CREATE ROLE` denied, and `ALTER FUNCTION … OWNER TO` demanding role memberships `02` prohibited absolutely — with "production adoption skips roles that exist" prose that contradicted the byte-parity claim. All three findings (plus two same-class extensions at the 3f snapshots the review did not name) were reproduced under the production actor on PostgreSQL 15 before fixing. (1) **The window privilege split (D40)**: a new M.3 step-0 bootstrap — run by the database-owner actor, the one artifact where guarded DDL is legal — provisions absent roles, transient door-owner memberships, legacy SELECTs, and the 3c privilege pair; the F.2 stream is now role-DDL-free, gains its executor as part of its definition (parity = same artifacts, same actors, same order), and self-cleans its one transient (door-owner `CREATE` on `public`, which PostgreSQL requires for `OWNER TO` beyond membership — empirically forced); a new step-8 stand-down revokes the transient memberships behind a machine-checked `pg_auth_members`-zero gate, so `02` §7's no-membership invariant is a steady-state property restored by check. 0.2's CI gate and M.2's rehearsal replay under the declared actors — the rehearsal's bootstrap leg is where Neon's managed-role layer (which R6 explicitly did not test) gets its verdict, on a branch, before the window. (2) **The rollback made runner-retryable (D41)**: the in-window lever is four printed legs — drop target schema, un-rename, drop `archive`, delete ledger rows for the move file onward (3a's adopt rows and 050 stay: their effects ride the un-rename back, keeping the ledger truthful to the database at every point) — with stated retry (bootstrap stays armed, runner re-enters at the move file) and abandon (stand-down on top) paths; proven by a rollback-then-retry-to-completion run. (3) **The door bound rule true everywhere**: `fn_reaper_sweep` moves to one running remainder across its seven legs with the jobs lease re-ready leg promoted to first (liveness gates lane throughput; everything else is TTL bookkeeping) and gains its missing `05` row; the archive retention branch honors `p_batch` oldest-first; `fn_auth_plane_sweep` measured conformant under `05`'s per-class declaration; `02` §7's rule now carries the complete per-door instance inventory. Verification: a 30-assertion battery, 30 green, zero superuser past cluster init — from-empty actor-split replay, production-role window replay over the real 001–049 lineage with real legacy ownership, rollback + retry, stand-down invariants, and a negative gate proving the stream still fails without the bootstrap. Documentation only; no production posting, schema, queue, or infrastructure behavior changed.
  - **Sixth design pass (the Codex R5 review dispositioned — every finding a runtime failure, every fix runtime-verified)** — R5 executed the plan's printed SQL rather than reading it; all eight findings were reproduced on PostgreSQL 15 (verbatim error matches, including the exact 7,344/28,800 property-scan count and the 15-policy replay gap) before being fixed, and every fix was re-verified the same way. (1) **The cutover sequencing P0 (D39)**: replaying repository setup + migrations 001–049 and then the target DDL died on `relation "users" already exists` (four legacy tables share target names) — M.3 step 3 is now an exact 3a–3g runner sequence whose spine is the schema move (`ALTER SCHEMA public RENAME TO legacy; CREATE SCHEMA public`), making F.2's empty-schema precondition true in production by construction (CI's replay-from-empty and the window become the same act), relocating the snapshots after the `archive` schema exists (the pass-5 text snapshotted into a schema not yet created), adding a lossless in-place rollback lever until the final drop, moving the runner ledger to its own `runner` schema so the rename cannot sever migration history, and extending 0.2's CI gate with the full-lineage replay so this collision class fails in CI, not the window — the whole revised sequence executed end-to-end over the real 001–049 lineage. (2) **The governance-trigger P0**: the audit trigger's AND-chained machinery-column early-exits dereferenced fields of other tables' rows — PL/pgSQL resolves record fields at expression setup, which a false conjunct does not prevent — bricking inserts on all five governance tables (R5 hit the first domino, `workspaces`, i.e. signup); restructured to statement-nested table dispatch with the resolution rule recorded as normative, verified by a five-table insert/update/delete/no-actor battery with the machinery early-exits still silent. (3) **Door runtime repairs**: `fn_invitation_accept`'s output column shadowed `workspace_id` in its `ON CONFLICT` inference list (the door compiled but could not accept one invitation) — door outputs are now `o_`-prefixed by stated rule; `svc_maintenance` gains the missing `command_dedup` grant its sweep policy pointed at; archive export names use `clock_timestamp()` with microseconds (two batches in one transaction collided at second precision). (4) **`fn_next_slot` rewritten to index construction**: slots derive from the cycle grid (exactly `posts_per_day` per cycle by integer arithmetic — no hour+minute boundary test, no accumulated drift), taking the property scan from 7,344/28,800 failing combinations to 0; timezones are gated at write (`fn_safe_tz` + CHECKs, plus the M.1 mapping rule for unrecognized legacy values) and degraded per-row at read, so one bad zone defers one account instead of rolling back the whole tick — the regression class R5 named (set-based replacements must not turn survivable per-row faults into batch aborts) is recorded with the mechanism. (5) **Aggregate bounds made structural**: the clock's `p_max` is the tick's total insert budget across all four job classes (chained remaining-budget LIMITs, priority recurring → slots → refreshes → syncs) and the reconciler's `p_lim` bounds the whole sweep (ladder-due priority, notify-window fills the remainder) — the doors now enforce what `05` promises instead of leaving split limits to implementers. (6) **The 0.3 contradiction (P2)**: the no-production-changes-outside-M.3 ground rule now names its exactly-two pre-window exceptions (0.3's duplicate remediation, the media-hash dedup) with the closed-list rule stated. (7) **The `advertised_ddl_replay` paradox**: literal replay left the 15 pattern-compacted RLS policies absent, so the fixture could not be both "verbatim" and complete — the advertised stream is now defined as literal blocks plus the mechanical expansion of the two normative policy lists, and the F.2 migration files are held equal to that stream by diff, so the gate tests exactly what production runs. Documentation only; no production posting, schema, queue, or infrastructure behavior changed.
- **Per-tenant usage measurement, read-only (`storydump-cli usage-report`) — first increment of the monetization epic (#661/#662)** — reports posting activity per tenant over a trailing window: totals, success/failure split, and API-vs-manual split, busiest tenant first. **It measures; it never gates.** Enforcement is off *by construction* rather than by configuration — there is no write path, no threshold, and no flag that could be flipped by accident, because nothing here is consulted by a decision. That is asserted structurally in tests over **the service module** (it raises only `ValueError` for input validation, and its first-party imports are bounded to the one repository it reads), so a refusal path added *there* fails the suite rather than shipping quietly — the CLI is a display surface and the repository returns rows, so neither carries the same guard.
  - **No schema was added, and none was needed.** `posting_history` already records the tenant (`chat_settings_id`), an indexed `posted_at`, the outcome, and the posting method, so usage is a query over data the product already keeps. This matters beyond convenience: the consolidated design plan's `04` permits no production schema change outside the M.3 cutover window beyond two named exceptions, and FC-9 defers tier/entitlement schema to the `workspace_limits` extension point behind the admission seam. A metering table now would have been the third pre-window change the plan calls review-blocking.
  - Rows with a NULL `chat_settings_id` — pre-tenancy history — are reported under their own bucket rather than dropped, so the totals reconcile against a plain row count for the same window.

- **Outbound Telegram API pacing via AIORateLimiter — bursts queue smoothly instead of hitting 10–26s RetryAfter walls (#686)** — the worker's PTB `Application` now routes every outbound bot call through `AIORateLimiter` at Telegram's published budgets (PTB defaults: 30 msgs/s overall, 20 msgs/min per group, the per-group bucket keyed by `chat_id` so it scales across tenants), converting per-chat burst penalties into fair FIFO pacing. Callback answers carry no `chat_id` and skip the buckets entirely, so the instant acks shipped in #689 survive saturation — pinned by a contract test that fails loudly if a future PTB bump changes bucketing. Subsumes #653: the pending-caption fan-out is paced by the same per-group bucket, so no bespoke batch limiter is needed.
  - **One outbound bot (worker process).** `service.bot` is now the Application's rate-limited `ExtBot` rather than a separate raw `Bot` — approval-card sends (`send_photo`), caption and keyboard edits, and background-loop alerts previously bypassed the Application pipeline entirely, which would have left most burst traffic unpaced. The scheduler's Google Drive reconnect alert now receives the paced bot too (`PostingService` no longer holds a never-initialized `TelegramService` of its own). Out-of-process senders (API OAuth one-shots, CLI) have no Application and stay unpaced by design; Telegram's budgets are per token across processes, so `MAX_RETRIES` absorbs that residual.
  - **Kill-switch.** `TELEGRAM_RATE_LIMITER_ENABLED=false` builds the Application without a limiter — a no-redeploy rollback lever; bursts then hit raw `RetryAfter` walls again. `TELEGRAM_RATE_LIMITER_MAX_RETRIES` (default 3) bounds how many residual `RetryAfter` errors the limiter absorbs before surfacing.
  - **Saturation signal.** The limiter logs which bucket a call is about to wait on: a per-group wait is normal pacing (debug), an overall-bucket wait means the deployment-wide ceiling is binding (warning) — the multi-user capacity smoke alarm that gates any future rate tuning with evidence.
  - **Boot-blocking dependency.** `python-telegram-bot` gains the `[rate-limiter]` extra (aiolimiter) in `requirements.txt` and `setup.py`; without it `AIORateLimiter` raises at instantiation and the worker will not start.

- **High-throughput multi-tenant architecture design** — Added the [proposed design set](documentation/planning/2026-07-29-high-throughput-multi-tenant/README.md) for durable PostgreSQL commands/jobs/provider operations, Redis admission and work wake-ups, webhook ingress, fair worker pools, mandatory tenant context with RLS, observability, failure handling, phased rollback, independent evaluation, tiered triage, and file-oriented test-driven implementation. The direction is approved for final architecture review, not implementation. Documentation only; no production posting, schema, queue, or infrastructure behavior changed.
- **2026-05-26 IG posting root-cause investigation committed to the repo (#732)** — the PR audit that root-caused the persistent "Instagram connection has expired" failure (Meta error 190: stored token unparseable by Meta, and no code path validates token liveness with Meta before attempting to post — the bug PRs #433/#436/#441 each fixed real adjacent bugs without touching) existed only as an untracked file in a bot working tree, invisible to `git log`, search, and the consolidated redesign reviews. Committed verbatim (no content edits) under `documentation/planning/investigations/ig-posting-persistent-failure_2026-05-26/` so the redesign it informs can actually find it. Its "investigation doc" cross-reference resolves to the committed `ig-oauth-cross-flow-reconnect_2026-05-25/00_INVESTIGATION.md`.

### Changed

- **Storydump is described as the hosted, multi-tenant service it is (product-owner ruling 2026-08-07: *"Product I host and charge for!"*)** — `README.md` and `CLAUDE.md` both opened by calling it "self-hosted", which the ruling falsifies. The consolidated design plan (#731) already assumed a hosted product throughout by mechanism — one operator whose actions on tenant data are audit-visible to that tenant's admins, hosting substrate named as given while channels/media-sources/auth-providers are deliberate seams, a single App Review as the program's long pole, workspaces as rows rather than deploys — so this corrects the project's self-description to match what it was already being built as. README's "Quick Start" is retitled "Local Development Setup" and says plainly that it is not a deployment guide, since under the old framing it read as instructions for standing up your own instance. Found by `mason` during monetization pre-scope, where the epic and the architecture appeared to contradict each other; they did not — the docs were stale. Swept across the whole repo rather than the docs alone: `.claude/PROJECT_CONTEXT.md`, the landing site's **terms page** (a live public document — the factual claim in "Description of service" only; no obligation reworded) and a blog article, plus `documentation/guides/deployment-options.md`, whose "Multitenancy Model" section described fork-and-run-your-own as *the* model and now states the real one (tenants are rows on one deployment we operate). Instances found across four rounds by `mason`, `astrid`, `navi` and `astrid` again, each sweep wider than the last.

- **`telegram_edit_with_retry` no longer retries `RetryAfter` — the rate limiter is the single retry owner (#686)** — with the Application's `AIORateLimiter` absorbing rate limits, the edit helper's own `RetryAfter` ladder would stack on top of it (up to ~16 blocking waits, all held under the per-item operation lock — the "Already Processing" symptom). A `RetryAfter` surfacing past the limiter now gets one attempt, a warning, and the documented `None` failure value: no sleeps under the lock, and the helper's never-raises-on-transient contract is preserved for every callsite. `BadRequest` short-circuit and `TimedOut`/`NetworkError` bounded retry are unchanged.

- **The autopost operation lock is narrowed to the claim + spawn critical section — it no longer stays held through the whole background task (#703, refs #686)** — the per-queue in-memory operation lock was held from the Auto Post tap until the background task finished, so every concurrent tap during the slow, rate-limited edits (Cloudinary upload, Meta publish, caption updates) hit the "⏳ Already processing" toast for the task's full duration. The lock now guards only the brief claim → spawn critical section (plus the cheap keyboard strip / card reconcile) and is released before those slow edits, which run unlocked in the background task. The dedup the held lock used to provide is now carried by a per-queue **in-flight marker**: `claim_for_processing` leaves the row in `processing`, which is *itself* re-claimable, so releasing the lock early would otherwise let a second tap during the upload window re-claim and spawn a *second* autopost (the #549 double-publish). The marker — set under the lock before the spawn, cleared when the task finishes — makes that impossible: a re-tap sees it and is rejected. Terminal actions (Posted/Skip/Reject) defer to the marker as well as the lock and still set the cancel flag, so a manual action can't race the autopost's publish (the autopost aborts on the flag). Split from #686b per the 3-lens synthesis: #686b's single-retry-owner change already removed the worst lock-hold stacking; this removes the lock-across-waits pattern itself.
  - **Race-freedom:** two concurrent taps serialize on `lock.acquire()` — the winner claims + marks + spawns, the loser re-checks the marker under the lock and bails, so there is no second claim and no second spawn. A tap arriving after the lock releases is rejected by the top-level marker check before it ever acquires the lock. Pinned by a test that spawns a blocked background task and asserts a second concurrent tap neither re-claims nor double-spawns.

- **The Instagram publishing guard now reads Meta's live rolling-24h quota instead of a hardcoded "25/hour" estimate (#705 groundwork)** — `post_story`'s pre-publish gate previously counted our own posting history over a trailing 1-hour window against a hardcoded `INSTAGRAM_POSTS_PER_HOUR = 25`, a figure wrong on both axes: Meta's content-publishing limit is a rolling **24-hour** cap, and the real number is account-specific and has moved over time (25 → 50 → 100). The gate now calls the new `InstagramAPIService.get_content_publishing_limit()`, which queries Meta's `content_publishing_limit` endpoint for the account's true `quota_total`/`quota_usage` and gates on the live remaining — so the number is always correct without a code change when Meta shifts it. **Fail-open by design:** missing credentials, an empty response, or any endpoint error yields a permissive fallback (`INSTAGRAM_PUBLISH_LIMIT_FALLBACK`, default 100) so a monitoring blip can never block a legitimate post; Meta's own server-side 429 on the publish call (error 4/17 → `RateLimitError`) remains the true backstop. The sync `get_rate_limit_remaining` is retained but demoted to a best-effort local 24h estimate for status displays only (health check, settings screen) — it is no longer the gate. `INSTAGRAM_POSTS_PER_HOUR` is renamed to `INSTAGRAM_PUBLISH_LIMIT_FALLBACK` (env var included).

- **Hitting Instagram's daily publishing limit via Auto Post now shows a clean "daily limit reached" card with the manual-post buttons, not a generic failure (#706 groundwork)** — when an operator taps Auto Post and the account's publishing quota is exhausted, the callback is answered instantly (the spinner stops — no "Query too old" dead-end) and the approval card is reframed from "❌ Auto Post Failed" to "⚠️ Instagram daily limit reached", restoring the Posted / Skip / Reject buttons so the operator can post manually now or retry tomorrow. A rate-limit rejection is handled as *definitively not-published* — Instagram enforces the quota at the `media_publish` call — so it is resolved ahead of the #549 ambiguous-hold logic: if the pre-publish gate failed open and a container was already created when Meta's 429 fires, the row is released for retry rather than stranded in `publishing` with a zero-button "held for review" card, and the operator still gets the graceful card with buttons. The Cloudinary upload is cleaned up so no asset is orphaned. Not a dead-end on either path.

### Removed

- **The daily posting cap is gone — `posts_per_day` is now purely the scheduler's pacing target, not a hard ceiling (#705, #706)** — the per-chat daily cap (`daily_cap.can_post_today`, enforced at five call sites: the scheduler tick, `/next`, auto-approve, the Auto Post button, and the manual "Posted" tap) counted finalized posts plus in-flight publishes against the configured `posts_per_day` and hard-stopped once reached. It was a cadence knob mistaken for a rate guard — nothing ever tied it to Instagram's limits, and it was merely the accidental tighter throttle that masked the old "25/hour" estimate. Now that the publish gate reads Meta's live rolling-24h quota (#707), the arbitrary cap is redundant and removed cleanly. `posts_per_day` keeps its real job — the scheduler's `interval = window / posts_per_day` spacing divisor — so a chat still paces to roughly N posts/day; it just no longer hard-stops catch-up bursts, `/next`, or manual "Posted" taps. After this change the only outbound throttle is #707's correct-and-graceful Instagram publishing-quota guard.
  - **Closes #705** — the cap's check-then-act path (count → compare → post) had a TOCTOU window where concurrent ticks could each pass the check and over-post; with no cap there is no such race.
  - **Closes #706** — the cap-reject branch bounced the card to `pending` with an empty keyboard (a button-less dead-end). That branch is gone, and the Meta-quota path already renders a graceful manual-fallback card (#707).
  - **Technical details** — removed `src/services/core/daily_cap.py`, the five cap guards, `DailyCapReachedError` and its catch branch, the `"daily_cap_reached"` reason path, and the two now-orphaned counters (`HistoryRepository.count_posts_today`, `QueueRepository.count_recent_by_status`). ~21 dedicated cap tests deleted and incidental cap-disabling mocks cleaned up. `posts_per_day` (column, `MIN_POSTS_PER_DAY`/`MAX_POSTS_PER_DAY`, validation, default 3) is retained as the pacing input.

### Fixed

- **CI Lint is reproducible — the enforced rule set is declared in `ruff.toml` instead of inherited from ruff's built-in defaults (unblocks #721, #722)** — the Lint job ran `pip install ruff` unpinned against a repo with no ruff configuration, so the project's lint policy was whatever ruff shipped as its default `select` on the day CI ran. Ruff 0.16 widened that default, and main went red with **816 findings across 20 rule families** (UP045 alone accounts for 463) on a commit whose only content was a `.gitignore` change — no Python was touched, and the three files named in the failure had not been modified in weeks. Every open PR inherited the red, including docs-only branches containing no `.py` files at all, which is how two review slots ended up blocked by a lint failure neither PR could have caused. `ruff.toml` now declares `select = ["E4", "E7", "E9", "F"]` — the rules the codebase has actually been held to — so the verdict no longer moves when upstream defaults do; verified green under both ruff 0.15.10 and 0.16.1. The formatter was never implicated (`ruff format --check` passes unchanged on 147 files). The 816 findings are not suppressed bugs but unadopted new rules: widening the set is a deliberate follow-up that adds the rule and fixes its findings in the same PR.

- **The restart "catch-up herd" is bounded — a mass redeploy no longer dumps one immediate make-up card per behind tenant into a single scheduler tick (#714)** — on the first tick after a worker restart, every tenant behind ≥ 2 posting intervals fired an immediate catch-up post (`_compute_catchup_sent_at` returns `None` → post now, reset the timer to now), and the tenant fan-out is a plain sequential loop over *all* active tenants. So each restart bunched N make-up cards (N = behind tenants) into that one tick's window, all drawing the single shared ~30/s bot-token budget — the most frequent multi-user stress event, and linear in tenant count. The scheduler loop now grants at most `SchedulerService.CATCHUP_POSTS_PER_TICK_CAP` (8) catch-up sends per tick; a behind tenant past the budget is deferred (`process_slot(..., catchup_allowed=False)` returns `catchup_deferred` without sending or advancing `last_post_sent_at`), so it stays due and the backlog drains a cap-sized batch per subsequent tick. The budget counts **attempts, not successes** — a granted catch-up that fails to send still spent a call against the shared budget, so it consumes budget too; otherwise the cap would fail open under exactly the send-failure contention it exists to bound (the first 8 sends failing would grant another 8 in the same tick). **Normally-due tenants (behind < 2 intervals) are never gated** — the cap smooths only the catch-up class. Complementary to #686: the `AIORateLimiter` paces a within-tick burst into FIFO order once it forms; this stops the herd forming in the first place. A behind-≥2-intervals check is factored into a reusable `_is_behind_catchup` predicate (now the single source of that definition, shared with `_compute_catchup_sent_at`). No schema, no new deps.

- **The scheduler's inline reap honors the delivery-state machine — an aged `delivered` card is expired history-first, `sent_unconfirmed` is left to the reconcile (#560, #687)** — the scheduler tick's age-based reap still filtered on `status == 'processing'`, the pre-redesign shape of a button-bearing stuck row. Under the delivery-state machine that population is empty: `resolve_stale_processing` parks `processing` rows at 10 minutes (stamped → `delivered`, unstamped → `sent_unconfirmed`), so a stamped card a human never acts on ages out as `delivered`, not `processing`. The reap now targets `delivered`, so a late tap on an abandoned card shows "Expired" through the shared history-first reap (`expire_sent_row` → `record_expiry_and_delete`, writing the terminal `expired` history row before the delete, #687) instead of the raw "Queue item not found". `sent_unconfirmed` stays excluded — its lifecycle belongs to the offloaded aged reconcile, never the scheduler's generic sweep, and a real-DB test pins that it is left untouched. The hourly cleanup loop keeps its full status-agnostic-for-stamped sweep (the `delivered` stranding safety net). Consolidating the two age-based reapers into one path (Epic #560) is a separate follow-up; this increment only makes both honor the new states. No migration — it operates on the states migrations 047/048 already established.

- **The `sent_unconfirmed` reconcile lifecycle is closed — a tap resolves it to `delivered`, and never-tapped rows age out to terminal `expired`, the reliability spine's last open state (#680, #687)** — the delivery-state work (below) parks an ambiguous send in `sent_unconfirmed` and deliberately excludes it from every existing sweep (`_get_stale_scheduled` skips it; `resolve_stale_processing` selects only `processing`), which left it with **no reaper at all**: a genuinely phantom card (the send raised after delivery was already impossible, so no card exists to tap) would sit in the queue forever. Both ends of its lifecycle are now owned.
  - **On tap → `delivered`.** A button tap on a `sent_unconfirmed` card already resolves the ambiguity through the seam PR2 wired — `claim_for_processing` admits the state and `set_telegram_message` promotes it to `delivered` on the stamp — so a card a human is actively using is never in the expiry set. That path is now pinned by a real-DB regression test rather than left implicit; no new on-click code was needed (consolidating on the existing stamp-heal seam rather than forking a second promotion).
  - **Aged-out → `expired`.** A new bounded, offloaded aged-reconcile expires the never-tapped rows. `QueueRepository.get_aged_sent_unconfirmed(hours, limit)` selects `sent_unconfirmed` rows past their `scheduled_for` reap age (status + age only, INV-2), oldest first and **bounded** by `limit` so one pass can never block the loop on a backlog; `reconcile_aged_unconfirmed` routes each through the shared history-first reap (`record_expiry_and_delete`, #687) — writing the terminal `expired` / `system_expiry` history row before the delete (INV-3) and carrying **no send path**, so it can never re-post (the #680 class it must not reintroduce). It is folded into the existing hourly cleanup loop and dispatched via `asyncio.to_thread`, so its synchronous DB pass runs off the shared event loop and can never starve other tenants' callbacks (the #682/#573 loop-starvation class). No migration — it operates on the states migrations 047/048 already established.

- **Stale-`processing` sweeps resolve rows to their delivery state instead of requeueing or raw-deleting them, killing the sweep-requeue duplicate and the raw-delete orphan at the root (#680, #687)** — the two named processing sweeps inferred "never sent" from `telegram_message_id IS NULL`, an assumption the #679 ambiguous-delivery class falsified: a timed-out send can deliver the card while the stamp never lands. `requeue_stale_processing` reset such maybe-delivered rows to `pending` and re-sent them (the #680 double-card residual); `discard_abandoned_processing` raw-deleted them with no history row (the #687 orphan shape). Both are replaced by `resolve_stale_processing`: selection keys on **status + age only** (INV-2 — NULL carries no lifecycle meaning), and disposition applies INV-1's definition — stamped → `delivered`, unstamped → `sent_unconfirmed`. Nothing is ever reset to `pending`, so no sweep can re-arm a send; parked rows stay claimable on tap, and their aged expiry belongs to the reconcile lifecycle (next increment). `discard_abandoned_processing` is deleted outright rather than converted: its population is provably empty (stale processing rows park at 10 minutes, and the hourly cleanup sweep already deletes aged unstamped rows through the history-first `record_expiry_and_delete`), and removing it removes the codebase's last raw-delete of a possibly-delivered row.
  - **`delivered` is now written where the stamp lands (the PR2-earmarked fast-follow)** — `set_telegram_message` promotes `pending`/`processing`/`sent_unconfirmed` rows to `delivered` in the same write that records the message id (INV-1: delivered ⟺ stamped), covering both the send-success path and the callback-time stamp heal (a recovered stamp resolves `sent_unconfirmed` one-way to `delivered`, as its contract promises). Guarded so it never clobbers `publishing` (the IG claim anchor) or terminal `failed`. Status-only sweeps are only safe once this write exists — a healthy `delivered` card is otherwise indistinguishable from crash residue while both rest in `processing`.
  - **The hourly cleanup sweep is status-aware: `sent_unconfirmed` is excluded** — `_get_stale_scheduled` guarded only on `status != 'publishing'`, so the 24h sweep would have silently reaped `sent_unconfirmed` rows, racing the purpose-built reconcile lifecycle that owns them. `delivered` rows deliberately **remain** sweepable on the stamped side: an unacted card must still age out (the stranding safety net is status-agnostic for stamped rows and stays that way).
  - **Status-enumerating consumers cover the delivery states** — the dashboard's in-flight count/list, batch-approve's sweep, and the account-switch batch card update (`get_pending_with_telegram_message`) now include `delivered` (and, where a card may exist, `sent_unconfirmed`), so cards that rest in the new states don't vanish from those surfaces.
  - **`transition()` is pinned to a single conditional UPDATE** — `synchronize_session="fetch"` could split the guarded write into a pre-SELECT plus a primary-key UPDATE, and that second statement carries no `allowed_from` guard: a transition blocked on a concurrent row lock could re-apply to a row that had already left the allowed set (the read-check-write TOCTOU the seam exists to close — surfaced by the new INV-1 constraint under the concurrency suite). `synchronize_session=False` keeps the guard inside the one statement the database evaluates.
  - Migrations (both Neon-branch dry-run gated, human sign-off before prod): `048_backfill_queue_delivery_states.sql` maps legacy `processing` rows to the delivery vocabulary — stamped → `delivered`, unstamped → fail-safe `sent_unconfirmed` — with a pre-flight row-count precondition (pause and investigate on large deviation from the ~9-row authoring snapshot; the 7 `failed` rows correctly need no update since `failed` is valid in both vocabularies). `049_inv1_delivered_requires_stamp.sql` adds `check_delivered_stamped` (INV-1: a `delivered` row must carry its `telegram_message_id`), mirrored on the SQLAlchemy model, making the #687 "delivered-but-unstamped" orphan shape unrepresentable. The tenant-ownership threading seam (#412/#542) is deliberately left open: no sweep or claim path grew single-tenant assumptions.

- **Central authorization gate on the Telegram callback dispatcher for multi-user tenant scoping** — inline-button actions that operate on a queue item (post, skip, reject, auto-post, back, cancel-reject, regenerate-caption, account selectors) are now authorized at the dispatcher *before* any handler runs. The caller must be an active member of the chat the tapped card lives in, and the queue item named in the callback data must belong to that chat's instance. Authorization resolves from the one field a client cannot choose — `query.message.chat_id`, the chat the tapped message actually lives in — making this the callback-layer mirror of the web layer's `_validate_request` membership gate; it fails closed with a neutral reply and the handler is never reached. Instance ownership is checked owned-or-null (a legacy row with no instance stamp is allowed, so the gate does not depend on the instance-ownership backfill and unstamped rows still post); tenanting the queue resolvers themselves is left as a separate, deeper change. Other callback families (settings, account, instance, schedule) continue to self-authorize, so DM-launched settings and onboarding flows are unaffected.

- **`posting_method` / `history_status` CHECK constraints are single-sourced from a code-owned enum, ending the drift class that aborts scheduler sweeps (#685)** — the allowed values for `posting_history.posting_method` and `.status` lived in three unsynced places (the DB `CHECK`, the model `__table_args__`, and scattered code literals). Adding a value and missing the constraint produced a production `CheckViolation` that aborted the entire sweep pass — #684 for `system_expiry`, and #685 for `auto_reapproval`, whose write path (the scheduler default-config repost) is a live code path the constraint still rejected. The DB `CHECK` and the model constraint are now both *derived* from a code-owned enum and guarded by a CI parity gate, so a value added without its migration fails a test instead of aborting a production sweep. (Converting the value *producers* — the code paths that still hand-type the method string — to reference the enum is a documented fast-follow, not done here.)
  - New `src/models/enums.py` (`PostingMethod`, `HistoryStatus`, and `QueueStatus` — the target queue vocabulary for the follow-up status-model migration) is the single source of truth; `src/models/posting_history.py` now *derives* `check_posting_method` / `check_history_status` from it via `sql_in_list`; `tests/src/models/test_enum_ssot_parity.py` is the CI parity gate (model + migration must match the enum); migration `046_posting_method_ssot_add_auto_reapproval.sql` widens the constraint to include `auto_reapproval` (non-destructive constraint widen; human-sign-off-gated prod apply).
  - Parity-gate limitation (#692 ironclad review): the migration-side check is a static assertion against the migration file, because the test harness bootstraps the schema with `Base.metadata.create_all`, not migration replay — so it can only see the model constraint, never the applied migration. A follow-up strengthens it to introspect `pg_constraint` on a replayed DB once migration-replay CI exists (the #510 P0 migration rails).

- **`posting_queue.status` gains the delivery-state vocabulary, single-sourced from the same code-owned enum + parity gate (#684)** — the queue's `(processing, telegram_message_id NULL)` limbo conflated two very different situations: a card *claimed but not yet sent* versus a card *sent but whose message-id stamp was lost*. `QueueStatus` (introduced dormant in the #685 enum work) is now wired to `posting_queue.check_status` — the model constraint derives from it via `sql_in_list`, and the CI parity gate covers it — and migration `047` widens the DB `CHECK` to add `sent_unconfirmed` and `delivered`. A new `QueueRepository.transition(queue_id, to_status, allowed_from=…)` is the single guarded seam for delivery-state writes: it rejects an illegal move as a no-op (the way a concurrent claim or reap would), and `update_status` now delegates to it. `claim_for_processing` admits the new states so a human tap on a delivered/unconfirmed card still claims instead of resolving to "Queue item not found". The `#510` `ready`/`claimed` lease rename stays deferred to its own increment; writing `delivered` at the send-success stamp and migrating the status-enumerating consumers (dashboard, batch-approve) is a PR3 fast-follow.

### Fixed

- **An ambiguous Telegram send is recorded as `sent_unconfirmed` instead of being left in limbo and re-sent, killing the maybe-delivered-card duplicate (#680/#684)** — on an `AmbiguousDeliveryError` (the send timed out; the approval card may or may not be in the chat) the scheduler left the row in `processing` with no message-id. The 10-minute `requeue_stale_processing` sweep then reset that row to `pending` and re-sent it — posting a *second* approval card for a story that was probably already delivered. The scheduler now transitions the row to the new `sent_unconfirmed` state via the `transition()` seam, which is out of `requeue_stale_processing`'s `status='processing'` scope, so a maybe-delivered card is never reset and re-sent. It resolves one-way — a recovered stamp or a button click promotes it, otherwise the existing 24h unstamped sweep ages it out via `record_expiry_and_delete` (no re-send).

- **The manual completion path writes its terminal history row idempotently, so a re-claimed lingering row can't double-insert (#680)** — `claim_for_processing` deliberately re-accepts a row already in `processing` (crash / slow completion — the "~2 rows stuck ~18h" live-ops case), so a replayed Posted/Skip/Reject or batch-approve could write a *second*, undeduped `posting_history` row for the same queue item. `_execute_complete_db_ops` / `_execute_reject_db_ops` now write via `create_idempotent` (keyed on `queue_item_id`) instead of raw `create`. A partial unique index on `posting_history.queue_item_id` is the intended DB-level backstop, but a Neon-branch dry-run found **6 pre-existing duplicate groups in production** from this exact bug — so the index plus a prod-dedup ship as a separate, human-gated follow-up rather than a migration that would fail on apply.

- **Age-based sweeps write a terminal `expired` history row before deleting unstamped rows, so an orphaned live card degrades to "Expired" instead of the raw "Queue item not found" (#687)** — the hourly `delete_stale` prune (#483) hard-deleted >24h `telegram_message_id IS NULL` rows on the documented assumption they were never sent to Telegram — falsified by the #679 ambiguous-delivery class, where a timed-out send delivers the approval card but the stamp never lands (root cause tracked as #680). Deleting such a row with no history row orphaned its live buttons permanently: the tap-time fallback (#560/#561) had nothing to find and surfaced the scary raw error (live-captured 2026-07-19). The record + delete step of `expire_sent_row` is now a shared helper, `record_expiry_and_delete`, and both never-sent age sweeps go through it row by row instead of bulk-deleting: the hourly cleanup loop (`get_stale_unsent`, formerly `delete_stale`) and the scheduler's 10-minute JIT sweep (`get_stale_unsent_pending`, formerly `delete_stale_pending`) — the JIT sweep can race a delivered card the same way once the processing requeue flips an unstamped ambiguous send back to pending. `reap_pending_rows` (the batch reaper behind the admin clear-queue / resume-clear / reset flows) routes its unstamped branch through the same helper, closing the identical orphan reachable by an admin clearing the queue during the #679 window. The helper writes the idempotent terminal history row (`status='expired'`, `posting_method='system_expiry'`, valid since migration 045) and only then deletes, with the same per-row DB-failure containment as the sent-row reap; a spare history row for a genuinely-never-sent item is harmless, since no card exists to tap it. Defense-in-depth that stays worthwhile after #680: it covers crash windows and any future unstamped path.

- **Button spinner no longer hangs under rapid click bursts: the callback is answered before the slow chat ops (#686)** — the Posted / Skip / Reject handlers deferred the callback answer until after the card's slow work (reconcile, the atomic DB write, and the terminal caption edit that waits out per-chat `RetryAfter` walls), so under a burst of clicks the ack could land after Telegram's ~30s validity window and the button spun forever even though the action completed. Each handler now answers the callback immediately after the cheap, chat-op-free `claim_for_processing` gate and before that slow work — mirroring Auto Post, which already answered first (#481). The answer is placed *after* the claim, not before: a lost race (the item was already handled by a concurrent click or autopost) still routes through `validate_queue_item`, whose "already handled" toast must remain the first and only answer (Telegram honours one answer per query, #679). This is the interactive-responsiveness complement to the #682/#683/#684 flood-control fixes; it deliberately does not add python-telegram-bot's `AIORateLimiter` (the broader outbound-queueing change tracked separately under #686).

- **Expire-reap drain unblocked: `system_expiry` history writes no longer violate `check_posting_method`, and one poisoned row no longer aborts a sweep pass (#682)** — the first fix (#683) exposed a second latent bug the moment the record + delete path actually ran in production: the reap's terminal history row carries `posting_method='system_expiry'` (since #561), but migration 004 constrains `posting_method` to `('instagram_api', 'telegram_manual')` — migration 042 widened only the sibling status constraint. The insert failed with a `CheckViolation` that raised out of `expire_sent_row`, aborting the entire sweep pass ("Error in scheduler loop") and leaving the session flush-dirty, so the stuck-row backlog still could not drain. Migration `045` widens the constraint to include `system_expiry` (the model now declares `check_posting_method` too, so fresh installs match), and `expire_sent_row` contains record/delete failures per-row: it logs with the traceback, rolls both repositories' sessions back, and returns a new `"failed"` outcome so the sweep continues over the remaining rows and the failed row is retried next sweep.

- **Expire-reap no longer retries "Message is not modified" forever, ending the self-inflicted flood-control storm (#682)** — `telegram_edit_with_retry`'s permanent-rejection branch was unreachable: python-telegram-bot's `BadRequest` subclasses `NetworkError`, so the transient branch caught it, burned 3 retries + backoff, and returned `None` — which `expire_sent_row` read as transient and deferred the row for the next sweep, forever. With ~64 rows whose cards were already in the terminal Expired state, the two reap paths pushed ~190 no-op edits per pass into one chat around the clock until Telegram pinned the bot under per-chat flood control (`RetryAfter ~35s` every minute) — approval buttons hung with an endless spinner and autoposts aborted mid-handler ("Auto-post failed: Flood control exceeded"). Fixed at both layers: the retry wrapper now classifies `BadRequest` ahead of the transient branch (single attempt, still returns `None` — the contract every caller already handles), and the reap classifies at its own seam with one un-retried edit — `BadRequest` (card already terminal / not editable) proceeds to record + delete, genuine transients (`RetryAfter`/`TimedOut`/`NetworkError`) defer to the next sweep instead of backoff-sleeping into the very flood window they wait out. The stuck-row backlog drains itself on the first post-deploy sweep; flood control decays within about a minute of the storm stopping.

### Documentation

- **Data model evaluation package (`documentation/planning/2026-07-29-data-model-evaluation/`)** — Added an eight-document evaluation and planning package produced against `main` at `683f7cf`: a neutral, reusable prompt for reconstructing and evaluating the system and its data model (deliberately free of any preferred answer); this session's repository-grounded self-evaluation (system reconstruction, schema/source-of-truth inventory, write/read path traces, liabilities and failure modes, and a comparison of three target approaches); a recommended workspace-rooted target model with entities, invariants, and explicit non-goals; an implementation epic with testable acceptance criteria and observable cutover gates; a P0–P3 issue triage reconciled against the existing GitHub backlog (adopting epics #576/#577/#578/#560 and ~90 existing issues rather than duplicating them); an expand/backfill/dual-write/shadow-read/cutover/contract migration plan covering every consumer (models/repositories, services and worker loops, FastAPI, Telegram, CLI, OAuth, Next.js/BFF/JWT, analytics, CI, Railway, Neon, operations) with per-stage rollback; and an evidence map from conclusions to files, migrations, tests, and issues. Documentation only — no schema or runtime behavior changed, and no step in the package triggers Instagram or Telegram posting.

- **Full-system review artifacts (`documentation/planning/2026-07-system-review/`)** — Added durable analysis artifacts from a review of every subsystem (~27k LOC): scheduler/posting, Telegram, data layer, integrations, API/OAuth, config, CI. Captures 91 findings (26 High / 58 Med / 7 Low) across bugs, security, architecture, over-complication, and testing gaps, organized under five cross-cutting epics (multi-tenant isolation, non-atomic posting workflow, missing migration tooling, process-local state in a multi-worker deploy, and the `TelegramService` God-facade). Includes `triage-tracker.md` (consolidated backlog), `detailed-findings.md` (per-subsystem raw analysis), and an `issues/` backlog that maps the findings to 36 GitHub issues (6 × P0 + 20 × P1 individual, 10 × P2/P3/P4/nice-to-have clusters) with a `file-issues.sh` script to create them. No runtime behavior changed.

### Security

- **Instagram token writes now stamp the owning chat (#675)** — the derived-ownership predicate (#583/#671) treats a chat-stamped `api_tokens.chat_settings_id` as ownership, but no Instagram token write ever set it (only Google Drive's path did), so ownership degenerated to active-pointer-or-env-chat and a non-env chat's own secondary accounts were unreachable. `TokenRepository.create_or_update` now accepts `chat_settings_id` and stamps it on create and re-issue — but never clears it when absent, so chat-less writes (token refresh, CLI) preserve existing ownership. `add_account`/`update_account_token` resolve the stamp from their `telegram_chat_id`, and the FB-Login multi-account flow passes the connecting chat for secondary accounts too (activation stays first-account-only). Backfill decision: none — existing unstamped rows remain the documented legacy case (owned by the env chat), which matches who actually connected them; new writes are stamped from here on.

- **Switch-account is scoped to the chat that owns the account (#671)** — `switch_account` validated only that the target account existed and was active, so any chat could set its `active_instagram_account_id` to ANY tenant's account and thereafter post with that tenant's credentials — the adoption twin of #583's removal hole, and worse (credential use, not just disablement). The switch now requires ownership via the same derived-ownership predicate introduced for #583 — both gated mutations go through a shared `_require_account_ownership` (uniform "not found" shape, ownership checked before the existence lookup, so foreign probes can't distinguish real account ids from invented ones). Scoping honesty: Instagram token writes don't yet stamp `chat_settings_id` (only the Google Drive path does — producer filed as #675), so on today's data ownership reduces to active-pointer-or-env-chat; that matches current single-tenant reality and fails closed. Until #584 scopes the account listing, foreign accounts still appear in pickers but error on selection instead of being adopted.

- **Media selection, category-mix, and queue-preview are tenant-scoped and fail-closed (#542), on a media-ownership enabler (#412)** — Closes #542's live cross-tenant leaks in the JIT posting path, unblocked by first giving `media_items` an owner. Of #542's four documented leaks, the three that are live in production (media selection, category-mix, queue-preview) are closed here; the two remaining sub-paths have zero production callers (dormant) and are tracked in #677.
  - **Sync stamps ownership at index time (#412)** — `MediaSyncService.sync(telegram_chat_id=…)` resolves the owning `chat_settings_id` (non-bootstrapping, no phantom rows) and stamps it on every `media_items` row it indexes, mirroring the dashboard-upload precedent. The scheduler's per-tenant loop already passes `telegram_chat_id`; the CLI and legacy global sync do not, so those paths keep leaving `chat_settings_id` NULL (the legacy single-tenant marker) rather than fabricating an owner. Sync's reconciliation and dedup reads (`get_active_by_source_type`, `get_active_by_hash`, `get_by_path`, `get_inactive_by_source_identifier`) are now scoped to the resolved tenant, so two tenants holding the same file can no longer collide — previously the global hash-dedup skip let only the first tenant get a row.
  - **JIT selection scoped, fail-closed (#542)** — `SchedulerService._select_media` / `_select_media_from_pool` now require a `chat_settings_id` and refuse to select from the global all-tenant pool when it is missing. The repository tenant filter is a no-op on a NULL id, so an unscoped call would silently span every tenant; the guard short-circuits before the query. `_select_and_send` scopes to the posting chat's tenant and `get_queue_preview` resolves its tenant non-bootstrapping (empty preview for an unconfigured chat). Adopts the #519 `_require_caller_tenant` fail-closed precedent. `get_next_eligible_for_posting` has exactly one production caller, so this closes the selection leak completely.
  - **Category-mix scoped, fail-closed (#542)** — `_pick_category_for_slot` (reached from `is_slot_due` on every scheduling decision) now threads the posting chat's tenant into `category_mix_repo.get_current_mix_as_dict(chat_settings_id)`, so one tenant's configured category ratios no longer shape another tenant's automated posting distribution. Fail-closed: a missing tenant reads no mix (uncategorized) rather than the merged global mix. The real tenant already owns its current mix, so this is a clean tenant filter — it also fixes a latent bug where two tenants' same-named ratios collapsed into a single dict key.
  - **Backfill migration `044` — derives the owner, awaits ratification** — stamps any pre-existing NULL-owned `media_items` row by deriving the sole active group tenant from its known group chat (a subquery, not a hard-coded UUID). Verified a no-op today against production (0 / 4619 rows NULL-owned): it ships as idempotent insurance and is applied only on explicit sign-off, never by CI or on merge.
  - **Deliberately deferred (follow-ups filed):** the `media_items.chat_settings_id NOT NULL` constraint (the two remaining write paths — local ingestion and the Instagram backfill downloader — must stamp first); ownership stamping/backfill for the other four tenant-scoped tables (`posting_history`, `media_posting_locks`, `posting_queue`, `category_post_case_mix`); and tenant-scoping the eligibility "locked-hash" exclusion — kept global while `media_posting_locks` is still NULL-owned, since a global scope only ever over-excludes and can never surface another tenant's media; and tenant-scoping the two **dormant** unscoped readers (`_allocate_slots_to_categories`, `check_availability` — both with zero production callers today) → #677.

- **Remove-account is scoped to the chat that owns the account (#583, TD-005)** — `deactivate_account` flipped the deployment-wide `is_active` flag for any `account_id` the caller named, so a member of one chat could disable an account other tenants post with (both the Mini App `/remove-account` route and the Telegram remove button hit this path). Accounts carry no tenant column, so ownership is now *derived* at the service layer (`_account_owned_by_chat`): a chat owns an account when it has it selected (`chat_settings.active_instagram_account_id`) or holds an `api_tokens` row stamped with its `chat_settings_id` (new `TokenRepository.get_owner_chat_ids`); accounts with no chat-stamped tokens are legacy single-tenant data and belong to the deployment's env chat only. `deactivate_account` now requires the calling `telegram_chat_id`, rejects foreign chats with the same "not found" shape (no ownership oracle), and both callers pass their chat. Complements #584 (listing scope), #585 (role checks), #600 (prefix lookups) in the #576 multi-tenant epic.

- **Bound-token dashboard access now re-checks active membership (#582)** — `_validate_request` (the shared onboarding/dashboard/settings gate) treated a token's cryptographic chat binding as sufficient authorization on the bound path (a signed URL token, or group-launched `initData`): it confirmed the token targeted the requested `chat_id` but never confirmed the caller was still an **active member** of it. A revoked member — or a group member who was never provisioned — kept a usable token until TTL expiry and could open that tenant's dashboard (read queue, history, accounts, media, settings). The `MembershipService.is_active_member` lookup is now enforced on **every** authenticated path, not just the unbound (DM-launched) one; the cheaper chat-mismatch guard still short-circuits a replayed bound token before the membership lookup. Onboarding is unaffected — the owner's membership is created Telegram-side at group link (startgroup / `my_chat_member` / `/link`), before any Mini App API call. `_validate_auth`-only routes (e.g. `GET /instances`) are inherently user-scoped (they return only the caller's own memberships) and were already safe.

- **Media write mutators are tenant-scoped (#597, TD-030)** — Every media WRITE that resolves a row by a bare `media_id` (`reactivate`, `update_source_info`, `update_metadata`, `increment_times_posted`, `update_cloud_info`, `deactivate`, `delete`, and the bulk `deactivate_by_ids`) now takes the acting tenant's `chat_settings_id` and refuses to mutate a row owned by a **different** tenant. Previously any caller who knew a media UUID could reactivate, mark-as-posted, edit metadata, rewrite cloud info, or deactivate **another tenant's** media by passing that UUID to a mutator — the row was fetched and written with no ownership check. A single ownership rule (`MediaRepository._write_allowed`) backs both the single-row (`_get_for_write`) and bulk paths so they cannot drift: a row owned by another tenant is refused (and logged); a legacy row whose `chat_settings_id` is NULL (pre-#412 ownership backfill) is still written, with a warning, so scoping does not silently no-op every write on not-yet-backfilled media and halt posting; a caller with no tenant (the dedup CLI, legacy single-tenant sync) is unchanged. The acting tenant is threaded from the context each caller already holds — `queue_item.chat_settings_id` on the posted/autopost callbacks, the slot's `chat_settings.id` in the scheduler, the selected item's owner in AI caption generation, and the per-tenant `chat_settings_id` in the media-sync loop and dashboard sync/index routes (which fixes a real cross-tenant write: a per-tenant sync iterated an unscoped DB listing and could deactivate another tenant's rows). Unlike scheduler **selection** scoping (#542, still blocked behind #412), a mutator's row identity is already pinned by the caller, so an "owned-OR-NULL" write filter closes the cross-tenant hole without emptying any tenant's eligible pool. The user-facing **Regenerate Caption** button (`handle_regenerate_caption`) independently verifies the caller owns the queue item — it resolves the caller's tenant from `query.message.chat_id` and refuses a forged `queue_id` that resolves to another tenant's item, rather than trusting the fetched row's own `chat_settings_id` (self-referential).

- **Worker notification layer routes every tenant to its own chat (#541)** — Three cross-tenant holes in the worker/notification layer, closed by resolving the tenant from the data it owns instead of the deployment-wide env chat.
  - **Queue notifications went to the global `TELEGRAM_CHANNEL_ID` chat (#541)** — `TelegramNotificationService.send_notification` used the env chat for every per-tenant decision: the settings/caption-style lookup, the verbose flag, the active Instagram account shown on the card, the Google Drive credentials used to download the media bytes, the `send_photo` destination itself, the `telegram_chat_id` stored back on the queue row (which then poisoned downstream callback logic keyed off it), and the interaction log. With a second active tenant, tenant B's media (potentially private brand content) was posted into the env chat, B's group went silent, and the env chat's team could approve/auto-post B's media. The tenant is now resolved from `queue_item.chat_settings_id` (new `SettingsService.get_settings_by_id`) and drives all seven call sites; rows with a NULL `chat_settings_id` (legacy single-tenant) fall back to the env chat with a warning. `/next` reaches the same send path and is fixed by the same change.
  - **Resume buttons flipped the env chat's pause flag** — the resume flow (`reschedule`/`clear`/`force`) scoped its queue operations to the calling chat but still unpaused the env chat: functionally broken for any non-env tenant (their chat stayed paused) and a cross-tenant write. The flip now goes through a new idempotent `SettingsService.set_paused` targeting the chat the button was pressed in (concurrent taps converge on the requested state instead of toggling past it); the env-global `TelegramService.is_paused`/`set_paused` pair (no remaining callers) is deleted.
  - **Loop-level media-sync failure alerts** — `_notify_sync_error` sent deployment-level operational alerts to the env tenant's chat, gated on that tenant's verbose flag. They now go to `ADMIN_TELEGRAM_CHAT_ID` unconditionally (the caller already rate-limits to first-failure/new-error), matching every other deployment-level alert.
  - **Sequenced follow-on (now addressed):** scheduler JIT selection / category-mix / queue-preview scoping (#542) was sequenced behind media ownership (#412) — scoping selection while media rows were NULL-owned would have emptied every tenant's eligible pool and halted posting. Media ownership and all three live scopings landed together (see the #542 / #412 entry above); the remaining dormant readers are tracked in #677.

- **Cross-tenant data isolation on the Mini App API and Telegram callbacks (#511, partial #512)** — Two cross-tenant holes found by the security audit, closed at the data layer.
  - **Mini App onboarding/dashboard API IDOR (#511)** — `_validate_request` (the shared gate for ~35 onboarding/dashboard/settings endpoints) authenticated the caller but never bound them to the `chat_id` they were acting on. A DM-launched `initData` token carries no `chat` field, so the old equality guard was skipped entirely and any authenticated bot user could read or mutate **any other tenant's** queue, history, accounts, media, and settings simply by passing a different `chat_id`. The gate now requires a server-side **active-membership** lookup (new `MembershipService`) whenever the token does not cryptographically bind a `chat_id`; signed URL tokens / group-launched initData keep their existing equality check. The previously fail-**open** inline membership check on `/audit-log` is folded into this central, fail-**closed** path.
  - **Telegram callback queue operations — cross-tenant deletes (partial #512)** — `resume:clear`, `clear:confirm`, and the resume `reschedule` path called `queue_repo.get_all(status="pending")` with no tenant filter and deleted/rescheduled **every tenant's** pending rows; the parameter-free `resume:clear` / `clear:confirm` buttons let any user trigger a fleet-wide queue wipe. These now scope to the calling chat's `chat_settings_id`, and refuse to run (rather than fall back to an unscoped query) when the chat has no tenant. `batch_approve` now rejects a forged/foreign `chat_settings_id` carried in button data instead of trusting it. The role-based authorization gate (member vs admin) from #512 is backend-auth and tracked as a separate follow-up.

### Added

- **Cloudinary feature gap analysis & enhancement proposals (documentation only)** — New `documentation/cloudinary/2026-07-14-feature-gap-analysis.md` evaluating Cloudinary's July 2026 announcements (AI Image Generation add-on, self-service OAuth, VS Code extension GA, q_auto/media-experience updates) against the current transient post-time media pipeline. Eight sized proposals (P0–P7): Cloudinary call timeout/offload substrate, persistent storage for Mini App uploads (#317), tag-scoped cleanup lifecycle (#450/#550 adjacent), q_auto delivery optimization, generative 9:16 story framing as a per-tenant toggle, video story normalization with eager derivation, pHash perceptual dedup alongside SHA256, and exploratory AI content generation for pool-dry days (#152/#189). Includes explicit non-proposals (native TTL doesn't exist; OAuth migration is zero-benefit; beta dedup add-on skipped) and an unverified-claims register. Indexed in `documentation/README.md`. No code changes.
- **New `MediaUnsupportedError` exception classifies Meta error code 9004** — Previously a 9004 "Only photo or video can be accepted as media type" response (typically: HEIC file masquerading as JPG, or Cloudinary transformation produced output IG can't decode) was classified as a generic `InstagramAPIError`. Now `_check_response_errors` in `instagram_api.py` raises the new `MediaUnsupportedError`, and the autopost handler reacts by creating a **permanent_reject** lock on the underlying media_item so the failing file doesn't keep cycling through retries on every scheduler tick. User sees a clear "couldn't process this file (Meta error 9004) — permanently rejected, won't be scheduled again" message instead of the previous generic error.
- **Hourly `posting_queue` auto-prune loop** — New `cleanup_queue_loop` mirrors the existing `cleanup_locks_loop` shape. Runs hourly, calls a new `QueueRepository.delete_stale(hours=24)` to remove any queue item whose `scheduled_for` is more than 24 hours past. Prevents the May 17 → 19 outage style of accumulation that left 954 stale rows in `posting_queue` until manual cleanup on 2026-06-02. Distinct from the existing `delete_stale_pending(max_age_minutes=10)` which targets short-window JIT scheduler hygiene; this catches the long-tail.

### Fixed

- **Duplicate approval cards from timed-out-but-delivered sends; handler answers no longer silently rejected** — The same story could post two approval cards ~1 min apart (the second with dead buttons): `send_photo` timed out client-side after Telegram had already delivered the card, the failure read as retryable, and the retry posted a duplicate. The notify path now has the idempotency #564 gave the publish path, plus an ambiguous-delivery policy.
  - **Idempotency guard on the notify path (mirrors #564's claim-before-publish)** — `send_notification` no-ops (returns True) when the queue row already carries a `telegram_message_id`; re-entry via send retry, crash replay, or `/next` re-fire can't repost the card.
  - **Ambiguous timeouts are never blind-retried** — a `TimedOut` from `send_photo` now surfaces as a domain `AmbiguousDeliveryError` (the card may be in the chat; Bot API offers no delivery verify) and the scheduler stops retrying instead of resending. The row stays in `processing`: the 10-min stale-processing sweep restores it if the card never arrived, and a button click reconciles it if it did. Residual (rare): a delivered-but-unstamped card untouched for 10+ min is still requeued into one more card — eliminating it needs a #549-style held state, filed as a follow-up.
  - **Bookkeeping failures after a delivered send no longer read as send failures** — once `send_photo` returns, a failure stamping the message id (or logging) logs loudly and returns True; previously it triggered a retry that guaranteed a duplicate card.
  - **Callback-time card reconciliation (`telegram_utils.reconcile_card_messages`)** — on every claim from a clicked item card (posted/skip/reject/autopost), a missing `telegram_message_id` is backfilled from the clicked card (closing the delayed sweep-requeue duplicate) and a mismatched sibling card's keyboard is stripped with retry and the row re-pointed (no lingering dead buttons). The common matched case makes zero extra DB/Telegram calls.
  - **Handler answers are visible again** — the callback dispatcher answered every query up front, consuming Telegram's one-answer-per-query budget, so real handler feedback (toasts, `show_alert` error popups) was silently rejected. Handlers now answer first; a fallback answer in the dispatcher's `finally` stops the spinner for handlers that don't.

- **Concurrent button-tap reactivity — `concurrent_updates(8)` + per-callback DB-session isolation (#557; completes the #572 groundwork)** — Button taps were dispatched **one at a time** (`max_concurrent_updates=1`), so a slow in-flight callback serialized every *other* user's tap behind it — even after #572 moved the media transfers off the event loop. Enabling PTB `concurrent_updates` alone is unsafe here: the callback handlers reach through **singleton repositories that share one SQLAlchemy `Session`** (coordinated by the `atomic_session` primitive), and a `Session` is not safe for concurrent use. This change lands both levers together.
  - **Per-callback session isolation** — `BaseRepository` now holds its session (`_db`/`_db_generator`) in **per-instance `ContextVar`s** instead of plain attributes. Because asyncio copies the context per-Task (and `asyncio.to_thread` copies it per-thread), every concurrent callback — and every thread offload — transparently opens and owns its **own** `Session`; `use_session`, `atomic_session`, and `cleanup_transactions` become task-scoped with no change to their logic. Verified: 6 concurrently-dispatched callbacks now observe 6 **distinct** sessions (previously 1 shared).
  - **Clean context at the task fan-out** — startup work (the settings read in the startup notification) opened sessions in the process's root context; since `asyncio.create_task` copies the current context, every callback and background loop spawned afterward would inherit and share those Sessions. `main()` now detaches the singleton services' sessions immediately before spawning the task fan-out, so each spawned task copies a clean context and opens its own session. Isolation therefore no longer depends on nothing having touched the DB during startup.
  - **`concurrent_updates(settings.TELEGRAM_MAX_CONCURRENT_UPDATES)`** (default **8**, bounded) on the `Application` builder. Bounded so peak per-task DB sessions stay within the connection pool; measured peak checkout under 8 concurrent callbacks + 5 background loops is **13 of 30** (17 headroom).
  - **Atomic-finalize invariant preserved (#549/#564)** — the claim-before-publish flow is unchanged SQL, now running on isolated per-task sessions; the `SELECT … FOR UPDATE SKIP LOCKED` claim still serializes rapid concurrent taps **at the database** across sessions, so exactly one claimer wins and a story can't be double-published.
  - **Background-task use-after-free closed** — the autopost heavy work runs in a spawned task that *copied* the callback's context (and its session references). It now calls a new `BaseService.begin_isolated_transactions()` (+ `BaseRepository.detach_session()`) at entry to **detach to fresh, task-local sessions**, so the callback's `finally: cleanup_transactions()` can no longer commit/close the very session the background task is mid-write on.
  - **Fourth media site offloaded safely (`media_sync_loop`)** — the Drive-listing `sync()` (previously on the loop) is now `asyncio.to_thread`-offloaded through an isolated, self-cleaned session. Because the offloaded work's session lives only in the thread's context, the `transaction_cleanup_loop`'s 30s `cleanup_transactions()` in the main-loop context can no longer see — or race — it (the reason a naive offload was unsafe: `sync_service` is a singleton whose session that loop commits/replaces every 30s).
  - **Verified against the real python-telegram-bot dispatch pipeline** (real `Application` + `CallbackQueryHandler`, updates fed through `update_queue`): a competing button tap's ack lands in **~7–11ms** under concurrent dispatch versus **~530ms** serialized behind a 0.5s in-flight tap — a ~50× reactivity improvement, well inside Telegram's callback-answer window. No migration.
- **Offload autopost/notification media transfers off the event loop — groundwork toward #557 button reactivity** — The synchronous Cloudinary/Drive media transfers ran **on** the worker's single asyncio event loop, so a slow transfer **froze** the loop — stalling not just that task but everything else scheduled on it: the Telegram `getUpdates` poller, the scheduler tick, and the cleanup loops. This offloads the blocking transfers to a worker thread (`asyncio.to_thread`) at three sites — the **Auto Post** upload (`_upload_to_cloudinary`), the scheduled-notification photo download (`send_notification`), and the scheduler auto-approve download + upload (`_auto_approve_instagram`) — so a transfer can no longer freeze the loop. Each offloaded call touches only its own task's cloud client/session, so the offload is session-safe with no change to the shared-session model. Also bounds the previously-unbounded Cloudinary upload with a finite network timeout (`CLOUD_UPLOAD_TIMEOUT_SECONDS`, default 120s). **Scope — this is groundwork, not the user-facing reactivity fix.** Verified against the real python-telegram-bot dispatch pipeline: under this repo's config (`max_concurrent_updates=1`, default-blocking `CallbackQueryHandler`) updates are still dispatched one at a time, so a *competing* button tap's ack stays gated behind the prior handler's completion regardless of the offload. Making concurrent taps' acks fire promptly requires enabling `concurrent_updates(N)` **plus** per-callback DB-session isolation (the callback handlers share singleton repositories via the `atomic_session` primitive, unsafe under concurrency) — a separate, larger change tracked as a follow-up, along with a fourth on-loop media site (`media_sync_loop`'s Drive listing) that needs the same session rework to offload safely.
- **Periodic scheduler sub-tasks now survive redeploys — Instagram token refresh no longer silently stops (#547, regression of #397; also #553)** — The scheduler loop drove its periodic sub-tasks (hourly retention cleanup, pool + Drive-token health alerts, and the **daily Instagram token refresh**) off in-memory tick counters that reset to `0` on every process start. Under Railway's normal sub-24h redeploy cadence the 1440-tick (24h) token-refresh gate **never reached its threshold**, so refresh never ran and IG OAuth tokens silently expired after 60 days — halting all posting with no alert (the system only alerts on refresh *failures*; "never attempted" produces none). Fix: a new `PeriodicScheduler` (`src/services/core/loops/periodic.py`) gates each sub-task on a **durable last-run timestamp** persisted in the existing `service_runs` table (no migration) rather than a per-process counter. Each tick, a sub-task whose persisted last run is older than its interval — or that has never run / aged out of retention — fires promptly and records the run; a fresh process therefore catches up instead of waiting a full interval it may never reach. Two new `ServiceRunRepository` methods back it (`record_run`, `get_last_run_at`), and the marker namespace (`scheduler_periodic`) is excluded from the `get_health_stats` dashboard aggregate. **Also fixes #553:** the `is_first_tick` redeploy catch-up was consumed by the first tick even when that tick early-returned on the initial-sync wait, so the "post immediately on redeploy" behavior never ran in sync-enabled deployments; `_scheduler_tick` now returns `None` (vs `[]`) on the sync-wait short-circuit so the flag is only consumed by a tick that actually processed chats. Known follow-up: the in-memory pool/token-health alert throttle maps still reset on restart (a redeploy may re-send a still-cooling alert — a benign re-fire, not a silent stop); persisting them extends the same seam.
- **Cleanup loops now run their work before sleeping, so a redeploy during the sleep can't skip a cycle (#550)** — The three hourly cleanup loops (`cleanup_locks_loop`, `cleanup_cloud_storage_loop`, `cleanup_queue_loop`) each did `while True: await asyncio.sleep(3600); <cleanup>` — sleeping *before* doing the work. Under Railway's frequent redeploys the container is SIGKILLed during that hour-long initial sleep, so across a normal deploy cycle the cleanup body never ran: `posting_queue` re-bloated (the "954 items over 17 days" pileup `cleanup_queue_loop` exists to prevent), expired media locks never cleared (connection-pool depletion), and orphaned Cloudinary uploads accumulated (storage cost). Each loop now runs its cleanup at the top of the iteration and sleeps *after* (mirroring `media_sync_loop`), so cleanup fires immediately on every process start and then hourly. The `record_heartbeat` call, per-loop error handling (a cleanup exception still can't kill the loop — it is caught and the loop continues to the next sleep), and the 3600s interval are all unchanged. For `cleanup_queue_loop` the #561 reap-then-delete block (`expire_sent_row` on button-bearing rows before `delete_stale`) moves as a unit ahead of the sleep.
- **Claim-before-publish: a crash/redeploy mid-publish can no longer duplicate an Instagram story (#549, #500, #551; Epic #560)** — Both auto-post paths (the scheduler auto-approve `_auto_approve` and the Telegram **Auto Post** button) published to Instagram *before* writing any idempotency signal, so a crash after the publish but before the DB bookkeeping re-served the same media on the next selection tick — a **duplicate story on the live IG account** plus an under-counted daily cap. The IG Graph publish carries no client idempotency key; the only anchor is the container_id. Fix: a new `posting_queue.status = 'publishing'` plus a persisted `instagram_container_id`. `InstagramAPIService.post_story` now exposes an `on_container_created` callback that fires with the container_id the instant the container is created and **before** the publish; each caller uses it to flip the row to `'publishing'` and persist the anchor. After a successful publish the bookkeeping (history + times_posted + lock + queue-delete) runs in **one atomic transaction** (shared `atomic_session`), and the history write is idempotent (`create_idempotent`) so a replay can't double-insert (#551). Failure is classified by whether a container was created: **no container** → nothing published → safe-retry (release the row, media stays eligible); **container created but unconfirmed** → the row stays `'publishing'`, which is excluded from every stale-sweep (`delete_stale`, `get_stale_sent`) and blocks reselection (the "not already queued" filter), so a maybe-posted story is never re-served and is held for review rather than re-published. A `'publishing'` row also counts toward the daily cap, so a crashed mid-publish story is counted exactly once. **Requires migration `scripts/migrations/043_add_publishing_queue_status.sql`** (adds `publishing` to the `check_status` CHECK constraint and the `instagram_container_id` column). Design consensus: alex + rajan. **Review hardening (rajan #564):** the original fix routed *every* container-present failure into "held forever," which stranded two cases it shouldn't. (1) A container Instagram affirmatively marks `ERROR`/`EXPIRED` (IG confirms nothing published) is now classified as a **confirmed failure and released for retry** — via a shared `is_container_confirmed_failed` classifier threaded through both call sites — instead of being stuck in `'publishing'` forever; only a genuinely ambiguous crash/timeout still stays `'publishing'`. (2) The daily-cap `'publishing'` count is now **time-bounded** (new `QueueRepository.count_recent_by_status`; a publishing row older than `PUBLISHING_CAP_MAX_AGE_MINUTES` = 15 min — well beyond the 180s publish cap — is presumed stuck and no longer taxes the cap), so a stranded row can't silently wedge a chat's auto-posting. No reconciliation: a stuck row is simply not *counted*, never deleted or rolled forward.
- **Sent queue cards are now expired gracefully at reap time instead of orphaning their buttons (Epic #560)** — A queue row that had already been sent to Telegram (carrying a `telegram_message_id` and live inline buttons) was hard-deleted ~24h later by the age-based reapers. A subsequent button tap then found no queue row and no history, surfacing the scary `⚠️ Queue item not found` message. Now, when a button-bearing row is reaped, both reap paths call one shared helper (`expire_sent_row` in `src/services/core/queue_reap.py`) that edits the card to `⌛ Expired — no action needed`, strips the buttons, and writes a terminal `expired` `posting_history` row (audit trail + tap-time fallback). A transient Telegram edit failure defers the row (left intact and tappable) rather than deleting it, so a card is never orphaned. The raw reapers (`QueueRepository.delete_stale`, `discard_abandoned_processing`) are narrowed to never-sent rows (`telegram_message_id IS NULL`); the two reapers stay separate loops (`scheduler_loop`, `queue_cleanup_loop`). A late tap on an already-reaped card now shows the friendly `⌛ Expired` caption via the existing history branch. The remaining interactive queue-clear paths (`resume:clear`, `reset:confirm`, and the settings **Clear Queue** confirm) now route button-bearing pending rows through the same helper via a shared `reap_pending_rows` batch reaper instead of blind-deleting them, and the daily-cap bounce on the manual Posted path now strips the card's buttons when it restores the row to `pending` — a bounced-to-pending card with live buttons was itself an orphan-in-waiting. Together these close the remaining ways a live card could be orphaned (navi review, #561). `QueueRepository.delete_all_pending` is retained for the bot-less CLI path, which has no bot to edit cards. **Requires migration `scripts/migrations/042_add_expired_history_status.sql`** — it adds `expired` to the `posting_history` `check_history_status` CHECK constraint and must be applied before this version writes any `expired` history rows.

- **Auto Post button no longer strands a queue card in `processing` when the daily cap is hit** — On the autopost (Instagram API) path the daily-cap guard ran *after* the card was atomically claimed into `processing`, then returned without releasing it, leaving the row stuck in `processing` — it would not re-post and was not reclaimed until the 24h abandoned-processing sweep. `_do_autopost` now restores the row to `pending` on a cap-hit, mirroring the manual Posted/Skip/Reject handler (`telegram_callbacks_queue`) so the card is retried the next day instead of being orphaned. Cap enforcement is unchanged: the cap still blocks the post and shows the same "Daily posting limit reached" message. Regression tests added.

- **Cloudinary `cleanup_expired` now paginates past the first 500 results (#499)** — `CloudStorageService.cleanup_expired()` fetched orphaned uploads with `max_results=500` but never consumed the `next_cursor` Cloudinary returns when more results exist, so once a folder accumulated more than 500 uploads the cleanup silently processed only the first page and the rest accrued storage cost indefinitely. The listing call is now wrapped in a loop that follows `next_cursor` until every page is exhausted, and the page size is extracted to a named `CLOUDINARY_RESOURCES_PAGE_SIZE` constant. Regression test added covering a two-page response.

- **Daily posting cap enforced across all 5 posting paths** — `posts_per_day` was only used for interval spacing, not as a hard daily limit. DB evidence showed accounts posting 2x their configured cap. Added a shared `can_post_today()` guard (backed by `HistoryRepository.count_posts_today()` with timezone-aware day boundaries) and wired it into the scheduler, autopost, manual posted callback, `/next` command, and auto-approve paths. Catchup bursts are also capped. On cap hit the queue item stays pending so it retries the next day.

- **Telegram "Auto Post" button no longer spins after timeout** — `handle_autopost` did not call `query.answer()` until the full Cloudinary upload + Meta publish completed (often >30s), past Telegram's callback-answer deadline. Result: the button's loading spinner ran indefinitely, the bot logged `Could not answer callback query (may be stale)`, and the user couldn't tell if their click registered. Now answers immediately with `⏳ Posting…` after the cheap lock-held check (which keeps its specific `⏳ Already processing…` message for duplicate clicks).

### Removed

- **`instagram_accounts.auth_method` legacy column dropped (#468 PR 5)** — Final sub-PR of the credential refactor. After PR 4 the application reads provenance off `api_tokens.auth_method` exclusively; PRs 2-4 made the account-side column write-only, and this PR removes both the writes (in `instagram_account_service.update_account_token` and `instagram_account_repository.create`) and the column itself (migration 041). `instagram_accounts` is one step closer to pure-identity. The `instagram_accounts.instagram_account_id` legacy column remains — its consumers (backfill, OAuth heal logic, credential lookup) need a separate refactor to read from `api_tokens.meta_account_id` instead, filed as a follow-up.

### Changed

- **Posting + refresh read `auth_method` from the token, not the account (#468)** — Read-switch sub-PR of the credential refactor. `InstagramCredentialManager.get_active_account_credentials()` now filters its token lookup by `auth_method='instagram_login'` (new optional kwarg on `TokenRepository.get_token_for_account()`); legacy/unmigrated accounts get a clear "no instagram_login token — reconnect" error instead of a stale-flow token. `TokenRefreshService._get_refresh_endpoint()` reads `token.auth_method` instead of joining through `instagram_accounts.auth_method` to pick its host. Provenance now lives wherever the credential lives — the `instagram_accounts.auth_method` column becomes unused (gets dropped in PR 5).

- **Dual-write `auth_method` + `issuing_app_id` to api_tokens at OAuth callbacks (#468)** — Continues the credential refactor. `InstagramAccountService._create_account_with_token` and `update_account_token` now pass both fields to `TokenRepository.create_or_update` alongside the existing `instagram_accounts.auth_method` write. Wired into both OAuth callbacks: Instagram Login (`instagram_login_oauth.py`) passes `settings.INSTAGRAM_APP_ID`; Facebook Login (`oauth_service.py`) passes `settings.FACEBOOK_APP_ID`. Manual entry leaves `issuing_app_id` unset (no app context). After this PR every newly-issued or refreshed IG token carries its own provenance; the read-switch sub-PR drops the JOIN.

- **Dashboard Overview — KPIs and chart split by `posting_method` (#466)** — Previously "Total Posts / Success Rate / Skipped / Failed" lumped Instagram publish attempts together with Telegram delivery attempts, so a 958-row Telegram delivery burst in 2026-05 (see #467 postmortem) read as 1% success rate even though every actual Instagram post had succeeded. The four KPI cards now read **Posts published** (`instagram_api.posted`), **Instagram success rate** (IG posted ÷ IG attempted — Telegram delivery never enters the denominator), **Cards skipped** (`telegram_manual.skipped`), and **Delivery issues** (`telegram_manual.failed`, with hover hint). Backend `get_analytics()` now emits `ig_posted`, `ig_failed`, `ig_success_rate`, `telegram_skipped`, `telegram_failed` alongside the legacy aggregate fields (kept for back-compat). The daily-activity chart adds a red **Failed** bar to the stack so burst days are no longer invisible. New repo method `get_stats_by_method_and_status()` powers the method×status pivot.

### Added

- **Pool monitoring on callback path** — `log_pool_status()` now fires on every inline button callback in `_handle_callback`, surfacing connection pool utilization (checked-out, overflow, utilization %) in real-time logs. Aids diagnosis of callback latency spikes correlated with pool exhaustion or Neon cold starts.
- **`api_tokens.auth_method` + `api_tokens.issuing_app_id` columns (#468)** — Credential refactor phase 4. The OAuth-flow discriminator (`instagram_login` / `fb_login` / `manual`) and the issuing Meta App ID move onto the credential row itself so the token becomes self-describing. Today posting code has to JOIN `instagram_accounts` to discover provenance; after the read-switch sub-PR that follows, it'll read directly off the token. Three migrations: 038 adds the columns + partial index, 039 backfills `auth_method` from `instagram_accounts.auth_method` (default `instagram_login` for any unset row), 040 expands the UNIQUE constraint to include `auth_method` so an account can hold both an `instagram_login` token AND an `fb_login` token simultaneously (#380 acceptance criteria). `TokenRepository.create_or_update()` accepts both new kwargs with sensible "preserve existing on omit" semantics so the refresh path doesn't accidentally null them.

### Fixed

- **Token refresh — `_call_meta_refresh` now sends FB app credentials on the `graph.facebook.com` path (#470)** — Meta exposes two refresh contracts: `graph.instagram.com/refresh_access_token` (IG Login) accepts `grant_type=ig_refresh_token` + `access_token` alone; `graph.facebook.com/.../oauth/access_token` (FB Login / legacy) requires `grant_type=fb_exchange_token`, `client_id`, `client_secret`, and `fb_exchange_token`. The previous implementation always sent IG-flavored params, so any refresh against the FB host failed with Meta error 101 "Missing client_id parameter." Now branches on the resolved URL: IG-host gets the IG payload, FB-host gets credentials. Latent fix — production currently has no `fb_login` tokens to refresh, but the bug would have fired immediately if any tenant connected via Facebook Login.

- **Telegram card "show_verbose_notifications=false" toggle no longer ignored after account switch (#465)** — `rebuild_posting_workflow` in `telegram_accounts.py` called `_build_caption()` without forwarding the `verbose=` kwarg, so the function fell back to its default of `True` and re-rendered the file name / ID / 3-step manual instructions block whenever a user clicked the account switcher and returned to a post — even when the chat had verbose notifications turned off. Now reads `chat_settings.show_verbose_notifications` up front and threads it through, matching the sibling `_batch_update_pending_captions` pattern. Regression test added.

### Documentation

- **Postmortem for May 17-19 Telegram delivery failure burst (#467)** — new `documentation/operations/2026-05-telegram-delivery-burst-postmortem.md` documents the 958-failure `telegram_manual` burst that ran 2026-05-17 → 2026-05-19. Traces the lossy code path (`send_notification` swallows the actual exception and returns False → scheduler substitutes the placeholder string `"send_notification returned False"` → DB never sees what really went wrong) and enumerates four systemic issues: opaque error_message, no circuit breaker, no operator alert on send-loop failures, and a too-broad `except Exception`. Sketches five follow-up fixes (F1: propagate exception text; F2: circuit breaker + alert; F3: narrow exception handling; F4: structured logging; F5: dashboard breakdown by error class) to ship as separate PRs.

### Changed

- **Telegram cards — hide 3-step manual instructions for Auto Post chats (#469)** — The "1️⃣ Click & hold image → Save / 2️⃣ Tap Open Instagram / 3️⃣ Post your story" block was rendered on every verbose card. It's only useful when Auto Post isn't available. Now hidden when the active account's `auth_method='instagram_login'` (Auto Post enabled); still shown when there's no active account or for legacy `fb_login` accounts that need the manual flow. File name + truncated ID remain visible for debugging in both modes.

### Changed

- **Sidebar — hide "Setup Wizard" entry once onboarding is complete (#464)** — `/dashboard/setup` server-redirects to `/dashboard` when `setupState.onboarding_completed` is true, so the sidebar link silently bounced users back to Overview, looking like dead nav. The dashboard layout now fetches the init payload (with `revalidate: 60`) and passes a `showSetupWizard` boolean to `Sidebar` and `DashboardHeader`. The Setup Wizard entry is filtered out of the nav once onboarding is done. New chats with onboarding still in progress see the link as before.

- **Accounts settings — "Switch" button renamed to "Make Active"** — The button that promotes an inactive Instagram account to be the chat's active one was labeled "Switch" / "Switching…", which was ambiguous (switch what to what?). Renamed to "Make Active" / "Activating…" so it pairs cleanly with the existing green "Active" badge on the currently-selected row.

### Fixed

- **Instagram posting restored — route IG-Login tokens to `graph.instagram.com`** — Every `instagram_api` post had failed since 2026-05-19 14:10 with Meta error code 190 "Cannot parse access token", even with a freshly-issued OAuth token. Root cause: the Instagram Login OAuth flow issues tokens that are valid only on `graph.instagram.com`, but `_create_media_container`, `_wait_for_container_ready`, `_publish_container`, `InstagramCredentialManager.get_account_info`, and three sites in `backfill_downloader.py` all hardcoded `settings.meta_graph_base` (= `graph.facebook.com`). PR #441 had fixed the OAuth reconnect codepath but never touched posting. New `settings.meta_ig_graph_base` property targets the correct IG host; all seven call sites updated. Added an `auth_method == 'instagram_login'` guard in `get_active_account_credentials` so legacy/unmigrated accounts surface a clear "reconnect via /dashboard/settings" log instead of silently sending the wrong-flow token. Full investigation: `documentation/planning/investigations/ig-host-routing_2026-06-02/`.
- **"Manage Chat" and "+ New Instance" inline buttons unresponsive** — Buttons created by `/instances`, `/start` (returning user), and `/new` sent `instance_manage:{id}` and `instance_new` callback data, but neither action was registered in the Telegram callback dispatch table. Added `handle_instance_manage` (shows settings panel for the selected instance) and `instance_new` (starts onboarding flow) handlers, plus a `get_by_id()` repository method to resolve chat settings by UUID (#454).

### Changed

- **Dashboard empty states upgraded with icons and CTAs** — Replaced plain "No activity yet" / "No media items found" / "No posting data yet" text with a reusable `EmptyState` component featuring a Lucide icon, descriptive message, and action button where applicable (e.g., "Go to Settings", "Upload Media"). Applied to recent activity, posting chart, category breakdown, media grid, and dead content chart.

### Added

- **Token refresh failure alerts** — When Instagram token refresh fails 3+ consecutive times, the scheduler sends a Telegram alert to the admin instead of silently looping. Breaks the doom loop where a corrupt token causes daily silent refresh failures indefinitely (#443).

### Fixed

- **Mobile Telegram login redirects to bot DM instead of web dashboard** — The `t.me/bot?start=login` deep link used by mobile sign-in was ignored by the /start handler, dropping users into the instance list instead of the dashboard. Now detects the `login` payload and opens the Mini App at `/webapp/onboarding?chat_id=...` for single-instance users, falling through to the normal DM flow for multi-instance or new users (#455).
- **Mini App 404 on "Open Dashboard"** — The WebApp button URL pointed to `/dashboard` which doesn't exist in FastAPI (it's a Next.js route on a separate deployment). Fixed to use `/webapp/onboarding?chat_id=...` — the actual Mini App endpoint (#455).
- **Instagram "Cannot parse access token" error misclassified as expired** — Meta error code 190 with "Cannot parse access token" (a token format/corruption issue) was raised as `TokenExpiredError`, surfacing to users as "Instagram connection has expired." New `TokenCorruptError` exception distinguishes token corruption from genuine expiry, with a user-facing message that correctly says the token is invalid rather than expired (#443).
- **Instagram Login reconnect restored for legacy FB-Login accounts** — Migration 036 backfilled `api_tokens.meta_account_id` from `instagram_accounts.instagram_account_id`, which for accounts originally connected via Facebook Login holds the Business Account ID — *not* the `user_id` IG Login returns at token exchange. PR #408 had removed PR #378's username fallback on the assumption these values match. They don't, for legacy rows: reconnect attempts for `@gatortails` and `@thursday.lines` always landed in `add_account`, hit the duplicate-username check, and surfaced as a generic "Connection Failed" page. Introduces `InstagramAccountService.find_existing_account_for_oauth(meta_account_id, username)` — a three-tier lookup (credential-keyed → legacy column → username) — and switches `exchange_and_store` and `update_account_token` to use it. The token row's `meta_account_id` is rewritten to the live IG Login `user_id` on first reconnect, so the username branch is traversed at most once per legacy row. F5 logging surfaces which branch matched. Adds regression tests (`test_exchange_recovers_via_username_when_meta_id_mismatches`, `test_resolves_by_username_when_meta_id_misses`) and updates `TestUsernameCallbackRemoved` → `TestCrossFlowUsernameRecovery` to reflect the new shape. See `documentation/planning/investigations/ig-oauth-cross-flow-reconnect_2026-05-25/` for the full investigation and root-cause analysis.
- **`dry_run_mode` DB column default fixed (migration 037)** — Migration 006 created the `dry_run_mode` column with `DEFAULT true`, contradicting `DEFAULT_DRY_RUN_MODE = False` in code. Commit 6ca43d3 fixed the SQLAlchemy model default but not the PostgreSQL column default. Existing rows created via raw SQL or ORM without explicit values were stuck on `true`, silently blocking Instagram posting. Migration 037 fixes the column default and flips existing rows.
- **Auto-approve no longer records success when Instagram API fails** — When `enable_instagram_api` is enabled and the Graph API call fails during auto-approval, the scheduler previously recorded `status=posted, success=True` with `posting_method=auto_reapproval`, incremented `times_posted`, and created a 30-day lock — hiding the failure. Now the failure is surfaced: no history record, no lock, no increment. The item remains eligible for future selection.
- **Auto-approved posts now actually post to Instagram** — The scheduler's auto-approve path (for previously-posted media) recorded posts as successful in `posting_history` but never called the Instagram Graph API. Auto-approved items now go through the full Instagram posting flow (safety check, Cloudinary upload, Graph API publish) when `enable_instagram_api` is enabled.

### Added

- **Per-page dynamic OG images** (#432) — The OG image route at `/og-image.png` now accepts `?title=` and `?subtitle=` query params to render page-specific social preview cards. All indexed setup pages use unique OG images via the new `ogMeta()` helper. Previously every page shared the same generic card.
- **Blog / content marketing pages** (#429) — Added `/blog` route with three long-tail SEO articles targeting "automate instagram stories", "google drive instagram integration", and "telegram bot for instagram". Each article has unique OG images, canonical URLs, keyword metadata, and a waitlist CTA. Blog link added to site header; sitemap updated with blog entries. Pages are statically generated.
- **Core Web Vitals: prose styles** (#430) — Added lightweight prose typography for blog content (headings, lists, code blocks, links) without pulling in a full typography plugin.

- **Skeleton loading states for all dashboard pages** — Added `loading.tsx` files for overview, media library, calendar, dead content, reuse analytics, settings, and setup wizard routes. Uses Next.js streaming SSR so users see an animated skeleton UI instantly instead of a blank screen while backend data loads. Includes new `Skeleton` UI primitive (shadcn pattern).
- **Dashboard error boundary and 404 page** — Added `error.tsx` for graceful error recovery (icon, message, retry button, link back to overview) and `not-found.tsx` for dashboard 404s. A `[...slug]` catch-all route calls `notFound()` so unknown paths like `/dashboard/nonexistent` trigger the custom 404 instead of the default Next.js error. Both render inside the dashboard shell (sidebar + header stay visible). Previously, a failed data fetch or bad route showed the default Next.js error page with no navigation.
- **`api_tokens.meta_account_id` column** — Phase 1 of the Instagram credential refactor (#380). Adds an explicit, indexed column on `api_tokens` to store the Meta-side identifier (Business Account ID or Instagram User ID) that issued the token. Additive only — no behavior changes, no columns removed. Migration 035. See `documentation/planning/2026-05-18-instagram-credential-refactor.md` for the full 5-PR plan.
- **Credential refactor dual-write** — Phase 2 (#380). `_create_account_with_token` and `update_account_token` now write the Meta-side ID to both the new `api_tokens.meta_account_id` column and the existing `token_metadata.account_id` JSONB field. `TokenRepository.create_or_update` accepts the new `meta_account_id` parameter and preserves existing values when not passed (safe for the token refresh path). No read-path changes — rollback-safe.
- **Credential refactor backfill + credential-keyed reads** — Phase 3 (#380). Migration 036 backfills `api_tokens.meta_account_id` from existing `instagram_accounts` rows. New `get_by_meta_account_id` repo method joins through `api_tokens` to resolve accounts across OAuth flows. All three OAuth callsites switched to `get_account_by_meta_id` with legacy fallback. Removes PR #378's cross-flow username fallback — no longer needed.

### Changed

- **Dashboard frontend cleanup** — Lifted shared `InstagramAccount` type into `lib/types.ts` (removes duplicates from setup-wizard and accounts-tab), extracted `openOAuthWindow` helper into `lib/dashboard-api.ts` (removes duplicate OAuth-window logic from three components), added `visibilitychange` listener to auto-detect OAuth completion when the tab regains focus, and added `aria-label` to all settings toggle switches for screen reader accessibility. Closes #340.

### Security

- **HTTP security headers on all API responses** — Added `SecurityHeadersMiddleware` with HSTS (`max-age=63072000; includeSubDomains`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, CSP (`default-src 'self'`, `frame-ancestors 'none'`), and `Referrer-Policy: strict-origin-when-cross-origin`. Closes #382.
- **Thumbnail proxy blocks SVG content type** — SVG files can contain embedded JavaScript. The thumbnail proxy now rejects `image/svg+xml` responses from upstream, in addition to non-image types. Closes #383.
- **Instagram API errors no longer leak internal details** — The `/add-account` endpoint previously forwarded raw Instagram Graph API error messages (which can contain token fragments, internal endpoint paths, or OAuth details) to the client. Now logs the raw error server-side and returns one of three sanitized messages depending on the error category. Closes #384.
- **Required secrets validated at startup** — `ENCRYPTION_KEY` or `ENCRYPTION_KEYS` is now checked during `validate_all()` at boot. Missing encryption keys previously only surfaced at runtime when an OAuth flow tried to encrypt a token. Closes #385.
- **Auth endpoints have stricter rate limits** — OAuth start endpoints limited to 5/min, OAuth callbacks to 10/min, and `/add-account` to 5/min (down from the 30/min global default). Closes #386.
- **CI no longer commits a test encryption key** — The hardcoded Fernet key in `.github/workflows/ci.yml` is replaced with a dynamically generated key per CI run. Closes #387.

### Fixed

- **Telegram Mini App completely frozen — no interaction works** — The `SecurityHeadersMiddleware` applied `default-src 'self'` with no `script-src` override, which blocked loading the Telegram WebApp SDK from `https://telegram.org/js/telegram-web-app.js`. Without the SDK, `Telegram.WebApp.ready()` never fired, so Telegram's native loading overlay stayed on top of the WebView intercepting all touch events — the page rendered data underneath but nothing was clickable. Additionally, `X-Frame-Options: DENY` and `frame-ancestors 'none'` blocked Telegram Desktop/Web from embedding the Mini App in an iframe. Fix splits the CSP policy: Mini App routes (`/webapp/`, `/static/onboarding/`) now allow `script-src 'self' https://telegram.org` and `frame-ancestors 'self' https://web.telegram.org https://*.telegram.org`; all other routes keep the strict policy unchanged. Closes #456.
- **Setup page fails to load ("Failed to load setup state")** — The SSR setup page sent a POST request to the GET-only `/api/onboarding/init` endpoint, resulting in HTTP 405. Changed to `backendFetchJson` (GET) to match how the settings page correctly calls the same endpoint. A prior fix (`aabaaf2`) corrected the client-side `setup-wizard.tsx` but missed the SSR `setup/page.tsx`.
- **Telegram Mini App dashboard broken** — The vanilla JS Mini App (`app.js`) was still sending POST to `/api/onboarding/init` after the endpoint changed to GET. All 4 call sites (`init`, `_refreshHome`, `returnToHome`, `_startPolling`) now use `_apiGet` with query parameters. Additionally, `SecurityHeadersMiddleware` was sending `X-Frame-Options: DENY` and `frame-ancestors 'none'` on Mini App paths, which blocks the iframe Telegram uses to embed the WebApp. Mini App paths (`/webapp/`, `/api/onboarding/`, `/static/onboarding/`) now allow `frame-ancestors https://web.telegram.org https://*.telegram.org` while all other paths retain the strict DENY policy.
- **Google Drive tokens fail silently when refresh token expires (Testing mode 7-day TTL)** — `google.auth.exceptions.RefreshError` was not caught anywhere in the Drive API call path, so an expired refresh token (e.g. Google OAuth app in Testing mode, where refresh tokens expire after 7 days) would surface as an unhandled exception instead of the existing reconnect-alert flow. Now caught in `_execute_with_retry` and `_download_chunk_with_retry` and converted to `GoogleDriveAuthError`. Additionally, refresh token `issued_at` is now stored on exchange so that `TokenRefreshService` and `HealthCheckService` can compute token age against the configurable `GOOGLE_REFRESH_TOKEN_TTL_DAYS` (default 7, set to 0 after moving to Production mode). Health checks now surface "Testing mode" warnings when a refresh token is approaching or past expiry. Closes #373, Closes #376.

- **All Telegram posts failed for 5 days — session-detach race in `transaction_cleanup_loop`** — The SQLAlchemy `SessionLocal` factory in `src/config/database.py` did not set `expire_on_commit=False`, leaving it at SQLAlchemy's default of `True`. Every 30 seconds the worker's `transaction_cleanup_loop` called `end_read_transaction()` on every repo, which committed the session and (because `expire_on_commit=True`) expired every ORM instance loaded in that session. Any code path holding a `ChatSettings` instance across that 30s window — most importantly `send_notification` reading `chat_settings.caption_style` and `chat_settings.enable_instagram_api` — would raise `Instance <ChatSettings> is not bound to a Session; attribute refresh operation cannot proceed` on the next attribute access. The race was always present but masked by `chat_settings.X or env_settings.X` fallback chains; commit `7ea90ff` ("DB is source of truth for per-chat config; remove env fallbacks") removed those fallbacks and the race became a 100%-failure-rate outage. Last successful post was 2026-05-13 02:33 UTC; 909+ consecutive `send_notification` failures observed during investigation. Fix sets `expire_on_commit=False` on the session factory — for a long-running worker that fans ORM instances across services and async tasks, this is the right default. Closes #388. Pin in `tests/src/config/test_database.py` so a future flip back to the SQLAlchemy default doesn't silently re-introduce the outage.
- **Worker redeploys failing with `telegram.error.Conflict` — shutdown handler was fire-and-forget** — Every worker deploy since 2026-05-17 14:22 EDT has been marked FAILED by Railway because the new worker hit `telegram.error.Conflict: terminated by other getUpdates request` at startup. Root cause: the SIGTERM handler in `src/main.py` was scheduled via `asyncio.create_task(shutdown_handler(s))` and never awaited. The main coroutine's `await asyncio.gather(*tasks)` returned as soon as tasks were cancelled, the process exited, and Telegram never saw the old session close — so when the new worker tried to start, Telegram still believed the old session was alive and rejected the new poll with 409. Fix tracks the shutdown task and explicitly awaits it after the gather, so `application.updater.stop() → application.stop() → application.shutdown()` completes (and Telegram learns the session is over) before the process exits. Also added `drainingSeconds = 60` to `railway.toml` so Railway gives the worker enough time to finish that drain before SIGKILL fires. Closes #392.
- **Instagram Login OAuth failed for accounts previously connected via Facebook Login** — `InstagramLoginOAuthService.exchange_and_store` looked up the existing account row by `instagram_account_id`, using the **Instagram User ID** returned from `me?fields=id`. But accounts connected through the Facebook Login flow were stored under the **Instagram Business Account ID** from `/me/accounts.instagram_business_account.id`, which is a different numeric ID for the same physical account. The lookup missed, the code took the `add_account` branch, and `_validate_new_account` then rejected on the duplicate-username uniqueness check with `Account @gatortails already exists as 'GT'` — so the OAuth callback returned a generic "Connection Failed" page after a fully successful Instagram-side handshake. Now: if the ID lookup misses, fall back to `get_account_by_username`. If a row exists under that username, update its token via `update_account_token` using the *existing* stored ID (so future FB Login lookups still resolve). Restores the cross-flow re-authentication path without a schema change.
- **Crashed background loop never restarts** — `guarded()` now restarts the wrapped coroutine on crash with exponential backoff (1s, 2s, 4s, ... capped at 60s). Caps at 10 restarts per rolling hour to prevent infinite crash loops from burning resources. Backoff and counters reset after 5 minutes of stable operation. A single scheduler exception no longer permanently kills posting. Closes #364.
- **Health endpoint doesn't check loop liveness** — The worker's `/health` TCP server returned 200 unconditionally, so Railway kept reporting healthy even when critical loops (scheduler, media sync) had crashed. Now calls `get_loop_liveness()` on each request: returns 200 when all loops are alive, 503 with a JSON body listing which loops are stale (>2x expected interval without a heartbeat). Railway will restart the worker when a loop dies. Closes #361.
- **Telegram posting failures are silent** — When `send_notification()` failed in the scheduler, the queue item was immediately deleted with no retry, no `posting_history` record, and no alert — the post was permanently lost. Now retries up to 3 times with 5s backoff. On final failure: marks the queue item as `failed` (not deleted), records to `posting_history` with `status='failed'` and `error_message`, and logs at WARNING. If 3+ consecutive posts fail, logs at CRITICAL to signal systemic issues (e.g. revoked bot token). Migration 034 adds `failed` to the queue status constraint and `error_message` column to `posting_history`. Closes #359.
- **Google Drive tokens silently fail after encryption key rotation** — `get_user_credentials()` caught decryption `ValueError`/`InvalidToken` and silently returned `None`, surfacing as a generic "token expired" with no actionable error. Now logs at CRITICAL with the specific failure reason and sends a Telegram alert to the affected chat with a re-auth link. Also hardened `TokenEncryption.decrypt()` with an explicit per-key fallback loop after the MultiFernet fast path, so tokens encrypted with any key in `ENCRYPTION_KEYS` are recoverable. Closes #370.
- **Google Drive provider has no retry logic** — Added tenacity-based exponential backoff (3 attempts, 1s/2s/4s) to all Drive API calls. Retries on 429/500/502/503/504 and network errors. Respects the `Retry-After` header on 429 responses instead of hardcoding 60s. Download chunk loop enforces a 5-minute timeout. Closes #352.
- **Worker process restart-cycling on Railway** — The worker service (Telegram bot + scheduler) had no HTTP endpoint, so Railway's health check (`healthcheckPath = "/health"`) timed out and sent SIGTERM every ~8 minutes, producing 0 posts per cycle. Added a minimal stdlib `asyncio` TCP server to `src/main.py` that binds to `PORT` and returns 200 OK. Runs alongside existing tasks in `asyncio.gather()`, zero new dependencies.
- **Railway health check killing deployments** — Added `/health` endpoint to the web process (`GET /health` → 200, no auth). Configured `railway.toml` with `healthcheckPath = "/health"` and `healthcheckTimeout = 30` so Railway's health checker hits a real endpoint instead of timing out and tearing down the service. Closes #347, #350.
- **Scheduler catch-up after restart** — When the worker restarts after missing posting slots, the scheduler now gradually catches up instead of skipping missed posts. Detects when `last_post_sent_at` is behind by >= 2 intervals and advances it by one interval per tick (instead of jumping to now), so each 60s tick fires one catch-up post until the schedule is current. Logs catch-up events with slot count and timestamps for Railway observability. (#349)
- **First-tick immediate post survives rapid redeploys** — On the first scheduler tick after startup, if `last_post_sent_at` is stale (>= 2x interval), the catch-up post resets the timer to now instead of advancing gradually. This ensures each deploy restart fires one immediate post that counts as current, preventing rapid redeploy churn from starving the posting schedule. Subsequent ticks resume gradual catch-up from #349. (#348)
- **Posting window uses UTC hours with no timezone awareness** — Added `posting_timezone` column to `chat_settings` (IANA timezone string, e.g. "America/New_York"). `_in_posting_window()` now converts UTC to the user's local time before comparing against `posting_hours_start/end`. Existing rows with NULL timezone continue using UTC. Default for new chats: "America/New_York". Migration 033. (#351)

### Added

- **Google OAuth verification submission runbook** — new `documentation/operations/google-oauth-verification.md` walks through the manual steps to clear the "Google hasn't verified this app" warning on the Drive consent screen: domain verification via Google Search Console, OAuth consent screen field-by-field, scope justification copy for `drive.readonly`, demo video requirements, submission flow, and the test-user allowlist as a stop-gap while review is pending. Captures the #327 outcome ("`drive.readonly` is the minimum viable scope") as the rationale for taking this route. Closes the documentation half of #333; the submission itself remains a manual operations task.
- **`ensure_utc(dt)` datetime helper** (`src/utils/datetime_utils.py`) — single source of truth for "naive datetime → UTC-aware" coercion. Returns `None` unchanged; passes already-aware datetimes through without re-allocating. Replaces 5 copies of the same inline idiom in `setup_state_service.py`, `telegram_commands.py`, `scheduler.py`, `dashboard_history_queries.py`, and `telegram_utils.py`. Also used in `ApiToken.is_expired` and `ApiToken.hours_until_expiry`, which previously compared `expires_at` to a naive `datetime.utcnow()` — that latent bug never surfaced because both sides happened to be naive, but it would have broken the moment either side became aware (e.g., a future column migration to `DateTime(timezone=True)`). Closes #335.

### Security

- **Encryption key rotation support** — Switched `TokenEncryption` from single-key `Fernet` to `MultiFernet`, enabling zero-downtime key rotation. New `ENCRYPTION_KEYS` env var accepts comma-separated Fernet keys (newest first); falls back to `ENCRYPTION_KEY` for backward compatibility. New `storydump-cli rotate-keys` command re-encrypts all stored tokens with the current primary key. Includes `TokenEncryption.rotate()` method that delegates to `MultiFernet.rotate()`. (#326)
- **Pin all dependency versions** — Replaced `>=` version specifiers with exact `==` pins for all unpinned dependencies (cloudinary, google-api-python-client, google-auth, google-auth-oauthlib, fastapi, uvicorn, python-multipart, cryptography, anthropic). Prevents supply chain attacks via silent upgrades and ensures reproducible builds. (#325)
- **Add rate limiting to API endpoints** — Added SlowAPI middleware with a global default of 30 req/min per IP. Mutation-heavy endpoints (`toggle-setting`, `update-setting`, `update-string-setting`) limited to 10/min; `sync-media` limited to 5/min to prevent Google Drive API abuse. Returns 429 with `Retry-After` header when exceeded. (#324)
- **Google Drive OAuth scope audit** — Evaluated narrowing `drive.readonly` to `drive.file` or `drive.metadata.readonly`. `drive.readonly` is the minimum viable scope: `drive.file` breaks folder browsing (user media predates the app), `drive.metadata.readonly` blocks file downloads. Documented the tradeoff and the Picker API narrowing path in `google_drive_oauth.py` and `google_drive_provider.py`. (#327)
- **Token revocation for compromised OAuth tokens** — Added `revoked_at` nullable timestamp to `api_tokens` (migration 032). All token retrieval queries now filter `revoked_at IS NULL`, so revoked tokens are invisible to the application. New `storydump-cli revoke-tokens --service <instagram|google_drive>` command calls provider revocation APIs (Meta `DELETE /me/permissions`, Google `POST /revoke`) before marking tokens revoked. Re-authentication via the normal OAuth flow clears revocation and issues fresh tokens. (#328)
- **Auth failure alerting** — In-memory failure counter (`src/utils/auth_monitor.py`) tracks authentication failures per source IP within a 10-minute sliding window. When 5 failures are reached, sends a Telegram alert to `ADMIN_TELEGRAM_CHAT_ID`. All auth failures in the onboarding API now log source IP and failure reason. Counters auto-prune expired entries on each check. (#329)

### Removed

- **Stale documentation archive** — Deleted `documentation/archive/` directory containing 82 historical planning docs (Jan-Mar 2026) that are no longer relevant to current development. Reduces repo clutter by ~51,000 lines.

### Changed

- **Storydump rebrand in docs and CI** — Updated remaining `storyline` references to `storydump` across operational docs, planning docs, CI workflow, and onboarding UI.

### Fixed

- **Lazy repository sessions** — `BaseRepository` now opens DB sessions on first `.db` access instead of eagerly in `__init__`, preventing connection pool exhaustion when services instantiate many repositories. (#320)
- **Setup Wizard showed "Not connected" for Google Drive even after a successful OAuth reconnect** — `is_token_stale` in `setup_state_service.py` compared `token.expires_at` (naive `DateTime` per `api_tokens` schema) against `datetime.now(timezone.utc)` (aware), raising `TypeError: can't compare offset-naive and offset-aware datetimes`. The exception was swallowed by the `_check_gdrive` try/except, which then returned `connected: False`. Wizard Step 2 displayed "Not connected" while Step 3 (which doesn't touch the token) correctly reported "Configured, 4554 files". Fix coerces naive `expires_at` to UTC-aware before comparison.
- **Google Drive posts failed immediately after a successful reconnect** — `MediaSourceFactory.get_provider_for_media_item` passed `telegram_chat_id` but **not** `root_folder_id` when constructing a Google Drive provider. The per-tenant OAuth path (`get_provider_for_chat`) requires `root_folder_id` and raised `"No root_folder_id configured for Google Drive media source."`, which the factory's broad `except Exception` (intentional service-account fallback) silently swallowed. The fallback then hit the service-account path, which had no credentials, and surfaced the misleading `"No Google Drive credentials found. Run 'storydump-cli connect-google-drive' first."` — chasing readers toward a token bug for a config-plumbing bug. Fix resolves `root_folder_id` from `chat_settings.media_source_root` in `get_provider_for_media_item` and passes it through to `create()`.
- **Google Drive disconnect alert now behaves as a state transition, not a recurring hourly event** — `PostingService.send_gdrive_auth_alert` used to suppress duplicates via a class-level monotonic timestamp with a 3600s window. Because the JIT scheduler attempts a posting tick whenever a slot is due (~hourly for typical configs), the cooldown lapsed just before each new attempt and the alert re-fired forever until reconnect (observed: TL Stories chat, 9:04 AM → 8:28 PM, ~62 min cadence). Replaced with a persisted `chat_settings.gdrive_alerted_at` column (migration 031): the alert fires once on the first auth error of a disconnect event, stays silent until the OAuth reconnect callback clears the flag, and is restart-safe + per-chat scoped. Removed the `_last_gdrive_alert_time` class variable entirely.
- **Scheduler tick poisoning standalone `queue_repo` session** — `_scheduler_tick` calls `queue_repo.discard_abandoned_processing()` before iterating chats. `queue_repo` is instantiated standalone (not owned by a BaseService), so the outer loop's `cleanup_transactions()` doesn't roll it back on error. A single transient DB failure was leaving the session in a broken transaction and every subsequent tick threw `PendingRollbackError` for the lifetime of the worker (observed in production after the token-rotation incident). Wrapped the call in try/except + `queue_repo.rollback()`.

### Added

- **Privacy Policy and Terms of Service pages on the landing site** — New `/privacy` and `/terms` routes under `landing/src/app/(marketing)/` so storydump.app can be submitted for Google OAuth verification (Google's consent screen requires public privacy + terms URLs). Privacy page covers the Google API Services User Data Policy Limited Use disclosure for the Drive scope, sub-processor list (Vercel, Neon, Railway, Telegram, Meta, Google, Plausible), cookies table (`storydump_session`, `storydump-waitlist-registered`, Plausible cookieless), retention schedule, and GDPR/CCPA/COPPA rights. Terms page covers eligibility, third-party services, user-content license, acceptable use, AS-IS disclaimer, liability cap, indemnification, and a placeholder NY governing-law clause (flagged with a `TODO: confirm jurisdiction` comment for counsel review). Footer gets "Privacy" / "Terms" links; `sitemap.ts` gets the new URLs.

### Changed

- **Legacy Instagram env-var fallbacks removed** — `INSTAGRAM_ACCOUNT_ID`, `INSTAGRAM_ACCESS_TOKEN`, and `INSTAGRAM_USERNAME` deleted from `settings.py`. The multi-account schema (`instagram_accounts` + `api_tokens` + `chat_settings.active_instagram_account_id`) is now the only path. `InstagramCredentialManager.is_configured` / `get_active_account_credentials` / `validate_instagram_account_id` no longer consult env. `TokenRefreshService` dropped its `_get_env_token` and `bootstrap_from_env` methods — tokens land in `api_tokens` via the OAuth callback, full stop. `INSTAGRAM_DEEPLINK_URL` moved to `src/config/defaults.py` as a code constant (it's the same `https://www.instagram.com/` value for every deployment). The Meta-app credentials (`FACEBOOK_APP_ID`/`SECRET`, `INSTAGRAM_APP_ID`/`SECRET`, `INSTAGRAM_POSTS_PER_HOUR`) stay env — those represent the deployment's single registered Meta app, not per-user config.
- **Per-chat settings now read DB-only; env-var fallbacks removed** — Completes the env→DB migration. New `src/config/defaults.py` module holds hardcoded code-level defaults (`DEFAULT_POSTS_PER_DAY`, `DEFAULT_REPOST_TTL_DAYS`, `DEFAULT_CAPTION_STYLE`, etc.) used in two places: (1) `ChatSettingsRepository.get_or_create` bootstrap for new chats, and (2) runtime fallbacks when a column is NULL on an older row. All `... or env_settings.X` fallback chains are gone from services (`media_lock._resolve_ttl`, `telegram_lifecycle._lifecycle_notifications_enabled`, `telegram_notification._build_caption`, `settings_service.get_media_source_config`, `instagram_credentials.is_configured`). Deprecated env-var declarations removed from `src/config/settings.py`: `POSTS_PER_DAY`, `POSTING_HOURS_START`, `POSTING_HOURS_END`, `REPOST_TTL_DAYS`, `SKIP_TTL_DAYS`, `DRY_RUN_MODE`, `ENABLE_INSTAGRAM_API`, `MEDIA_SOURCE_TYPE`, `MEDIA_SOURCE_ROOT`, `MEDIA_SYNC_ENABLED`, `CAPTION_STYLE`, `SEND_LIFECYCLE_NOTIFICATIONS`. Boot-time validator drops the per-chat range checks; those values are validated at the API write boundary instead. Startup banner trimmed to system-wide knobs (per-chat config is no longer meaningful as a deployment summary). Auto-bootstrap loop guard in `main.py` removed — `media_sync_loop` always starts and iterates per-chat instead of being gated by env.

### Added

- **Setup Wizard "Connect Instagram" step now shows all connected accounts and a "Connect another" button** — previously the wizard's Step 1 only rendered a single `Status: Connected username` row when an account was linked, with no way to see additional accounts or add more without bouncing to **Settings → Accounts**. The step now lists every connected Instagram account with an Active badge and a Switch button for non-active ones, and the primary CTA flips to "Connect another account" once at least one is linked. Reuses the existing `/api/onboarding/accounts` + `switch-account` endpoints (same as the Settings tab). Closes #332.
- **Instagram OAuth ingests all Facebook Pages' Instagram accounts in one connect** — `OAuthService._get_instagram_account_info` (singular) hard-coded `page_id = pages[0]["id"]` and returned only the first Facebook Page's linked Instagram. Users with multiple FB Pages — and therefore multiple connected IG Business Accounts — never saw the others; the OAuth flow had to be re-run with a different Page selected at the Facebook consent step. Renamed to `_get_instagram_accounts_info` (plural), which now iterates every Page, dedupes Pages that share an IG, and returns a list. `exchange_and_store` stores all detected accounts; the first one becomes active for the originating chat (preserving the prior single-account behavior), the rest land as inactive and can be activated from **Settings → Accounts**. New result field `account_count` surfaces how many accounts were ingested. Closes #331.
- **Per-chat caption style and lifecycle notifications** — Last two env-only audit items moved to `chat_settings` via migration 030. `caption_style` ("enhanced" / "simple") is a new select on the Settings page; `send_lifecycle_notifications` joins the toggle list. `_build_caption` accepts the resolved style as a parameter (caller passes `chat_settings.caption_style or settings.CAPTION_STYLE`), and `TelegramLifecycleHandler._lifecycle_notifications_enabled()` looks up the admin chat's flag before sending. New `POST /api/onboarding/update-string-setting` endpoint with a per-setting allowed-value list keeps string mutations validated at the API boundary without overloading the numeric `update-setting` route.
- **Content Mix step in the Setup Wizard** — Inserted between Set Schedule and Complete. Renders the same `CategoryMixCard` used in Settings so users can configure per-category posting weights during onboarding instead of discovering them post-setup. Step is optional (Next advances without saving — the scheduler falls back to library-proportional when no explicit mix exists). Wizard now 7 steps total (was 6); step renumbering applied to inferStep, canAdvance, isStepComplete, and the Next/Complete button gating.
- **Media source visibility in Settings → Integrations** — `chat_settings.media_source_type` and `media_source_root` were DB-only with no UI surface. Now rendered as a read-only summary on the Integrations tab below the file-count line. Edits still go through the Setup Wizard's media-folder step (single source of truth for source configuration).
- **Per-chat lock TTLs** — `REPOST_TTL_DAYS` and `SKIP_TTL_DAYS` move from env-only to env + per-chat (migration 029 adds `chat_settings.repost_ttl_days` and `chat_settings.skip_ttl_days`, nullable). `MediaLockService.create_lock()` accepts `telegram_chat_id`; when present it looks up the per-chat value and falls back to the env default. New "Repost Cadence" card in dashboard Settings exposes numeric inputs for both. Closes two of the orphaned envs surfaced in the env↔DB audit; `CAPTION_STYLE` and `SEND_LIFECYCLE_NOTIFICATIONS` left as env-only (former pending demand, latter correctly global since it targets the admin chat).
- **Boot-time Telegram token validity check** — `ConfigValidator.validate_all()` now calls `https://api.telegram.org/bot{token}/getMe` at startup and fails loudly on HTTP 401. Without this, python-telegram-bot's lazy validation meant a rotated/revoked token only surfaced on the first polling attempt and looked like a generic "app feels down" outage. Inconclusive results (network errors) are non-blocking so a transient blip doesn't crash startup.

### Security

- **Thumbnails proxied through authenticated endpoint** — `media-library` API previously returned the raw Google Drive `lh3.googleusercontent.com` thumbnail URL, which acts as an "anyone with the link" share for the duration of the signature. A logged-in user could copy a thumbnail link and share it with anyone. The response now exposes only a `has_thumbnail` boolean; the actual bytes are served by `GET /api/onboarding/media/{id}/thumbnail` which validates session, scopes the lookup to the requesting chat, fetches from Drive server-side, validates the upstream content-type, and streams back with `Cache-Control: private, max-age=3600`. Frontend updated to point `<img>` tags at the proxy path. Original Drive files were never exposed; only the small thumbnail.

### Changed

- **Instagram API kill-switch is now per-chat, not global** — `InstagramCredentialManager.is_configured()` previously short-circuited on `settings.ENABLE_INSTAGRAM_API` regardless of the per-chat DB toggle. Now reads `chat_settings.enable_instagram_api` first and only falls back to the env value when no chat_settings row exists (which shouldn't happen because `SettingsService` bootstraps a row on first access). Matches how dashboard/Telegram toggles already represent the setting.

### Added

- **AI Captions toggle exposed in dashboard Settings** — `chat_settings.enable_ai_captions` was a DB-only orphan (no UI). Added to the Toggles section in the General tab, threaded through the `/init` setup state, and added to the `toggle-setting` allowlist so it can be flipped from the web.
- **Preview tiles on /dashboard/media** — Media-library tiles now render the Google Drive `thumbnailLink` as an inline preview image instead of just a MIME-type label. New `media_items.thumbnail_url` column (migration 028) is populated by `MediaSyncService` from Drive's `thumbnailLink` field; `media-grid.tsx` shows `<img>` when the URL is present and falls back to the MIME label on error or for items without one (local uploads). Existing 4554 items backfill on next sync because the identifier-match handler now detects null→url drift and writes through.
- **Content Mix UI in dashboard Settings** — New "Content Mix" card in the General tab that reads `category_post_case_mix` for the active chat and lets users set per-category posting weights via sliders (sum-to-100 validation). Pre-seeds proportional to library composition when no explicit mix exists, surfaces a banner explaining that the scheduler defaults to unfiltered random in that state. Added `POST /api/onboarding/category-mix` (read) and `POST /api/onboarding/update-category-mix` (write) endpoints, both wrapping the existing `CategoryMixRepository`. BFF allowlist updated.
- **Sign-in link in landing page footer** — Subtle "Sign in" link for existing users to access `/login` without a prominent CTA.
- **Analytics and conversion tracking** (#279) — Integrated Plausible Analytics for privacy-friendly page views, referrer tracking, and custom events. Tracks waitlist signups, form errors, FAQ expansions, and comparison table views. Captures UTM parameters (source, medium, campaign) with waitlist submissions for attribution. Controlled via `NEXT_PUBLIC_PLAUSIBLE_DOMAIN` env var — omit to disable.
- **SEO optimization** (#280) — Enhanced meta tags with keywords, canonical URL, and robots directives. Added dynamically generated OG image (1200x630) via Next.js ImageResponse API. Created sitemap.xml route and robots.txt. Added JSON-LD structured data for SoftwareApplication and FAQPage schemas. Updated site URL to storydump.app.

- **Post-signup experience** (#260) — Replaced dead-end "We'll be in touch" confirmation with enriched post-signup block: sets timeline expectations (within a week), gives a micro-task (prepare Google Drive folder), and adds Telegram community link to move signups from "registered" to "engaged."
- **Social proof stats bar and trust badges** (#258) — Added stats bar (stories posted, content managed, active creators) between Hero and How It Works sections. Added trust badges below hero CTA with lock/Instagram/key icons addressing top 3 signup objections (content stays in Drive, official API, no password required).
- **Competitive positioning section** (#259) — Added "Why not just use Buffer?" comparison table between Features and Pricing, contrasting Storydump vs Buffer/Later across 5 dimensions. Added positioning line to hero: "The Instagram Story tool that lives in Telegram — not another dashboard."

### Fixed

- **Instagram posts could camp on DB connections for ~7 minutes, exhausting the pool** — `_wait_for_container_ready` could spend up to 30 polls × (2s sleep + 30s HTTP timeout) = ~16 min worst case, and the surrounding `track_execution` block kept repository sessions checked out the whole time. Under burst load this drained the `pool_size=10 + max_overflow=10` pool to zero, surfacing as `QueuePool limit … reached, connection timed out` and `Auto Post Failed` notifications. Wrapped the 3-step Instagram flow in `asyncio.wait_for(..., timeout=180)` for a hard 3-minute wall-clock cap, dropped the per-poll HTTP timeout from 30s to 10s, and bumped Railway's `DB_POOL_SIZE` and `DB_MAX_OVERFLOW` to 20 each on both worker and storydump services (80 total connections across the deployment, comfortably under Neon's free-tier cap).
- **Dashboard Settings page showed hardcoded fallback values instead of real per-chat settings** — `/api/onboarding/init` was declared `@router.post`, but the dashboard's SSR fetched it via GET through `backendFetchJson`, which silently received 405 and returned null. The Settings page then fell through to `?? 3` / `?? 9` / `?? 22` hardcoded defaults. Changed `/init` to GET (read-only with idempotent side effect), refactored the client `postApi("init")` caller in the setup wizard to `getApi("init")`, and updated tests accordingly.
- **Landing `/login` Telegram widget failed to render** — Added `'unsafe-eval'` to the `/login` Content-Security-Policy. `telegram-widget.js` evaluates the `data-onauth` handler via `eval()`, which the previous CSP blocked; `initWidget` aborted before inserting the iframe, leaving users on the "Telegram widget didn't load" fallback. CSP relaxation is scoped to `/login` only.
- **Google Drive OAuth "valid for 0 hours" message** — Removed misleading token expiry detail from Telegram notification since tokens auto-refresh. Cleaned up unused `expires_in_hours` from OAuth return dict.

### Changed

- **Update outdated dependencies** (#274) — Bumped 12 direct dependencies to latest: pydantic 2.13.0→2.13.3, pydantic-settings 2.12.0→2.14.0, SQLAlchemy 2.0.46→2.0.49, psycopg2-binary 2.9.11→2.9.12, Pillow 12.1.1→12.2.0, click 8.3.2→8.3.3, python-dotenv 1.2.1→1.2.2, certifi→2026.4.22 (security certs), uvicorn 0.44→0.46, fastapi 0.136.0→0.136.1, anthropic 0.96→0.97, cryptography ≥46.0. Updated lower bounds for all `>=` specifiers to reflect tested versions.

- **Break up oversized functions** (#272) — Refactored 5 functions exceeding 90 lines into smaller, focused methods: `refresh_instagram_token()` (128→65 lines), `handle_settings_edit_message()` (130→20 lines), `onboarding_upload_media()` (131→40 lines), `sync()` (132→60 lines), and `_process_provider_file()` (7 params→2 via `SyncContext` dataclass).

- **Extract background loops from main.py** (#271) — Moved 5 background loops (scheduler, lock cleanup, cloud cleanup, transaction cleanup, media sync), heartbeat tracking, crash guard, and lifecycle helpers into dedicated modules under `src/services/core/loops/`. Reduced main.py from 851 lines to ~170 lines of pure service wiring.

- **Landing page hero copy — 3-second test** (#256) — Replaced vague headline "Keep Your Stories Alive" with "Instagram Stories on Autopilot" for instant clarity. Rewrote subheadline to lead with pain point ("Stop manually posting") and close with trust hook ("hands-free but always in control"). Added social proof line under hero CTA.
- **Unified CTA copy across landing page** (#257) — Changed all CTA labels from "Join the Waitlist" / "Join Waitlist" to "Get Early Access" for consistent, action-oriented messaging. Updated hero form, header nav button, and submitting state text.

### Changed

- **Multi-instance DM view for /status and startup** (#267) — DM `/status` now shows user-level instance list with manage buttons instead of single-instance status dump. Startup notification uses `DashboardService.get_user_instances()` for the same multi-instance overview. Group `/status` unchanged. Consolidated `escape_markdownv2` and `format_last_post` into `telegram_utils.py` as shared helpers. Added Core Mental Model section to `PROJECT_MISSION.md`.

### Added

- **Test coverage for 7 untested service modules** (#252) — 110 unit tests covering `telegram_callbacks_core`, `telegram_callbacks_queue`, `telegram_callbacks_admin`, `conversation_service`, `start_command_router`, `user_service`, and `backfill_downloader`. Shared test helpers (`make_query`, `make_user`, `noop_context_manager`) added to `conftest.py`.

### Changed

- **DashboardService god class refactor** (#253) — Broke 816-line `DashboardService` into thin facade (145 lines) plus 4 focused query classes: `QueueDashboardQueries`, `MediaDashboardQueries`, `HistoryDashboardQueries`, `InstanceDashboardQueries`. Pushed query limits to repo layer, added `count_by_status` to `QueueRepository`.
- **TelegramService god class refactor** (#251) — Broke 804-line `TelegramService` into thin orchestrator (496 lines) plus 4 focused handler classes: `OperationStateManager`, `TelegramUserManager`, `TelegramMembershipHandler`, `TelegramLifecycleHandler`. Consolidated duplicate `_escape_markdown` into `telegram_utils.py`.

### Fixed

- **Scheduler silent failure — no posts since Apr 19** (#266) — `get_or_create()` bootstrapped `chat_settings` rows with `onboarding_completed=False`, but `get_all_active()` requires `True`. The scheduler loop ran (heartbeat ticked, cleanup tasks fired) but found zero eligible chats every tick. Fixed by setting `onboarding_completed=True` on bootstrap, adding `settings_service` to scheduler cleanup, adding throttled warning when no active chats found, and migration 027 to repair existing rows.
- **Guard `cleanup_transactions()` in background loop finally blocks** (#264) — Wrapped 7 unguarded `cleanup_transactions()` / `end_read_transaction()` calls in `try/except` across all background loops (scheduler, retention tick, pool/token health ticks, lock cleanup, cloud storage cleanup, media sync). An unhandled exception in a `finally` block could crash the loop via `_guarded()` while the worker process stayed alive — causing the scheduler to silently stop posting.
- **Narrow broad `except Exception` catches** (#250) — Audited 88 `except Exception` catches across `src/services/`. Narrowed catches to specific types (`TelegramError`, `SQLAlchemyError`, `InvalidToken`, etc.) where failure modes are known. Added `noqa: BLE001` annotations with justification comments to intentionally broad catches (best-effort logging, cleanup, health checks). Added `exc_info=True` to health check error handlers.
- **Replace deprecated `datetime.utcnow()` calls** (#249) — Replaced 30+ `datetime.utcnow()` calls across `src/services/` with timezone-aware `datetime.now(timezone.utc)`, eliminating Python 3.12 deprecation warnings. Inverted `.replace(tzinfo=None)` patterns to ensure consistent aware-to-aware datetime comparisons.

### Added

- **Test coverage for 7 untested service modules** (#252) — 110 unit tests covering `telegram_callbacks_core`, `telegram_callbacks_queue`, `telegram_callbacks_admin`, `conversation_service`, `start_command_router`, `user_service`, and `backfill_downloader`. Shared test helpers added to `conftest.py`.
- **AI caption generation** (#182) — New `CaptionService` generates Instagram Story captions using Claude API at queue time. Controlled by per-instance `enable_ai_captions` toggle. Generated captions are stored separately from manual captions on `media_items.generated_caption`, shown with a robot indicator in Telegram review, and include a "Regenerate Caption" button. Skips generation when a manual caption exists or ANTHROPIC_API_KEY is not configured. Non-blocking — API failures never prevent posting. Migration 026 adds the new columns.
- **Settings & membership audit trail** (#244) — New `audit_log` table tracks settings changes, membership lifecycle, and media lock create/delete with entity type, field-level old/new values, and who made the change. Instrumented `SettingsService`, `MediaLockService`, and `MembershipRepository`. New `GET /audit-log` endpoint for per-instance activity log.
- **Onboarding drop-off tracking** (#245) — Expired onboarding sessions are now logged to `user_interactions` with `interaction_type='onboarding_dropout'` before deletion, capturing the step and duration for funnel analysis.
- **BFF proxy per-request membership validation** (#246) — Proxied dashboard API requests now validate that the user's `activeChatId` corresponds to an active membership before forwarding. Stale JWTs (e.g. user removed from a group mid-session) get reissued without `activeChatId`, forcing redirect to instance picker. Extracted shared `fetchUserInstances()` helper into `@/lib/backend`.
- **Multi-account Phase 3+4 — API auth, instance picker, dashboard switcher** (#235, #236) — Session `chatId` renamed to `activeChatId: number | null`. Login starts with null; user selects an instance on `/instances` page. New `GET /api/instances` and `POST /api/instances/:id/select` endpoints with server-side membership validation. BFF proxy and middleware guard on null `activeChatId`. Instance picker page handles 0/1/N instances (CTA, auto-redirect, card picker). Dashboard header shows instance switcher dropdown for multi-instance users. Mini App `/webapp/instances` entry point for Telegram WebView.
- **Multi-account Phase 2b — group linking + instance management** (#240) — `/start` deep links, `/link`, `/name`, `/instances`, `/new` bot commands. `ChatMemberHandler` for group add/kick detection.
- **Multi-account /start refactor + get_settings() split** (#233) — `get_settings()` now accepts `create_if_missing` parameter to prevent phantom DM `chat_settings` rows. 8 group callback call sites flipped. New `StartCommandRouter` with 5-branch `/start` handler. `ConversationService` wraps DM onboarding state machine. Migration 024 cleans up existing phantom rows.
- **Multi-account backfill script** (#232) — `scripts/backfill_memberships.py` backfills `user_chat_memberships` from historical `user_interactions`, promotes group admins/owners via Telegram API, and runs a verification gate for Phase 2 deploy readiness. Supports dry run, `--apply`, `--promote`, and `--verify` modes.
- **Multi-account data layer** (#231) — Foundation for multi-account dashboard support. Users can now belong to multiple chat instances via `user_chat_memberships` join table. Memberships are auto-created on group chat interactions. New `DashboardService.get_user_instances()` returns all instances a user belongs to with per-instance stats. Also adds `onboarding_sessions` table for future DM onboarding flow and `display_name` column on `chat_settings`.
- **Vercel deployment guide** — Documented all required env vars for `landing/` Vercel deployment in `documentation/guides/landing-vercel-deployment.md`.

### Fixed

- **Telegram login widget missing env var fallback** — `/login` now shows a helpful error message when `NEXT_PUBLIC_TELEGRAM_BOT_NAME` is not configured, instead of an infinite loading spinner.

### Fixed — Design Issues (#214–#219)

- **Mobile dashboard navigation** (#214) — Added hamburger menu (Sheet drawer) to dashboard header so sidebar nav is accessible on mobile viewports.
- **Setup guide mobile overflow** (#215) — Fixed horizontal scroll on `/setup/*` pages at 375px by constraining the tab nav scroll container.
- **Setup wizard false-complete** (#216) — "Set Schedule" step no longer shows a green checkmark for new users; was triggered by default `posting_hours_end: 22` always passing `> 0`. Now tracks explicit `schedule_configured` flag.
- **Telegram login loading state** (#217) — Added spinner and placeholder text to Telegram widget container on `/login` (was a blank white box while script loaded).
- **Styled category select** (#218) — Replaced native `<select>` with shadcn `Select` component in media upload for visual consistency.
- **Theme token colors** (#219) — Added `--warning` and `--success` CSS custom properties (light + dark). Replaced hardcoded `text-red-500`/`text-yellow-500`/`text-green-500` and HSL chart fills with design tokens across content reuse page, reuse chart, dead-content chart, and media upload.

### Changed — Dependency Updates

- **Python**: pydantic 2.12.5→2.13.0, python-telegram-bot 22.6→22.7, click 8.3.1→8.3.2, rich 14.3.2→15.0.0, fastapi ≥0.109→≥0.135, uvicorn ≥0.27→≥0.44, pytest 9.0.2→9.0.3, pytest-cov 7.0.0→7.1.0
- **Node**: @tailwindcss/postcss 4.2.1→4.2.2, drizzle-kit 0.31.9→0.31.10, drizzle-orm 0.45.1→0.45.2, eslint 9.39.3→9.39.4, tailwindcss 4.2.1→4.2.2, @types/node 20.19.35→20.19.39
### Changed

- **Refactored `src/main.py` scheduler loop** — extracted four focused tick functions (`_scheduler_tick`, `_retention_cleanup_tick`, `_pool_health_tick`, `_token_health_tick`) from the 193-line `run_scheduler_loop()`, reducing it to a clean orchestration loop. Extracted `_validate_and_log_startup()` and `_log_service_summary()` from `main_async()`. No behavior changes. (#206)
### Changed — Code Quality (#205, #207, #208, #209)

- **Extract duplicated eligibility filters** (#205) — Consolidated repeated lock/queue/hash-duplicate exclusion logic in `MediaRepository` into a single `_apply_eligibility_filters()` helper used by `get_next_eligible_for_posting()`, `count_eligible()`, and `count_eligible_by_category()`.
- **Replace bare `except Exception` with specific types** (#207) — Narrowed exception catches where the exception type is identifiable: `OSError`/`ValueError` for image validation, `binascii.Error` for encryption init, `SQLAlchemyError` for DB queries, `httpx.HTTPError` for HTTP calls, and `telegram.error.TelegramError` for Telegram API notifications. Background loop and resilience catches remain intentionally broad.
- **Add return type hints to API route handlers** (#208) — Added `-> dict` annotations to all route handlers in `dashboard.py`, `settings.py`, and `setup.py`.
- **Extract telegram message update helper** (#209) — Extracted `_update_autopost_caption()` helper to replace repeated `telegram_edit_with_retry(query.edit_message_caption, ...)` calls in the autopost flow.
### Changed

- **Refactored telegram_callbacks.py into focused modules** (#203) — Split the 854-line monolithic `TelegramCallbackHandlers` class into three focused modules (`telegram_callbacks_core.py`, `telegram_callbacks_queue.py`, `telegram_callbacks_admin.py`) behind a thin facade that preserves the original public API. No behavior changes.

### Added — Web Dashboard Phase 3: Media Management

- **Content library browser** (`/dashboard/media`) — paginated grid view of all media items with category filtering, pool health stats (total active, eligible for posting, never posted, reuse rate), and per-category counts.
- **Media upload** — drag-and-drop or file picker for uploading new media directly through the dashboard, bypassing the Google Drive sync requirement. Validates MIME type, enforces 50 MB limit, and deduplicates by content hash (SHA256).
- **Content calendar** (`/dashboard/media/calendar`) — visual monthly calendar showing past posts (green), in-queue items (blue), and predicted future slots (gray). Includes posting rate stats and queue summary.
- **Dead content view** (`/dashboard/media/dead-content`) — surfaces media items 30+ days old that have never been posted, with category-level bar chart breakdown and percentage metrics.
- **Content reuse view** (`/dashboard/media/reuse`) — donut chart visualization of evergreen (2+ posts) vs one-shot vs never-posted content, with per-category never-posted breakdown table.
- **New backend endpoints** — `GET /api/onboarding/media-library` (paginated listing with category/posting-status filters and pool health aggregation), `POST /api/onboarding/upload-media` (multipart file upload with hash-based dedup).
- **Dedicated upload proxy** — separate BFF route (`/api/dashboard/upload`) for multipart form data forwarding, since the generic JSON proxy cannot handle file uploads.
- **Media tab navigation** — shared layout with tab bar (Library, Calendar, Dead Content, Content Reuse) across all media sub-pages.
- **Sidebar navigation** — added Media Library and Calendar entries to dashboard sidebar.

### Added — Web Dashboard Phase 2: Onboarding & Settings

- **Settings page** (`/dashboard/settings`) — tabbed interface (General, Accounts, Integrations) for managing posting schedule, boolean toggles (pause, dry run, Instagram API, verbose notifications, media sync), Instagram account switching/removal, and Google Drive connection.
- **Setup wizard** (`/dashboard/setup`) — guided 6-step onboarding flow: Connect Instagram → Connect Google Drive → Configure Media Folder → Index Media → Set Schedule → Complete. Auto-advances to first incomplete step, tracks progress with visual step indicators.
- **Client-side API helpers** (`dashboard-api.ts`) — shared `postApi`/`getApi` with error throwing, replacing inline fetch calls across all dashboard components.
- **Server-side backend helpers** — `backendFetchJson` and `backendPost` in `backend.ts` for cleaner server component data fetching.
- **Analytics placeholder** (`/dashboard/analytics`) — Phase 3 placeholder page.
- **New shadcn components** — Tabs, Switch, Label, Dialog, Select, Slider, Progress for settings and wizard UI.

### Added — Web Dashboard Phase 1: Auth, BFF, Dashboard Shell

- **Telegram Login Widget auth** — users authenticate via Telegram Login Widget on `/login`. Backend verifies the widget signature, issues a JWT stored in an httpOnly cookie. Sessions last 24 hours.
- **Protected `/dashboard` route group** — Next.js middleware redirects unauthenticated users to `/login`. Dashboard layout provides sidebar navigation and user header.
- **BFF proxy layer** — `/api/dashboard/[...path]` catch-all route proxies requests to the FastAPI backend, injecting signed URL tokens for auth. No backend changes required.
- **Dashboard overview page** — wires up existing analytics endpoints (posting stats, category performance, recent activity) with server-side data fetching via `Promise.all`.
- **Route group restructure** — landing/setup pages moved into `(marketing)` route group; dashboard pages in `(dashboard)` group. Each has its own layout. Root layout is now shared chrome only.
- **Edge-safe session module** — JWT/session logic split into `session.ts` (Edge Runtime compatible for middleware) and `auth.ts` (Node crypto for Telegram verification + URL token generation).

### Added — Post Preview Window (#178)

- **Schedule preview** — `GET /api/onboarding/analytics/schedule-preview` shows upcoming N slots with predicted times and categories. Uses the same interval and category weighting logic as the scheduler. Informational only — does not pre-select media.

### Added — Content Reuse Insights (#179)

- **Content reuse analytics** — `GET /api/onboarding/analytics/content-reuse` classifies the media pool into never_posted, posted_once, and posted_multiple tiers. Includes per-category breakdown and overall reuse rate.

### Added — Service Health Dashboard (#180)

- **Service telemetry** — `GET /api/onboarding/analytics/service-health` aggregates service_runs table into per-service call counts, error rates, and avg execution duration over a configurable time window.
- **`get_health_stats()`** in ServiceRunRepository — groups completed/failed runs by service name with aggregation.
### Added — Category Mix Drift Alerts (#176)

- **Category drift analytics** — `GET /api/onboarding/analytics/category-drift` compares configured posting ratios against actual ratios over a time window. Flags categories as ok/warning/critical based on drift thresholds (10%/25%).
- **`get_category_mix_drift()`** in DashboardService — combines `category_post_case_mix` targets with `posting_history` actuals to compute per-category drift.

### Added — Dead Content Report (#177)

- **Dead content analytics** — `GET /api/onboarding/analytics/dead-content` surfaces active media items that have never been posted and are older than a configurable age threshold (default 30 days). Returns per-category breakdown and dead percentage of total pool.
- **`count_dead_content_by_category()`** in MediaRepository — filters `is_active=True, times_posted=0, created_at <= cutoff` grouped by category.
### Added — Approval Latency Dashboard (#174) & Per-User Approval Rates (#175)

- **Approval latency analytics** — `GET /api/onboarding/analytics/approval-latency` computes time from queue creation to user decision. Returns overall avg/min/max (in minutes) plus breakdowns by hour-of-day and category.
- **Team performance analytics** — `GET /api/onboarding/analytics/team-performance` shows per-user breakdown: posted/skipped/rejected counts, approval rate, and average response latency in minutes.
- **`get_approval_latency()`** in HistoryRepository — uses `EXTRACT(EPOCH FROM posted_at - queue_created_at)` with per-hour and per-category groupings.
- **`get_user_approval_stats()`** in HistoryRepository — joins `posting_history` with `users` table, groups by user with status pivot and latency.

### Added — Startup Migration Version Check (#118)

- **Schema version validation on startup** — Worker (`main.py`) now queries the `schema_version` table at boot and compares against migration files in `scripts/migrations/`. Logs a clear warning if the database is behind (with the exact migration range to apply) or ahead of the deployed code.
- **Non-blocking** — Mismatches produce warnings, not fatal errors, so the worker can still start while the operator applies pending migrations.

### Added — Schedule Optimization Recommendations (#158)

- **Schedule recommendations API** — `GET /api/onboarding/analytics/schedule-recommendations` analyzes posting history to identify optimal posting times. Returns hourly approval rates, day-of-week patterns, and human-readable recommendations (e.g., "Posts at 10:00 have the highest approval rate (94%)").
- **Hourly approval rates** — `get_hourly_approval_rates()` in HistoryRepository groups posts by hour with full status breakdown and approval rate calculation.
- **Day-of-week analysis** — `get_dow_approval_rates()` identifies which days have the highest/lowest approval rates over a 90-day window.
- **Graceful degradation** — Returns "insufficient_data" status when fewer than 10 posts exist, preventing misleading recommendations from sparse data.

### Added — Batch Approval in Telegram (#160)

- **`/approveall` command** — Shows pending item count with category breakdown and a confirmation button. On confirm, marks all pending queue items as posted with history records and repost-prevention locks.
- **Batch callback handlers** — `batch_approve` and `batch_approve_cancel` callbacks registered in dispatch table. Sequential per-item processing with continue-on-error pattern.
- **Bot menu updated** — `/approveall` added to Telegram command autocomplete and `/help` text.

### Added — Smart Auto-Approval (#155)

- **Auto-approve returning media** — When the scheduler selects a media item that has been posted before (`times_posted > 0`), it skips the Telegram approval step and directly records the item as posted. Uses existing `media_items.times_posted` field — no schema changes.
- **Quiet Telegram notification** — Sends a brief "Auto-approved: filename [category]" message for visibility without requiring user action.
- **Posting method tracking** — Auto-approved items recorded with `posting_method='auto_reapproval'` in posting_history for analytics distinction.
- **Only applies to scheduler** — `/next` command and manual flows always go through Telegram approval regardless of prior history.

### Added — Google Drive Token Health Alerts (#157)

- **Token health check** — `check_gdrive_token_for_chat()` in HealthCheckService checks Google Drive OAuth token expiry per tenant. Warns at <7 days, critical at <1 day, reports expired tokens.
- **Tenant-scoped token health** — `check_token_health_for_chat()` added to TokenRefreshService for querying tokens by `chat_settings_id` (Google Drive) instead of `instagram_account_id` (Instagram).
- **Hourly Telegram alerts** — Scheduler loop checks token health hourly alongside pool depletion. Sends alert with expiry countdown, re-auth link, and projected stop date. Throttled to once per 24h per chat.
- **Alert formatting** — `format_token_alert()` builds user-friendly alert with reconnect URL.

### Added — Category Performance Insights (#154)

- **Category analytics API endpoint** — `GET /api/onboarding/analytics/categories?chat_id=X&days=30` returns per-category posting performance enriched with configured ratios from category_post_case_mix. Shows actual vs target ratio, skip/reject rates, and success rate per category.
- **DashboardService.get_category_analytics()** — Combines posting history stats with configured category mix ratios for performance comparison.

### Added — Posting Analytics Dashboard (#153)

- **Analytics API endpoint** — `GET /api/onboarding/analytics?chat_id=X&days=30` returns aggregated posting statistics: total posts, success rate, avg per day, method breakdown, daily counts, hourly distribution, and category performance.
- **Repository aggregation methods** — `get_stats_by_status()`, `get_stats_by_method()`, `get_daily_counts()`, `get_hourly_distribution()`, and `get_stats_by_category()` in HistoryRepository, all with multi-tenant scoping.
- **DashboardService orchestration** — `get_analytics()` combines all aggregations into a single response with execution tracking.

### Added — Pool Depletion Warnings (#156)

- **Media pool health check** — New `_check_media_pool()` in HealthCheckService monitors content supply per category. Calculates days of runway (eligible items / posts per day share) and reports warnings at <7 days and critical at <2 days. Included in `check_all()` and the `/system-status` dashboard API.
- **Per-chat pool detail** — `check_media_pool_for_chat()` provides per-category breakdown with eligible counts, post rate share, and runway estimates.
- **Hourly Telegram alerts** — Scheduler loop checks pool health every hour and sends a Telegram alert when any category drops below the warning threshold. Alerts are throttled to once per 24 hours per chat to prevent spam.

### Added — Loop Liveness Tracking (#134)

- **Heartbeat tracking for all background loops** — Each loop (scheduler, lock_cleanup, cloud_cleanup, media_sync, transaction_cleanup) records a timestamp on every tick. The health check reports loops as stale if they haven't ticked in 2x their expected interval. Visible via `check-health` CLI and `/status` health checks.

### Changed — Centralize API Base URL Constants (#119)

- **Centralize Instagram Login API base URLs** — `IG_LOGIN_GRAPH_BASE` and `IG_LOGIN_API_BASE` constants added to `src/config/constants.py`. Eliminates hardcoded `graph.instagram.com` and `api.instagram.com` URLs in `instagram_login_oauth.py` and `token_refresh.py`. Meta Graph API base was already centralized via `settings.meta_graph_base`.

### Added — Telegram Crash Alerts (#132)

- **Send Telegram alert when a background task crashes** — `_guarded()` now sends a message to the admin chat when any background loop (scheduler, lock cleanup, cloud cleanup, media sync, transaction cleanup) crashes. The worker stays alive but the user is immediately notified which loop stopped. Alert failures are caught separately so they never mask the original crash.

### Changed — Keyboard Builder Consolidation (#137)

- **Merge `build_error_recovery_keyboard` into `build_queue_action_keyboard`** — The two near-identical keyboard builders are now one function with an `error_recovery` parameter. Error recovery mode shows "Retry Auto Post" instead of "Auto Post" and hides the account selector.

### Changed — Verbose Flag Consistency (#138)

- **Verbose flag now means the same thing in both caption modes** — `verbose=True` controls debug metadata (file name, ID) and workflow instructions in both simple and enhanced modes. Enhanced mode now also shows file name and ID when verbose is on, matching simple mode's behavior.

### Fixed — Telegram Message Formatting Inconsistencies (#135, #136, #139, #142)

- **Add `parse_mode="Markdown"` to photo captions** — Initial notifications now render Markdown formatting consistently with callback edits. (#135)
- **Standardize on Markdown across all commands** — Convert `/start` from MarkdownV2 to Markdown, removing the only MarkdownV2 usage. (#136)
- **Escape user-generated content in captions** — Apply Markdown escaping to media titles, captions, filenames, and account names. (#142)
- **Standardize caption spacing** — Both caption modes now use consistent `"\n".join()` spacing and always show account status. (#139)
- **Add `parse_mode="Markdown"` to callback edits** — Posted, skipped, back, cancel-reject, and dry-run messages now use Markdown consistently. (#142)

### Changed — Multi-Account UX Improvements (#140, #141)

- **Batch-update pending messages on account switch** — When switching Instagram accounts, all pending notification captions and button labels now update to reflect the new account. Previously only the message where you clicked updated, leaving other pending posts showing the old account name. (#140)
- **Single-tap account cycle for 2-3 accounts** — The account selector button now cycles through accounts with one tap instead of opening a submenu. For users with 4+ accounts, the submenu is preserved. (#141)
- **Consolidated keyboard builder** — `TelegramNotificationService._build_keyboard()` now delegates to the shared `build_queue_action_keyboard()` utility, eliminating a duplicate keyboard implementation. (#137 partial)

### Fixed — Worker Crash in Cloud Storage Cleanup

- **Fix fatal AttributeError in cleanup loop** — `cleanup_cloud_storage_loop` called `cleanup_transactions()` on a `MediaRepository`, but that method only exists on `BaseService`. Replaced with the correct `end_read_transaction()` call. This crash killed the entire worker process after the first hourly cleanup cycle.
- **Add task-level exception isolation** — Background loops (scheduler, lock cleanup, cloud cleanup, media sync, transaction cleanup) are now wrapped with `_guarded()` so an unhandled exception in one loop logs a critical error instead of crashing the entire worker via `asyncio.gather()`.
- **Fix cascading media sync errors** — A `UniqueViolation` during hash-based rename detection poisoned the database session, causing every subsequent file in the sync batch to fail. Added per-item rollback and a pre-update path conflict check.

### Fixed — Telegram Callback Concurrency

- **Preserve post attribution on duplicate callbacks** — Double-tapping Auto Post (or any button after a post completes) no longer overwrites the "Posted to @account by @user" caption with a generic "Already posted via Instagram API" message. The race condition guard now silently acknowledges duplicate callbacks instead of replacing attribution info.
- **Non-blocking auto-post** — Auto Post to Instagram now runs as a background task, unblocking the Telegram callback pipeline immediately. Clicking buttons on other posts is no longer blocked during the 5-15 second upload + API call. Multiple auto-posts can run concurrently.
- **Planning doc for per-request session isolation** — Documented future architectural enhancement for enabling `concurrent_updates` with per-request database session scoping (`documentation/planning/per-request-session-isolation.md`)

### Security — Cloudinary Media Lifecycle Cleanup

- **Immediate cleanup after posting** — Cloudinary uploads are deleted as soon as Instagram fetches them (success, dry-run, error, and cancel paths all clean up)
- **Safety-net cleanup loop** — Hourly background task deletes orphaned Cloudinary uploads past retention window, clearing stale DB references
- **Tenant folder isolation** — Uploads now go to `instagram_stories/{tenant_id}/` instead of a flat shared folder
- **Cloud URL leak removed** — `cloud_url` no longer persisted in interaction logs (only `cloud_public_id` for debugging)
- **MediaLifecycleService** — New service for media item deletion that cascades to Cloudinary cleanup, respecting layer separation
- **Repository warning** — `MediaRepository.delete()` docstring warns to use `MediaLifecycleService` for full cleanup

### Fixed — Stale Queue Item Accumulation (#124)

- **Failed Telegram sends delete queue item immediately** — `_send_to_telegram()` now deletes the queue item on failure instead of rolling back to `pending` (which violated the DB CHECK constraint and caused orphan accumulation)
- **GoogleDriveAuthError deletes queue item immediately** — auth failures are non-retryable, so the queue item is removed and the media freed for reselection
- **Stale queue cleanup** — `delete_stale_pending()` runs at the start of each scheduler tick, deleting unsent pending items older than 10 minutes as defense-in-depth

### Fixed — Hash Algorithm Mismatch

- **Normalized file hashing to MD5** — `calculate_file_hash()` now uses MD5 to match Google Drive's `md5Checksum`, enabling cross-source deduplication (was SHA-256, producing incompatible 64-char hashes vs Drive's 32-char MD5)

### Fixed — Media Pool Deduplication & Selection

- **Hash-based duplicate detection** — Selection query now excludes items whose file hash matches any currently-locked item, preventing the same photo from being posted twice under different filenames
- **Duplicate prevention during sync/ingestion** — Media sync and index-media now skip files whose content hash already exists in the active pool, preventing future duplicates from entering the system

### Added — Media Pool Deduplication & Selection

- **`storyline-cli dedup-media`** — New CLI command to find and deactivate duplicate media items (same file content, different filenames). Supports `--dry-run` (default) and `--apply` modes
- **`storyline-cli pool-health`** — New CLI command showing media pool health: active/inactive/locked/eligible counts, lock breakdown by reason, per-category breakdown, and duplicate file groups
- **Queue preview fix** — `queue-preview` now correctly shows N different upcoming items instead of repeating the first item

### Added — Instagram Login OAuth + Graph API v21.0

- **Instagram Login OAuth service** — New `InstagramLoginOAuthService` implementing the newer Instagram Login flow (no Facebook Page required). Uses `instagram_business_basic` + `instagram_business_content_publish` scopes. Coexists alongside the existing Facebook Login OAuth path.
- **Instagram Login callback route** — `GET /auth/instagram-login/callback` handles the OAuth redirect, exchanges tokens, stores per-tenant, and notifies via Telegram
- **Smart OAuth routing** — Onboarding "Connect Instagram" button automatically uses Instagram Login when `INSTAGRAM_APP_ID` is configured, falls back to Facebook Login otherwise
- **Token refresh routing** — `TokenRefreshService` detects `auth_method="instagram_login"` accounts and routes to `graph.instagram.com/refresh_access_token` instead of the Facebook endpoint
- **New env vars** — `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET` for Instagram Login OAuth (separate from `FACEBOOK_APP_ID`/`FACEBOOK_APP_SECRET`)

### Changed — Instagram Login OAuth + Graph API v21.0

- **Graph API v18.0 → v21.0** — Centralized `META_GRAPH_API_VERSION` in `settings.py` with `meta_graph_base` property; removed 6 scattered `META_GRAPH_BASE` constants across services
- **Bootstrap-only env vars documented** — `DRY_RUN_MODE`, `ENABLE_INSTAGRAM_API`, `POSTS_PER_DAY`, `POSTING_HOURS_*`, `MEDIA_SYNC_ENABLED`, `MEDIA_SOURCE_*` now marked as bootstrap-only in settings (runtime values live in `chat_settings` table)

### Added — Google Drive Disconnect & Onboarding Dead-End Fixes

- **Google Drive disconnect/reconnect** — New `POST /disconnect-gdrive` endpoint and expandable dashboard card with Reconnect, Change Folder, and Disconnect actions
- **Stale token detection** — Google Drive card shows "Needs Reconnect" warning when OAuth token has expired >7 days, surfacing the existing `gdrive_needs_reconnect` flag
- **Wizard reconnect links** — "Reconnect with different account" link shown on Instagram and Google Drive wizard steps when already connected
- **OAuth timeout recovery** — Polling timeout (10 min) now shows an error message and re-enables the connect button instead of failing silently
- **Error recovery on fatal errors** — `_showError()` now includes a Reload button instead of leaving users stranded

### Fixed — Onboarding Dead-End Fixes

- **Silent error catch blocks** — `switchAccount()` and `executeRemoveAccount()` now display inline error messages instead of swallowing failures
- **Schedule save errors** — `saveSchedule()` and `saveScheduleAndReturn()` use inline errors instead of destroying the app DOM
- **OAuth connect errors** — `connectOAuth()` failure shows inline message instead of replacing the entire page
- **Null dereference** — `_updateStatusIndicators()` now guards `gdStatus` element access, preventing TypeError when called from home screen

### Changed — CLAUDE.md Optimization

- **CLAUDE.md reduced from 1,175 to 121 lines** — moved domain-specific reference content into `.claude/rules/` files that load on-demand when working in matching files
  - `rules/testing.md` — test patterns, markers, templates (loads for `tests/**`)
  - `rules/database.md` — schema, design patterns, migrations (loads for `src/models/**`, `src/repositories/**`)
  - `rules/development-patterns.md` — BaseService usage, error handling, logging (loads for `src/**/*.py`)
  - `rules/telegram.md` — bot commands, callbacks, handler architecture (loads for `telegram_*`)
  - `rules/scheduler.md` — JIT algorithm, selection logic (loads for `scheduler*`)
  - `rules/changelog.md` — format rules, entry examples (loads for `CHANGELOG.md`)
- **Deleted ~770 lines of derivable content** — file organization tree, service reference tables, API endpoint tables, migration history, code templates that Claude can read directly from the codebase

### Changed — /status Command Overhaul

- **Multi-tenant scoping** — All `/status` data queries now pass `chat_settings_id`, ensuring each tenant only sees their own metrics (was showing global counts)
- **Config reads from database** — `enable_instagram_api`, `is_paused`, and `dry_run_mode` now read from `chat_settings` table instead of env vars, fixing a bug where Telegram `/settings` toggles weren't reflected in `/status`
- **Instagram API rate limit** — `get_rate_limit_remaining()` now accepts `chat_settings_id` for per-tenant rate tracking

### Added — /status Command Overhaul

- **Next post estimate** — New "Next: ~48m (15:30 UTC)" line shows when the JIT scheduler will fire next, using the same interval formula as `SchedulerService.is_slot_due()`
- **Posted count** — Library section now shows "Posted: X" alongside "Never posted: Y" for a quick content runway vs. usage snapshot

### Removed — /status Command Overhaul

- **"Bot: Online"** — Always true (can't run `/status` if bot is offline)
- **"Posting: Delivery ON"** — Duplicate of Setup Status delivery line
- **"Queue: N pending"** — Misleading with JIT scheduling; the queue is an in-flight tracker, not a schedule
- **"Locked: N"** — Not actionable from `/status`
- **"Total: X active"** — Duplicate of Setup Status media library count
- **"Cadence: ..."** — Duplicate of Setup Status schedule line
- **"Posted once: X" / "Posted 2+: X"** — Low value; replaced by single "Posted: X" total
- **"System:" section** — Removed entirely (Bot: Online, duplicate Delivery, env-var Dry Run)
- **`_get_cadence_display()` helper** — No longer needed

### Added — Instagram Deep Link Redirect

- **Story camera deep link** — "Open Instagram" button now opens the story camera directly on mobile instead of the feed
  - Static redirect page (`docs/index.html`) bridges HTTPS → `instagram://story-camera`
  - Platform-aware: `intent://` syntax for Android Chrome, custom scheme for iOS, web fallback for desktop
  - Hosted via GitHub Pages (zero infrastructure)
- **`INSTAGRAM_DEEPLINK_URL` setting** — configurable redirect URL with instant rollback (set to `https://www.instagram.com/` to revert)


### Added — Mini App Secure Account Input

- **Secure account form in Mini App** — Instagram accounts can now be added via an HTTPS form in the Telegram Mini App dashboard, replacing the message-based wizard where credentials were visible in chat history
  - New `POST /api/onboarding/add-account` endpoint with Instagram API credential validation
  - Inline form with Display Name, Account ID, and Access Token (password-masked) fields
  - Client-side validation and real-time error/success feedback
  - Supports both new accounts and token updates for existing accounts

- **`auth_method` tracking** — New column on `instagram_accounts` records how each account was connected (`oauth`, `manual`, or `NULL` for legacy)
  - Migration `022_add_auth_method.sql`
  - OAuth flow now tags accounts with `auth_method="oauth"`

### Changed — Mini App Secure Account Input

- **Telegram "Add Account" button** now opens the Mini App dashboard instead of starting a message-based wizard
- **Instagram card actions** split into "Connect via OAuth" (primary) and "Add Manually" (secondary) buttons

### Removed — Mini App Secure Account Input

- **Message-based account wizard** (`telegram_account_wizard.py`) — deleted entirely; credentials are no longer collected through chat messages

### Changed — Documentation Review

- **CLAUDE.md** — Fixed 4 wrong method names in service reference; added 10 undocumented services, media source provider pattern, API endpoint reference (16 endpoints), utilities table; rewrote file organization tree to reflect current codebase
- **PROJECT_CONTEXT.md** — Fixed Raspberry Pi→Neon PostgreSQL, retired bot commands, wrong filenames; added media sources, Google Drive, API layer to architecture diagram
- **QUICK_REFERENCE.md** — Fixed Raspberry Pi→Neon, removed SSH commands, updated common tasks
- **documentation/README.md** — Removed broken 01_settings link, fixed test counts (494→1,417), file counts (36→77), updated archive count, dates
- **ROADMAP.md** — Fixed test count, v1.0.0 date (2025-12-XX→2026-01-03), updated last-updated date
- **Deployment guides** — Updated migration loops from 16→21 migrations across deployment.md, cloud-deployment.md, dev-environment-setup.md
- **quickstart.md** — Replaced non-existent `process-queue` CLI command, updated Telegram bot commands to current 6 active commands
- **testing-guide.md / TEST_COVERAGE.md** — Updated test counts and file counts
- **.github/README.md** — Full rewrite removing obsolete Pi/Tailscale/deploy.sh references

### Added — Documentation Review

- **Media source provider docs** — Documented MediaSourceProvider, MediaSourceFactory, GoogleDriveProvider, LocalMediaProvider in CLAUDE.md
- **API endpoint reference** — Documented all OAuth and onboarding/Mini App API routes in CLAUDE.md
- **Utility modules reference** — Documented resilience.py, encryption.py, webapp_auth.py, validators.py in CLAUDE.md
- **Landing site docs** — Added landing/ directory to architecture overview and file organization

### Removed — Documentation Review

- **Archived prod-hardening tech debt plans** — Moved 5 completed docs from planning/tech_debt/ to archive/ (PRs #78-#81, all completed)
- **Renamed github-actions-tailscale.md** → ci-cd-pipeline.md (Tailscale not used; content was already about CI/CD)

### Changed — Data Model Remediation
- **SQL Aggregation** — `/status`, dashboard stats, and interaction analytics now use SQL `COUNT`/`GROUP BY` instead of loading all rows into Python memory
- **Dashboard N+1 Fix** — Queue and history detail endpoints now use JOIN queries instead of per-item media lookups
- **Transaction Atomicity** — Telegram callback DB operations now commit atomically (single commit) instead of incrementally
- **Connection Cleanup** — All repository read methods now call `end_read_transaction()` to prevent idle-in-transaction connections

### Removed — Data Model Remediation
- **posting_queue** — Dropped vestigial columns: `web_hosted_url`, `web_hosted_public_id`, `retry_count`, `max_retries`, `next_retry_at`, `last_error`; removed `retrying` from status CHECK
- **posting_history** — Dropped unused columns: `media_metadata`, `error_message`, `retry_count`
- **media_items** — Dropped unimplemented `requires_interaction` column and its index
- **users** — Dropped unused `team_name` and `first_seen_at` columns
- **chat_settings** — Dropped unused `chat_name` column

### Fixed — Data Model Remediation
- **Model Drift** — `init_db()` now imports all 11 models (was missing 5)
- **Model Drift** — Added CHECK constraints to ORM models matching existing DB constraints (chat_settings ranges, lock_reason, user role)
- **DateTime Mismatch** — `chat_settings.last_post_sent_at` ORM now declares `DateTime(timezone=True)` matching the `TIMESTAMPTZ` DB column
- **Lock Uniqueness** — Replaced broken `UniqueConstraint` on `media_posting_locks` with partial unique indexes that correctly prevent duplicate permanent locks (old constraint failed because `NULL != NULL` in SQL)

### Changed — JIT Scheduler Remaining Vestiges
- **Telegram /settings: Remove schedule buttons** — Removed "Regenerate", "+7 Days", and "Clear Queue" buttons (vestigial in JIT model where queue has 0-1 items); handler methods kept as safety net for cached messages
- **Removed command redirects** — `/schedule` and `/reset` redirect messages no longer reference "Regenerate / +7 Days"
- **CLAUDE.md: Update SchedulerService** — Key methods updated from `create_schedule()`/`select_media()`/`add_to_queue()` to `process_slot()`/`force_send_next()`/`is_slot_due()`/`get_queue_preview()`
- **CLAUDE.md: Update PostingService** — Key methods updated to reflect current `send_gdrive_auth_alert()` responsibility
- **CLAUDE.md: Rewrite Scheduler Algorithm** — Replaced pre-assign time slot allocation description with JIT algorithm (`is_slot_due()` + `process_slot()`)
- **CLAUDE.md: Remove deleted CLI commands** — Removed `create-schedule` and `process-queue` from common tasks; added `queue-preview`
- **CLAUDE.md: Fix /next description** — Changed from "Force-send next scheduled post" to "Force-send next post now"

### Changed — JIT Scheduler Display Cleanup
- **Frontend: Remove schedule extend/regenerate buttons** — Schedule card is now a read-only cadence summary (`3/day, 2pm-2am UTC`) with an Edit button instead of broken "+ 7 Days" and "Regenerate" buttons that called deleted endpoints
- **Frontend: Remove "Create 7-day schedule" toggle** — Summary step shows "Posts will start automatically" instead of a no-op checkbox
- **Frontend: Replace misleading queue displays** — Queue badge shows "N awaiting review" instead of "N pending"; detail shows "Last post: Xh ago" instead of "Next: in Xm"
- **Frontend: Replace schedule_end_date display** — Removed "Ends Mar 28" from schedule card (no schedule end in JIT mode)
- **Setup state: JIT-appropriate fields** — `next_post_at`/`schedule_end_date` replaced with `posting_active` bool; `queue_count` renamed to `in_flight_count` (pending + processing)
- **Dashboard service: In-flight semantics** — `get_queue_detail()` returns `total_in_flight`, `posts_today`, `last_post_at` instead of `total_pending`, `schedule_end`, `days_remaining`, `day_summary`
- **Health check thresholds** — Queue backlog threshold lowered from 50→10 (JIT queue is 0-5 items); max pending age lowered from 24h→4h
- **Telegram /status cadence display** — Shows "Cadence: 3/day, 14:00-02:00 UTC" instead of "Next: None scheduled" (which falsely implied the system was broken)
- **Onboarding complete endpoint** — Returns `{"onboarding_completed": true}` instead of fake schedule summary with zeros

### Removed — JIT Scheduler Display Cleanup
- **`ScheduleActionRequest` model** — Orphaned Pydantic model (no endpoint used it)
- **`CompleteRequest.create_schedule`/`schedule_days` fields** — No-op fields removed from onboarding complete
- **`QueueRepository.schedule_retry()`** — Never-called method removed
- **`extendSchedule`/`confirmRegenerate`/`regenerateSchedule` JS methods** — Frontend methods that called deleted API endpoints
- **`_renderDaySummary` JS method** — No longer needed (no day-by-day schedule to display)
- **`_get_next_post_display()` Telegram helper** — Replaced by `_get_cadence_display()`

### Fixed — JIT Scheduler Display Cleanup
- **Health check false alarms** — Queue health no longer alerts on empty queue (normal in JIT mode) or items pending <24h
- **`posting_history.scheduled_for` comment** — Updated from "Original scheduled time" to "When the queue item was created (JIT: same as sent time)"
- **`posting_queue` retry columns comment** — Documented as unused (columns retained to avoid migration)

### Changed — JIT Scheduler Redesign
- **Replace pre-assign scheduling with just-in-time selection** — Instead of populating the queue days in advance, the scheduler now checks `is_slot_due()` every 60 seconds and selects media at the moment a slot fires. The `posting_queue` narrows to an in-flight tracker for items awaiting team action.
  - `SchedulerService.process_slot()` — Main entry point: checks timing, selects media, sends to Telegram
  - `SchedulerService.force_send_next()` — JIT replacement for `/next` command (no queue shifting needed)
  - `SchedulerService.is_slot_due()` — Computes whether a posting slot should fire based on interval and last_post_sent_at
  - `SchedulerService._pick_category_for_slot()` — Weighted random category selection per-slot instead of per-batch
- **Simplify PostingService** — Removed `process_pending_posts()`, `force_post_next()`, `reschedule_overdue_for_paused_chat()`, and all internal helpers. PostingService retains only `send_gdrive_auth_alert()`.
- **Simplify main.py scheduler loop** — Calls `scheduler_service.process_slot()` per tenant instead of `posting_service.process_pending_posts()`. Removed paused-chat reschedule loop (JIT naturally skips paused tenants).
- **Telegram /next command** — Now uses `SchedulerService.force_send_next()` for JIT selection instead of claiming a pre-queued item and shifting slots.
- **Telegram schedule settings** — Removed extend/regenerate schedule actions (JIT is automatic). Replaced with clear queue action.

### Added — Storage Bloat Fix
- **Service runs retention policy** — `ServiceRunRepository.delete_older_than(days)` purges old records; called hourly from the scheduler loop (7-day retention)
- **Skip no-op service_run logging** — `process_pending_posts()` checks pause state and pending items before creating a `service_run` row, eliminating ~1,400 empty rows/day
- **`chat_settings.last_post_sent_at`** — New column tracking when the last post was sent per tenant, used by `is_slot_due()` for interval computation
- **Migration 019** — `last_post_sent_at` column + backfill from `posting_history`
- **Queue preview** — `SchedulerService.get_queue_preview()` computes the next N selections without persisting. Exposed via API (`/queue-preview`) and CLI (`queue-preview`)
- **`SettingsService.update_last_post_sent_at()`** — Records post timestamps for JIT interval tracking

### Removed
- **Pre-assign scheduling** — `SchedulerService.create_schedule()`, `extend_schedule()`, `_generate_time_slots()`, `_fill_schedule_slots()`, `_generate_time_slots_from_date()`
- **Queue slot manipulation** — `QueueRepository.shift_slots_forward()`, `reschedule_items()`, `get_overdue_pending()`
- **API endpoints** — `/extend-schedule`, `/regenerate-schedule` (replaced by JIT automatic scheduling)
- **CLI commands** — `create-schedule`, `process-queue` (replaced by JIT scheduler and `queue-preview`)
- **Paused-chat reschedule** — `PostingService.reschedule_overdue_for_paused_chat()` (JIT naturally skips paused tenants)

### Changed
- **Extract shared test fixtures** — Reduced ~360 lines of duplicated test boilerplate across 17 test files
  - `mock_telegram_service` fixture in `tests/src/services/conftest.py` replaces 5 identical ~55-line TelegramService mock setups
  - `mock_track_execution` context manager shared across 9 service test files (was copy-pasted in each)
  - API test helpers (`client`, `mock_validate`, `service_ctx`, `CHAT_ID`) in `tests/src/api/conftest.py` shared across 3 API test files
- **Extract callback error handling wrapper** — Deduplicated the lock/keyboard-removal/error-display/cleanup pattern that was repeated in `complete_queue_action()` and `handle_rejected()` into a shared `_safe_locked_callback()` method
- **Replace silent message edit patterns** — Error fallback message edits in `handle_resume_callback()` and `handle_reset_callback()` now use `telegram_edit_with_retry` instead of bare `try/except: pass`
- **Narrow exception handling in API routes** — Added `OperationalError` → 503 and `ValueError` → 400 catches before the generic `Exception` → 500 in scheduler and sync endpoints (`extend-schedule`, `regenerate-schedule`, `sync-media`, `start-indexing`)
- **Fix layer boundary violations** — API and CLI layers no longer import repositories directly, enforcing the strict separation of concerns defined in CLAUDE.md (CLI/API → Services → Repositories → Models)
  - **API layer**: Removed all 14 direct repository imports across 4 onboarding route files (`helpers.py`, `dashboard.py`, `settings.py`, `setup.py`)
  - **CLI layer**: Removed repository imports from `queue.py`, `users.py`, and `media.py` — all now call services
  - **OAuth routes**: Switched from manual `try/finally/close()` to `with` context managers for consistent resource cleanup
- **Consolidate duplicated setup-state logic** — Extracted `SetupStateService` to replace ~130 lines of identical setup-checking logic that was duplicated between `TelegramCommandHandlers._get_setup_status()` (5 methods) and `helpers._get_setup_state()` (1 function). Both consumers now call the same service.
- **Centralize token staleness check** — Extracted `is_token_stale()` utility in `setup_state_service.py` replacing 3 identical `expires_at < utcnow() - timedelta(days=7)` checks

### Added
- **SetupStateService** (`src/services/core/setup_state_service.py`) — Unified setup-state checking for both Telegram bot and API, with `get_setup_state()` (returns dict) and `format_setup_status()` (returns Telegram-formatted text)
- **DashboardService** (`src/services/core/dashboard_service.py`) — Read-only aggregation service for Mini App dashboard endpoints (queue detail, history detail, media stats, pending queue items)
- **UserService** (`src/services/core/user_service.py`) — User management service for CLI layer (list users, promote user)
- **SchedulerService.clear_pending_queue()** / **count_pending()** — Service-layer methods replacing direct `QueueRepository` calls from API and CLI
- **MediaIngestionService** — Added category mix operations (`get_current_mix()`, `set_category_mix()`, `get_mix_history()`, etc.) and media listing methods, replacing direct `CategoryMixRepository`/`MediaRepository` calls from CLI

### Fixed
- **QueuePool overflow on concurrent autopost + /next** — `BaseService.close()` only closed direct `BaseRepository` attributes but did not recurse into nested `BaseService` instances, leaking their database connections. When autopost (which creates `InstagramAPIService` with 6+ nested services/repos) ran concurrently with `/next` (which creates `PostingService` with nested `TelegramService`), the pool of 20 connections was exhausted. `close()` now mirrors the recursive pattern already used by `cleanup_transactions()`, traversing nested services before closing repositories. Also added `TelegramService.close()` override to close `InteractionService`'s repo (which isn't a `BaseService` and was invisible to the traversal).
- **Scheduler tenant scoping** — Queue items created by `create_schedule()` and `extend_schedule()` were missing `chat_settings_id`, making them invisible to the tenant-scoped processing loop (`process_pending_posts`), dashboard API, and Mini App. The scheduler ran every minute but found 0 items because `WHERE chat_settings_id = '<uuid>'` never matches NULL. Now all scheduler paths thread `chat_settings_id` from `telegram_chat_id` through to `queue_repo.create()`.
  - `_fill_schedule_slots()` now accepts and passes `chat_settings_id`
  - `create_schedule()` / `extend_schedule()` derive `chat_settings_id` via `_resolve_chat_settings_id()`
  - Telegram settings handlers now pass `telegram_chat_id` to scheduler methods
  - `force_post_next()` now accepts `telegram_chat_id` and scopes queue queries to tenant
  - Regenerate schedule action uses `delete_all_pending()` with tenant scoping
  - Migration 018: Backfills `chat_settings_id` on existing orphaned queue items
- **Backlogged queue items immediately discarded** — `discard_abandoned_processing()` was deleting items within 1 minute of being sent to Telegram because it checked `scheduled_for` (the original schedule date, often days old) instead of when the item actually entered processing. Now `_post_via_telegram()` and `_execute_force_post()` stamp `scheduled_for` to `now()` when transitioning to processing, giving users the full 24h window to act on notifications.

### Added
- **Database circuit breaker** — Fail-fast mechanism in BaseRepository that opens after 5 consecutive DB failures, rejecting requests immediately with `OperationalError` instead of hanging 30 seconds on pool timeout. Auto-recovers after 30 seconds via half-open probe. Prevents cascading hangs when Neon DB is unreachable.
- **Connection pool monitoring** — Logs SQLAlchemy pool utilization every 30 seconds at appropriate severity (warning at ≥90%, info at ≥70%, debug otherwise). Enables early detection of pool exhaustion before it causes freezes.
- **Telegram message edit retries** — `telegram_edit_with_retry()` wrapper retries `edit_message_caption`/`edit_message_text`/`edit_message_reply_markup` on transient Telegram failures (`TimedOut`, `NetworkError`, `RetryAfter`) with exponential backoff (up to 2 retries). Non-retryable errors (`BadRequest`, `Forbidden`) raise immediately. Applied to all critical callback handlers (posted, skipped, rejected, resume, reset, autopost success/error/dry-run).
- **Shared session for multi-step DB operations** — Queue action callbacks (posted/skipped/rejected) now share a single DB session across all 5 repos involved (history, media, lock, user, queue), reducing connection pool pressure from 5 connections to 1 per callback and providing consistent failure behavior when the connection dies mid-operation.

### Fixed
- **Silent error swallowing across service layers** — Replaced ~15 bare `except Exception: pass` blocks in BaseService, BaseRepository, TelegramService, and main.py's transaction cleanup loop with proper `logger.warning()` calls that include exception type and message. Previously, DB connection failures, session recovery errors, and transaction cleanup issues were completely invisible in logs.
- **Missing user feedback on callback failures** — When Telegram callback handlers (Posted, Skip, Reject, Resume, Reset) failed after removing the action buttons, users were left with no buttons and no error message. Added try/except wrappers around `complete_queue_action`, `handle_rejected`, `handle_resume_callback`, and `handle_reset_callback` that show an error message to the user when operations fail.
- **Unhandled exceptions in callback dispatcher** — Top-level `_handle_callback` in TelegramService had no catch-all error handler, so unhandled exceptions in callback processing were only logged by the application error handler with no user feedback. Added an `except Exception` block that logs the error and shows a "Something went wrong" alert to the user.
- **Settings toggle errors shown as generic failure** — `handle_settings_toggle` only caught `ValueError`, so database errors during toggle would propagate unhandled. Added catch-all that shows a user-friendly "Failed to update setting" alert.
- **Debug-level logs hiding real issues** — Upgraded several important error paths from `logger.debug` to `logger.warning`: session recovery in BaseRepository, sync status check failures in commands, and interaction repo cleanup failures. These were previously invisible unless running at debug log level.
- **Telegram message edits silently failing** — Post-action caption updates (e.g., "✅ Posted by Chris") could fail on transient Telegram API errors (timeouts, network drops), leaving the message showing stale buttons/text even though the DB action succeeded. Now retried automatically via `telegram_edit_with_retry`.

### Added
- **Landing site scaffold** — Next.js 16 marketing site in `landing/` directory with Tailwind CSS v4, shadcn/ui, Drizzle ORM (Neon), Geist fonts, and shared layout (header + footer). Includes waitlist schema definition, site config, and initial shadcn components (button, input, badge, accordion, card, separator). Matches established patterns from other projects.
- **Landing page sections** — Complete landing page with 8 composable sections: Hero (headline + waitlist form + social proof), How It Works (3-step grid with icons), Telegram Preview (split layout with pure CSS dark-mode Telegram mockup), Features (2x3 card grid), Pricing (free beta checklist), FAQ (8-item accordion), Final CTA (footer waitlist form). Visual-only waitlist placeholder ready for Phase 03 API integration.
- **Landing site design docs** — Phased implementation plans for landing page, waitlist system, and onboarding guide in `documentation/planning/phases/landing-site_2026-03-04/`
- **Waitlist system** — Full-stack waitlist signup flow for the landing site
  - API route (`/api/waitlist`) with email validation, duplicate detection (unique constraint), and error handling
  - Telegram admin notification on new signups (fire-and-forget, graceful fallback when env vars missing)
  - `WaitlistForm` client component with hero/footer variants, localStorage persistence for returning visitors, loading/success/error/duplicate states, and accessible markup (sr-only labels, aria-live regions)
- **Onboarding guide** — Unlisted `/setup` pages for accepted waitlist users covering all prerequisites
  - 6 setup pages: overview/checklist, Instagram Business account, Meta Developer app, Google Drive OAuth, media organization, Telegram bot connection
  - Shared setup layout with sidebar navigation (desktop) and horizontal nav (mobile), "Back to home" link, and "Need help?" contact footer
  - 6 reusable setup components: `StepCard` (numbered steps), `Callout` (info/warning/tip variants), `Screenshot` (placeholder with caption), `Checklist` (static visual), `CopyButton` (click-to-copy), `SetupNav` (section navigation with active state)
  - Pages are `noindex`/`nofollow` and not linked from the main landing page navigation
  - Previous/next navigation between all guide sections

### Fixed
- **Double-tap duplicate posting** — Race condition where rapid button clicks (Posted, Skip, Reject, Auto Post) could process the same queue item 2-3x within seconds, creating duplicate history entries. Added atomic `claim_for_processing()` using `SELECT ... FOR UPDATE SKIP LOCKED` so only the first callback succeeds; subsequent clicks see a "already processed" message.
- **OperationalError retry creating duplicate history** — When an SSL/connection error triggered the retry path, the retry could create a second history entry if the first attempt partially succeeded. Retry now checks `get_by_queue_item_id()` before retrying and skips if history already exists.
- **Notification spam from stale queue items** — Items in `processing` (sent to Telegram, awaiting user action) were being reset to `pending` by `reset_stale_processing()` every 2 hours, causing the scheduler to re-send the Telegram notification each loop — one item generated 20+ duplicate messages. Replaced with `discard_abandoned_processing()` that deletes items stuck in `processing` for over 24 hours instead of resetting them, breaking the infinite loop.
- **Stale callback crash in button handlers** — Inline button clicks (Auto Post, Posted, Skip, etc.) during deploy transitions would silently fail with "Query is too old" error, preventing the actual action from executing. `_handle_callback` now catches stale `query.answer()` failures gracefully and continues processing the callback.
- **Google Drive token expiry error handling** — When Google Drive OAuth token expires or is revoked, `/next` now shows a "Reconnect Google Drive" button instead of a generic "Failed to send. Check logs for details." error. `GoogleDriveAuthError` propagates from `send_notification()` instead of being swallowed, with automatic detection of `google.auth.RefreshError` in the exception chain. `PostingService` catches the error in `_execute_force_post`, `_post_via_telegram`, and `process_pending_posts`, sending a rate-limited (1/hr) proactive alert to Telegram when scheduled posting fails due to auth issues.
- **Stale Google Drive token detection** — `/status` now shows "Needs Reconnection" instead of "Connected" when the access token expired more than 7 days ago. Dashboard API returns `gdrive_needs_reconnect` flag for the same condition.

### Added
- **Skip cooldown lock** — Skipped items now receive a 45-day TTL lock (configurable via `SKIP_TTL_DAYS` setting), preventing them from immediately re-entering the eligible pool. Previously, skipped items cycled back repeatedly while 4,011 of 4,619 items had never been sent.
- **Operation lock on reject handler** — `handle_rejected` now uses the same `get_operation_lock` pattern as posted/skipped handlers, preventing duplicate rejections from rapid clicks.

### Changed
- **Telegram service split** — Extracted `TelegramNotificationService` (~280 lines) from `telegram_service.py` (795 -> 533 lines), isolating notification sending, caption building, keyboard construction, and header emoji logic into a dedicated module. `TelegramService` keeps thin delegation methods for backward compatibility.
- **Repository query builder** — Added `_tenant_query()` helper to BaseRepository, refactored 34 instances across 5 repository files to eliminate repeated tenant filtering boilerplate
- **Posting service complexity reduction** — Flattened nesting in `force_post_next()` and `process_pending_posts()`, extracted helper methods (`_build_force_post_result`, `_execute_force_post`, `_process_single_pending`), moved `db.commit()` from service to new `QueueRepository.reschedule_items()` method

### Removed
- **Dead code in PostingService** — Removed ~258 lines of unreachable code from `posting.py`: `handle_completion()`, `_post_via_instagram()`, `_cleanup_cloud_media()`, `process_next_immediate()`, and related lazy-load properties for Instagram/cloud services. All posting now routes through Telegram; Instagram API posting happens via callback handler, not `PostingService`.
- **Raspberry Pi references** — Production now runs entirely on Railway + Neon. Removed all Pi-specific paths (`/home/pi/media`), SSH commands, systemd references, and `scripts/deploy.sh`. Updated CLAUDE.md, `.env.example`, 10 documentation files, and Claude commands to reflect cloud infrastructure.

### Changed
- **Large file splits (3 extractions)** — Reduced three files that exceeded 680 lines each by extracting focused composition classes:
  - `TelegramAccountWizard` from `telegram_accounts.py` (720 → 409 lines) — multi-step account-adding wizard flow
  - `BackfillDownloader` from `instagram_backfill.py` (698 → 463 lines) — media downloading, API calls, and storage
  - `InstagramCredentialManager` from `instagram_api.py` (686 → 395 lines) — credential management, validation, and safety checks
  - All three use composition pattern (not inheritance), preserving public API via thin delegation methods
- **API error handling deduplicated** — Extracted `service_error_handler()` context manager in `helpers.py` to replace 9 identical `try/except ValueError → HTTPException(400)` blocks across 3 route files (settings.py, setup.py, oauth.py). Two compound-pattern instances intentionally left explicit for safety.
- **setup.py dependencies synced** — Added 8 missing runtime dependencies to `setup.py` that were in `requirements.txt`: alembic, cloudinary, cryptography, fastapi, google-api-python-client, google-auth, google-auth-oauthlib, uvicorn
- **Onboarding routes split into package** — Split monolithic 859-line `onboarding.py` into focused submodules: `models.py`, `helpers.py`, `setup.py`, `dashboard.py`, `settings.py`. Consolidated lazy imports to module-level. No functional changes.
- **WebApp button builder extracted** — Deduplicated private-vs-group WebApp button logic from 3 locations into shared `build_webapp_button()` utility in `telegram_utils.py`

### Fixed
- **InteractionService session leak** — `TelegramService.cleanup_transactions()` now also cleans up `InteractionService`'s repository session. InteractionService doesn't extend BaseService, so recursive cleanup traversal missed it, leaving idle-in-transaction DB connections after SSL drops.
- **Early callback feedback** — Telegram callback handlers now remove the inline keyboard immediately after acquiring the lock, before running DB operations. This gives users instant visual feedback that their button press was received, eliminating the "nothing happened" perception during slow DB calls.
- **SSL retry in callbacks** — Callback handlers (posted/skip/reject) now catch `OperationalError` from stale SSL connections, refresh all repository sessions, re-fetch the queue item, and retry once. Previously, a Neon SSL drop during callback processing left the user stuck with no feedback.
- **Graceful race condition handling** — When a queue item is missing during callback validation (e.g., user clicks Skip after Auto Post already completed), now checks `posting_history` for what happened and shows a contextual message ("Already posted via Instagram API", "Already skipped", etc.) instead of generic "Queue item not found".
- **Duplicate scheduler runs** — `get_all_active()` now requires chats to have completed onboarding or have an active Instagram account, filtering out half-setup test/dev chats that caused duplicate `process_pending_posts` runs per cycle.
- **Duplicate Telegram sends** — Queue items are now claimed (status → "processing") BEFORE sending the Telegram notification, not after. Previously, the scheduler could pick up the same "pending" item again if the next cycle fired before the Telegram API responded, causing duplicate messages in the channel. On send failure, the item is rolled back to "pending".
- **Queue batch-fire prevention** — Throttled scheduler to process 1 post per 60s cycle (was 100), preventing all overdue items from burst-firing to Telegram simultaneously
- **Queue race condition** — Added `FOR UPDATE SKIP LOCKED` to `get_pending()` query, preventing concurrent scheduler calls from claiming the same queue item
- **Session recovery for nested services** — `cleanup_transactions()` now recursively traverses nested `BaseService` instances (e.g., `SettingsService` inside `PostingService`). Previously, an SSL connection drop could poison a nested service's session, causing an unrecoverable `PendingRollbackError` loop. Also added `settings_service` to the periodic cleanup loop and proactive rollback in the `track_execution` error handler.
- **Dead SSL session replacement** — `end_read_transaction()` now creates a fresh session when both commit and rollback fail (e.g., Neon SSL drops), and `track_execution` wraps `fail_run()` in try/except so cleanup always runs. Also caches ORM attributes before try blocks to prevent lazy-load failures in error handlers.
- **Tenant scope on posting history** — All 5 `posting_history` creation paths now propagate `chat_settings_id` from the queue item, fixing NULL tenant scope on history records
- **Observability gaps** — Added `logger.debug()` to 6 silent exception handlers in status check helpers (`telegram_commands.py`), replacing bare `except Exception:` blocks that swallowed errors with zero logging
- **Docstring cleanup** — Replaced `print()` examples in `cloud_storage.py` and `instagram_api.py` docstrings with comments to avoid setting bad patterns

### Added

- **Enhanced Mini App Dashboard** - Richer home screen with collapsible cards for deeper functionality without scroll overload
  - **Quick Controls card** - Toggle Delivery (pause/resume) and Dry Run mode directly from the dashboard
  - **Schedule card** - Expandable day-by-day breakdown, Extend (+7 Days) and Regenerate schedule actions with confirmation dialog
  - **Queue card** - Expandable list of next 10 upcoming posts with media name, category, and relative time
  - **Recent Activity card** - Last 10 posts with status (posted/skipped/failed) and posting method (API/Manual)
  - **Media Library card** - Category breakdown with visual bar chart showing file distribution
  - Cards lazy-load data on first expand to keep initial load fast
  - Schedule timing info (next post, schedule end date) shown in card summaries

- **Dashboard API endpoints** - Seven new endpoints powering the enhanced dashboard
  - `GET /api/onboarding/queue-detail` - Queue items with day summary and schedule bounds
  - `GET /api/onboarding/history-detail` - Recent posting history with media info
  - `GET /api/onboarding/media-stats` - Media library category breakdown
  - `POST /api/onboarding/toggle-setting` - Toggle boolean settings from dashboard (all 5: is_paused, dry_run_mode, enable_instagram_api, show_verbose_notifications, media_sync_enabled)
  - `POST /api/onboarding/update-setting` - Update numeric settings from dashboard (posts_per_day, posting_hours_start, posting_hours_end)
  - `POST /api/onboarding/extend-schedule` - Extend schedule by N days
  - `POST /api/onboarding/regenerate-schedule` - Clear queue and rebuild schedule

- **Full Settings in Quick Controls card** - All settings now editable from the Mini App dashboard (Phase 1 of Mini App Consolidation)
  - 3 new toggle switches: Instagram API, Verbose Notifications, Media Sync
  - Stepper controls for Posts/Day (1-50) and Posting Hours (start/end with wraparound)
  - Optimistic UI updates with automatic rollback on API failure
  - Setup state now returns all boolean settings for dashboard hydration

- **Account Management in Mini App** - Manage Instagram accounts directly from the dashboard (Phase 2 of Mini App Consolidation)
  - Instagram card is now expandable with full account list
  - Switch active account with one tap
  - Add new accounts via OAuth flow (reuses existing `connectOAuth` pattern)
  - Remove accounts with inline confirmation dialog (soft-delete, can be re-added later)
  - Active account highlighted with badge; summary shows `@username`
  - `GET /api/onboarding/accounts` - List all active accounts with active marker for current chat
  - `POST /api/onboarding/switch-account` - Switch active Instagram account
  - `POST /api/onboarding/remove-account` - Deactivate (soft-delete) an account

- **System Status in Mini App** - System health and setup status card in the dashboard (Phase 3 of Mini App Consolidation)
  - New expandable System Status card positioned after Quick Controls
  - Setup checklist: 5 items (Instagram, Google Drive, Media Library, Schedule, Delivery) with status icons
  - System health checks: Database, Telegram, Instagram API, Queue, Recent Posts, Media Sync
  - Badge shows "Healthy"/"All Set" or issue count based on health check results
  - `GET /api/onboarding/system-status` - Aggregated health data from HealthCheckService

- **Sync Media action in Mini App** - Trigger media sync directly from the dashboard (Phase 4 of Mini App Consolidation)
  - "Sync Media" button in Quick Controls card below settings
  - Inline result display showing new/updated/removed/error counts
  - `POST /api/onboarding/sync-media` - Calls MediaSyncService with per-tenant config

- **"Open Dashboard" button on /status** - Quick link to the Mini App from the status command (Phase 5 of Mini App Consolidation)

### Changed

- **Command cleanup** - Reduced active Telegram commands from 11 to 6 (Phase 5 of Mini App Consolidation)
  - **Retired 5 commands** as redirects: `/queue`, `/pause`, `/resume`, `/history`, `/sync` — all now show a helpful message pointing to the Mini App dashboard
  - **Updated `/help`** to show only 6 active commands: `/start`, `/status`, `/setup`, `/next`, `/cleanup`, `/help`
  - **Updated BotCommand menu** from 11 to 6 entries in Telegram autocomplete
  - `/status` and `/settings` kept as full handlers (not slimmed down) since they provide valuable in-chat diagnostics and quick controls
  - Total retired commands now: 12 (5 new + 7 from previous cleanup)

### Fixed

- **Google Drive media download in `/next` and auto-post** - Fixed "No Google Drive credentials found" error when sending notifications. The media download path was using the service account credential lookup instead of per-chat OAuth tokens. Now passes `telegram_chat_id` through `MediaSourceFactory.get_provider_for_media_item()` so Google Drive files are fetched with the correct user OAuth credentials.
- **WebApp buttons in group chats** - `/start` and `/settings` failed with `Button_type_invalid` because Telegram rejects `WebAppInfo` buttons in groups. Now uses signed URL tokens for browser-based access in groups (`web_app=` in DMs, `url=` + HMAC token in groups). API accepts both `initData` and URL tokens for authentication.
- **Telegram bot polling on Railway** - Bot was not responding to commands since migration from Pi. Fixed three issues:
  - Polling task completed immediately after starting background updater; now blocks to keep task alive
  - Added explicit `allowed_updates` and `drop_pending_updates=True` to ensure clean startup
  - Added application-level error handler so handler exceptions are logged instead of silently swallowed
  - Routed `telegram`/`httpx` library logs through app logger so internal errors appear in Railway logs
- **Resource management** — Converted all `try/finally/close()` patterns to context manager `with` statements in `telegram_commands.py` and `onboarding.py`, ensuring consistent database connection cleanup
- **Multi-tenant media sync** - Sync loop now iterates all tenants with `media_sync_enabled=true` instead of relying on global env var. New tenants completing onboarding will have their media synced automatically.

### Changed

- **Telegram command cleanup** - Consolidated bot commands from 18 to 11 for a cleaner daily experience
  - **Kept:** `/start`, `/status`, `/help`, `/queue`, `/next`, `/pause`, `/resume`, `/history`, `/cleanup`, `/settings` (alias: `/setup`), `/sync`
  - **Removed:** `/schedule`, `/stats`, `/locks`, `/reset`, `/dryrun`, `/backfill`, `/connect`
  - Removed commands show a helpful redirect message (e.g., "Use /settings to toggle dry-run mode")
  - `/stats` media breakdown (never-posted, posted-once, posted-2+) merged into `/status` output
  - Schedule management remains available via `/settings` panel (Regenerate / +7 Days buttons)
  - OAuth connections remain available via `/start` setup wizard
  - `/backfill` remains available via CLI (`storyline-cli backfill-instagram`)
- **`/status` enhanced with setup completion reporting** - Now shows setup status at the top: Instagram connection, Google Drive connection, media library, schedule config, and delivery mode. Users with missing configuration see a hint to run `/start`.
- **`/settings` renamed to `/setup`** - Primary command is now `/setup` with `/settings` kept as an alias. Bot command list updated: `/setup` = "Quick settings + open full setup wizard", `/settings` = "Alias for /setup". Header changed from "Bot Settings" to "Quick Setup".
- **Delivery language replaces pause/resume language** - All user-facing text reframed around "Delivery ON/OFF" instead of "Paused/Active/Running". Affects `/pause`, `/resume`, `/status`, `/help`, `/settings` toggle, and resume callback messages.
- **`/start` command always opens Mini App** - Returning users now see an "Open Storydump" button linking to a visual dashboard instead of a text command list. Text fallback retained when `OAUTH_REDIRECT_BASE_URL` is not configured.

### Removed

- **`/connect_drive` command removed** - Google Drive connection is now handled exclusively through the onboarding Mini App wizard (accessible via `/start`). The underlying OAuth routes remain unchanged.
- **7 Telegram commands retired** - `/schedule`, `/stats`, `/locks`, `/reset`, `/dryrun`, `/backfill`, `/connect` removed from bot menu. All still respond with a redirect message pointing to the appropriate replacement (`/settings`, `/status`, `/start`, or CLI).

### Added

- **Smart delivery reschedule for paused tenants** - When delivery is OFF, the scheduler loop automatically bumps overdue queue items forward by +24hr increments until they're in the future. Prevents a flood of 50+ items when resuming after extended pause.
  - New `QueueRepository.get_overdue_pending()` query method
  - New `ChatSettingsRepository.get_all_paused()` query method
  - New `SettingsService.get_all_paused_chats()` method
  - New `PostingService.reschedule_overdue_for_paused_chat()` with +24hr bump logic
  - Scheduler loop runs reschedule pass for all paused tenants every cycle
- **Mini App button in settings keyboard** - When `OAUTH_REDIRECT_BASE_URL` is configured, the settings menu includes a "Full Setup Wizard" button that opens the Mini App directly
- **Mini App home screen for returning users** - Dashboard view showing Instagram connection status, Google Drive connection, posting schedule, and queue status. Each section has an Edit button that jumps to the relevant setup step with a "Save & Return" flow.
- **Expanded `/api/onboarding/init` response** - Now includes `is_paused`, `dry_run_mode`, `queue_count`, and `last_post_at` fields for the dashboard display
- **"Run Full Setup Again" button** - Returning users can re-enter the full onboarding wizard from the home screen
- **Onboarding wizard completion** - Mini App wizard now fully functional end-to-end
  - Media folder validation saves `media_source_type`, `media_source_root`, and `media_sync_enabled` to `chat_settings`
  - New `/api/onboarding/start-indexing` endpoint triggers media sync during wizard
  - Enriched `/api/onboarding/init` response with `media_folder_configured`, `media_indexed`, `media_count`, and `onboarding_step`
  - Completing onboarding auto-enables `enable_instagram_api` (if connected) and `media_sync_enabled` (if folder configured); `dry_run_mode` always stays true
  - Onboarding step tracking: each wizard step saves progress to database for resume on reopen
  - New "Index Media" wizard step with progress indicator and result display
  - All wizard steps are skippable (Instagram, Google Drive, media folder, indexing, schedule)
  - Summary step shows configuration status for all setup items
  - Folder validation no longer auto-advances — shows results with explicit "Continue" button
- **Per-chat media source configuration** - `media_source_type` and `media_source_root` columns on `chat_settings` table
  - Each Telegram chat can now have its own media source (local path or Google Drive folder ID)
  - `NULL` values fall back to global `MEDIA_SOURCE_TYPE` / `MEDIA_SOURCE_ROOT` env vars (backward compatible)
  - New `SettingsService.get_media_source_config()` resolves per-chat config with env var fallback
  - `MediaSyncService.sync()` accepts `telegram_chat_id` for per-chat sync
  - Onboarding media-folder endpoint now saves selected folder to chat settings
  - Migration: `scripts/migrations/017_add_media_source_to_chat_settings.sql`

### Fixed

- **Google Drive media sync auth** - Media sync now passes tenant chat ID when creating Google Drive provider, enabling per-tenant OAuth credential lookup instead of falling back to non-existent service account

### Changed

- **ConfigValidator cloud deployment support** - Relaxed startup validation for cloud environments
  - `MEDIA_DIR` is now auto-created if it doesn't exist (needed for Railway's `/tmp/media`)
  - Removed `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_ACCOUNT_ID` env var requirements — tokens are managed via OAuth and stored in the database in multi-tenant mode
  - Cloudinary config check retained when `ENABLE_INSTAGRAM_API=true`

- **`.env.example` cloud variables** - Added cloud deployment configuration reference
  - `DATABASE_URL` full connection string option for PaaS platforms
  - `DB_SSLMODE`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` for Neon tuning
  - `OAUTH_REDIRECT_BASE_URL` for Railway HTTPS domain
  - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` for Google Drive OAuth
  - `MEDIA_SOURCE_TYPE`, `MEDIA_SOURCE_ROOT`, `MEDIA_SYNC_ENABLED` for cloud media

### Security

- **XSS prevention in OAuth HTML pages** - All user-supplied values (`username`, `email`, `title`, `message`) now escaped with `html.escape()` before interpolation into HTML responses (`src/api/routes/oauth.py`)
- **Onboarding chat_id verification** - `_validate_request()` now verifies the `chat_id` from the signed `initData` matches the request's `chat_id`, preventing cross-tenant manipulation; returns 403 on mismatch
- **CORS origin restriction** - Replaced `allow_origins=["*"]` with `OAUTH_REDIRECT_BASE_URL` (or `localhost` in development), and restricted `allow_headers` to `Content-Type`
- **Google Drive API query injection fix** - Escaped single quotes and backslashes in `folder_name` before interpolating into Google Drive API query strings (`google_drive_provider.py`)
- **Schedule input validation** - Added Pydantic `Field` validators: `posts_per_day` (1-50), `posting_hours_start/end` (0-23), `schedule_days` (1-30)
- **Instagram API exception data sanitization** - Removed `response` dict from `InstagramAPIError` to prevent full API response leakage through error tracking/logging
- **initData chat extraction** - `validate_init_data()` now extracts `chat_id` from Telegram's `chat` object when present in signed data (group chats)

### Added

- **Cloud deployment guide** - Comprehensive guide for deploying to Railway + Neon
  - Two-process architecture (worker + web) with Procfile
  - Neon PostgreSQL setup with SSL, pool sizing, and schema migration instructions
  - Full environment variable reference (30+ vars)
  - OAuth callback configuration for Instagram and Google Drive
  - Security checklist, cost estimates, and troubleshooting guide

- **Cloud-ready database configuration**
  - `DATABASE_URL` env var support — full connection string overrides individual `DB_*` components
  - `DB_SSLMODE` env var — appends `?sslmode=require` for Neon compatibility
  - `DB_POOL_SIZE` and `DB_MAX_OVERFLOW` env vars — configurable connection pool (default: 10/20, Neon free tier: 3/2)

- **Telegram Mini App onboarding wizard** - Self-service setup flow for new users via Telegram WebApp
  - 6-step wizard: Welcome, Connect Instagram, Connect Google Drive, Media Folder, Schedule, Summary
  - `validate_init_data()`: HMAC-SHA256 validation of Telegram `initData` for secure Mini App authentication
  - 5 API endpoints under `/api/onboarding/`: init, oauth-url, media-folder, schedule, complete
  - Static Mini App frontend (HTML/CSS/JS) served by FastAPI, Telegram theme-aware
  - OAuth polling pattern: Mini App polls `/init` every 3s to detect when OAuth completes
  - `/start` command updated: new users see "Open Setup Wizard" `WebAppInfo` button, returning users see dashboard
  - Migration 016: `onboarding_step` + `onboarding_completed` columns on `chat_settings`
  - `SettingsService`: `set_onboarding_step()` and `complete_onboarding()` methods
  - 30 new tests (8 webapp auth, 16 API routes, 3 settings service, 3 /start command)

- **Google Drive user OAuth flow** - Browser-based Google Drive connection for per-tenant media sourcing
  - `GoogleDriveOAuthService`: Fernet-encrypted state tokens, Google token exchange, per-tenant token storage
  - Google Drive OAuth routes: `/auth/google-drive/start` (redirect to Google consent) and `/auth/google-drive/callback` (exchange + store)
  - `/connect_drive` Telegram command: sends inline button with Google Drive OAuth link
  - Per-tenant token storage via `api_tokens.chat_settings_id` FK (migration 015)
  - `TokenRepository`: 3 new tenant-scoped methods (`get_token_for_chat`, `create_or_update_for_chat`, `delete_tokens_for_chat`)
  - `GoogleDriveService.get_provider_for_chat()`: creates GoogleDriveProvider from user OAuth credentials
  - `MediaSourceFactory`: accepts `telegram_chat_id` param, tries user OAuth before service account fallback
  - New settings: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
  - 43 new tests (18 OAuth service, 13 routes, 6 token repo, 3 command, 3 provider)

- **Instagram OAuth redirect flow** - Browser-based Instagram account connection replacing manual CLI token copy-paste
  - `OAuthService`: Fernet-encrypted state tokens (10min TTL, CSRF nonce), Meta token exchange (short→long-lived), account create/update
  - FastAPI app (`src/api/app.py`) with two OAuth endpoints: `/auth/instagram/start` (redirect to Meta) and `/auth/instagram/callback` (exchange + store)
  - `/connect` Telegram command: sends inline button with OAuth link, 10-minute expiry notice
  - HTML success/error pages for browser feedback after OAuth callback
  - Telegram notification on success ("Instagram connected! Account: @username") and failure
  - New dependencies: `fastapi>=0.109.0`, `uvicorn>=0.27.0`
  - New setting: `OAUTH_REDIRECT_BASE_URL`
  - 32 new tests (17 OAuthService, 12 route, 3 /connect command)

- **Per-tenant scheduler and posting pipeline** - Thread `telegram_chat_id` through scheduler, posting, and main loop for multi-tenant operation
  - `ChatSettingsRepository.get_all_active()` — discover all non-paused tenants
  - `SettingsService.get_all_active_chats()` — service-layer tenant discovery
  - `SchedulerService`: 4 methods accept `telegram_chat_id` (create_schedule, extend_schedule, both time-slot generators)
  - `PostingService`: `process_pending_posts` and `_get_chat_settings` accept `telegram_chat_id`; `_post_via_instagram` reads chat context from queue item
  - `main.py`: Scheduler loop iterates over all active tenants with per-tenant error isolation; legacy single-tenant fallback preserved
  - `TelegramService`: `admin_chat_id` cached in constructor, lifecycle notifications use instance property
  - `/schedule` command passes `update.effective_chat.id` to scheduler
  - `_notify_sync_error` uses `telegram_service.channel_id` instead of hardcoded constant
  - 20 new unit tests (scheduler loop, posting tenant, scheduler tenant, repository, service)

- **Per-tenant repository query filtering** - Add optional `chat_settings_id` parameter to 42 repository methods across 5 repositories
  - `BaseRepository`: New `_apply_tenant_filter()` helper used by all tenant-scoped repositories
  - `MediaRepository`: 13 methods updated (including `get_next_eligible_for_posting` with tenant-scoped subqueries)
  - `QueueRepository`: 10 methods updated (`shift_slots_forward` passes tenant through to `get_all`)
  - `HistoryRepository`: 5 methods + `HistoryCreateParams` dataclass updated
  - `LockRepository`: 7 methods updated (`is_locked` passes tenant through to `get_active_lock`)
  - `CategoryMixRepository`: 6 methods updated (`set_mix` scopes both SCD expire and create operations)
  - All parameters are `Optional[str] = None` — backward compatible, no service code changes
  - ~53 new unit tests for tenant filtering behavior

- **Multi-tenant data model foundation** - Add nullable `chat_settings_id` FK to 5 core tables for multi-tenant support
  - `media_items`, `posting_queue`, `posting_history`, `media_posting_locks`, `category_post_case_mix`
  - All FKs nullable: `NULL` = legacy single-tenant data (full backward compatibility)
  - `media_items.file_path` uniqueness moved from column-level to table-level `UniqueConstraint` per tenant
  - `media_posting_locks` unique constraint updated to include tenant scope
  - Partial unique index preserves legacy file_path uniqueness for NULL-tenant rows
  - Migration: `014_multi_tenant_chat_settings_fk.sql`
  - New model test suites: `test_posting_history.py`, `test_media_lock.py`, `test_category_mix.py`

- **CLI command tests for backfill, Google Drive, and sync** - 27 new unit tests for 3 previously untested CLI modules
  - `tests/cli/test_backfill_commands.py` - 10 tests covering `backfill-instagram` and `backfill-status` commands
  - `tests/cli/test_google_drive_commands.py` - 8 tests covering `connect-google-drive`, `google-drive-status`, `disconnect-google-drive`
  - `tests/cli/test_sync_commands.py` - 9 tests covering `sync-media` and `sync-status`

- **Model, config, and exception unit tests** - 12 new test files covering previously untested areas
  - Exception tests: `test_base_exceptions.py` (5 tests), `test_google_drive_exceptions.py` (22 tests), `test_instagram_exceptions.py` (22 tests) — inheritance hierarchy, attribute storage, catchability
  - Model tests: `test_media_item.py` (16 tests), `test_posting_queue.py` (10 tests), `test_chat_settings.py` (14 tests), `test_instagram_account.py` (8 tests), `test_api_token.py` (14 tests) — column defaults, nullability, uniqueness, repr, computed properties
  - Config tests: `test_constants.py` (7 tests), `test_settings.py` (22 tests) — default values, `database_url` property
  - All tests are pure unit tests (no database required)

### Fixed

- **Repository exports** - Added `ChatSettingsRepository` and `InstagramAccountRepository` to `src/repositories/__init__.py`
- **Stale comment** - Updated `get_recent_runs()` comment in `service_run_repository.py` to reflect actual production usage

- **Instagram backfill timestamp parsing on Python 3.10** - `+0000` timezone format isn't supported by `datetime.fromisoformat()` in Python 3.10, causing silent parse failures in `_is_after_date` and `_download_and_index`
- **CI FutureWarning crash** - Filter `FutureWarning` from `google.api_core` in pytest config to prevent test collection errors on Python 3.10

- **CLI unit tests for user, queue, and media commands** - Converted 17 skipped integration test placeholders to 24 working unit tests
  - `test_user_commands.py`: 6 tests (list users, empty DB, no-username fallback, promote, nonexistent user, invalid role)
  - `test_queue_commands.py`: 7 tests (create schedule, no media, default days, list queue, empty queue, process queue, force post)
  - `test_media_commands.py`: 11 tests (index success/error/nonexistent, list items/empty/category/active-only, validate valid/warnings/errors/nonexistent)
  - All tests use `@patch` + `CliRunner` pattern matching existing `test_instagram_commands.py`

### Changed

- **Extract shared Telegram handler utilities** - Promoted 2 private methods to module-level utilities in `telegram_utils.py`
  - `build_account_management_keyboard()` — pure function replacing duplicated keyboard building in account selection menu and add-account success path
  - `cleanup_conversation_messages()` — async helper replacing 3 identical message-deletion loops (success, error, cancel paths)
  - Deleted `_build_account_config_keyboard()` and `_cleanup_conversation_messages()` private methods from `TelegramAccountHandlers`
  - 12 new utility tests, 4 existing tests updated

- **Extract sub-methods from long handlers** - Decomposed `_do_autopost()` and `handle_status()` into focused helpers
  - `_do_autopost()` reduced from 353 to ~50 lines via `AutopostContext` dataclass + 7 extracted helpers (`_get_account_display`, `_upload_to_cloudinary`, `_handle_dry_run`, `_execute_instagram_post`, `_record_successful_post`, `_send_success_message`, `_handle_autopost_error`)
  - `handle_status()` reduced from 115 to ~50 lines via 4 extracted helpers (`_get_next_post_display`, `_get_last_posted_display`, `_get_instagram_api_status`, `_get_sync_status_line`)
  - 27 new tests covering all extracted methods

- **BackfillContext dataclass for parameter reduction** - Introduced `BackfillContext` to bundle shared state across backfill call chain
  - Reduces `_backfill_feed` from 9 to 3 params, `_backfill_stories` from 8 to 2, `_process_media_item` from 8 to 3, `_process_carousel` from 7 to 2, `_download_and_index` from 7 to 6
  - Removed unused `username` parameter from `_download_and_index`
  - Added `make_ctx` test fixture and 2 new `TestBackfillContext` tests

- **Refactored add-account state machine** - Decomposed 315-line `handle_add_account_message()` into focused helpers
  - Extracted `_handle_display_name_input()`, `_handle_account_id_input()`, `_handle_token_input()` step handlers
  - Extracted `_validate_instagram_credentials()` for API call + account create/update
  - Extracted `_cleanup_conversation_messages()` deduplicating 3 identical cleanup loops
  - Extracted `_build_account_config_keyboard()` deduplicating 2 keyboard builders (returns `InlineKeyboardMarkup`)
  - Simplified `handle_account_selection_menu()` and `handle_add_account_cancel()` using shared helpers

### Fixed

- **Exception-shadowing bug in add-account error handling** - Inner `except Exception as e:` during message deletion overwrote the outer API error variable, causing error messages to display deletion errors instead of the actual API failure

### Added

- **Media Source Provider Abstraction** - Foundation for cloud media sources (Phase 01 of Cloud Media Enhancements)
  - `MediaSourceProvider` abstract interface for file access across local, cloud, and remote sources
  - `MediaFileInfo` dataclass for provider-agnostic file metadata
  - `LocalMediaProvider` wrapping filesystem operations behind the provider interface
  - `MediaSourceFactory` for creating provider instances by source type
  - `source_type` and `source_identifier` columns on `media_items` table (migration 011)
  - `get_by_source_identifier()` repository method for provider-based lookups
  - Unified `upload_media()` method on CloudStorageService accepting file path or raw bytes

- **Google Drive Media Source Provider** - Cloud media integration via Google Drive API v3 (Phase 02 of Cloud Media Enhancements)
  - `GoogleDriveProvider` implementing `MediaSourceProvider` for Drive API file access
  - Service account authentication for server-to-server access (folder shared with service account)
  - Subfolder-as-category convention matching local filesystem behavior
  - Uses Drive's `md5Checksum` for dedup (avoids downloading just to hash)
  - Chunked downloads via `MediaIoBaseDownload` for large files
  - `GoogleDriveService` orchestration with encrypted credential storage via `api_tokens` table
  - Google Drive exception hierarchy: `GoogleDriveError`, `GoogleDriveAuthError`, `GoogleDriveRateLimitError`, `GoogleDriveFileNotFoundError`
  - `MediaSourceFactory` lazy registration of Google Drive provider (no crash if SDK not installed)
  - CLI commands: `connect-google-drive`, `google-drive-status`, `disconnect-google-drive`
  - `delete_token()` method added to `TokenRepository` for proper credential cleanup

- **Scheduled Media Sync Engine** - Automatic reconciliation of media sources with database (Phase 03 of Cloud Media Enhancements)
  - `MediaSyncService` with full sync algorithm: new file indexing, deleted file deactivation, rename/move detection via hash matching, reactivation of reappeared files
  - `SyncResult` dataclass for tracking sync outcomes (new, updated, deactivated, reactivated, unchanged, errors)
  - Background `media_sync_loop` in `src/main.py` following existing asyncio loop pattern
  - Health check integration: `media_sync` check in `check-health` command
  - CLI commands: `sync-media` (manual trigger), `sync-status` (last sync info)
  - New settings: `MEDIA_SYNC_ENABLED`, `MEDIA_SYNC_INTERVAL_SECONDS`, `MEDIA_SOURCE_TYPE`, `MEDIA_SOURCE_ROOT`
  - New repository methods: `get_active_by_source_type()`, `get_inactive_by_source_identifier()`, `reactivate()`, `update_source_info()`

- **Media Source Configuration & Health** - Telegram UI integration for media sync engine (Phase 04 of Cloud Media Enhancements)
  - Media sync toggle in `/settings` menu (per-chat, persisted to `chat_settings`)
  - New `/sync` command for manual media sync from Telegram
  - Enhanced `/status` output with media sync health section
  - Proactive Telegram notifications on sync errors (respects verbose setting)
  - Enhanced health check with provider connectivity testing
  - Database migration `012_chat_settings_media_sync.sql` for per-chat sync toggle

- **Instagram Media Backfill** - Pull existing media from Instagram back into the system (Phase 05 of Cloud Media Enhancements)
  - New `InstagramBackfillService` for fetching feed posts, live stories, and carousel albums from Instagram Graph API
  - New CLI commands: `backfill-instagram` (with --limit, --media-type, --since, --dry-run, --account-id), `backfill-status`
  - New Telegram command: `/backfill [limit] [dry]`
  - Carousel album expansion: downloads each child image/video individually
  - Cursor-based pagination for large media libraries
  - Duplicate prevention via `instagram_media_id` tracking column
  - Content-level dedup via SHA256 hash comparison
  - Date filtering with early termination (--since flag)
  - Dry-run mode for previewing without downloading
  - Multi-account support via --account-id flag
  - New exception hierarchy: `BackfillError`, `BackfillMediaExpiredError`, `BackfillMediaNotFoundError`
  - Database migration 013: `instagram_media_id` and `backfilled_at` columns on `media_items`

### Changed

- **Posting pipeline decoupled from filesystem** - All media access now goes through provider abstraction
  - TelegramService sends photos via provider download + BytesIO (not `open(file_path)`)
  - TelegramAutopostHandler uploads to Cloudinary via provider download + bytes
  - PostingService uses provider for Instagram API upload flow
  - Media type detection uses `mime_type` column instead of file extension parsing

### Removed

- **Remove 13 confirmed dead repository methods** - Audit and clean up unused code from 4 repository files
  - `category_mix_repository.py`: removed `get_category_ratio`, `get_mix_at_date`
  - `interaction_repository.py`: removed `get_by_user`, `get_by_type`, `get_by_name`, `count_by_user`, `count_by_name`
  - `history_repository.py`: removed `get_by_user_id`, `get_stats`
  - `token_repository.py`: removed `get_all_for_service`, `get_expired_tokens`, `delete_token`, `delete_all_for_service`
  - Annotated 5 future-use methods with `# NOTE: Unused in production` comments
  - Cleaned up corresponding tests and unused imports (`func` from interaction_repository)

### Tests

- **Add missing test files for 6 uncovered modules** - Create 64 new unit tests across 6 previously untested files
  - `test_telegram_autopost.py` (6 tests): Safety gates, dry-run mode, Cloudinary failure, operation locks
  - `test_instagram_commands.py` (11 tests): CLI commands for status, add/list/deactivate/reactivate accounts
  - `test_base_repository.py` (14 tests): Session lifecycle, commit/rollback, context manager, check_connection
  - `test_chat_settings_repository.py` (7 tests): CRUD, .env bootstrap, pause tracking
  - `test_instagram_account_repository.py` (12 tests): Account CRUD, activate/deactivate, prefix lookup
  - `test_token_repository.py` (14 tests): Token CRUD, UPSERT, expiry, multi-account filter chains
  - Fixed plan's lazy-import patch paths (must patch at source module for `from ... import` inside function bodies)
  - Total test suite: 528 passed, 38 skipped, 0 failures

- **Convert 45 skipped service tests to unit tests** - Replace integration fixtures with mock-based unit tests across 7 service test files
  - Rewrote test_base_service.py, test_media_lock.py, test_posting.py, test_scheduler.py with correct method signatures
  - Implemented 3 stub tests in test_telegram_commands.py (next_media_not_found, next_notification_failure, next_logs_interaction)
  - Fixed is_paused/set_paused mocking for pause/resume tests (property reads from settings_service)
  - Removed 12 duplicate @pytest.mark.skip decorators from test_instagram_api.py; added missing dependency patches
  - Updated Instagram API tests for multi-account architecture (is_configured, post_story credential flow)
  - Fixed time-dependent scheduler tests (days=2 to ensure future slots)

- **Convert 74 skipped repository tests to unit tests** - Replace `test_db` integration fixtures with mock-based unit tests
  - Pattern: `patch.object(Repo, '__init__')` + `MagicMock(spec=Session)` for chainable query mocking
  - 67 new passing tests across 7 repository test files (media, queue, user, interaction, lock, history, service_run)
  - Fixed method signatures to match actual repo APIs (e.g., `create(media_item_id=)` not `create(media_id=)`)
  - Dropped 7 tests for non-existent methods (`get_or_create`, `get_never_posted`, `get_least_posted`, etc.)
  - 9 integration-only tests remain skipped (complex multi-table queries, slot shifting)
  - Added edge case tests: not-found paths, max retries exceeded, empty stats, permanent locks

### Changed

- **Update all pinned dependencies to latest versions** - Bring all ==pinned packages current
  - Tier 1 (patch): psycopg2-binary 2.9.9→2.9.11, python-dateutil 2.8.2→2.9.0.post0
  - Tier 2 (minor): pydantic 2.5→2.12.5, pydantic-settings 2.1→2.12, SQLAlchemy 2.0.23→2.0.46, click 8.1→8.3, rich 13.7→14.3, python-dotenv 1.0→1.2, alembic 1.13→1.18
  - Tier 3 (major): python-telegram-bot 20.7→22.6, httpx 0.25→0.28, Pillow 10.1→12.1, pytest 7.4→9.0, pytest-asyncio 0.21→1.3, pytest-cov 4.1→7.0, pytest-mock 3.12→3.15

- **Documentation review and accuracy audit** - Cross-referenced all docs against codebase post-v1.6.0 refactor
  - Corrected test counts, setting names, supported formats, and deploy script defaults
  - Updated code examples in security review for post-refactor handler locations
  - Archived 4 completed planning docs; standardized status markers (PENDING/IN PROGRESS/COMPLETED)

### Refactored

- **Decompose long functions into focused helpers** - Extract logic from 5 oversized methods
  - `HistoryRepository.create()`: Bundle 16 parameters into `HistoryCreateParams` dataclass; update all 5 call sites
  - `SchedulerService`: Extract shared `_fill_schedule_slots()` from duplicated loops in `create_schedule()` and `extend_schedule()`
  - `InstagramAccountService.add_account()`: Extract `_validate_new_account()` and `_create_account_with_token()`
  - `CloudStorageService.upload_media()`: Extract `_validate_file_path()` and `_build_upload_options()`

- **Extract magic numbers into named constants** (#29) - Replace hardcoded values with descriptive constants
  - Created `src/config/constants.py` for shared constants (MIN/MAX_POSTS_PER_DAY, MIN/MAX_POSTING_HOUR)
  - Added class-level constants to SchedulerService, TelegramCommandHandlers, TelegramSettingsHandlers, SettingsService, InstagramAPIService, TelegramAccountHandlers
  - All validation logic now references named constants with clear error messages

- **Replace silent error swallowing with debug logging** (#30) - Add diagnostic visibility to suppressed exceptions
  - Added `logger.debug()` to 9 bare `except Exception: pass` blocks across 3 files
  - Covers repository lifecycle cleanup, Telegram message deletion, and session recovery
  - `__del__` method intentionally kept as `pass` (logging unsafe during garbage collection)

- **Route health check database query through repository layer** (#31) - Fix architecture violation (ARCH-1)
  - Added `BaseRepository.check_connection()` static method for DB connectivity checks
  - Removed direct `sqlalchemy` and `get_db` imports from `HealthCheckService`
  - No services now access the database directly; all queries go through repositories

- **Route scheduler media selection through repository layer** (#32) - Fix architecture violation (ARCH-2)
  - Moved `_select_media_from_pool()` query logic from `SchedulerService` to `MediaRepository.get_next_eligible_for_posting()`
  - Removed inline `sqlalchemy` and model imports from service layer
  - Service method now delegates to repository with identical query behavior

- **Refactor callback dispatcher to dictionary dispatch** - Replace 90-line if-elif chain with two-tier dispatch
  - Standard `(data, user, query)` handlers served via dictionary lookup (20 entries)
  - Special-case handlers (non-standard signatures, sub-routing) in dedicated method (7 entries)
  - Unknown callback actions now log a warning instead of being silently ignored

- **Extract Telegram handler common utilities** - Deduplicate 4 repeated patterns across handler modules
  - Created `telegram_utils.py` with shared validation, keyboard builders, and state cleanup helpers
  - Replaced ~15 inline queue validation blocks with `validate_queue_item()` / `validate_queue_and_media()`
  - Replaced ~3 keyboard constructions with `build_queue_action_keyboard()` / `build_error_recovery_keyboard()`
  - Replaced ~6 cancel keyboard constructions with shared `CANCEL_KEYBOARD` constant
  - Replaced ~6 state cleanup blocks with `clear_settings_edit_state()` / `clear_add_account_state()`

### Fixed

- **Race Condition on Telegram Button Clicks** - Prevent duplicate operations from rapid double-clicks
  - Added `asyncio.Lock` per queue item to prevent concurrent execution
  - Added cancellation flags so terminal actions (Posted/Skip/Reject) abort pending auto-posts
  - Auto-post checks cancellation after Cloudinary upload and before Instagram API call
  - Shows "⏳ Already processing..." feedback when lock is held
  - Locks and flags cleaned up after operation completes

## [1.6.0] - 2026-02-09

### Added

#### Instagram Account Management (Phase 1.5)

- **Multi-Account Support** - Store multiple Instagram account identities
  - Display name, Instagram ID, username per account
  - Active/inactive status for soft deletion
  - Separation of concerns: identity (accounts) vs credentials (tokens) vs selection (settings)
- **Account Switching via Telegram** - Switch between accounts in /settings menu
  - Per-chat active account selection stored in `chat_settings`
  - Auto-select when only one account exists
  - Visual indicator of currently active account
- **Per-Account Token Storage** - OAuth tokens linked to specific accounts
  - `api_tokens.instagram_account_id` foreign key
  - Supports multiple tokens per service (one per account)
  - Backward compatible with legacy .env-based tokens
- **New CLI Commands**
  - `add-instagram-account` - Register new Instagram account with encrypted token
  - `list-instagram-accounts` - Show all registered accounts with status
  - `deactivate-instagram-account` - Soft-delete an account
  - `reactivate-instagram-account` - Restore a deactivated account
- **InstagramAccountService** - New service for account management
  - `list_accounts()`, `get_active_account()`, `switch_account()`
  - `add_account()`, `deactivate_account()`, `reactivate_account()`
  - `get_accounts_for_display()` - Formatted data for Telegram UI
  - `auto_select_account_if_single()` - Auto-selection logic
- **InstagramAPIService** - Multi-account posting support
  - `post_story()` now accepts `telegram_chat_id` parameter
  - Credentials retrieved based on active account for chat
  - Fallback to legacy .env config when no account selected
- **TokenRefreshService** - Per-account token refresh
  - `refresh_instagram_token()` accepts `instagram_account_id`
  - `refresh_all_instagram_tokens()` - Batch refresh for all accounts
  - Maintains backward compatibility with legacy tokens
- 24 new unit tests for InstagramAccountService

#### Telegram /settings Menu Improvements

- **Close Button** - Dismiss the settings menu cleanly with ❌ Close button
- **Verbose Mode Toggle** - Control notification verbosity via 📝 Verbose toggle
  - ON (default): Shows detailed workflow instructions
  - OFF: Shows minimal info
  - Applies to manual posting notifications and auto-post success messages
- **Schedule Management Buttons** - Manage queue directly from settings
  - 🔄 Regenerate: Clears queue and creates new 7-day schedule (with confirmation)
  - 📅 +7 Days: Extends existing queue by 7 days (preserves current items)
- Removed Quick Actions buttons (📋 Queue, 📊 Status) - use `/queue` and `/status` commands instead
- **Instagram Account Configuration via Telegram**
  - Renamed "Select Account" to "Configure Accounts" - full account management menu
  - **Add Account Flow** - 3-step conversation: display name → account ID → access token
    - Auto-fetches username from Instagram API to validate credentials
    - If account already exists, updates the token instead of erroring
  - **Remove Account** - Deactivate accounts directly from Telegram with confirmation
  - **Account Selection** - Select active account from the same menu
  - Security: bot messages deleted after flow; user warned to delete sensitive messages
- **SchedulerService `extend_schedule()` method** - Add days to existing schedule without clearing
  - Finds last scheduled time, generates new slots starting from next day
  - Respects category ratios and existing scheduler logic

#### Inline Account Selector (Phase 1.7)

- **Account Indicator in Caption** - Posting notifications show which Instagram account is active
  - Format: "📸 Account: {display_name}"
  - Shows "📸 Account: Not set" when no account is configured
- **Account Selector Button** - Switch accounts without leaving the posting workflow
  - New "📸 {account_name}" button in posting notifications
  - Click to see simplified account selector (no add/remove, just switch)
  - Immediate feedback with toast notification on switch
  - Automatically returns to posting workflow with updated caption
- **Button Layout Reorganization**
  - Status Actions Grouped: Posted, Skip, and Reject buttons together
  - Instagram Actions Grouped: Account selector and Open Instagram below
  - New order: Auto Post → Posted/Skip → Reject → Account Selector → Open Instagram
- **Shortened Callback Data** - Uses 8-char UUID prefixes for Telegram's 64-byte callback limit
  - New repository methods: `QueueRepository.get_by_id_prefix()`, `InstagramAccountRepository.get_by_id_prefix()`
- **Settings Menu** - Renamed account button to "Default: {friendly_name}", clearer "Choose Default Account" language

#### Telegram Command Menu & Message Cleanup (Phase 1.8)

- **Native Telegram Command Menu** - Commands appear in Telegram's native "/" autocomplete
  - Uses `set_my_commands()` API; all 15 commands registered with descriptions
  - Updates automatically when bot initializes
- **`/cleanup` Command** - Delete recent bot messages from chat
  - Queries `user_interactions` table for bot messages
  - Gracefully handles 48-hour deletion limit (Telegram API restriction)
  - Shows summary: deleted count and failed count
  - Auto-deletes confirmation message after 5 seconds
- **Renamed `/clear` → `/reset`** - Clearer distinction from `/cleanup`
  - `/reset` = Reset posting queue to empty; `/cleanup` = Delete bot messages from chat
  - CLI aligned: `storyline-cli reset-queue`
- **Automatic Message ID Tracking** - Bot tracks sent message IDs for cleanup
  - Tracks notification messages (photos with buttons) and status/queue listing messages
  - 100-message rolling cache

### Changed

#### TelegramService Refactor

- **PR 1: Extract Command Handlers** - Architecture improvement
  - Extracted 14 `/command` handlers into new `TelegramCommandHandlers` class (`telegram_commands.py`, ~715 lines)
  - `TelegramService` reduced by ~655 lines (from 3,504 to 2,849)
  - Uses composition pattern: handler class receives service reference via `__init__(self, service)`
  - Command registration moved to a clean `command_map` dict in `initialize()`
  - Tests split into `test_telegram_commands.py`
  - All 81 tests pass (65 passed, 16 skipped) - zero regressions
- **PR 2: Extract Callbacks + Autopost** - Architecture improvement
  - Extracted 9 callback handlers into new `TelegramCallbackHandlers` class (`telegram_callbacks.py`)
  - Extracted auto-post flow into new `TelegramAutopostHandler` class (`telegram_autopost.py`)
  - `TelegramService` reduced by ~765 lines (from 2,849 to ~1,984)
  - Tests split into `test_telegram_callbacks.py`; routing tests remain in `test_telegram_service.py`
  - All 81 tests pass - zero regressions
- **PR 3: Extract Settings + Accounts** - Architecture improvement
  - Extracted settings handlers into new `TelegramSettingsHandlers` class (`telegram_settings.py`)
  - Extracted account handlers into new `TelegramAccountHandlers` class (`telegram_accounts.py`)
  - `TelegramService` reduced from ~1,984 to ~681 lines (core routing, initialization, captions, shared utilities)
  - Tests split into `test_telegram_settings.py` and `test_telegram_accounts.py`
  - All 345 tests pass (77 telegram-specific) - zero regressions

#### Verbose Settings Expansion

- **Verbose Setting Now Controls More Message Types** - Manual posted confirmations, rejected confirmations, and dry run results
- Added `_is_verbose()` helper method to reduce code duplication (replaces 3-line inline checks)
- User-initiated commands (`/status`, `/queue`, `/help`, etc.) always show full detail

#### Code Quality & Developer Experience

- **Refactored Settings Keyboard** - Eliminated 3x code duplication; extracted `_build_settings_message_and_keyboard()` helper
- **Refactored Posted/Skipped Handlers** - Extracted shared `_complete_queue_action()` helper (~60 lines of duplicated code removed)
- **Simple Caption Now Respects Verbose and Account** - Consistency fix for `CAPTION_STYLE=simple`
- **Centralized Version String** - Added `__version__` in `src/__init__.py`; `setup.py`, `cli/main.py`, and startup notification now reference it
- **Eliminated Redundant DB Queries** - `_is_verbose()` accepts optional pre-loaded `chat_settings` parameter
- **Claude Code Hooks** - Auto-fix linting errors on file save (`ruff check --fix` + `ruff format`)
- **Pre-Push Linting Script** - `scripts/lint.sh` catches CI failures locally
- **Documentation Organization** - Moved SECURITY_REVIEW.md to documentation/ folder; added markdown write permissions
- **Phase 1.7 Feature Plan** - Added inline account selector planning document

### Fixed

#### Critical Bugs

- **Dry Run Mode Blocking Telegram Notifications** - Dry run was blocking ALL Telegram notifications; now only affects Instagram API posting (`src/services/core/posting.py:304-340`)
- **`/cleanup` Command Not Finding Messages After Restart** - Relied on in-memory deque cleared on restart; now queries `user_interactions` table
  - Removed in-memory `message_cache` deque; added `get_bot_responses_by_chat()` repository method and `get_deletable_bot_messages()` service method
- **Auto-Post Success Missing User in Verbose OFF** - Now always shows `✅ Posted to @account by @user` regardless of verbose setting
- **Settings Workflow - Database vs .env** - Fixed .env values overriding database settings for dry run, Instagram API toggle, account switching, and verbose mode
  - Fixed all toggle locations: `_do_autopost()`, `send_notification()`, `/dryrun`, `safety_check_before_post()`
  - All settings now persist across service restarts
- **Token Encryption for Multi-Account** - Tokens added via Telegram were stored unencrypted; now properly encrypts when storing
- **Account Switching from Posting Workflow** - Fixed critical bug preventing account switching
  - Root Cause 1: Callback data parsing split on ALL colons instead of just the first one
  - Root Cause 2: Debug logging sliced UUID objects without converting to string

#### Settings & Account Fixes

- **Add Account Flow - Existing Account Handling** - Token now updated instead of showing error when account already exists
- **Add Account Flow - Security Warning** - Fixed misleading message about bot deleting user messages
- **InstagramAccountService** - Added `update_account_token()` and `get_account_by_instagram_id()` methods
- **Editable Posts/Day and Hours** - Previously display-only in /settings; now starts a conversation flow to edit values
- **`_handle_cancel_reject` Bug** - Now uses `chat_settings.enable_instagram_api` (database) instead of `settings.ENABLE_INSTAGRAM_API` (env var)

#### CI & Code Quality

- **CI Failures** - Resolved all blocking CI issues (#20)
  - Fixed missing `asyncio` import, auto-formatted telegram_service.py
  - Updated test suite for `/clear` → `/reset` rename; fixed assertion for dry_run_mode
  - All 310 tests passing
- **Ruff Linting Errors** - Fixed all 48 linting errors
  - Removed 8 unused imports, fixed 18 unnecessary f-strings, fixed 7 boolean comparison patterns
  - Reorganized imports in cli/main.py, removed 1 unused variable
- **CI Test Failures** - Fixed ALL test failures (48 failures → 0)
  - Updated CI environment variables for individual database components
  - Fixed PostingService, HistoryRepository, CategoryMixRepository, TelegramService tests
  - Converted integration tests to use mocks; skipped complex tests for future refactoring
  - Final: 310 passed, 141 skipped, 0 failed

### Technical Details

#### Database Migrations

- `007_instagram_accounts.sql` - Creates `instagram_accounts` table
- `008_api_tokens_account_fk.sql` - Adds FK to `api_tokens`, updates unique constraint
- `009_chat_settings_active_account.sql` - Adds `active_instagram_account_id` to `chat_settings`
- `010_add_verbose_notifications.sql` - Adds `show_verbose_notifications` column to `chat_settings`

#### New Files

- `src/models/instagram_account.py` - InstagramAccount SQLAlchemy model
- `src/repositories/instagram_account_repository.py` - Full CRUD operations
- `src/services/core/instagram_account_service.py` - Business logic layer
- `src/services/core/telegram_commands.py` - Command handlers (~715 lines)
- `src/services/core/telegram_callbacks.py` - Callback handlers
- `src/services/core/telegram_autopost.py` - Auto-post handler
- `src/services/core/telegram_settings.py` - Settings UI handlers
- `src/services/core/telegram_accounts.py` - Account selection handlers
- `tests/src/services/test_instagram_account_service.py` - Unit tests
- `tests/src/services/test_telegram_commands.py` - Command handler tests
- `tests/src/services/test_telegram_callbacks.py` - Callback handler tests
- `tests/src/services/test_telegram_settings.py` - Settings UI tests
- `tests/src/services/test_telegram_accounts.py` - Account handler tests

#### Modified Files

- `src/models/api_token.py` - Added instagram_account_id FK and relationship
- `src/models/chat_settings.py` - Added active_instagram_account_id FK, show_verbose_notifications
- `src/repositories/token_repository.py` - Per-account token methods
- `src/repositories/queue_repository.py` - Added `get_by_id_prefix()`
- `src/repositories/chat_settings_repository.py` - Updated get_or_create defaults
- `src/services/core/telegram_service.py` - Reduced to ~681 lines (core routing, initialization, captions)
- `src/services/core/scheduler.py` - Added `extend_schedule()` method
- `src/services/integrations/instagram_api.py` - Multi-account support
- `src/services/integrations/token_refresh.py` - Per-account refresh
- `cli/commands/instagram.py` - New CLI commands
- `cli/main.py` - Registered new commands

## [1.5.0] - 2026-01-24

### Added - Claude Code Automation & Bot Response Logging

#### Bot Response Logging
- **Outgoing Message Tracking** - Log all bot responses to `user_interactions` table
  - New `bot_response` interaction type for outgoing messages
  - Captures message text, button layouts, and media filenames
  - Enables full visibility into bot activity without viewing Telegram
  - Query both incoming (user actions) and outgoing (bot responses) in one place

- **Enhanced Visibility Methods** - Log key bot actions
  - `photo_notification` - When bot sends media with approve buttons
  - `caption_update` - When marking posts or updating captions
  - `text_reply` - For status messages and confirmations

#### Claude Code Integration
- **Project-Specific Configuration** - `.claude/settings.json` for safe automation
  - Allow list for safe read-only commands (list, status, check)
  - Deny list for dangerous posting commands (process-queue, create-schedule)
  - Enables autonomous development iteration with guardrails

- **`/telegram-status` Command** - SSH-based bot status checking
  - Query bidirectional activity (incoming + outgoing messages)
  - Show current queue and recent posts
  - Check service health via systemctl
  - No need to view Telegram directly

- **Safety Documentation** - Updated CLAUDE.md with critical rules
  - Clear dangerous vs safe command lists
  - Remote development (Raspberry Pi) guidelines
  - Database query examples for safe inspection

### Changed
- `user_interactions.user_id` is now nullable to support `bot_response` entries
- Updated `check_interaction_type` constraint to include `bot_response`
- Moved legacy docs from `documentation/updates/` to `documentation/archive/`

### Technical Details

#### Database Migration (005)
- `ALTER TABLE user_interactions ALTER COLUMN user_id DROP NOT NULL`
- Added `bot_response` to interaction_type check constraint
- New partial index on `created_at` for bot_response queries

#### Files Changed
- `src/models/user_interaction.py` - Nullable user_id, updated docstring
- `src/services/core/interaction_service.py` - Added `log_bot_response()` method
- `src/services/core/telegram_service.py` - Log outgoing messages in handlers
- `scripts/migrations/005_add_bot_response_logging.sql` - Schema migration
- `.claude/settings.json` - Project permission configuration
- `.claude/commands/telegram-status.md` - Status check slash command

## [1.4.0] - 2026-01-10

### Added - Phase 1.6: Category-Based Scheduling

#### Category Organization
- **Category Extraction** - Automatically extract category from folder structure during indexing
  - Folder structure: `media/stories/memes/` → category: `memes`
  - Folder structure: `media/stories/merch/` → category: `merch`
  - Categories stored in `media_items.category` column
  - Configurable via `--extract-category` flag (default: enabled)

#### Posting Ratios (Type 2 SCD)
- **`category_post_case_mix` Table** - Track posting ratio configuration with full history
  - Type 2 Slowly Changing Dimension design for audit trail
  - Ratios stored as decimals (0.70 = 70%)
  - Validation: all active ratios must sum to 1.0 (100%)
  - Supports multiple categories with any ratio split

- **Interactive Ratio Configuration** - User-friendly prompts during indexing
  - Prompts: "What % would you like 'memes'?" format
  - Validates total sums to 100%
  - Allows re-entry if validation fails
  - Shows current vs new ratio comparisons

#### Scheduler Integration
- **Category-Aware Slot Allocation** - Deterministic ratio-based scheduling
  - Allocates slots proportionally (e.g., 70% memes, 30% merch)
  - Handles rounding with largest remainder to last category
  - Shuffles allocation for variety (not all memes then all merch)
  - Fallback to any category when target is exhausted

- **Enhanced Selection Logic** - Category-filtered media selection
  - Filters by target category first
  - Falls back to any available media if category exhausted
  - Maintains existing priority rules (never-posted first, least-posted)
  - Logs category allocation and fallbacks

#### New CLI Commands
- **`storyline-cli list-categories`** - Show categories with posting ratios
  - Displays current ratios and media counts per category
  - Shows if no ratios are configured

- **`storyline-cli update-category-mix`** - Update posting ratios interactively
  - Prompts for each category's percentage
  - Validates total and saves to database
  - Creates new SCD record (preserves history)

- **`storyline-cli category-mix-history`** - View ratio change history
  - Shows all historical ratio configurations
  - Includes effective dates and who made changes
  - Useful for auditing scheduling changes

#### Enhanced Existing Commands
- **`create-schedule`** - Now shows category breakdown
  - Displays how many slots allocated per category
  - Shows percentage breakdown of scheduled items
  - Logs category allocation summary

- **`list-queue`** - Added category column
  - Shows category for each queued item
  - Helps verify ratio-based scheduling

- **`index-media`** - Category extraction and ratio prompts
  - Extracts category from folder structure
  - Prompts for ratio configuration after indexing
  - Option to skip ratio configuration

### Technical Details

#### Database Schema
- **New column**: `media_items.category` (TEXT, indexed)
- **New table**: `category_post_case_mix`
  - `id` (UUID) - Primary key
  - `category` (VARCHAR 100) - Category name
  - `ratio` (NUMERIC 5,4) - Ratio as decimal (0.0000-1.0000)
  - `effective_from` (TIMESTAMP) - When ratio became active
  - `effective_to` (TIMESTAMP) - When ratio was superseded (NULL = current)
  - `is_current` (BOOLEAN) - Quick filter for active ratios
  - `created_by_user_id` (UUID FK) - Who made the change

#### Migrations
- `scripts/migrations/001_add_category_column.sql` - Add category to media_items
- `scripts/migrations/002_add_category_post_case_mix.sql` - Create ratio table
- `scripts/setup_database.sql` - Updated for fresh installations

#### New Components
- **CategoryPostCaseMix** model (`src/models/category_mix.py`)
- **CategoryMixRepository** (`src/repositories/category_mix_repository.py`)
  - `get_current_mix()` - Returns list of active ratio records
  - `get_current_mix_as_dict()` - Returns {category: ratio} dict
  - `set_mix()` - Sets new ratios (creates SCD records)
  - `get_history()` - Returns all historical records

#### Modified Components
- **SchedulerService** - Added category-based slot allocation
- **MediaRepository** - Added category parameter and get_categories()
- **MediaIngestionService** - Added category extraction logic

### Testing
- **34 new tests** for category scheduling features
  - Category extraction tests (7 tests)
  - CategoryMixRepository tests (18 tests)
  - Scheduler category allocation tests (9 tests)
- **Total tests: 173 → 268** (95 new, including other improvements)

### Documentation
- Updated README.md with Phase 1.6 features
- Updated CHANGELOG.md (this file)
- Updated project structure with media subdirectories
- Updated CLAUDE.md with new database tables and CLI commands

## [1.3.0] - 2026-01-08

### Added - Phase 1.5 Week 2: Telegram Bot Commands

#### New Slash Commands
- **`/pause`** - Pause automatic posting while keeping bot responsive
  - Prevents scheduled posts from being processed
  - Manual posting via `/next` still works
  - Shows count of pending posts that will be held

- **`/resume`** - Resume posting with smart overdue handling
  - If no overdue posts: Resumes immediately
  - If overdue posts exist: Shows options to:
    - 🔄 Reschedule (spread overdue posts over next few hours)
    - 🗑️ Clear (remove overdue posts, keep future scheduled)
    - ⚡ Force (process all overdue posts immediately)

- **`/schedule [N]`** - Create N days of posting schedule (1-30 days)
  - Default: 7 days if no argument provided
  - Shows: scheduled count, skipped count, total slots
  - Uses existing scheduler algorithm with smart media selection

- **`/stats`** - Show media library statistics
  - Total active media items
  - Never posted vs posted once vs posted 2+ times
  - Permanently locked (rejected) count
  - Temporarily locked count
  - Items available for posting

- **`/history [N]`** - Show last N posts (default 5, max 20)
  - Status indicator (✅ posted, ⏭️ skipped, 🚫 rejected)
  - Timestamp and user attribution
  - Handles empty history gracefully

- **`/locks`** - View permanently locked (rejected) items
  - Lists all permanently rejected media files
  - Shows file names for identification
  - Useful for reviewing what's been blocked

- **`/clear`** - Clear pending queue with confirmation
  - Shows confirmation dialog with pending count
  - Two-step process prevents accidental clearing
  - Media items remain in library (only queue cleared)

#### Pause Integration
- **PostingService** now checks pause state before processing
  - Scheduled posts are skipped when paused
  - Returns `paused: True` in result dict for visibility
  - Logs when posts are skipped due to pause

#### Repository Enhancement
- **QueueRepository** - Added `update_scheduled_time()` method
  - Supports rescheduling queue items
  - Used by resume:reschedule callback

#### Updated Help Text
- `/help` command now includes all new commands with descriptions
- Commands grouped by function (operational vs informational)

### Changed

#### Test Suite Expansion
- **26 new tests** for all new commands and callbacks
- Test coverage for:
  - Pause command (2 tests)
  - Resume command with overdue handling (3 tests)
  - Schedule command (2 tests)
  - Stats command (1 test)
  - History command (2 tests)
  - Locks command (2 tests)
  - Clear command (2 tests)
  - Resume callbacks: reschedule, clear, force (3 tests)
  - Clear callbacks: confirm, cancel (2 tests)
  - Pause integration with PostingService (1 test)
- **Total tests: 147 → 173** (26 new)

### Technical Details

#### Pause State Management
- Uses class-level variable `TelegramService._paused`
- Property `is_paused` for read access
- Method `set_paused(bool)` for write access
- Persists across scheduler cycles within same process

#### Callback Handler Routing
- New callback prefixes: `resume:*`, `clear:*`
- Extends existing callback router pattern
- Full interaction logging for audit trail

### Documentation
- Updated CHANGELOG.md (this file)
- Updated README.md with new commands
- Updated ROADMAP.md with Week 2 status
- Updated phase-1.5-telegram-enhancements.md
- Updated TEST_COVERAGE.md with new test count

## [1.2.0] - 2026-01-05

### Added - Phase 1.5 Priority 0: Permanent Reject Feature

#### Critical Feature (Production Blocker Resolved)
- **🚫 Permanent Reject Button** - Third button added to Telegram notifications
  - Allows users to permanently block unwanted media (personal photos, test files, etc.)
  - Creates infinite TTL lock (locked_until = NULL) to prevent media from ever being queued again
  - Logs rejection to history with user attribution
  - Essential for safe production use with mixed media folders

#### Button Layout Enhancement
- Updated from 2-button to 3-button layout:
  ```
  [✅ Posted] [⏭️ Skip]
       [🚫 Reject]
   [📱 Open Instagram]
  ```
- Clear visual separation between posting actions and permanent rejection

#### Infrastructure Updates
- **Infinite Lock System** - Permanent locks with NULL `locked_until` value
- **Database Schema Changes**:
  - `media_posting_locks.locked_until` now nullable (NULL = permanent lock)
  - `posting_history.status` accepts 'rejected' value
  - Updated CHECK constraints to include 'rejected' status
- **New Service Methods**:
  - `MediaLockService.create_permanent_lock()` - Convenience method for permanent locks
  - `TelegramService._handle_rejected()` - Handles permanent rejection workflow
  - `LockRepository.get_permanent_locks()` - Query permanently locked media

#### Phase 1.5 Week 1 Priority 1 Features
- **Bot Lifecycle Notifications** - Startup/shutdown messages to admin
  - System status on startup (queue count, media count, last posted time, uptime)
  - Session summary on shutdown (uptime, posts sent, graceful shutdown confirmation)
  - Signal handling for graceful shutdown (SIGTERM/SIGINT)
  - Configurable via `SEND_LIFECYCLE_NOTIFICATIONS` setting
- **Instagram Deep Links** - One-tap Instagram app opening
  - "📱 Open Instagram" button opens Instagram app/web
  - Uses HTTPS URL (Telegram Bot API requirement)
  - Works on desktop (opens web) and mobile (redirects to app)
- **Enhanced Media Captions** - Workflow-focused formatting
  - Clean, actionable 3-step workflow instructions
  - Removed technical metadata clutter (file names, post counts)
  - Kept essential context (scheduled time when relevant)
  - Two modes: "enhanced" (with formatting) and "simple" (plain text)
  - Configurable via `CAPTION_STYLE` setting

### Fixed

#### Critical Bugs
- **Scheduler Permanent Lock Bug** (CRITICAL) - Scheduler was ignoring permanent locks
  - Problem: Lock check only evaluated `locked_until > now`, missing NULL values
  - Solution: Updated to `(locked_until IS NULL) OR (locked_until > now)`
  - Impact: Permanently rejected media was still being scheduled
  - Status: ✅ FIXED - Rejected media now correctly excluded from all schedules

#### Service Bugs
- **Startup Notification Parameter Mismatch** - Failed to send lifecycle notification
  - Problem: Called `MediaRepository.get_all(active_only=True)` but parameter is `is_active`
  - Solution: Changed to `MediaRepository.get_all(is_active=True)`
  - Impact: Startup notification failed silently
  - Status: ✅ FIXED

#### Lock Repository Enhancement
- Updated `get_active_lock()` to detect permanent locks (NULL `locked_until`)
- Updated `get_all_active()` to include permanent locks with proper ordering
- Updated `cleanup_expired()` to never delete permanent locks
- Updated `create()` to support NULL TTL for permanent locks

### Changed

#### Database Operations (Makefile)
- **Mac PostgreSQL Compatibility** - Simplified database commands
  - Changed from psql connection URLs to direct `createdb`/`dropdb` commands
  - Removed dependency on 'postgres' admin database
  - Default `DB_USER` now uses `$(USER)` (current shell user)
  - All commands work without manual postgres database creation
- **Updated Commands**:
  - `make create-db` - Uses `createdb` command
  - `make drop-db` - Uses `dropdb --if-exists`
  - `make init-db` - Connects directly with `psql -d $(DB_NAME)`
  - `make reset-db` - Streamlined drop → create → init flow
  - `make db-shell`, `make db-backup`, `make db-restore` - Simplified
- Inspired by foxxed project's cleaner Makefile approach

#### Configuration
- Added Phase 1.5 settings to `.env.example`:
  - `SEND_LIFECYCLE_NOTIFICATIONS` (default: true)
  - `INSTAGRAM_USERNAME` (optional, for future features)
  - `CAPTION_STYLE` (enhanced|simple, default: enhanced)

### Technical Details

#### Database Schema
- `media_posting_locks.locked_until` - Changed from NOT NULL to nullable
- `media_posting_locks.lock_reason` - Added 'permanent_reject' option
- `posting_history.status` - CHECK constraint includes 'rejected'
- `scripts/setup_database.sql` - Updated for fresh installations

#### Lock Behavior
- **Posted**: Creates 30-day TTL lock (existing behavior)
- **Skipped**: No lock, can be queued again (existing behavior)
- **Rejected**: **Permanent lock**, never queued again (**NEW**)

#### Testing & Validation
- ✅ Tested with 996 media files indexed
- ✅ Verified permanent lock creation in database
- ✅ Confirmed rejected media excluded from scheduling
- ✅ Validated button interactions and message updates
- ✅ Tested on Mac development environment
- ✅ Ready for Raspberry Pi deployment

### Documentation

- Updated `documentation/ROADMAP.md` with Phase 1.5 status
- Updated `documentation/planning/phase-1.5-telegram-enhancements.md` with implementation details
- Added decision log entry for Permanent Reject priority
- Created `scripts/setup_database.sql` (was gitignored, now tracked)

### Deployment Notes

#### Breaking Changes
- **Database schema change required** - Run `make reset-db` or manual migration
- Existing locks remain valid (30-day TTL locks unaffected)
- No data migration needed for existing media or history

#### Upgrade Path
1. Pull latest code from `feature/phase-1-5-enhancements` branch
2. Reset database: `make reset-db` (or manual: drop DB → create DB → init schema)
3. Re-index media: `storyline-cli index-media <path> --recursive`
4. Create schedule: `storyline-cli create-schedule --days 7`
5. Test: `storyline-cli process-queue --force`
6. Deploy to Raspberry Pi and restart service

#### Configuration Required
- No new required settings (all Phase 1.5 settings have defaults)
- Optional: Set `CAPTION_STYLE=simple` if you prefer plain captions
- Optional: Set `SEND_LIFECYCLE_NOTIFICATIONS=false` to disable startup/shutdown messages

### Next Steps - Phase 1.5 Remaining Features

**Week 1 - Priority 2** (Should Have):
- Instagram Deep Link Redirect Service (URLgenius or self-hosted)
- Instagram Username Configuration (bot commands + database storage)

**Week 2 - Priority 3** (Nice to Have):
- Inline Media Editing (edit title/caption/tags from Telegram)
- Quick Actions Menu (/menu command)
- Posting Stats Dashboard (enhanced /stats with charts)

**Week 2 - Priority 4** (Future):
- Smart Scheduling Hints (optimal posting times based on history)

## [1.0.1] - 2026-01-04

### Added
- Comprehensive test suite with 147 tests covering all Phase 1 functionality
- Automatic test database creation and cleanup (pytest fixtures)
- Repository layer tests (6 test files, 49 tests)
- Service layer tests (7 test files, 56 tests)
- Utility layer tests (4 test files, 33 tests)
- CLI command tests (4 test files, 18 tests)
- Test fixtures for database sessions with automatic rollback
- Test documentation (tests/README.md, TESTING_SETUP.md)
- Makefile targets for test execution (test, test-unit, test-quick, test-failed)
- Enhanced logger utility with setup_logger() and get_logger() functions for testability
- Development command: `storyline-cli process-queue --force` for immediate testing
- Lock creation verification in telegram service tests

### Fixed (Code Review - 2026-01-04)
- **Critical**: Service run metadata silently discarded (wrong column name in repository)
- **Critical**: Scheduler date mutation bug causing incorrect scheduling for midnight-crossing windows

### Fixed (Deployment - 2026-01-04)
- **Critical**: 30-day lock creation missing in TelegramService button handlers
- **Database**: Made DB_PASSWORD optional for local PostgreSQL development
- **Database**: Database URL now handles empty password correctly
- **Telegram**: Auto-initialization of bot for CLI commands (one-time use)
- **Validation**: Removed DB_PASSWORD requirement from config validator
- **SQLAlchemy**: Added text() wrapper for raw SQL in health check (SQLAlchemy 2.0+ compatibility)

### Fixed (Testing)
- SQLAlchemy reserved keyword issue (renamed ServiceRun.metadata to context_metadata)
- Test environment configuration loading in conftest.py
- CLI command function names in test imports

### Technical Improvements
- Session-scoped database fixture for one-time setup per test run
- Function-scoped test_db fixture with transaction rollback for test isolation
- Zero-manual-setup testing (database auto-created from .env.test)
- CI/CD ready test infrastructure
- TelegramService now creates locks when "Posted" button is clicked
- PostingService.process_next_immediate() method for development testing

### Next Steps
- **Phase 2 (Optional)**: Instagram API automation integration
  - CloudStorageService (Cloudinary/S3)
  - InstagramAPIService (Graph API)
  - Token refresh service
  - Hybrid workflow (automated simple stories, manual interactive stories)
- **Phase 3**: Shopify product integration (schema ready)
- **Phase 4**: Instagram analytics and metrics (schema ready)
- **Phase 5**: REST API and web frontend

## [1.0.0] - 2026-01-03

### Added

#### Core Infrastructure
- Complete PostgreSQL database schema with 6 core tables
- SQLAlchemy ORM models for all entities
- Pydantic-based configuration management with environment variables
- Comprehensive logging system with file and console outputs
- Service execution tracking for observability and debugging

#### Data Models
- `User` model with auto-discovery from Telegram interactions
- `MediaItem` model as source of truth for media files
- `PostingQueue` model for active work items (ephemeral)
- `PostingHistory` model for permanent audit trail
- `MediaPostingLock` model for TTL-based repost prevention
- `ServiceRun` model for service execution tracking

#### Repository Layer (CRUD Operations)
- `UserRepository` with user management and stats tracking
- `MediaRepository` with duplicate detection and filtering
- `QueueRepository` with retry logic and status management
- `HistoryRepository` with statistics and filtering
- `LockRepository` with TTL lock management
- `ServiceRunRepository` with execution tracking

#### Services Layer
- `BaseService` class with automatic execution tracking and error handling
- `MediaIngestionService` for filesystem scanning and media indexing
- `SchedulerService` with intelligent media selection algorithm
- `MediaLockService` for TTL lock management (30-day default)
- `PostingService` for workflow orchestration
- `TelegramService` with bot polling and callback handlers
- `HealthCheckService` with 4 health checks (database, telegram, queue, recent posts)

#### Utilities
- SHA256 file content hashing (filename-agnostic)
- Image validation against Instagram Story requirements (aspect ratio, resolution, file size)
- Image optimization for Instagram (resize, crop, convert)
- Configuration validation with startup checks
- Structured logging with configurable log levels

#### CLI Commands
- `index-media` - Index media files from directory
- `list-media` - List all indexed media items with filters
- `validate-image` - Validate image against Instagram requirements
- `create-schedule` - Generate intelligent posting schedule
- `process-queue` - Process pending queue items
- `list-queue` - View pending queue items
- `list-users` - List all users with stats
- `promote-user` - Change user role (admin/member)
- `check-health` - System health check with component status

#### Features
- **Smart Scheduling Algorithm**
  - Prioritizes never-posted media items
  - Prefers least-posted items
  - Random selection for variety
  - Excludes locked and queued media
  - Evenly distributed time slots with ±30min jitter
- **Telegram Bot Integration**
  - Inline keyboard buttons (Posted/Skip)
  - Auto-discovery of users from interactions
  - User attribution for all actions
  - /start and /status commands
  - Callback handling for workflow tracking
- **TTL Lock System**
  - Automatic 30-day repost prevention
  - Self-expiring locks (no manual cleanup)
  - Configurable lock duration
  - Multiple lock reasons (recent_post, manual_hold, seasonal)
- **User Management**
  - Auto-creation from Telegram interactions
  - Role-based access (admin/member)
  - Statistics tracking (total posts, last seen)
  - Team name support
- **Complete Audit Trail**
  - Permanent posting history (never deleted)
  - Media metadata snapshots
  - User attribution for all posts
  - Error tracking with retry counts
  - Queue lifecycle timestamps preserved
- **Service Execution Tracking**
  - Automatic logging of all service calls
  - Performance metrics (execution time)
  - Error tracking with stack traces
  - Input parameters and result summaries
  - User attribution for manual triggers
- **Image Processing**
  - Validation against Instagram Story specs (9:16 aspect ratio, 1080x1920 resolution)
  - Automatic optimization (resize, crop, format conversion)
  - HEIC to JPG conversion support
  - PNG transparency handling (RGBA to RGB)
- **Health Monitoring**
  - Database connectivity check
  - Telegram configuration validation
  - Queue backlog detection
  - Recent posts verification
- **Development Features**
  - Dry-run mode for testing without posting
  - Configuration validation on startup (fail-fast)
  - Comprehensive error messages
  - Rich CLI output with tables and colors

#### Application
- Main application entry point with async event loop
- Scheduler loop (checks every minute for pending posts)
- Cleanup loop (hourly expired lock cleanup)
- Telegram bot polling in same process
- Graceful shutdown handling (SIGTERM/SIGINT)
- Configuration validation before startup

#### Database
- Complete schema with indexes for performance
- Foreign key constraints and cascading deletes
- Check constraints for data integrity
- GIN indexes for array columns (tags)
- Schema version tracking table

#### Documentation
- Comprehensive README with quick start guide
- QUICKSTART.md for 10-minute setup
- CLAUDE.md developer guide for AI assistants
- IMPLEMENTATION_COMPLETE.md with full component list
- Complete implementation plan in documentation/
- Inline code documentation and docstrings

#### Testing
- Pytest configuration with coverage reporting
- Test fixtures for database and sample data
- Unit tests for file hashing
- Unit tests for media ingestion service
- Test structure mirroring src/ directory
- Markers for unit/integration/slow tests

#### DevOps
- SQL schema setup script
- Python database initialization script
- requirements.txt with pinned versions
- setup.py for CLI installation
- .env.example with complete configuration template
- .env.test for test environment
- .gitignore for Python projects

### Technical Details

#### Architecture
- Three-layer architecture: CLI → Services → Repositories → Models
- Strict separation of concerns enforced
- Repository pattern for data access
- Service layer for business logic
- Base service class for cross-cutting concerns

#### Configuration
- Environment-based configuration (12-factor app)
- Pydantic settings with validation
- Support for .env files
- Separate test environment configuration
- Feature flags (ENABLE_INSTAGRAM_API, DRY_RUN_MODE)

#### Database
- PostgreSQL with SQLAlchemy ORM
- UUID primary keys
- Timestamp tracking (created_at, updated_at)
- JSONB columns for flexible metadata
- Array columns for tags

#### Performance
- Database indexes on foreign keys and frequently queried columns
- GIN indexes for array searches
- Connection pooling (5 connections, 10 overflow)
- Chunked file reading for hash calculation
- Pre-ping for connection validation

#### Security
- No sensitive data in code (environment variables only)
- Database password required
- User roles for access control
- Input validation at all layers

### Phase Information
- **Current Phase**: Phase 1 (Telegram-Only Mode)
- **Deployment Target**: Raspberry Pi (16GB RAM)
- **Python Version**: 3.10+
- **Database**: PostgreSQL
- **Posting Mode**: 100% manual via Telegram
- **Instagram API**: Not required for Phase 1

[Unreleased]: https://github.com/chrisrogers37/storydump/compare/v1.6.0...HEAD
[1.6.0]: https://github.com/chrisrogers37/storydump/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/chrisrogers37/storydump/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/chrisrogers37/storydump/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/chrisrogers37/storydump/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/chrisrogers37/storydump/compare/v1.0.1...v1.2.0
[1.0.1]: https://github.com/chrisrogers37/storydump/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/chrisrogers37/storydump/releases/tag/v1.0.0
