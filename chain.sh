#!/usr/bin/env bash
# Runs scheduling rounds every 15 min inside one job (LOOP_MINUTES), writes results back after each round,
# and keeps exactly one successor run queued. `chain.sh dispatch` only does the hand-over.
# `chain.sh watchdog` (cron job, no secrets) starts a new chain when no workflow_dispatch run is queued, pending or in progress.
# Write-back: the round's commit is rebased onto origin with `-X theirs` (where lines conflict the scheduler's copy wins,
# e.g. when an owner committed the same kernel output by hand; everything else committed meanwhile survives). If the merged
# state file is not valid JSON the round's version is taken whole. If the rebase still fails (e.g. a file the round changed
# was deleted meanwhile), only the state file and the kernel dirs the round changed are written back (state-only commit) and
# the outputs fetched in that round are marked unfetched, so the next round fetches them again and nothing is lost for good.
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
validJson() { python3 -c 'import json, sys; json.load(open(sys.argv[1]))' "$1" 2>/dev/null; }
stateOnly() {
  # $1 = origin at round start, $2 = this round's commit. Rebuild the round on top of origin with the state file and the
  # kernel dirs the round changed only; the fetched outputs are dropped and marked unfetched (the try is not counted).
  orch=$(dirname "$statePath")
  keep=$(git diff --name-only "$1" "$2" -- "$orch/kernels" | sed -E "s#^($orch/kernels/[^/]+)/.*#\1#" | sort -u)
  git reset -q --hard "origin/$SRC_BRANCH"; git clean -qfd
  for p in $keep; do git rm -rq --ignore-unmatch -- "$p"; git checkout -q "$2" -- "$p" 2>/dev/null || true; done
  git show "$2:$statePath" > "$here/state.round.json" || return 1
  git show "$1:$statePath" > "$here/state.base.json" 2>/dev/null || : > "$here/state.base.json"
  cp "$here/state.round.json" "$here/state.merged.json"
  if [ -f "$statePath" ]; then
    git merge-file -q --ours "$here/state.merged.json" "$here/state.base.json" "$statePath" >/dev/null 2>&1 || true
  fi
  if validJson "$here/state.merged.json"; then
    cp "$here/state.merged.json" "$statePath"
  else
    echo "the merged $statePath is not valid JSON, taking this round's version whole"; cp "$here/state.round.json" "$statePath"
  fi
  rm -f "$here/state.round.json" "$here/state.base.json" "$here/state.merged.json"
  python3 - "$statePath" "$1" <<'EOF'
import json, subprocess, sys
p, base = sys.argv[1], sys.argv[2]
d = json.load(open(p))
try:
    old = json.loads(subprocess.run(['git', 'show', f'{base}:{p}'], capture_output=True, text=True, check=True).stdout).get('jobs', {})
except Exception as e:
    old = None
    print('the state at round start is unreadable, fetch marks left as they are:', e)
drop = []
for slug, st in (d.get('jobs', {}) if old is not None else {}).items():
    if st.get('fetchedAt') and st['fetchedAt'] != old.get(slug, {}).get('fetchedAt'):
        for k in ('fetchedAt', 'fetched', 'skipped', 'fetchedBytes'):
            st.pop(k, None)
        st['fetchTries'] = max(0, int(st.get('fetchTries', 1)) - 1)
        drop.append(slug)
if drop:
    if d.get('runs'):
        d['runs'][-1]['refetch'] = drop
    with open(p, 'w') as f:
        json.dump(d, f, indent=1, sort_keys=True)
        f.write('\n')
    print(len(drop), 'output(s) fetched this round are dropped with the conflict and will be fetched again:', ' '.join(drop))
EOF
  git add -A -- "$orch"
  if git diff --cached --quiet; then echo "state unchanged, nothing left to write back"; return 2; fi
  python3 "$here/scan.py" --staged || { echo "scan hit on the state file"; git reset -q --hard HEAD; git clean -qfd; return 1; }
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
      base=$(git rev-parse HEAD)
      git commit -q -m "sched: round $(date -u +%Y-%m-%dT%H:%MZ)"
      round=$(git rev-parse HEAD)
      ok=0
      for i in 1 2 3; do
        gitAuth fetch -q origin "$SRC_BRANCH" || true
        git diff --quiet "$base" "origin/$SRC_BRANCH" -- "$statePath" 2>/dev/null || echo "$statePath changed on origin during this round; where lines conflict the scheduler's version wins"
        if git -c advice.mergeConflict=false rebase -q -X theirs "origin/$SRC_BRANCH"; then
          if ! validJson "$statePath"; then
            echo "the merged $statePath is not valid JSON, taking this round's version whole"
            git checkout -q "$round" -- "$statePath" && git commit -q --amend --no-edit
          fi
        else
          git rebase --abort || true
          echo "rebase failed even with -X theirs, writing back the state file only"
          stateOnly "$base" "$round"; rc=$?
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
