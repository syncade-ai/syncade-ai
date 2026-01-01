"""Tests for :mod:`syncade.orchestrator`.

Uses :class:`FakeAdapter` exclusively via the ``adapter_factory``
parameter — no real CLI calls. Each test sets up an ephemeral git
repo in ``tmp_path`` so the snapshot + worktree provisioning steps
exercise real git, then injects fakes for the reviewer dispatch.

Total runtime under 5 seconds (the brief's bound).
"""

from __future__ import annotations

import json
import subprocess

import pytest

from syncade.adapters.fake import FakeAdapter
from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.worktree import WorktreeError
from tests.orchestrator._helpers import (
    _factory_returning,
    _ship,
)
from tests.orchestrator._resume_fixtures import _prepare_aborted_run

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestResumeRehydration:
    """Round-level resume: completed rounds get rehydrated into
    round_results so downstream consumers (loop-summary, PR-14
    cross-round-context) see the prior state."""

    def test_resume_skips_completed_rounds_and_rehydrates(self, repo_with_pr_doc, monkeypatch):
        """Round 0 completed (with producer commit + manifest);
        resume runs only round 1+. The rehydrated round 0 lands in
        ``result.rounds`` with its prior state."""
        from syncade.orchestrator import run_review
        from syncade.orchestrator.resume import plan_resume

        repo_root, pr_doc = repo_with_pr_doc
        # Make the operator branch look like main.
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
        # The fixture writes a fabricated round-0 producer commit SHA.
        # For tree-drift check to pass we need force_drift=True
        # because the operator's actual HEAD doesn't equal the
        # fabricated fixture SHA.
        run_dir, expected_round_1_sha = _prepare_aborted_run(
            repo_root,
            pr_doc,
            completed_round_count=1,
            max_rounds=2,
            aborted_round_partial=True,
            aborted_exit_code=40,  # write loop-manifest with exit 40
        )
        plan = plan_resume(repo_root, run_dir)
        assert plan.resumed_round == 1
        assert plan.completed_rounds == [0]

        # Run with force_drift=True since the operator's HEAD won't
        # match the fabricated round-1 expected SHA.
        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 2},
        )
        factory = _factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship()))
        result = run_review(
            repo_root=repo_root,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=factory,
            logger=Logger(level="quiet"),
            resume_plan=plan,
            force_drift=True,
        )
        # Result.rounds should have BOTH round 0 (rehydrated) AND
        # round 1 (freshly run).
        assert len(result.rounds) == 2
        rehydrated_round_0 = result.rounds[0]
        # Round 0's reviewer summaries from the fixture (proves
        # the rehydration loaded prior round content into memory).
        assert rehydrated_round_0.round_idx == 0
        assert rehydrated_round_0.dispatch_result.results[0].output.summary == "round 0 rv1 summary"
        # Round 0 had a producer commit recorded in the fixture.
        assert rehydrated_round_0.producer_result is not None
        assert rehydrated_round_0.producer_result.outcome == "committed"
        # Round 1 is the freshly-run round.
        new_round_1 = result.rounds[1]
        assert new_round_1.round_idx == 1
        # The fresh round 1 saw the actual operator's HEAD (since
        # force_drift was on); its reviewer dispatch consumed the
        # FakeAdapters above and produced SHIP.

    def test_tree_drift_accepts_matching_sha256_head(self, tmp_path):
        from syncade.orchestrator.resume import check_tree_drift

        repo_root = tmp_path / "sha256-repo"
        repo_root.mkdir()
        try:
            subprocess.run(
                ["git", "init", "-q", "-b", "main", "--object-format=sha256"],
                cwd=repo_root,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            pytest.skip(f"git does not support sha256 object-format: {exc.stderr}")
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo_root, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo_root, check=True)
        (repo_root / "README.md").write_text("sha256\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_root, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        assert len(sha) == 64
        check_tree_drift(repo_root, sha, "main")

    def test_resume_drops_partial_round_directory(self, repo_with_pr_doc):
        """Round 0 completed; round 1 has partial state (some
        reviewer stdouts) but no manifest. Resume should rmtree
        round-1/ before the fresh round 1 runs."""
        from syncade.orchestrator import run_review
        from syncade.orchestrator.resume import plan_resume

        repo_root, pr_doc = repo_with_pr_doc
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
        run_dir, _ = _prepare_aborted_run(
            repo_root,
            pr_doc,
            completed_round_count=1,
            max_rounds=2,
            aborted_round_partial=True,
            aborted_exit_code=40,
        )
        partial_round_dir = run_dir / "round-1"
        # Confirm fixture: partial round exists with a stale file.
        assert (partial_round_dir / "rv1.stdout").is_file()
        assert (partial_round_dir / "rv1.stdout").read_text() == "partial output"
        plan = plan_resume(repo_root, run_dir)
        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 2},
        )
        factory = _factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship()))
        run_review(
            repo_root=repo_root,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=factory,
            logger=Logger(level="quiet"),
            resume_plan=plan,
            force_drift=True,
        )
        # After resume, round 1's reviewer files are FRESH — the
        # partial "partial output" content from the fixture is
        # gone (overwritten by the new run).
        rv1_stdout = (partial_round_dir / "rv1.stdout").read_text()
        assert rv1_stdout != "partial output"

    def test_resume_tree_drift_refuses_without_force(self, repo_with_pr_doc, tmp_path):
        """The operator's HEAD doesn't match the resumed round's
        expected SHA AND --force-drift is False → exit 60 via
        WorktreeError (which the CLI maps to exit 60)."""
        from syncade.orchestrator import run_review
        from syncade.orchestrator.resume import plan_resume

        repo_root, pr_doc = repo_with_pr_doc
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
        # The fixture round 1 expected SHA is fabricated and doesn't
        # match the operator's actual HEAD → drift on the SHA check.
        run_dir, _ = _prepare_aborted_run(
            repo_root,
            pr_doc,
            completed_round_count=1,
            max_rounds=2,
            aborted_round_partial=True,
            aborted_exit_code=40,
        )
        plan = plan_resume(repo_root, run_dir)
        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 2},
        )
        factory = _factory_returning(FakeAdapter(_ship()))
        with pytest.raises(WorktreeError, match="drift"):
            run_review(
                repo_root=repo_root,
                pr_doc_path=pr_doc,
                config=config,
                adapter_factory=factory,
                logger=Logger(level="quiet"),
                resume_plan=plan,
                force_drift=False,
            )

    def test_resume_force_drift_proceeds_under_mismatch(self, repo_with_pr_doc):
        """--force-drift bypasses the tree-drift check and the
        resumed round snapshots from current HEAD."""
        from syncade.orchestrator import run_review
        from syncade.orchestrator.resume import plan_resume

        repo_root, pr_doc = repo_with_pr_doc
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
        run_dir, _ = _prepare_aborted_run(
            repo_root,
            pr_doc,
            completed_round_count=1,
            max_rounds=2,
            aborted_round_partial=False,
            aborted_exit_code=40,
        )
        plan = plan_resume(repo_root, run_dir)
        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 2},
        )
        factory = _factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship()))
        result = run_review(
            repo_root=repo_root,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=factory,
            logger=Logger(level="quiet"),
            resume_plan=plan,
            force_drift=True,
        )
        # The fresh round 1's snapshot SHA is the operator's actual
        # HEAD, not the fixture's fabricated expected SHA.
        actual_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert result.rounds[1].snapshot.commit_sha == actual_head

    def test_resume_round_0_when_no_rounds_completed(self, repo_with_pr_doc):
        """No completed rounds → resumed_round=0, no rehydration."""
        from syncade.orchestrator import run_review
        from syncade.orchestrator.resume import plan_resume

        repo_root, pr_doc = repo_with_pr_doc
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
        run_dir, _ = _prepare_aborted_run(
            repo_root,
            pr_doc,
            completed_round_count=0,
            max_rounds=2,
            aborted_round_partial=False,
            aborted_exit_code=40,
        )
        plan = plan_resume(repo_root, run_dir)
        assert plan.resumed_round == 0
        assert plan.completed_rounds == []
        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 2},
        )
        factory = _factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship()))
        result = run_review(
            repo_root=repo_root,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=factory,
            logger=Logger(level="quiet"),
            resume_plan=plan,
            # No drift — round 0 snapshots from operator HEAD which IS the starting_sha.
            force_drift=False,
        )
        # Only fresh round 0 ran.
        assert len(result.rounds) == 1
        assert result.rounds[0].round_idx == 0

    def test_resume_reuses_original_run_id(self, repo_with_pr_doc):
        """The resumed run writes into the original run-id directory,
        NOT a fresh one."""
        from syncade.orchestrator import run_review
        from syncade.orchestrator.resume import plan_resume

        repo_root, pr_doc = repo_with_pr_doc
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
        original_run_id = "2026-05-28T08-30-30"
        run_dir, _ = _prepare_aborted_run(
            repo_root,
            pr_doc,
            run_id=original_run_id,
            completed_round_count=0,
            max_rounds=2,
            aborted_round_partial=False,
            aborted_exit_code=40,
        )
        plan = plan_resume(repo_root, run_dir)
        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 2},
        )
        factory = _factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship()))
        result = run_review(
            repo_root=repo_root,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=factory,
            logger=Logger(level="quiet"),
            resume_plan=plan,
            force_drift=False,
        )
        # The resumed run wrote into the original directory.
        assert result.artifacts.run_dir == run_dir
        # And the original run-init.json is preserved (not
        # overwritten on resume).
        run_init_path = run_dir / "run-init.json"
        assert run_init_path.is_file()
        original_init = json.loads(run_init_path.read_text())
        assert original_init["syncade_version"] == "0.1.0"

    def test_resume_run_init_is_not_rewritten(self, repo_with_pr_doc):
        """run-init.json is written ONCE per run-id; resume does NOT
        rewrite it. Pin this so a future refactor that moves the
        write into the resume path doesn't silently clobber the
        original record."""
        from syncade.orchestrator import run_review
        from syncade.orchestrator.resume import plan_resume

        repo_root, pr_doc = repo_with_pr_doc
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
        run_dir, _ = _prepare_aborted_run(
            repo_root,
            pr_doc,
            completed_round_count=0,
            max_rounds=2,
            aborted_round_partial=False,
            aborted_exit_code=40,
        )
        # Pin the original run-init.json's mtime BEFORE the resume.
        run_init_path = run_dir / "run-init.json"
        original_mtime = run_init_path.stat().st_mtime_ns
        plan = plan_resume(repo_root, run_dir)
        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 2},
        )
        factory = _factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship()))
        run_review(
            repo_root=repo_root,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=factory,
            logger=Logger(level="quiet"),
            resume_plan=plan,
            force_drift=False,
        )
        # mtime unchanged → file was NOT rewritten.
        assert run_init_path.stat().st_mtime_ns == original_mtime

    def test_resume_extends_max_rounds_when_cli_overrides(self, repo_with_pr_doc):
        """--resume --max-rounds N allows continuing past the original run's cap.

        Scenario: original run had max_rounds=1, all 1 round completed
        cleanly, but loop-manifest recorded exit 40 (environment failure
        after the final round's persistence). The operator passes
        --resume --max-rounds 2. plan_resume with max_rounds_override=2
        should NOT raise the degenerate error; instead it should return
        resumed_round=1. The orchestrator then runs round 1 with the
        new cap.
        """
        from syncade.orchestrator import run_review
        from syncade.orchestrator.resume import plan_resume

        repo_root, pr_doc = repo_with_pr_doc
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
        # 1 completed round, max_rounds=1, exit 40 on loop-manifest.
        run_dir, _ = _prepare_aborted_run(
            repo_root,
            pr_doc,
            completed_round_count=1,
            max_rounds=1,
            aborted_round_partial=False,
            aborted_exit_code=40,
        )

        # Without override: all 1 round completed → degenerate ResumeError.
        from syncade.orchestrator.resume import ResumeError

        with pytest.raises(ResumeError, match="degenerate"):
            plan_resume(repo_root, run_dir)

        # With max_rounds_override=2: plan_resume should surface resumed_round=1.
        plan = plan_resume(repo_root, run_dir, max_rounds_override=2)
        assert plan.resumed_round == 1
        assert plan.completed_rounds == [0]

        # Run with the extended cap — round 1 dispatches and SHIPs.
        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 2},
        )
        factory = _factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship()))
        result = run_review(
            repo_root=repo_root,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=factory,
            logger=Logger(level="quiet"),
            resume_plan=plan,
            force_drift=True,
        )
        # Round 0 rehydrated + round 1 freshly run.
        assert len(result.rounds) == 2
        assert result.rounds[0].round_idx == 0
        assert result.rounds[1].round_idx == 1

    def test_resume_with_undersized_max_rounds_still_runs_the_resumed_round(self, repo_with_pr_doc):
        """Regression: a too-small ``--max-rounds`` on resume must NOT
        produce a zero-round, false exit-0 SHIP.

        Scenario: original run had max_rounds=3, aborted at round 2
        (rounds 0+1 completed, round 2 partial). The operator resumes
        with ``--max-rounds 1`` (<= resumed_round). ``plan_resume`` uses
        ``effective_max_rounds = max(3, 1) = 3`` and surfaces
        resumed_round=2, but ``config.loop.max_rounds`` is the raw 1.
        Pre-fix, ``run_review``'s loop was ``range(2, 1)`` — empty: no
        round ran, ``final_exit_code`` stayed SUCCESS, and the run
        returned a false exit-0 SHIP pointing at a rehydrated NO-SHIP
        round. The fix bumps the loop bound to the planner's effective
        cap so the resumed round actually runs.
        """
        from syncade.adapters.fake import FakeSynthesizerAdapter
        from syncade.exit_codes import MAX_ROUNDS_REACHED
        from syncade.orchestrator import run_review
        from syncade.orchestrator.resume import plan_resume
        from tests.orchestrator._helpers import _no_ship, _synth_with_blocker

        repo_root, pr_doc = repo_with_pr_doc
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
        # 3-round run, aborted at round 2 (rounds 0+1 done, round 2 partial).
        run_dir, _ = _prepare_aborted_run(
            repo_root,
            pr_doc,
            completed_round_count=2,
            max_rounds=3,
            aborted_round_partial=True,
            aborted_exit_code=40,
        )

        # Mirror the CLI's ``--resume --max-rounds 1`` path: plan with the
        # override, then run with config.loop.max_rounds == 1.
        plan = plan_resume(repo_root, run_dir, max_rounds_override=1)
        assert plan.resumed_round == 2
        assert plan.completed_rounds == [0, 1]
        assert plan.max_rounds == 3  # the ORIGINAL cap

        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 1},  # the too-small override
        )
        factory = _factory_returning(FakeAdapter(_no_ship()), FakeAdapter(_no_ship()))
        result = run_review(
            repo_root=repo_root,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=factory,
            synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_with_blocker()),
            logger=Logger(level="quiet"),
            resume_plan=plan,
            force_drift=True,
        )
        # INVARIANT: never a false exit-0 SHIP on zero review work.
        assert result.exit_code != 0
        # The resumed round (2) actually ran: rounds 0+1 rehydrated + round 2.
        assert len(result.rounds) == 3
        assert result.rounds[2].round_idx == 2
        # Round 2 is the final round (effective cap 3) and was NO-SHIP →
        # exit 20 (max rounds reached), not a silent ship.
        assert result.exit_code == MAX_ROUNDS_REACHED
        assert result.termination_reason == "max_rounds_reached"
