#!/bin/zsh
# Mutation battery for the legacy tear-out, phase 05 (the documentation's end state): the phase ships no
# code — its product is the live pages and the pin that keeps a legacy name from returning to one
# (tests/test_agent_docs.py). Each behaviour of the pin has one named mutation that must make its test
# FAIL ("killed"): a legacy name planted back into a live page, or the predicate weakened. Files are
# restored from the COMMITTED tree after each, so commit first. Needs no database. `ONLY=<regex>` runs a
# subset; `STORYDUMP_ROOT` points the battery at a worktree. The recipe sets no variable of either tier.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=/Users/chris/Projects/storydump/.venv/bin/python
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL PYTHONDONTWRITEBYTECODE=1 DB_PORT=65432 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
mkdir -p /tmp/claude
RAN=0
EXPECTED=$(grep -cE '^(check|append|plant) "' "$0")

verdict() {  # name rc — reads /tmp/claude/mut.log
  local name=$1 rc=$2
  local summary; summary=$(grep -E '^=+ .*(passed|failed|error|deselected|no tests ran)' /tmp/claude/mut.log | tail -1)
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name  [$summary]"
  elif [ $rc -eq 0 ]; then echo "SURVIVED (bad): $name  [$summary]"
  elif ! grep -qE '^=+ .*[0-9]+ failed' /tmp/claude/mut.log; then echo "KILLED BY ERROR (check): $name  [$summary]"
  else echo "killed: $name  [$summary]"; fi
}

mutate() {  # file old new — exit 3 unless `old` occurs exactly once
  OLD="$2" NEW="$3" $PY - "$1" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text(); old = os.environ["OLD"]; new = os.environ["NEW"]
if s.count(old) != 1:
    print(f"MUTATION NOT APPLIED ({s.count(old)} matches)"); sys.exit(3)
p.write_text(s.replace(old, new, 1))
PY
}

check() {  # name file old new test-selector — the predicate weakened
  local name=$1 file=$2 old=$3 new=$4 sel=$5
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$UNIT $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

append() {  # name file line test-selector — a legacy name written back into a live page
  local name=$1 file=$2 line=$3 sel=$4
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  if [ ! -f "$file" ]; then echo "MUTATION NOT APPLIED (no such page): $name"; return; fi
  print -r -- "" >> "$file"; print -r -- "$line" >> "$file"
  eval "$UNIT $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

plant() {  # name new-file content test-selector — a file that must NOT exist, created
  local name=$1 file=$2 content=$3 sel=$4
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  if [ -e "$file" ]; then echo "MUTATION NOT APPLIED (exists): $name"; return; fi
  mkdir -p "$(dirname "$file")"; print -r -- "$content" > "$file"
  eval "$UNIT $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  rm -f "$file"; rmdir "$(dirname "$file")" 2>/dev/null
}

TDOCS=tests/test_agent_docs.py
PIN="$TDOCS -k no_live_page_names_the_legacy_tier"
CONTROL="$TDOCS -k pin_sees_a_planted_legacy_name"

# --- a legacy name written back into a live page ----------------------------------------------------
append "a legacy table returns to the database rule" .claude/rules/database.md 'Rows wait in `posting_queue` until the scheduler picks them.' "$PIN"
append "the safety block names a legacy table again" CLAUDE.md 'Never run mutating SQL on `posting_history`.' "$PIN"
append "a deleted module path returns to the canonical guide" AGENTS.md 'The readers live in `src/repositories/`.' "$PIN"
append "a dead variable returns to a runbook" documentation/operations/monitoring.md 'Set `WORKER_IMPL=target` on the worker service first.' "$PIN"
append "a legacy table returns to a guide" documentation/guides/deployment.md 'Check `chat_settings` for the active account.' "$PIN"
plant "a new live page describes the legacy tier" documentation/operations/legacy-queue-howto.md '# How to read `posting_queue`' "$PIN"

# --- the predicate weakened ----------------------------------------------------------------------------
check "the pin stops reading table names" $TDOCS '    hits = {t for t in _legacy_only_tables() if re.search(rf"\b{t}\b", text)}' '    hits = set()' "$CONTROL"
check "the pin stops reading deleted paths" $TDOCS '    hits |= {p for p in DELETED_PATHS if p in text}' '    hits |= set()' "$CONTROL"
check "the pin stops reading dead variables" $TDOCS '    hits |= {v for v in _retired_variables() if re.search(rf"\b{v}\b", text)}' '    hits |= set()' "$CONTROL"
check "a snapshot's name reads as its table's (the word boundary goes)" $TDOCS '    hits = {t for t in _legacy_only_tables() if re.search(rf"\b{t}\b", text)}' '    hits = {t for t in _legacy_only_tables() if t in text}' "$CONTROL"
check "the names both tiers use read as legacy names (the derivation ignores the target)" $TDOCS '    reused = _target_table_names()
    return tuple(t for t in LEGACY_TABLES if t not in reused)' '    return tuple(LEGACY_TABLES)' "$CONTROL"
check "the agent pages leave the live roots" $TDOCS '    ".claude",
    "documentation/operations",' '    "documentation/operations",' "$TDOCS -k live_roots_cover"
check "the exemptions stop being honoured" $TDOCS '        found = _legacy_names_in(page.read_text()) - set(
            LEGACY_NAME_EXEMPT.get(rel, {})
        )' '        found = _legacy_names_in(page.read_text())' "$PIN"
check "an exemption nothing uses stays" $TDOCS 'LEGACY_NAME_EXEMPT: dict[str, dict[str, str]] = {' 'LEGACY_NAME_EXEMPT: dict[str, dict[str, str]] = {
    "README.md": {"posting_queue": "nothing on the page uses this"},' "$TDOCS -k exemption_is_still_used"

# --- the deleted paths stay deleted ------------------------------------------------------------------------
plant "a deleted package comes back" src/repositories/__init__.py '"""back"""' "$TDOCS -k deleted_paths_are_gone"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
