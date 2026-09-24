#!/bin/zsh
# Mutation battery for the transit sweep's unreadable-age rule and the one ISO-8601 parse behind it: each
# behaviour has one named mutation that must make its test FAIL ("killed") — and must PASS on the clean
# tree first, or the verdict is BASELINE RED; a selector that selects nothing is NO TEST SELECTED, never a
# kill. Files are restored from the COMMITTED tree after each, so commit first. No database: the sweep is
# driven through a recording SDK fake. `STORYDUMP_ROOT` points the battery at a worktree.
#
# The interpreter is part of the verdict. `fromisoformat` is narrow on 3.10 (CI's interpreter) and wide
# from 3.11, so a mutation that hands a value to it raw is observable on one side only: those checks are
# tagged `3.10` or `3.11+` and report NOT APPLICABLE on the other. `STORYDUMP_PY` picks the interpreter —
# run the battery under each to cover every check.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=${STORYDUMP_PY:-/Users/chris/Projects/storydump/.venv/bin/python}
cd "$ROOT" || exit 2
PYVER=$($PY -c 'import sys; print("%d.%d" % sys.version_info[:2])') || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL DB_PORT=65432 PYTHONDONTWRITEBYTECODE=1 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
# One log per run, never a fixed path: two batteries on one host must not read each other's verdicts.
LOG=$(mktemp) || exit 2
trap 'rm -f "$LOG"' EXIT
RAN=0
SKIPPED=0
EXPECTED=$(grep -cE '^check "' "$0")
verdict() {
  local name=$1 rc=$2
  local summary; summary=$(grep -E '^=+ .*(passed|failed|error|deselected|no tests ran)' "$LOG" | tail -1)
  if grep -qE '/ 0 selected|no tests ran' "$LOG"; then echo "NO TEST SELECTED (bad): $name  [$summary]"
  elif [ $rc -eq 0 ]; then echo "SURVIVED (bad): $name  [$summary]"
  elif ! grep -qE '^=+ .*[0-9]+ failed' "$LOG"; then echo "KILLED BY ERROR (check): $name  [$summary]"
  else echo "killed: $name  [$summary]"; fi
}
mutate() {
  OLD="$2" NEW="$3" $PY - "$1" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text(); old = os.environ["OLD"]; new = os.environ["NEW"]
if s.count(old) != 1:
    print(f"MUTATION NOT APPLIED ({s.count(old)} matches)"); sys.exit(3)
p.write_text(s.replace(old, new, 1))
PY
}
check() {  # name file old new test-selector [3.10 | 3.11+]
  local name=$1 file=$2 old=$3 new=$4 sel=$5 where=${6:-}
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  if { [ "$where" = "3.10" ] && [ "$PYVER" != "3.10" ]; } || { [ "$where" = "3.11+" ] && [ "$PYVER" = "3.10" ]; }; then
    SKIPPED=$((SKIPPED + 1)); echo "NOT APPLICABLE on $PYVER (observable on $where): $name"; return
  fi
  RAN=$((RAN + 1))
  eval "$UNIT $sel" > "$LOG" 2>&1; local base=$?
  if grep -qE '/ 0 selected|no tests ran' "$LOG"; then echo "NO TEST SELECTED (bad): $name  [$(grep -E '^=+ .*(selected|no tests ran)' "$LOG" | tail -1)]"; return; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [$(grep -E '^=+ .*(passed|failed|error)' "$LOG" | tail -1)]"; return; fi
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$UNIT $sel" > "$LOG" 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

T="tests/src/services/target/test_transit.py"
D="tests/src/utils/test_datetime_utils.py"

# The sweep: reaping needs an age that was READ.
check "an unreadable age is reaped" src/services/target/transit.py '                    if created is None:
                        unread.append((row.get("public_id"), raw))
                    elif created < cutoff:' '                    if created is None:
                        unread.append((row.get("public_id"), raw))
                    if created is None or created < cutoff:' "$T -k unreadable_age"
check "an unreadable age is reaped, at the door that deletes" src/services/target/transit.py '                    if created is None:
                        unread.append((row.get("public_id"), raw))
                    elif created < cutoff:' '                    if created is None:
                        unread.append((row.get("public_id"), raw))
                    if created is None or created < cutoff:' "$T -k destroys_nothing_in_flight"
check "the unreadable rows go unnamed" src/services/target/transit.py '        if unread:
            logger.error(' '        if False:
            logger.error(' "$T -k unreadable_age"
check "one error per row, not one per sweep" src/services/target/transit.py '        if unread:
            logger.error(' '        for _ in unread:
            logger.error(' "$T -k unreadable_age"
check "a non-string age reaches the parse" src/services/target/transit.py '        if not isinstance(value, str):
            return None
' '' "$T -k unreadable_age"
check "the sweep parses with the interpreter's bare fromisoformat" src/services/target/transit.py '            return ensure_utc(parse_iso_timestamp(value))' '            return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))' "$T -k provider_format_shift_does_not_reap"

# The parse: one shape, the same answer on every interpreter.
check "a short fraction pads on the wrong side" src/utils/datetime_utils.py '        canonical += "." + fraction[:6].ljust(6, "0")' '        canonical += "." + fraction[:6].rjust(6, "0")' "$D -k fraction_width"
check "a long fraction rounds instead of truncating" src/utils/datetime_utils.py '        canonical += "." + fraction[:6].ljust(6, "0")' '        canonical += "." + str(round(int(fraction.ljust(9, "0")[:9]) / 1000)).zfill(6)[:6]' "$D -k fraction_width"
check "the fraction reaches fromisoformat as it came" src/utils/datetime_utils.py '        canonical += "." + fraction[:6].ljust(6, "0")' '        canonical += "." + fraction' "$D -k fraction_width" 3.10
check "the offset minutes are dropped" src/utils/datetime_utils.py '        canonical += offset_hours + ":" + (offset_minutes or "00")' '        canonical += offset_hours + ":00"' "$D -k offset_spelling"
check "a negative offset reads as positive" src/utils/datetime_utils.py '        canonical += offset_hours + ":" + (offset_minutes or "00")' '        canonical += offset_hours.replace("-", "+") + ":" + (offset_minutes or "00")' "$D -k offset_spelling"
check "Z reads as no offset" src/utils/datetime_utils.py '    if zulu is not None:
        canonical += "+00:00"' '    if zulu is not None:
        pass' "$D -k offset_spelling"
check "the separator narrows to T" src/utils/datetime_utils.py '    r"(?:[Tt ](\d{2}:\d{2})' '    r"(?:T(\d{2}:\d{2})' "$D -k separator"
check "a missing offset defaults to UTC in the parse" src/utils/datetime_utils.py '        canonical += offset_hours + ":" + (offset_minutes or "00")
    return datetime.fromisoformat(canonical)' '        canonical += offset_hours + ":" + (offset_minutes or "00")
    else:
        canonical += "+00:00"
    return datetime.fromisoformat(canonical)' "$D -k offsetless"
check "a value outside the shape falls back to fromisoformat" src/utils/datetime_utils.py '    if match is None:
        raise ValueError(f"not an extended-format ISO-8601 timestamp: {value!r}")' '    if match is None:
        return datetime.fromisoformat(value)' "$D -k refuses" 3.11+

echo "ran $RAN of $EXPECTED mutations, $SKIPPED not applicable on $PYVER${ONLY:+ (ONLY=$ONLY)}"
