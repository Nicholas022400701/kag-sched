# Security notes

This repository is public and runs unattended (a self-continuing `workflow_dispatch` chain plus a cron watchdog that only restarts the chain) with three secrets. Measures in place:

## Secrets and logs
- Secrets are read only from `secrets.*` and passed to steps as environment variables. They never appear in `run:` script text, so the step header cannot print them.
- The chain job's first step fails the job if any of `KAGGLE_API_TOKEN`, `KAGGLE_USER`, `SRC_REPO_PAT` is empty and registers each value with `::add-mask::`, together with the upper-cased user name and the base64 form of the token used for `git push`. GitHub additionally masks every `secrets.*` value on its own.
- `sched.py` wraps `stdout`/`stderr` in a masker that replaces the Kaggle user name and the token with `<u>` in everything it or the Kaggle client prints, and it never prints kernel URLs, dataset names, owner-qualified refs or error bodies longer than 160 characters.
- Text outputs downloaded from Kaggle are rewritten with the user name replaced before they are committed.

## Repository content
- The public repository contains only the scheduler, the scanner and this documentation. Job definitions, kernels and results live in the private source repository.
- `scan.py` runs twice per round: once over the public tree and once over the staged write-back. It fails the job on Kaggle token prefixes, GitHub token prefixes, private keys, AWS keys, and on the literal user name and token values taken from the environment, and prints only file, line and pattern name.

## Workflow hardening
- Triggers are `schedule` and `workflow_dispatch` only. No `pull_request`, `pull_request_target`, `issue_comment` or other event that could be driven by an outside contributor.
- `permissions: contents: read, actions: write` for the workflow's `GITHUB_TOKEN`: `actions: write` is needed only to start runs with `gh workflow run` (the chain's successor, the watchdog's restart); it cannot read secrets or write repository content. The private repository is reached only through the fine-grained token.
- The cron job (`watchdog`) checks out only this repository, receives no secret, runs no round and calls just `gh run list` and, when no chain run is alive, `gh workflow run`.
- All checkouts use `persist-credentials: false`; the push authenticates with a per-command `http.extraheader`, so no token is stored in `.git/config`.
- Third-party actions are pinned to full commit SHAs (`actions/checkout` v4.2.2). The Python client is pinned (`kaggle==2.2.4`).
- Job-level `concurrency` groups: `sched` for the chain job (a dispatched successor waits as *pending*; a cron run never enters this group, so it cannot replace the successor) and `watchdog` for the cron job. `timeout-minutes: 355` bounds each chain run and 5 the watchdog; `chain.sh` stops starting rounds when the next one would exceed `loopMinutes`.
- `chain.sh` prints round numbers, timestamps, git result words and `gh` messages only; the `git` authentication header is passed per command and never written to disk.
- No artifacts, caches or job summaries are produced; the private checkout is deleted in a final `always()` step.
- `aio.yml` (one-shot archive job, `workflow_dispatch` only, group `aio`, `contents: read`, `timeout-minutes: 150`) holds the same three secrets, uses the same masking step, pinned checkout, `persist-credentials: false` and pinned client, and deletes its working data in an `always()` step; the public-tree self scan of the chain (`scan.py --paths … .github`) covers it. Unlike the chain, which only reads JSON and kernel files from the private repository, it executes a script from there (`deliverables/aio/pack.sh`) with the secrets in its environment, so write access to the private repository is now equivalent to access to the three secrets; the release it publishes goes to the private repository through `SRC_REPO_PAT`.

## Recommended settings for the public repository
- *Settings → Actions → General*: require approval for all outside collaborators; allow only actions created by GitHub or pinned by SHA.
- Use a fine-grained token limited to the private source repository with *Contents: read and write* and an expiry of at most 90 days; rotate it before expiry and revoke it if a run log ever looks wrong.
- Keep Kaggle kernels created by this scheduler private.

## What the logs still reveal
Kernel slugs, their statuses, hour counts and the name of the private source repository from the workflow file. Choose slugs that do not identify people or projects.
