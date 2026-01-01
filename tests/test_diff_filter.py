"""Tests for :mod:`syncade.diff_filter` (PR-17 Part 2).

``filter_diff_for_reviewer`` strips the hunks of named files from a
unified diff so reviewers — whose worktrees already have CLAUDE.md /
AGENTS.md removed — don't see a diff hunk claiming those files changed.
That mismatch (file absent from worktree, but present in the diff) is
the recurring "tracked deletion" false positive PR-16 hit.
"""

from syncade.config import ReviewConfig
from syncade.diff_filter import REVIEWER_STRIP_FILES, filter_diff_for_reviewer
from syncade.worktree_paths import _strip_files

# ---------------------------------------------------------------------------
# Diff fixtures — realistic `git diff <base>..HEAD` output shapes.
# ---------------------------------------------------------------------------

_CODE_HUNK = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1,2 +1,3 @@\n"
    " line one\n"
    "+added line\n"
    " line two\n"
)

_CLAUDE_HUNK = (
    "diff --git a/CLAUDE.md b/CLAUDE.md\n"
    "index 3333333..4444444 100644\n"
    "--- a/CLAUDE.md\n"
    "+++ b/CLAUDE.md\n"
    "@@ -1 +1,2 @@\n"
    " project memory\n"
    "+new memory line\n"
)

_AGENTS_HUNK = (
    "diff --git a/AGENTS.md b/AGENTS.md\n"
    "index 5555555..6666666 100644\n"
    "--- a/AGENTS.md\n"
    "+++ b/AGENTS.md\n"
    "@@ -1 +1,2 @@\n"
    " agents\n"
    "+more\n"
)


class TestFilterDiffForReviewer:
    def test_strips_one_target_preserves_others(self):
        """Multi-file diff with one stripped file → that file's hunk is
        gone; the other files' hunks are preserved byte-for-byte."""
        diff = _CODE_HUNK + _CLAUDE_HUNK
        result = filter_diff_for_reviewer(diff, ("CLAUDE.md", "AGENTS.md"))
        assert result == _CODE_HUNK
        assert "CLAUDE.md" not in result

    def test_no_target_files_returns_input_unchanged(self):
        """A diff that touches none of the stripped files comes through
        byte-identical."""
        diff = _CODE_HUNK
        result = filter_diff_for_reviewer(diff, ("CLAUDE.md", "AGENTS.md"))
        assert result == diff

    def test_only_target_files_returns_empty(self):
        """Every hunk is a stripped file → the reviewer sees an empty
        diff (round.py substitutes the no-diff sentinel downstream)."""
        diff = _CLAUDE_HUNK + _AGENTS_HUNK
        result = filter_diff_for_reviewer(diff, ("CLAUDE.md", "AGENTS.md"))
        assert result == ""

    def test_empty_input_returns_empty(self):
        assert filter_diff_for_reviewer("", ("CLAUDE.md", "AGENTS.md")) == ""

    def test_binary_hunk_for_target_is_stripped(self):
        """The `diff --git` boundary marker is present even for binary
        diffs (which have no `@@` chunk markers), so a binary CLAUDE.md
        hunk is still recognized and stripped."""
        binary_claude = (
            "diff --git a/CLAUDE.md b/CLAUDE.md\n"
            "index 1111111..2222222 100644\n"
            "Binary files a/CLAUDE.md and b/CLAUDE.md differ\n"
        )
        diff = _CODE_HUNK + binary_claude
        result = filter_diff_for_reviewer(diff, ("CLAUDE.md", "AGENTS.md"))
        assert result == _CODE_HUNK
        assert "Binary files" not in result

    def test_nested_path_target_is_stripped(self):
        """Basename matching: a `docs/CLAUDE.md` hunk is stripped even
        though the strip list holds the bare basename."""
        nested = (
            "diff --git a/docs/CLAUDE.md b/docs/CLAUDE.md\n"
            "index 1111111..2222222 100644\n"
            "--- a/docs/CLAUDE.md\n"
            "+++ b/docs/CLAUDE.md\n"
            "@@ -1 +1,2 @@\n"
            " x\n"
            "+y\n"
        )
        diff = _CODE_HUNK + nested
        result = filter_diff_for_reviewer(diff, ("CLAUDE.md", "AGENTS.md"))
        assert result == _CODE_HUNK

    def test_nested_agents_path_target_is_stripped(self):
        nested = (
            "diff --git a/pkg/AGENTS.md b/pkg/AGENTS.md\n"
            "index 1111111..2222222 100644\n"
            "--- a/pkg/AGENTS.md\n"
            "+++ b/pkg/AGENTS.md\n"
            "@@ -1 +1,2 @@\n"
            " x\n"
            "+y\n"
        )
        diff = _CODE_HUNK + nested
        result = filter_diff_for_reviewer(diff, ("CLAUDE.md", "AGENTS.md"))
        assert result == _CODE_HUNK

    def test_strips_both_targets_keeps_code(self):
        """Interleaved order is preserved for retained hunks."""
        diff = _CLAUDE_HUNK + _CODE_HUNK + _AGENTS_HUNK
        result = filter_diff_for_reviewer(diff, ("CLAUDE.md", "AGENTS.md"))
        assert result == _CODE_HUNK

    def test_deletion_of_target_is_stripped(self):
        """A genuine tracked deletion of CLAUDE.md (the PR-16 FP shape):
        git still writes `diff --git a/CLAUDE.md b/CLAUDE.md`, so the
        hunk is recognized."""
        deletion = (
            "diff --git a/CLAUDE.md b/CLAUDE.md\n"
            "deleted file mode 100644\n"
            "index 3333333..0000000\n"
            "--- a/CLAUDE.md\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-project memory\n"
        )
        diff = _CODE_HUNK + deletion
        assert filter_diff_for_reviewer(diff, ("CLAUDE.md", "AGENTS.md")) == _CODE_HUNK

    def test_does_not_mutate_input(self):
        diff = _CODE_HUNK + _CLAUDE_HUNK
        original = str(diff)
        filter_diff_for_reviewer(diff, ("CLAUDE.md", "AGENTS.md"))
        assert diff == original

    def test_empty_strip_list_returns_input_unchanged(self):
        """No files to strip → nothing removed."""
        diff = _CODE_HUNK + _CLAUDE_HUNK
        assert filter_diff_for_reviewer(diff, ()) == diff

    def test_partial_strip_list(self):
        """Only CLAUDE.md in the strip list → AGENTS.md survives."""
        diff = _CODE_HUNK + _CLAUDE_HUNK + _AGENTS_HUNK
        result = filter_diff_for_reviewer(diff, ("CLAUDE.md",))
        assert result == _CODE_HUNK + _AGENTS_HUNK


class TestReviewerStripFilesConstant:
    def test_constant_value(self):
        assert REVIEWER_STRIP_FILES == ("CLAUDE.md", "AGENTS.md")
        assert isinstance(REVIEWER_STRIP_FILES, tuple)

    def test_constant_is_config_default_source_of_truth(self):
        """PR-17 single-source-of-truth contract: the configurable
        worktree-strip default and the diff-filter constant must be the
        SAME list, so the worktree strip and the reviewer-facing diff
        filter can never diverge. ``ReviewConfig.strip_repo_context_files``
        defaults to ``list(REVIEWER_STRIP_FILES)``."""
        assert ReviewConfig().strip_repo_context_files == list(REVIEWER_STRIP_FILES)

    def test_worktree_and_diff_strip_surfaces_use_same_context_file_names(self, tmp_path):
        config = ReviewConfig()
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "CLAUDE.md").write_text("secret\n")
        (tmp_path / "docs" / "AGENTS.md").write_text("secret\n")
        (tmp_path / "docs" / "README.md").write_text("public\n")

        _strip_files(tmp_path, config.strip_repo_context_files)

        diff = (
            "diff --git a/docs/CLAUDE.md b/docs/CLAUDE.md\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/docs/AGENTS.md b/docs/AGENTS.md\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n" + _CODE_HUNK
        )

        assert not (tmp_path / "docs" / "CLAUDE.md").exists()
        assert not (tmp_path / "docs" / "AGENTS.md").exists()
        assert (tmp_path / "docs" / "README.md").exists()
        assert filter_diff_for_reviewer(diff, config.strip_repo_context_files) == _CODE_HUNK
