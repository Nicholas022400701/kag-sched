# Security notes

This repository is public and runs on a cron schedule with three secrets. Measures in place:

## Secrets and logs
- Secrets are read only from `secrets.*` and passed to steps as environment variables. They never appear in `run:` script text, so the step header cannot print them.
- The first step fails the job if any of `KAGGLE_API_TOKEN`, `KAGGLE_USER`, `SRC_REPO_PAT` is empty and registers each value with `::add-mask::`, together with the upper-cased user name and the base64 form of the token used for `git push`. GitHub additionally masks every `secrets.*` value on its own.
- `sched.py` wraps `stdout`/`stderr` in a masker that replaces the Kaggle user name and the token with `<u>` in everything it or the Kaggle client prints, and it never prints kernel URLs, dataset names, owner-qualified refs or error bodies longer than 160 characters.
- Text outputs downloaded from Kaggle are rewritten with the user name replaced before they are committed.

## Repository content
- The public repository contains only the scheduler, the scanner and this documentation. Job definitions, kernels and results live in the private source repository.
- `scan.py` runs twice per round: once over the public tree and once over the staged write-back. It fails the job on Kaggle token prefixes, GitHub token prefixes, private keys, AWS keys, and on the literal user name and token values taken from the environment, and prints only file, line and pattern name.

## Workflow hardening
- Triggers are `schedule` and `workflow_dispatch` only. No `pull_request`, `pull_request_target`, `issue_comment` or other event that could be driven by an outside contributor.
- `permissions: contents: read` for the workflow's `GITHUB_TOKEN`; the private repository is reached only through the fine-grained token.
- Both checkouts use `persist-credentials: false`; the push authenticates with a per-command `http.extraheader`, so no token is stored in `.git/config`.
- Third-party actions are pinned to full commit SHAs (`actions/checkout` v4.2.2). The Python client is pinned (`kaggle==2.2.4`).
- `concurrency` prevents overlapping rounds, `timeout-minutes: 20` bounds each run.
- No artifacts, caches or job summaries are produced; the private checkout is deleted in a final `always()` step.

## Recommended settings for the public repository
- *Settings → Actions → General*: require approval for all outside collaborators; allow only actions created by GitHub or pinned by SHA.
- Use a fine-grained token limited to the private source repository with *Contents: read and write* and an expiry of at most 90 days; rotate it before expiry and revoke it if a run log ever looks wrong.
- Keep Kaggle kernels created by this scheduler private.

## What the logs still reveal
Kernel slugs, their statuses, hour counts and the name of the private source repository from the workflow file. Choose slugs that do not identify people or projects.
