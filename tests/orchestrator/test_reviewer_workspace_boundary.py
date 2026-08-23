"""End-to-end boundary tests for Git-less reviewer workspaces."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.config import SyncadeConfig
from syncade.orchestrator import run_review
from syncade.snapshot import take_snapshot
from tests.orchestrator._helpers import _factory_returning, _init_git_repo, _ship


class _BoundaryObserver(FakeAdapter):
    def __init__(self) -> None:
        super().__init__(canned_output=_ship())
        self.observations: list[dict[str, object]] = []

    def build_invocation(self, reviewer_config, worktree_path: Path, prompt: str):
        git_probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        files = {
            str(path.relative_to(worktree_path)): path.read_bytes()
            for path in worktree_path.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        self.observations.append(
            {
                "has_git": os.path.lexists(worktree_path / ".git"),
                "git_returncode": git_probe.returncode,
                "files": files,
                "prompt": prompt,
            }
        )
        return super().build_invocation(reviewer_config, worktree_path, prompt)


def _repo_with_change(tmp_path: Path) -> Path:
    repo = (tmp_path / "repo").resolve()
    _init_git_repo(repo, files={"source.txt": "base\n"})
    (repo / "source.txt").write_text("review this exact diff\n")
    subprocess.run(["git", "add", "source.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "work change"], cwd=repo, check=True)
    return repo


def _config(**kwargs) -> SyncadeConfig:
    return SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_rounds": 1, **kwargs.pop("loop", {})},
        **kwargs,
    )


@pytest.mark.parametrize("location", ["tracked", "untracked", "outside"])
def test_pr_doc_inputs_and_diff_survive_gitless_export(tmp_path: Path, location: str) -> None:
    repo = _repo_with_change(tmp_path)
    if location == "outside":
        pr_doc = tmp_path / "brief.md"
    else:
        pr_doc = repo / "docs" / "brief.md"
        pr_doc.parent.mkdir()
    pr_doc.write_text(f"authoritative {location} brief\n")
    if location == "tracked":
        subprocess.run(["git", "add", "docs/brief.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "track brief"], cwd=repo, check=True)

    expected_diff = take_snapshot(repo, base_ref="main").diff_text
    observers = [_BoundaryObserver(), _BoundaryObserver()]
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_config(),
        base_ref="main",
        adapter_factory=_factory_returning(*observers),
        synthesizer_adapter=FakeSynthesizerAdapter(),
    )

    assert result.exit_code == 0
    for observer in observers:
        observation = observer.observations[0]
        assert observation["has_git"] is False
        assert observation["git_returncode"] != 0
        assert expected_diff in observation["prompt"]
        if location == "outside":
            digest = hashlib.sha256(str(pr_doc).encode()).hexdigest()[:16]
            ref = f".syncade-inputs/pr-doc-{digest}-brief.md"
        else:
            ref = "docs/brief.md"
        assert ref in observation["prompt"]
        assert observation["files"][ref] == pr_doc.read_bytes()


def test_tracked_stripped_brief_is_relocated_without_restoring_original(tmp_path: Path) -> None:
    repo = _repo_with_change(tmp_path)
    pr_doc = repo / "CLAUDE.md"
    pr_doc.write_text("authoritative but stripped\n")
    subprocess.run(["git", "add", "CLAUDE.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "track stripped brief"], cwd=repo, check=True)
    digest = hashlib.sha256(str(pr_doc).encode()).hexdigest()[:16]
    reserved = f".syncade-inputs/pr-doc-{digest}-CLAUDE.md"
    observers = [_BoundaryObserver(), _BoundaryObserver()]

    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_config(),
        base_ref="main",
        adapter_factory=_factory_returning(*observers),
        synthesizer_adapter=FakeSynthesizerAdapter(),
    )

    assert result.exit_code == 0
    for observer in observers:
        observation = observer.observations[0]
        assert "CLAUDE.md" not in observation["files"]
        assert observation["files"][reserved] == pr_doc.read_bytes()
        assert reserved in observation["prompt"]


def test_tracked_brief_staging_uses_authoritative_current_bytes(tmp_path: Path) -> None:
    repo = _repo_with_change(tmp_path)
    pr_doc = repo / "brief.md"
    pr_doc.write_text("committed brief\n")
    subprocess.run(["git", "add", "brief.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "track brief"], cwd=repo, check=True)
    pr_doc.write_text("operator-selected current brief\n")
    observers = [_BoundaryObserver(), _BoundaryObserver()]

    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_config(),
        base_ref="main",
        adapter_factory=_factory_returning(*observers),
        synthesizer_adapter=FakeSynthesizerAdapter(),
    )

    assert result.exit_code == 0
    for observer in observers:
        assert observer.observations[0]["files"]["brief.md"] == pr_doc.read_bytes()


def test_trusted_test_and_check_legs_remain_git_worktrees(tmp_path: Path) -> None:
    repo = _repo_with_change(tmp_path)
    pr_doc = tmp_path / "brief.md"
    pr_doc.write_text("brief\n")
    observers = [_BoundaryObserver(), _BoundaryObserver()]
    git_probe = "test -e .git && git rev-parse --is-inside-work-tree"

    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_config(
            loop={"test_command": git_probe},
            checks=[{"name": "git-check", "command": git_probe, "severity": "blocking"}],
        ),
        base_ref="main",
        adapter_factory=_factory_returning(*observers),
        synthesizer_adapter=FakeSynthesizerAdapter(),
    )

    assert result.exit_code == 0
    assert all(item["has_git"] is False for obs in observers for item in obs.observations)
    assert result.test_result is not None
    assert result.test_result.outcome == "passed"
    assert result.rounds[-1].check_results[0].outcome == "passed"
