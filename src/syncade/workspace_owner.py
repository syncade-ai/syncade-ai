"""Ownership records for the run workspaces under ``worktree_base``.

``worktree_base`` defaults to ``/tmp/syncade``, a path no single repository
owns, so several repositories create ``<worktree_base>/<run-id>/`` trees side by
side. Garbage collection must never reclaim a tree that belongs to another
repository, and live Git cannot answer the ownership question on its behalf: a
reviewer export carries no ``.git`` at all, and every workspace outlives the run
that made it. So ownership is written down at the one moment it is known for
certain — when the directory is created. The record in the shared tree is
hard-linked into trusted ``<repo>/.syncade/workspace-claims/`` state by the
invocation that creates it. Orphan GC requires those two paths to name the same
file, so hostile bytes in the shared base are never sufficient evidence.

**The workspace root is named, never derived.** Every creator already holds the
run id as a STRING, so the root is ``base_dir / run_id.parts[0]`` — a join, not
a subtraction. Recovering it instead by subtracting ``base_dir`` from a
already-built path made "which directory is the root?" a question about path
normalization, and four consecutive review rounds each answered one spelling of
it and opened the next: ``..`` components, then symlink following, then a
``/tmp`` -> ``/private/tmp`` base whose resolved spelling no longer matched the
caller's, which silently wrote NO record on the default macOS configuration.
Naming the id removes the arithmetic, so none of those spellings exist to get
wrong. The id itself is validated once, at the door.

The module is a leaf (stdlib plus :mod:`syncade.process`) because the three
workspace creators and :mod:`syncade.gc_worktrees` all have to reach it, and
:mod:`syncade.persistence` — which owns the repository's other atomic writer —
imports :mod:`syncade.worktree`, so borrowing that writer here would close an
import cycle.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from contextlib import suppress
from pathlib import Path, PurePosixPath

from syncade.process import SubprocessError, run_subprocess

# Re-exported for import stability: every caller and test that imported these from
# `workspace_owner` before the PR-h-06a.1 split still can. The redundant-alias form marks
# them as deliberate re-exports rather than unused imports. Per the Decomposition Rule,
# MONKEYPATCH `syncade.workspace_claims` — patching them here will not affect the bodies
# that read them.
from syncade.workspace_claims import (
    _WORKSPACE_CLAIMS_SIDECAR_SUFFIX as _WORKSPACE_CLAIMS_SIDECAR_SUFFIX,
)
from syncade.workspace_claims import _WORKSPACE_CLAIMS_SUBDIR as _WORKSPACE_CLAIMS_SUBDIR
from syncade.workspace_claims import OWNER_RECORD_NAME as OWNER_RECORD_NAME
from syncade.workspace_claims import _regular_file_identity as _regular_file_identity
from syncade.workspace_claims import _write_workspace_claim
from syncade.workspace_claims import remove_workspace_claim as remove_workspace_claim
from syncade.workspace_claims import workspace_claim_matches as workspace_claim_matches

OWNER_RECORD_VERSION = 1

# A record is ~100 bytes. The cap is not a guess at a legitimate size, it is a
# refusal to read an arbitrary file from a world-shared directory into memory.
_MAX_RECORD_BYTES = 4096


class WorkspaceOwnerError(FileExistsError):
    """A workspace root exists and belongs to a DIFFERENT repository.

    Subclasses ``FileExistsError`` on purpose. The orchestrator already treats
    that as "this run id is taken, pick another", which is exactly the right
    response, and every caller mapping ``OSError`` to a provisioning failure
    keeps working unchanged.
    """


_GIT_COMMON_DIR_TIMEOUT_SECONDS: float = 30.0


def resolve_best_effort(path: Path) -> Path:
    """``path`` resolved, or ``path`` unchanged when it cannot be."""
    try:
        return path.resolve()
    except OSError:
        return path


def git_common_dir(path: Path) -> Path | None:
    """The resolved Git common directory for ``path``, or ``None``.

    This is the repository's identity for ownership purposes: linked worktrees
    of one repository share a common directory, so they are correctly one owner
    rather than several. Writer and reader must derive it the SAME way — a
    record written from one normalization and compared against another would
    make a repository disown its own workspace — which is why this function is
    shared rather than reimplemented per caller.

    GIT_* variables are stripped from the child environment so an inherited
    GIT_DIR or GIT_WORK_TREE from an outer workspace provisioning step cannot
    route git to a different repository and record the wrong identity.
    """
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        result = run_subprocess(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=path,
            env=clean_env,
            timeout=_GIT_COMMON_DIR_TIMEOUT_SECONDS,
        )
    except SubprocessError:
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    common_dir = Path(raw)
    if not common_dir.is_absolute():
        common_dir = path / common_dir
    return resolve_best_effort(common_dir)


def run_root(base_dir: Path, run_id: str) -> Path:
    """``<base_dir>/<first component of run_id>`` — the tree GC reclaims.

    Managers are handed NESTED ids (``<run-id>/round-2``, and
    ``<run-id>/round-2/producer-worktree``) but GC reclaims whole
    ``<worktree_base>/<run-id>/`` trees, so the record belongs at that top
    level. Raises on any id that is not a plain relative path, because a run id
    is generated by this program and anything else is a bug or an attack — and
    refusing one string at the door is what lets every path below be a simple
    join with nothing to normalize.
    """
    # Validate the raw string before pathlib normalization: PurePosixPath silently
    # strips duplicate slashes and dot components, so "a//b", "a/./b", and
    # "//server/share" would all pass a check on .parts alone.
    if (
        not run_id
        or run_id.startswith("/")
        or any(part in ("", ".", "..") for part in run_id.split("/"))
    ):
        raise ValueError(f"run id must be a plain relative path, got {run_id!r}")
    return base_dir / run_id_parts(run_id)[0]


def run_id_parts(run_id: str) -> tuple[str, ...]:
    """The validated components of ``run_id``. See :func:`run_root` for the rules."""
    if (
        not run_id
        or run_id.startswith("/")
        or any(part in ("", ".", "..") for part in run_id.split("/"))
    ):
        raise ValueError(f"run id must be a plain relative path, got {run_id!r}")
    return PurePosixPath(run_id).parts


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """``json.loads`` keeps the LAST of duplicate keys, silently.

    So ``{"repo_common_dir": "<theirs>", "repo_common_dir": "<ours>"}`` would read
    as ours. This repository has met that exact bypass twice before (the findings
    parser and the installer manifest); rejecting the document is the answer that
    worked both times.
    """
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in ownership record")
        seen[key] = value
    return seen


def _owner_from_fd(dir_fd: int, expected_run_id: str) -> Path | None:
    """:func:`owner_of`, reading the record inside an already-open directory."""
    try:
        fd = os.open(OWNER_RECORD_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        # Both are load-bearing. A FIFO with no writer fails the read below with
        # EAGAIN, but a FIFO someone is actively writing a VALID record into
        # opens and reads fine — S_ISREG is the only thing that refuses it, and
        # the test says so by constructing exactly that.
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RECORD_BYTES:
            return None
        raw = os.read(fd, _MAX_RECORD_BYTES).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)
    return _owner_from_text(raw, expected_run_id)


def _owner_from_text(raw: str, expected_run_id: str) -> Path | None:
    try:
        data = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except ValueError:  # JSON errors, including the duplicate-key refusal
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != OWNER_RECORD_VERSION:
        return None
    if data.get("run_id") != expected_run_id:
        return None
    recorded = data.get("repo_common_dir")
    if not isinstance(recorded, str) or not recorded:
        return None
    candidate = Path(recorded)
    return candidate if candidate.is_absolute() else None


def _write_record_at(dir_fd: int, run_id: str, common_dir: Path) -> bool:
    """Claim the directory behind ``dir_fd``. Return whether this call won.

    Written to a temp name and LINKED into place: the content is complete before
    the record exists, so no reader sees a partial one, and ``link`` fails
    ``EEXIST`` rather than replacing another repository's claim. Everything is
    relative to ``dir_fd``, so no name is resolved twice. Errors return false:
    ownership metadata is best effort and must never fail workspace creation.
    """
    text = json.dumps(
        {
            "version": OWNER_RECORD_VERSION,
            "repo_common_dir": str(common_dir),
            "run_id": run_id,
        },
        sort_keys=True,
    )
    tmp = f"{OWNER_RECORD_NAME}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    created = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        os.link(tmp, OWNER_RECORD_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        created = True
    except OSError:
        pass
    finally:
        with suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
    return created


def record_owner(root: Path, repo_root: Path) -> None:
    """Record that ``repo_root`` owns ``root`` and bind that record to repo state.

    Best effort by design, and never raises. A record that cannot be written is
    simply absent, and an unrecorded workspace is one GC cannot prove it owns,
    so it is left alone — the failure degrades to reclaiming less, never to
    deleting a stranger's files. Failing a review over garbage-collection
    metadata would be the worse trade.
    """
    common_dir = git_common_dir(repo_root)
    if common_dir is None:
        return
    try:
        root.mkdir(parents=True, exist_ok=True)
        dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        created = _write_record_at(dir_fd, root.name, common_dir)
        if created:
            _write_workspace_claim(repo_root, root.name, dir_fd)
    finally:
        os.close(dir_fd)


def owner_of(root: Path) -> Path | None:
    """The Git common directory recorded as owning the tree at ``root``.

    ``None`` means "cannot prove", and every state that is not an intact record
    this repository could have written returns it: absent, unreadable, not a
    regular file, oversized, malformed, carrying duplicate keys, a version this
    build does not know, naming a different run than the directory it sits in, or
    naming anything but an absolute path.

    The costs are not symmetric, which is the whole reason to fail closed: a
    false negative leaks disk, a false positive deletes another repository's
    work.

    Two deliberate refusals:

    - the record is read through ONE lookup. ``lstat`` then open-by-path is two,
      and a symlink swapped in between them makes this tree answer with another
      tree's record. ``O_NOFOLLOW`` refuses it at open time and ``fstat``
      describes the descriptor actually opened. (``O_NONBLOCK`` is there because
      opening a FIFO read-only would otherwise block until a writer appeared.)
    - the recorded path is NOT resolved. It is compared literally against this
      repository's resolved common dir, so a record naming a symlink that
      happens to resolve into this repository does not read as ownership.
      Resolving here would CREATE that attack rather than close it.
    """
    try:
        dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        return _owner_from_fd(dir_fd, root.name)
    finally:
        os.close(dir_fd)


def create_run_dir(base_dir: Path, run_id: str, repo_root: Path, *, exist_ok: bool = True) -> Path:
    """Create ``<base_dir>/<run_id>`` AND record who owns its root. Returns it.

    The one authority. Ownership used to be stamped by each creator calling
    :func:`record_owner` after its own ``mkdir``, which made recording a thing to
    REMEMBER — and the creator that forgot (the orchestrator's own fresh-run
    reservation) produced exactly the unowned tree the record exists to prevent.
    The invocation that creates the record also hard-links that same file into
    trusted repo state; an existing record is never retroactively blessed.

    **Every directory is created and opened relative to a pinned descriptor**,
    never re-resolved by path. Materialising the root and then writing into it by
    NAME is check-then-act: a same-user process can swap a symlink into the gap
    and the record, and every nested workspace, land somewhere else. Re-checking
    ``is_symlink()`` after ``mkdir`` narrows that window without closing it,
    which is the shape this repository already knows does not converge. Holding
    the directory open removes the second lookup instead.

    ``exist_ok`` applies to the ROOT, which is where the orchestrator's atomic
    reservation happens — ``mkdir`` wins or raises, so two runs racing on one id
    cannot both claim it.

    **The boundary, stated because it was once overstated.** What is closed is
    THIS function's own sequence: no name it uses is resolved twice. What is NOT
    closed is what callers do afterwards. The returned value is a PATH, and the
    managers populate it by path — ``git worktree add``, a checkout export, a
    seeded repository — because git takes paths, not descriptors. A same-user
    process that swaps a symlink in after this returns can still capture that
    payload.

    That gap is not fixable by adding checks here, and it was attempted: six
    successive commits pinned the manager's own ``mkdir``, then detected a swap
    after population, then made cleanup fd-relative — 611 lines, and a blind
    panel still reported the class open, unanimously, because the populating
    call resolves the path itself. Closing it needs a different capability
    (populating through a descriptor) rather than another guard.

    The attacker it requires is a same-user local process racing on the shared
    base, which is outside this wave's trusted-repo / untrusted-model threat
    model. So the honest position is a narrow guarantee plainly stated, not a
    broad one defended by an ever-growing list of windows.
    """
    parts = run_id_parts(run_id)
    this_owner = git_common_dir(repo_root)
    # A FileExistsError escaping from here would be read as "this run id is
    # taken" by the orchestrator, which then bumps the id and tries again — a
    # FILE at the base looks like a hundred taken ids and ends in a confusing
    # RuntimeError. That error means one thing only, so a bad base is converted.
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        base_fd = os.open(base_dir, os.O_RDONLY | os.O_DIRECTORY)
    except FileExistsError as exc:
        raise NotADirectoryError(
            errno.ENOTDIR, f"workspace base is not a directory: {base_dir}"
        ) from exc
    try:
        root_was_created = True
        try:
            os.mkdir(parts[0], dir_fd=base_fd)
        except FileExistsError:
            if not exist_ok:
                raise
            root_was_created = False
        try:
            root_fd = os.open(
                parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base_fd
            )
        except OSError as exc:
            # O_NOFOLLOW|O_DIRECTORY already refused it; this only explains why.
            # The raw errno for a symlinked root is ENOTDIR or ELOOP, which tells
            # an operator nothing about what is actually wrong.
            raise WorkspaceOwnerError(
                f"workspace root {base_dir / parts[0]} is not a plain directory "
                f"(a symlink, or not openable): {exc}"
            ) from exc
    finally:
        os.close(base_fd)

    fds = [root_fd]
    try:
        created = _claim(
            root_fd, parts[0], this_owner, base_dir / parts[0], root_was_created=root_was_created
        )
        if created:
            _write_workspace_claim(repo_root, parts[0], root_fd)
        for part in parts[1:]:
            with suppress(FileExistsError):
                os.mkdir(part, dir_fd=fds[-1])
            fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
    finally:
        for fd in fds:
            with suppress(OSError):
                os.close(fd)
    return base_dir / run_id


def _record_file_exists_at(dir_fd: int) -> bool:
    """True when ANY entry named OWNER_RECORD_NAME is present under ``dir_fd``.

    Distinguishes the truly-absent case (ENOENT) from a malformed or unreadable
    record: the former signals a pre-registry/foreign root, the latter may be
    this repository's own interrupted write.
    """
    try:
        fd = os.open(OWNER_RECORD_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    os.close(fd)
    return True


def _claim(
    root_fd: int,
    run_id: str,
    this_owner: Path | None,
    shown: Path,
    *,
    root_was_created: bool = True,
) -> bool:
    """Refuse a foreign or pre-existing recordless root; return whether this call wrote its record.

    Only the invocation that CREATES the root may write its initial claim.  A
    truly absent record on a pre-existing root is refused: adopting it would
    bless pre-registry or foreign shared-base content.  A malformed or
    unreadable record (record file present but unparseable) is NOT refused: it
    may be this repository's own interrupted write.
    """
    existing = _owner_from_fd(root_fd, run_id)
    if existing is not None and existing != this_owner:
        raise WorkspaceOwnerError(
            f"workspace root {shown} is owned by a different repository ({existing})"
        )
    created = False
    if existing is None:
        if not root_was_created and not _record_file_exists_at(root_fd):
            raise WorkspaceOwnerError(
                f"workspace root {shown} already exists without an ownership record; "
                f"refusing to adopt a pre-existing recordless directory"
            )
        if this_owner is not None:
            created = _write_record_at(root_fd, run_id, this_owner)
            # A racing claimant may have won the link; only one record exists and it
            # decides. Re-reading through the SAME descriptor cannot be raced.
            won = _owner_from_fd(root_fd, run_id)
            if won is not None and won != this_owner:
                raise WorkspaceOwnerError(
                    f"workspace root {shown} was claimed by a different repository ({won})"
                )
            created = created and won == this_owner
    return created
