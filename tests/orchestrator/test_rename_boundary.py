"""A boundary-crossing rename must not read as "nothing to review" — PR-h-02c.5 item 1.

`diff_filter` drops a section when EITHER side's basename is a strip target. That is right
when the DESTINATION is one, and wrong when only the SOURCE was: `git mv CLAUDE.md app.py`
drops the section, so the reviewer is never told `app.py` appeared.

Alone that is a blind spot — the file is still in the reviewer's worktree, and a rename
with a large rewrite falls below git's similarity threshold and is emitted as delete+add,
which survives. It becomes a FALSE SHIP in composition with PR-h-02d: if such a rename is
the only change the filtered diff is empty, the run terminates `no_changes_to_review` at
exit 0, and ZERO reviewers are dispatched — so nothing reads that worktree, because nothing
runs. Measured before the fix: `exit=0 reason='no_changes_to_review' dispatched=[]`.

The backstop is deliberately NOT "the unfiltered diff must be empty": that would revert
PR-h-02d's D3, under which a legitimately all-repo-context change SHOULD be known-empty.
`test_repo_context_only_change_is_still_known_empty` is the guard on that, and it is the
test that fails if someone later reaches for the blunter rule.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from syncade.adapters.fake import FakeAdapter
from syncade.exit_codes import SUCCESS
from syncade.logging import Logger
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import _factory_returning, _ship, _two_reviewer_config
from tests.orchestrator.test_empty_diff import _init_repo


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _review(tmp_path: Path, prep):
    """Run a review over whatever ``prep`` commits; return (result, dispatched names)."""
    repo, pr_doc, head = _init_repo(tmp_path)
    base = prep(repo, head)
    dispatched: list[str] = []

    class Tracking(FakeAdapter):
        def build_invocation(self, reviewer_config, worktree_path, prompt):
            dispatched.append(reviewer_config.name)
            return super().build_invocation(reviewer_config, worktree_path, prompt)

    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_two_reviewer_config(),
        logger=Logger("quiet"),
        adapter_factory=_factory_returning(*[Tracking(canned_output=_ship()) for _ in range(2)]),
        base_ref=base,
        worktree_base=tmp_path / "wt",
    )
    return result, dispatched


def _commit_ctx(repo: Path) -> str:
    (repo / "CLAUDE.md").write_text("PROJECT MEMORY\n" * 12)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add context")
    return _git(repo, "rev-parse", "HEAD")


class TestBoundaryRename:
    def test_rename_out_of_context_is_reviewed_not_skipped(self, tmp_path):
        """The false SHIP: a real file appears, and nothing reviews it."""

        def prep(repo, _head):
            base = _commit_ctx(repo)
            _git(repo, "mv", "CLAUDE.md", "app.py")
            _git(repo, "commit", "-qm", "promote notes to code")
            return base

        result, dispatched = _review(tmp_path, prep)
        assert result.termination_reason != "no_changes_to_review", (
            "a boundary rename was reported as nothing to review"
        )
        assert dispatched, "zero reviewers dispatched for a real change"

    def test_repo_context_only_change_is_still_known_empty(self, tmp_path):
        """D3 must survive. This fails if the backstop is widened to 'unfiltered diff
        must be empty', which is the tempting-but-wrong version."""

        def prep(repo, _head):
            base = _commit_ctx(repo)
            (repo / "CLAUDE.md").write_text("EDITED MEMORY\n" * 12)
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "edit context only")
            return base

        result, dispatched = _review(tmp_path, prep)
        assert result.exit_code == SUCCESS
        assert result.termination_reason == "no_changes_to_review"
        assert dispatched == []

    def test_base_equals_head_is_still_known_empty(self, tmp_path):
        result, dispatched = _review(tmp_path, lambda repo, head: head)
        assert result.termination_reason == "no_changes_to_review"
        assert dispatched == []

    @pytest.mark.parametrize(
        "src,dst,expect_reviewed",
        [
            ("CLAUDE.md", "app.py", True),  # context -> code: the bug
            ("app.py", "CLAUDE.md", False),  # code -> context: destination is stripped
            ("CLAUDE.md", "AGENTS.md", False),  # context -> context
        ],
    )
    def test_rename_direction_matrix(self, tmp_path, src, dst, expect_reviewed):
        """Three of four directions are already correct; they are the regression guard."""

        def prep(repo, _head):
            (repo / src).write_text("PAYLOAD\n" * 12)
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "seed")
            base = _git(repo, "rev-parse", "HEAD")
            _git(repo, "mv", src, dst)
            _git(repo, "commit", "-qm", "rename")
            return base

        result, dispatched = _review(tmp_path, prep)
        reviewed = result.termination_reason != "no_changes_to_review"
        assert reviewed is expect_reviewed, (
            f"{src} -> {dst}: reviewed={reviewed}, expected {expect_reviewed} "
            f"(reason={result.termination_reason!r}, dispatched={dispatched})"
        )


class TestBoundaryRenameIsAnnounced:
    """PR-h-02c.5 item 2 (D1(a)): the reviewer is TOLD the destination appeared.

    Item 1 stopped the false SHIP; the concealment itself remained — a boundary rename
    triggered a review but the diff still never mentioned the new file.

    Measured before choosing the shape: the source path occurs in the header, in
    ``rename from``, and in ``--- a/<path>``; and on a rename WITH edits the stripped
    file's own lines appear as hunk CONTEXT. So redacting paths alone still leaks content,
    which is why the body is discarded rather than rewritten.
    """

    @staticmethod
    def _boundary_diff(tmp_path: Path, *, edit: bool) -> str:
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "config", "user.email", "t@t")
        _git(tmp_path, "config", "user.name", "t")
        (tmp_path / "CLAUDE.md").write_text("SECRET memory\n" * 14)
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", "base")
        base = _git(tmp_path, "rev-parse", "HEAD")
        _git(tmp_path, "mv", "CLAUDE.md", "app.py")
        if edit:
            target = tmp_path / "app.py"
            target.write_text(target.read_text() + "NEW CODE\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", "rename")
        return _git(tmp_path, "diff", "-M", f"{base}..HEAD")

    @pytest.mark.parametrize("edit", [False, True], ids=["pure_rename", "rename_with_edit"])
    def test_destination_announced_source_withheld(self, tmp_path, edit):
        from syncade.diff_filter import filter_diff_for_reviewer

        filtered = filter_diff_for_reviewer(
            self._boundary_diff(tmp_path, edit=edit), ["CLAUDE.md", "AGENTS.md"]
        )
        assert "app.py" in filtered, "reviewer is not told the destination appeared"
        assert "CLAUDE.md" not in filtered, "stripped source NAME leaked"
        assert "SECRET" not in filtered, "stripped source CONTENT leaked"

    def test_placeholder_points_at_the_workspace(self, tmp_path):
        """The file IS on disk for the reviewer, so the note must be actionable."""
        from syncade.diff_filter import filter_diff_for_reviewer

        filtered = filter_diff_for_reviewer(
            self._boundary_diff(tmp_path, edit=False), ["CLAUDE.md", "AGENTS.md"]
        )
        assert "workspace" in filtered

    def test_nested_destination_full_path_announced(self, tmp_path):
        """Destination directory components must not be dropped.

        ``git mv CLAUDE.md src/app.py`` — reviewers must see ``src/app.py``, not
        just ``app.py``. Before the fix, ``_decode_header`` returned only basenames
        and ``_redacted_boundary_rename`` emitted ``b/app.py`` / ``rename to app.py``.
        """
        from syncade.diff_filter import concealed_destinations, filter_diff_for_reviewer

        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "config", "user.email", "t@t")
        _git(tmp_path, "config", "user.name", "t")
        (tmp_path / "CLAUDE.md").write_text("SECRET memory\n" * 14)
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / ".gitkeep").write_text("")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", "base")
        base = _git(tmp_path, "rev-parse", "HEAD")
        _git(tmp_path, "mv", "CLAUDE.md", "src/app.py")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", "promote notes into src")
        diff = _git(tmp_path, "diff", "-M", f"{base}..HEAD")

        filtered = filter_diff_for_reviewer(diff, ["CLAUDE.md", "AGENTS.md"])
        assert "src/app.py" in filtered, "nested destination path not announced"
        assert "CLAUDE.md" not in filtered, "stripped source name leaked"
        assert "SECRET" not in filtered, "stripped source content leaked"

        hidden = concealed_destinations(diff, ["CLAUDE.md", "AGENTS.md"])
        assert hidden == ["src/app.py"], f"concealed_destinations returned {hidden!r}"
