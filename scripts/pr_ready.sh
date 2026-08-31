#!/usr/bin/env bash
# Is a PR ACTUALLY ready to merge? — the ways a green rollup lies.
#
# `gh pr checks` and the rollup answer "did any check fail". That question is
# blind to the case that actually bites: a check that was NEVER SCHEDULED.
# A CONFLICTING PR has no computable merge commit, so `pull_request` workflows
# never run at all, while App integrations (Vercel, GitGuardian) keep reporting
# SUCCESS against the head SHA. The rollup then reads ALL-GREEN with the entire
# test suite missing. Measured 2026-08-31: four open PRs in that state, all four
# reading all-green, every one of them would have passed a by-name check.
#
# Usage:  scripts/pr_ready.sh <pr-number> [check-name ...]
# Exit:   0 ready · 1 not ready · 2 usage · 3 cannot look
set -uo pipefail

# The CI jobs that must be present AND green. Kept explicit so a reader can see
# what is required, and PINNED against ci.yml below so adding a job to CI
# without adding it here fails loudly instead of silently widening the gap this
# script exists to close.
REQUIRED_DEFAULT=(
  "Lint" "FC-2 Telegram ratchet" "Test" "Security Scan" "Front End" "Changelog Check"
)

usage() { echo "usage: $0 <pr-number> [check-name ...]" >&2; exit 2; }
[ $# -ge 1 ] || usage
case "$1" in ''|*[!0-9]*) usage ;; esac
PR="$1"; shift
if [ $# -gt 0 ]; then REQUIRED=("$@"); else REQUIRED=("${REQUIRED_DEFAULT[@]}"); fi

# Drift gate. Only when using the defaults: an explicit override is the caller
# deliberately asking a narrower question.
WF="$(dirname "$0")/../.github/workflows/ci.yml"
if [ $# -eq 0 ] && [ -r "$WF" ]; then
  declared=$(sed -n 's/^    name: \(.*\)$/\1/p' "$WF" | sort)
  listed=$(printf '%s\n' "${REQUIRED_DEFAULT[@]}" | sort)
  if [ "$declared" != "$listed" ]; then
    echo "REFUSING: REQUIRED_DEFAULT has drifted from ci.yml." >&2
    echo "  only in ci.yml:  $(comm -23 <(echo "$declared") <(echo "$listed") | paste -sd', ' -)" >&2
    echo "  only in script:  $(comm -13 <(echo "$declared") <(echo "$listed") | paste -sd', ' -)" >&2
    exit 3
  fi
fi

J=$(gh pr view "$PR" --json mergeable,headRefOid,headRefName,baseRefName,statusCheckRollup 2>/dev/null) || {
  echo "CANNOT LOOK: gh could not read PR #$PR" >&2; exit 3; }

# (1) Not mergeable => workflows are not scheduled. Checked FIRST, because in
#     this state the check list is meaningless rather than merely incomplete.
MERGEABLE=$(jq -r '.mergeable // "UNKNOWN"' <<<"$J")
if [ "$MERGEABLE" != MERGEABLE ]; then
  echo "NOT READY: mergeable=$MERGEABLE — pull_request workflows are not scheduled in this state,"
  echo "           so any green below was computed against a base that no longer applies."
  exit 1
fi

# (1b) STALE-BUT-NOT-CONFLICTING. A branch behind its base is MERGEABLE and its
#      checks are green -- for a merge result that no longer exists. This is the
#      shape that survives a by-name rung, because nothing is missing and nothing
#      is red. It is only caught incidentally when the base has since ADDED a CI
#      job (measured: PR #729, 198 commits behind, was flagged solely because
#      `Front End` post-dates its last run). Take away that coincidence and a
#      stale PR reads fully green, so staleness is tested directly here.
HEADREF=$(jq -r '.headRefName // ""' <<<"$J")
BASEREF=$(jq -r '.baseRefName // "main"' <<<"$J")
if [ -n "$HEADREF" ]; then
  BEHIND=$(gh api "repos/{owner}/{repo}/compare/${BASEREF}...${HEADREF}" --jq '.behind_by' 2>/dev/null)
  case "${BEHIND:-}" in
    ''|*[!0-9]*) echo "NOTE: could not determine staleness against ${BASEREF} — treating as UNKNOWN, not as up-to-date" >&2 ;;
    0) : ;;
    *) echo "NOT READY: ${BEHIND} commit(s) behind ${BASEREF} — the green below was computed"
       echo "           for a merge result that no longer applies. Rebase, then let CI re-run."
       exit 1 ;;
  esac
fi

FAIL=0
for name in "${REQUIRED[@]}"; do
  # (2) TWO NODE TYPES: CheckRun carries .status/.conclusion, StatusContext
  #     carries .state. Reading one silently drops the other.
  # (3) `gh` renders a pending conclusion as "" and NOT null, so `//` never
  #     falls through — a RUNNING check reads as absent unless .status is
  #     tested explicitly. RUNNING and ABSENT need opposite responses (wait vs
  #     investigate); this script reported one as the other until it was run
  #     against a PR whose state was already known.
  v=$(jq -r --arg n "$name" '
        .statusCheckRollup[] | select((.name // .context) == $n) |
        if .__typename == "CheckRun" then
          (if (.status // "") != "COMPLETED" then "RUNNING(\(.status // "?"))"
           elif (.conclusion // "") == "" then "NO-CONCLUSION"
           else .conclusion end)
        else (if (.state // "") == "" then "NO-STATE" else .state end) end' <<<"$J")
  # (4) ABSENT is the dangerous one — "nothing failed" is TRUE of a check that
  #     never ran, so absence must never be allowed to read as success.
  if [ -z "$v" ]; then
    echo "NOT READY: required check '$name' is ABSENT from the rollup"; FAIL=1
  elif [ "$v" != SUCCESS ]; then
    echo "NOT READY: '$name' = $v"; FAIL=1
  fi
done
[ "$FAIL" = 0 ] || exit 1

echo "READY: mergeable, and all ${#REQUIRED[@]} required checks present and green at $(jq -r .headRefOid <<<"$J" | cut -c1-7)"
