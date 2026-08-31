"""A record that cannot be trusted must read as "not ours".

The costs are not symmetric: a false negative leaks disk, a false positive
deletes another repository's work. So every state that is not an intact record
this repository could have written yields ``None``, and each state is pinned
individually — a single "malformed" test would pass while one specific spelling
of malformed still read as ownership.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from syncade.gc_worktrees import repo_owned_orphan_trees
from syncade.workspace_owner import (
    OWNER_RECORD_NAME,
    git_common_dir,
    owner_of,
    record_owner,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    return root


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    t = tmp_path / "base" / "run-1"
    t.mkdir(parents=True)
    return t


def _write(tree: Path, payload: str) -> None:
    (tree / OWNER_RECORD_NAME).write_text(payload, encoding="utf-8")


def _is_reclaimable(repo: Path, tree: Path) -> bool:
    return tree in repo_owned_orphan_trees(repo, [tree], known_run_ids=set())


# --- the five states named in the brief ------------------------------------


def test_absent(repo: Path, tree: Path) -> None:
    assert owner_of(tree) is None
    assert not _is_reclaimable(repo, tree)


def test_unreadable(repo: Path, tree: Path) -> None:
    record = tree / OWNER_RECORD_NAME
    record.write_text("{}")
    record.chmod(0o000)
    try:
        assert owner_of(tree) is None
        assert not _is_reclaimable(repo, tree)
    finally:
        record.chmod(0o600)


def test_malformed(repo: Path, tree: Path) -> None:
    for payload in ("", "}{", "null", "[]", '"a string"', "{", '{"a":'):
        _write(tree, payload)
        assert owner_of(tree) is None, payload
        assert not _is_reclaimable(repo, tree), payload


def test_ambiguous_a_record_naming_a_different_run(repo: Path, tree: Path) -> None:
    """A record whose run id is not its own directory was copied or moved."""
    _write(
        tree,
        json.dumps(
            {
                "version": 1,
                "repo_common_dir": str(git_common_dir(repo)),
                "run_id": "some-other-run",
            }
        ),
    )
    assert owner_of(tree) is None
    assert not _is_reclaimable(repo, tree)


def test_naming_a_different_repository(tmp_path: Path, repo: Path, tree: Path) -> None:
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=stranger, check=True, capture_output=True)
    record_owner(tree, stranger)

    assert owner_of(tree) == git_common_dir(stranger)
    assert not _is_reclaimable(repo, tree)


# --- the three attacks named in the brief ----------------------------------


def test_a_symlinked_record_cannot_borrow_another_trees_answer(repo: Path, tree: Path) -> None:
    """Without an lstat guard, one tree answers with another tree's record.

    The borrowed record names THIS tree's run id, so the run-id check cannot be
    what rejects it. That matters: an earlier version of this test borrowed a
    record carrying its own directory's id, so the run-id check rejected it and
    the test passed with the symlink guard removed — it was pinning the wrong
    thing.
    """
    ours = tree.parent / "run-ours"
    ours.mkdir()
    (ours / OWNER_RECORD_NAME).write_text(
        json.dumps(
            {
                "version": 1,
                "repo_common_dir": str(git_common_dir(repo)),
                "run_id": tree.name,
            }
        ),
        encoding="utf-8",
    )
    assert owner_of(ours) is None, "the decoy names another run, as intended"

    (tree / OWNER_RECORD_NAME).symlink_to(ours / OWNER_RECORD_NAME)
    assert owner_of(tree) is None
    assert not _is_reclaimable(repo, tree)


def test_a_record_naming_a_symlink_into_this_repo_is_not_ownership(
    tmp_path: Path, repo: Path, tree: Path
) -> None:
    """The recorded path is compared literally, never resolved.

    Resolving it would CREATE this attack: a record naming any symlink that
    happens to point at our common dir would read as our own claim.
    """
    link = tmp_path / "looks-innocent"
    link.symlink_to(git_common_dir(repo), target_is_directory=True)
    _write(
        tree,
        json.dumps({"version": 1, "repo_common_dir": str(link), "run_id": tree.name}),
    )
    assert owner_of(tree) != git_common_dir(repo)
    assert not _is_reclaimable(repo, tree)


def test_duplicate_keys_are_refused_rather_than_resolved_last_wins(
    tmp_path: Path, repo: Path, tree: Path
) -> None:
    """``json.loads`` silently keeps the LAST duplicate, so the last one wins."""
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=stranger, check=True, capture_output=True)
    _write(
        tree,
        f'{{"version": 1, "run_id": "{tree.name}", '
        f'"repo_common_dir": "{git_common_dir(stranger)}", '
        f'"repo_common_dir": "{git_common_dir(repo)}"}}',
    )
    assert owner_of(tree) is None
    assert not _is_reclaimable(repo, tree)


# --- the remaining shapes --------------------------------------------------


def test_an_unknown_version_is_refused(repo: Path, tree: Path) -> None:
    """An older build must not act on a record a newer one wrote.

    The same rule the metrics view already applies to a future schema.
    """
    for version in (0, 2, 99, "1", None):
        _write(
            tree,
            json.dumps(
                {
                    "version": version,
                    "repo_common_dir": str(git_common_dir(repo)),
                    "run_id": tree.name,
                }
            ),
        )
        assert owner_of(tree) is None, version
        assert not _is_reclaimable(repo, tree), version


def test_a_non_regular_record_is_refused(repo: Path, tree: Path) -> None:
    """A FIFO at the record path used to hang the installer's read forever."""
    os.mkfifo(tree / OWNER_RECORD_NAME)
    assert owner_of(tree) is None
    assert not _is_reclaimable(repo, tree)


def test_an_oversized_record_is_not_read_into_memory(repo: Path, tree: Path) -> None:
    """The record is ~100 bytes; the cap refuses to read an arbitrary file from a
    world-shared directory into memory.

    The payload is otherwise VALID and padded past the cap, so the cap is the only
    thing that can reject it. A first version of this test omitted
    ``repo_common_dir``, which made it pass with the cap removed — it was pinning
    a missing field, not the cap.
    """
    payload = {
        "version": 1,
        "repo_common_dir": str(git_common_dir(repo)),
        "run_id": tree.name,
    }
    assert owner_of(tree) is None
    _write(tree, json.dumps(payload))
    assert owner_of(tree) == git_common_dir(repo), "unpadded, this record is trusted"

    _write(tree, json.dumps({**payload, "pad": "x" * 8192}))
    assert owner_of(tree) is None
    assert not _is_reclaimable(repo, tree)


def test_a_relative_or_non_string_owner_is_refused(repo: Path, tree: Path) -> None:
    for value in ("relative/path", "", 1, None, ["/a"], {"a": 1}):
        _write(tree, json.dumps({"version": 1, "repo_common_dir": value, "run_id": tree.name}))
        assert owner_of(tree) is None, value


def test_the_intact_record_this_repository_writes_is_trusted(repo: Path, tree: Path) -> None:
    """The control: without this, every assertion above could pass vacuously."""
    record_owner(tree, repo)
    assert owner_of(tree) == git_common_dir(repo)
    assert _is_reclaimable(repo, tree)


def test_the_record_is_read_through_one_lookup_not_two(repo: Path, tree: Path) -> None:
    """``lstat`` then open-by-path is check-then-act on a NAME, not on the file read.

    A symlink swapped in between the two makes this tree answer with another
    tree's record. ``O_NOFOLLOW`` refuses it at open time and ``fstat`` describes
    the descriptor actually opened, so there is no second resolution to race.
    This pins the property directly: a symlink at the record path is refused even
    when its target is a perfectly valid record for THIS tree.
    """
    decoy = tree.parent / "decoy"
    decoy.mkdir()
    (decoy / OWNER_RECORD_NAME).write_text(
        json.dumps(
            {
                "version": 1,
                "repo_common_dir": str(git_common_dir(repo)),
                "run_id": tree.name,
            }
        ),
        encoding="utf-8",
    )
    # Read directly, the target is valid for this tree — so only the refusal to
    # FOLLOW can be what rejects it below.
    assert owner_of(decoy) is None, "decoy names another run, so reading it directly fails"

    (tree / OWNER_RECORD_NAME).symlink_to(decoy / OWNER_RECORD_NAME)
    assert owner_of(tree) is None
    assert not _is_reclaimable(repo, tree)


def test_a_fifo_someone_is_actively_writing_is_still_refused(repo: Path, tree: Path) -> None:
    """This is what makes ``S_ISREG`` a guard rather than a decoration.

    Every other non-regular shape is rejected by the READ: a FIFO with no writer
    returns EAGAIN under ``O_NONBLOCK``, a directory returns EISDIR. So removing
    ``S_ISREG`` leaves those green, and it looked unprovable. A FIFO with a live
    writer is the case that separates them — it opens, it reads, and the bytes
    are a perfectly valid record for this tree. Only the check on what the
    descriptor IS refuses it.
    """
    import threading

    payload = json.dumps(
        {
            "version": 1,
            "repo_common_dir": str(git_common_dir(repo)),
            "run_id": tree.name,
        }
    ).encode("utf-8")
    fifo = tree / OWNER_RECORD_NAME
    os.mkfifo(fifo)
    written = threading.Event()

    def _feed() -> None:
        wfd = os.open(fifo, os.O_WRONLY)  # blocks until a reader arrives
        try:
            os.write(wfd, payload)
            written.set()
            # Hold the write end open so the reader never sees EOF mid-test.
            written.wait(timeout=5)
        finally:
            os.close(wfd)

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    # Unblock the feeder and hold a reader open so the payload stays buffered.
    holder = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert written.wait(timeout=5), "fixture must get the payload into the fifo"
        assert owner_of(tree) is None
        assert not _is_reclaimable(repo, tree)
    finally:
        os.close(holder)
        feeder.join(timeout=5)
