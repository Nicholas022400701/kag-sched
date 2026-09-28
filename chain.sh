#!/usr/bin/env bash
# Runs scheduling rounds every 15 min inside one job (LOOP_MINUTES), writes results back after each round,
# and keeps exactly one successor run queued. `chain.sh dispatch` only does the hand-over.
# `chain.sh watchdog` (cron job, no secrets) starts a new chain when no workflow_dispatch run is queued, pending or in progress.
# Write-back: the round's commit is rebased onto origin with `-X theirs` (the scheduler's fetched copy wins a same-file conflict,
# e.g. when an owner committed the same kernel output by hand); if the rebase still fails, only orch/state.json is written back
# so a round's bookkeeping is never lost.
# Env: SRC_DIR, SRC_BRANCH, SRC_REPO_PAT, DRY_RUN, LOOP_MINUTES, NEXT_LOOP_MINUTES, GH_TOKEN, GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_WORKSPACE.
set -u
here="${GITHUB_WORKSPACE:-$(cd "$(dirname "$0")" && pwd)}"
statePath="${STATE_PATH:-orch/state.json}"
num() { case "${1:-}" in ''|*[!0-9]*) echo 0 ;; *) echo "$1" ;; esac; }
loopMin=$(num "${LOOP_MINUTES:-0}")
nextMin=$(num "${NEXT_LOOP_MINUTES:-$loopMin}")
sleepSec="${SLEEP_SEC:-900}"
loopSec="${LOOP_SEC:-$((loopMin * 60))}"
gitAuth() { git -c "http.https://github.com/.extraheader=AUTHORIZATION: basic $auth" "$@"; }
liveRuns() {
  gh run list -R "$GITHUB_REPOSITORY" -w sched.yml -e workflow_dispatch -L 30 --json databaseId,status,event \
    -q "[.[] | select(.status != \"completed\" and .event == \"workflow_dispatch\" and (.databaseId|tostring) != \"$GITHUB_RUN_ID\")] | length" 2>/dev/null || echo "?"
}
startRun() {
  if gh workflow run sched.yml -R "$GITHUB_REPOSITORY" -f loopMinutes="$1" -f dryRun=false; then
    echo "dispatched the next run (loopMinutes $1)"
  else
    echo "dispatch failed"
  fi
}
dispatch() {
  [ "$loopMin" -gt 0 ] || return 0
  if [ "${DRY_RUN:-false}" = "true" ]; then echo "dry run: no hand-over"; return 0; fi
  others=$(liveRuns)
  if [ "$others" != "0" ] && [ "$others" != "?" ]; then echo "next run already queued or running ($others), not dispatching"; return 0; fi
  [ "$others" = "?" ] && echo "run list unavailable, dispatching anyway"
  startRun "$nextMin"
}
watchdog() {
  others=$(liveRuns)
  case "$others" in
    '?') echo "run list unavailable, not dispatching" ;;
    0) echo "no chain run alive"; startRun "${WATCHDOG_LOOP_MINUTES:-330}" ;;
    *) echo "chain alive ($others run(s) queued or in progress)" ;;
  esac
}
stateOnly() {
  # keep this round's state file, drop everything else, re-commit on top of origin
  cp "$statePath" "$here/state.keep.json" || return 1
  git reset -q --hard "origin/$SRC_BRANCH"; git clean -qfd
  cp "$here/state.keep.json" "$statePath"
  git add "$statePath"
  if git diff --cached --quiet; then echo "state unchanged, nothing left to write back"; return 2; fi
  python3 "$here/scan.py" --staged || { echo "scan hit on the state file"; git reset -q --hard HEAD; return 1; }
  git commit -q -m "sched: round $(date -u +%Y-%m-%dT%H:%MZ) (state only)"
}
case "${1:-}" in
  dispatch) dispatch; exit 0 ;;
  watchdog) watchdog; exit 0 ;;
esac
auth=$(printf 'x-access-token:%s' "$SRC_REPO_PAT" | base64 -w0)
start=$(date +%s)
cd "$SRC_DIR"
git config user.name sched-bot
git config user.email sched-bot@users.noreply.github.com
n=0
while :; do
  n=$((n + 1))
  echo "round $n at $(date -u +%H:%M:%SZ), loop $loopMin min"
  if [ "$n" -gt 1 ]; then
    if gitAuth fetch -q origin "$SRC_BRANCH"; then git reset -q --hard "origin/$SRC_BRANCH"; else echo "fetch failed, using the previous checkout"; fi
    git clean -qfd
  fi
  if [ "${DRY_RUN:-false}" = "true" ]; then
    (cd "$here" && python3 sched.py --dry-run) || echo "sched.py exit $?"
  else
    (cd "$here" && python3 sched.py) || echo "sched.py exit $?"
    git add -A .
    if git diff --cached --quiet; then
      echo "nothing to write back"
    elif ! python3 "$here/scan.py" --staged; then
      echo "scan hit: this round is not written back"
      git reset -q --hard HEAD; git clean -qfd
    else
      git commit -q -m "sched: round $(date -u +%Y-%m-%dT%H:%MZ)"
      ok=0
      for i in 1 2 3; do
        gitAuth fetch -q origin "$SRC_BRANCH" || true
        if ! git rebase -q -X theirs "origin/$SRC_BRANCH"; then
          git rebase --abort || true
          echo "rebase failed even with -X theirs, writing back the state file only"
          stateOnly; rc=$?
          if [ "$rc" = 2 ]; then ok=1; break; fi
          [ "$rc" = 0 ] || { echo "state-only write-back not possible"; break; }
        fi
        if gitAuth push -q origin "HEAD:$SRC_BRANCH"; then ok=1; echo "written back"; break; fi
        sleep 10
      done
      [ "$ok" = 1 ] || echo "write-back failed, the next round redoes it"
    fi
  fi
  dispatch
  elapsed=$(( $(date +%s) - start ))
  if [ "$loopMin" -le 0 ] || [ $((elapsed + sleepSec)) -ge "$loopSec" ]; then echo "done after $n round(s), $((elapsed / 60)) min"; break; fi
  sleep "$sleepSec"
done
