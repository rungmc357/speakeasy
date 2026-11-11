# AGENTS.md

The few rules every change in this repo needs. Commands and detail are in [CONTRIBUTING.md](CONTRIBUTING.md).

- This repo is public. Run `scripts/scan-secrets.sh` before every push.
- A change to a request or response changes `docs/API.md` and the contract fixtures in `mac/Tests/SpeakeasyCoreTests/Contract/` in the same pull request.
- The iPhone app builds against the Swift package in `mac/`. A change there can break that app.
- One issue, one branch, one writer. Don't push to a branch you didn't open.
- Done means a test that fails on the base and passes on the head, and no test newly fails.
