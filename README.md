# kag-sched

A tiny cron scheduler for Kaggle kernels that runs as a GitHub Actions job. Every 15 minutes it does **one round**:

1. checks the account's GPU quota and lists the account's kernels with a fixed slug prefix (default `st-`) that ran in the last 24 h;
2. queries the status of those kernels (429 or any other API error is treated as *unknown*: nothing is pushed in that round);
3. downloads the outputs of jobs that reached `COMPLETE` / `ERROR` / `CANCEL_ACKNOWLEDGED` (text and small images only);
4. for a failed job that declared `resume`, creates one derived job whose kernel mounts the failed run's output;
5. pushes the next runnable job while `RUNNING + QUEUED` GPU kernels stay below the slot limit, the projected quota keeps a safety margin, and the job owner's hour budget is not exceeded;
6. commits `orch/state.json` and the fetched outputs back to a **private** source repository.

The scheduler holds no project code. Kernels, the job list and the results live in the private repository that the workflow checks out with a token.

## Layout expected in the private repository

```
orch/plan.json        job list and limits (edited by people)
orch/state.json       scheduler memory (written by the scheduler)
orch/kernels/<slug>/  kernel-metadata.json + main.py (also where derived resume kernels are written)
<resultsDir>/raw/<slug>/   fetched outputs, one directory per kernel slug, plus _manifest.json
```

`kernelDir` may point anywhere inside the private repository; only `kernel-metadata.json` and the file named by its `code_file` are uploaded by the Kaggle client.

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
  "maxTotalMB": 50,
  "forbidden": ["regex", "checked case-insensitively against every kernel file before push"],
  "owners": {
    "B": {"budgetH": 12, "maxConcurrent": 2, "priority": 0, "resultsDir": "results/r2"},
    "A": {"budgetH": 4, "maxConcurrent": 1, "priority": 1, "resultsDir": "results/r1"}
  },
  "jobs": [
    {"id": "st-example-01", "kernelDir": "orch/kernels/st-example-01", "owner": "A",
     "needH": 1.5, "timeoutSec": 7200, "deps": [], "priority": 0, "resume": false}
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

Rules applied to every push: `enable_gpu` from the job's `kernel-metadata.json` decides whether a job is a GPU job (the Kaggle list endpoint does not report the accelerator, so kernels on the account that are not in the plan are counted as GPU sessions). GPU jobs need `gpuBusy < maxBusy`, `quota.remainH - reserved >= max(minRemainH, needH)` (`reserved` = the not-yet-elapsed part of `needH` of jobs still running) and `owner used + needH <= budgetH`. CPU jobs only need a free CPU slot. `used` counts finished jobs by kernel wall clock and running jobs by `max(elapsed, needH)`.

Wall clock of a finished kernel: the last `"time"` stamp of the downloaded kernel log when present (this is the kernel's own run time), otherwise push time to the first round that saw the terminal state (up to one cron interval too long). A `CANCEL_ACKNOWLEDGED` job with a timeout counts at least its timeout.

A job whose slug already exists on the account is **adopted**, never pushed again. That makes the scheduler safe to run next to manual pushes and safe after losing `state.json`.

## state.json

`jobs.<slug>` carries `owner`, `gpu`, `needH`, `pushedAt`, `status`, `statusAt`, `endedAt`, `wallH`, `fetchedAt`, `fetched`, `skipped`, `failureMessage`, `blocked` (pre-push check failure with reason; cleared when the kernel directory changes), `lastPushError`, `adopted`, `resumedBy` / `resumeOf`. `runs` keeps the last 100 rounds with quota, busy counts and the actions taken.

## Fetched outputs

Only `.json .md .txt .csv .log` (each up to `maxTextMB`) and `.png .jpg .jpeg .webp .gif .svg` (each up to `maxImgMB`) are downloaded, `maxTotalMB` in total per kernel. Everything else is listed as skipped in `_manifest.json`. The account name is replaced by `<u>` inside text outputs.

## Setup

1. Fork or copy this repository as a **public** repository (public repositories get unlimited Actions minutes; the cron trigger is only active on the default branch).
2. Edit `SRC_REPO` and `SRC_BRANCH` at the top of `.github/workflows/sched.yml`.
3. Add three repository secrets under *Settings → Secrets and variables → Actions*:
   - `KAGGLE_API_TOKEN` – Kaggle API token of the kernel owner;
   - `KAGGLE_USER` – the owner's Kaggle user name;
   - `SRC_REPO_PAT` – fine-grained personal access token restricted to the private source repository with *Contents: read and write*.
4. Put `orch/plan.json` (and an empty `{"jobs": {}, "runs": []}` as `orch/state.json`) into the source repository.
5. Trigger the workflow once by hand (*Actions → sched → Run workflow*). `dryRun` shows the decisions without pushing or writing.
6. Add the secrets before the first scheduled tick: a scheduled run without them fails at the first step and GitHub mails the owner about every failed run.

Local use: `KAGGLE_API_TOKEN=… KAGGLE_USER=… SRC_DIR=/path/to/private/checkout python3 sched.py --dry-run`.

## Limits

- GitHub runs scheduled workflows with a delay of a few minutes at busy times and disables the cron on repositories without activity for 60 days.
- `maxBusy` is the account-level cap on concurrent batch GPU sessions; Kaggle refuses pushes above it, which the scheduler records as `lastPushError` and retries next round.
- The scheduler never cancels a kernel; the Kaggle API has no cancel call.
