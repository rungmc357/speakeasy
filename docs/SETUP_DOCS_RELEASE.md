# Setup documentation release check

`docs/AGENT_SETUP.md` is the source for the website's `/setup.md`, which agents read during installation.

Before a release that changes setup:

1. Update `docs/AGENT_SETUP.md` and the README together. Check the same-Mac, phone, existing-chat and terminal-only paths.
2. Copy `docs/AGENT_SETUP.md` verbatim to `setup.md` in the website repository. Prepare a website branch and preview; publishing needs the owner's approval.
3. Check the preview before publishing:
   ```sh
   SPEAKEASY_SETUP_URL=https://your-preview.example/setup.md bash scripts/check-setup-sync.sh
   ```
4. After the approved website publish, run `bash scripts/check-setup-sync.sh` against the live website. Exit 0 means identical; 1 means drift; 2 means the guide could not be fetched. Do not call the documentation synchronized until this passes against the public URL.

A code release does not publish the website. If publication is waiting for approval, record that the new setup guide is prepared but not live; keep unrelated app releases independent.
