"""Orchestrator soft dirty-tree note tests for :mod:`syncade.snapshot`.

Split from the original ``tests/test_snapshot.py`` — covers the
end-to-end orchestrator-warning thread (snapshot dirty-state →
user-facing soft note with the untracked count). Uses real ``git``
subprocess calls against an ephemeral repo under ``tmp_path`` — no
mocking.
"""

from __future__ import annotations

import shutil

import pytest

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not found on PATH",
)


class TestSoftNoteCountInOrchestratorWarning:
    """T2.10: the orchestrator's soft dirty-tree note includes
    the untracked count in the user-facing message. Test against
    the real orchestrator path so we exercise the snapshot →
    warning thread end-to-end."""

    def _drive(self, repo_path, *, untracked_files):
        """Init a tiny repo, plant untracked files, run the
        orchestrator with FakeAdapter + FakeSynth, capture stderr."""
        import io
        import subprocess
        import sys

        from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
        from syncade.config import SyncadeConfig
        from syncade.findings import ReviewerOutput
        from syncade.logging import Logger
        from syncade.orchestrator import run_review
        from syncade.synthesis import SynthesizerOutput

        repo_path.mkdir(parents=True, exist_ok=True)
        for cmd in (
            ["init", "-q"],
            ["config", "user.email", "t@e.com"],
            ["config", "user.name", "T"],
            ["config", "commit.gpgsign", "false"],
        ):
            subprocess.run(["git", *cmd], cwd=repo_path, check=True)
        (repo_path / "README.md").write_text("repo\n")
        subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_path, check=True)
        for f in untracked_files:
            (repo_path / f).write_text("scratch\n")

        pr_doc = repo_path.parent / "pr.md"
        pr_doc.write_text("# x\n")
        ship = ReviewerOutput(
            verdict="SHIP",
            findings=[],
            summary="x",
            priority_order=[],
            coverage_gaps=[],
            dismissed_concerns=[],
        )

        def factory(_p):
            return FakeAdapter(canned_output=ship)

        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            run_review(
                repo_root=repo_path,
                pr_doc_path=pr_doc,
                config=SyncadeConfig(
                    reviewers=[
                        {"name": "rv1", "provider": "fake1", "model": "x"},
                        {"name": "rv2", "provider": "fake2", "model": "y"},
                    ],
                    # PR-8: single-pass back-compat — preserve PR-7.5
                    # warning-only dirty-tree behavior. (Loop mode
                    # would refuse with exit 60 instead of warning.)
                    loop={"max_rounds": 1},
                ),
                logger=Logger("normal"),
                adapter_factory=factory,
                synthesizer_adapter=FakeSynthesizerAdapter(
                    canned_output=SynthesizerOutput(consolidated_findings=[], synthesis_summary="x")
                ),
            )
        finally:
            sys.stderr = old_stderr
        return captured.getvalue().lower()

    def test_untracked_only_message_includes_count(self, tmp_path):
        stderr = self._drive(tmp_path / "repo", untracked_files=["a.txt", "b.txt", "c.txt"])
        assert "untracked files (not reviewed): 3 files" in stderr, (
            f"soft note missing the explicit count; got:\n{stderr}"
        )

    def test_untracked_singular_uses_file_not_files(self, tmp_path):
        """One file → singular 'file'. English-language nicety."""
        stderr = self._drive(tmp_path / "repo", untracked_files=["only-one.txt"])
        assert "1 file." in stderr or "1 file " in stderr, (
            f"soft note must use singular 'file' for count=1; got:\n{stderr}"
        )

    def test_both_state_message_includes_count(self, tmp_path):
        """In 'both' state the soft note still appears (after the
        strong warning) and includes the untracked count."""
        # Need to plant a tracked modification PLUS untracked
        # files. Build the repo by hand here so we control state.
        import io
        import subprocess
        import sys

        from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
        from syncade.config import SyncadeConfig
        from syncade.findings import ReviewerOutput
        from syncade.logging import Logger
        from syncade.orchestrator import run_review
        from syncade.synthesis import SynthesizerOutput

        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in (
            ["init", "-q"],
            ["config", "user.email", "t@e.com"],
            ["config", "user.name", "T"],
            ["config", "commit.gpgsign", "false"],
        ):
            subprocess.run(["git", *cmd], cwd=repo, check=True)
        (repo / "README.md").write_text("repo\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        (repo / "README.md").write_text("tracked-modification\n")  # tracked-modified
        (repo / "u1.txt").write_text("u\n")
        (repo / "u2.txt").write_text("u\n")

        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# x\n")
        ship = ReviewerOutput(
            verdict="SHIP",
            findings=[],
            summary="x",
            priority_order=[],
            coverage_gaps=[],
            dismissed_concerns=[],
        )

        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=SyncadeConfig(
                    reviewers=[
                        {"name": "rv1", "provider": "fake1", "model": "x"},
                        {"name": "rv2", "provider": "fake2", "model": "y"},
                    ],
                    # PR-8: single-pass back-compat for warning-only
                    # dirty-tree behavior.
                    loop={"max_rounds": 1},
                ),
                logger=Logger("normal"),
                adapter_factory=lambda _p: FakeAdapter(canned_output=ship),
                synthesizer_adapter=FakeSynthesizerAdapter(
                    canned_output=SynthesizerOutput(consolidated_findings=[], synthesis_summary="x")
                ),
            )
        finally:
            sys.stderr = old_stderr
        stderr = captured.getvalue().lower()
        # Strong warning present
        assert "uncommitted modifications to tracked" in stderr
        # Soft note present with count
        assert "untracked files (not reviewed): 2 files" in stderr
