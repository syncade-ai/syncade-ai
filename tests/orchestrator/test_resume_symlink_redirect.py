"""PR-h-06b remediation — a symlink below the base must not redirect the delete.

The blocker both blind reviewers found: `_safe_resume_rmtree` RESOLVED the target and
then asked its questions about the resolved path. A run root that is a symlink to a
SIBLING run root inside the same base survives every guard — the resolved path is still
under the base, `tree_contains_repo_root` is false, and the sibling is owned by this same
repository, so ownership passes too — and the sibling's round directory is deleted while
the caller is told the resumed path was cleared.

The cure is the one `CLAUDE.md` already records for `workspace_owner`: **do not resolve
and then interrogate the answer.** Every component below the trusted base is proven not
to be a symlink, so the lexical path and the real path are the same path by construction
and there is no spelling left to enumerate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from syncade.orchestrator.loop import _safe_resume_rmtree
from syncade.workspace_owner import create_run_dir


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    return repo


def _owned_round(base: Path, repo: Path, run_id: str) -> Path:
    """An ownership-recorded `<base>/<run_id>/round-1` with a file in it."""
    create_run_dir(base, f"{run_id}/round-1", repo)
    leaf = base / run_id / "round-1"
    (leaf / "payload").write_text("x", encoding="utf-8")
    return leaf


@pytest.fixture
def _quiet_lsof(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real lsof would answer "empty" here anyway; pin it so the test is hermetic."""
    import syncade.gc_execute as gc_execute_module
    from syncade.process import SubprocessResult

    monkeypatch.setattr(
        gc_execute_module,
        "run_subprocess",
        lambda argv, **k: SubprocessResult(
            returncode=1, stdout="", stderr="", duration_seconds=0.0
        ),
    )


def test_symlinked_run_root_does_not_redirect_the_delete_to_a_sibling(
    tmp_path: Path, _quiet_lsof: None
) -> None:
    """`<base>/run-a` -> `<base>/run-b`, both owned by this repo. run-b must survive."""
    repo = _repo(tmp_path)
    base = tmp_path / "wt"
    base.mkdir()
    victim = _owned_round(base, repo, "run-b")
    (base / "run-a").symlink_to(base / "run-b", target_is_directory=True)

    cleared = _safe_resume_rmtree(base / "run-a" / "round-1", base, repo, reap=True)

    assert victim.exists(), "a sibling run's workspace must not be deleted"
    assert (victim / "payload").exists()
    assert cleared is False, "a symlinked run root still blocks re-provisioning"


def test_symlinked_run_root_does_not_redirect_artifact_cleanup(tmp_path: Path) -> None:
    """The same redirect on the `reap=False` path, where the victim is run HISTORY.

    `.syncade/runs/` is tier 1 — never deleted — so redirecting a delete into a sibling
    run there destroys history `metrics.db` rebuilds from.
    """
    repo = _repo(tmp_path)
    runs_root = repo / ".syncade" / "runs"
    victim = runs_root / "run-b" / "round-1"
    victim.mkdir(parents=True)
    (victim / "manifest.json").write_text("{}", encoding="utf-8")
    (runs_root / "run-a").symlink_to(runs_root / "run-b", target_is_directory=True)

    cleared = _safe_resume_rmtree(runs_root / "run-a" / "round-1", runs_root, repo, reap=False)

    assert victim.exists(), "a sibling run's artifacts must not be deleted"
    assert (victim / "manifest.json").exists()
    assert cleared is False


def test_absent_leaf_under_a_symlinked_run_root_is_not_a_safe_no_op(
    tmp_path: Path, _quiet_lsof: None
) -> None:
    """The leaf is gone but the symlink is still there, and provisioning refuses it."""
    repo = _repo(tmp_path)
    base = tmp_path / "wt"
    base.mkdir()
    _owned_round(base, repo, "run-b")
    (base / "run-b" / "round-1").rename(base / "run-b" / "moved-away")
    (base / "run-a").symlink_to(base / "run-b", target_is_directory=True)

    cleared = _safe_resume_rmtree(base / "run-a" / "round-1", base, repo, reap=True)

    assert cleared is False, "the symlinked run root still blocks re-provisioning"


def test_ordinary_nested_target_is_still_removed(tmp_path: Path, _quiet_lsof: None) -> None:
    """CONTROL. Without it, a refuse-everything regression passes the three above."""
    repo = _repo(tmp_path)
    base = tmp_path / "wt"
    base.mkdir()
    leaf = _owned_round(base, repo, "run-a")

    cleared = _safe_resume_rmtree(leaf, base, repo, reap=True)

    assert cleared is True
    assert not leaf.exists()


def test_ordinary_artifact_dir_is_still_removed(tmp_path: Path) -> None:
    """CONTROL for the `reap=False` path."""
    repo = _repo(tmp_path)
    runs_root = repo / ".syncade" / "runs"
    leaf = runs_root / "run-a" / "round-1"
    leaf.mkdir(parents=True)
    (leaf / "manifest.json").write_text("{}", encoding="utf-8")

    cleared = _safe_resume_rmtree(leaf, runs_root, repo, reap=False)

    assert cleared is True
    assert not leaf.exists()


def test_missing_base_is_a_safe_no_op(tmp_path: Path) -> None:
    """A worktree base that does not exist yet cannot be hiding anything.

    Pinning components below the base first required resolving the base, and doing that
    STRICTLY turned a first resume — where the base has never been created — into a hard
    refusal. Nothing is in the way when there is no base.
    """
    repo = _repo(tmp_path)
    base = tmp_path / "never-created"

    assert _safe_resume_rmtree(base / "run-a" / "round-1", base, repo, reap=True) is True


def test_a_non_directory_component_does_not_make_an_existing_target_look_absent(
    tmp_path: Path, _quiet_lsof: None
) -> None:
    """`Path.exists()` answers False for several "I cannot look" errors, not just absence.

    It SWALLOWS `ENOTDIR` (and `ELOOP`, and `EBADF`) and returns False, so a regular file
    planted where the run root belongs reads as "the target is absent" — the helper
    reports nothing in the way, and the caller destroys the round's artifacts before
    provisioning refuses the file that was there all along. Deterministic, not a race.

    (`EACCES` is NOT swallowed — it propagates and the existing `except OSError` already
    catches it. This test covers the spelling that actually leaks.)
    """
    repo = _repo(tmp_path)
    runs_root = repo / ".syncade" / "runs"
    runs_root.mkdir(parents=True)
    (runs_root / "run-a").write_text("not a directory", encoding="utf-8")

    cleared = _safe_resume_rmtree(runs_root / "run-a" / "round-1", runs_root, repo, reap=False)

    assert (runs_root / "run-a").exists(), "precondition: the obstruction is really there"
    assert cleared is False, "an obstructed path is not an absent one"
