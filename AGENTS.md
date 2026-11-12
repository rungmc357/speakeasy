# AGENTS.md

The few rules every change in this repo needs. Commands and detail are in [CONTRIBUTING.md](CONTRIBUTING.md).

- This repo is public. Run `scripts/scan-secrets.sh` before every push. It runs the secret scan and the real-call check, and both must pass.
- A change to a request or response changes `docs/API.md` and the contract fixtures in `mac/Tests/SpeakeasyCoreTests/Contract/` in the same pull request.
- The iPhone app builds against the Swift package in `mac/`. A change there can break that app.
- Base your branch on the open build branch (`bNN`) and target it with your pull request, never `main` or another lane's branch. A docs-only change may target `main` when no build branch is open.
- One issue, one branch, one writer. Don't push to a branch you didn't open.
- Push your branch early and often. Unpushed work is invisible to integration.
- Done means a test that fails on the base and passes on the head, and no test newly fails.
