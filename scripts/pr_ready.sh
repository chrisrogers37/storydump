#!/usr/bin/env bash
# Is a PR ACTUALLY ready to merge? — the ways a green rollup lies.
#
# `gh pr checks` and the rollup answer "did any check fail". That is blind to
# the case that bites: a check that was NEVER SCHEDULED. A CONFLICTING PR has no
# computable merge commit, so `pull_request` workflows never run while App
# integrations keep reporting SUCCESS against the head SHA — the rollup reads
# ALL-GREEN with the whole suite missing. Measured 2026-08-31: four open PRs in
# that state, all four all-green, every one would have passed a by-name rung.
#
# ── THE ONE RULE, and it applies to every external call below ───────────────
#
# A FAILURE TO OBTAIN A VALUE MUST NEVER RENDER AS A PASSING VALUE.
#
# This is the defect the tool exists to catch, and the first version contained
# it twice (found in review, both reproduced live): the staleness check
# note-and-continued when `gh api` failed, and the drift gate silently skipped
# when `ci.yml` was unreadable. `gh api` writes its error body to STDOUT, so a
# failed call yields a plausible-looking string rather than nothing — which is
# why "did it look numeric" is checked explicitly and a miss REFUSES.
# Every fetch here therefore ends in `exit 3` (CANNOT LOOK), never a fallthrough.
#
# Usage:
#   scripts/pr_ready.sh <pr-number> [check-name ...]
#   scripts/pr_ready.sh --from-json F --behind N [check-name ...]   # offline
# Exit: 0 ready · 1 not ready · 2 usage · 3 cannot look
set -uo pipefail

REQUIRED_DEFAULT=(
  "Lint" "FC-2 Telegram ratchet" "Test" "Security Scan" "Front End" "Changelog Check"
)

die_lookup() { echo "CANNOT LOOK: $1" >&2; exit 3; }
usage() { echo "usage: $0 <pr-number> [check-name ...]" >&2
          echo "       $0 --from-json <file> --behind <n> [check-name ...]" >&2; exit 2; }

PR=""; FROM_JSON=""; BEHIND_OVERRIDE=""
WF="${PR_READY_WORKFLOW:-$(dirname "$0")/../.github/workflows/ci.yml}"
while [ $# -gt 0 ]; do
  case "$1" in
    --from-json) FROM_JSON="${2:-}"; shift 2 ;;
    --behind)    BEHIND_OVERRIDE="${2:-}"; shift 2 ;;
    --workflow)  WF="${2:-}"; shift 2 ;;
    -*)          usage ;;
    *)           if [ -z "$PR" ] && [ -z "$FROM_JSON" ]; then PR="$1"; shift
                 else break; fi ;;
  esac
done
if [ -n "$FROM_JSON" ]; then
  [ -n "$BEHIND_OVERRIDE" ] || usage
else
  [ -n "$PR" ] || usage
  case "$PR" in ''|*[!0-9]*) usage ;; esac
fi
EXPLICIT=0
if [ $# -gt 0 ]; then REQUIRED=("$@"); EXPLICIT=1; else REQUIRED=("${REQUIRED_DEFAULT[@]}"); fi

# ── Drift gate. REFUSES when it cannot read ci.yml (bug 2). ─────────────────
# A hardcoded list that silently misses a newly added CI job re-creates the
# absence-blindness this script exists to close, so the list is pinned. An
# explicit override is the caller deliberately asking a narrower question and
# is not pinned.
if [ "$EXPLICIT" = 0 ]; then
  [ -r "$WF" ] || die_lookup "cannot read $WF — the drift gate cannot run, and skipping it is how the required list goes stale unnoticed"
  declared=$(sed -n 's/^    name: \(.*\)$/\1/p' "$WF" | sort)
  [ -n "$declared" ] || die_lookup "parsed zero job names out of $WF — the drift gate cannot run"
  listed=$(printf '%s\n' "${REQUIRED_DEFAULT[@]}" | sort)
  if [ "$declared" != "$listed" ]; then
    echo "REFUSING: REQUIRED_DEFAULT has drifted from $WF" >&2
    echo "  only in ci.yml: $(comm -23 <(echo "$declared") <(echo "$listed") | paste -sd', ' -)" >&2
    echo "  only in script: $(comm -13 <(echo "$declared") <(echo "$listed") | paste -sd', ' -)" >&2
    exit 3
  fi
fi

# ── Fetch. Every failure is exit 3, never a fallthrough. ────────────────────
if [ -n "$FROM_JSON" ]; then
  [ -r "$FROM_JSON" ] || die_lookup "cannot read $FROM_JSON"
  J=$(cat "$FROM_JSON") || die_lookup "cannot read $FROM_JSON"
else
  J=$(gh pr view "$PR" --json mergeable,headRefOid,headRefName,baseRefName,statusCheckRollup 2>/dev/null) \
    || die_lookup "gh could not read PR #$PR"
fi
jq -e . >/dev/null 2>&1 <<<"$J" || die_lookup "PR payload was not JSON"

MERGEABLE=$(jq -r '.mergeable // "UNKNOWN"' <<<"$J")
if [ "$MERGEABLE" != MERGEABLE ]; then
  echo "NOT READY: mergeable=$MERGEABLE — pull_request workflows are not scheduled in this"
  echo "           state, so any green below was computed against a base that no longer applies."
  exit 1
fi

# Staleness. A branch behind its base is MERGEABLE with green checks, for a
# merge result that no longer exists — the shape that survives a by-name rung.
# Caught only INCIDENTALLY otherwise: PR #729 (198 behind) was flagged solely
# because `Front End` post-dates its last run.
if [ -n "$BEHIND_OVERRIDE" ]; then
  BEHIND="$BEHIND_OVERRIDE"
else
  HEADREF=$(jq -r '.headRefName // ""' <<<"$J")
  BASEREF=$(jq -r '.baseRefName // "main"' <<<"$J")
  [ -n "$HEADREF" ] || die_lookup "PR payload carried no headRefName"
  # NOT piped: a pipeline's $? is the last stage's, and `gh api` writes its
  # error body to STDOUT, so a 404 arrives looking like a payload.
  BEHIND=$(gh api "repos/{owner}/{repo}/compare/${BASEREF}...${HEADREF}" --jq '.behind_by' 2>/dev/null)
  rc=$?
  [ "$rc" = 0 ] || die_lookup "compare ${BASEREF}...${HEADREF} failed (rc=$rc) — staleness is UNKNOWN, which is not the same as up to date"
fi
case "${BEHIND:-}" in
  ''|*[!0-9]*) die_lookup "compare returned a non-numeric behind_by (${BEHIND:0:60}) — staleness is UNKNOWN, which is not the same as up to date" ;;
  0) : ;;
  *) echo "NOT READY: ${BEHIND} commit(s) behind ${BASEREF:-base} — the green below was computed"
     echo "           for a merge result that no longer applies. Rebase, then let CI re-run."
     exit 1 ;;
esac

FAIL=0
for name in "${REQUIRED[@]}"; do
  # TWO NODE TYPES: CheckRun carries .status/.conclusion, StatusContext .state.
  # `gh` renders a pending conclusion as "" and NOT null, so `//` never falls
  # through and a RUNNING check reads as ABSENT — two states with opposite
  # responses (wait vs investigate).
  v=$(jq -r --arg n "$name" '
        .statusCheckRollup[]? | select((.name // .context) == $n) |
        if .__typename == "CheckRun" then
          (if (.status // "") != "COMPLETED" then "RUNNING(\(.status // "?"))"
           elif (.conclusion // "") == "" then "NO-CONCLUSION"
           else .conclusion end)
        else (if (.state // "") == "" then "NO-STATE" else .state end) end' <<<"$J")
  # ABSENT is the dangerous one: "nothing failed" is TRUE of a check that never
  # ran, so absence must never read as success.
  if [ -z "$v" ]; then
    echo "NOT READY: required check '$name' is ABSENT from the rollup"; FAIL=1
  elif [ "$v" != SUCCESS ]; then
    echo "NOT READY: '$name' = $v"; FAIL=1
  fi
done
[ "$FAIL" = 0 ] || exit 1

echo "READY: mergeable, and all ${#REQUIRED[@]} required checks present and green at $(jq -r '.headRefOid // "?"' <<<"$J" | cut -c1-7)"
