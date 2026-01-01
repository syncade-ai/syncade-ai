"""run_status is wired into run_review: the breadcrumb tracks phases and is
finalized with the run's real reason + exit code on the normal path."""

from __future__ import annotations

import json
import signal as _signal_mod
import subprocess
import unittest.mock

import pytest

from syncade import run_status
from syncade.adapters.fake import FakeAdapter, FakeProducerAdapter
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
    _two_reviewer_config,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def test_run_review_writes_and_finalizes_status(repo_with_pr_doc):
    run_status.clear_active()
    repo, pr_doc = repo_with_pr_doc
    adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_two_reviewer_config(),
        adapter_factory=_factory_returning(*adapters),
    )
    status = json.loads((result.artifacts.run_dir / "status.json").read_text())
    assert status["state"] == "terminated"
    assert status["reason"] == result.termination_reason  # "ship" (Literal str)
    assert status["exit_code"] == result.exit_code  # 0
    # the last phase before SHIP was the round-0 checking leg (synth → check → finalize)
    assert status["phase"] == "round-0: checking"
    assert run_status.active() is None  # finalize cleared it
    run_status.clear_active()


def test_direct_run_review_exception_finalizes_status(repo_with_pr_doc, tmp_path):
    """run_review finalizes status on uncaught exception; direct API callers
    must never leave a stale running breadcrumb (no CLI wrapper present)."""
    run_status.clear_active()
    repo, pr_doc = repo_with_pr_doc
    adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]

    # Inject a failure INSIDE run_review after begin() by patching _finalize_run
    # to raise. This simulates any unexpected exception that escapes the round loop.
    boom = RuntimeError("injected post-begin failure")
    import syncade.orchestrator.loop as loop_mod

    with unittest.mock.patch.object(loop_mod, "_finalize_run", side_effect=boom):
        with pytest.raises(RuntimeError, match="injected post-begin failure"):
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=_two_reviewer_config(),
                adapter_factory=_factory_returning(*adapters),
                worktree_base=tmp_path,
            )

    # status.json must exist and be terminated (not running).
    runs_root = repo / ".syncade" / "runs"
    run_dirs = [d for d in runs_root.iterdir() if d.is_dir()]
    assert run_dirs, "no run dir was created"
    status = json.loads((run_dirs[0] / "status.json").read_text())
    assert status["state"] == "terminated"
    assert status["reason"] == "exception:RuntimeError"
    assert run_status.active() is None
    run_status.clear_active()


def test_synth_check_phases_recorded_in_status(repo_with_pr_doc):
    """synthesizing and checking phases are written to status.json, so a hard
    kill during those phases reports the correct breadcrumb (not 'reviewing')."""
    run_status.clear_active()
    repo, pr_doc = repo_with_pr_doc
    observed_phases: list[str] = []

    real_update_phase = run_status.update_phase

    def _spy_update_phase(phase, round_index=None):
        observed_phases.append(phase)
        real_update_phase(phase, round_index)

    adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
    with unittest.mock.patch.object(run_status, "update_phase", side_effect=_spy_update_phase):
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )

    assert "round-0: synthesizing" in observed_phases
    assert "round-0: checking" in observed_phases
    run_status.clear_active()


def test_signal_inside_run_review_preserves_breadcrumb_for_cli(repo_with_pr_doc):
    """A signal-induced KeyboardInterrupt inside run_review must NOT finalize
    status.json as exception:KeyboardInterrupt — the breadcrumb must stay
    'running' so the CLI's finalize_signal() can write signal:SIGTERM + 143."""
    run_status.clear_active()
    run_status._received_signal = None
    repo, pr_doc = repo_with_pr_doc

    real_update_phase = run_status.update_phase
    fired = [False]

    def _inject_signal(phase, round_index=None):
        real_update_phase(phase, round_index)
        if not fired[0] and phase == "round-0: reviewing":
            fired[0] = True
            run_status._received_signal = int(_signal_mod.SIGTERM)
            raise KeyboardInterrupt

    adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
    with unittest.mock.patch.object(run_status, "update_phase", side_effect=_inject_signal):
        with pytest.raises(KeyboardInterrupt):
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=_two_reviewer_config(),
                adapter_factory=_factory_returning(*adapters),
            )

    runs_root = repo / ".syncade" / "runs"
    run_dirs = [d for d in runs_root.iterdir() if d.is_dir()]
    assert run_dirs, "no run dir was created"
    status = json.loads((run_dirs[0] / "status.json").read_text())
    # run_review must NOT have finalized with exception:KeyboardInterrupt
    assert status["state"] == "running", (
        "run_review finalized the breadcrumb before the CLI signal handler "
        "could; status must stay 'running' so finalize_signal() writes "
        "signal:SIGTERM"
    )

    # Simulate the CLI's KeyboardInterrupt handler
    exit_code = run_status.finalize_signal()
    assert exit_code == 128 + int(_signal_mod.SIGTERM)
    status2 = json.loads((run_dirs[0] / "status.json").read_text())
    assert status2["state"] == "terminated"
    assert status2["reason"] == "signal:SIGTERM"
    assert status2["exit_code"] == 128 + int(_signal_mod.SIGTERM)

    run_status.clear_active()
    run_status._received_signal = None


def test_worktree_error_after_begin_finalized_with_exit_code_60(repo_with_pr_doc):
    """A WorktreeError raised inside run_review after begin() must finalize
    status.json with exit_code=60, not null — so status.json matches the
    CLI's mapped exit code for both direct API callers and CLI callers."""
    run_status.clear_active()
    repo, pr_doc = repo_with_pr_doc

    import syncade.orchestrator.loop as loop_mod

    def _raise_wte(**kwargs):
        raise WorktreeError("injected post-begin worktree failure")

    with unittest.mock.patch.object(loop_mod, "_run_round_step", side_effect=_raise_wte):
        with pytest.raises(WorktreeError):
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=_two_reviewer_config(),
                adapter_factory=_factory_returning(
                    FakeAdapter(canned_output=_ship()),
                    FakeAdapter(canned_output=_ship()),
                ),
            )

    runs_root = repo / ".syncade" / "runs"
    run_dirs = [d for d in runs_root.iterdir() if d.is_dir()]
    assert run_dirs, "no run dir was created"
    status = json.loads((run_dirs[0] / "status.json").read_text())
    assert status["state"] == "terminated"
    assert status["reason"] == "exception:WorktreeError"
    assert status["exit_code"] == 60, (
        "WorktreeError after begin() must finalize with exit_code=60 so "
        "status.json is not mistaken for a hard SIGKILL (exit_code=null)"
    )
    run_status.clear_active()


def _expect_finalized(repo, reason, exit_code, exc_type, **run_kwargs):
    """Patch _run_round_step to raise, run run_review, and return the finalized status."""
    import syncade.orchestrator.loop as loop_mod

    with unittest.mock.patch.object(
        loop_mod, "_run_round_step", side_effect=run_kwargs.pop("_raise")
    ):
        with pytest.raises(exc_type):
            run_review(**run_kwargs)
    run_dirs = [d for d in (repo / ".syncade" / "runs").iterdir() if d.is_dir()]
    assert run_dirs, "no run dir was created"
    status = json.loads((run_dirs[0] / "status.json").read_text())
    assert status["state"] == "terminated"
    assert status["reason"] == reason
    assert status["exit_code"] == exit_code
    run_status.clear_active()


def test_snapshot_error_after_begin_finalized_with_exit_code_60(repo_with_pr_doc):
    """A post-begin SnapshotError (the CLI maps it to 60) must finalize status.json
    with exit_code=60, not null — else durable status disagrees with the exit."""
    run_status.clear_active()
    repo, pr_doc = repo_with_pr_doc
    from syncade.snapshot import SnapshotError

    def _raise(**kwargs):
        raise SnapshotError("injected post-begin snapshot failure")

    _expect_finalized(
        repo,
        "exception:SnapshotError",
        60,
        SnapshotError,
        _raise=_raise,
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_two_reviewer_config(),
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_ship()), FakeAdapter(canned_output=_ship())
        ),
    )


def test_resume_error_after_begin_finalized_with_exit_code_60(repo_with_pr_doc):
    """A post-begin ResumeError must finalize with exit_code=60 (maps to 60 in the CLI)."""
    run_status.clear_active()
    repo, pr_doc = repo_with_pr_doc
    from syncade.orchestrator.resume_types import ResumeError

    def _raise(**kwargs):
        raise ResumeError("injected post-begin resume failure")

    _expect_finalized(
        repo,
        "exception:ResumeError",
        60,
        ResumeError,
        _raise=_raise,
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_two_reviewer_config(),
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_ship()), FakeAdapter(canned_output=_ship())
        ),
    )


def test_system_exit_after_begin_finalizes_status(repo_with_pr_doc):
    """A parent-side SystemExit after begin() must NOT leave status.json `running` —
    the BaseException finalizer catches it (the except Exception guard does not)."""
    run_status.clear_active()
    repo, pr_doc = repo_with_pr_doc

    def _raise(**kwargs):
        raise SystemExit(3)

    _expect_finalized(
        repo,
        "exception:SystemExit",
        None,
        SystemExit,
        _raise=_raise,
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_two_reviewer_config(),
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_ship()), FakeAdapter(canned_output=_ship())
        ),
    )


def test_resume_begins_breadcrumb_before_drift_check(repo_with_pr_doc):
    """Bug (unanimous): in resume mode a signal after the run dir is known but
    before begin() left the reused run's OLD status.json stale while stderr claimed
    the signal was recorded. begin() must run BEFORE the drift check so the resume
    setup window has an active breadcrumb."""
    run_status.clear_active()
    repo, pr_doc = repo_with_pr_doc
    import syncade.orchestrator.loop as loop_mod
    from syncade.orchestrator.resume import plan_resume
    from tests.orchestrator._resume_fixtures import _prepare_aborted_run

    run_dir, _ = _prepare_aborted_run(repo, pr_doc, completed_round_count=1, max_rounds=2)
    plan = plan_resume(repo, run_dir)

    captured: dict = {}

    def _spy_drift(*args, **kwargs):
        captured["active_when_drift_runs"] = run_status.active()
        raise WorktreeError("drift check fails (spy)")

    with unittest.mock.patch.object(loop_mod, "check_tree_drift", side_effect=_spy_drift):
        with pytest.raises(WorktreeError):
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=_two_reviewer_config(),
                adapter_factory=_factory_returning(
                    FakeAdapter(canned_output=_ship()), FakeAdapter(canned_output=_ship())
                ),
                resume_plan=plan,
                force_drift=False,
            )
    assert captured["active_when_drift_runs"] is not None, (
        "begin() must run before the resume drift check — else a signal in that "
        "window leaves the resume run's OLD status.json stale"
    )
    run_status.clear_active()


def test_later_round_snapshot_preceded_by_snapshotting_phase(repo_with_pr_doc):
    """For round > 0, update_phase('round-N: snapshotting') must be called
    before take_snapshot() so a hard kill during a later-round snapshot shows
    the correct phase breadcrumb."""
    run_status.clear_active()
    repo, pr_doc = repo_with_pr_doc

    observed_phases: list[str] = []
    real_update_phase = run_status.update_phase

    def _spy_update_phase(phase, round_index=None):
        observed_phases.append(phase)
        real_update_phase(phase, round_index)

    config = SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_rounds": 2},
    )
    # Round 0 NO-SHIP → producer commits → round 1 SHIP
    synth_adapter = _RoundCyclingSynth(_synth_with_blocker(), _synth_clean())
    round_adapters = [
        FakeAdapter(canned_output=_no_ship()),
        FakeAdapter(canned_output=_no_ship()),
        FakeAdapter(canned_output=_ship()),
        FakeAdapter(canned_output=_ship()),
    ]
    with unittest.mock.patch.object(run_status, "update_phase", side_effect=_spy_update_phase):
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=_factory_returning(*round_adapters),
            synthesizer_adapter=synth_adapter,
            producer_adapter=FakeProducerAdapter(commit_message="fix: round-0 producer fix"),
        )

    assert "round-1: snapshotting" in observed_phases, (
        "later-round snapshot must be preceded by 'round-N: snapshotting' "
        "phase so a hard kill during the snapshot shows the correct breadcrumb"
    )
    run_status.clear_active()


def test_fresh_run_begins_breadcrumb_before_persist_run_init(repo_with_pr_doc):
    """Fresh runs must call begin() before persist_run_init so a signal during
    initial artifact writes still leaves a finalized status.json in the run dir."""
    run_status.clear_active()
    repo, pr_doc = repo_with_pr_doc
    import syncade.orchestrator.loop as loop_mod

    captured: dict = {}

    real_persist_run_init = loop_mod.persist_run_init

    def _spy_persist_run_init(*args, **kwargs):
        captured["active_when_persist_run_init_runs"] = run_status.active()
        return real_persist_run_init(*args, **kwargs)

    with unittest.mock.patch.object(
        loop_mod, "persist_run_init", side_effect=_spy_persist_run_init
    ):
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(
                FakeAdapter(canned_output=_ship()), FakeAdapter(canned_output=_ship())
            ),
        )

    assert captured["active_when_persist_run_init_runs"] is not None, (
        "begin() must run before persist_run_init — else a signal in the "
        "fresh-run setup window leaves the run dir with no status.json"
    )
    run_status.clear_active()


def test_fresh_run_post_begin_exception_finalizes_status(repo_with_pr_doc, tmp_path):
    """An exception during fresh-run post-begin setup (persist_run_init) must
    finalize status.json as 'terminated', not leave it stuck as 'running'."""
    run_status.clear_active()
    repo, pr_doc = repo_with_pr_doc
    import syncade.orchestrator.loop as loop_mod

    boom = RuntimeError("injected persist_run_init failure")
    with unittest.mock.patch.object(loop_mod, "persist_run_init", side_effect=boom):
        with pytest.raises(RuntimeError, match="injected persist_run_init failure"):
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=_two_reviewer_config(),
                adapter_factory=_factory_returning(
                    FakeAdapter(canned_output=_ship()), FakeAdapter(canned_output=_ship())
                ),
                worktree_base=tmp_path,
            )

    runs_root = repo / ".syncade" / "runs"
    run_dirs = [d for d in runs_root.iterdir() if d.is_dir()]
    assert run_dirs, "no run dir was created"
    status = json.loads((run_dirs[0] / "status.json").read_text())
    assert status["state"] == "terminated", (
        "post-begin setup failure must finalize status.json as 'terminated', "
        "not leave it stuck as 'running' (misread as a hard-kill breadcrumb)"
    )
    assert status["reason"] == "exception:RuntimeError"
    assert run_status.active() is None
    run_status.clear_active()


def test_branch_advancing_phase_recorded_before_advance_branch_ref(repo_with_pr_doc):
    """The 'branch advancing' phase must be written before _advance_branch_ref
    is called so a kill inside that operation shows the correct breadcrumb
    rather than the previous 'producer' phase."""
    run_status.clear_active()
    repo, pr_doc = repo_with_pr_doc

    observed_phases: list[str] = []
    real_update_phase = run_status.update_phase

    def _spy_update_phase(phase, round_index=None):
        observed_phases.append(phase)
        real_update_phase(phase, round_index)

    config = SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_rounds": 2},
    )
    synth_adapter = _RoundCyclingSynth(_synth_with_blocker(), _synth_clean())
    round_adapters = [
        FakeAdapter(canned_output=_no_ship()),
        FakeAdapter(canned_output=_no_ship()),
        FakeAdapter(canned_output=_ship()),
        FakeAdapter(canned_output=_ship()),
    ]
    with unittest.mock.patch.object(run_status, "update_phase", side_effect=_spy_update_phase):
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=_factory_returning(*round_adapters),
            synthesizer_adapter=synth_adapter,
            producer_adapter=FakeProducerAdapter(commit_message="fix: round-0 producer fix"),
        )

    # 'branch advancing' must appear and must come before 'branch advanced'
    assert "round-0: branch advancing" in observed_phases, (
        "'branch advancing' phase must be recorded before _advance_branch_ref "
        "so a kill during the operation shows the correct phase breadcrumb"
    )
    advancing_idx = observed_phases.index("round-0: branch advancing")
    if "round-0: branch advanced" in observed_phases:
        advanced_idx = observed_phases.index("round-0: branch advanced")
        assert advancing_idx < advanced_idx
    run_status.clear_active()
