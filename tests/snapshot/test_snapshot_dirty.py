"""Dirty-state classification + orchestrator soft-note tests for
:mod:`syncade.snapshot`.

Split from the original ``tests/test_snapshot.py`` — covers the
``dirty_state`` / ``untracked_count`` fields, the
``_classify_porcelain`` helpers, the ``Snapshot`` dataclass invariants,
and the orchestrator's soft dirty-tree note. Uses real ``git`` subprocess
calls against an ephemeral repo under ``tmp_path`` — no mocking.
"""

from __future__ import annotations

import shutil

import pytest

from syncade.snapshot import Snapshot, SnapshotError, take_snapshot
from tests.snapshot._helpers import _git

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not found on PATH",
)


# ---------------------------------------------------------------------------
# Dirty tree (documented behavior: snapshot captures HEAD silently)
# ---------------------------------------------------------------------------


class TestDirtyTree:
    def test_unstaged_changes_do_not_raise(self, single_commit_repo):
        """A dirty working tree is intentionally NOT a snapshot error
        in PR-05. The reviewers see HEAD; uncommitted changes are
        invisible. Documented in take_snapshot's docstring."""
        repo_path, _ = single_commit_repo
        (repo_path / "README.md").write_text("modified but not committed\n")
        snap = take_snapshot(repo_path)
        # Snapshot succeeded; HEAD SHA is still the initial commit.
        assert isinstance(snap, Snapshot)
        # The diff was not requested, so dirty state isn't captured.
        assert snap.diff_text == ""

    def test_staged_but_uncommitted_changes_also_silently_ignored(self, single_commit_repo):
        repo_path, _ = single_commit_repo
        (repo_path / "new.txt").write_text("staged\n")
        _git(repo_path, "add", "new.txt")
        snap = take_snapshot(repo_path)
        assert isinstance(snap, Snapshot)
        assert snap.diff_text == ""


class TestSnapshotDirtyStateField:
    """PR-7.5: ``Snapshot.dirty_state`` is a four-state Literal
    classification of ``git status --porcelain`` output —
    distinguishing tracked-modified (the actually-dangerous case)
    from untracked-only (the usually-intentional Phase-04 Acme
    case).

    The four states are: ``"clean"``, ``"tracked"``,
    ``"untracked"``, ``"both"``."""

    def test_clean_tree_is_classified_clean(self, single_commit_repo):
        repo_path, _ = single_commit_repo
        snap = take_snapshot(repo_path)
        assert snap.dirty_state == "clean"

    def test_tracked_modification_is_classified_tracked(self, single_commit_repo):
        repo_path, _ = single_commit_repo
        (repo_path / "README.md").write_text("modified but not committed\n")
        snap = take_snapshot(repo_path)
        assert snap.dirty_state == "tracked"

    def test_staged_tracked_change_is_classified_tracked(self, single_commit_repo):
        """A staged-but-uncommitted modification to a tracked file —
        porcelain prefix ``"M "`` (mode marker in column 0, space
        in column 1). Classifies as tracked."""
        repo_path, _ = single_commit_repo
        (repo_path / "README.md").write_text("staged-modified content\n")
        _git(repo_path, "add", "README.md")
        snap = take_snapshot(repo_path)
        assert snap.dirty_state == "tracked"

    def test_staged_new_file_is_classified_tracked(self, single_commit_repo):
        """``git add`` of a new file → porcelain prefix ``"A "`` —
        still classifies as tracked since the file is no longer
        in the untracked set once it's been added."""
        repo_path, _ = single_commit_repo
        (repo_path / "newfile.txt").write_text("staged content\n")
        _git(repo_path, "add", "newfile.txt")
        snap = take_snapshot(repo_path)
        assert snap.dirty_state == "tracked"

    def test_untracked_only_is_classified_untracked(self, single_commit_repo):
        """The Phase-04 Acme case: untracked-only scratch files.
        The four-state classifier distinguishes this from tracked changes."""
        repo_path, _ = single_commit_repo
        (repo_path / "scratch.txt").write_text("untracked\n")
        snap = take_snapshot(repo_path)
        assert snap.dirty_state == "untracked"

    def test_untracked_with_spaces_in_name(self, single_commit_repo):
        """Edge case: ``?? path/with spaces.txt`` — the porcelain
        parser must classify by the first two chars, not by
        whitespace-tokenizing the line."""
        repo_path, _ = single_commit_repo
        (repo_path / "spaces in name.txt").write_text("scratch\n")
        snap = take_snapshot(repo_path)
        assert snap.dirty_state == "untracked"

    def test_untracked_with_leading_dot(self, single_commit_repo):
        """``?? .dotfile`` is still untracked; nothing in the parser
        should special-case dotfiles."""
        repo_path, _ = single_commit_repo
        (repo_path / ".scratch").write_text("hidden\n")
        snap = take_snapshot(repo_path)
        assert snap.dirty_state == "untracked"

    def test_both_states_present_is_classified_both(self, single_commit_repo):
        """Tracked-modified + untracked-only co-occurring → ``both``.
        Operator needs to know about both classes."""
        repo_path, _ = single_commit_repo
        (repo_path / "README.md").write_text("tracked modification\n")
        (repo_path / "scratch.txt").write_text("untracked\n")
        snap = take_snapshot(repo_path)
        assert snap.dirty_state == "both"


class TestClassifyPorcelainSynthesizedInput:
    """Direct unit tests of the ``_classify_porcelain`` helper.
    Synthesizes porcelain output to exercise the prefix-matching
    rules without needing real git state."""

    def test_empty_string_is_clean(self):
        from syncade.snapshot import _classify_porcelain

        assert _classify_porcelain("") == "clean"

    def test_whitespace_only_is_clean(self):
        from syncade.snapshot import _classify_porcelain

        assert _classify_porcelain("   \n\t\n") == "clean"

    def test_modified_unstaged_is_tracked(self):
        """``" M file"`` (space in col 0, M in col 1) — modified
        but not staged."""
        from syncade.snapshot import _classify_porcelain

        assert _classify_porcelain(" M src/foo.py\n") == "tracked"

    def test_modified_staged_is_tracked(self):
        """``"M  file"`` (M in col 0) — staged modification."""
        from syncade.snapshot import _classify_porcelain

        assert _classify_porcelain("M  src/foo.py\n") == "tracked"

    def test_modified_staged_and_modified_again_is_tracked(self):
        """``"MM file"`` — staged-and-then-modified-again."""
        from syncade.snapshot import _classify_porcelain

        assert _classify_porcelain("MM src/foo.py\n") == "tracked"

    def test_added_file_is_tracked(self):
        from syncade.snapshot import _classify_porcelain

        assert _classify_porcelain("A  src/new.py\n") == "tracked"

    def test_deleted_file_is_tracked(self):
        from syncade.snapshot import _classify_porcelain

        assert _classify_porcelain("D  src/gone.py\n") == "tracked"

    def test_renamed_file_is_tracked(self):
        from syncade.snapshot import _classify_porcelain

        assert _classify_porcelain("R  old.py -> new.py\n") == "tracked"

    def test_copied_file_is_tracked(self):
        from syncade.snapshot import _classify_porcelain

        assert _classify_porcelain("C  src/foo.py -> src/bar.py\n") == "tracked"

    def test_untracked_is_untracked(self):
        from syncade.snapshot import _classify_porcelain

        assert _classify_porcelain("?? scratch.txt\n") == "untracked"

    def test_multiple_untracked_only_is_untracked(self):
        from syncade.snapshot import _classify_porcelain

        out = "?? a.txt\n?? b.txt\n?? .dotfile\n"
        assert _classify_porcelain(out) == "untracked"

    def test_mixed_is_both(self):
        from syncade.snapshot import _classify_porcelain

        out = " M src/foo.py\n?? scratch.txt\n"
        assert _classify_porcelain(out) == "both"

    def test_phase_04_acme_regression_fixture(self):
        """Phase-04 Acme run had two ``cowork/alpha-briefings/*.md``
        untracked files at snapshot time. The PR-5.6 boolean
        ``dirty=True`` fired the strong "uncommitted changes" warning
        — misleading; the operator knew about and intended the
        scratch files. The fixture is the verbatim porcelain output
        from that run."""
        from pathlib import Path

        from syncade.snapshot import _classify_porcelain

        fixture = (
            Path(__file__).parent.parent
            / "fixtures"
            / "pr-07.5-untracked-only-warning"
            / "porcelain.txt"
        )
        if not fixture.exists():
            pytest.skip("Phase-04 fixture not present")
        out = fixture.read_text()
        assert _classify_porcelain(out) == "untracked"


# ---------------------------------------------------------------------------
# Snapshot dataclass invariants
# ---------------------------------------------------------------------------


class TestSnapshotDataclass:
    def test_snapshot_is_frozen(self, single_commit_repo):
        from dataclasses import FrozenInstanceError

        repo_path, _ = single_commit_repo
        snap = take_snapshot(repo_path)
        with pytest.raises(FrozenInstanceError):
            snap.commit_sha = "x" * 40  # type: ignore[misc]

    def test_public_surface_has_docstrings(self):
        import inspect

        assert inspect.getdoc(Snapshot)
        assert inspect.getdoc(SnapshotError)
        assert inspect.getdoc(take_snapshot)


class TestUntrackedCount:
    """T2.10: Snapshot.untracked_count is captured at snapshot time
    so the orchestrator's soft note can include the count without a
    second ``git status`` shell-out."""

    def test_clean_tree_count_is_zero(self, single_commit_repo):
        repo_path, _ = single_commit_repo
        snap = take_snapshot(repo_path)
        assert snap.untracked_count == 0

    def test_tracked_only_count_is_zero(self, single_commit_repo):
        """Tracked modifications don't count as untracked — the
        field is specifically the untracked count."""
        repo_path, _ = single_commit_repo
        (repo_path / "README.md").write_text("modified\n")
        snap = take_snapshot(repo_path)
        assert snap.dirty_state == "tracked"
        assert snap.untracked_count == 0

    def test_untracked_only_count(self, single_commit_repo):
        repo_path, _ = single_commit_repo
        (repo_path / "a.txt").write_text("scratch\n")
        (repo_path / "b.txt").write_text("scratch\n")
        (repo_path / "c.txt").write_text("scratch\n")
        snap = take_snapshot(repo_path)
        assert snap.dirty_state == "untracked"
        assert snap.untracked_count == 3

    def test_both_states_count_untracked_only(self, single_commit_repo):
        """In 'both' state, untracked_count counts ONLY the
        untracked files (not the tracked modifications)."""
        repo_path, _ = single_commit_repo
        (repo_path / "README.md").write_text("tracked-modified\n")
        (repo_path / "scratch1.txt").write_text("u1\n")
        (repo_path / "scratch2.txt").write_text("u2\n")
        snap = take_snapshot(repo_path)
        assert snap.dirty_state == "both"
        assert snap.untracked_count == 2

    def test_classify_porcelain_with_counts_synthetic(self):
        """Direct unit test of the helper. Documents the
        clean/tracked/untracked/both states' count semantics."""
        from syncade.snapshot import _classify_porcelain_with_counts

        assert _classify_porcelain_with_counts("") == ("clean", 0)
        assert _classify_porcelain_with_counts(" M file.py\n") == ("tracked", 0)
        assert _classify_porcelain_with_counts("?? a\n?? b\n") == ("untracked", 2)
        assert _classify_porcelain_with_counts(" M foo\n?? bar\n?? baz\n") == ("both", 2)
