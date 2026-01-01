"""Tests for :mod:`syncade.orchestrator`.

Uses :class:`FakeAdapter` exclusively via the ``adapter_factory``
parameter — no real CLI calls. Each test sets up an ephemeral git
repo in ``tmp_path`` so the snapshot + worktree provisioning steps
exercise real git, then injects fakes for the reviewer dispatch.

Total runtime under 5 seconds (the brief's bound).
"""

from __future__ import annotations

import subprocess

import pytest

from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.config import SyncadeConfig
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import (
    _current_branch_ref,
    _factory_returning,
    _no_ship,
    _RoundCyclingSynth,
    _ship,
    _synth_clean,
    _synth_with_blocker,
)

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestMultiRoundLoop:
    """PR-8: the for-loop that wraps the per-round pipeline.

    These tests exercise the new multi-round behavior end-to-end
    against fakes. Production behavior (real ``claude`` / ``codex``
    subprocesses) is covered by the smoke tests
    (``tests/smoke/test_loop_smoke.py``).

    Single-pass back-compat (``max_rounds=1``) is verified by the
    ``TestTestReRunActive`` class above, which the brief calls out
    as the pinning regression.
    """

    def _multi_round_config(
        self, max_rounds: int = 3, test_command: str | None = None
    ) -> SyncadeConfig:
        """Two reviewers + explicit max_rounds. The producer config
        defaults are fine; tests inject ``producer_adapter`` to
        avoid spawning real subprocesses."""
        loop: dict[str, object] = {"max_rounds": max_rounds}
        if test_command is not None:
            loop["test_command"] = test_command
        return SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop=loop,
        )

    def test_round_0_ships_no_producer(self, repo_with_pr_doc):
        """Round 0 SHIPs → exit 0, producer never runs, only round-0/
        artifacts written. Termination reason: ship."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        # FakeProducerAdapter would never be called; pass an
        # exception-canned one to prove that.
        from syncade.adapters.base import ReviewerInvocationError as _RIE
        from syncade.adapters.fake import FakeProducerAdapter

        producer = FakeProducerAdapter(
            canned_exception=_RIE(
                "producer should not be called for SHIP round",
                returncode=1,
                stdout="",
                stderr="",
            ),
        )
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=3),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_clean()),
            producer_adapter=producer,
        )
        assert result.exit_code == 0
        assert result.termination_reason == "ship"
        assert result.final_round == 0
        assert len(result.rounds) == 1
        # Producer never called
        assert producer.parse_output_calls == 0
        # Only round-0/ on disk
        round_dirs = sorted(result.artifacts.run_dir.glob("round-*"))
        assert len(round_dirs) == 1
        assert round_dirs[0].name == "round-0"

    def test_round_0_no_ship_max_rounds_1_exit_30(self, repo_with_pr_doc):
        """max_rounds=1 + round 0 NO-SHIP → exit 30 (PR-7.5
        back-compat). NOT exit 20: the single-pass operator didn't ask for
        retries."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_no_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=1),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_with_blocker()),
        )
        assert result.exit_code == 30
        assert result.termination_reason == "findings_present"
        assert len(result.rounds) == 1

    def test_round_0_no_ship_max_rounds_2_round_1_ships(self, repo_with_pr_doc):
        """max_rounds=2: round 0 NO-SHIP → producer commits →
        round 1 SHIPs → exit 0. Both round-0/ and round-1/ on disk."""
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        # Round 0 reviewers + round 1 reviewers (factory consumes in order)
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        producer = FakeProducerAdapter(commit_message="fix: round 0 producer")
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=2),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(
                _synth_with_blocker(),  # round 0
                _synth_clean(),  # round 1
            ),
            producer_adapter=producer,
        )
        assert result.exit_code == 0
        assert result.termination_reason == "ship"
        assert result.final_round == 1
        assert len(result.rounds) == 2
        # Both round dirs exist
        round_dirs = sorted(result.artifacts.run_dir.glob("round-*"))
        assert {p.name for p in round_dirs} == {"round-0", "round-1"}
        # Round 0 has producer artifacts; round 1 doesn't
        assert (round_dirs[0] / "producer.stdout").exists()
        assert (round_dirs[0] / "producer.commit.txt").exists()
        assert not (round_dirs[1] / "producer.stdout").exists()

    def test_max_rounds_2_round_1_no_ship_exit_20(self, repo_with_pr_doc):
        """max_rounds=2: both rounds NO-SHIP → exit 20 (max rounds
        reached). The producer ran between rounds but didn't fix
        the problem."""
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
        ]
        producer = FakeProducerAdapter(commit_message="fix: attempt 1")
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=2),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(
                _synth_with_blocker(),
                _synth_with_blocker(),
            ),
            producer_adapter=producer,
        )
        assert result.exit_code == 20
        assert result.termination_reason == "max_rounds_reached"
        assert result.final_round == 1
        assert len(result.rounds) == 2

    def test_producer_stalled_exit_30(self, repo_with_pr_doc):
        """max_rounds=3: round 0 NO-SHIP, producer stalls (no
        commit) → exit 30 + termination_reason="producer_stalled"."""
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_no_ship()) for _ in range(2)]
        # commit_message=None → no fixture commit → HEAD stays put
        producer = FakeProducerAdapter()
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=3),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker()),
            producer_adapter=producer,
        )
        assert result.exit_code == 30
        assert result.termination_reason == "producer_stalled"
        assert result.final_round == 0
        # round-0/producer.* exist; commit.txt records starting_sha
        round0 = result.artifacts.run_dir / "round-0"
        assert (round0 / "producer.stdout").exists()
        commit_sha = (round0 / "producer.commit.txt").read_text().strip()
        # The fake didn't move HEAD, so ending_sha == starting_sha
        assert commit_sha == result.snapshot.commit_sha

    def test_producer_subprocess_error_exit_40(self, repo_with_pr_doc):
        """max_rounds=3: producer raises ReviewerInvocationError →
        exit 40 + termination_reason="producer_subprocess_error"."""
        from syncade.adapters.base import ReviewerInvocationError as _RIE
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_no_ship()) for _ in range(2)]
        producer = FakeProducerAdapter(
            canned_exception=_RIE(
                "claude failed",
                returncode=1,
                stdout="",
                stderr="",
            ),
        )
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=3),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker()),
            producer_adapter=producer,
        )
        assert result.exit_code == 40
        assert result.termination_reason == "producer_subprocess_error"
        # Producer artifacts written with error.txt
        round0 = result.artifacts.run_dir / "round-0"
        assert (round0 / "producer.error.txt").exists()

    def test_branch_advance_lands_on_named_branch(self, repo_with_pr_doc):
        """The producer commits on the worktree's detached HEAD; the
        orchestrator runs ``git update-ref refs/heads/<branch>
        <ending_sha>`` from repo_root. Verify the operator's branch
        moved to the producer's commit."""
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        # Round 0 NO-SHIP → producer → round 1 SHIP
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        producer = FakeProducerAdapter(commit_message="fix: branch advance test")

        branch_ref = _current_branch_ref(repo)
        branch_before = subprocess.run(
            ["git", "rev-parse", branch_ref],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=2),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_clean()),
            producer_adapter=producer,
        )
        assert result.exit_code == 0

        branch_after = subprocess.run(
            ["git", "rev-parse", branch_ref],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert branch_after != branch_before
        # And the new HEAD matches the producer's ending SHA from
        # round 0's producer phase
        round0_commit = (
            (result.artifacts.run_dir / "round-0" / "producer.commit.txt").read_text().strip()
        )
        assert branch_after == round0_commit

    def test_run_artifacts_producer_paths_list_aligns_with_rounds(self, repo_with_pr_doc):
        """PR-8 R2.T6: ``RunArtifacts.producer_paths`` is a flat
        list parallel to ``rounds[]`` per the brief — one entry
        per round, populated when the producer ran on that
        round, None when it didn't.
        """
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        # 2-round NO-SHIP → SHIP scenario: round 0 has a
        # producer, round 1 does NOT.
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        producer = FakeProducerAdapter(commit_message="fix: r2.t6 test")
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=2),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_clean()),
            producer_adapter=producer,
        )
        assert result.exit_code == 0
        # producer_paths list length matches rounds count
        assert len(result.artifacts.producer_paths) == 2
        # Round 0 has producer artifacts; round 1 doesn't.
        assert result.artifacts.producer_paths[0] is not None
        assert result.artifacts.producer_paths[1] is None
        # And the round-0 entry matches rounds[0].producer_paths
        assert result.artifacts.producer_paths[0] is result.artifacts.rounds[0].producer_paths

    def test_run_artifacts_run_root_findings_md_path_field(self, repo_with_pr_doc):
        """PR-8 R2.T5 + R2.T6: ``RunArtifacts.run_root_findings_md_path``
        is the canonical typed reference to ``<run_dir>/findings.md``.
        Populated when the file was written; None when no round
        produced findings.md."""
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        producer = FakeProducerAdapter()
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=1),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
            producer_adapter=producer,
        )
        assert result.exit_code == 0
        assert result.artifacts.run_root_findings_md_path is not None
        assert result.artifacts.run_root_findings_md_path.is_file()
        assert (
            result.artifacts.run_root_findings_md_path == result.artifacts.run_dir / "findings.md"
        )

    def test_run_root_findings_md_mirrors_latest_round(self, repo_with_pr_doc):
        """PR-8 R2.T5: ``<run_dir>/findings.md`` (run-root) is a
        copy of the latest round's per-round ``findings.md`` so
        the operator and the future skill bridge can address the
        active report without knowing the round number.

        Drives a multi-round NO-SHIP → producer → SHIP scenario.
        Round 0's findings.md (NO-SHIP) is overwritten by round
        1's (SHIP). Asserts the run-root copy matches round-1's
        content + sits at ``<run_dir>/findings.md``.
        """
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        producer = FakeProducerAdapter(commit_message="fix: r2.t5 test")
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=2),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_clean()),
            producer_adapter=producer,
        )
        assert result.exit_code == 0

        run_root_findings = result.artifacts.run_dir / "findings.md"
        assert run_root_findings.is_file(), (
            "PR-8 R2.T5 regression: <run_dir>/findings.md is "
            "missing — the run-root convenience copy of the latest "
            "round's findings.md."
        )

        round1_findings = result.artifacts.run_dir / "round-1" / "findings.md"
        assert round1_findings.is_file()
        # The run-root copy mirrors round-1's per-round
        # findings.md (the latest round to write one).
        assert run_root_findings.read_text() == round1_findings.read_text()
        # And distinct from round-0's findings (NO-SHIP vs SHIP).
        round0_findings = result.artifacts.run_dir / "round-0" / "findings.md"
        assert round0_findings.is_file()
        assert run_root_findings.read_text() != round0_findings.read_text()

    def test_run_root_findings_md_present_after_round_0(self, repo_with_pr_doc):
        """PR-8 R2.T5: even on a single-round SHIP run, the
        run-root findings.md is written — it's the canonical
        address for "the current findings" regardless of round
        count."""
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        producer = FakeProducerAdapter()
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=3),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_clean()),
            producer_adapter=producer,
        )
        assert result.exit_code == 0
        run_root_findings = result.artifacts.run_dir / "findings.md"
        assert run_root_findings.is_file()
        round0_findings = result.artifacts.run_dir / "round-0" / "findings.md"
        assert run_root_findings.read_text() == round0_findings.read_text()
