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
        assert "producer target branch: feature" in capsys.readouterr().err

    def test_announcement_survives_quiet(self, tmp_path, capsys):
        # The branch target is a safety disclosure — it must print even under --quiet
        # (like the auth block), so a `--quiet --allow-default-branch` run still says where
        # producer work is targeted.
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
        assert "producer target branch: feature" in capsys.readouterr().err


class TestCliRefusesBeforeAuthProbe:
    """The CLI must refuse a committing default-branch run (exit 60). Under D1(c)
    (PR-h-02d.5), a baseless run is refused BEFORE auth; a based run defers the guard
    to run_review so auth is probed first — the invariant is refusal, not ordering.
    A no-change run on the default branch must NOT be refused; it exits 0 with
    no_changes_to_review instead."""

    def test_no_change_on_default_branch_exits_zero_not_refused(self, tmp_path, monkeypatch):
        """Regression: known-empty run (--base HEAD) on main must exit 0 not 60.

        The commit guard is moot when no producer runs; refusing the run before
        loop.py can classify the diff as no-change is the ordering bug this test pins.
        """
        import syncade.auth_preflight as ap
        from syncade.cli import main

        repo = _repo_on(tmp_path, "main", remote_default="main")
        (repo / "brief.md").write_text("# B\n")
        monkeypatch.chdir(repo)
        # Auth probe must succeed so the run reaches run_review (not refused by auth).
        monkeypatch.setattr(ap, "probe_codex_state", lambda: ("subscription", ""))
        rc = main(["brief.md", "--max-rounds", "2", "--base", "HEAD"])
        assert rc == 0, f"no-change run on default branch should exit 0, got {rc}"

    def test_committing_run_on_default_branch_refused(self, tmp_path, monkeypatch):
        """A run with actual changes (base != HEAD) on the default branch must be refused.

        Under D1(c) (PR-h-02d.5), based diffs defer the commit guard to run_review, so
        auth is probed first. The invariant is that the run is still refused (exit 60),
        not that refusal happens before auth.
        """
        import syncade.auth_preflight as ap
        from syncade.cli import main

        repo = _repo_on(tmp_path, "main", remote_default="main")
        # Add a second commit so HEAD is ahead of the initial commit.
        (repo / "brief.md").write_text("# B\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add brief"], cwd=repo, check=True)
        # base = parent of HEAD → diff is non-empty → run_review refuses on default branch.
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        monkeypatch.chdir(repo)
        # Auth must proceed so run_review is reached; its guard refuses the committing run.
        monkeypatch.setattr(ap, "probe_codex_state", lambda: ("subscription", ""))
        rc = main(["brief.md", "--max-rounds", "2", "--base", base_sha])
        assert rc == 60, f"committing run on default branch should be refused (exit 60), got {rc}"

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

    def test_resume_whose_pinned_base_filters_empty_is_not_refused(self, tmp_path, monkeypatch):
        """PR-h-02d.5 item 3: the resume guard used literal `base_oid == HEAD` equality, so a
        resumed run whose pinned base is NOT HEAD but whose diff filters to nothing was
        refused before auth — while `run_review` classifies it `no_changes_to_review` and
        exits 0. That divergence is the bug; the guard must defer when it cannot prove the
        run commits."""
        import subprocess

        import syncade.auth_preflight as ap
        from syncade.cli import main
        from tests.orchestrator._resume_fixtures import _prepare_aborted_run

        repo = _repo_on(tmp_path, "main", remote_default="main")
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        # Only repo-context changed since the pinned base: the filtered diff is empty, so
        # no producer can run — but base_oid != HEAD, which the old equality check missed.
        (repo / "CLAUDE.md").write_text("project memory\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "context only"], cwd=repo, check=True)

        _prepare_aborted_run(
            repo,
            pr_doc,
            completed_round_count=0,
            max_rounds=3,
            aborted_exit_code=40,
            base_oid=base,
        )
        monkeypatch.chdir(repo)
        # Auth must succeed so the run reaches run_review — the assertion is that the
        # guard did not refuse first, not that a full review completes.
        monkeypatch.setattr(ap, "probe_codex_state", lambda: ("subscription", ""))

        rc = main(["--resume", "latest"])
        assert rc != 60, (
            "resume refused a run whose diff filters empty; run_review would classify it "
            "no_changes_to_review and exit 0"
        )

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

    def test_scope_resolves_to_head_not_refused(self, tmp_path, monkeypatch):
        """Regression: --scope since-last-review resolving to HEAD must exit 0, not 60.

        Scope resolution happened AFTER the guard in the old code, so the guard saw
        will_commit=True even though the diff would be empty and no producer would run.
        """
        import json

        import syncade.auth_preflight as ap
        from syncade.cli import main

        repo = _repo_on(tmp_path, "main", remote_default="main")
        (repo / "brief.md").write_text("# B\n")
        # Record the CURRENT HEAD as last-reviewed so --scope since-last-review resolves to HEAD.
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        syncade_dir = repo / ".syncade"
        syncade_dir.mkdir(exist_ok=True)
        (syncade_dir / "last-reviewed.json").write_text(
            json.dumps({"main": {"sha": head_sha, "run_id": "t", "recorded_at_utc": "2026-08-03"}})
        )
        monkeypatch.chdir(repo)
        monkeypatch.setattr(ap, "probe_codex_state", lambda: ("subscription", ""))
        rc = main(["brief.md", "--max-rounds", "2", "--scope", "since-last-review"])
        assert rc == 0, f"no-change scope run on default branch should exit 0, got {rc}"

    def test_annotated_tag_base_at_head_not_refused(self, tmp_path, monkeypatch):
        """Regression: --base pointing to an annotated tag at HEAD must exit 0, not 60.

        The old code used `git rev-parse <tag>` which returns the tag object SHA, not
        the commit SHA — so it compared a tag object to HEAD's commit and got False.
        """
        import syncade.auth_preflight as ap
        from syncade.cli import main

        repo = _repo_on(tmp_path, "main", remote_default="main")
        (repo / "brief.md").write_text("# B\n")
        # Create an annotated tag at current HEAD.
        subprocess.run(["git", "tag", "-a", "v1.0", "-m", "release"], cwd=repo, check=True)
        monkeypatch.chdir(repo)
        monkeypatch.setattr(ap, "probe_codex_state", lambda: ("subscription", ""))
        rc = main(["brief.md", "--max-rounds", "2", "--base", "v1.0"])
        assert rc == 0, f"annotated-tag base at HEAD on default branch should exit 0, got {rc}"

    def test_three_dot_empty_diff_not_refused(self, tmp_path, monkeypatch):
        """Regression: --base pointing to a descendant branch must exit 0, not 60.

        Three-dot diff is empty when merge-base(HEAD, base) == HEAD (HEAD is an
        ancestor of base). The old code only checked literal ref equality, missing this.
        """
        import syncade.auth_preflight as ap
        from syncade.cli import main

        # Create a repo where main is ahead of feature: main has an extra commit.
        repo = _repo_on(tmp_path, "feature", with_main=True, remote_default="feature")
        # Advance main past feature's HEAD (use specific file, not -A, to avoid capturing brief.md).
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
        (repo / "extra.txt").write_text("extra\n")
        subprocess.run(["git", "add", "extra.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "advance main"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "-q", "feature"], cwd=repo, check=True)
        # Write brief.md AFTER returning to feature so it isn't accidentally committed to main.
        (repo / "brief.md").write_text("# B\n")
        # Now HEAD is at feature; --base main → three-dot diff = merge-base(feature, main) =
        # feature's HEAD → empty diff → no producer → guard should not fire.
        monkeypatch.chdir(repo)
        monkeypatch.setattr(ap, "probe_codex_state", lambda: ("subscription", ""))
        rc = main(["brief.md", "--max-rounds", "2", "--base", "main"])
        assert rc == 0, f"three-dot empty diff on default branch should exit 0, got {rc}"

    def test_resume_base_oid_at_head_not_refused(self, tmp_path, monkeypatch):
        """Regression: a resumed run whose pinned base_oid equals HEAD must exit 0, not 60.

        If base_oid == HEAD, the diff is empty; no producer runs. The old code only
        checked effective_max_rounds, so it refused the guard even for no-change resumes.
        """
        import syncade.cli.resume_mode as rm
        from syncade.cli import main
        from tests.orchestrator._resume_fixtures import _prepare_aborted_run

        repo = _repo_on(tmp_path, "main", remote_default="main")
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        (repo / ".syncade").mkdir()
        (repo / ".syncade" / "config.toml").write_text("[loop]\nmax_rounds = 3\n")
        _prepare_aborted_run(
            repo,
            pr_doc,
            completed_round_count=0,
            max_rounds=3,
            aborted_exit_code=40,
            base_oid=head_sha,
        )
        monkeypatch.chdir(repo)
        # Sentinel: reaching auth_gate means the guard did NOT preempt the resume.
        monkeypatch.setattr(rm, "auth_gate", lambda *a, **k: 42)
        assert main(["--resume", "latest"]) == 42, (
            "no-change resume (base_oid==HEAD) on default branch should reach "
            "auth_gate, not exit 60"
        )

    def test_two_dot_descendant_base_commits_are_guarded(self, tmp_path, monkeypatch):
        """--two-dot with a base that is a descendant of HEAD must still be refused on main.

        The two-dot diff (base..HEAD) is non-empty when base descends from HEAD.
        Under D1(c), based diffs defer to run_review, which refuses the committing run.
        """
        import syncade.auth_preflight as ap
        from syncade.cli import main

        # Repo on main (the default branch). Create a feature branch AHEAD of main.
        repo = _repo_on(tmp_path, "main", remote_default="main")
        (repo / "brief.md").write_text("# B\n")
        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
        (repo / "extra.txt").write_text("extra\n")
        subprocess.run(["git", "add", "extra.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "advance feature"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
        monkeypatch.chdir(repo)
        # Auth must proceed; run_review's guard refuses the committing run.
        monkeypatch.setattr(ap, "probe_codex_state", lambda: ("subscription", ""))
        # --two-dot --base feature: the two-dot diff is non-empty (feature descends from main).
        rc = main(["brief.md", "--max-rounds", "2", "--two-dot", "--base", "feature"])
        assert rc == 60, (
            f"--two-dot committing run on default branch should be refused (exit 60), got {rc}"
        )

    def test_resume_descendant_base_oid_is_not_exempted(self, tmp_path, monkeypatch):
        """Resumed run with plan.base_oid pointing to a descendant of HEAD must still be refused.

        For two-dot runs, plan.base_oid is the original --base SHA (not a merge-base).
        Under D1(c), the pre-auth guard defers to run_review for any based diff; run_review
        refuses when the two-dot diff (feature_sha..HEAD) is non-empty.
        """
        import syncade.auth_preflight as ap
        from syncade.cli import main
        from tests.orchestrator._resume_fixtures import _prepare_aborted_run

        repo = _repo_on(tmp_path, "main", remote_default="main")
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")
        (repo / ".syncade").mkdir()
        (repo / ".syncade" / "config.toml").write_text("[loop]\nmax_rounds = 3\n")
        # Create a commit on a feature branch: feature_sha is a descendant of main (HEAD).
        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
        (repo / "extra.txt").write_text("extra\n")
        subprocess.run(["git", "add", "extra.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "advance feature"], cwd=repo, check=True)
        feature_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
        # Stage a resume with base_oid = feature_sha (a descendant of HEAD/main).
        _prepare_aborted_run(
            repo,
            pr_doc,
            completed_round_count=0,
            max_rounds=3,
            aborted_exit_code=40,
            base_oid=feature_sha,
        )
        monkeypatch.chdir(repo)
        # Auth must proceed; run_review's guard refuses the committing run.
        monkeypatch.setattr(ap, "probe_codex_state", lambda: ("subscription", ""))
        rc = main(["--resume", "latest"])
        assert rc == 60, (
            f"resumed run with descendant base_oid on default branch should be refused "
            f"(exit 60), got {rc}"
        )


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
            lambda *a, **k: (
                reached.append(1)
                or type(
                    "R",
                    (),
                    {
                        "exit_code": 0,
                        "dispatch_result": type("D", (), {"results": [object()]})(),
                        "rounds": [
                            type(
                                "Rnd",
                                (),
                                {
                                    "dispatch_result": type(
                                        "D2", (), {"reviewer_subprocess_started": True}
                                    )(),
                                },
                            )()
                        ],
                    },
                )()
            ),
        )
        # `--allow-auto-init`: PR-h-04 item B refuses auto-init in a POPULATED directory
        # by default (a baseline commit there captures whatever it finds). This fixture
        # writes a brief into the dir, so the flag is setup, not the thing under test.
        cli.main(["brief.md", "--max-rounds", "2", "--base", "HEAD", "--allow-auto-init"])
        assert reached, "auto-inited fresh dir was refused by the guard instead of proceeding"
