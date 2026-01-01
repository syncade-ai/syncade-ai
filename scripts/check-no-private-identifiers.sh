#!/usr/bin/env bash
# check-no-private-identifiers.sh — the pre-publish leak gate (PR-v2-26).
#
# Fails if a private identifier — a former project codename, or the operator's
# home-directory username — appears in any tracked file's CONTENT or FILENAME.
# Both artifacts leaked a real project's identity into shipped test fixtures; this
# gate makes their return a build failure rather than a public data leak.
#
# Content-only greps are not enough: a codename hidden in a *filename*
# (e.g. a test module named after the project) slips a `git grep`, so this also
# scans `git ls-files`. Matching is case-insensitive.
#
# This script assembles the tokens at runtime from concatenated fragments, so the
# literals never appear in this file — otherwise the gate would trip on itself.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# Assembled so the literal strings are never present in this tracked file.
tok_code="mer""cury"
tok_home="nickciu""botariu"

fail=0
for tok in "$tok_code" "$tok_home"; do
  content="$(git grep -ail -e "$tok" || true)"
  if [ -n "$content" ]; then
    echo "PRIVATE IDENTIFIER in tracked content (case-insensitive match):"
    printf '%s\n' "$content" | sed 's/^/  /'
    fail=1
  fi
  names="$(git ls-files | grep -i -- "$tok" || true)"
  if [ -n "$names" ]; then
    echo "PRIVATE IDENTIFIER in tracked filename:"
    printf '%s\n' "$names" | sed 's/^/  /'
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "FAIL: private identifiers must be scrubbed before any public push." >&2
  exit 1
fi
echo "OK: no private identifiers in tracked content or filenames."
