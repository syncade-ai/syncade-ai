from __future__ import annotations

import json
import subprocess

import pytest

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _RoundCyclingSynth,
    _ship,
    _synth_clean,
    _synth_with_blocker,
    _two_reviewer_config,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestReviewerWorktreeConfinement:
    """Worktree-escape fix: reviewers must be handed a worktree-LOCAL PR-doc
    reference (not the operator's absolute MAIN path), and the doc must be
    readable inside each reviewer's worktree — so the reviewer reviews its
    isolated snapshot, not the live main repo. Empirically: run
    2026-05-30T21-22-19 round 0, claude-reviewer followed the absolute MAIN
    pr_doc path and cd'd into the operator's repo for most of its commands."""

    def test_reviewer_prompt_uses_worktree_local_pr_doc_not_absolute_main_path(
        self, repo_with_pr_doc
    ):
        repo, pr_doc = repo_with_pr_doc  # pr_doc lives OUTSIDE the repo (tmp_path/pr.md)
        fa1 = FakeAdapter(canned_output=_no_ship())  # rv1 → 1 finding at index 0
        fa2 = FakeAdapter(canned_output=_ship())
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(fa1, fa2),
            synthesizer_adapter=FakeSynthesizerAdapter(
                canned_output=_synth_with_blocker(reviewer_name="rv1")
            ),
            logger=Logger(level="quiet"),
        )
        # NO-SHIP → exit 30 → worktrees preserved on disk for the checks below.
        assert result.exit_code == 30
        for fa in (fa1, fa2):
            assert fa.invocations, "reviewer should have been dispatched"
            _cfg, worktree_path, prompt = fa.invocations[0]
            # The absolute MAIN pr_doc path — the escape lure — is GONE.
            assert str(pr_doc) not in prompt
            # An out-of-repo doc is referenced by a reserved, collision-free
            # worktree-local ref under .syncade-inputs/ (H2: a same-basename
            # tracked file must not be able to shadow the supplied doc).
            assert ".syncade-inputs/pr-doc-" in prompt
            assert "pr.md" in prompt
            # And the doc is actually readable INSIDE the reviewer's worktree at
            # that reserved ref, so it resolves without leaving the sandbox.
            copied = list((worktree_path / ".syncade-inputs").glob("pr-doc-*-pr.md"))
            assert len(copied) == 1 and copied[0].is_file()


class TestLastReviewedRecording:
    """PR-B: a completed verdict (exit 0/20/30) records the branch's run-end HEAD
    to .syncade/last-reviewed.json (per-branch); an aborted run (e.g. exit 40)
    records nothing."""

    @staticmethod
    def _branch_and_head(repo):
        b = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        h = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        return b, h

    def test_records_branch_head_on_ship(self, repo_with_pr_doc):
        from syncade.persistence import read_last_reviewed

        repo, pr_doc = repo_with_pr_doc
        branch, head = self._branch_and_head(repo)
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(
                FakeAdapter(canned_output=_ship()), FakeAdapter(canned_output=_ship())
            ),
            synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_clean()),
            logger=Logger(level="quiet"),
        )
        assert result.exit_code == 0
        assert read_last_reviewed(repo, branch) == head

    def test_records_sha256_branch_head_on_ship(self, tmp_path):
        from syncade.persistence import read_last_reviewed

        repo = tmp_path / "sha256-repo"
        repo.mkdir()
        try:
            subprocess.run(
                ["git", "init", "-q", "-b", "main", "--object-format=sha256"],
                cwd=repo,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            pytest.skip(f"git does not support sha256 object-format: {exc.stderr}")
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        (repo / "README.md").write_text("sha256\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# Synthetic PR\n\n**Goal:** sha256 fixture\n", encoding="utf-8")
        branch, head = self._branch_and_head(repo)

        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(
                FakeAdapter(canned_output=_ship()), FakeAdapter(canned_output=_ship())
            ),
            synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_clean()),
            logger=Logger(level="quiet"),
        )

        assert len(head) == 64
        assert result.exit_code == 0
        assert read_last_reviewed(repo, branch) == head

    def test_no_record_on_reviewer_failure(self, repo_with_pr_doc):
        from syncade.persistence import read_last_reviewed

        repo, pr_doc = repo_with_pr_doc
        branch, _ = self._branch_and_head(repo)
        adapters = [
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(
                canned_exception=ReviewerInvocationError("boom", returncode=1, stdout="", stderr="")
            ),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_clean()),
            logger=Logger(level="quiet"),
        )
        assert result.exit_code == 40
        assert read_last_reviewed(repo, branch) is None


class TestMechanicalChecks:
    """PR-21: user-defined mechanical checks. Each ``[[checks]]`` block runs in
    its own stripped worktree (the test-leg machinery) when reviewers succeeded
    AND the synth produced output. A ``blocking`` failure folds into the verdict
    like a failing test (→ exit 30; subprocess_error → exit 40); an ``advisory``
    failure is surfaced but NEVER reaches ``_compute_exit_code``. Zero checks =
    today's loop, byte-identical.

    Shell ``true`` / ``false`` / a missing binary stand in for real tools;
    ``max_rounds=1`` keeps these off the producer path (no producer injected)."""

    def _config(self, checks: list[dict]) -> SyncadeConfig:
        return SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 1},
            checks=checks,
        )

    def test_no_checks_configured_unchanged(self, repo_with_pr_doc):
        """Zero-config cardinal invariant: no checks → no check_results, exit
        reflects synth-clean only (byte-identical to pre-PR-21)."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config([]),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.exit_code == 0
        assert result.rounds[-1].check_results == []

    def test_blocking_check_passing_exit_0(self, repo_with_pr_doc):
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config([{"name": "lint", "command": "true", "severity": "blocking"}]),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.exit_code == 0
        assert len(result.rounds[-1].check_results) == 1
        assert result.rounds[-1].check_results[0].outcome == "passed"

    def test_blocking_check_failing_exit_30(self, repo_with_pr_doc):
        """Synth-clean + a failing BLOCKING check → exit 30, the same
        mechanical OR a failing test leg uses. NOT a ConsolidatedFinding."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config([{"name": "lint", "command": "false", "severity": "blocking"}]),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.exit_code == 30
        cr = result.rounds[-1].check_results[0]
        assert cr.outcome == "failed"
        assert cr.severity == "blocking"

    def test_advisory_check_failing_still_ships_exit_0(self, repo_with_pr_doc):
        """The load-bearing invariant: a failing ADVISORY check is surfaced but
        NEVER gates — exit stays 0 (advisory results are structurally never
        passed to ``_compute_exit_code``)."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(
                [{"name": "file-length", "command": "false", "severity": "advisory"}]
            ),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.exit_code == 0
        cr = result.rounds[-1].check_results[0]
        assert cr.outcome == "failed"
        assert cr.severity == "advisory"

    def test_blocking_check_subprocess_error_exit_40(self, repo_with_pr_doc):
        """A blocking check whose binary is missing (exit 127 →
        subprocess_error) → exit 40, the same bucket as a test subprocess
        error."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(
                [{"name": "lint", "command": "thisbinarydoesnotexistxyzzy", "severity": "blocking"}]
            ),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.exit_code == 40
        assert result.rounds[-1].check_results[0].outcome == "subprocess_error"

    def test_blocking_check_subprocess_error_termination_reason(self, repo_with_pr_doc):
        """PR-22 blocker 1: a blocking-check subprocess_error is classified as
        termination_reason='check_subprocess_error', not the misleading
        'reviewer_failure' fall-through — so loop-summary.md attributes the
        ABORT to the check, not to a (clean) reviewer subprocess."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(
                [{"name": "lint", "command": "thisbinarydoesnotexistxyzzy", "severity": "blocking"}]
            ),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.exit_code == 40
        assert result.termination_reason == "check_subprocess_error"

    def test_advisory_subprocess_error_still_ships(self, repo_with_pr_doc):
        """Advisory never gates — even a subprocess_error advisory check ships."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(
                [{"name": "loc", "command": "thisbinarydoesnotexistxyzzy", "severity": "advisory"}]
            ),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.exit_code == 0
        assert result.rounds[-1].check_results[0].outcome == "subprocess_error"

    def test_checks_run_on_synth_blocker_round(self, repo_with_pr_doc):
        """Unlike the test leg (which skips synth-blocker rounds), checks run
        whenever reviewers + synth succeeded — so advisory drift is visible even
        on a NO-SHIP round. Verdict stays 30 (from synth); the check still
        ran."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config([{"name": "loc", "command": "false", "severity": "advisory"}]),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(
                canned_output=_synth_with_blocker(reviewer_name="rv1")
            ),
        )
        assert result.exit_code == 30  # from the synth blocker
        assert len(result.rounds[-1].check_results) == 1  # check ran anyway

    def test_check_results_survive_producer_round_reconstruction(self, repo_with_pr_doc):
        """A NO-SHIP round that triggers the producer must NOT lose its check
        results. After the producer runs, the loop rebuilds the RoundResult and
        re-writes manifest.json + summary.md; those rewrites have to carry
        check_results through, or the '## Mechanical checks' section silently
        vanishes from the producer round even though the raw artifacts and
        findings.md still have it. Reproduces the surfacing-erasure on the
        common multi-round NO-SHIP path (the synth-blocker-round test above
        used max_rounds=1, so the producer never ran)."""
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        # Round 0 NO-SHIP (synth blocker) → producer commits → round 1 SHIPs.
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 2},
            checks=[{"name": "loc", "command": "false", "severity": "advisory"}],
        )
        producer = FakeProducerAdapter(commit_message="fix: address round 0 blocker")
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(
                _synth_with_blocker(reviewer_name="rv1"),  # round 0 → NO-SHIP → producer runs
                _synth_clean(),  # round 1 → SHIP
            ),
            producer_adapter=producer,
        )
        assert result.exit_code == 0
        assert result.final_round == 1
        # In-memory: round 0's check_results survive the producer-aware rebuild.
        assert len(result.rounds[0].check_results) == 1
        assert result.rounds[0].check_results[0].name == "loc"
        # On disk: round 0's manifest.json + summary.md (re-written after the
        # producer ran) still carry the checks — not just findings.md.
        round0_dir = result.artifacts.run_dir / "round-0"
        manifest = json.loads((round0_dir / "manifest.json").read_text())
        assert "checks" in manifest
        assert [c["name"] for c in manifest["checks"]] == ["loc"]
        assert "## Mechanical checks" in (round0_dir / "summary.md").read_text()

    def test_blocking_subprocess_error_with_synth_blocker_aborts_exit_40(self, repo_with_pr_doc):
        """Precedence: a blocking check whose harness couldn't run
        (subprocess_error → exit 40) outranks a synth blocker (exit 30). Because
        checks run on synth-blocker rounds too (unlike the test leg), the two can
        coincide; the round is then indeterminate — a gate couldn't execute — so
        the operator must fix the environment first. The synth-blocker early
        return previously masked the blocking subprocess_error, exiting 30."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config(
                [{"name": "lint", "command": "thisbinarydoesnotexistxyzzy", "severity": "blocking"}]
            ),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(
                canned_output=_synth_with_blocker(reviewer_name="rv1")
            ),
        )
        assert result.exit_code == 40  # subprocess_error outranks the synth blocker
        assert result.rounds[-1].check_results[0].outcome == "subprocess_error"
