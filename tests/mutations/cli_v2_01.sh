#!/bin/zsh
# Mutation battery for phase 01 of the v2 CLI (tokens as principals): each mutation must make
# its named test FAIL ("killed"); the file is restored from the COMMITTED tree after each, so
# commit first. Run from the repo root with the sandbox off (the units resolve DNS; the gates
# need the Docker postgres on 65433). `ONLY=<regex>` runs a subset.
set -u
cd /Users/chris/Projects/storydump || exit 2
KEY=$(.venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE PYTHONDONTWRITEBYTECODE=1 TELEGRAM_BOT_TOKEN=dummy TELEGRAM_CHANNEL_ID=1 ADMIN_TELEGRAM_CHAT_ID=1 DB_PORT=65432 ENCRYPTION_KEY=$KEY .venv/bin/pytest -q -p no:cacheprovider --no-cov -x"
GATE="env PYTHONDONTWRITEBYTECODE=1 PATH=/opt/homebrew/opt/postgresql@15/bin:$PATH DB_HOST=localhost DB_PORT=65433 DB_USER=test_user DB_PASSWORD=test_password DB_NAME=storyline_ai TEST_DB_NAME=storyline_test REQUIRE_TEST_DATABASE=1 TELEGRAM_BOT_TOKEN=dummy TELEGRAM_CHANNEL_ID=1 ADMIN_TELEGRAM_CHAT_ID=1 ENCRYPTION_KEY=$KEY .venv/bin/pytest -q -p no:cacheprovider --no-cov -x"
WEB="cd /Users/chris/Projects/storydump/landing && npx vitest run --reporter=dot"
mkdir -p /tmp/claude

check() {  # name file old new runner test-selector
  local name=$1 file=$2 old=$3 new=$4 runner=$5 sel=$6
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  OLD="$old" NEW="$new" .venv/bin/python - "$file" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text(); old = os.environ["OLD"]; new = os.environ["NEW"]
if s.count(old) != 1:
    print(f"MUTATION NOT APPLIED ({s.count(old)} matches)"); sys.exit(3)
p.write_text(s.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  # Two mutations of the SAME size written within the same second leave the interpreter a
  # bytecode cache it considers valid (mtime + size match), so the second run executes the
  # first mutation's code — the expiry mutation "survived" twice that way. Purge it.
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$runner $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  local summary; summary=$(grep -E '^=+ .*(passed|failed|error|deselected|no tests ran)|Tests +[0-9]|No test files found' /tmp/claude/mut.log | tail -1)
  # A selector that matches no test exits non-zero too — that is not a kill. Nor is a kill by a
  # collection or fixture error a test's verdict; both are flagged for a human to read.
  if grep -qE '/ 0 selected|no tests ran|No test files found' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name  [$summary]"
  elif [ $rc -eq 0 ]; then echo "SURVIVED (bad): $name  [$summary]"
  elif ! grep -qE '^=+ .*[0-9]+ failed|Tests +[0-9]+ failed' /tmp/claude/mut.log; then echo "KILLED BY ERROR (check): $name  [$summary]"
  else echo "killed: $name  [$summary]"; fi
  cd /Users/chris/Projects/storydump && git checkout -- "$file"
}
PR=src/api/principal.py
V1=src/api/routes/v1.py
TR=src/api/routes/tokens.py
ST=src/services/target/service_tokens.py
AP=src/api/app.py
MR=scripts/migration_runner.py
VO=src/services/target/vocabulary.py
TP=tests/src/api/test_token_principal.py
TT=tests/src/api/test_token_routes.py
TS=tests/src/services/target/test_service_tokens.py
GT=tests/scripts/test_service_tokens_gate.py

# --- the principal ----------------------------------------------------------
check "a prefixed bearer is not routed to the token resolver" $PR '    if bearer is not None and service_tokens.is_token(bearer):' '    if False and bearer is not None and service_tokens.is_token(bearer):' "$UNIT" "$TP -k a_prefixed_bearer_resolves"
check "the cookie can carry a token" $PR '    bearer = presented_bearer(request)
    if bearer is not None and service_tokens.is_token(bearer):' '    bearer = presented_token(request)
    if bearer is not None and service_tokens.is_token(bearer):' "$UNIT" "$TP -k cookie_that_happens_to_start"
check "require_session lets a token through" $PR '    if principal.is_token:
        raise TokenRefused(
            "session_required"' '    if False:
        raise TokenRefused(
            "session_required"' "$UNIT" "$TP -k every_route_outside_the_allowlist"
check "a token shares the dedup namespace with sessions" $PR '            return f"token:{self.token_id}"' '            return str(self.token_id)' "$UNIT" "$TT -k acts_as_the_person_over_cli"
check "a service identity audits as a user" $PR '        return "operator" if self.is_service_identity else "user"' '        return "user"' "$UNIT" "$TP -k audits_as_operator"
check "the minting route admits a token" $PR '        ("GET", "/api/v1/me/principal"),' '        ("GET", "/api/v1/me/principal"),
        ("POST", "/api/v1/me/tokens"),' "$UNIT" "$TP -k names_no_minting_route"

# --- the app factory --------------------------------------------------------
check "a dead token is unmapped (500, not 401)" $AP '    "invalid_token": 401,' '' "$UNIT" "$TP -k dead_token_is_401"
check "session_required is answered without its reason" $AP '        return JSONResponse(
            status_code=status, content={"detail": str(exc), "reason": exc.reason}
        )

    @app.exception_handler(TokenArgsInvalid)' '        return JSONResponse(status_code=status, content={"detail": str(exc)})

    @app.exception_handler(TokenArgsInvalid)' "$UNIT" "$TP -k every_route_outside_the_allowlist"
check "a bad mint body is a 500" $AP '    @app.exception_handler(TokenArgsInvalid)
    async def _token_args(request: Request, exc: TokenArgsInvalid):
        return JSONResponse(
            status_code=400, content={"detail": str(exc), "reason": "invalid_args"}
        )' '    async def _token_args(request: Request, exc: TokenArgsInvalid):
        return JSONResponse(
            status_code=400, content={"detail": str(exc), "reason": "invalid_args"}
        )' "$UNIT" "$TT -k bad_body_is_400"

# --- the command route under a token ---------------------------------------
check "a service identity may write" $V1 '        raise TokenRefused("readonly_token", "a service identity reads only")' '        return None' "$UNIT" "$TT -k service_identity_never_writes"
check "a readonly person token may write" $V1 '    if principal.token_role != "operator":
        raise TokenRefused("readonly_token", "this token is read-only")' '    if False:
        raise TokenRefused("readonly_token", "this token is read-only")' "$UNIT" "$TT -k readonly_person_token_is_refused"
check "a service identity may address another workspace" $V1 '        require_own_workspace(principal, workspace_id)
        raise TokenRefused("readonly_token", "a service identity reads only")' '        raise TokenRefused("readonly_token", "a service identity reads only")' "$UNIT" "$TT -k another_workspace_is_wrong_workspace"
check "the command rides the web channel under a token" $V1 '        channel=principal.channel,
        args={**body, **(extra or {})},' '        channel=CHANNEL,
        args={**body, **(extra or {})},' "$UNIT" "$TT -k acts_as_the_person_over_cli"
check "the token name never reaches the command" $V1 '        actor_label=principal.token_name,' '        actor_label=None,' "$UNIT" "$TT -k acts_as_the_person_over_cli"
check "no cli_command row is written" $V1 '        if principal.is_token:
            await _audit_cli_command(' '        if False:
            await _audit_cli_command(' "$UNIT" "$TT -k acts_as_the_person_over_cli"
check "the cli_command row is written for sessions too" $V1 '        if principal.is_token:
            await _audit_cli_command(' '        if True:
            await _audit_cli_command(' "$UNIT" "$TT -k session_writes_no_cli_command_row"
check "the tenant transaction claims the web channel for a token" $V1 '        channel=principal.channel,
    ).begin()' '        channel=CHANNEL,
    ).begin()' "$GATE" "$GT -k end_to_end"
check "the cli_command row forgets the story" $V1 '            entity_kind, entity_id = "post_intent", str(uuid.UUID(intent_id))' '            entity_kind, entity_id = "workspace", workspace_id' "$GATE" "$GT -k end_to_end"
check "the cli_command row forgets the token" $V1 '        "token_id": principal.token_id,' '        "token_id": None,' "$GATE" "$GT -k end_to_end"

# --- the token routes -------------------------------------------------------
check "the body raises a service identity's role" $TR '            role="readonly",
            workspace_id=str(ws),' '            role=body.get("role", "readonly"),
            workspace_id=str(ws),' "$UNIT" "$TT -k service_role_cannot_be_raised"
check "a person's mint is open to tokens" $TR 'async def mint_my_token(
    request: Request, principal: Principal = Depends(require_session)
):' 'async def mint_my_token(
    request: Request, principal: Principal = Depends(current_principal)
):' "$UNIT" "$TP -k require_session_is_the_dependency"
check "a service identity lists any workspace's identities" $TR '    if principal.is_service_identity:
        require_own_workspace(principal, str(ws))
        async with v1._open_tenant(request, str(ws), principal) as session:
            rows = await service_tokens.list_for_workspace(' '    if principal.is_service_identity:
        async with v1._open_tenant(request, str(ws), principal) as session:
            rows = await service_tokens.list_for_workspace(' "$UNIT" "$TT -k lists_its_own_workspace_only"
check "a service identity revokes its siblings" $TR '        if token_id != principal.token_id:
            raise TokenRefused(
                "readonly_token", "a service identity may revoke only itself"
            )' '' "$UNIT" "$TT -k may_revoke_only_itself"
check "a person's revoke ignores the subject" $TR '        revoked = await service_tokens.revoke(
            conn, token_id=str(token_id), user_id=principal.user_id
        )' '        revoked = await service_tokens.revoke(
            conn, token_id=str(token_id), user_id=principal.user_id
        ) or True' "$UNIT" "$TT -k someone_elses_token_is_not_found"
check "a service identity has personal tokens" $TR '    if principal.is_service_identity:
        raise TokenRefused("session_required", "a service identity has no person")
    engine = require_engine(request)
    async with engine.connect() as conn:' '    engine = require_engine(request)
    async with engine.connect() as conn:' "$UNIT" "$TT -k service_identity_has_no_personal_tokens"

# --- the token service ------------------------------------------------------
check "the secret is stored, not its hash" $ST '                    "h": token_hash(secret),' '                    "h": secret,' "$UNIT" "$TS -k stores_only_the_hash"
check "a token may have two subjects" $ST '    if (user_id is None) == (workspace_id is None):' '    if user_id is None and workspace_id is None:' "$UNIT" "$TS -k refuses_bad_arguments"
check "the name has no ceiling" $ST '    if not clean or len(clean) > NAME_MAX:' '    if not clean:' "$UNIT" "$TS -k refuses_bad_arguments"
check "the expiry has no ceiling" $ST '        or not 1 <= expires_in_days <= MAX_EXPIRY_DAYS' '        or not 1 <= expires_in_days' "$UNIT" "$TS -k refuses_bad_arguments"
check "a revoked token resolves" $ST '    if row["revoked"]:
        raise TenantResolutionError("revoked_token")' '    if False:
        raise TenantResolutionError("revoked_token")' "$UNIT" "$TS -k revoked_wins"
check "an expired token resolves" $ST '    if row["expired"]:
        raise TenantResolutionError("expired_token")' '    if False:
        raise TenantResolutionError("expired_token")' "$UNIT" "$TS -k expired_is_expired"
check "a disabled person's token resolves" $ST '    if row["user_id"] is not None and row["user_state"] != "active":' '    if False:' "$UNIT" "$TS -k disabled_person"
check "an inactive workspace's identity resolves" $ST '        if state is None or state["state"] != "active":' '        if False:' "$UNIT" "$TS -k needs_an_active_workspace"
check "the use is never stamped" $ST '  UPDATE service_tokens s SET last_used_at = now()' '  UPDATE service_tokens s SET last_used_at = s.last_used_at' "$GATE" "$GT -k end_to_end"
check "revoke ignores the subject" $ST '            " WHERE id = :id AND revoked_at IS NULL AND user_id = :u"' '            " WHERE id = :id AND revoked_at IS NULL"' "$UNIT" "$TS -k scoped_to_the_subject"

# --- the runner's ledger grant (F7 c) ---------------------------------------
check "the runner never grants its ledger" $MR '        cur.execute(LEDGER_GRANT_SQL)' '        pass' "$GATE" "tests/scripts/test_migration_runner_ledger_grant.py -k may_read_the_ledger"

# --- the vocabulary ---------------------------------------------------------
check "a token refusal leaves the vocabulary" $VO '    "wrong_workspace",
)

#: Why a presented token' '
)

#: Why a presented token' "$UNIT" "$TP -k token_refusal_reasons_are_the_vocabulary"
check "an exit code moves" $VO '    if status in (401, 403):
        return EXIT_NOT_AUTHORIZED' '    if status in (401, 403):
        return EXIT_REFUSED' "$UNIT" "tests/storydump_cli -k exit"
check "the envelope accepts a document with both data and error" $VO '    if (data is None) == (error is None):
        raise ValueError("exactly one of data/error must be present")' '    if data is None and error is None:
        raise ValueError("exactly one of data/error must be present")' "$UNIT" "tests/src/services/target/test_vocabulary.py -k malformed"

# --- the adversarial fold ---------------------------------------------------
check "a readonly token revokes the person's other tokens" $TR '        and token_id != principal.token_id
    ):
        raise TokenRefused("readonly_token", "a read-only token may revoke only itself")' '        and False
    ):
        raise TokenRefused("readonly_token", "a read-only token may revoke only itself")' "$UNIT" "$TT -k cannot_revoke_the_persons_other_tokens"
check "the cli_command row trusts the caller's intent id" $V1 '    intent_id = result.data.get("intent_id") if isinstance(result.data, dict) else None' '    intent_id = command.args.get("intent_id")' "$UNIT" "$TT -k claimed_intent_id_never_becomes"
check "a token reason is unmapped" $AP '    "wrong_workspace": 403,
}' '}' "$UNIT" "tests/src/api/test_app_factory.py -k token_reasons"
check "a bearer goes over plain http to any host" storydump_cli/client.py '    if parsed.scheme == "http" and (host in LOOPBACK_HOSTS or host.startswith("127.")):' '    if parsed.scheme == "http":' "$UNIT" "tests/storydump_cli/test_client.py -k plain_http"
check "a file login leaves the keychain copy" storydump_cli/commands/auth.py '        try:  # one stored token, not two: an earlier keychain login goes
            runtime.backend().delete()' '        try:  # one stored token, not two: an earlier keychain login goes
            pass' "$UNIT" "tests/storydump_cli/test_main.py -k keychain_copy"
check "a session may wear the token prefix" src/services/target/sessions.py '        if not value.startswith(vocabulary.TOKEN_PREFIX):' '        if True:' "$UNIT" "tests/src/services/target/test_sessions.py"
# The authentication transaction rolls back on refusal, so a refused attempt never persists a
# stamp with or without the guard (the gate proves that); the guard is pinned by its unit test.
check "the person-bound stamp is not gated on the person being active" $ST "    \"     AND t.user_id IS NOT NULL AND t.user_state = 'active'\"" "" "$UNIT" "$TS -k gated_on_the_person"
check "a refused service identity is stamped" $ST '        await executor.execute(
            _STAMP, {"id": str(row["id"]), "throttle": STAMP_THROTTLE_SECONDS}
        )' '        pass' "$UNIT" "$TS -k live_service_identity_resolves"
