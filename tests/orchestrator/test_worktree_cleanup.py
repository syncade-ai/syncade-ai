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
from pathlib import Path

import pytest

from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.config import SyncadeConfig
from syncade.orchestrator import run_review
from syncade.worktree import WorktreeError
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _RoundCyclingSynth,  # noqa: F401  (resolved via sys.modules[__name__] in a test)
    _ship,
    _synth_clean,
    _synth_with_blocker,
    _two_reviewer_config,
)

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestTestLegWorktreeProvisioningFailure:
    """T1.3: when reviewer dispatch and synth both succeed but the
    test-leg worktree provisioning fails, run_review must:

    1. Write manifest.json, summary.md, findings.md BEFORE escaping.
    2. Re-raise the WorktreeError so the CLI maps to exit 60.
    3. NOT write test-run.* files (the leg never ran).
    4. Record ``test_skip_reason="test_worktree_error"`` and the
       captured ``WorktreeError`` on the result (well, on the
       RunResult that would be returned if the orchestrator didn't
       re-raise — we check via the on-disk manifest instead).

    Without this fix, a downstream worktree provisioning hiccup
    would lose the operator's reviewer + synth work entirely.
    """

    def test_test_worktree_failure_preserves_manifest_summary_findings(
        self, repo_with_pr_doc, monkeypatch
    ):
        """Force ``manager.create("tests", ...)`` to raise after the
        first two reviewer worktrees provision successfully. Assert:
        the three artifacts exist on disk before WorktreeError
        propagates out, exit code in manifest is 60, summary.md
        flags the test-worktree-error skip reason, and test-run.*
        files are absent."""
        repo, pr_doc = repo_with_pr_doc

        # Monkeypatch WorktreeManager.create to raise WorktreeError
        # only when called with the reserved "tests" name. Reviewer
        # worktrees provision normally.
        from syncade.orchestrator import _TEST_WORKTREE_NAME
        from syncade.worktree import WorktreeManager

        real_create = WorktreeManager.create

        def patched_create(self, reviewer_name, commit_sha, strip_files=None):
            if reviewer_name == _TEST_WORKTREE_NAME:
                raise WorktreeError("test injection: simulated test-worktree provisioning failure")
            return real_create(
                self,
                reviewer_name=reviewer_name,
                commit_sha=commit_sha,
                strip_files=strip_files,
            )

        monkeypatch.setattr(WorktreeManager, "create", patched_create)

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]

        # Build a config with test_command set so the test leg
        # fires. The synth fake returns a clean output so the
        # test-leg precondition is satisfied.
        cfg = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            # PR-8 single-pass back-compat
            loop={"test_command": "echo would-have-run", "max_rounds": 1},
        )

        with pytest.raises(WorktreeError, match="test injection"):
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=cfg,
                adapter_factory=_factory_returning(*adapters),
                synthesizer_adapter=FakeSynthesizerAdapter(),
            )

        # The orchestrator re-raised AFTER writing artifacts. Find
        # the run directory that was created (the most recent one
        # in <repo>/.syncade/runs/).
        runs_dir = repo / ".syncade" / "runs"
        assert runs_dir.is_dir()
        run_dirs = sorted(runs_dir.glob("*"))
        # .gitignore is in there too; pick the actual run directory.
        run_dirs = [p for p in run_dirs if p.is_dir()]
        assert len(run_dirs) >= 1, f"no run directory created: {list(runs_dir.iterdir())}"
        round_dir = run_dirs[-1] / "round-0"

        # 1. All three artifact files exist.
        assert (round_dir / "manifest.json").is_file(), "manifest.json not preserved"
        assert (round_dir / "summary.md").is_file(), "summary.md not preserved"
        assert (round_dir / "findings.md").is_file(), "findings.md not preserved"

        # 2. Manifest round_exit_code is 60 (matches what the CLI
        # returns). PR-8 polish R1.T1: key was ``exit_code`` pre-PR-8.
        manifest = json.loads((round_dir / "manifest.json").read_text())
        assert manifest["round_exit_code"] == 60

        # 3. test-run.* files NOT written (the leg never ran).
        for suffix in ("stdout", "stderr", "exit-code.txt"):
            assert not (round_dir / f"test-run.{suffix}").exists(), (
                f"test-run.{suffix} was written for a leg that never ran"
            )
        assert manifest["test_run"] is None, (
            f"manifest.test_run must be null when the leg didn't run; got {manifest['test_run']!r}"
        )

        # 4. summary.md flags the test-worktree-error skip reason
        # with the explicit T2.8 message.
        summary = (round_dir / "summary.md").read_text()
        ts_section = summary[summary.find("## Test Suite") : summary.find("## Next steps")]
        assert "worktree could not be provisioned" in ts_section.lower(), (
            f"summary.md Test Suite section did not name the worktree-error "
            f"skip reason; got:\n{ts_section}"
        )

        # R2.T2.9: Next-steps for this path must use the
        # test-worktree-error variant (NOT the generic reviewer-
        # worktree variant that points at .error.txt files that
        # don't exist here).
        next_steps = summary[summary.find("## Next steps") :]
        assert "test re-run leg's worktree could not be provisioned" in next_steps.lower(), (
            f"exit-60 Next-steps did not use the test-worktree-error variant; got:\n{next_steps}"
        )
        # The variant must explicitly note that reviewer + synth
        # artifacts ARE valid (the operator can read them). The
        # rendered text wraps across lines so check via flattened
        # whitespace.
        flattened = " ".join(next_steps.lower().split())
        assert "reviewer + synthesizer artifacts are valid" in flattened
        # And must NOT direct the operator at .error.txt files
        # (those don't exist for the test-worktree-error path —
        # the reviewer/synth phases succeeded).
        assert ".error.txt" not in next_steps

        # 5. R2.T1.2: findings.md headline must say ABORT, NOT SHIP.
        # Pre-R2.T1.2, _compute_findings_md_verdict was called
        # without test_skip_reason, so the test_result-is-None
        # path returned SHIP — directly contradicting the exit
        # 60 in manifest.json. The verdict now reflects the
        # leg-couldn't-run state.
        findings = (round_dir / "findings.md").read_text()
        head = "\n".join(findings.splitlines()[:5])
        assert "**Verdict:** ABORT" in head, (
            f"findings.md must say Verdict: ABORT on test-worktree-error "
            f"(matches exit 60, not exit 0). Got headline:\n{head}"
        )
        assert "**Verdict:** SHIP" not in head, (
            "findings.md says SHIP on test-worktree-error — directly "
            "contradicts the exit 60 in manifest.json"
        )

        # 6. R2.T1.3: reviewer worktrees must be PRESERVED on
        # this exception path so the operator can inspect what
        # each reviewer saw — same contract as any other
        # exception exit from the `with WorktreeManager` block.
        # Pre-R2.T1.3 we re-raised AFTER the with block, by
        # which time cleanup_all had already wiped the
        # worktrees.
        run_id = run_dirs[-1].name
        tmp_run_dir = SyncadeConfig().worktree_base / run_id
        assert tmp_run_dir.exists(), (
            f"reviewer worktrees were wiped on test-worktree-error path; "
            f"expected <worktree_base>/{run_id}/ to survive for inspection. "
            f"Violates the WorktreeManager preserve-on-exception contract."
        )
        # PR-8: worktrees live at ``<tmp_run_dir>/round-N/<reviewer>/``
        # — the per-round WorktreeManager nests them under round-N/.
        # Pre-PR-8 they were flat under <tmp_run_dir>; the per-round
        # nesting prevents round-1's "rv1" from colliding with
        # round-0's "rv1" worktree path. Both worktrees still
        # survive the exception path.
        round_0_tmp = tmp_run_dir / "round-0"
        assert round_0_tmp.exists()
        assert (round_0_tmp / "rv1").exists()
        assert (round_0_tmp / "rv2").exists()

    def test_persist_failure_chains_with_test_worktree_error(self, repo_with_pr_doc, monkeypatch):
        """R2.T3.12: when test-leg worktree provisioning failure
        AND a downstream persist_* failure both occur, the
        original WorktreeError must be visible via Python
        exception chaining (``raise X from Y``). Pre-R2.T3.12
        the persist failure silently swallowed the captured
        WorktreeError — the operator only saw the persist
        failure with no causal context."""
        repo, pr_doc = repo_with_pr_doc

        from syncade.orchestrator import _TEST_WORKTREE_NAME
        from syncade.worktree import WorktreeManager

        # Force the test worktree to fail.
        real_create = WorktreeManager.create

        def patched_create(self, reviewer_name, commit_sha, strip_files=None):
            if reviewer_name == _TEST_WORKTREE_NAME:
                raise WorktreeError("upstream worktree failure")
            return real_create(
                self,
                reviewer_name=reviewer_name,
                commit_sha=commit_sha,
                strip_files=strip_files,
            )

        # Force persistence to fail (monkeypatch persist_findings_md
        # to raise). The test_worktree_error has already been
        # captured by this point; the persistence failure should
        # propagate WITH the original WorktreeError as cause.
        import syncade.orchestrator.round as round_module

        def boom_findings(*a, **kw):
            raise RuntimeError("simulated persistence failure")

        monkeypatch.setattr(WorktreeManager, "create", patched_create)
        monkeypatch.setattr(round_module, "persist_findings_md", boom_findings)

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        cfg = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            # PR-8 single-pass back-compat
            loop={"test_command": "true", "max_rounds": 1},
        )

        with pytest.raises(RuntimeError, match="simulated persistence failure") as exc_info:
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=cfg,
                adapter_factory=_factory_returning(*adapters),
                synthesizer_adapter=FakeSynthesizerAdapter(),
            )

        # __cause__ must be the original WorktreeError (via
        # ``raise persist_exc from test_worktree_error``).
        assert exc_info.value.__cause__ is not None, (
            "R2.T3.12: persistence failure must chain from the "
            "captured WorktreeError; got __cause__=None — the "
            "upstream worktree error was silently dropped"
        )
        assert isinstance(exc_info.value.__cause__, WorktreeError), (
            f"R2.T3.12: __cause__ should be the captured WorktreeError; "
            f"got {type(exc_info.value.__cause__).__name__}"
        )
        assert "upstream worktree failure" in str(exc_info.value.__cause__)

    def test_logger_testing_does_not_fire_before_worktree_provisioning(
        self, repo_with_pr_doc, monkeypatch, capsys
    ):
        """The "running test re-run leg" event must not fire when test
        worktree provisioning fails — the leg never actually ran."""
        repo, pr_doc = repo_with_pr_doc

        from syncade.orchestrator import _TEST_WORKTREE_NAME
        from syncade.worktree import WorktreeManager

        real_create = WorktreeManager.create

        def patched(self, reviewer_name, commit_sha, strip_files=None):
            if reviewer_name == _TEST_WORKTREE_NAME:
                raise WorktreeError("simulated test-worktree failure")
            return real_create(
                self,
                reviewer_name=reviewer_name,
                commit_sha=commit_sha,
                strip_files=strip_files,
            )

        monkeypatch.setattr(WorktreeManager, "create", patched)

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        cfg = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            # PR-8 single-pass back-compat
            loop={"test_command": "echo would-have-run", "max_rounds": 1},
        )

        with pytest.raises(WorktreeError):
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=cfg,
                adapter_factory=_factory_returning(*adapters),
                synthesizer_adapter=FakeSynthesizerAdapter(),
            )

        captured = capsys.readouterr()
        # The "running test re-run leg" log line must NOT have
        # fired, since the leg's worktree didn't provision.
        assert "running test re-run leg" not in captured.out
        # The "test re-run skipped" line SHOULD have fired,
        # naming the provisioning failure.
        assert "test re-run skipped" in captured.out
        assert "test worktree provisioning failed" in captured.out

    def test_final_round_test_worktree_error_warns_and_cleans_prior_rounds(
        self, repo_with_pr_doc, monkeypatch, capsys
    ):
        """N3 + N4: a multi-round run where round 0 NO-SHIPs, the producer
        commits (advancing the branch), and round 1 would SHIP but hits a
        test-worktree provisioning failure.

        The early ``test_worktree_error`` re-raise must NOT bypass:
          * N3 — the branch-advance "working tree not synced" warning, since
            round 0's producer moved the branch ref.
          * N4 — cleanup of every PRIOR round's reviewer/producer worktrees;
            only the current (failed) round's worktrees stay for diagnostics.
        """
        from syncade.adapters.fake import FakeProducerAdapter
        from syncade.orchestrator import _TEST_WORKTREE_NAME
        from syncade.worktree import WorktreeManager

        # Fail ONLY the test-leg worktree. Round 0's test leg is skipped
        # (active blocker), so this fires for the first time in round 1.
        real_create = WorktreeManager.create

        def patched_create(self, reviewer_name, commit_sha, strip_files=None):
            if reviewer_name == _TEST_WORKTREE_NAME:
                raise WorktreeError("test injection: simulated test-worktree provisioning failure")
            return real_create(
                self,
                reviewer_name=reviewer_name,
                commit_sha=commit_sha,
                strip_files=strip_files,
            )

        monkeypatch.setattr(WorktreeManager, "create", patched_create)

        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        cfg = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"test_command": "echo would-have-run", "max_rounds": 2},
        )
        producer = FakeProducerAdapter(commit_message="fix: advance branch in round 0")

        import sys

        cycling_synth_cls = sys.modules[__name__]._RoundCyclingSynth

        with pytest.raises(WorktreeError, match="test injection"):
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=cfg,
                adapter_factory=_factory_returning(*adapters),
                synthesizer_adapter=cycling_synth_cls(_synth_with_blocker(), _synth_clean()),
                producer_adapter=producer,
            )

        # N3: the branch-advance warning must have fired (logger.warning ->
        # stderr) even though the run exits via the test_worktree_error raise.
        err = capsys.readouterr().err
        assert "has been advanced to the latest producer commit" in err, (
            "N3 regression: branch-advance warning was skipped on the "
            f"test_worktree_error early-raise path; stderr was:\n{err}"
        )

        runs_dir = repo / ".syncade" / "runs"
        run_dirs = [p for p in sorted(runs_dir.glob("*")) if p.is_dir()]
        assert len(run_dirs) >= 1
        run_id = run_dirs[-1].name
        tmp_run_dir = SyncadeConfig().worktree_base / run_id

        # N4: round 0's reviewer + producer worktrees must be cleaned (they are
        # superseded — the branch already advanced past them).
        assert not (tmp_run_dir / "round-0" / "rv1").exists(), (
            "N4 regression: prior-round reviewer worktree leaked on the "
            "test_worktree_error early-raise path"
        )
        assert not (tmp_run_dir / "round-0" / "rv2").exists()
        assert not (tmp_run_dir / "round-0" / "producer-worktree").exists(), (
            "N4 regression: prior-round producer worktree leaked on the "
            "test_worktree_error early-raise path"
        )

        # ...but the CURRENT (failed) round's reviewer worktrees stay on disk
        # for the operator to inspect what each reviewer saw.
        assert (tmp_run_dir / "round-1" / "rv1").exists(), (
            "current round's worktrees must be preserved for diagnostics"
        )
        assert (tmp_run_dir / "round-1" / "rv2").exists()


class TestWorktreeCleanup:
    def test_worktrees_are_cleaned_up_on_successful_run(self, repo_with_pr_doc):
        """After a clean run (any exit code where no exception bubbled
        out of WorktreeManager's context), the /tmp/syncade/<run-id>/
        directory should no longer exist. The orchestrator's
        WorktreeManager.__exit__ on clean-exit cleans up the worktrees
        and the run_dir."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )
        run_id = result.artifacts.run_dir.name
        tmp_run_dir = SyncadeConfig().worktree_base / run_id
        assert not tmp_run_dir.exists(), f"worktree dir still present: {tmp_run_dir}"

    def test_multi_round_run_cleans_empty_round_dirs(self, repo_with_pr_doc):
        """PR-8 polish R1.T5: a multi-round run where each round
        runs both reviewers AND a producer leaves empty
        ``round-N/`` subdirs under ``<base>/<run_id>/`` because the
        reviewer-manager's cleanup_all tries to rmdir ``round-N/``
        BEFORE the producer-manager cleans its
        ``round-N/producer-worktree/``. Pre-R1.T5 these empty
        dirs persisted; the post-loop cleanup of
        ``<base>/<run_id>/`` failed (dir not empty). Now we walk
        bottom-up and rmdir every empty subdir.

        Drive a 2-round loop where round 0 NO-SHIPs + producer
        commits + round 1 SHIPs. Verify the post-loop
        ``<base>/<run_id>/`` is gone entirely."""
        from syncade.adapters.fake import FakeProducerAdapter

        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        # Build a multi-round config via the existing
        # TestMultiRoundLoop helper pattern.
        cfg = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 2},
        )
        producer = FakeProducerAdapter(commit_message="fix: cleanup test")

        # Use the same _RoundCyclingSynth helper class defined
        # later in this file (TestMultiRoundLoop). Import via the
        # module to avoid a forward-reference at parse time.
        import sys

        this_module = sys.modules[__name__]
        cycling_synth_cls = this_module._RoundCyclingSynth

        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=cfg,
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=cycling_synth_cls(_synth_with_blocker(), _synth_clean()),
            producer_adapter=producer,
        )
        assert result.exit_code == 0

        run_id = result.artifacts.run_dir.name
        tmp_run_dir = SyncadeConfig().worktree_base / run_id
        # The whole <base>/<run_id>/ tree should be cleaned —
        # including the empty round-0/ + round-1/ leftovers the
        # pre-R1.T5 code path produced.
        assert not tmp_run_dir.exists(), (
            f"PR-8 polish R1.T5 regression: multi-round run left "
            f"leftover at {tmp_run_dir}; expected clean removal. "
            f"Contents: {list(tmp_run_dir.rglob('*')) if tmp_run_dir.exists() else 'gone'}"
        )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_missing_pr_doc_raises_file_not_found(self, repo_with_pr_doc):
        repo, _ = repo_with_pr_doc
        with pytest.raises(FileNotFoundError):
            run_review(
                repo_root=repo,
                pr_doc_path=Path("/no-such-file.md"),
                config=_two_reviewer_config(),
                adapter_factory=_factory_returning(),
            )

    def test_missing_repo_root_raises_not_a_directory(self, tmp_path):
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")
        with pytest.raises(NotADirectoryError):
            run_review(
                repo_root=tmp_path / "no-such-dir",
                pr_doc_path=pr_doc,
                config=_two_reviewer_config(),
                adapter_factory=_factory_returning(),
            )
