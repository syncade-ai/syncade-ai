"""Multi-round loop orchestrator tests (giant-class split, part 3 of 3).

Moved verbatim from the former ``tests/test_orchestrator.py``:
the ``TestMultiRoundLoop`` methods covering producer-commit / detached-HEAD
termination, dirty-tree refusal in loop mode, and per-round producer artifacts.

The single ``TestMultiRoundLoop`` class is split by method group across
sibling modules (``test_multiround*.py``); each module keeps the same class
name (distinct node paths) plus the shared ``_multi_round_config`` helper.

Fixtures (``repo_with_pr_doc`` + the two autouse fixtures
``_isolated_worktree_base`` / ``_default_to_fake_synthesizer``) come from
``tests/orchestrator/conftest.py``.
"""

from __future__ import annotations

import subprocess

import pytest

from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.config import SyncadeConfig
from syncade.orchestrator import run_review
from syncade.worktree import WorktreeError
from tests.orchestrator._helpers import (
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

    def test_no_sync_warning_when_no_producer_committed(self, repo_with_pr_doc, capsys):
        """The post-loop sync-recipe warning only fires when at
        least one producer round actually committed. SHIP at
        round 0 + no producer → no warning."""
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        producer = FakeProducerAdapter()  # never runs

        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=3),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_clean()),
            producer_adapter=producer,
        )
        assert result.exit_code == 0
        err = capsys.readouterr().err
        assert "branch refs/heads/main has been advanced" not in err

    def test_detached_head_commit_terminates_instead_of_reviewing_stale_sha(
        self, repo_with_pr_doc, capsys
    ):
        """Detached HEAD at invocation means there is no branch ref to
        advance. A producer commit must terminate the loop rather than
        letting round 1 review the original stale SHA and potentially
        SHIP it."""
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        current_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        subprocess.run(["git", "checkout", "-q", current_sha], cwd=repo, check=True)

        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        producer = FakeProducerAdapter(commit_message="fix: detached test")
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=2),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_clean()),
            producer_adapter=producer,
        )
        assert result.exit_code == 30
        assert result.termination_reason == "producer_stalled"
        assert len(result.rounds) == 1
        assert result.final_round == 0

        repo_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        round0_commit = (
            (result.artifacts.run_dir / "round-0" / "producer.commit.txt").read_text().strip()
        )
        assert repo_head == current_sha
        assert round0_commit != current_sha

        err = capsys.readouterr().err
        # No branch-advance message because there's no branch to advance.
        assert "has been advanced" not in err
        assert "detached HEAD" in err

    def test_dirty_tree_refusal_in_loop_mode(self, repo_with_pr_doc):
        """max_rounds > 1 + tracked-modified working tree + no
        --force-dirty → WorktreeError (exit 60). The orchestrator
        refuses to start; reviewer/synth subprocesses never run."""
        repo, pr_doc = repo_with_pr_doc
        # Dirty the tree
        (repo / "README.md").write_text("modified\n", encoding="utf-8")

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        with pytest.raises(WorktreeError) as exc_info:
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=self._multi_round_config(max_rounds=2),
                adapter_factory=_factory_returning(*adapters),
                synthesizer_adapter=FakeSynthesizerAdapter(),
            )
        msg = str(exc_info.value)
        assert "loop mode" in msg
        assert "max_rounds=2" in msg
        assert "force-dirty" in msg or "--force-dirty" in msg

    def test_force_dirty_bypasses_loop_mode_refusal(self, repo_with_pr_doc):
        """``force_dirty=True`` bypasses the loop-mode refusal —
        the loop runs to completion despite the tracked-modified
        WIP. The warning still fires (the operator is making the
        race condition explicit, not pretending it doesn't exist)."""
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        (repo / "README.md").write_text("modified\n", encoding="utf-8")

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        # max_rounds=2 + force_dirty → loop runs; round 0 SHIPs so
        # the producer never runs (just verifying the dirty-tree
        # refusal is bypassed).
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=2),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_clean()),
            producer_adapter=FakeProducerAdapter(),
            force_dirty=True,
        )
        assert result.exit_code == 0

    def test_dirty_tree_allowed_for_untracked_only(self, repo_with_pr_doc):
        """Loop-mode refusal only fires on tracked-modified; an
        untracked-only working tree is fine (those files don't
        enter any worktree)."""
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        # Untracked file — NOT a modification to tracked file.
        (repo / "scratch.txt").write_text("untracked\n", encoding="utf-8")

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=2),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_clean()),
            producer_adapter=FakeProducerAdapter(),
        )
        assert result.exit_code == 0

    def test_max_rounds_1_does_not_refuse_dirty_tree(self, repo_with_pr_doc):
        """max_rounds=1 (single-pass back-compat) keeps PR-7.5's
        warning-only dirty-tree behavior — no refusal even when
        tracked-modified."""
        repo, pr_doc = repo_with_pr_doc
        (repo / "README.md").write_text("modified\n", encoding="utf-8")

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=1),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        # No refusal — runs to completion with exit 0
        assert result.exit_code == 0

    def test_producer_artifacts_persisted_per_round(self, repo_with_pr_doc):
        """Each round that ran a producer has producer.{stdout,stderr,
        commit.txt} in its round-N/ dir. Producer artifacts
        in round-N/ correspond to round N's NO-SHIP → fix
        attempt."""
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
        ]
        producer = FakeProducerAdapter(commit_message="fix: try again")
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=3),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(
                _synth_with_blocker(),
                _synth_with_blocker(),
                _synth_with_blocker(),
            ),
            producer_adapter=producer,
        )
        assert result.exit_code == 20  # max_rounds_reached
        assert len(result.rounds) == 3
        # Each round that ran a producer has artifacts in its dir.
        # Rounds 0 and 1 ran producers (rounds 0 and 1 NO-SHIPped
        # with rounds remaining); round 2 is the last round (no
        # producer fired after it).
        for n in (0, 1):
            round_dir = result.artifacts.run_dir / f"round-{n}"
            assert (round_dir / "producer.stdout").exists()
            assert (round_dir / "producer.commit.txt").exists()
        # Round 2 (the final round): no producer.* files
        round2 = result.artifacts.run_dir / "round-2"
        assert not (round2 / "producer.stdout").exists()
