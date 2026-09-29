# kag-sched

A tiny scheduler for Kaggle kernels that runs as a GitHub Actions job. Every 15 minutes it does **one round**:

1. checks the account's GPU quota and lists the account's kernels with a fixed slug prefix (default `st-`) that ran in the last 24 h;
2. queries the status of those kernels (429 or any other API error is treated as *unknown*: nothing is pushed in that round);
3. for a failed job that declared `resume`, creates one derived job whose kernel mounts the failed run's output;
4. pushes the next runnable job while `RUNNING + QUEUED` GPU kernels stay below the slot limit, the projected quota keeps a safety margin, and the job owner's hour budget is not exceeded;
5. downloads the outputs of jobs that reached `COMPLETE` / `ERROR` / `CANCEL_ACKNOWLEDGED` (text and small images only) — after the pushes, because a download can take minutes (three kernel outputs, 60 MB, took 13 min in this deployment) and a free GPU slot should not wait for it;
6. commits `orch/state.json` and the fetched outputs back to a **private** source repository.

The scheduler holds no project code. Kernels, the job list and the results live in the private repository that the workflow checks out with a token.

## How it keeps running

Two jobs in one workflow, and the cron never touches the chain:

- **Chain** (job `round`, concurrency group `sched`). *Run workflow* with `loopMinutes` (default 330) starts a chain run. `chain.sh` runs a round, writes back, sleeps 15 min and repeats until the next round would start after `loopMinutes`; the job is bounded by `timeout-minutes: 355` (GitHub's limit is 360). Before each round the private checkout is reset to the tip of the source branch, so plan edits made by people during the run are picked up. After every round the run makes sure that exactly one successor is queued: `gh run list -e workflow_dispatch` must show no other run that is queued, pending or in progress, then `gh workflow run sched.yml -f loopMinutes=<nextLoopMinutes>` (with the run's own `GITHUB_TOKEN`, hence `permissions: actions: write`; `workflow_dispatch` events created with `GITHUB_TOKEN` do start runs). The successor waits as *pending* in group `sched` until the current run ends, so a crash or timeout of the current run does not break the chain. If the run list cannot be read, the chain dispatches anyway (a duplicate pending run is replaced, a missing one would end the chain).
- **Watchdog** (job `watchdog`, group `watchdog`, on the `*/15` cron). It checks out only this repository, gets no secret and runs no round. `chain.sh watchdog` lists the `workflow_dispatch` runs; if none is queued, pending or in progress it starts a new chain with `loopMinutes` 330, otherwise it prints `chain alive` and ends within seconds. If the run list cannot be read it does nothing (a missed check costs 15 min, a second chain would double every push).
- **Write-back** (after every round). The round's changes (`orch/state.json`, fetched outputs, derived kernels) are committed and rebased onto the tip of the source branch with `-X theirs`: where a line conflicts, the scheduler's version wins (a kernel output that a person committed by hand into `<resultsDir>/raw/<slug>/` while the round was downloading the same files is replaced by the downloaded copy), everything else committed meanwhile survives. If the merged `orch/state.json` is not valid JSON, the round's version is taken whole. If the rebase fails anyway (a file the round changed was deleted or renamed meanwhile), the round is rebuilt as a **state-only commit**: `orch/state.json` (three-way merged the same way) and the kernel directories the round created are written back, the downloaded outputs are dropped and marked unfetched (`runs[].refetch`), and the next round downloads them again. Before this, a same-file conflict dropped the whole round including a `pushed` mark, and every later round downloaded the same outputs again and failed the same way.
- `nextLoopMinutes` lets a short verification run (`loopMinutes=35` = three rounds) hand over to a full-length chain (`nextLoopMinutes=330`). A dry run does not hand over.
- To stop: disable the workflow (*Actions → sched → ··· → Disable workflow*; this silences the cron), then cancel the pending run and the running one. Cancelling alone is not enough, the watchdog restarts the chain at the next tick. To change the interval or the code: push to `main`; the successor run picks it up (a `workflow_dispatch` run uses the workflow file of the commit it was created from), the current run keeps its checkout.

A second, unrelated workflow lives next to the chain: `.github/workflows/aio.yml` (job `pack`, `workflow_dispatch` only, own concurrency group `aio`, `permissions: contents: read`, the same three secrets, the same masking step and pinned checkout as `sched.yml`). One run checks out the private repository with full history, runs its `deliverables/aio/pack.sh` and publishes the all-in-one archive (a bundle, four zip volumes, `SHA256SUMS`) as a release on the private repository (`tag` input, default `allinone-<short sha>`); its first run's log contained only dataset codes and byte counts, and its working data is deleted in a final `always()` step. It touches neither the chain nor the watchdog: the watchdog counts `sched.yml` runs only, and the two workflows never share a concurrency group.

Why two jobs: with one job and a workflow-level `concurrency: sched`, the first cron run that fired while a successor was pending replaced it (a group holds one pending run; the newer wins), and the running chain run did not dispatch again because it saw a non-completed run; when it ended, nothing followed, and in the next 4 h 20 min the cron produced two single rounds. A job that is skipped by its `if` is never queued, so a cron run now cannot enter `sched`.

## Layout expected in the private repository

```
orch/plan.json        limits and owners (edited by people; may also carry a "jobs" list)
orch/plan/<X>.json    one job list per team member (a JSON array or {"jobs": [...]}); orch/jobs/*.json is read the same way
orch/state.json       scheduler memory (written by the scheduler)
orch/kernels/<slug>/  kernel-metadata.json + main.py (also where derived resume kernels are written)
<resultsDir>/raw/<slug>/   fetched outputs, one directory per kernel slug, plus _manifest.json (written by the scheduler only)
```

Who writes where: `<resultsDir>/raw/<slug>/` and the derived `orch/kernels/<id>-r1/` belong to the scheduler; people do not commit into them (a file that needs a hand edit is copied elsewhere first; a hand-committed copy inside `raw/` is replaced by the scheduler's copy at the next write-back that touches the same lines). `orch/state.json` is the scheduler's memory: a hand edit is possible, but only between two rounds — look at the time of the last `sched-bot` commit first; a round starts every 15 min and writes back at its end, up to 13 min later when outputs are downloaded — and where a hand edit and the round's change touch the same lines, the round wins with no notice beyond the log line `orch/state.json changed on origin during this round`. The plan files (`orch/plan.json`, `orch/plan/*.json`) are the people's; a `orch/plan/<X>.json` that is not valid JSON is skipped for the round with a log line, a `orch/plan.json` or `orch/state.json` that is not valid JSON stops the round (`sched.py exit 2`) until it is fixed by hand.

`kernelDir` may point anywhere inside the private repository; only `kernel-metadata.json` and the file named by its `code_file` are uploaded by the Kaggle client.

Job lists are merged in this order: `plan.json` `jobs`, then `orch/plan/*.json`, then `orch/jobs/*.json`, each directory in file-name order. The first definition of an id wins and later duplicates are reported in the log. One file per person avoids the lost-update problem of several people rewriting one shared file.

## plan.json

```json
{
  "prefix": "st-",
  "maxBusy": 2,
  "maxBusyCpu": 2,
  "minRemainH": 3,
  "activeWindowH": 24,
  "maxTextMB": 20,
  "maxImgMB": 5,
  "maxTotalMB": 20,
  "fetchSkip": ["frames/**", "*.mp4", "*.npy", "*.pt", "*.ckpt", "*.tar"],
  "forbidden": ["regex", "checked case-insensitively against every kernel file before push"],
  "owners": {
    "B": {"budgetH": 12, "maxConcurrent": 2, "priority": 0, "resultsDir": "results/r2"},
    "A": {"budgetH": 4, "maxConcurrent": 1, "priority": 1, "resultsDir": "results/r1"}
  },
  "jobs": [
    {"id": "st-example-01", "kernelDir": "orch/kernels/st-example-01", "owner": "A",
     "needH": 1.5, "timeoutSec": 7200, "deps": [], "priority": 0, "resume": false,
     "fetchSkip": ["out/**/*.png"]}
  ]
}
```

| field | meaning |
| --- | --- |
| `id` | kernel slug; must start with `prefix`; must equal the slug part of `kernel-metadata.json` `id` (`<owner>/<slug>`) |
| `kernelDir` | directory with `kernel-metadata.json` and the code file |
| `owner` | key of `owners`; supplies the hour budget, concurrency cap, priority and results directory |
| `needH` | expected GPU wall-clock hours; used for the budget, the quota projection and the running estimate |
| `timeoutSec` | passed to the Kaggle push as the kernel timeout |
| `deps` | job ids that must be `COMPLETE` before this job may be pushed |
| `priority` | lower runs first inside an owner; owners are ordered by their own `priority` |
| `resume` | `true`: after `ERROR` or `CANCEL_ACKNOWLEDGED`, create `<id>-r1` once, same kernel files, with the failed kernel added to `kernel_sources` so its output is mounted under `/kaggle/input/<id>/`; the kernel code decides what to do with it. Setting it back to `false` withdraws a derived job that has not been pushed yet |
| `adopt` | `true`: the scheduler never pushes this job; it only adopts the kernel once the slug exists on the account (budget, slots, fetch and resume then work as for a pushed job). For kernels that a person pushes by hand: commit the entry first, push second |
| `fetchSkip` | extra glob patterns for output files that stay on Kaggle (added to the global `fetchSkip`) |

Rules applied to every push: `enable_gpu` from the job's `kernel-metadata.json` decides whether a job is a GPU job (the Kaggle list endpoint does not report the accelerator, so kernels on the account that are not in the plan are counted as GPU sessions). GPU jobs need `gpuBusy < maxBusy`, `quota.remainH - reserved >= max(minRemainH, needH)` (`reserved` = the not-yet-elapsed part of `needH` of jobs still running) and `owner used + needH <= budgetH`. CPU jobs only need a free CPU slot. `used` counts finished jobs by kernel wall clock and running jobs by `max(elapsed, needH)`. If Kaggle refuses a GPU push with `Maximum batch GPU session count`, a session the list does not show is running (an older version of a kernel that was pushed twice); the round then treats the GPU slots as full (`runs[].hiddenGpu`) and the job is retried next round.

Wall clock of a finished kernel: the last `"time"` stamp of the downloaded kernel log when present (this is the kernel's own run time), otherwise push time to the first round that saw the terminal state (up to one cron interval too long). A `CANCEL_ACKNOWLEDGED` job with a timeout counts at least its timeout. The log is downloaded after the pushes of the same round, so the budget check of the round that sees a job end uses the push-to-observation value; from the next round on, the log value.

An entry that has not been pushed takes `owner`, `needH` and `timeoutSec` from the plan every round, so plan edits count immediately; a pushed job keeps the values it was pushed with.

A job whose slug already exists on the account is **adopted**, never pushed again. That makes the scheduler safe to run next to manual pushes and safe after losing `state.json`. The other direction needs discipline: a plan entry without `adopt` is pushed by the scheduler as soon as it is runnable, so a kernel that a person wants to push by hand must be committed with `"adopt": true` before the manual push, otherwise both push it.

## state.json

`jobs.<slug>` carries `owner`, `gpu`, `needH`, `pushedAt`, `status`, `statusAt`, `endedAt`, `wallH`, `fetchedAt`, `fetched`, `skipped`, `failureMessage`, `blocked` (pre-push check failure with reason; cleared when the kernel directory changes), `lastPushError`, `adopted`, `resumedBy` / `resumeOf`, `fetchTries` (a fetch is attempted three times at most). `runs` keeps the last 100 rounds with quota, busy counts and the actions taken; `refetch` in a round lists outputs that a state-only write-back dropped.

## Fetched outputs

Only `.json .md .txt .csv .log` (each up to `maxTextMB`) and `.png .jpg .jpeg .webp .gif .svg` (each up to `maxImgMB`) are downloaded, `maxTotalMB` in total per kernel. Files matching a `fetchSkip` glob are not downloaded at all: the global list in `plan.json` (default `frames/**`, `*.mp4`, `*.npy`, `*.pt`, `*.ckpt`, `*.tar`) plus the job's own list. A pattern is tested case-insensitively against the file path and every trailing sub-path (`frames/**` matches `out/frames/0001.jpg`; `*` and `**` both cross `/`; `out/**/*.png` matches `out/figs/a.png` but not `out/a.png`). Everything else is listed as skipped in `_manifest.json` with the reason (`type`, `size`, `total`, `fetchSkip`). The account name is replaced by `<u>` inside text outputs. A kernel is fetched once; changing `fetchSkip` later does not re-fetch it (a state-only write-back is the exception: it clears the fetch mark and the next round downloads the outputs again).

## Setup

1. Fork or copy this repository as a **public** repository (public repositories get unlimited Actions minutes; the cron trigger is only active on the default branch).
2. Edit `SRC_REPO` and `SRC_BRANCH` at the top of `.github/workflows/sched.yml`.
3. Add three repository secrets under *Settings → Secrets and variables → Actions*:
   - `KAGGLE_API_TOKEN` – Kaggle API token of the kernel owner;
   - `KAGGLE_USER` – the owner's Kaggle user name;
   - `SRC_REPO_PAT` – fine-grained personal access token restricted to the private source repository with *Contents: read and write*.
4. Put `orch/plan.json` (limits and owners; job lists may go into `orch/plan/<member>.json`) and an empty `{"jobs": {}, "runs": []}` as `orch/state.json` into the source repository.
5. Trigger the workflow once by hand (*Actions → sched → Run workflow*) with `loopMinutes` 0 and `dryRun` on to see the decisions without pushing or writing; then start the chain with `loopMinutes` 330 (the watchdog would start one at its next tick anyway).
6. Add the secrets before the first run: a run without them fails at the first step and GitHub mails the owner about every failed run.

Local use: `KAGGLE_API_TOKEN=… KAGGLE_USER=… SRC_DIR=/path/to/private/checkout python3 sched.py --dry-run`.

## If something stops

- *Actions* tab, event `workflow_dispatch`: one run in progress and one pending is the healthy picture. Nothing for more than 15 min means the watchdog did not run either; look at the `schedule` runs.
- No `schedule` runs: a freshly added or changed `schedule` usually starts 15–60 min late, GitHub drops scheduled runs at peak load (the start of every hour is the worst) and disables the cron after 60 days without a commit and in every fork by default (*Actions → sched* shows *This scheduled workflow is disabled*; re-enable with the button). Scheduled runs come only from the default branch. In this deployment the cron fired for the first time 4.5 h after the workflow was created and then at irregular intervals, which is why the chain does not depend on it. Start a chain by hand: *Run workflow*, `loopMinutes` 330.
- `watchdog` printed `run list unavailable, not dispatching`: a transient API error; the next tick tries again.
- A `round` run failed at *Require and mask secrets*: a secret is missing or the fine-grained token expired. Fix the secret; the watchdog restarts the chain at the next tick.
- `rebase failed even with -X theirs, writing back the state file only`: a file the round changed was deleted or renamed on the source branch during the round; the state-only commit keeps the bookkeeping and the round's outputs are downloaded again next round (`runs[].refetch`). `write-back failed, the next round redoes it`: the push was refused three times in a row (the branch kept moving) or the state-only commit was not possible; the next round repeats the round.
- `orch/state.json changed on origin during this round; where lines conflict the scheduler's version wins`: someone edited the state while a round was running; check that the edit survived, and edit the state only between two rounds.
- `sched.py exit 2` after `is not valid JSON`: a hand edit broke `orch/state.json` or `orch/plan.json`; every round stops there until the file is fixed. A broken `orch/plan/<X>.json` is only skipped (`skipped this round`).
- `scan hit: this round is not written back`: a fetched output or the state contains a token-like string or the user name. Nothing of that round was kept, so it repeats every round until the file is excluded with a `fetchSkip` pattern in the plan.
- A kernel has to be stopped (a double push left an older version running, a job hangs): the API cannot do it. `cancel_kernel_session` needs the session id, and Kaggle exposes that id only after the session has ended (in the `kf/<id>/` part of the output-file URLs); the output list of a running session is empty and the live log stream carries no id (tested with a CPU probe kernel). Stop the session on the Kaggle web page; the next round records `CANCEL_ACKNOWLEDGED`.
- Two chain runs alive at once (a hand-started run next to the chain): the newer pending run replaced the older pending one; nothing to do, the runs serialize in group `sched`.

## Limits

- GitHub runs scheduled workflows with a delay of a few minutes at busy times, sometimes not at all, and disables the cron on repositories without activity for 60 days. Only the watchdog depends on the cron.
- A chain run holds the three secrets in its environment for up to 355 min; the log masking is the same as for a single round. The watchdog job holds no secret.
- `maxBusy` is the account-level cap on concurrent batch GPU sessions; Kaggle refuses pushes above it, which the scheduler records as `lastPushError` and retries next round.
- The scheduler never cancels a kernel; the Kaggle API has no cancel call.
