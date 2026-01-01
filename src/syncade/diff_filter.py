"""Reviewer-facing diff filtering.

Reviewer worktrees have ``CLAUDE.md`` / ``AGENTS.md`` removed (the
architectural invariant that reviewers must not see project memory).
But ``git diff <base>..HEAD`` — captured in the operator's repo, where
those files still exist — contains hunks for them. A reviewer whose
worktree lacks ``CLAUDE.md`` but whose diff shows it changed will flag
a "tracked deletion" / missing-file finding. That false positive
persisted across all three rounds of the validation and drove the
loop to exit 20.

:func:`filter_diff_for_reviewer` removes those hunks at the orchestrator
boundary, before the diff reaches ``render_reviewer_prompt``. The
worktree strip is unchanged; the diff strip complements it so the file
is invisible across BOTH surfaces.

:data:`REVIEWER_STRIP_FILES` is the single source of truth for which
files are stripped: it feeds the default of
:attr:`syncade.config.ReviewConfig.strip_repo_context_files`, which the
orchestrator passes to BOTH the worktree-create call AND this filter.
That way the two surfaces share one runtime list and can never diverge
(if an operator customizes ``strip_repo_context_files``, both the
worktree strip and the diff filter pick it up).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

REVIEWER_STRIP_FILES: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")
"""Files removed from reviewer worktrees AND from the reviewer-facing
diff. Single source of truth: the default of
:attr:`syncade.config.ReviewConfig.strip_repo_context_files` is
``list(REVIEWER_STRIP_FILES)``, and the orchestrator passes that config
value to both the worktree strip and :func:`filter_diff_for_reviewer`."""

# The canonical per-file boundary in a unified diff: git emits a
# ``diff --git a/<path> b/<path>`` line for every file — including binary
# files and pure renames/deletions, which carry no ``@@`` chunk markers.
# Greedy ``.+`` on the a-path backtracks to the last `` b/`` so the
# common ``a/X b/X`` case parses cleanly; pathological paths containing
# `` b/`` are not a concern for the basenames we strip.
_DIFF_GIT_HEADER = re.compile(r"^diff --git a/(?P<a>.+) b/(?P<b>.+)$")


def filter_diff_for_reviewer(diff_text: str, strip_files: Iterable[str]) -> str:
    """Remove the diff hunks of ``strip_files`` from a unified diff.

    Used by the orchestrator before calling ``render_reviewer_prompt`` so
    reviewers don't see edits to files that are stripped from their
    worktrees. ``snapshot.diff_text`` itself is never modified — callers
    rebind the filtered result locally.

    Matching is by basename on EITHER side of the ``diff --git`` header,
    so ``CLAUDE.md``, ``docs/CLAUDE.md``, and a tracked deletion of
    ``CLAUDE.md`` (which git still writes as ``a/CLAUDE.md b/CLAUDE.md``)
    are all recognized. A ``diff --git`` line that fails to parse is
    conservatively KEPT — under-stripping leaves the reviewer-template
    "Stripped files" note as a backstop, whereas over-stripping could
    drop real code from review.

    Edge cases (per the design):

    - empty ``diff_text`` → ``""``
    - no hunk matches → original text returned byte-for-byte
    - every hunk matches → ``""``

    Args:
        diff_text: A unified diff string (``git diff <base>..HEAD``).
        strip_files: File names whose hunks to remove. Entries are
            compared by basename, so bare ``"CLAUDE.md"`` matches a
            nested ``docs/CLAUDE.md`` hunk.

    Returns:
        The diff with the matching files' hunks removed; retained hunks
        are preserved byte-for-byte and in their original order.
    """
    if not diff_text:
        return diff_text

    strip_basenames = {name.rsplit("/", 1)[-1] for name in strip_files}
    if not strip_basenames:
        return diff_text

    # Split into per-file sections on ``diff --git`` boundaries, keeping
    # line endings so retained hunks are reassembled byte-for-byte. Any
    # content before the first ``diff --git`` (anomalous for git diff,
    # but handled defensively) becomes a leading section with no header,
    # which is always kept.
    sections: list[list[str]] = []
    current: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)

    kept: list[str] = []
    for section in sections:
        if _section_targets_stripped_file(section[0], strip_basenames):
            continue
        kept.extend(section)
    return "".join(kept)


def _section_targets_stripped_file(header_line: str, strip_basenames: set[str]) -> bool:
    """True iff ``header_line`` is a ``diff --git`` header whose a- or
    b-path basename is in ``strip_basenames``. Non-header lines (a
    defensive preamble section) and unparseable headers return False
    (keep the section)."""
    match = _DIFF_GIT_HEADER.match(header_line.rstrip("\n"))
    if match is None:
        return False
    a_base = match.group("a").rsplit("/", 1)[-1]
    b_base = match.group("b").rsplit("/", 1)[-1]
    return a_base in strip_basenames or b_base in strip_basenames
