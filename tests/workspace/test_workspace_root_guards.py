"""The workspace-root door: what a run id and a run root are allowed to be.

Creating a workspace is a join, not path arithmetic (see
``test_ownership_record.py``), and that is only safe because the id is validated
once at the door and the root is refused when it is not ours to write into.
These are the refusals, each pinned separately — a single "bad input" test would
pass while one specific spelling still got through, which is exactly how
``//server/share`` survived a check that already listed ``"/"``.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from syncade.orchestrator.loop_finalize import _reclaim_shared_run_dir
from syncade.workspace_owner import (
    OWNER_RECORD_NAME,
    WorkspaceOwnerError,
    create_run_dir,
    git_common_dir,
    record_owner,
    run_root,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "repo")


# --- symlink escape and duplicate-run-id regression tests --------------------


# --- the run id is NAMED, so there is no path arithmetic to get wrong -------


def test_the_default_symlinked_base_still_records(repo: Path, tmp_path: Path) -> None:
    """The regression that broke the feature on every default macOS run.

    ``/tmp`` is a symlink to ``/private/tmp`` and the shipped default base is
    ``/tmp/syncade``. Deriving the root by subtracting a RESOLVED base from an
    unresolved path made the containment check fail for every ordinary caller,
    and the helper silently wrote no record at all.
    """
    real = tmp_path / "real"
    real.mkdir()
    base = tmp_path / "link"
    base.symlink_to(real, target_is_directory=True)

    create_run_dir(base, "run-1/round-0", repo)

    assert (base / "run-1" / OWNER_RECORD_NAME).is_file()
    assert (real / "run-1" / OWNER_RECORD_NAME).is_file()


def test_a_relative_base_records_at_the_right_level(repo: Path, tmp_path: Path) -> None:
    """A relative ``worktree_base`` used to produce ``base/base/<run-id>``."""
    monkey = tmp_path / "cwd"
    monkey.mkdir()
    import os as _os

    prior = Path.cwd()
    _os.chdir(monkey)
    try:
        create_run_dir(Path("wt"), "run-2/round-0", repo)
        assert (monkey / "wt" / "run-2" / OWNER_RECORD_NAME).is_file()
        assert not (monkey / "wt" / "wt").exists()
    finally:
        _os.chdir(prior)


def test_a_run_id_that_is_not_a_plain_relative_path_is_refused(tmp_path: Path) -> None:
    """Refused at the door, so no path below has anything to normalize."""
    base = tmp_path / "base"
    for bad in ("../escape", "decoy/../evil", "/absolute", "", ".", ".."):
        with pytest.raises(ValueError, match="plain relative path"):
            run_root(base, bad)
    assert not base.exists(), "a refused run id must not create anything"


@pytest.mark.parametrize("bad", ["//server/share", "a/./b", "a//b"])
def test_run_root_rejects_non_plain_spellings_before_normalization(
    tmp_path: Path, bad: str
) -> None:
    """Spellings that pathlib normalizes away must be rejected on the raw string.

    PurePosixPath silently collapses "//", "./", and duplicate slashes, so a
    check on .parts alone would accept these forms and return a path that does
    not match what the caller passed.
    """
    base = tmp_path / "base"
    with pytest.raises(ValueError, match="plain relative path"):
        run_root(base, bad)


def test_a_symlinked_root_is_refused_rather_than_followed(repo: Path, tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "run-3").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileExistsError, match="symlink"):
        create_run_dir(base, "run-3/round-0", repo)
    assert not (outside / "round-0").exists()
    assert not (outside / OWNER_RECORD_NAME).exists()


def test_a_second_repository_cannot_create_under_the_first_claim(tmp_path: Path) -> None:
    """A root owned by one repository is closed to all others.

    The second repository's attempt raises before creating any nested directory,
    leaving the original record intact.
    """
    base = tmp_path / "base"
    first, second = _make_repo(tmp_path / "first"), _make_repo(tmp_path / "second")

    create_run_dir(base, "shared/round-0", first)
    owner = json.loads((base / "shared" / OWNER_RECORD_NAME).read_text())["repo_common_dir"]

    with pytest.raises(FileExistsError, match="owned by a different repository"):
        create_run_dir(base, "shared/round-1", second)

    # Record unchanged; the second repo's nested dir was never created.
    after = json.loads((base / "shared" / OWNER_RECORD_NAME).read_text())["repo_common_dir"]
    assert after == owner == str(git_common_dir(first))
    assert not (base / "shared" / "round-1").exists()


def test_second_repository_cannot_create_nested_workspace_under_foreign_root(
    tmp_path: Path,
) -> None:
    """Repo B must not land files under a root already recorded to repo A.

    If it could, repo A's orphan GC would treat the whole root as its own and
    delete repo B's workspace.  create_run_dir must raise before creating any
    nested directory when the existing root's record names a different repo.
    """
    base = tmp_path / "base"
    repo_a = _make_repo(tmp_path / "repo_a")
    repo_b = _make_repo(tmp_path / "repo_b")

    # Repo A claims the run root.
    create_run_dir(base, "shared/round-0", repo_a)
    assert (base / "shared" / OWNER_RECORD_NAME).is_file()

    # Repo B must not be able to land a nested workspace under the same root.
    with pytest.raises(FileExistsError, match="owned by a different repository"):
        create_run_dir(base, "shared/round-1", repo_b)

    # Verify repo B's nested directory was never created.
    assert not (base / "shared" / "round-1").exists()


def test_the_root_is_recorded_before_any_nested_component(repo: Path, tmp_path: Path) -> None:
    """A nested mkdir can fail on its own; the root must already be owned.

    The root is recorded by the first successful create_run_dir call.  A
    subsequent call that fails while creating a deeper nested directory must
    leave the already-recorded root intact — not wipe the record.
    """
    base = tmp_path / "base"
    # Establish ownership of the root by a successful first call.
    create_run_dir(base, "run-4/round-0", repo)
    assert (base / "run-4" / OWNER_RECORD_NAME).is_file()

    # Block the nested dir for a second call on the same root.
    (base / "run-4" / "round-1").write_text("a file where the round dir belongs")

    with pytest.raises(OSError):
        create_run_dir(base, "run-4/round-1/producer-worktree", repo)
    # The ownership record still holds after the failure.
    assert (base / "run-4" / OWNER_RECORD_NAME).is_file()


def test_a_failed_write_leaves_no_record_and_no_exception(repo: Path, tmp_path: Path) -> None:
    """The degradation is 'GC reclaims less', never 'the review dies'."""
    base = tmp_path / "base"
    base.mkdir()
    blocked = base / "run-5"
    blocked.write_text("a file where the run root should be")
    record_owner(blocked, repo)
    assert blocked.read_text() == "a file where the run root should be"


def _make_repo(root: Path) -> Path:
    root.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "T"],
    ):
        subprocess.run(cmd, cwd=root, check=True, capture_output=True)
    (root / "f.txt").write_text(root.name)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True, capture_output=True)
    return root


def test_concurrent_creators_cannot_both_claim_one_root(tmp_path: Path) -> None:
    """The record belongs to whoever created the root, under a real race.

    Check-then-write passes a single-threaded test and still loses here: both
    creators see no record, both write, and the loser's payload can later be
    deleted by whoever won. ``os.link`` makes claiming one operation.
    """

    base = tmp_path / "base"
    repos = [_make_repo(tmp_path / f"repo-{i}") for i in range(8)]
    start = threading.Barrier(len(repos))

    def claim(repo: Path) -> None:
        start.wait()
        try:
            create_run_dir(base, "contested/round-0", repo)
        except FileExistsError:
            pass  # losers that see the winner's record raise; that is expected

    threads = [threading.Thread(target=claim, args=(r,)) for r in repos]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    recorded = json.loads((base / "contested" / OWNER_RECORD_NAME).read_text())
    owners = {str(git_common_dir(r)) for r in repos}
    assert recorded["repo_common_dir"] in owners, "the record must name one real claimant"
    leftovers = [p.name for p in (base / "contested").iterdir() if ".tmp" in p.name]
    assert leftovers == [], f"losing writers left temp files behind: {leftovers}"


def test_recordless_existing_root_is_refused(tmp_path: Path) -> None:
    """A pre-existing recordless root is refused rather than adopted.

    Only the invocation that creates the root can publish its initial claim.
    Adopting a pre-existing recordless directory would bless pre-registry or
    foreign shared-base content as this repository's own workspace, which GC
    could then delete.
    """
    repo = _make_repo(tmp_path / "repo")
    base = tmp_path / "base"
    root = base / "run-retry"
    root.mkdir(parents=True)
    assert not (root / OWNER_RECORD_NAME).exists()
    with pytest.raises(WorkspaceOwnerError):
        create_run_dir(base, "run-retry/round-0", repo)
    # The pre-existing root must remain unmodified.
    assert not (root / OWNER_RECORD_NAME).exists()


def test_concurrent_distinct_nested_dirs_only_winner_succeeds(tmp_path: Path) -> None:
    """Two repos racing on a shared absent root with different nested dirs.

    The losing repository must raise rather than place its workspace under the
    winner's ownership record, even when the nested run-id component differs.
    """

    repo_a = _make_repo(tmp_path / "repo-a")
    repo_b = _make_repo(tmp_path / "repo-b")
    base = tmp_path / "base"
    barrier = threading.Barrier(2)
    successes: list[Path] = []
    errors: list[type] = []

    def create_for(repo: Path, nested: str) -> None:
        barrier.wait()
        try:
            create_run_dir(base, nested, repo)
            successes.append(repo)
        except FileExistsError:
            errors.append(repo)

    t1 = threading.Thread(target=create_for, args=(repo_a, "shared-run/round-0"))
    t2 = threading.Thread(target=create_for, args=(repo_b, "shared-run/round-1"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one repo succeeds; the other is rejected by the post-creation check.
    assert len(successes) == 1
    assert len(errors) == 1
    recorded = json.loads((base / "shared-run" / OWNER_RECORD_NAME).read_text())
    assert recorded["repo_common_dir"] == str(git_common_dir(successes[0]))


def test_same_process_concurrent_record_owner_leaves_intact_record(tmp_path: Path) -> None:
    """Same-process concurrent record_owner calls must not corrupt the published record.

    With a pid-only temp filename, all threads share the same temp path.  After
    one thread hard-links tmp → path (the published record), another thread
    opens and truncates that same temp path, writing through the hard link and
    corrupting the published record.  A per-call unique temp name prevents this.
    """

    repos = [_make_repo(tmp_path / f"repo-{i}") for i in range(6)]
    base = tmp_path / "base"
    root = base / "run-pid-race"
    root.mkdir(parents=True)
    barrier = threading.Barrier(len(repos))

    def try_claim(repo: Path) -> None:
        barrier.wait()
        record_owner(root, repo)

    threads = [threading.Thread(target=try_claim, args=(r,)) for r in repos]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    record_path = root / OWNER_RECORD_NAME
    assert record_path.is_file(), "a record must exist after concurrent claims"
    data = json.loads(record_path.read_text())
    all_owners = {str(git_common_dir(r)) for r in repos}
    assert data["repo_common_dir"] in all_owners, "record must name one real claimant"
    leftovers = [p.name for p in root.iterdir() if ".tmp" in p.name]
    assert leftovers == [], f"concurrent writers left temp files: {leftovers}"


def test_a_symlink_swapped_in_after_the_root_exists_captures_nothing(
    repo: Path, tmp_path: Path
) -> None:
    """The window this closes, exercised against the real sequence.

    A previous shape re-checked ``is_symlink()`` after ``mkdir`` and then wrote
    the record and every nested directory BY NAME. That narrowed the window
    without closing it: swap a symlink in after the re-check and the record and
    the whole workspace land in the attacker's directory.

    ``create_run_dir`` now holds the root open and works relative to that
    descriptor, so a swap afterwards cannot redirect anything — the descriptor
    still names the inode that was created. The old shape cannot pass this test;
    an earlier version of it patched ``Path.mkdir`` to inject the symlink, which
    the fd-based path never calls, so it silently stopped exercising anything.
    """
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    outside.mkdir()

    real_root = base / "run-race"
    original_open = os.open
    swapped = {"done": False}

    def _swap_after_root_is_opened(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        # The instant the root is open, replace its NAME with a symlink. Anything
        # still resolving by name from here on lands in `outside`.
        if not swapped["done"] and path == "run-race" and kwargs.get("dir_fd") is not None:
            swapped["done"] = True
            real_root.rename(tmp_path / "moved-away")
            real_root.symlink_to(outside, target_is_directory=True)
        return fd

    with patch.object(os, "open", _swap_after_root_is_opened):
        create_run_dir(base, "run-race/round-0", repo)

    assert swapped["done"], "the fixture must have actually performed the swap"
    assert not (outside / OWNER_RECORD_NAME).exists(), "record must not land in the target"
    assert not (outside / "round-0").exists(), "nested dir must not land in the target"
    moved = tmp_path / "moved-away"
    assert (moved / OWNER_RECORD_NAME).is_file(), "record belongs to the inode we opened"
    assert (moved / "round-0").is_dir(), "nested dir belongs to the inode we opened"


def test_reclaim_does_not_follow_symlinked_run_root(tmp_path: Path) -> None:
    """_reclaim_shared_run_dir must not unlink the owner record via a symlink.

    A swapped symlink at <worktree_base>/<run-id> would cause the finalizer to
    delete the ownership record inside the symlink target — an uncontrolled
    location outside the run tree.  The helper must return without touching
    anything when the run root is a symlink.
    """
    real = tmp_path / "real"
    real.mkdir()
    owner_record = real / OWNER_RECORD_NAME
    owner_record.write_text(
        '{"version": 1, "repo_common_dir": "/some/repo/.git", "run_id": "run-1"}'
    )

    run_dir = tmp_path / "base" / "run-1"
    (tmp_path / "base").mkdir()
    run_dir.symlink_to(real, target_is_directory=True)

    _reclaim_shared_run_dir(run_dir)

    assert owner_record.exists(), "owner record in symlink target must not be deleted"
