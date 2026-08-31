"""Recordless and unreadable-known-run workspaces are reported precisely.

Recordless syncade-shaped trees may predate the registry or survive a
best-effort record-write failure. They can never be proven owned, so they need
manual removal. An unreadable tree tied by name to repo-local run artifacts is
also reported, but its record and shape remain unknown; making it inspectable
and rerunning GC may classify it. Malformed and foreign records are left but
excluded from this report rather than mislabeled as recordless.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncade.cli.gc_mode import _report_unclaimable
from syncade.gc import execute_gc, plan_gc
from syncade.gc_worktrees import tree_size_bytes, unclaimable_trees
from syncade.workspace_owner import record_owner


def _repo(path: Path) -> Path:
    (path / ".syncade" / "runs").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    return path


def _tree(base: Path, run_id: str, *, payload: int = 0) -> Path:
    tree = base / run_id
    (tree / "round-0").mkdir(parents=True)
    if payload:
        (tree / "round-0" / "blob.bin").write_bytes(b"x" * payload)
    return tree


# --- what is reported, and what is not -------------------------------------


def test_repo_root_is_excluded_from_unclaimable_report(tmp_path: Path) -> None:
    """The operator's checkout must not appear in the unclaimable list.

    When worktree_base is the repo's parent directory, the repo root is an
    immediate subdirectory with no ownership record.  Reporting it as
    unclaimable would point the operator at deleting non-syncade data.
    """
    base = tmp_path  # repo root is a direct child of the base
    repo = _repo(base / "my-repo")
    unrelated = base / "unrelated-dir"
    unrelated.mkdir()

    got = unclaimable_trees(repo, sorted(base.iterdir()), known_run_ids=set())

    assert repo not in got, "operator checkout must not appear in the unclaimable report"
    assert unrelated not in got, (
        "an unrelated non-syncade dir must not appear in the unclaimable report"
    )


def test_malformed_record_tree_is_not_reported_as_recordless(tmp_path: Path) -> None:
    """A directory with a malformed owner record is NOT the same as one with no record.

    Reporting it as "recordless" would misstate Item 5: the directory has a
    record, it is just not a valid one that any repository trusts.
    """
    repo = _repo(tmp_path / "repo")
    base = tmp_path / "wt"
    corrupt = _tree(base, "corrupt-run")
    (corrupt / ".syncade-owner.json").write_text("{not valid json!!!}", encoding="utf-8")

    got = unclaimable_trees(repo, sorted(base.iterdir()), known_run_ids=set())

    assert corrupt not in got, "a tree with a malformed record must not appear as recordless"


def test_unrelated_directory_is_not_reported(tmp_path: Path) -> None:
    """An arbitrary directory without syncade workspace structure is not reported.

    Only directories with a round-N layout are recognised as pre-registry
    syncade workspace roots; a plain directory under the same base is not our
    business to report.
    """
    repo = _repo(tmp_path / "repo")
    base = tmp_path / "wt"
    unrelated = base / "some-other-tool"
    unrelated.mkdir(parents=True)
    (unrelated / "output.log").write_text("nothing to do with syncade", encoding="utf-8")
    stranded = _tree(base, "old-run")  # is a syncade workspace root

    got = unclaimable_trees(repo, sorted(base.iterdir()), known_run_ids=set())

    assert unrelated not in got, (
        "unrelated directory without round-N structure must not be reported"
    )
    assert stranded in got, (
        "genuine pre-registry orphan with round-N structure must still be reported"
    )


def test_all_recordless_workspaces_are_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    base = tmp_path / "wt"

    stranded = _tree(base, "old-run")  # predates records
    ours = _tree(base, "our-run")
    record_owner(ours, repo)
    stranger = _tree(base, "their-run")
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=theirs, check=True, capture_output=True)
    record_owner(stranger, theirs)
    live = _tree(base, "live-run")

    got = unclaimable_trees(repo, sorted(base.iterdir()), known_run_ids={"live-run"})

    assert got == sorted([stranded, live])
    assert ours not in got, "a tree we can prove is ours is reclaimable, not stranded"
    assert stranger not in got, "a stranger's disk is not our unfinished business"
    assert live in got, (
        "a recordless tree cannot enter the ownership-proven normal removal path, so an "
        "existing repo-local run directory must not make it disappear from the report"
    )


def test_plan_reports_recordless_tree_for_an_existing_collectable_run(tmp_path: Path) -> None:
    """A known run without ownership proof is reported, never silently dropped or deleted."""
    repo = _repo(tmp_path / "repo")
    run_id = "known-recordless-run"
    run_dir = repo / ".syncade" / "runs" / run_id
    run_dir.mkdir()
    (run_dir / "run-init.json").write_text("{}", encoding="utf-8")
    (run_dir / "loop-manifest.json").write_text('{"final_exit_code": 0}', encoding="utf-8")
    base = tmp_path / "wt"
    recordless = _tree(base, run_id, payload=64)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=base, worktree_max_age_days=0)

    assert recordless not in plan.worktree_trees_to_remove
    assert recordless not in plan.orphan_worktree_trees
    assert plan.unclaimable_trees == [recordless]
    assert plan.unclaimable_bytes == 64

    execute_gc(plan, dry_run=False, repo_root=repo)
    assert (recordless / "round-0" / "blob.bin").read_bytes() == b"x" * 64


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions are required")
def test_unreadable_known_workspace_is_reported_with_unknown_size(tmp_path: Path) -> None:
    """Inspection failure is visible, not silently converted to absent or zero bytes."""
    repo = _repo(tmp_path / "repo")
    run_id = "known-unreadable-run"
    (repo / ".syncade" / "runs" / run_id).mkdir()
    base = tmp_path / "wt"
    unreadable = _tree(base, run_id, payload=64)
    unreadable.chmod(0)
    try:
        plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=base, worktree_max_age_days=0)

        assert plan.unclaimable_trees == [unreadable]
        assert plan.unclaimable_bytes is None
        assert "size unknown (unreadable contents)" in _render([unreadable], None)
    finally:
        unreadable.chmod(0o700)


def test_the_reported_size_is_true(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    base = tmp_path / "wt"
    _tree(base, "a", payload=1000)
    _tree(base, "b", payload=2500)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=base, worktree_max_age_days=0)

    assert len(plan.unclaimable_trees) == 2
    assert plan.unclaimable_bytes == 3500
    assert sum(tree_size_bytes(t) for t in plan.unclaimable_trees) == 3500


def test_a_symlinked_entry_is_not_counted(tmp_path: Path) -> None:
    """Sizes must not be inflated by following a link out of the base."""
    base = tmp_path / "wt"
    tree = _tree(base, "a", payload=100)
    big = tmp_path / "elsewhere.bin"
    big.write_bytes(b"y" * 50_000)
    (tree / "round-0" / "link.bin").symlink_to(big)

    assert tree_size_bytes(tree) == 100


# --- they are REPORTED, never executed -------------------------------------


def test_gc_never_removes_an_unclaimable_tree(tmp_path: Path) -> None:
    """The list is a notice. Nothing downstream may treat it as a removal set."""
    repo = _repo(tmp_path / "repo")
    base = tmp_path / "wt"
    stranded = _tree(base, "old-run", payload=64)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=base, worktree_max_age_days=0)
    assert plan.unclaimable_trees == [stranded]

    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert stranded.exists()
    assert (stranded / "round-0" / "blob.bin").read_bytes() == b"x" * 64
    assert stranded not in report.worktrees_removed


# --- the wording ------------------------------------------------------------


def _render(trees: list[Path], size: int | None, *, quiet: bool = True) -> str:
    plan = SimpleNamespace(unclaimable_trees=trees, unclaimable_bytes=size)
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _report_unclaimable(plan, quiet=quiet)
    return buf.getvalue()


def test_nothing_is_said_when_there_is_nothing_to_say(tmp_path: Path) -> None:
    assert _render([], 0) == ""


def test_the_message_names_both_cases_and_operator_actions(tmp_path: Path) -> None:
    out = _render([tmp_path / "old-run"], 1500)
    assert "1 workspace(s)" in out
    assert "1.5 KB" in out
    assert "recordless syncade-shaped trees" in out
    assert "unreadable trees" in out
    assert "will not remove these paths on this run" in out
    assert "Remove recordless trees yourself" in out
    assert "make unreadable trees inspectable and rerun GC" in out
    assert "never" not in out.lower()


@pytest.mark.parametrize(
    "promise",
    [
        "not yet",
        "pending",
        "will be removed",
        "future run",
        "next run",
        "skipped for now",
    ],
)
def test_the_message_never_reads_as_a_promise(tmp_path: Path, promise: str) -> None:
    """A tripwire on vague phrasings that imply queued automatic cleanup.

    CEILING: a denylist cannot prove the wording is honest — the positive
    assertions above are what pin both cases and their distinct next actions.
    """
    out = _render([tmp_path / "old-run"], 1500, quiet=False).lower()
    assert promise not in out


def test_each_reported_path_is_labelled_with_why(tmp_path: Path) -> None:
    out = _render([tmp_path / "old-run"], 1500, quiet=False)
    assert "not removed (recordless or unreadable known-run workspace)" in out
    assert str(tmp_path / "old-run") in out
