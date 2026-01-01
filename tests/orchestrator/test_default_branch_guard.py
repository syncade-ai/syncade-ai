"""PR-v2-26 item 2 — the default-branch commit guard.

Covers the brief's claims C2 (refuse BEFORE dispatch, exempt the right cases) and C3
(enforced at the run-entry choke, so a DIRECT ``run_review`` on the default branch is
guarded — not only the CLI wrapper).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from syncade.orchestrator.branch_guard import guard_default_branch
from syncade.worktree import WorktreeError
from tests.orchestrator._helpers import (
    FakeAdapter,
    _factory_returning,
    _fake_origin_head,
    _ship,
)


def _repo_on(
    tmp_path: Path, branch: str, *, with_main: bool = False, remote_default: str | None = None
) -> Path:
    """A one-commit git repo whose HEAD sits on ``branch``. When ``with_main`` and
    ``branch != 'main'``, a ``main`` branch also exists. When ``remote_default`` is set, an
    authoritative ``origin/HEAD -> <remote_default>`` is faked (no real remote); otherwise
    the repo is local-only (no origin/HEAD)."""
    repo = (tmp_path / "repo").resolve()
    repo.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    (repo / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    if branch == "master":
        subprocess.run(["git", "branch", "-M", "master"], cwd=repo, check=True)
    elif branch != "main":
        if with_main:
            subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=repo, check=True)
        else:
            subprocess.run(["git", "branch", "-M", branch], cwd=repo, check=True)
    if remote_default is not None:
        # The remote-default branch must exist as a ref to point origin/HEAD at.
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", remote_default],
            cwd=repo,
            capture_output=True,
        ).returncode
        if exists != 0:
            subprocess.run(["git", "branch", remote_default], cwd=repo, check=True)
        _fake_origin_head(repo, remote_default)
    return repo


class TestGuardUnit:
    """The pure predicate under PR-v2-26's synthesis policy: origin/HEAD authoritative; else
    a local main/master is the best-effort default (refuse iff HEAD is it or a common
    integration name); else (no main/master at all) refuse. Fresh-dir auto-init is exempted
    upstream via ``allow``, not here."""

    # --- 1. authoritative (origin/HEAD present) ---

    def test_remote_default_branch_refuses(self, tmp_path):
        repo = _repo_on(tmp_path, "main", remote_default="main")
        with pytest.raises(WorktreeError, match="default branch"):
            guard_default_branch(repo, "main", allow=False, will_commit=True)

    def test_remote_uncommon_default_name_refuses(self, tmp_path):
        # origin/HEAD -> release, on release: the authoritative path catches ANY name.
        repo = _repo_on(tmp_path, "release", remote_default="release")
        with pytest.raises(WorktreeError, match="default branch"):
            guard_default_branch(repo, "release", allow=False, will_commit=True)

    def test_remote_default_on_feature_branch_allowed(self, tmp_path):
        repo = _repo_on(tmp_path, "feature", with_main=True, remote_default="main")
        guard_default_branch(repo, "feature", allow=False, will_commit=True)  # no raise

    # --- 2. no remote, local main/master exists ---

    def test_local_main_on_main_refuses(self, tmp_path):
        repo = _repo_on(tmp_path, "main")
        with pytest.raises(WorktreeError, match="looks like this repo's default"):
            guard_default_branch(repo, "main", allow=False, will_commit=True)

    def test_local_feature_beside_main_allowed(self, tmp_path):
        # Round-6 case: a normal local repo (main + feature, no remote yet) on 'feature'
        # must proceed — 'main' is the local default, 'feature' is clearly not it.
        repo = _repo_on(tmp_path, "main")
        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
        guard_default_branch(repo, "feature", allow=False, will_commit=True)  # no raise

    def test_local_trunk_beside_vestigial_main_refuses(self, tmp_path):
        # Round-4 K case: on 'trunk' with a vestigial local 'main'. 'trunk' is a common
        # integration name, so refuse even though it != the local 'main'.
        repo = _repo_on(tmp_path, "trunk")
        subprocess.run(["git", "branch", "main"], cwd=repo, check=True)
        with pytest.raises(WorktreeError, match="looks like this repo's default"):
            guard_default_branch(repo, "trunk", allow=False, will_commit=True)

    # --- 3. no remote AND no local main/master ---

    def test_local_only_uncommon_default_refuses(self, tmp_path):
        # Round-5 case: a local-only 'release' with no main/master — HEAD is the only
        # integration branch, so refuse.
        repo = _repo_on(tmp_path, "release")
        with pytest.raises(WorktreeError, match="no local main/master"):
            guard_default_branch(repo, "release", allow=False, will_commit=True)

    # --- exemptions ---

    def test_allow_flag_overrides(self, tmp_path):
        repo = _repo_on(tmp_path, "main")
        guard_default_branch(repo, "main", allow=True, will_commit=True)  # no raise

    def test_single_pass_is_exempt(self, tmp_path):
        repo = _repo_on(tmp_path, "main", remote_default="main")
        guard_default_branch(repo, "main", allow=False, will_commit=False)  # no raise

    def test_detached_head_is_exempt(self, tmp_path):
        repo = _repo_on(tmp_path, "main")
        guard_default_branch(repo, None, allow=False, will_commit=True)  # no raise


class TestGuardViaRunReview:
    """C3: the guard fires from a DIRECT run_review call, before any dispatch."""

    def _config(self):
        return SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 2},
        )

    def _run(self, repo, pr_doc, **kw):
        return run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(),
            adapter_factory=_factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship())),
            logger=Logger(level="quiet"),
            **kw,
        )

    def test_direct_call_on_default_branch_refuses(self, tmp_path):
        repo = _repo_on(tmp_path, "main", remote_default="main")
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")
        with pytest.raises(WorktreeError, match="default branch"):
            self._run(repo, pr_doc)

    def test_allow_default_branch_lets_it_proceed(self, tmp_path):
        repo = _repo_on(tmp_path, "main")
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")
        # SHIP round 0 → no producer, clean exit; the point is the guard did NOT raise.
        result = self._run(repo, pr_doc, allow_default_branch=True)
        assert result.exit_code == 0

    def test_feature_branch_announces_target_and_runs(self, tmp_path, capsys):
        repo = _repo_on(tmp_path, "feature", with_main=True, remote_default="main")
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(),
            adapter_factory=_factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship())),
            logger=Logger(level="normal"),
        )
        assert result.exit_code == 0
        assert "commits will land on: feature" in capsys.readouterr().err

    def test_announcement_survives_quiet(self, tmp_path, capsys):
        # The branch target is a safety disclosure — it must print even under --quiet
        # (like the auth block), so a `--quiet --allow-default-branch` run still says where
        # commits go.
        repo = _repo_on(tmp_path, "feature", with_main=True, remote_default="main")
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(),
            adapter_factory=_factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship())),
            logger=Logger(level="quiet"),
        )
        assert "commits will land on: feature" in capsys.readouterr().err


class TestCliRefusesBeforeAuthProbe:
    """The CLI must refuse the default branch BEFORE auth_gate probes `codex login status`
    (a subprocess). Otherwise a zero-config run on `main` spawns codex and prints auth
    output before the refusal."""

    def test_default_branch_refused_without_probing_codex(self, tmp_path, monkeypatch):
        import syncade.auth_preflight as ap
        from syncade.cli import main

        repo = _repo_on(tmp_path, "main", remote_default="main")
        (repo / "brief.md").write_text("# B\n")
        monkeypatch.chdir(repo)

        def _boom(*a, **k):
            raise AssertionError("codex was probed before the default-branch refusal")

        monkeypatch.setattr(ap, "probe_codex_state", _boom)
        rc = main(["brief.md", "--max-rounds", "2", "--base", "HEAD"])
        assert rc == 60  # refused, and _boom was never hit

    def test_multiround_resume_on_main_refused_despite_config_drift(self, tmp_path, monkeypatch):
        """A run launched at max_rounds=3, resumed with config drifted to 1, still commits
        (effective = max(1, plan=3)). Resume on `main` must refuse — BEFORE the codex probe —
        using the plan's cap, not the drifted config."""
        import syncade.auth_preflight as ap
        from syncade.cli import main
        from tests.orchestrator._resume_fixtures import _prepare_aborted_run

        repo = _repo_on(tmp_path, "main", remote_default="main")
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")
        (repo / ".syncade").mkdir()
        (repo / ".syncade" / "config.toml").write_text("[loop]\nmax_rounds = 1\n")
        _prepare_aborted_run(
            repo, pr_doc, completed_round_count=0, max_rounds=3, aborted_exit_code=40
        )
        monkeypatch.chdir(repo)

        def _boom(*a, **k):
            raise AssertionError("codex was probed before the resume default-branch refusal")

        monkeypatch.setattr(ap, "probe_codex_state", _boom)
        assert main(["--resume", "latest"]) == 60

    def test_single_pass_resume_on_main_is_not_refused(self, tmp_path, monkeypatch):
        """A resumable single-pass run (effective max_rounds == 1) commits nothing, so a
        resume on `main` must NOT be refused by the guard (it should reach auth_gate)."""
        import syncade.cli.resume_mode as rm
        from syncade.cli import main
        from tests.orchestrator._resume_fixtures import _prepare_aborted_run

        repo = _repo_on(tmp_path, "main")
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")
        (repo / ".syncade").mkdir()
        (repo / ".syncade" / "config.toml").write_text("[loop]\nmax_rounds = 1\n")
        _prepare_aborted_run(
            repo, pr_doc, completed_round_count=0, max_rounds=1, aborted_exit_code=40
        )
        monkeypatch.chdir(repo)
        # Sentinel: reaching auth_gate means the guard did NOT preempt the single-pass resume.
        monkeypatch.setattr(rm, "auth_gate", lambda *a, **k: 42)
        assert main(["--resume", "latest"]) == 42


class TestAutoInitExemption:
    """A repo syncade itself auto-creates (fresh non-git dir) is exempt from the guard —
    there is no pre-existing integration branch to protect (PR-v2-26 round 5 decision)."""

    def test_fresh_dir_autoinit_is_not_refused(self, tmp_path, monkeypatch):
        import syncade.auth_preflight as ap
        import syncade.cli as cli

        (tmp_path / "brief.md").write_text("# B\n")
        monkeypatch.chdir(tmp_path)
        for k, v in (
            ("GIT_AUTHOR_NAME", "t"),
            ("GIT_AUTHOR_EMAIL", "t@e.com"),
            ("GIT_COMMITTER_NAME", "t"),
            ("GIT_COMMITTER_EMAIL", "t@e.com"),
        ):
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(ap, "probe_codex_state", lambda *a, **k: ("subscription", ""))
        reached = []
        monkeypatch.setattr(
            cli,
            "run_review",
            lambda *a, **k: reached.append(1) or type("R", (), {"exit_code": 0})(),
        )
        cli.main(["brief.md", "--max-rounds", "2", "--base", "HEAD"])
        assert reached, "auto-inited fresh dir was refused by the guard instead of proceeding"
