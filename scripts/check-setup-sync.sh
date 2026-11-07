#!/usr/bin/env bash
# Fails when the published setup guide (https://speakeasyvoice.ai/setup.md, what agents read)
# differs from docs/AGENT_SETUP.md. Run before every release; the site copy is a verbatim copy
# of docs/AGENT_SETUP.md, so any docs edit must be copied to the site and published too.
set -euo pipefail
cd "$(dirname "$0")/.."
url="${SPEAKEASY_SETUP_URL:-https://speakeasyvoice.ai/setup.md}"
tmp="$(mktemp -t setup.md.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
if ! curl -fsS --max-time 20 "$url" -o "$tmp"; then
  echo "check-setup-sync: couldn't fetch $url" >&2
  exit 2
fi
if ! diff -u docs/AGENT_SETUP.md "$tmp"; then
  echo "" >&2
  echo "check-setup-sync: docs/AGENT_SETUP.md and $url differ." >&2
  echo "Copy docs/AGENT_SETUP.md to the site's setup.md, publish it, then rerun this check." >&2
  exit 1
fi
echo "check-setup-sync: docs/AGENT_SETUP.md matches $url"
