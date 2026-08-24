#!/usr/bin/env bash
# check-loc.sh <limit> — a mechanical code-line gate for syncade's [[checks]]
# (PR-21). Lists every git-tracked .py file under src/ and tests/ whose
# nonblank, non-comment line count exceeds <limit>, and exits non-zero if any do.
#
# Wired into syncade's own .syncade/config.toml as a BLOCKING check after PR-R3
# cleared the tree under the cap. The `findings_md.py drifted to 506 and nothing
# caught it` lesson from the PR-20 dogfood is enforced through the mechanical
# lane, not prompt text.
#
# Runs in a fresh git worktree (the check leg's blindness mechanism), so
# `git ls-files` sees the snapshot's tracked sources — no .venv / build noise.
set -euo pipefail

limit="${1:?usage: check-loc.sh <limit>}"

# Reject a non-numeric limit loudly. Without this, a mistyped limit makes the
# `[ "$n" -gt "$limit" ]` comparison below emit "integer expression expected"
# for every file yet still exit 0 with a success message — silently disabling
# the gate for that invocation. A gate that can't evaluate its threshold must
# fail, not pass.
case $limit in
    '' | *[!0-9]*)
        printf 'error: limit must be a non-negative integer, got: %s\n' "$limit" >&2
        exit 2
        ;;
esac

# Enumerate BEFORE the loop, and refuse an enumeration we could not make.
#
# `git ls-files` used to live in the loop's process substitution, where its failure is
# invisible to `set -euo pipefail`: the loop read nothing, found nothing over the limit, and
# printed the success line. Measured — in a directory with no `.git` and a planted 900-line
# file, this gate said "all tracked src/ + tests/ .py files are within 500 code LOC" and
# exited 0. Same for a real repository with an empty tracked set.
#
# Same rule as the limit check above, applied to the INPUT rather than the threshold: a gate
# that cannot see what it is checking must fail, not pass. Exit 2 (not 1) keeps "I could not
# check" distinguishable from "I checked and found violations" for any caller reading codes.
# stderr is NOT merged into the capture: a warning on git's stderr that happened to end in
# `.py` would enter the file list, and could make the emptiness check below see a non-empty
# list when the real tracked set is empty — defeating the guard this block exists to add.
# Git's own message still reaches the terminal; `git ls-files src tests` reproduces it.
if ! tracked=$(git ls-files src tests); then
    printf 'error: cannot enumerate tracked files (git failed — not a repository?)\n' >&2
    exit 2
fi
files=$(printf '%s\n' "$tracked" | grep '\.py$' || true)
if [ -z "$files" ]; then
    printf 'error: no tracked .py files under src/ or tests/ — refusing to report success over an empty enumeration\n' >&2
    exit 2
fi

over=0
exempt=0
while IFS= read -r f; do
    # `git ls-files` includes tracked paths deleted in the current dirty
    # worktree. Skip those so the gate can validate deletion patches before
    # they are committed.
    [ -f "$f" ] || continue
    n=$(awk '!/^[[:space:]]*$/ && !/^[[:space:]]*#/' "$f" | wc -l)
    if [ "$n" -gt "$limit" ]; then
        case "$f" in
            tests/resume/test_round_trip.py)
                printf '%s: %d code LOC > %d (exempt: cohesive resume round-trip fixture)\n' "$f" "$n" "$limit"
                exempt=$((exempt + 1))
                continue
                ;;
        esac
        printf '%s: %d code LOC > %d\n' "$f" "$n" "$limit"
        over=$((over + 1))
    fi
done <<< "$files"

if [ "$over" -gt 0 ]; then
    printf '\n%d file(s) over %d code LOC\n' "$over" "$limit" >&2
    exit 1
fi
if [ "$exempt" -gt 0 ]; then
    echo "all non-exempt tracked src/ + tests/ .py files are within $limit code LOC ($exempt targeted exemption)"
else
    echo "all tracked src/ + tests/ .py files are within $limit code LOC"
fi
