# kag-sched

A tiny scheduler for Kaggle kernels that runs as a GitHub Actions job. Every 15 minutes it does **one round**:

1. checks the account's GPU quota and lists the account's kernels with a fixed slug prefix (default `st-`) that ran in the last 24 h;
2. queries the status of those kernels (429 or any other API error is treated as *unknown*: nothing is pushed in that round);
3. downloads the outputs of jobs that reached `COMPLETE` / `ERROR` / `CANCEL_ACKNOWLEDGED` (text and small images only);
4. for a failed job that declared `resume`, creates one derived job whose kernel mounts the failed run's output;
5. pushes the next runnable job while `RUNNING + QUEUED` GPU kernels stay below the slot limit, the projected quota keeps a safety margin, and the job owner's hour budget is not exceeded;
6. commits `orch/state.json` and the fetched outputs back to a **private** source repository.

The scheduler holds no project code. Kernels, the job list and the results live in the private repository that the workflow checks out with a token.

## How it keeps running

GitHub's `schedule` trigger is unreliable for a fresh repository (in this deployment it did not fire once in 2.5 h), so the rounds are driven by a **self-continuing chain** of `workflow_dispatch` runs; the cron stays as a fallback that only adds single rounds.

- *Run workflow* with `loopMinutes` (default 330) starts a chain run. `chain.sh` runs a round, writes back, sleeps 15 min and repeats until the next round would start after `loopMinutes`; the job is bounded by `timeout-minutes: 355` (GitHub's limit is 360).
- Before each round the private checkout is reset to the tip of the source branch, so plan edits made by people during the run are picked up.
- After every round the run makes sure that exactly one successor is queued: `gh run list` (with the run's own `GITHUB_TOKEN`, hence `permissions: actions: write`) must show no other `workflow_dispatch` run that is queued or in progress, then `gh workflow run sched.yml -f loopMinutes=<nextLoopMinutes>`. A cron run that is pending does not count: under `concurrency` the newest pending run replaces the older one, so a cron run that slipped in and cancelled the successor is itself replaced by a fresh successor within 15 min. `workflow_dispatch` events created with `GITHUB_TOKEN` do start runs (they are the documented exception to the no-recursion rule). The successor sits in *pending* under `concurrency: sched` until the current run ends, so a crash or timeout of the current run does not break the chain.
- `nextLoopMinutes` lets a short verification run (`loopMinutes=35` = three rounds) hand over to a full-length chain (`nextLoopMinutes=330`).
- Runs started by the cron do one round and never dispatch anything; a dry run does not hand over either.
- To stop the chain: cancel the pending run first, then the running one (a cancelled run does not dispatch a successor; a running one would). To change the interval or the code: push to `main`; the next chain run picks it up, the current one keeps its checkout.

## Layout expected in the private repository

```
orch/plan.json        limits and owners (edited by people; may also carry a "jobs" list)
orch/plan/<X>.json    one job list per team member (a JSON array or {"jobs": [...]}); orch/jobs/*.json is read the same way
orch/state.json       scheduler memory (written by the scheduler)
orch/kernels/<slug>/  kernel-metadata.json + main.py (also where derived resume kernels are written)
<resultsDir>/raw/<slug>/   fetched outputs, one directory per kernel slug, plus _manifest.json
```

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
| `resume` | `true`: after `ERROR` or `CANCEL_ACKNOWLEDGED`, create `<id>-r1` once, same kernel files, with the failed kernel added to `kernel_sources` so its output is mounted under `/kaggle/input/<id>/`; the kernel code decides what to do with it |
| `fetchSkip` | extra glob patterns for output files that stay on Kaggle (added to the global `fetchSkip`) |

Rules applied to every push: `enable_gpu` from the job's `kernel-metadata.json` decides whether a job is a GPU job (the Kaggle list endpoint does not report the accelerator, so kernels on the account that are not in the plan are counted as GPU sessions). GPU jobs need `gpuBusy < maxBusy`, `quota.remainH - reserved >= max(minRemainH, needH)` (`reserved` = the not-yet-elapsed part of `needH` of jobs still running) and `owner used + needH <= budgetH`. CPU jobs only need a free CPU slot. `used` counts finished jobs by kernel wall clock and running jobs by `max(elapsed, needH)`.

Wall clock of a finished kernel: the last `"time"` stamp of the downloaded kernel log when present (this is the kernel's own run time), otherwise push time to the first round that saw the terminal state (up to one cron interval too long). A `CANCEL_ACKNOWLEDGED` job with a timeout counts at least its timeout.

A job whose slug already exists on the account is **adopted**, never pushed again. That makes the scheduler safe to run next to manual pushes and safe after losing `state.json`.

## state.json

`jobs.<slug>` carries `owner`, `gpu`, `needH`, `pushedAt`, `status`, `statusAt`, `endedAt`, `wallH`, `fetchedAt`, `fetched`, `skipped`, `failureMessage`, `blocked` (pre-push check failure with reason; cleared when the kernel directory changes), `lastPushError`, `adopted`, `resumedBy` / `resumeOf`. `runs` keeps the last 100 rounds with quota, busy counts and the actions taken.

## Fetched outputs

Only `.json .md .txt .csv .log` (each up to `maxTextMB`) and `.png .jpg .jpeg .webp .gif .svg` (each up to `maxImgMB`) are downloaded, `maxTotalMB` in total per kernel. Files matching a `fetchSkip` glob are not downloaded at all: the global list in `plan.json` (default `frames/**`, `*.mp4`, `*.npy`, `*.pt`, `*.ckpt`, `*.tar`) plus the job's own list. A pattern is tested case-insensitively against the file path and every trailing sub-path (`frames/**` matches `out/frames/0001.jpg`; `*` and `**` both cross `/`; `out/**/*.png` matches `out/figs/a.png` but not `out/a.png`). Everything else is listed as skipped in `_manifest.json` with the reason (`type`, `size`, `total`, `fetchSkip`). The account name is replaced by `<u>` inside text outputs. A kernel is fetched once; changing `fetchSkip` later does not re-fetch it.

## Setup

1. Fork or copy this repository as a **public** repository (public repositories get unlimited Actions minutes; the cron trigger is only active on the default branch).
2. Edit `SRC_REPO` and `SRC_BRANCH` at the top of `.github/workflows/sched.yml`.
3. Add three repository secrets under *Settings → Secrets and variables → Actions*:
   - `KAGGLE_API_TOKEN` – Kaggle API token of the kernel owner;
   - `KAGGLE_USER` – the owner's Kaggle user name;
   - `SRC_REPO_PAT` – fine-grained personal access token restricted to the private source repository with *Contents: read and write*.
4. Put `orch/plan.json` (limits and owners; job lists may go into `orch/plan/<member>.json`) and an empty `{"jobs": {}, "runs": []}` as `orch/state.json` into the source repository.
5. Trigger the workflow once by hand (*Actions → sched → Run workflow*) with `loopMinutes` 0 and `dryRun` on to see the decisions without pushing or writing; then start the chain with `loopMinutes` 330.
6. Add the secrets before the first run: a run without them fails at the first step and GitHub mails the owner about every failed run.

Local use: `KAGGLE_API_TOKEN=… KAGGLE_USER=… SRC_DIR=/path/to/private/checkout python3 sched.py --dry-run`.

## If the cron does not fire

The chain above does not depend on the cron. If you still want scheduled single rounds: a freshly added or changed `schedule` usually starts 15–60 min late, GitHub drops scheduled runs at peak load (the start of every hour is the worst), and here it never fired in the first 2.5 h. Check in this order; each step is one look, no code change:

1. *Actions* tab → filter by event `schedule`: is there any run at all? Scheduled runs only appear from the **default branch** and only for the workflow file on that branch (`git ls-remote origin HEAD` must point at the branch that holds `.github/workflows/sched.yml`).
2. *Actions* tab → *sched* → the workflow must not show *This scheduled workflow is disabled* (GitHub disables schedules after 60 days without a commit, and in every fork by default). Re-enable with the button, or push any commit to the default branch.
3. *Settings → Actions → General*: *Allow all actions and reusable workflows* (or at least GitHub-owned ones) and *Workflow permissions* not blocked at the account or organization level.
4. `GET /repos/<owner>/<repo>/actions/workflows` → `state` must be `active`; `disabled_manually` / `disabled_inactivity` explain themselves.
5. The workflow parses: the cron string is quoted (`- cron: '*/15 * * * *'`), five fields, UTC. A YAML error shows up as a failed `workflow_dispatch` run or as a red *sched* entry in the Actions tab.
6. If everything above is fine and nothing ran for two full intervals, edit and push the workflow file (a whitespace change is enough); GitHub re-registers the schedule on every change of the file.
7. Hand-run with *Run workflow* and `loopMinutes` 0 to check the pipeline itself; for continuous operation use the chain (`loopMinutes` 330) instead of repeated hand runs.

## Limits

- GitHub runs scheduled workflows with a delay of a few minutes at busy times, sometimes not at all, and disables the cron on repositories without activity for 60 days.
- A chain run holds the three secrets in its environment for up to 355 min; the log masking is the same as for a single round.
- `maxBusy` is the account-level cap on concurrent batch GPU sessions; Kaggle refuses pushes above it, which the scheduler records as `lastPushError` and retries next round.
- The scheduler never cancels a kernel; the Kaggle API has no cancel call.
