#!/bin/zsh
# Mutation battery for the worker's event-loop watchdog (#1664): each behaviour has one named mutation
# that must make its test FAIL ("killed"), and must PASS on the clean tree first, or the verdict is
# BASELINE RED; a selector that selects nothing is NO TEST SELECTED, never a kill. Files are restored
# from the COMMITTED tree after each, so commit first, and run it in its own worktree
# (`STORYDUMP_ROOT`). No database: these are unit tests. `STORYDUMP_PY` names the interpreter.
#
# Three mutants cost real time before they die: the thread that never reads its verdict and the exit
# that waits on its stream each run into a 30 s wait in their test, and the stop that never ends the
# thread into a 5 s join.
#
# NOT here: that `run()` arms the watchdog and disarms it. The worker gate pins that
# (`tests/scripts/test_w1_worker_gate.py`, the `app.watchdog` asserts), and it needs a PostgreSQL.
#
# `DRY=1` applies nothing and runs nothing: it only checks that every mutation still finds its line
# exactly once, which is the part that rots when the module is edited.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=${STORYDUMP_PY:-/Users/chris/Projects/storydump/.venv/bin/python}
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE -u TARGET_DATABASE_URL DB_PORT=65432 PYTHONDONTWRITEBYTECODE=1 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
mkdir -p /tmp/claude
RAN=0
EXPECTED=$(grep -cE '^check "' "$0")
verdict() {
  local name=$1 rc=$2
  local summary; summary=$(grep -E '^=+ .*(passed|failed|error|deselected|no tests ran)' /tmp/claude/mut.log | tail -1)
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name  [$summary]"
  elif [ $rc -eq 0 ]; then echo "SURVIVED (bad): $name  [$summary]"
  elif ! grep -qE '^=+ .*[0-9]+ failed' /tmp/claude/mut.log; then echo "KILLED BY ERROR (check): $name  [$summary]"
  else echo "killed: $name  [$summary]"; fi
}
mutate() {  # file old new; with DRY=1 only counts the matches
  OLD="$2" NEW="$3" $PY - "$1" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text(); old = os.environ["OLD"]; new = os.environ["NEW"]
if s.count(old) != 1:
    print(f"MUTATION NOT APPLIED ({s.count(old)} matches)"); sys.exit(3)
if os.environ.get("DRY"):
    sys.exit(0)
p.write_text(s.replace(old, new, 1))
PY
}
check() {  # name file old new test-selector
  local name=$1 file=$2 old=$3 new=$4 sel=$5
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  if [ -n "${DRY:-}" ]; then
    if mutate "$file" "$old" "$new"; then echo "would mutate: $name"; else echo "MUTATION NOT APPLIED: $name"; fi
    return
  fi
  eval "$UNIT $sel" > /tmp/claude/mut.log 2>&1; local base=$?
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name  [$(grep -E '^=+ .*(selected|no tests ran)' /tmp/claude/mut.log | tail -1)]"; return; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [$(grep -E '^=+ .*(passed|failed|error)' /tmp/claude/mut.log | tail -1)]"; return; fi
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$UNIT $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

W=src/services/target/loop_watchdog.py
T="tests/src/services/target/test_loop_watchdog.py"
TW="tests/src/test_worker.py -k 'watchdog or stopped_loop'"

# The verdict (a clock moved by hand)
check "the bound fires at the bound, not past it" $W '        if quiet > self._stall_seconds:' '        if quiet >= self._stall_seconds:' "$T"
check "lost time is never a verdict" $W '        if away > self._beat_seconds + self._stall_seconds:' '        if False:' "$T"
check "one late check is lost time" $W '        if away > self._beat_seconds + self._stall_seconds:' '        if away > self._beat_seconds:' "$T"
check "a beat does not stamp" $W '        self._last_beat = self._clock()
        self.beats += 1' '        self.beats += 1' "$T"
check "a beat as long as the bound is accepted" $W '        if not 0 < beat_seconds < stall_seconds:' '        if False:' "$T"

# The thread (a real loop)
check "the thread never reads its verdict" $W '            reason = self.check()' '            reason = None' "$T"
check "the thread fires at every check" $W '                self._on_stall(reason)
                return' '                self._on_stall(reason)' "$T"
check "stop leaves the beat armed" $W '            self._timer.cancel()' '            pass' "$T"
check "stop never ends the thread" $W '        self._stopping.set()' '        pass' "$T"
check "a thread that never started is kept" $W '        thread.start()
        self._thread = thread' '        self._thread = thread
        thread.start()' "$T"

# The exit
check "the exit code is a task death's" $W 'STALL_EXIT_CODE = 3' 'STALL_EXIT_CODE = 1' "$T"
check "the exit waits on its stream" $W '        reporter.join(grace_seconds)' '        reporter.join()' "$T"
check "the exit is skipped when the report cannot start" $W '    finally:
        os._exit(STALL_EXIT_CODE)' '    except Exception:
        raise
    else:
        os._exit(STALL_EXIT_CODE)' "$T"
check "no stacks in the report" $W '            faulthandler.dump_traceback(file=out, all_threads=True)' '            pass' "$T"

# The wiring a unit test can see
check "the status line drops the watchdog" src/worker.py '        line += f" watchdog[armed={watchdog.armed}]"' '        pass' "$TW"
check "the bound outgrows the monitor's threshold" src/services/target/work_loop.py '    loop_stall_seconds: float = 300.0' '    loop_stall_seconds: float = 600.0' "$TW"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}${DRY:+ (DRY: nothing applied, nothing run)}"
