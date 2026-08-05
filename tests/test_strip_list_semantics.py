"""What `strip_repo_context_files` actually matches — PR-h-03 item 2.

The docs called these "globs" and the config field promised the worktree strip and the diff
filter "can never diverge". Both claims were false. This pins the real semantics so the
documentation cannot drift away from them again:

- matching is BASENAME EQUALITY, not globbing
- the two surfaces share the LIST but not the MATCHING

A doc fix without this test would be true only until someone edits either matcher.
"""

from __future__ import annotations

import pytest

from syncade.diff_filter import filter_diff_for_reviewer
from syncade.worktree_paths import _strip_files

_DIFF = "diff --git a/{p} b/{p}\n+SECRET\n"


@pytest.mark.parametrize(
    "entry,strips",
    [
        ("notes.md", True),  # exact basename — the documented shape
        ("*.md", False),  # NOT a glob; the docs used to say it was
        ("**/notes.md", True),  # "works", but only because rsplit discards the '**/'
    ],
)
def test_diff_filter_matches_by_basename_not_glob(entry, strips):
    filtered = filter_diff_for_reviewer(_DIFF.format(p="notes.md"), [entry])
    assert ("SECRET" not in filtered) is strips


def test_the_two_surfaces_share_the_list_but_not_the_matching(tmp_path):
    """The divergence the config field used to deny.

    An entry containing '/' strips the diff hunk but leaves the file readable in the
    reviewer's worktree — a leak through the surface the other one covers.
    """
    entry = "docs/CLAUDE.md"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "CLAUDE.md").write_text("SECRET\n")

    _strip_files(tmp_path, [entry])
    worktree_still_has_it = (tmp_path / "docs" / "CLAUDE.md").exists()

    diff_stripped = "SECRET" not in filter_diff_for_reviewer(
        _DIFF.format(p="docs/CLAUDE.md"), [entry]
    )

    assert diff_stripped, "diff filter no longer basenames a '/'-bearing entry"
    assert worktree_still_has_it, (
        "the worktree strip now accepts a '/'-bearing entry — the divergence is fixed, so "
        "the docs in config.py / config-reference.md / diff_filter.py must be updated"
    )


def test_bare_basenames_are_handled_identically_by_both(tmp_path):
    """The shape the docs now recommend must actually work on both surfaces."""
    (tmp_path / "CLAUDE.md").write_text("SECRET\n")
    _strip_files(tmp_path, ["CLAUDE.md"])
    assert not (tmp_path / "CLAUDE.md").exists()
    assert "SECRET" not in filter_diff_for_reviewer(_DIFF.format(p="CLAUDE.md"), ["CLAUDE.md"])
