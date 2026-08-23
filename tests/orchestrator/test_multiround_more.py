"""Orchestrator multi-round loop tests — part 2 of 3.

The worktree-preservation + branch-advance methods of ``TestMultiRoundLoop``
(split out of the giant single class). Same class name as
``test_multiround.py`` (intentional — distinct node paths).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from syncade.adapters.fake import FakeAdapter
from syncade.adapters.producer import ProducerOutput
from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from syncade.orchestrator.branch_advance import _advance_branch_ref
from syncade.process import SubprocessResult
from syncade.producer import ProducerResult
from syncade.snapshot import Snapshot
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


def test_advance_branch_ref_routes_git_calls_through_run_subprocess_with_timeout(monkeypatch):
    calls = []

    def fake_run_subprocess(argv, *, cwd=None, env=None, timeout=None, input_text=None):
        del env, input_text
        calls.append((argv, cwd, timeout))
        return SubprocessResult(0, "", "", 0.0)

    monkeypatch.setattr(
        "syncade.orchestrator.branch_advance.run_subprocess",
        fake_run_subprocess,
        raising=False,
    )
    repo_root = Path("/tmp/syncade-test-repo")
    starting_sha = "a" * 40
    ending_sha = "b" * 40
    snapshot = Snapshot(
        repo_root=repo_root,
        commit_sha=starting_sha,
        branch="main",
        base_ref=None,
        diff_text="",
        dirty_state="clean",
    )
    producer_result = ProducerResult(
        outcome="committed",
        starting_sha=starting_sha,
        ending_sha=ending_sha,
        duration_seconds=0.0,
        output=ProducerOutput(narrative_text="committed"),
        error=None,
        raw_subprocess_result=None,
    )

    status = _advance_branch_ref(
        repo_root=repo_root,
        snapshot=snapshot,
        producer_result=producer_result,
        logger=Logger("quiet"),
    )

    assert status == "advanced"
    expected_ancestor_argv = [
        "git",
        "--no-replace-objects",
        "merge-base",
        "--is-ancestor",
        starting_sha,
        ending_sha,
    ]
    assert calls == [
        (expected_ancestor_argv, repo_root, 30.0),
        (["git", "update-ref", "refs/heads/main", ending_sha, starting_sha], repo_root, 30.0),
    ]


def test_advance_branch_ref_replace_ref_cannot_bypass_ancestor_check(tmp_path):
    """A refs/replace/* on the producer's ending SHA must not trick the
    is-ancestor check into accepting a non-descendant commit.

    Attack: replace ending_sha with a fake commit whose parent IS starting_sha.
    Without --no-replace-objects git sees ending_sha as a descendant of
    starting_sha and allows the fast-forward; the real (unrelated) ending_sha
    then lands on the branch.  With the flag the real graph is used."""
    import os

    _ENV = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def _g(cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **_ENV},
        ).stdout.strip()

    repo = tmp_path / "repo"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "main")
    _g(repo, "config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("A\n")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-m", "A")
    starting_sha = _g(repo, "rev-parse", "HEAD")

    # Create an orphan commit unrelated to main — this will be ending_sha.
    _g(repo, "checkout", "--orphan", "orphan")
    _g(repo, "rm", "-rf", "--cached", ".")
    (repo / "orphan.txt").write_text("orphan\n")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-m", "orphan")
    ending_sha = _g(repo, "rev-parse", "HEAD")
    _g(repo, "checkout", "-q", "main")

    # Build a fake commit whose parent IS starting_sha and replace ending_sha with it.
    fake_tree = _g(repo, "rev-parse", "HEAD^{tree}")
    fake_commit = _g(repo, "commit-tree", fake_tree, "-p", starting_sha, "-m", "fake")
    _g(repo, "update-ref", f"refs/replace/{ending_sha}", fake_commit)

    snapshot = Snapshot(
        repo_root=repo,
        commit_sha=starting_sha,
        branch="main",
        base_ref=None,
        diff_text="",
        dirty_state="clean",
    )
    pr = ProducerResult(
        outcome="committed",
        starting_sha=starting_sha,
        ending_sha=ending_sha,
        duration_seconds=0.0,
        output=ProducerOutput(narrative_text=""),
        error=None,
        raw_subprocess_result=None,
    )

    status = _advance_branch_ref(
        repo_root=repo, snapshot=snapshot, producer_result=pr, logger=Logger("quiet")
    )
    assert status == "non_descendant", (
        f"replacement ref bypassed the ancestor check: got {status!r}"
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

    def test_stalled_producer_preserves_worktree_for_inspection(self, repo_with_pr_doc):
        """PR-8 R2.T3 + PRD: ``On exit 20 / 30 / 10: keep worktrees
        for inspection``. A stalled producer terminates the loop
        with exit 30 + producer_stalled — the producer's worktree
        MUST stay on disk so the operator can inspect what (if
        anything) the producer wrote before deciding not to commit.
        """
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
        ]
        # Stalled producer (no commit_message → no fixture commit)
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

        # PR-8 R2.T3: producer worktree dir preserved on exit 30
        # so the operator can inspect what was tried.
        run_id = result.artifacts.run_dir.name
        producer_worktree_dir = (
            SyncadeConfig().worktree_base / run_id / "round-0" / "producer-worktree" / "producer"
        )
        assert producer_worktree_dir.exists(), (
            f"PR-8 R2.T3 regression: stalled producer's worktree "
            f"was cleaned up but PRD says exit 30 keeps worktrees "
            f"for inspection. Expected: {producer_worktree_dir}"
        )
        assert (producer_worktree_dir / ".git").is_dir()
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=producer_worktree_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert (producer_worktree_dir / common).resolve() == (
            producer_worktree_dir / ".git"
        ).resolve()
        assert (
            subprocess.run(
                ["git", "remote"],
                cwd=producer_worktree_dir,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            == ""
        )

    def test_max_rounds_reached_preserves_worktrees(self, repo_with_pr_doc):
        """PR-8 R2.T3: exit 20 (max_rounds_reached) also preserves
        worktrees per PRD."""
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
        ]
        producer = FakeProducerAdapter(commit_message="fix: r2.t3 max-rounds")
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=2),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_with_blocker()),
            producer_adapter=producer,
        )
        assert result.exit_code == 20
        assert result.termination_reason == "max_rounds_reached"

        run_id = result.artifacts.run_dir.name
        run_dir = SyncadeConfig().worktree_base / run_id
        assert run_dir.exists(), (
            f"PR-8 R2.T3 regression: exit 20 should preserve "
            f"worktrees per PRD; got cleanup at {run_dir}"
        )
        # Round 0's producer worktree preserved (producer ran
        # between round 0 and round 1).
        wt0 = run_dir / "round-0" / "producer-worktree" / "producer"
        assert wt0.exists(), (
            f"PR-8 R2.T3 regression: round 0 producer worktree missing under preserved tree: {wt0}"
        )
        # Round 1 is the LAST round — no producer ran after it
        # (the loop terminates immediately at max_rounds_reached
        # without dispatching a producer phase). So no
        # round-1/producer-worktree/ exists. But the reviewer
        # worktrees DO exist.
        for r_name in ("rv1", "rv2"):
            wt = run_dir / "round-1" / r_name
            assert wt.exists(), (
                f"PR-8 R2.T3 regression: round 1 reviewer worktree "
                f"{r_name} missing under preserved tree: {wt}"
            )

    def test_non_descendant_producer_commit_terminates_loop(self, repo_with_pr_doc, capsys):
        """PR-8 R2.T2 regression: when the producer's ending SHA
        is NOT a descendant of the starting SHA, the orchestrator
        MUST terminate the loop with exit 30 + producer_stalled.
        Without this guard, the next round's snapshot would read
        the original (unadvanced) branch SHA and dispatch
        reviewers against stale code — potentially declaring
        SHIP on code that the producer was supposed to fix.

        Drive this by having the fake producer create an ORPHAN
        commit (`git commit-tree` style, no parent) so it's not a
        descendant of the round-start SHA. The orchestrator's
        ``git merge-base --is-ancestor starting ending`` check
        returns non-zero → ``_advance_branch_ref`` returns
        ``non_descendant`` → loop terminates.
        """
        from syncade.adapters.producer import ProducerOutput

        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            # Round 1 reviewers should NEVER be reached (loop
            # terminates after round 0's branch-advance failure).
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ]

        # Custom producer that returns an orphan commit SHA (not
        # a descendant of starting_sha). We do this by writing a
        # real orphan commit to the producer worktree's repo
        # state, then claiming THAT SHA as ending_sha. The
        # orchestrator's ancestor check will detect the orphan.

        class _OrphanCommitProducer:
            """Fake adapter that simulates a producer doing
            ``git reset --hard <unrelated>``: returns an orphan
            commit SHA from build_invocation's side effect, then
            run_producer's stall-detection picks up ending_sha
            via ``git rev-parse HEAD`` in the worktree."""

            name = "fake-producer"

            def __init__(self):
                self.invocations = []
                self.parse_output_calls = 0
                self.check_auth_calls = 0

            def build_invocation(self, producer_config, worktree_path, prompt):
                import subprocess as sp

                self.invocations.append((producer_config, worktree_path, prompt))
                # Create an orphan commit in the producer worktree:
                # checkout to a new orphan branch, commit, return.
                # The new HEAD will be a SHA NOT descended from
                # the round-start SHA.
                sp.run(
                    ["git", "checkout", "--orphan", "orphan-branch"],
                    cwd=worktree_path,
                    check=True,
                    capture_output=True,
                )
                (worktree_path / "ORPHAN").write_text("orphan content\n")
                sp.run(["git", "add", "-A"], cwd=worktree_path, check=True, capture_output=True)
                sp.run(
                    [
                        "git",
                        "-c",
                        "user.email=orphan@test",
                        "-c",
                        "user.name=Orphan",
                        "commit",
                        "-m",
                        "orphan: not a descendant of starting_sha",
                    ],
                    cwd=worktree_path,
                    check=True,
                    capture_output=True,
                )
                import os
                import shutil as _shutil

                from syncade.adapters.base import Invocation

                resolved = _shutil.which("true") or "true"
                return Invocation(
                    argv=[resolved],
                    cwd=worktree_path,
                    env=dict(os.environ),
                    stdin_text=None,
                    timeout_seconds=None,
                )

            def parse_output(self, result):
                self.parse_output_calls += 1
                return ProducerOutput(narrative_text="orphan-commit producer")

            def check_auth(self):
                self.check_auth_calls += 1

        branch_ref = _current_branch_ref(repo)
        branch_before = subprocess.run(
            ["git", "rev-parse", branch_ref],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        producer = _OrphanCommitProducer()
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=3),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(
                _synth_with_blocker(),
                _synth_clean(),  # would-be round 1; should never run
                _synth_clean(),  # would-be round 2; should never run
            ),
            producer_adapter=producer,
        )

        # The loop MUST terminate with producer_stalled (exit 30)
        # because the orphan commit isn't a descendant.
        assert result.exit_code == 30, (
            f"PR-8 R2.T2 regression: non-descendant producer commit "
            f"must terminate loop with exit 30; got {result.exit_code} + "
            f"reason {result.termination_reason!r}"
        )
        assert result.termination_reason == "producer_stalled"
        # Only ONE round ran — the loop didn't continue past the
        # branch-advance failure.
        assert len(result.rounds) == 1
        assert result.final_round == 0

        branch_after = subprocess.run(
            ["git", "rev-parse", branch_ref],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert branch_after == branch_before, (
            "PR-8 R2.T2 regression: branch advanced on a non-"
            "descendant producer SHA (refused-but-loop-continued "
            "is the stale-SHIP bug this test pins against)."
        )

        # The warning + producer_stalled diagnostic appear in stderr.
        err = capsys.readouterr().err
        assert "NOT a descendant" in err
        assert "producer_stalled" in err or "stall" in err.lower()
        assert "has been advanced to the latest producer commit" not in err

    def test_working_tree_NOT_synced_after_branch_advance(self, repo_with_pr_doc, capsys):
        """PR-8 R2.T1 (REVERSES R1.T6): the brief is explicit —
        ``do not use `git checkout` in the user's working tree
        (don't touch their checkout)``. The orchestrator advances
        the branch ref via ``git update-ref``; the operator's
        working tree is NOT updated. Operator must sync themselves
        post-loop (the loop emits a warning naming the sync
        recipe).

        Same scenario as the pre-R2.T1 test, but inverted: the
        producer's fixture file should NOT appear in repo_root
        because the orchestrator never touched the operator's
        checkout. The branch ref DID advance — `git log` shows
        the producer's commit.
        """
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        producer = FakeProducerAdapter(commit_message="fix: r2.t1 test")

        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=2),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_clean()),
            producer_adapter=producer,
        )
        assert result.exit_code == 0

        # PR-8 invariant: orchestrator does NOT touch the user's
        # checkout. The fake producer's fixture file lives in the
        # producer's /tmp worktree (now cleaned up), not in
        # repo_root.
        assert not (repo / ".syncade-fake-producer-0").exists(), (
            "PR-8 R2.T1 spec violation: orchestrator touched the "
            "operator's working tree (the producer's fixture file "
            "appeared in repo_root, meaning the working tree was "
            "synced — but the PR-8 brief explicitly forbids "
            "modifying the user's checkout)."
        )

        # The branch ref DID advance — verify.
        branch_ref = _current_branch_ref(repo)
        new_head = subprocess.run(
            ["git", "rev-parse", branch_ref],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        round0_commit = (
            (result.artifacts.run_dir / "round-0" / "producer.commit.txt").read_text().strip()
        )
        assert new_head == round0_commit

        # And the orchestrator emitted the post-run warning
        # telling the operator how to sync manually.
        err = capsys.readouterr().err
        assert f"branch {branch_ref} has been advanced" in err
        assert "git reset --hard HEAD" in err or "stash" in err.lower()
