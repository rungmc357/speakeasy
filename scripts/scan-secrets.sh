#!/usr/bin/env bash
# Fails if the tree contains anything that looks like a credential or a personal identifier.
# Run before every push.
set -euo pipefail
cd "$(dirname "$0")/.."
# Every check below uses `git grep`, which matches nothing outside a repository (and only the outer
# repository's files inside someone else's). Refuse to report "clean" for a tree it can't search.
if [[ "$(git rev-parse --show-toplevel 2>/dev/null)" != "$(pwd -P)" ]]; then
  echo "scan-secrets: $(pwd -P) is not the top of a git repository, so nothing would be searched." >&2
  echo "  For a plain snapshot (git archive), run: git init -q && git add -A  first." >&2; exit 2
fi
patterns=(
  'sk-[A-Za-z0-9_-]{20,}'            # OpenAI-style keys
  'sk-ant-[A-Za-z0-9_-]{20,}'        # Anthropic keys
  'ghp_[A-Za-z0-9]{30,}'             # GitHub tokens
  'xox[bpas]-[A-Za-z0-9-]{10,}'      # Slack tokens
  'AKIA[0-9A-Z]{16}'                 # AWS access keys
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'op://'                            # 1Password references
  '[a-z0-9-]+\.tail[0-9a-f]{6}\.ts\.net'   # a real tailnet hostname
  '\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b'  # tailnet IPs
  '[0-9]{17,20}'                     # Discord/Telegram-style IDs
)
extra_file="${SPEAKEASY_PRIVATE_TERMS:-$HOME/.config/speakeasy/private-terms.txt}"   # local personal terms, one per line
if [[ ! -f "$extra_file" && -z "${SPEAKEASY_NO_PRIVATE_TERMS:-}" ]]; then
  echo "scan-secrets: personal-terms list missing ($extra_file). Refusing to pass without it." >&2
  echo "  (set SPEAKEASY_NO_PRIVATE_TERMS=1 only on machines that never held personal data)" >&2; exit 2
fi
allow_file="${SPEAKEASY_ALLOW_TERMS:-}"     # optional local file of exact public strings that may contain a term
status=0
for p in "${patterns[@]}"; do
  if git grep -nIE -e "$p" -- . ':!scripts/scan-secrets.sh' >/tmp/speakeasy-scan.$$ 2>/dev/null; then
    echo "FOUND pattern: $p"; cat /tmp/speakeasy-scan.$$; status=1
  fi
done
if [[ -n "$extra_file" && -f "$extra_file" ]]; then
  while IFS= read -r term; do
    [[ -z "$term" ]] && continue
    if git grep -nIiF -e "$term" -- . >/tmp/speakeasy-scan.$$ 2>/dev/null; then
      if [[ -n "$allow_file" && -f "$allow_file" ]]; then
        # Drop hits whose only occurrences of the term sit inside an allowed public string.
        : >/tmp/speakeasy-scan-keep.$$
        while IFS= read -r hit; do
          rest="$hit"
          while IFS= read -r allowed; do
            [[ -z "$allowed" ]] && continue
            rest="${rest//"$allowed"/}"
          done < "$allow_file"
          if printf '%s' "$rest" | grep -qiF -e "$term"; then printf '%s\n' "$hit" >>/tmp/speakeasy-scan-keep.$$; fi
        done </tmp/speakeasy-scan.$$
        mv /tmp/speakeasy-scan-keep.$$ /tmp/speakeasy-scan.$$
      fi
      if [[ -s /tmp/speakeasy-scan.$$ ]]; then
        echo "FOUND private term"; cut -d: -f1,2 /tmp/speakeasy-scan.$$; status=1
      fi
    fi
  done < "$extra_file"
fi
rm -f /tmp/speakeasy-scan.$$
# Real sentences from your own voice calls must never be pasted into tests or docs.
if [[ -z "${SPEAKEASY_NO_CALL_CHECK:-}" ]] && command -v python3 >/dev/null; then
  python3 scripts/check-real-calls.py || status=1
fi
[[ $status -eq 0 ]] && echo "scan-secrets: clean"
exit $status
