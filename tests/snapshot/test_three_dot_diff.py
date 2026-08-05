"""Three-dot (branch-point) diff semantics — PR-h-02 increment B, audit rank 19.

Diffing the literal ``base..HEAD`` range renders every commit that landed on
the base but not on our branch as a DELETION in our diff. Reviewers are asked
to justify removals nobody made, and the producer is handed those phantom
deletions as work. It is the default path for any branch that is behind its
base, which is most branches most of the time — the campaign that shipped
PR-h-01 diffed ``main..HEAD`` for three runs and six rounds.

Every case here CALIBRATES: it asserts the fixture really does produce the
wrong result under two-dot before asserting three-dot fixes it, so a pass
cannot come from a fixture that never exercised the behaviour.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from syncade.snapshot import SnapshotError, take_snapshot


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def behind_base(tmp_path: Path) -> Path:
    """A ``feature`` branch that is BEHIND ``main``.

    ``main`` gains ``teammate.py`` after we branched, so the literal
    ``main..HEAD`` range shows it as a deletion of work we never touched.
    """
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "shared.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "root")
    _git(tmp_path, "checkout", "-qb", "feature")
    (tmp_path / "mine.py").write_text("def mine(): pass\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "my work")
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "teammate.py").write_text("def helper(): pass\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "teammate work")
    _git(tmp_path, "checkout", "-q", "feature")
    return tmp_path


class TestBranchPointDiff:
    def test_branch_behind_base_has_no_phantom_deletions(self, behind_base):
        two_dot = take_snapshot(behind_base, base_ref="main", three_dot=False).diff_text
        assert "teammate.py" in two_dot and "-def helper" in two_dot, (
            "fixture does not reproduce the phantom deletion under two-dot"
        )

        three_dot = take_snapshot(behind_base, base_ref="main").diff_text
        assert "teammate.py" not in three_dot
        assert "mine.py" in three_dot

    def test_a_real_deletion_on_our_branch_still_shows(self, behind_base):
        """The inverse guard: suppressing phantom deletions must not suppress ours."""
        (behind_base / "shared.py").unlink()
        _git(behind_base, "add", "-A")
        _git(behind_base, "commit", "-qm", "delete shared")

        diff = take_snapshot(behind_base, base_ref="main").diff_text
        assert "shared.py" in diff and "-x = 1" in diff

    def test_two_dot_escape_hatch_restores_the_literal_range(self, behind_base):
        diff = take_snapshot(behind_base, base_ref="main", three_dot=False).diff_text
        assert "teammate.py" in diff

    def test_base_oid_records_the_branch_point_not_the_base_tip(self, behind_base):
        """``base_oid`` must name what the diff was ACTUALLY taken against."""
        tip = _git(behind_base, "rev-parse", "main")
        branch_point = _git(behind_base, "merge-base", "main", "HEAD")
        assert tip != branch_point, "fixture has no divergence to distinguish the two"

        snap = take_snapshot(behind_base, base_ref="main")
        assert snap.base_oid == branch_point

    def test_resnapshotting_the_pinned_oid_is_stable(self, behind_base):
        """What a later round and a resume do: re-snapshot against ``base_oid``."""
        first = take_snapshot(behind_base, base_ref="main")
        again = take_snapshot(behind_base, base_ref=first.base_oid, three_dot=False)
        assert again.base_oid == first.base_oid
        assert again.diff_text == first.diff_text

    def test_base_already_an_ancestor_is_unchanged_by_three_dot(self, tmp_path):
        """The common case must be byte-identical, or this PR changed every review."""
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "config", "user.email", "t@t")
        _git(tmp_path, "config", "user.name", "t")
        (tmp_path / "a.py").write_text("one\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", "one")
        (tmp_path / "a.py").write_text("two\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", "two")

        assert (
            take_snapshot(tmp_path, base_ref="HEAD~1").diff_text
            == take_snapshot(tmp_path, base_ref="HEAD~1", three_dot=False).diff_text
        )


class TestUnrelatedHistories:
    @pytest.fixture
    def unrelated(self, tmp_path: Path) -> Path:
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "config", "user.email", "t@t")
        _git(tmp_path, "config", "user.name", "t")
        (tmp_path / "a").write_text("a\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", "a")
        _git(tmp_path, "checkout", "-q", "--orphan", "other")
        _git(tmp_path, "rm", "-rqf", ".")
        (tmp_path / "b").write_text("b\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", "b")
        return tmp_path

    def test_no_common_ancestor_fails_with_an_actionable_message(self, unrelated):
        """There is no branch point, so refuse — but name the way forward."""
        with pytest.raises(SnapshotError) as exc:
            take_snapshot(unrelated, base_ref="main")
        assert "no common ancestor" in str(exc.value)
        assert "--two-dot" in str(exc.value)

    def test_the_escape_hatch_works_there(self, unrelated):
        assert "b" in take_snapshot(unrelated, base_ref="main", three_dot=False).diff_text
