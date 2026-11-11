# AGENTS.md

Read this before you change anything. It is for coding agents and for people. This repo is public, so this file names no person, machine or chat. Lines marked CONFIRM need the owner's check before this file merges.

## Map

- `plugin/speakeasy/` is the Hermes plugin (Python). The version is in `plugin/speakeasy/plugin.yaml`.
- `mac/` is the Mac app and the shared Swift package (`SpeakeasyCore`, `SpeakeasyClient`). The iPhone app in the private Apple repo builds against this package at a pinned sha, so a change here can break that app.
- `relay/push/` is the push relay.
- `docs/API.md` is the contract between the plugin and every app. `docs/ARCHITECTURE.md` explains the design. `docs/AGENT_SETUP.md` is the source of the website's `/setup.md`.

## Rules that are easy to break

1. Run `scripts/scan-secrets.sh` before every push. Install the hook with `ln -sf ../../scripts/pre-push .git/hooks/pre-push`.
2. A change to a request or response shape changes `docs/API.md` and the contract fixtures in `mac/Tests/SpeakeasyCoreTests/Contract/` in the same pull request.
3. A change to setup changes `docs/AGENT_SETUP.md` and `README.md` together. Then follow `docs/SETUP_DOCS_RELEASE.md`.
4. Every user-visible change adds a line to `CHANGELOG.md` under the version in `plugin.yaml`.
5. Nothing at runtime writes the Hermes `config.yaml`, opens `.env` or `auth.json`, or restarts the gateway.

## Tests

Commands are in `CONTRIBUTING.md`. All of them run offline. Known failures are in `scripts/known-test-issues.tsv`. `scripts/plugin-tests-parallel.py` runs the plugin suite in shards.

## Branches

- `main` is what users install. CONFIRM: only the owner merges to `main` and publishes releases.
- `bNN` or `relNNN` is the branch for the build in progress. Base pull requests on it.
- One issue, one branch, one writer. Do not push to a branch you did not open.

## Done

A lane is done when a test fails on the base and passes on the head, no test newly fails, and the pull request names the issue and the base. A summary that says "done" is not evidence.
