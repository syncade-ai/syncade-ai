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


def _allocated(tree: Path) -> int:
    """Disk given to this tree's REGULAR files, mirroring ``tree_size_bytes``' contract.

    Symlinks are skipped, because ``tree_size_bytes`` skips them. Counting them here
    made this helper disagree with the function it exists to check — invisibly on APFS,
    where a symlink occupies no blocks, and by exactly one block per symlink on ext4,
    which is how CI caught it and the dev machine did not.
    """
    total = 0
    for root, _dirs, files in os.walk(tree):
        for name in files:
            path = os.path.join(root, name)
            st = os.lstat(path)
            if os.path.islink(path):
                continue
            total += st.st_blocks * 512
    return total


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

    got = unclaimable_trees(repo, sorted(base.iterdir()), known_run_ids=set()).all_trees

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

    got = unclaimable_trees(repo, sorted(base.iterdir()), known_run_ids=set()).all_trees

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

    got = unclaimable_trees(repo, sorted(base.iterdir()), known_run_ids=set()).all_trees

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

    got = unclaimable_trees(repo, sorted(base.iterdir()), known_run_ids={"live-run"}).all_trees

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
    assert plan.unclaimable_recordless_trees == [recordless]
    assert plan.unclaimable_bytes == _allocated(recordless)

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

        assert plan.unclaimable_unreadable_trees == [unreadable]
        assert plan.unclaimable_bytes is None
        assert "size unknown (unreadable contents)" in _render([unreadable], None)
    finally:
        unreadable.chmod(0o700)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions are required")
def test_known_workspace_with_unreadable_record_file_is_reported(tmp_path: Path) -> None:
    """An existing but unreadable owner record leaves ownership unproven.

    When ``.syncade-owner.json`` exists but has mode 0, ``lstat()`` still succeeds —
    the tree was silently skipped rather than reported.  A known-run workspace with
    an unreadable record must appear in ``unclaimable_unreadable_trees`` so the
    operator knows to fix permissions and rerun GC.
    """
    from syncade.workspace_owner import OWNER_RECORD_NAME

    repo = _repo(tmp_path / "repo")
    run_id = "known-run-with-unreadable-record"
    (repo / ".syncade" / "runs" / run_id).mkdir()
    base = tmp_path / "wt"
    ws = _tree(base, run_id, payload=64)
    record = ws / OWNER_RECORD_NAME
    record.write_text('{"repo": "some-other-repo"}', encoding="utf-8")
    record.chmod(0)
    try:
        plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=base, worktree_max_age_days=0)

        assert ws in plan.unclaimable_unreadable_trees, (
            "a known-run workspace whose owner record cannot be read must appear as unreadable"
        )
        assert ws not in plan.unclaimable_recordless_trees, (
            "a workspace with a record file — even an unreadable one — is not recordless"
        )
    finally:
        record.chmod(0o600)


def test_record_readability_probe_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readability probe must not load the record file into memory.

    A large or corrupt .syncade-owner.json must not cause GC planning to
    allocate unbounded memory before the directory is proven syncade-shaped or
    repo-local.  Simulating OOM from read_bytes() proves the probe is bounded:
    old code would propagate MemoryError; the fixed code opens a descriptor and
    closes it without reading any bytes.
    """
    from syncade.workspace_owner import OWNER_RECORD_NAME

    repo = _repo(tmp_path / "repo")
    base = tmp_path / "wt"
    ws = _tree(base, "run-large-record")
    record = ws / OWNER_RECORD_NAME
    record.write_bytes(b"x" * 1_000_000)  # large readable record

    def _oom(*args, **kwargs):
        raise MemoryError("simulated OOM from unbounded read")

    monkeypatch.setattr(Path, "read_bytes", _oom)

    # Must complete without MemoryError; the record file exists so the workspace
    # is excluded from unclaimable (it is not recordless).
    result = unclaimable_trees(repo, sorted(base.iterdir()), known_run_ids=set())
    assert ws not in result.all_trees, (
        "a workspace with a record file must not appear in the unclaimable report"
    )


def test_the_reported_size_is_true(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    base = tmp_path / "wt"
    _tree(base, "a", payload=1000)
    _tree(base, "b", payload=2500)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=base, worktree_max_age_days=0)

    trees = plan.unclaimable_recordless_trees
    assert len(trees) == 2
    expected = sum(_allocated(t) for t in trees)
    assert plan.unclaimable_bytes == expected
    assert sum(tree_size_bytes(t) for t in trees) == expected


def test_a_symlinked_entry_is_not_counted(tmp_path: Path) -> None:
    """Sizes must not be inflated by following a link out of the base."""
    base = tmp_path / "wt"
    tree = _tree(base, "a", payload=100)
    big = tmp_path / "elsewhere.bin"
    big.write_bytes(b"y" * 50_000)
    (tree / "round-0" / "link.bin").symlink_to(big)

    # The claim is that the link's 50 KB TARGET is not followed, not any particular
    # byte count — sizes are allocated now, so the one real file rounds to a block.
    got = tree_size_bytes(tree)
    assert got == _allocated(tree)
    assert got < 50_000, "the symlink target outside the base must not be counted"


# --- they are REPORTED, never executed -------------------------------------


def test_gc_never_removes_an_unclaimable_tree(tmp_path: Path) -> None:
    """The list is a notice. Nothing downstream may treat it as a removal set."""
    repo = _repo(tmp_path / "repo")
    base = tmp_path / "wt"
    stranded = _tree(base, "old-run", payload=64)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=base, worktree_max_age_days=0)
    assert plan.unclaimable_recordless_trees == [stranded]

    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert stranded.exists()
    assert (stranded / "round-0" / "blob.bin").read_bytes() == b"x" * 64
    assert stranded not in report.worktrees_removed


# --- the wording ------------------------------------------------------------


def _render(
    trees: list[Path],
    size: int | None,
    *,
    quiet: bool = True,
    unreadable: list[Path] | None = None,
) -> str:
    plan = SimpleNamespace(
        unclaimable_recordless_trees=trees,
        unclaimable_unreadable_trees=unreadable or [],
        unclaimable_bytes=size,
    )
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _report_unclaimable(plan, quiet=quiet)
    return buf.getvalue()


def test_nothing_is_said_when_there_is_nothing_to_say(tmp_path: Path) -> None:
    assert _render([], 0) == ""


def test_the_message_names_both_cases_and_operator_actions(tmp_path: Path) -> None:
    """The summary counts each half separately and names its own next action."""
    out = _render([tmp_path / "old-run"], 1500, unreadable=[tmp_path / "opaque-run"])
    assert "2 workspace(s)" in out
    assert "1.5 KB" in out
    assert "1 recordless syncade-shaped tree(s) to remove yourself" in out
    assert "1 unreadable tree(s) to make inspectable before rerunning GC" in out
    assert "will not remove these paths on this run" in out
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
    out = _render([tmp_path / "old-run"], 1500, quiet=False, unreadable=[tmp_path / "opaque-run"])
    assert f"not removed (recordless workspace — delete it yourself): {tmp_path / 'old-run'}" in out
    assert (
        f"not removed (could not inspect — fix permissions and rerun gc): {tmp_path / 'opaque-run'}"
    ) in out


# --- B01: the two categories carry two different operator actions ------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions are required")
def test_each_reported_path_says_which_of_the_two_actions_applies(tmp_path: Path) -> None:
    """A path proven recordless and a path merely unreadable get DIFFERENT labels.

    The report prescribes two actions — delete the recordless yourself, make the
    unreadable inspectable and rerun — so a single shared label leaves the operator
    unable to sort the paths it just printed. Measured on the author's machine at
    v0.9.0: 38 paths, one identical `recordless or unreadable` label, two actions.
    """
    repo = _repo(tmp_path / "repo")
    known = "known-unreadable-run"
    (repo / ".syncade" / "runs" / known).mkdir()
    base = tmp_path / "wt"
    recordless = _tree(base, "aaa-recordless", payload=64)
    unreadable = _tree(base, known, payload=64)
    unreadable.chmod(0)
    try:
        plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=base, worktree_max_age_days=0)

        assert plan.unclaimable_recordless_trees == [recordless]
        assert plan.unclaimable_unreadable_trees == [unreadable]

        lines = _render_plan(plan, quiet=False).splitlines()
        recordless_line = next(ln for ln in lines if str(recordless) in ln)
        unreadable_line = next(ln for ln in lines if str(unreadable) in ln)

        assert recordless_line != unreadable_line
        # The recordless path must not be described as possibly-unreadable, and the
        # unreadable one must not be asserted permanently recordless.
        assert "unreadable" not in recordless_line
        assert "recordless" not in unreadable_line
    finally:
        unreadable.chmod(0o700)


def _render_plan(plan, *, quiet: bool) -> str:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _report_unclaimable(plan, quiet=quiet)
    return buf.getvalue()


# --- B02: the reported size is disk, not the sum of logical file lengths -----


@pytest.mark.skipif(os.name == "nt", reason="st_blocks is POSIX-only")
def test_the_reported_size_is_the_disk_the_operator_gets_back(tmp_path: Path) -> None:
    """GC reports ALLOCATED bytes, because that is what deleting the tree returns.

    Summing ``st_size`` understates a tree of many small files by whatever each
    one is rounded up to. Measured on the author's corpus at v0.9.0: 1.81 GB
    reported against 2.14 GB of real disk across the 38 stranded trees — a 15.6%
    understatement in the one number the upgrade note asks an operator to act on.
    """
    base = tmp_path / "wt"
    tree = _tree(base, "many-small-files")
    for i in range(64):
        (tree / "round-0" / f"f{i}.txt").write_bytes(b"x")

    got = tree_size_bytes(tree)

    assert got == _allocated(tree)
    assert got > 64, (
        "64 one-byte files occupy at least one block each; a size of 64 is the "
        "sum of logical lengths, not the disk the operator reclaims"
    )


@pytest.mark.skipif(os.name == "nt", reason="du(1) and st_blocks are POSIX-only")
def test_the_reported_size_is_bounded_by_du_and_never_understates_files(tmp_path: Path) -> None:
    """`du` is the oracle, but the claim is a BOUND, not equality — and that is deliberate.

    This test asserted `== du_kb * 1024` and passed on APFS, where directory inodes
    occupy zero blocks. On ext4 they occupy one block each, so CI failed by exactly
    8192 bytes over two directories. The implementation was right and the assertion was
    the over-claim — the same over-claim that cost this slice a four-round loop, left in
    the one place that executes after being corrected in three places that only read.

    What is actually promised: never understate the disk the files hold, never exceed
    what `du` reports. `tree_size_bytes` omits directory inodes and symlinks, both of
    which `du` counts, so it sits at or below `du` on every filesystem.
    """
    base = tmp_path / "wt"
    tree = _tree(base, "a", payload=3000)
    for i in range(20):
        (tree / "round-0" / f"g{i}.bin").write_bytes(b"y" * (i * 700))

    du_bytes = (
        int(
            subprocess.run(
                ["du", "-sk", str(tree)], check=True, capture_output=True, text=True
            ).stdout.split()[0]
        )
        * 1024
    )
    got = tree_size_bytes(tree)

    assert got == _allocated(tree), "must account for every regular file's allocated blocks"
    assert got <= du_bytes, (
        f"reported {got} exceeds du's {du_bytes} — the report must never overstate disk"
    )
    assert du_bytes - got <= 4096 * 2, (
        f"gap of {du_bytes - got} exceeds the documented ceiling of one block per "
        "directory; the tree has two"
    )


@pytest.mark.skipif(os.name == "nt", reason="st_blocks is POSIX-only")
def test_freed_transcript_bytes_are_disk_too(tmp_path: Path) -> None:
    """`bytes_freed` is the OTHER number GC presents as disk, and it had the same bug.

    Fixing only the unclaimable report would leave one of two operator-facing size
    figures measured in logical bytes and the other in allocated ones.
    """
    repo = _repo(tmp_path / "repo")
    run_dir = repo / ".syncade" / "runs" / "old-run"
    round_dir = run_dir / "round-0"
    round_dir.mkdir(parents=True)
    (run_dir / "run-init.json").write_text("{}", encoding="utf-8")
    (run_dir / "loop-manifest.json").write_text('{"final_exit_code": 0}', encoding="utf-8")
    for i in range(48):
        (round_dir / f"r{i}.stdout").write_bytes(b"x")
    logical = sum(p.stat().st_size for p in round_dir.glob("*.stdout"))
    allocated = _allocated(round_dir)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path / "wt")
    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert logical < allocated, "fixture must have files smaller than one block"
    assert report.bytes_freed == allocated, (
        "transcripts freed must be reported as the disk returned, not the sum of "
        f"logical lengths ({logical})"
    )


@pytest.mark.skipif(os.name == "nt", reason="os.mkfifo and st_blocks are POSIX-only")
def test_fifo_owner_record_on_known_run_workspace_is_reported_not_hung(
    tmp_path: Path,
) -> None:
    """A FIFO at the owner-record path must not block GC planning.

    The unreadable-record fix introduced a ``read_bytes()`` probe that opens the
    record path and blocks forever when it is a FIFO.  The fix is to inspect
    ``lstat().st_mode`` first and refuse to call ``read_bytes()`` on non-regular
    files.  A FIFO record on a known-run workspace must appear as unreadable, not
    cause the process to hang.
    """
    from syncade.workspace_owner import OWNER_RECORD_NAME

    repo = _repo(tmp_path / "repo")
    run_id = "known-run-with-fifo-record"
    (repo / ".syncade" / "runs" / run_id).mkdir()
    base = tmp_path / "wt"
    ws = _tree(base, run_id, payload=64)
    record = ws / OWNER_RECORD_NAME
    os.mkfifo(record)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=base, worktree_max_age_days=0)

    assert ws in plan.unclaimable_unreadable_trees, (
        "a known-run workspace with a FIFO owner record must appear as unreadable"
    )
    assert ws not in plan.unclaimable_recordless_trees, (
        "a workspace with a record file (FIFO) is not recordless"
    )


@pytest.mark.skipif(os.name == "nt", reason="symlinks and st_blocks are POSIX-only")
def test_symlinked_owner_record_on_known_run_workspace_is_reported(
    tmp_path: Path,
) -> None:
    """A symlink at the owner-record path must be classified as unreadable, not skipped.

    owner_of() refuses symlink records.  If the record path is a symlink to a
    readable regular file, read_bytes() would succeed and the workspace would be
    silently dropped from all plan fields.  The fix checks lstat().st_mode and
    treats non-regular-file records the same as unreadable ones for known-run
    trees.
    """
    from syncade.workspace_owner import OWNER_RECORD_NAME

    repo = _repo(tmp_path / "repo")
    run_id = "known-run-with-symlinked-record"
    (repo / ".syncade" / "runs" / run_id).mkdir()
    base = tmp_path / "wt"
    ws = _tree(base, run_id, payload=64)
    target = tmp_path / "real-record.json"
    target.write_text('{"some": "data"}', encoding="utf-8")
    (ws / OWNER_RECORD_NAME).symlink_to(target)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=base, worktree_max_age_days=0)

    assert ws in plan.unclaimable_unreadable_trees, (
        "a known-run workspace with a symlinked owner record must appear as unreadable"
    )
    assert ws not in plan.unclaimable_recordless_trees, (
        "a workspace with a record file (symlink) is not recordless"
    )


@pytest.mark.skipif(os.name == "nt", reason="hard links and st_blocks are POSIX-only")
def test_hardlinked_workspace_files_not_double_counted(tmp_path: Path) -> None:
    """Hard-linked inodes must be counted once by tree_size_bytes.

    If two directory entries share an inode, deleting the tree returns disk equal
    to one copy.  Counting each entry independently overstates by a factor of the
    link count and diverges from ``du -sk``.
    """
    base = tmp_path / "wt"
    tree = _tree(base, "a")
    original = tree / "round-0" / "data.bin"
    original.write_bytes(b"x" * 4096)
    os.link(original, tree / "round-0" / "data-link.bin")

    single_inode_size = os.lstat(original).st_blocks * 512
    got = tree_size_bytes(tree)

    assert got == single_inode_size, (
        f"hard-linked inode counted {got // single_inode_size}x instead of once"
    )


@pytest.mark.skipif(os.name == "nt", reason="hard links and st_blocks are POSIX-only")
def test_hardlinked_transcripts_bytes_freed_not_double_counted(tmp_path: Path) -> None:
    """Hard-linked transcript inodes must be counted once in bytes_freed.

    Two hard-linked transcript names refer to the same on-disk blocks; unlinking
    both returns only one copy's worth of disk.  bytes_freed must reflect the
    actual disk delta, not the sum of allocated bytes per path.
    """
    repo = _repo(tmp_path / "repo")
    run_dir = repo / ".syncade" / "runs" / "old-run"
    round_dir = run_dir / "round-0"
    round_dir.mkdir(parents=True)
    (run_dir / "run-init.json").write_text("{}", encoding="utf-8")
    (run_dir / "loop-manifest.json").write_text('{"final_exit_code": 0}', encoding="utf-8")
    original = round_dir / "r0.stdout"
    original.write_bytes(b"x" * 4096)
    os.link(original, round_dir / "r0.stderr")

    single_inode_size = os.lstat(original).st_blocks * 512

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path / "wt")
    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert report.bytes_freed == single_inode_size, (
        "two hard-linked transcripts share one inode; bytes_freed must reflect "
        f"one inode's disk ({single_inode_size}), not two ({2 * single_inode_size})"
    )
