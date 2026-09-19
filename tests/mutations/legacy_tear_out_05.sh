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
PLANTED=
# an interrupt must not leave a planted legacy page or a mutated file behind
trap '[ -n "$PLANTED" ] && rm -f "$PLANTED"; git -C "$ROOT" checkout -- . 2>/dev/null' INT TERM

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
  mkdir -p "$(dirname "$file")"; PLANTED=$file; print -r -- "$content" > "$file"
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
check "the pin stops reading table names" $TDOCS '    names = _legacy_only_tables() + _deleted_paths() + _retired_variables()' '    names = _deleted_paths() + _retired_variables()' "$CONTROL"
check "the pin stops reading deleted paths" $TDOCS '    names = _legacy_only_tables() + _deleted_paths() + _retired_variables()' '    names = _legacy_only_tables() + _retired_variables()' "$CONTROL"
check "the pin stops reading dead variables" $TDOCS '    names = _legacy_only_tables() + _deleted_paths() + _retired_variables()' '    names = _legacy_only_tables() + _deleted_paths()' "$CONTROL"
check "a snapshot's name reads as its table's (the word boundary goes)" $TDOCS '        return re.compile(rf"\b{name}\b", re.IGNORECASE)' '        return re.compile(name, re.IGNORECASE)' "$CONTROL"
check "a table in capitals passes (a SQL example names the legacy table unseen)" $TDOCS '        return re.compile(rf"\b{name}\b", re.IGNORECASE)' '        return re.compile(rf"\b{name}\b")' "$CONTROL"
check "a deleted module in its dotted spelling passes" $TDOCS '        return re.compile(re.escape(name).replace("/", "[/.]") + r"(?![\w-])")' '        return re.compile(re.escape(name) + r"(?![\w-])")' "$CONTROL"
check "a live module that shares a deleted package's letters reads as deleted (the trailing boundary goes)" $TDOCS '        return re.compile(re.escape(name).replace("/", "[/.]") + r"(?![\w-])")' '        return re.compile(re.escape(name).replace("/", "[/.]"))' "$CONTROL"
check "a deleted FILE is matched with its suffix only (the dotted module spelling passes)" $TDOCS '    return DELETED_PACKAGES + tuple(f.removesuffix(".py") for f in DELETED_FILES)' '    return DELETED_PACKAGES + tuple(DELETED_FILES)' "$CONTROL"
check "a live target column reads as a dead variable (the variables are case-folded)" $TDOCS '    return re.compile(rf"\b{name}\b")' '    return re.compile(rf"\b{name}\b", re.IGNORECASE)' "$CONTROL"
check "the names both tiers use read as legacy names (the derivation ignores the target)" $TDOCS '    reused = _target_table_names()
    return tuple(t for t in LEGACY_TABLES if t not in reused)' '    return tuple(LEGACY_TABLES)' "$CONTROL"
check "the agent pages leave the live roots" $TDOCS '    ".claude",
    "documentation/operations",' '    "documentation/operations",' "$TDOCS -k live_roots_cover"
check "the nested agent pages leave the live roots (the rules are under .claude/rules/)" $TDOCS '        pages += [path] if path.is_file() else sorted(path.rglob("*.md"))' '        pages += [path] if path.is_file() else sorted(path.glob("*.md"))' "$TDOCS -k live_roots_cover"
check "the plans and the archive read as live pages" $TDOCS '        pages += sorted((ROOT / root).glob("*.md"))' '        pages += sorted((ROOT / root).rglob("*.md"))' "$TDOCS -k live_roots_cover"
check "the documentation directory's own pages leave the live roots" $TDOCS 'LIVE_FLAT_ROOTS = ("documentation",)' 'LIVE_FLAT_ROOTS = ()' "$TDOCS -k live_roots_cover"
check "the exemptions stop being honoured" $TDOCS '        found = _legacy_names_in(page.read_text()) - set(
            LEGACY_NAME_EXEMPT.get(rel, {})
        )' '        found = _legacy_names_in(page.read_text())' "$PIN"
check "an exemption nothing uses stays" $TDOCS 'LEGACY_NAME_EXEMPT: dict[str, dict[str, tuple[int, str]]] = {' 'LEGACY_NAME_EXEMPT: dict[str, dict[str, tuple[int, str]]] = {
    "README.md": {"posting_queue": (1, "nothing on the page uses this")},' "$TDOCS -k exemption_is_exact"
check "a page that names the thing FEWER times than its exemption was read for passes" $TDOCS '            elif (found := len(_pattern(name).findall(text))) != count:' '            elif (found := len(_pattern(name).findall(text))) > count:' "$TDOCS -k exemption_is_read_again"
check "an exemption for a name that is no legacy name passes" $TDOCS '            if name not in known:' '            if False:' "$TDOCS -k exemption_is_read_again"
check "an exemption for no mention at all passes" $TDOCS '            elif count < 1:' '            elif False:' "$TDOCS -k exemption_is_read_again"
append "a stale mention hides beside an exempt one (the pager page gains a line about the worker's token)" documentation/operations/posting-monitor.md 'Set `TELEGRAM_BOT_TOKEN` on the worker service too.' "$TDOCS -k exemption_is_exact"

# --- which pages are live -----------------------------------------------------------------------------
check "an archived page under a live root reads as live" $TDOCS '    return [p for p in pages if "archive" not in p.relative_to(ROOT).parts]' '    return pages' "$TDOCS -k archive_under_a_live_root"
check "a checkout under a directory named archive has no live pages (the pin passes over nothing)" $TDOCS '    return [p for p in pages if "archive" not in p.relative_to(ROOT).parts]' '    return [p for p in pages if "archive" not in p.parts]' "$TDOCS -k archive_under_a_live_root"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
