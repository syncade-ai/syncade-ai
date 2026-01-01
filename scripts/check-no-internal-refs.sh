#!/usr/bin/env bash
# check-no-internal-refs.sh — keep internal dev references out of the PUBLIC release surface.
#
# The public release (pr-v2-oss-release) ships an allowlist; internal dev artifacts (docs/prs/,
# the PRD, the dogfood log, docs/what-this-is.md, principal reviews/audits, docs/design + notes,
# CLAUDE.md/AGENTS.md, scripts/reviewer_usefulness.py) do NOT ship. Two failure modes to prevent:
#
#   Tier A — a reference to an EXCLUDED file from ANY shipped file (a README/comment pointing at
#            docs/prs/…, the PRD, or reviewer_usefulness.py) dangles in the public repo.
#   Tier B — an internal PR-number citation (PR-v2-9, PR-29, pr-24) in a USER-FACING doc or the
#            installable skill bundles — surfaces process noise to public readers/users.
#
# Deliberately OUT of scope (project decision): bare PR-number PROVENANCE comments in src/ and
# tests/ — maintainer context, not sensitive (private identifiers are scrubbed by a separate
# gate), and useful to contributors. CLAUDE.md/AGENTS.md filenames are generic harness convention,
# not banned. The oss-release script re-runs both tiers over the actual staged tree.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

fail=0
report() { # $1 = header, remaining = grep hits
  local header="$1"; shift
  if [ -n "$*" ]; then
    echo "$header" >&2
    printf '%s\n' "$*" | sed 's/^/  /' >&2
    fail=1
  fi
}

# --- Tier A: references to EXCLUDED files, banned in every shipped file --------------------------
# Only docs/config-reference.md ships from docs/, so exclude all of docs/ and scan it back in.
tierA_pattern='docs/prs|docs/syncade-prd|docs/dogfood-log|docs/principal-|docs/what-this-is|docs/design/|docs/notes/|docs/codebase-review|docs/consolidated-audit|reviewer.usefulness'
tierA_exclude=(
  ':!docs' ':!CLAUDE.md' ':!AGENTS.md' ':!.syncade/'
  ':!scripts/reviewer_usefulness.py' ':!scripts/check-no-internal-refs.sh' ':!scripts/oss-release.sh'
)
tierA="$( { git grep -niE -e "$tierA_pattern" -- . "${tierA_exclude[@]}"; \
            git grep -niE -e "$tierA_pattern" -- docs/config-reference.md; } | sort -u || true )"
report "DANGLING reference to an EXCLUDED (unshipped) file in a shipped file:" "$tierA"

# --- Tier B: internal PR-number citations, banned in user-facing docs + skill bundles -----------
tierB_pattern='PR-v2-[0-9]|pr-v2-[0-9]|PR-[0-9]|pr-[0-9]|PR #[0-9]'
tierB_paths=(
  README.md SECURITY.md CONTRIBUTING.md CHANGELOG.md CODE_OF_CONDUCT.md
  docs/config-reference.md
  .claude/skills .codex/skills src/syncade/skills
)
tierB="$(git grep -niE -e "$tierB_pattern" -- "${tierB_paths[@]}" | sort -u || true)"
report "INTERNAL PR-number reference in a user-facing doc / skill bundle:" "$tierB"

if [ "$fail" -ne 0 ]; then
  echo "FAIL: scrub internal dev references before any public push." >&2
  exit 1
fi
echo "OK: no dangling excluded-file refs, and no internal PR-refs in user-facing docs/skills."
