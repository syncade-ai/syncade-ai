"""PR-h-12 item 2 — resume protection is age-bounded, for WORKTREES only.

`gc_protection` protected a resume-eligible run unconditionally, and nothing ever ended that
protection: 4.4 GB across 73 run dirs with `--gc` reporting nothing to do.

**The brief blamed exit 20 for this and was wrong** — `_RESUMABLE_EXIT_CODES` is
`{10, 25, 40, 60, 70}` and max-rounds has never been in it. Measured over the real corpus, 49 of
65 protected runs carry no `loop-manifest.json` at all: they were INTERRUPTED before the loop
terminator wrote one, so protection fails closed and nothing completes them. That is a stronger
case for an age bound than the brief's version, since an interrupted run is the least resumable
of all — `--resume` refuses on tree drift within days.

The bound is tier 3 only. A released run keeps its transcripts and its run directory — those are
tiers 2 and 1, and nothing here may touch them. That separation is why this needed its own key
rather than reusing `max_age_days`, which gates transcripts: one key cannot be non-zero for
worktrees and zero for transcripts simultaneously.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from syncade.gc import DEFAULT_WORKTREE_MAX_AGE_DAYS, plan_gc
from syncade.workspace_owner import record_owner

#: Exit 40 (subprocess error) IS resume-eligible. Exit 20 is NOT — `_RESUMABLE_EXIT_CODES` is
#: {10, 25, 40, 60, 70}, and max-rounds has never been in it. The brief claimed otherwise and
#: this file's first draft believed it, so every case came back unprotected and proved nothing.
#: Measured over the real corpus, 49 of 65 protected runs are protected for a THIRD reason: no
#: `loop-manifest.json` at all, because they were interrupted before the terminator wrote one.
#: `test_an_interrupted_run_is_the_common_case` covers that shape directly.
_RESUMABLE_EXIT = 40


def _run(repo: Path, run_id: str, *, exit_code: int, age_days: float) -> Path:
    """A complete run of a given age. `run-init.json` + `loop-manifest.json` are what make
    `gc_should_conservatively_protect` answer from the exit code rather than failing closed."""
    d = repo / ".syncade" / "runs" / run_id
    (d / "round-0").mkdir(parents=True)
    (d / "run-init.json").write_text("{}")
    (d / "loop-manifest.json").write_text(json.dumps({"final_exit_code": exit_code}))
    (d / "round-0" / "manifest.json").write_text("{}")
    (d / "round-0" / "reviewer.stdout").write_text("x" * 2048)
    when = time.time() - age_days * 86400
    for p in (d / "round-0", d):
        os.utime(p, (when, when))
    return d


def _worktree(base: Path, run_id: str, repo: Path) -> Path:
    tree = base / run_id
    (tree / "reviewer").mkdir(parents=True)
    (tree / "reviewer" / "file.txt").write_text("worktree content")
    record_owner(tree, repo)
    return tree


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".syncade" / "runs").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    return root


def test_an_aged_resumable_run_releases_its_worktree(repo: Path, tmp_path: Path) -> None:
    """A resume-eligible run, three weeks old — far past any plausible resume, since `--resume`
    refuses on tree drift long before that."""
    base = tmp_path / "wt"
    _run(repo, "2020-01-01T00-00-00", exit_code=_RESUMABLE_EXIT, age_days=21)
    _worktree(base, "2020-01-01T00-00-00", repo)

    plan = plan_gc(repo, keep=0, worktree_max_age_days=14, worktree_base=base)

    assert plan.worktree_age_released == ["2020-01-01T00-00-00"]
    assert any(t.name == "2020-01-01T00-00-00" for t in plan.worktree_trees_to_remove)


def test_a_young_resumable_run_is_still_fully_protected(repo: Path, tmp_path: Path) -> None:
    """The mitigation the brief demands: nobody is inspecting a three-week-old worktree, but
    someone may well be inspecting yesterday's."""
    base = tmp_path / "wt"
    _run(repo, "2026-01-01T00-00-00", exit_code=_RESUMABLE_EXIT, age_days=2)
    _worktree(base, "2026-01-01T00-00-00", repo)

    plan = plan_gc(repo, keep=0, worktree_max_age_days=14, worktree_base=base)

    assert plan.worktree_age_released == []
    assert plan.worktree_trees_to_remove == []
    assert "2026-01-01T00-00-00" in plan.protected_run_ids


def test_the_age_release_frees_only_worktrees(repo: Path, tmp_path: Path) -> None:
    """Tier 3 only. A released run must NOT become slimmable — its transcripts are tier 2 and its
    directory is tier 1, and releasing those would silently change transcript retention, which is
    the exact thing the separate key exists to prevent.
    """
    base = tmp_path / "wt"
    _run(repo, "2020-01-01T00-00-00", exit_code=_RESUMABLE_EXIT, age_days=99)
    _worktree(base, "2020-01-01T00-00-00", repo)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_max_age_days=14, worktree_base=base)

    assert plan.worktree_age_released == ["2020-01-01T00-00-00"]
    assert plan.runs_to_slim == [], "tier 2 must stay protected for a resumable run"
    assert "2020-01-01T00-00-00" in plan.protected_run_ids, "tier 1 protection is unchanged"


def test_zero_disables_the_bound_and_preserves_the_old_behaviour(
    repo: Path, tmp_path: Path
) -> None:
    """The opt-out the brief requires pinned: `0` must never remove a worktree today's code keeps.

    This is what an operator sets when they are mid-investigation on an old run and want the
    previous never-ending protection back.
    """
    base = tmp_path / "wt"
    _run(repo, "2020-01-01T00-00-00", exit_code=_RESUMABLE_EXIT, age_days=999)
    _worktree(base, "2020-01-01T00-00-00", repo)

    plan = plan_gc(repo, keep=0, worktree_max_age_days=0, worktree_base=base)

    assert plan.worktree_age_released == []
    assert plan.worktree_trees_to_remove == []


def test_a_non_resumable_run_never_needed_the_bound(repo: Path, tmp_path: Path) -> None:
    """A SHIP run was already unprotected and its worktree already collectable. The bound must
    not be doing the work here, or a regression in it would hide behind this path."""
    base = tmp_path / "wt"
    _run(repo, "2020-06-01T00-00-00", exit_code=0, age_days=30)
    _worktree(base, "2020-06-01T00-00-00", repo)

    plan = plan_gc(repo, keep=0, worktree_max_age_days=0, worktree_base=base)

    assert plan.worktree_age_released == [], "not released — it was never protected"
    assert any(t.name == "2020-06-01T00-00-00" for t in plan.worktree_trees_to_remove)


def test_the_default_is_the_calibrated_fourteen_days() -> None:
    """Pinned because the number is evidence, not taste: 421 runs / 65 protected, 14d releases
    86% and keeps 9. Changing it should mean re-measuring, not re-guessing."""
    assert DEFAULT_WORKTREE_MAX_AGE_DAYS == 14


def test_an_interrupted_run_is_the_common_case(repo: Path, tmp_path: Path) -> None:
    """49 of the 65 protected runs in the real corpus have NO `loop-manifest.json`.

    They were killed before the loop terminator wrote one, so `gc_should_conservatively_protect`
    fails closed — correctly, since it cannot know what happened — and nothing ever completes
    them, so the protection is permanent. This is the shape that actually accumulates, and it is
    NOT what the brief described.
    """
    base = tmp_path / "wt"
    d = repo / ".syncade" / "runs" / "2020-03-01T00-00-00"
    (d / "round-0").mkdir(parents=True)
    (d / "run-init.json").write_text("{}")  # started, never finished: no loop-manifest.json
    when = time.time() - 60 * 86400
    os.utime(d, (when, when))
    _worktree(base, "2020-03-01T00-00-00", repo)

    plan = plan_gc(repo, keep=0, worktree_max_age_days=14, worktree_base=base)

    assert "2020-03-01T00-00-00" in plan.protected_run_ids, "fails closed — nothing completed it"
    assert plan.worktree_age_released == ["2020-03-01T00-00-00"]
    assert plan.runs_to_slim == [], "tier 2 stays protected; only the worktree is released"


def test_execution_actually_removes_the_released_worktree(repo: Path, tmp_path: Path) -> None:
    """The layer the first version of this file did not test, and the bug lived exactly there.

    `plan_gc` named the tree correctly while `execute_gc` skipped it: `run_id_protected_now`
    falls through to a DISK re-check that re-derives protection from the run directory, which is
    unchanged — only AGE releases it. So the bound computed perfectly and removed nothing, and
    every plan-level assertion above still passed. Only an end-to-end run caught it.
    """
    from syncade.gc import execute_gc

    base = tmp_path / "wt"
    _run(repo, "2020-01-01T00-00-00", exit_code=_RESUMABLE_EXIT, age_days=40)
    _worktree(base, "2020-01-01T00-00-00", repo)
    tree_root = base / "2020-01-01T00-00-00"

    plan = plan_gc(repo, keep=0, worktree_max_age_days=14, worktree_base=base)
    execute_gc(plan, dry_run=False, repo_root=repo)

    assert not tree_root.exists(), "a released worktree must actually be removed, not just planned"
    run_dir = repo / ".syncade" / "runs" / "2020-01-01T00-00-00"
    assert run_dir.exists(), "tier 1 untouched"
    assert (run_dir / "round-0" / "reviewer.stdout").exists(), "tier 2 untouched"


def test_execution_still_honours_protection_for_a_young_run(repo: Path, tmp_path: Path) -> None:
    """The bypass must be scoped to age-released runs only, or it removes the TOCTOU guard that
    exists for a run which became resume-eligible between plan and execute."""
    from syncade.gc import execute_gc

    base = tmp_path / "wt"
    _run(repo, "2026-01-01T00-00-00", exit_code=_RESUMABLE_EXIT, age_days=1)
    _worktree(base, "2026-01-01T00-00-00", repo)
    tree_root = base / "2026-01-01T00-00-00"

    plan = plan_gc(repo, keep=0, worktree_max_age_days=14, worktree_base=base)
    execute_gc(plan, dry_run=False, repo_root=repo)

    assert tree_root.exists(), "a young resumable run keeps its worktree"


def test_a_run_that_becomes_protected_between_plan_and_execute_is_still_skipped(
    repo: Path, tmp_path: Path
) -> None:
    """The TOCTOU guard the age-release bypass must not swallow.

    `worktree_age_released` is what lets execution skip the protection re-check, so it must
    contain ONLY runs that were protected at plan time and aged out. Widening it to every aged
    run looks harmless — those runs are already slimmable, so the removal set is identical — but
    it hands the bypass to runs whose protection status could still change, which is exactly what
    the re-check exists to catch. Mutation-checked: dropping `d.name in protected` from
    `_worktree_age_released` passes every other test in this file and fails this one.
    """
    from syncade.gc import execute_gc

    base = tmp_path / "wt"
    run_id = "2020-09-01T00-00-00"
    d = _run(repo, run_id, exit_code=0, age_days=40)  # SHIP: unprotected at plan time
    _worktree(base, run_id, repo)
    tree_root = base / run_id

    plan = plan_gc(repo, keep=0, worktree_max_age_days=14, worktree_base=base)
    assert plan.worktree_age_released == [], "it was never protected, so nothing to release"
    assert any(t.name == run_id for t in plan.worktree_trees_to_remove)

    # ...and now it becomes resume-eligible, after planning.
    (d / "loop-manifest.json").write_text(json.dumps({"final_exit_code": _RESUMABLE_EXIT}))

    execute_gc(plan, dry_run=False, repo_root=repo)

    assert tree_root.exists(), (
        "a run that became protected between plan and execute must keep its worktree — the "
        "age-release bypass is scoped to runs that were protected AND aged out, not to every "
        "old run"
    )


def test_old_unprotected_run_within_keep_window_loses_its_worktree(
    repo: Path, tmp_path: Path
) -> None:
    """An unprotected (SHIP) run inside the transcript keep window keeps its TRANSCRIPTS but
    must lose its WORKTREE once older than worktree_max_age_days.

    Before the fix, worktree selection was coupled to slim_names (transcript selection). A run
    inside keep=20 was never in slim_names, so its worktree survived indefinitely regardless
    of worktree_max_age_days. The two tiers must be selected independently.

    max_age_days=90 ensures the 30-day-old run is NOT old enough to lose its transcripts (only
    runs older than 90 days would be slimmed), while worktree_max_age_days=14 does flag it for
    worktree removal. The run is inside keep=5 (only 3 runs exist) so slim_names is empty.
    """
    base = tmp_path / "wt"
    # Three SHIP runs; all inside keep=5; only the oldest is past the 14-day worktree floor
    _run(repo, "2026-01-01T00-00-00", exit_code=0, age_days=30)  # 30d old — past worktree floor
    _run(repo, "2026-02-01T00-00-00", exit_code=0, age_days=2)
    _run(repo, "2026-03-01T00-00-00", exit_code=0, age_days=1)
    _worktree(base, "2026-01-01T00-00-00", repo)  # worktree only for the old run

    # max_age_days=90: runs must be 90d old before transcripts are slimmed, so the 30d run stays
    plan = plan_gc(repo, keep=5, max_age_days=90, worktree_max_age_days=14, worktree_base=base)

    # The old run is inside the transcript keep window — its transcripts must be KEPT
    assert plan.runs_to_slim == [], "no run is old enough (90d) to slim transcripts"
    # But its worktree is older than worktree_max_age_days — must be REMOVED
    assert any(t.name == "2026-01-01T00-00-00" for t in plan.worktree_trees_to_remove), (
        "worktree selection must be independent of slim_names; an old unprotected run inside "
        "the keep window must still lose its worktree once past worktree_max_age_days"
    )


def test_young_run_beyond_the_keep_window_keeps_its_worktree(repo: Path, tmp_path: Path) -> None:
    """THE MIRROR of the test above, and the blocker a dogfood round found surviving a fix.

    That test proves an OLD run loses its worktree even when its transcripts are kept. This
    proves a YOUNG run KEEPS its worktree even when its transcripts are slimmed — the direction
    that was still broken, because `all_worktree_ids` was a union WITH `slim_names`. Under normal
    run volume everything falls out of `keep` within days, so a one-day-old worktree an operator
    was still reading was deleted with the age floor bypassed entirely. Fixing only the first
    direction reads as fixed and is not; a decoupling needs both.
    """
    base = tmp_path / "wt"
    for i in range(4):  # 4 runs, keep=1 -> three fall outside the transcript keep window
        _run(repo, f"2026-03-0{i + 1}T00-00-00", exit_code=0, age_days=1)
    _worktree(base, "2026-03-01T00-00-00", repo)

    plan = plan_gc(repo, keep=1, max_age_days=0, worktree_max_age_days=14, worktree_base=base)

    assert "2026-03-01T00-00-00" in plan.runs_to_slim, "fixture check: transcripts ARE slimmable"
    assert plan.worktree_trees_to_remove == [], (
        "a worktree one day old must survive a 14-day floor even when its run's transcripts are "
        "eligible for slimming — tier 3 is selected by its own age rule, never by tier 2's"
    )
