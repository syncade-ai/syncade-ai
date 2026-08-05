"""Pre-dispatch empty-diff terminal decisions — PR-h-02d item 3.

Four acceptance claims (from the brief):

1. Zero subprocesses on a known-empty run — asserted on dispatch count.
2. Absent base still dispatches normally — base=None is unaffected.
3. Fail-closed drop refuses (exit 60), never exits 0.
4. 3a vs 3b are distinguished — context-only and unreadable both filter to
   empty diff_text, but only unreadable triggers the refusal.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from syncade.adapters.fake import FakeAdapter, FakeProducerAdapter
from syncade.config import SyncadeConfig
from syncade.exit_codes import SUCCESS, WORKTREE_ERROR
from syncade.logging import Logger
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import (
    _factory_returning,
    _init_git_repo,
    _no_ship,
    _RoundCyclingSynth,
    _ship,
    _synth_with_blocker,
    _two_reviewer_config,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def _init_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    """Fresh git repo on a 'work' branch with an initial commit and a PR doc."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    # work branch + faked origin/HEAD so the default-branch guard is satisfied
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=repo, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", sha], cwd=repo, check=True)
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=repo,
        check=True,
    )
    pr_doc = repo / "pr.md"
    pr_doc.write_text("# PR\n")
    subprocess.run(["git", "add", "pr.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add pr doc"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, pr_doc, head


class TestKnownEmptyDiff:
    """D1: base resolves to HEAD — diff is empty — no reviewers dispatched."""

    def test_zero_reviewers_dispatched_on_known_empty_diff(self, tmp_path):
        """Claim 1: dispatch count = 0 on a known-empty run."""
        repo, pr_doc, head = _init_repo(tmp_path)
        dispatched: list[str] = []

        class TrackingAdapter(FakeAdapter):
            def build_invocation(self, reviewer_config, worktree_path, prompt):
                dispatched.append(reviewer_config.name)
                return super().build_invocation(reviewer_config, worktree_path, prompt)

        adapters = [TrackingAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            logger=Logger("normal"),
            adapter_factory=_factory_returning(*adapters),
            base_ref=head,  # base == HEAD → empty diff
            worktree_base=tmp_path / "wt",
        )
        assert dispatched == [], f"expected 0 dispatches, got {dispatched}"
        assert result.exit_code == SUCCESS
        assert result.termination_reason == "no_changes_to_review"

    def test_no_changes_loop_manifest_records_correct_reason(self, tmp_path):
        """Claim 5 (honesty): loop-manifest.json records no_changes_to_review."""
        import json

        repo, pr_doc, head = _init_repo(tmp_path)
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            logger=Logger("normal"),
            adapter_factory=_factory_returning(*adapters),
            base_ref=head,
            worktree_base=tmp_path / "wt",
        )
        manifest_path = result.artifacts.run_dir / "loop-manifest.json"
        assert manifest_path.exists(), "loop-manifest.json was not written"
        data = json.loads(manifest_path.read_text())
        assert data["termination_reason"] == "no_changes_to_review"
        assert data["final_exit_code"] == SUCCESS


class TestNoChangesArtifacts:
    """Regression: durable artifacts must not show internal sentinel exit code 1."""

    def _run_no_changes(self, tmp_path):
        import json

        repo, pr_doc, head = _init_repo(tmp_path)
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            logger=Logger("normal"),
            adapter_factory=_factory_returning(*adapters),
            base_ref=head,
            worktree_base=tmp_path / "wt",
        )
        round_dir = result.artifacts.rounds[0].round_dir
        round_manifest = json.loads((round_dir / "manifest.json").read_text())
        round_summary = (round_dir / "summary.md").read_text()
        loop_summary = result.artifacts.loop_summary_path.read_text()
        return result, round_manifest, round_summary, loop_summary

    def test_round_manifest_exit_code_is_zero_not_one(self, tmp_path):
        """Round manifest must record round_exit_code=0, not the old sentinel 1."""
        _, manifest, _, _ = self._run_no_changes(tmp_path)
        assert manifest["round_exit_code"] == 0, (
            f"expected round_exit_code=0, got {manifest['round_exit_code']}"
        )

    def test_round_summary_shows_no_changes_label_not_unknown(self, tmp_path):
        """Per-round summary.md must say NO_CHANGES_TO_REVIEW, not 'UNKNOWN'."""
        _, _, summary, _ = self._run_no_changes(tmp_path)
        assert "NO_CHANGES_TO_REVIEW" in summary, (
            "expected 'NO_CHANGES_TO_REVIEW' in per-round summary.md"
        )
        assert "UNKNOWN" not in summary, (
            "per-round summary.md must not contain 'UNKNOWN' exit label"
        )

    def test_loop_summary_shows_nothing_to_review_not_ship(self, tmp_path):
        """Loop summary final verdict must be 'NOTHING TO REVIEW', not 'SHIP'."""
        _, _, _, loop_summary = self._run_no_changes(tmp_path)
        assert "NOTHING TO REVIEW" in loop_summary, (
            "expected 'NOTHING TO REVIEW' in loop-summary.md final verdict"
        )
        # A plain SHIP verdict must NOT appear (the no-changes path never reviewed)
        # Check the verdict line specifically — "SHIP" may appear elsewhere as e.g.
        # a reviewer output label in the per-round table, so we look for the verdict
        # section only.
        assert "Final verdict: SHIP" not in loop_summary, (
            "loop-summary.md must not claim SHIP for a no-changes run"
        )


class TestAbsentBaseUnaffected:
    """L3: absent base (no --base/--scope) still dispatches normally."""

    def test_absent_base_dispatches_reviewers(self, tmp_path):
        """Claim 2: base=None → reviewers dispatched as before."""
        repo, pr_doc, _ = _init_repo(tmp_path)
        dispatched: list[str] = []

        class TrackingAdapter(FakeAdapter):
            def build_invocation(self, reviewer_config, worktree_path, prompt):
                dispatched.append(reviewer_config.name)
                return super().build_invocation(reviewer_config, worktree_path, prompt)

        adapters = [TrackingAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            logger=Logger("normal"),
            adapter_factory=_factory_returning(*adapters),
            base_ref=None,  # absent — review full HEAD
            worktree_base=tmp_path / "wt",
        )
        assert len(dispatched) == 2, f"expected 2 dispatches, got {dispatched}"
        assert result.exit_code == SUCCESS
        assert result.termination_reason == "ship"


class TestFailClosedRefusal:
    """D2: a diff whose only sections are unidentifiable must exit 60, never 0."""

    def test_unreadable_only_diff_refuses_exit_60(self, tmp_path):
        """Claim 3: fail-closed sections refuse with exit 60."""
        from unittest.mock import patch

        repo, pr_doc, head = _init_repo(tmp_path)

        # Add a commit so HEAD != base
        (repo / "feature.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "feature.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add feature"], cwd=repo, check=True)

        dispatched: list[str] = []

        class TrackingAdapter(FakeAdapter):
            def build_invocation(self, reviewer_config, worktree_path, prompt):
                dispatched.append(reviewer_config.name)
                return super().build_invocation(reviewer_config, worktree_path, prompt)

        adapters = [TrackingAdapter(canned_output=_ship()) for _ in range(2)]
        with (
            patch("syncade.orchestrator.round.filter_diff_for_reviewer") as mock_filter,
            patch("syncade.orchestrator.round.unidentifiable_sections") as mock_unid,
        ):
            mock_unid.return_value = ['diff --git "x/bad.py" b/bad.py']
            mock_filter.return_value = ""
            result = run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=_two_reviewer_config(),
                logger=Logger("normal"),
                adapter_factory=_factory_returning(*adapters),
                base_ref=head,
                worktree_base=tmp_path / "wt",
            )
        assert dispatched == [], f"expected 0 dispatches on refusal, got {dispatched}"
        assert result.exit_code == WORKTREE_ERROR

    def test_3a_vs_3b_distinguished(self, tmp_path):
        """Claim 4: context-only diff (3a) → no_changes_to_review; unreadable (3b) → exit 60."""
        from unittest.mock import patch

        repo, pr_doc, head = _init_repo(tmp_path)
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]

        # 3a: context-only — filter drops everything, unidentifiable returns []
        with (
            patch("syncade.orchestrator.round.filter_diff_for_reviewer", return_value=""),
            patch("syncade.orchestrator.round.unidentifiable_sections", return_value=[]),
        ):
            result_3a = run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=_two_reviewer_config(),
                logger=Logger("normal"),
                adapter_factory=_factory_returning(*adapters),
                base_ref=head,
                worktree_base=tmp_path / "wt_3a",
            )
        assert result_3a.exit_code == SUCCESS
        assert result_3a.termination_reason == "no_changes_to_review"

        # 3b: unreadable — filter drops everything, unidentifiable returns something
        adapters2 = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        with (
            patch("syncade.orchestrator.round.filter_diff_for_reviewer", return_value=""),
            patch(
                "syncade.orchestrator.round.unidentifiable_sections",
                return_value=['diff --git "bad" b/bad'],
            ),
        ):
            result_3b = run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=_two_reviewer_config(),
                logger=Logger("normal"),
                adapter_factory=_factory_returning(*adapters2),
                base_ref=head,
                worktree_base=tmp_path / "wt_3b",
            )
        assert result_3b.exit_code == WORKTREE_ERROR

    def _run_fail_closed(self, tmp_path, wt_suffix="wt"):
        """Helper: run with a mocked fail-closed refusal and return the result."""
        import json
        from unittest.mock import patch

        repo, pr_doc, head = _init_repo(tmp_path)
        (repo / "feature.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "feature.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add feature"], cwd=repo, check=True)

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        bad_header = 'diff --git "x/bad\xc3\xafve.py" b/bad.py'
        with (
            patch("syncade.orchestrator.round.filter_diff_for_reviewer", return_value=""),
            patch(
                "syncade.orchestrator.round.unidentifiable_sections",
                return_value=[bad_header],
            ),
        ):
            result = run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=_two_reviewer_config(),
                logger=Logger("normal"),
                adapter_factory=_factory_returning(*adapters),
                base_ref=head,
                worktree_base=tmp_path / wt_suffix,
            )
        round_dir = result.artifacts.rounds[0].round_dir
        manifest = json.loads((round_dir / "manifest.json").read_text())
        summary = (round_dir / "summary.md").read_text()
        loop_summary = result.artifacts.loop_summary_path.read_text()
        refused_path = round_dir / "diff-refused.txt"
        return result, manifest, summary, loop_summary, refused_path, bad_header

    def test_diff_refused_artifact_written(self, tmp_path):
        """Regression: diff-refused.txt must exist and name the dropped headers."""
        _, _, _, _, refused_path, bad_header = self._run_fail_closed(tmp_path)
        assert refused_path.exists(), "diff-refused.txt was not written on fail-closed refusal"
        content = refused_path.read_text()
        assert bad_header in content, "diff-refused.txt must contain the unidentifiable header"

    def test_round_manifest_has_refusal_headers(self, tmp_path):
        """Regression: manifest.json must carry diff_filter_refusal_headers."""
        _, manifest, _, _, _, bad_header = self._run_fail_closed(tmp_path)
        assert "diff_filter_refusal_headers" in manifest, (
            "manifest.json must have diff_filter_refusal_headers on fail-closed refusal"
        )
        assert bad_header in manifest["diff_filter_refusal_headers"], (
            "diff_filter_refusal_headers must list the dropped header"
        )

    def test_round_summary_shows_diff_malformed_label(self, tmp_path):
        """Regression: per-round summary.md must say DIFF_MALFORMED, not UNKNOWN."""
        _, _, summary, _, _, _ = self._run_fail_closed(tmp_path)
        assert "DIFF_MALFORMED" in summary, (
            "per-round summary.md must show DIFF_MALFORMED exit label, not UNKNOWN"
        )

    def test_loop_summary_termination_reason_is_diff_malformed(self, tmp_path):
        """Regression: loop-summary must render diff_malformed reason, not worktree_error."""
        _, _, _, loop_summary, _, _ = self._run_fail_closed(tmp_path)
        assert "diff_malformed" in loop_summary or "diff filter refusal" in loop_summary, (
            "loop-summary.md must render the diff_malformed termination reason"
        )

    def test_termination_reason_is_diff_malformed_not_worktree_error(self, tmp_path):
        """Regression: RunResult.termination_reason must be diff_malformed, not worktree_error."""
        result, _, _, _, _, _ = self._run_fail_closed(tmp_path)
        assert result.termination_reason == "diff_malformed", (
            f"expected termination_reason='diff_malformed', got {result.termination_reason!r}"
        )

    def test_loop_summary_reviewer_line_is_not_dispatched_not_zero_failed(self, tmp_path):
        """Regression: loop-summary reviewer line must say 'not dispatched' not '0 failed'
        for a diff_malformed refusal where no reviewers were dispatched."""
        _, _, _, loop_summary, _, _ = self._run_fail_closed(tmp_path)
        assert "0 failed" not in loop_summary, (
            "loop-summary must not say '0 failed' for diff_malformed — no reviewers ran"
        )
        assert "not dispatched" in loop_summary, (
            "loop-summary must say 'not dispatched' for diff_malformed reviewer line"
        )


def _init_repo_on_default_branch(tmp_path: Path) -> tuple[Path, Path, str]:
    """Repo on 'main' with origin/HEAD -> main (default branch guard active), plus PR doc.

    Returns (repo, pr_doc, head_sha).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    # Fake origin/HEAD → main so the authoritative remote-default path applies.
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", head], cwd=repo, check=True)
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=repo,
        check=True,
    )
    pr_doc = repo / "pr.md"
    pr_doc.write_text("# PR\n")
    return repo, pr_doc, head


class TestNoChangeBypassesCommitGuards:
    """Regression: commit-safety guards (dirty-tree, default-branch) must not fire when the
    diff is known-empty — no producer runs, so the guards are irrelevant and would
    incorrectly refuse valid no-change runs."""

    def test_known_empty_on_default_branch_exits_zero(self, tmp_path):
        """A no-change run (base == HEAD) on the default branch must exit 0, not be refused
        by the default-branch commit guard."""
        repo, pr_doc, head = _init_repo_on_default_branch(tmp_path)
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            logger=Logger("quiet"),
            adapter_factory=_factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship())),
            base_ref=head,  # base == HEAD → empty diff
            worktree_base=tmp_path / "wt",
            # No allow_default_branch — the guard would normally refuse.
        )
        assert result.exit_code == SUCCESS, (
            f"known-empty run on default branch should exit 0, got {result.exit_code}"
        )
        assert result.termination_reason == "no_changes_to_review"

    def test_known_empty_with_tracked_dirty_tree_exits_zero(self, tmp_path):
        """A no-change run (base == HEAD) with a tracked-dirty working tree must not be
        refused by the loop-mode dirty-tree guard (no producer will commit, no race)."""
        repo, pr_doc, head = _init_repo(tmp_path)  # work branch (guard not active)
        # Introduce a tracked-modified file AFTER taking the HEAD we'll use as base.
        (repo / "README.md").write_text("dirty\n")
        # Do NOT commit — leave it as a tracked modification (dirty state = "tracked").
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            logger=Logger("quiet"),
            adapter_factory=_factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship())),
            base_ref=head,  # base == HEAD (before the dirty edit) → empty committed diff
            worktree_base=tmp_path / "wt",
        )
        assert result.exit_code == SUCCESS, (
            f"known-empty run with dirty tree should exit 0, got {result.exit_code}"
        )
        assert result.termination_reason == "no_changes_to_review"


class TestProducerEmptiedDiff:
    """Regression for Blocker 2 (PR-h-02d): late-round empty diff after prior-round model spend
    must use 'producer_emptied_diff', not 'no_changes_to_review'.

    A run where round 0 dispatches reviewers and commits a producer, then round 1 sees an
    empty diff, must be honest: it says 'producer_emptied_diff' (prior spend happened), not
    'no_changes_to_review' (which falsely claims no model work was done).
    """

    def _multi_round_config(self) -> SyncadeConfig:
        return SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 2},
        )

    def test_late_round_empty_diff_is_producer_emptied_diff(self, tmp_path, monkeypatch):
        """Round 0 dispatches reviewers + commits producer; round 1 sees empty diff.
        Termination reason must be 'producer_emptied_diff', NOT 'no_changes_to_review'."""
        from syncade.snapshot import Snapshot

        repo = tmp_path / "repo"
        head = _init_git_repo(repo)
        # Add a commit so the diff is non-empty in round 0.
        (repo / "feature.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "feature.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add feature"], cwd=repo, check=True)
        pr_doc = repo / "pr.md"
        pr_doc.write_text("# PR\n")
        subprocess.run(["git", "add", "pr.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add pr doc"], cwd=repo, check=True)

        # After round 0's producer commits, round 1 re-snapshots and finds nothing.
        # Intercept take_snapshot (only called for round_idx > 0) to return an empty diff.
        import syncade.orchestrator.loop_round_step as lrs_module

        _real_take_snapshot = lrs_module.take_snapshot

        def _empty_snapshot(repo_root, *, base_ref=None, three_dot=True):
            real = _real_take_snapshot(repo_root, base_ref=base_ref, three_dot=three_dot)
            return Snapshot(
                repo_root=real.repo_root,
                commit_sha=real.commit_sha,
                branch=real.branch,
                base_ref=real.base_ref,
                diff_text="",
                dirty_state="clean",
                untracked_count=0,
                base_oid=real.base_oid or base_ref,
            )

        monkeypatch.setattr(lrs_module, "take_snapshot", _empty_snapshot)

        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(),
            logger=Logger("quiet"),
            adapter_factory=_factory_returning(
                FakeAdapter(canned_output=_no_ship()),
                FakeAdapter(canned_output=_no_ship()),
            ),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker()),
            producer_adapter=FakeProducerAdapter(commit_message="fix: empty all changes"),
            worktree_base=tmp_path / "wt",
            base_ref=head,
        )

        assert result.termination_reason == "producer_emptied_diff", (
            f"expected 'producer_emptied_diff' for late-round empty diff after prior spend, "
            f"got {result.termination_reason!r}"
        )
        assert result.exit_code == SUCCESS, (
            f"expected exit 0 for producer_emptied_diff, got {result.exit_code}"
        )
