"""The CLI must not refuse a run that ``run_review`` accepts — PR-h-02d.5.

``guard_default_branch`` is enforced twice on purpose: once at the run-entry choke, and
once in the CLI *before* the auth probe so a doomed run does not pay for it. PR-h-02d then
introduced runs that reach exit 0 WITHOUT committing, and ``run_review`` classifies those
correctly via the authoritative rule. The CLI cannot use that rule where it runs — it has
not snapshotted, resolved ``--scope``, or loaded the strip list — so it substitutes a
cheaper predicate, and every cheaper predicate tried so far is wrong for some input.

This file asserts the INVARIANT rather than any particular predicate, so it survives
whichever of the brief's designs (a)–(d) is chosen: **for a given repository, the CLI must
not refuse what the library accepts.**

Both halves are asserted on the SAME repo, because either alone proves nothing — "the CLI
refuses" is only a bug if the library would have proceeded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import syncade.cli as cli_module
from syncade.adapters.fake import FakeAdapter
from syncade.exit_codes import SUCCESS
from syncade.logging import Logger
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import _factory_returning, _ship, _two_reviewer_config


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo_on_default_branch(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    (repo / "pr.md").write_text("# PR\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pr doc")
    return repo


def _make_no_change(repo: Path, shape: str) -> str:
    """Commit a change that resolves to "nothing to review"; return the base SHA."""
    base = _git(repo, "rev-parse", "HEAD")
    if shape == "empty_commit_identical_tree":
        _git(repo, "commit", "-q", "--allow-empty", "-m", "empty")
    elif shape == "repo_context_only":
        (repo / "CLAUDE.md").write_text("project memory\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "context only")
    else:  # pragma: no cover - guard against a typo in the parametrize list
        raise AssertionError(shape)
    return base


_SHAPES = ["empty_commit_identical_tree", "repo_context_only"]


@pytest.mark.parametrize("shape", _SHAPES)
def test_library_accepts_the_no_change_run(repo_on_default_branch, shape):
    """Half one. Without this, "the CLI refuses" would not be a divergence."""
    base = _make_no_change(repo_on_default_branch, shape)
    dispatched: list[str] = []

    class Tracking(FakeAdapter):
        def build_invocation(self, reviewer_config, worktree_path, prompt):
            dispatched.append(reviewer_config.name)
            return super().build_invocation(reviewer_config, worktree_path, prompt)

    result = run_review(
        repo_root=repo_on_default_branch,
        pr_doc_path=repo_on_default_branch / "pr.md",
        config=_two_reviewer_config(),
        logger=Logger("quiet"),
        adapter_factory=_factory_returning(*[Tracking(canned_output=_ship()) for _ in range(2)]),
        base_ref=base,
        worktree_base=repo_on_default_branch.parent / "wt",
    )
    assert result.exit_code == SUCCESS
    assert result.termination_reason == "no_changes_to_review"
    assert dispatched == []


@pytest.mark.parametrize("shape", _SHAPES)
def test_cli_reaches_run_review_for_the_same_repo(repo_on_default_branch, shape, monkeypatch):
    """Half two, and the one that fails today.

    Stubs ``run_review`` at the site ``cli`` actually calls (per the decomposition rule)
    so the assertion is "did the CLI let us through", not "did a review succeed". No
    provider work happens either way.
    """
    base = _make_no_change(repo_on_default_branch, shape)
    reached: list[bool] = []

    def _stub(**kwargs):
        reached.append(True)
        raise SystemExit(0)

    monkeypatch.setattr(cli_module, "run_review", _stub)

    try:
        cli_module.main(
            [
                "--repo-root",
                str(repo_on_default_branch),
                "--base",
                base,
                "--max-rounds",
                "3",
                str(repo_on_default_branch / "pr.md"),
            ]
        )
    except SystemExit:
        pass

    assert reached, (
        "the CLI refused before reaching run_review, which classifies this same repo as "
        "no_changes_to_review with zero dispatches"
    )


def test_cli_proves_commit_defers_for_non_ascii_strip_file(repo_on_default_branch):
    """A C-quoted non-ASCII strip-file path must not produce a false proof of commit.

    ``git diff --name-only`` C-quotes non-ASCII paths (e.g. ``naïve.md`` becomes
    ``"na\\303\\257ve.md"``). The old basename match compared this quoted form against the
    decoded strip-list entry and always missed, returning True — a false proof that the
    run commits — while ``run_review`` classifies it ``no_changes_to_review``.
    """
    from syncade.cli.resolve import _cli_proves_commit

    base = _git(repo_on_default_branch, "rev-parse", "HEAD")
    # naïve.md is non-ASCII; git --name-only will C-quote it in the old code path.
    (repo_on_default_branch / "naïve.md").write_text("context\n")
    _git(repo_on_default_branch, "add", "-A")
    _git(repo_on_default_branch, "commit", "-qm", "non-ascii strip file")

    # naïve.md is in the strip list → run_review returns no_changes_to_review.
    # The CLI must defer (return False), not prove commit (return True).
    assert _cli_proves_commit(repo_on_default_branch, base, ["naïve.md"]) is False
