"""The trusted-side half of workspace ownership: the repo-local CLAIM.

Split out of :mod:`syncade.workspace_owner` (PR-h-06a.1) because the two halves answer
different questions and this one carries the reasoning: the RECORD says which repository
a workspace names, and the CLAIM is the proof, held in trusted repo state, that this
repository actually wrote that record.  ``workspace_owner`` re-exports every public name
here, so importers are unchanged — but MONKEYPATCH THIS MODULE, per the Decomposition
Rule: a package-level patch does not affect the submodule global a function body reads.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

OWNER_RECORD_NAME = ".syncade-owner.json"
"""The record's filename.

It lives in this LEAF rather than beside the record writer because both authorities need
it — the record is written under this name, and the claim is a hard link to that exact
file — and ``workspace_owner`` imports this module, not the reverse. ``workspace_owner``
re-exports it, so every existing importer is unchanged.
"""

# Claim files live at <repo-root>/.syncade/workspace-claims/<run-id-root>.
# Each is a HARD LINK to the workspace's owner record, not a reusable marker.
# The two authorities are therefore one file: a stale link cannot authenticate
# a replacement record at the same shared-base name.  A companion sidecar
# (<run-id-root>.root) records the workspace root directory's identity
# (st_dev, st_ino) at creation time.  workspace_claim_matches() checks both.
# The sidecar aims at the one case the hard link cannot answer — a stale claim
# hard-linked INTO a replacement root, where both links really are one inode —
# and it lands only where the filesystem does not recycle the deleted root's
# inode.  ext4 does, and so does overlayfs on ext4, which is Docker's default,
# so on most Linux the sidecar is inert and check (1) is the whole guard.  See
# workspace_claim_matches() for what that leaves exposed (very little) and why.
# Only (st_dev, st_ino) is recorded — not st_ctime_ns
# — because creating child directories mutates directory ctime during normal
# provisioning but never changes st_ino.  Using .syncade/ (not .git/) keeps
# provisioning out of the operator repository's Git storage.
_WORKSPACE_CLAIMS_SUBDIR = Path(".syncade") / "workspace-claims"
_WORKSPACE_CLAIMS_SIDECAR_SUFFIX = ".root"


def _write_workspace_claim(repo_root: Path, run_id_root: str, record_dir_fd: int) -> None:
    """Hard-link the owner record into trusted repo state. Never raises.

    This is called only by the invocation that atomically created the owner
    record. Linking that exact inode makes the proof non-reusable by a different
    record file: a claim left after manual deletion still names the old inode.
    A cross-device link failure leaves the workspace unclaimable, which leaks
    disk rather than risking deletion of a stranger's files.

    A sidecar (<run-id-root>.root) is written alongside the hard link recording
    the workspace root directory's (st_dev, st_ino).  workspace_claim_matches()
    verifies this matches the live root — which discriminates only where the
    filesystem gives a replacement directory a DIFFERENT inode than the one it
    just freed.  ext4 does not, nor does overlayfs on ext4.  st_ctime_ns is
    deliberately excluded:
    creating child directories during normal provisioning mutates directory
    ctime after the claim is written.
    """
    try:
        claims = repo_root / _WORKSPACE_CLAIMS_SUBDIR
        claims.mkdir(parents=True, exist_ok=True)
        os.link(
            OWNER_RECORD_NAME,
            claims / run_id_root,
            src_dir_fd=record_dir_fd,
            follow_symlinks=False,
        )
        root_stat = os.fstat(record_dir_fd)
        sidecar = claims / (run_id_root + _WORKSPACE_CLAIMS_SIDECAR_SUFFIX)
        sidecar.write_text(
            json.dumps(
                {
                    "st_dev": root_stat.st_dev,
                    "st_ino": root_stat.st_ino,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _regular_file_identity(
    path: str | Path, *, dir_fd: int | None = None
) -> tuple[int, int] | None:
    """``(device, inode)`` for one opened regular file, or ``None``."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        return (info.st_dev, info.st_ino) if stat.S_ISREG(info.st_mode) else None
    except OSError:
        return None
    finally:
        os.close(fd)


def workspace_claim_matches(repo_root: Path, root: Path) -> bool:
    """True iff repo state and ``root`` name the exact same owner-record file
    AND the claim was written against this workspace root's inode.

    Two checks are required, and both must pass:

    1. **Record-inode check.** The workspace record and the repo-side claim file
       are hard links to the same inode.  This rules out forged or copied
       replacement bytes: a different file cannot reuse a stale claim.

    2. **Root-inode check.** The claim's sidecar records the workspace root
       directory's ``(st_dev, st_ino)`` at creation time and we compare it to
       the live root.  It aims at the one case check (1) cannot answer: a stale
       claim hard-linked into a replacement workspace as its record, where both
       links really are the same inode.  A missing sidecar fails closed.
       ``st_ctime_ns`` is not stored because creating child directories mutates
       directory ctime during normal provisioning.

       **Check (2) discriminates only where the filesystem does not recycle the
       deleted root's inode.** Measured as the attack succeeding, on the shape a
       real out-of-band deletion has: ext4 20/20, overlayfs-on-ext4 20/20,
       tmpfs 0/20, macOS APFS 0/20.  Overlay has no answer of its own — it
       inherits the upper filesystem — so Docker's default recycles, and **most
       Linux is on the inert side, CI included.**

       **What holds everywhere is check (1), and it holds by CONSTRUCTION rather
       than by measurement.**  The claim IS a hard link to the record, so while
       it exists the old record inode has ``nlink >= 1`` and cannot be
       reallocated; and hard links cannot cross devices, so the comparison is
       always same-device.  A replacement record is therefore necessarily a
       different ``(st_dev, st_ino)`` on any POSIX filesystem — whatever the
       ROOT's inode did.  So a workspace this code re-creates after an
       out-of-band deletion is never authenticated by the stale claim, and
       copied, forged and foreign records are refused the same way.

       What check (2)'s inertness exposes is exactly one thing: a DELIBERATE
       ``os.link`` of the stale claim into a replacement directory, by a process
       running as this user.  Syncade does not attempt to defend against that,
       here or anywhere else.  Note it needs no timing race, so it is NOT the
       class the ``create_run_dir`` fd-pinning and PR-h-06b resume reverts
       declined — those were races; this is a scope decision on its own footing.

       **No PORTABLE fix exists.**  ext4 does expose a stable directory identity
       past ``(st_dev, st_ino)`` — ``name_to_handle_at`` carries the inode
       generation, measured 0/20 collisions where ``st_ino`` collided 20/20 —
       but overlayfs answers ``EOPNOTSUPP`` and macOS has no equivalent.  A
       nonce does not help either: the attack replays the old record, so it
       replays the nonce.  ``path/to/pr.md`` records the
       measurements.

    Both files are opened without following symlinks.
    """
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        record_identity = _regular_file_identity(OWNER_RECORD_NAME, dir_fd=root_fd)
        root_stat = os.fstat(root_fd)
    finally:
        os.close(root_fd)

    claim_identity = _regular_file_identity(repo_root / _WORKSPACE_CLAIMS_SUBDIR / root.name)
    if record_identity is None or record_identity != claim_identity:
        return False

    # Root-inode check: the sidecar must name the CURRENT root directory.
    sidecar = repo_root / _WORKSPACE_CLAIMS_SUBDIR / (root.name + _WORKSPACE_CLAIMS_SIDECAR_SUFFIX)
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        stored = (int(data["st_dev"]), int(data["st_ino"]))
    except (OSError, KeyError, ValueError, TypeError):
        return False
    return stored == (root_stat.st_dev, root_stat.st_ino)


def remove_workspace_claim(repo_root: Path, run_id_root: str) -> None:
    """Remove the repo-side claim and its sidecar for ``run_id_root``.

    Best effort, never raises. Called after a workspace is successfully removed
    (by GC or by run finalization) to keep trusted runtime state bounded. A
    missed cleanup is safe: the hard link still names the removed workspace's old
    record, not a replacement record.
    """
    try:
        (repo_root / _WORKSPACE_CLAIMS_SUBDIR / run_id_root).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        sidecar_name = run_id_root + _WORKSPACE_CLAIMS_SIDECAR_SUFFIX
        (repo_root / _WORKSPACE_CLAIMS_SUBDIR / sidecar_name).unlink(missing_ok=True)
    except OSError:
        pass
