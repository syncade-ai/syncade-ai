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

# --- Which tree am I on? -------------------------------------------------------------------------
# On the DEV tree a raw scan is meaningless and always red: the unshipped files are SUPPOSED to be
# here, and shipped files legitimately reference them right up until `oss-scrub.py` rewrites those
# refs during staging. Scanning it answers a question nobody asked, which is why this gate — alone
# among the repo's three — was never wired into CI, and why "it is red" outlived three audits.
#
# The question that matters on the dev tree is "WOULD THE PUBLIC SNAPSHOT BE CLEAN?". So answer it:
# stage + scrub exactly as a release would, then re-run this same scan over that snapshot. Green
# here therefore means the same thing it means at release time, and it CANNOT go stale — it runs
# the real staging, not a description of it.
#
# `scripts/oss-scrub.py` is the discriminator: a tree that gets scrubbed before shipping is by
# definition the dev tree. The staged snapshot does not contain it (not on the allowlist), so the
# recursion terminates after exactly one hop, with no flag to keep in sync.
if [ -f scripts/oss-scrub.py ] && [ -z "${SYNCADE_OSS_SNAPSHOT:-}" ]; then
  echo "dev tree — verifying the PUBLIC SNAPSHOT this tree would produce:" >&2
  self="$PWD/scripts/check-no-internal-refs.sh"   # resolve BEFORE cd'ing into the snapshot
  stage="$(scripts/oss-stage.sh)"
  trap 'rm -rf "$stage"' EXIT
  git -C "$stage" init -q -b main && git -C "$stage" add -A
  ( cd "$stage" && SYNCADE_OSS_SNAPSHOT=1 exec "$self" )
  exit $?
fi

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
            git grep -niE -e "$tierA_pattern" -- docs/config-reference.md docs/how-to-use.md; } | sort -u || true )"
report "DANGLING reference to an EXCLUDED (unshipped) file in a shipped file:" "$tierA"


# --- Tier B: internal PR-number citations, banned in user-facing docs + skill bundles -----------
# `PR-h-`/`pr-h-` are NOT covered by `PR-[0-9]` — after `PR-` comes a letter — so the entire
# hardening-wave series (and `pr-h-field-NN`) was invisible to this gate. Found while cutting
# 0.7.0: one `PR-h-10 item 6` reference had already shipped to the public CHANGELOG. Matched
# on the prefix rather than a digit class so `pr-h-field-07` is covered by construction.
tierB_pattern='PR-v2-[0-9]|pr-v2-[0-9]|PR-[0-9]|pr-[0-9]|PR #[0-9]|PR-h-|pr-h-'
tierB_paths=(
  README.md SECURITY.md CONTRIBUTING.md CHANGELOG.md CODE_OF_CONDUCT.md
  docs/config-reference.md docs/how-to-use.md
  .claude/skills .codex/skills src/syncade/skills
)
tierB="$(git grep -niE -e "$tierB_pattern" -- "${tierB_paths[@]}" | sort -u || true)"
report "INTERNAL PR-number reference in a user-facing doc / skill bundle:" "$tierB"

# --- Tier A2: references to a scripts/ path that is NOT IN THIS TREE ------------------------------
# The pattern above is a hand-maintained list of doc paths, so it only catches what someone
# thought to add. It missed two real dangling refs in one PR: CONTRIBUTING.md told public
# contributors to run scripts/check-no-private-identifiers.sh, which is dev-repo-only, and the
# first fix for that named scripts/oss-release.sh, which does not ship either.
#
# This needs no list. In snapshot mode the tree IS the public snapshot, so any scripts/ path
# named by a USER-FACING file either exists here or is a promise the reader cannot keep. Skipped
# on the dev tree, where every script legitimately exists.
#
# Scoped to the same paths as tier B, and for the same reason already recorded there: in src/
# and tests/ a scripts/ path is usually test DATA or maintainer context (a fixture command like
# `scripts/check.sh`, a docstring naming the dev tooling), not an instruction anyone will follow.
# Widening it there produced exactly those false positives.
if [ -n "${SYNCADE_OSS_SNAPSHOT:-}" ]; then
  tierA2=""
  while IFS= read -r hit; do
    [ -n "$hit" ] || continue
    file="${hit%%:*}"
    for ref in $(printf '%s\n' "$hit" | grep -oE 'scripts/[A-Za-z0-9._-]+\.(sh|py)' | sort -u); do
      [ -e "$ref" ] || tierA2="${tierA2}${file}: references missing ${ref}"$'\n'
    done
    # CHANGELOG is exempt: announcing a REMOVAL has to name the thing removed, and
    # "scripts/install-skill.sh was deleted" is a true statement about a file that is
    # correctly absent — the opposite of a promise the reader cannot keep.
  done < <(git grep -nE 'scripts/[A-Za-z0-9._-]+\.(sh|py)' -- "${tierB_paths[@]}" ':!CHANGELOG.md' || true)
  report "Shipped file references a scripts/ path absent from the snapshot:" \
    "$(printf '%s' "$tierA2" | sort -u)"
fi

if [ "$fail" -ne 0 ]; then
  echo "FAIL: scrub internal dev references before any public push." >&2
  exit 1
fi
echo "OK: no dangling excluded-file refs, and no internal PR-refs in user-facing docs/skills."
