"""Regression tests for the C-loop audit cluster (H3, M2, M3).

All three findings live in :mod:`syncade.orchestrator.loop`:

- **H3** — the loop-mode tracked-dirty refusal carried an
  ``and resume_plan is None`` clause, so resumed runs slipped past it.
- **M2** — the resume cleanup deleted the stale round / worktree subtrees
  with a raw ``shutil.rmtree`` that would follow a swapped parent symlink
  out of the worktree base.
- **M3** — the fresh-run reservation only probed ``tmp_run_dir.exists()``
  (check-then-act) before accepting a run-id for the shared ``/tmp``
  worktree dir, instead of reserving it atomically.

Fixtures (``repo_with_pr_doc`` plus the two autouse fixtures
``_isolated_worktree_base`` / ``_default_to_fake_synthesizer``) come from
``tests/orchestrator/conftest.py``.
"""

from __future__ import annotations

import subprocess

import pytest

from syncade.adapters.fake import FakeAdapter
from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from syncade.orchestrator.resume import plan_resume
from syncade.worktree import WorktreeError
from tests.orchestrator._helpers import _factory_returning, _ship
from tests.orchestrator._resume_fixtures import _prepare_aborted_run

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def _two_reviewer_loop_config(max_rounds: int = 2) -> SyncadeConfig:
    return SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_rounds": max_rounds},
    )


class _StopRun(Exception):
    """Sentinel raised from a patched ``_run_round_step`` so a test can halt
    ``run_review`` right after the reservation / resume-cleanup phase and
    inspect on-disk state without provisioning or running a real round."""


# --- H3: resumed runs are not exempt from the dirty-tree refusal ---------


def test_resume_refuses_tracked_dirty_tree_in_loop_mode(repo_with_pr_doc):
    """A resumed loop-mode run (max_rounds > 1) with a tracked-modified
    working tree and no ``--force-dirty`` is refused, exactly like a fresh
    run. ``force_drift=True`` only neutralizes the fabricated-SHA drift
    check so the *before-fix* path would otherwise run to completion; the
    dirty refusal fires before the resume branch where drift is handled."""
    repo, pr_doc = repo_with_pr_doc
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo, check=False)
    run_dir, _ = _prepare_aborted_run(
        repo, pr_doc, completed_round_count=1, max_rounds=2, aborted_exit_code=40
    )
    plan = plan_resume(repo, run_dir)
    # Dirty a tracked file AFTER planning — planning reads on-disk
    # artifacts, not the working tree.
    (repo / "README.md").write_text("modified\n", encoding="utf-8")

    with pytest.raises(WorktreeError) as exc_info:
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_loop_config(max_rounds=2),
            adapter_factory=_factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship())),
            resume_plan=plan,
            force_dirty=False,
            force_drift=True,
            logger=Logger(level="quiet"),
        )
    msg = str(exc_info.value)
    assert "loop mode" in msg
    assert "force-dirty" in msg


def test_resume_refuses_dirty_when_effective_max_rounds_bumped(repo_with_pr_doc):
    """H3 completeness: a resume of a multi-round run must be refused even
    when the *entry* config (or a ``--max-rounds`` override) has drifted to
    ``max_rounds=1``. Rehydration later bumps the cap to
    ``max(config, resume_plan.max_rounds)``, so the dirty refusal must read
    that same effective value — otherwise ``syncade --resume <id>
    --max-rounds 1`` on a 2-round run reads the un-bumped 1, slips the gate,
    and the producer commits over the tracked WIP. Before the fix the refusal
    read ``config.loop.max_rounds`` (1 here) and was skipped."""
    repo, pr_doc = repo_with_pr_doc
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo, check=False)
    run_dir, _ = _prepare_aborted_run(
        repo, pr_doc, completed_round_count=1, max_rounds=2, aborted_exit_code=40
    )
    plan = plan_resume(repo, run_dir)
    assert plan.max_rounds > 1  # original run was multi-round
    (repo / "README.md").write_text("modified\n", encoding="utf-8")

    with pytest.raises(WorktreeError) as exc_info:
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            # Entry config drifted to single-pass; only the rehydrated cap is >1.
            config=_two_reviewer_loop_config(max_rounds=1),
            adapter_factory=_factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship())),
            resume_plan=plan,
            force_dirty=False,
            force_drift=True,
            logger=Logger(level="quiet"),
        )
    msg = str(exc_info.value)
    assert "loop mode" in msg
    assert "force-dirty" in msg


# --- M2: resume cleanup refuses targets that escape the worktree base ----


def test_resume_cleanup_refuses_target_outside_worktree_base(repo_with_pr_doc, tmp_path):
    """The resume cleanup removes the stale worktree subtree through the
    hardened ``_safe_resume_rmtree``, which refuses a target that resolves
    outside the worktree base (here via a swapped ``<run_id>`` parent
    symlink). Before the fix a raw ``shutil.rmtree`` followed the symlink
    and deleted the external victim.

    The guard-refused cleanup now raises ``WorktreeError`` (not a silent
    ``_StopRun`` continuation), because an existing but unremovable target
    blocks re-provisioning and must stop the resume explicitly."""
    repo, pr_doc = repo_with_pr_doc
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo, check=False)
    run_dir, _ = _prepare_aborted_run(
        repo, pr_doc, completed_round_count=1, max_rounds=2, aborted_exit_code=40
    )
    plan = plan_resume(repo, run_dir)
    resumed_round = plan.resumed_round

    # An external victim OUTSIDE the worktree base, holding a sentinel the
    # cleanup must never delete.
    victim = tmp_path / "victim"
    (victim / f"round-{resumed_round}").mkdir(parents=True)
    sentinel = victim / f"round-{resumed_round}" / "keep.txt"
    sentinel.write_text("do not delete", encoding="utf-8")

    # Worktree base whose <run_id> entry is a symlink escaping to the
    # victim, so effective_worktree_base/<run_id>/round-N resolves outside
    # the base.
    worktree_base = tmp_path / "wt-base"
    worktree_base.mkdir()
    (worktree_base / plan.run_id).symlink_to(victim, target_is_directory=True)

    with pytest.raises(WorktreeError):
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_loop_config(max_rounds=2),
            adapter_factory=_factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship())),
            resume_plan=plan,
            force_drift=True,
            worktree_base=worktree_base,
            logger=Logger(level="quiet"),
        )

    # The out-of-base victim survived the resume cleanup.
    assert sentinel.exists()
    assert sentinel.read_text(encoding="utf-8") == "do not delete"


def test_resume_cleanup_refuses_symlink_leaf_identity(repo_with_pr_doc, tmp_path):
    """A resumed worktree dir that is itself a symlink fails the identity
    guard (``tree_identity`` returns None for symlinks) and is left alone —
    the symlink target is never followed.

    The guard-refused cleanup now raises ``WorktreeError`` because the symlink
    blocks re-provisioning just as a failed delete would."""
    repo, pr_doc = repo_with_pr_doc
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo, check=False)
    run_dir, _ = _prepare_aborted_run(
        repo, pr_doc, completed_round_count=1, max_rounds=2, aborted_exit_code=40
    )
    plan = plan_resume(repo, run_dir)
    resumed_round = plan.resumed_round

    victim = tmp_path / "victim"
    victim.mkdir()
    sentinel = victim / "keep.txt"
    sentinel.write_text("do not delete", encoding="utf-8")

    worktree_base = tmp_path / "wt-base"
    (worktree_base / plan.run_id).mkdir(parents=True)
    # The resumed round's worktree dir is a symlink to the victim.
    (worktree_base / plan.run_id / f"round-{resumed_round}").symlink_to(
        victim, target_is_directory=True
    )

    with pytest.raises(WorktreeError):
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_loop_config(max_rounds=2),
            adapter_factory=_factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship())),
            resume_plan=plan,
            force_drift=True,
            worktree_base=worktree_base,
            logger=Logger(level="quiet"),
        )

    assert sentinel.exists()


def test_resume_worktree_cleanup_reaps_in_cwd_processes_before_delete(tmp_path, monkeypatch):
    """M2: an EXTERNAL worktree removal (reap=True) reaps in-cwd processes
    (GC-equivalent) BEFORE the rmtree, so a worktree is never deleted out
    from under a live subprocess of the aborted round. Pinned
    deterministically — the reap must run, on the resolved target, while it
    still exists. The real-process kill is covered by
    tests/smoke/test_resume_reap_smoke.py."""
    import syncade.gc_execute as gc_execute_module
    from syncade.orchestrator.loop import _safe_resume_rmtree
    from syncade.workspace_owner import record_owner

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True, capture_output=True)
    base = tmp_path / "wt-base"
    target = base / "run" / "round-1"
    target.mkdir(parents=True)
    # Ownership lives at the run root (target.parent = base/"run"), not the round dir.
    record_owner(target.parent, repo_root)

    seen: list[tuple[object, bool]] = []

    def _spy(tree):
        seen.append((tree, tree.exists()))
        return [], True

    monkeypatch.setattr(gc_execute_module, "reap_processes_in_tree", _spy)

    _safe_resume_rmtree(target, base, repo_root, reap=True)

    assert seen, "reap was not invoked before deletion"
    reaped_path, existed_when_reaped = seen[0]
    assert reaped_path == target.resolve()
    assert existed_when_reaped is True  # reaped while the tree still existed
    assert not target.exists()  # rmtree still ran after the reap


def test_resume_artifact_cleanup_does_not_reap(tmp_path, monkeypatch):
    """M2 over-reach fix: a run-artifact removal (reap=False, the default)
    must NOT SIGKILL anything — an operator may be inspecting
    .syncade/runs/... — but still removes the dir with a guarded rmtree.

    This is the RESUME path dropping a partial ``round-N/`` so it can be re-run. Do
    not read it as "GC does this too": since PR-v2-18, GC never removes a run
    directory at all — it prunes subprocess transcripts and keeps the history."""
    import syncade.gc_execute as gc_execute_module
    from syncade.orchestrator.loop import _safe_resume_rmtree

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    base = tmp_path / "runs"
    target = base / "run" / "round-1"
    target.mkdir(parents=True)

    reaped: list[object] = []
    monkeypatch.setattr(
        gc_execute_module,
        "reap_processes_in_tree",
        lambda tree: (reaped.append(tree), ([], True))[1],
    )

    _safe_resume_rmtree(target, base, repo_root)  # default reap=False

    assert reaped == []  # no SIGKILL inside a persisted artifact dir
    assert not target.exists()  # but the guarded rmtree still ran


def test_resume_cleanup_does_not_reap_a_refused_tree(tmp_path, monkeypatch):
    """A guard-refused target (escapes the base via a parent symlink) is a
    no-op that must NOT reap even when reap=True is requested — resume never
    kills processes in a tree it refuses to touch."""
    import syncade.gc_execute as gc_execute_module
    from syncade.orchestrator.loop import _safe_resume_rmtree

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    victim = tmp_path / "victim"
    (victim / "round-1").mkdir(parents=True)
    base = tmp_path / "wt-base"
    base.mkdir()
    (base / "run").symlink_to(victim, target_is_directory=True)
    target = base / "run" / "round-1"  # resolves OUTSIDE base

    reaped: list[object] = []
    monkeypatch.setattr(
        gc_execute_module,
        "reap_processes_in_tree",
        lambda tree: (reaped.append(tree), ([], True))[1],
    )

    _safe_resume_rmtree(target, base, repo_root, reap=True)

    assert reaped == []  # refused before any reap
    assert (victim / "round-1").exists()  # victim untouched


def test_resume_cleanup_reaps_worktree_but_not_run_artifacts(repo_with_pr_doc, monkeypatch):
    """Call-site intent: the resume flow reaps the EXTERNAL worktree subtree
    (reap=True) but removes the persisted .syncade/runs artifact dir with
    reap=False, so an operator shelling into the run dir during resume is not
    SIGKILLed (M2 over-reach fix)."""
    import syncade.orchestrator.loop as loop_module

    repo, pr_doc = repo_with_pr_doc
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo, check=False)
    run_dir, _ = _prepare_aborted_run(
        repo, pr_doc, completed_round_count=1, max_rounds=2, aborted_exit_code=40
    )
    plan = plan_resume(repo, run_dir)

    calls: list[tuple[object, bool]] = []

    def _record(target, base, repo_root, *, reap=False):
        calls.append((target, reap))
        return True  # "cleared"; the abandonment path has its own test file

    def _stop(**kwargs):
        raise _StopRun

    monkeypatch.setattr(loop_module, "_safe_resume_rmtree", _record)
    monkeypatch.setattr(loop_module, "_run_round_step", _stop)

    with pytest.raises(_StopRun):
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_loop_config(max_rounds=2),
            adapter_factory=_factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship())),
            resume_plan=plan,
            force_drift=True,
            logger=Logger(level="quiet"),
        )

    assert len(calls) == 2, calls
    (worktree_target, worktree_reap), (artifact_target, artifact_reap) = calls
    # PR-h-06b item 4 REVERSED this order, and the order is now load-bearing: the
    # workspace must be cleared before the artifact dir is destroyed, because a
    # workspace that cannot be cleared stops the resume, and losing the artifacts on
    # the way to that stop is the regression the reordering fixes.
    # First removal: the external worktree subtree → reap.
    assert "runs" not in str(worktree_target)
    assert worktree_reap is True
    # Second removal: the persisted run-artifact dir → NO reap (no SIGKILL).
    assert "runs" in str(artifact_target)
    assert artifact_reap is False


# --- M3: fresh-run reservation atomically claims the /tmp worktree dir ----


def test_fresh_run_atomically_reserves_tmp_worktree_dir(repo_with_pr_doc, tmp_path, monkeypatch):
    """The fresh-run reservation ``mkdir``s the chosen run-id's ``/tmp``
    worktree dir (atomic reservation), bumping the run-id when the base
    id's tmp dir already exists. Before the fix it only probed
    ``.exists()`` and never reserved the accepted id's tmp dir, so a
    concurrent run could still claim it."""
    import syncade.orchestrator.loop as loop_module

    repo, pr_doc = repo_with_pr_doc
    base_id = "2026-06-28T12-00-00"
    monkeypatch.setattr(loop_module, "generate_run_id", lambda: base_id)

    worktree_base = tmp_path / "wt-base"
    # Pre-create the base run-id's tmp worktree dir → forces a bump.
    (worktree_base / base_id).mkdir(parents=True)

    captured: dict[str, object] = {}

    def _capture_and_stop(**kwargs):
        rid = kwargs["run_id"]
        captured["run_id"] = rid
        captured["tmp_reserved"] = (worktree_base / rid).is_dir()
        raise _StopRun

    monkeypatch.setattr(loop_module, "_run_round_step", _capture_and_stop)

    with pytest.raises(_StopRun):
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_loop_config(max_rounds=1),
            adapter_factory=_factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship())),
            worktree_base=worktree_base,
            logger=Logger(level="quiet"),
        )

    # Base id's tmp dir pre-existed → run-id bumped, no collision.
    assert captured["run_id"] == f"{base_id}-2"
    # The accepted id's tmp dir was reserved (created) by the loop itself.
    assert captured["tmp_reserved"] is True
    assert (worktree_base / f"{base_id}-2").is_dir()


# --- artifact-dir cleanup return check ---


def test_resume_raises_worktree_error_when_artifact_dir_cleanup_refused(repo_with_pr_doc, tmp_path):
    """When the round artifact dir is a symlink, _safe_resume_rmtree returns False
    and run_review raises WorktreeError instead of letting mkdir fail later with
    a raw FileExistsError.

    The workspace cleanup runs first (absent → True) so resume reaches the artifact
    cleanup, which then refuses the symlink (reap=False) and triggers the check.
    """
    repo, pr_doc = repo_with_pr_doc
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo, check=False)
    run_dir, _ = _prepare_aborted_run(
        repo, pr_doc, completed_round_count=1, max_rounds=2, aborted_exit_code=40
    )
    plan = plan_resume(repo, run_dir)
    resumed_round = plan.resumed_round

    # Replace the existing round artifact dir with a symlink to an external victim.
    victim = tmp_path / "victim"
    victim.mkdir()
    sentinel = victim / "keep.txt"
    sentinel.write_text("do not delete", encoding="utf-8")

    round_artifact_dir = run_dir / f"round-{resumed_round}"
    import shutil

    shutil.rmtree(round_artifact_dir)
    round_artifact_dir.symlink_to(victim, target_is_directory=True)

    # Workspace dir is absent from the worktree base, so workspace cleanup
    # returns True (nothing in the way) and the artifact cleanup is reached.
    worktree_base = tmp_path / "wt-base"
    worktree_base.mkdir()

    with pytest.raises(WorktreeError) as exc_info:
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_loop_config(max_rounds=2),
            adapter_factory=_factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship())),
            resume_plan=plan,
            force_drift=True,
            worktree_base=worktree_base,
            logger=Logger(level="quiet"),
        )

    assert str(round_artifact_dir) in str(exc_info.value)
    # The symlink target was never touched.
    assert sentinel.exists()
    assert sentinel.read_text(encoding="utf-8") == "do not delete"
