"""Two repositories sharing one ``worktree_base`` must never reclaim each other's.

Built from the measured shape rather than an invented one. On the author's
machine ``/tmp/syncade`` held 23 workspace trees, of which 9 belonged to an
unrelated project that shares the same default base — they carried that
project's ``package.json``, not this one's. ``worktree_base`` defaults to
``/tmp/syncade``, which no repository owns, so this is the ordinary case and not
a corner.

Against the pre-PR-h-06a implementation this file is RED: ownership was proven
by finding a worktree registered in the repo's ``git worktree list`` under the
tree, and PR-h-05 left reviewer exports and producer stores with no such
registration, so each repository reclaimed NOTHING — including its own. The
"never the other's" half passed vacuously; the "always its own" half is what
fails, which is why both halves are asserted here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from syncade.gc_worktrees import repo_owned_orphan_trees
from syncade.workspace_owner import (
    OWNER_RECORD_NAME,
    OWNER_RECORD_VERSION,
    create_run_dir,
    git_common_dir,
    record_owner,
    workspace_claim_matches,
)


def _sidecar_path(repo_root: Path, run_id: str) -> Path:
    """The claim's root-inode sidecar — check 2's stored side."""
    return repo_root / ".syncade" / "workspace-claims" / (run_id + ".root")


# Run-id shaped for BOTH projects on purpose: an implementation that decided
# ownership by "this directory name looks like a run id" would reclaim every
# tree here, and the cross-direction assertions below would fail.
_ALPHA_RUNS = ("2026-08-24T09-00-00", "2026-08-24T09-30-00")
_BETA_RUNS = ("2026-08-24T10-00-00", "2026-08-24T10-30-00", "2026-08-24T11-00-00")
_RECORDLESS = ("2026-08-01T08-00-00", "2026-08-02T08-00-00")


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    return path


def _workspace(base: Path, run_id: str, owner: Path | None) -> Path:
    """A reviewer-export-shaped tree: real files, no ``.git`` anywhere."""
    tree = base / run_id
    (tree / "round-0" / "reviewer").mkdir(parents=True)
    (tree / "round-0" / "reviewer" / "app.py").write_text("x = 1\n")
    assert not (tree / "round-0" / "reviewer" / ".git").exists()
    if owner is not None:
        record_owner(tree, owner)
    return tree


def test_neither_repository_reclaims_the_others_workspaces(tmp_path: Path) -> None:
    base = tmp_path / "syncade"
    alpha, beta = _repo(tmp_path / "alpha"), _repo(tmp_path / "beta")

    alpha_trees = sorted(_workspace(base, r, alpha) for r in _ALPHA_RUNS)
    beta_trees = sorted(_workspace(base, r, beta) for r in _BETA_RUNS)
    strays = sorted(_workspace(base, r, None) for r in _RECORDLESS)

    candidates = sorted(p for p in base.iterdir() if p.is_dir())
    assert len(candidates) == len(alpha_trees) + len(beta_trees) + len(strays)

    from_alpha = repo_owned_orphan_trees(alpha, candidates, known_run_ids=set())
    from_beta = repo_owned_orphan_trees(beta, candidates, known_run_ids=set())

    # Each reclaims exactly its own — the half that is RED before the fix.
    assert from_alpha == alpha_trees
    assert from_beta == beta_trees

    # Neither reaches across, and the recordless trees belong to no one.
    assert not set(from_alpha) & set(beta_trees)
    assert not set(from_beta) & set(alpha_trees)
    assert not (set(from_alpha) | set(from_beta)) & set(strays)


def test_a_live_run_is_excluded_even_when_the_record_names_us(tmp_path: Path) -> None:
    """``known_run_ids`` still gates: an orphan is a run whose artifacts are GONE."""
    base = tmp_path / "syncade"
    alpha = _repo(tmp_path / "alpha")
    live = _workspace(base, "2026-08-24T12-00-00", alpha)
    dead = _workspace(base, "2026-08-24T13-00-00", alpha)

    candidates = sorted(p for p in base.iterdir() if p.is_dir())
    assert repo_owned_orphan_trees(alpha, candidates, known_run_ids={live.name}) == [dead]


def test_a_repository_that_is_not_a_repository_reclaims_nothing(tmp_path: Path) -> None:
    """No identity to compare against means no claim can be verified."""
    base = tmp_path / "syncade"
    alpha = _repo(tmp_path / "alpha")
    _workspace(base, "2026-08-24T14-00-00", alpha)

    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()
    candidates = sorted(p for p in base.iterdir() if p.is_dir())
    assert repo_owned_orphan_trees(not_a_repo, candidates, known_run_ids=set()) == []


def test_forged_self_naming_record_is_not_reclaimed(tmp_path: Path) -> None:
    """A forged workspace record naming this repo's common dir must not be reclaimed.

    Under the hostile-content threat model a model actor can write any file into
    the shared worktree base, including a syntactically valid ownership record
    that names this repository's git common dir. Without a trusted-side hard
    link to that exact record, it must read as "not ours" and be left alone.

    This test is RED against the single-factor implementation (owner_of alone)
    and GREEN once the claim-file second factor is required.
    """
    base = tmp_path / "syncade"
    alpha = _repo(tmp_path / "alpha")

    # Legitimate workspace: record AND claim file both written by this repo.
    legit_id = "2026-08-24T09-00-00"
    legit = _workspace(base, legit_id, alpha)

    # Forged workspace: record claims alpha's common dir, but alpha never linked this file.
    forged_id = "2026-08-24T10-00-00"
    forged = base / forged_id
    (forged / "round-0").mkdir(parents=True)
    alpha_common = git_common_dir(alpha)
    assert alpha_common is not None, "test repo must have a git common dir"
    forged_record = {
        "version": OWNER_RECORD_VERSION,
        "repo_common_dir": str(alpha_common),
        "run_id": forged_id,
    }
    (forged / OWNER_RECORD_NAME).write_text(json.dumps(forged_record, sort_keys=True))

    candidates = sorted(p for p in base.iterdir() if p.is_dir())
    result = repo_owned_orphan_trees(alpha, candidates, known_run_ids=set())

    assert result == [legit], (
        f"expected only the legitimate workspace to be reclaimed, got {result}"
    )
    assert forged not in result, "forged self-naming record must not be reclaimed"


def test_stale_claim_does_not_authenticate_a_replacement_record(tmp_path: Path) -> None:
    """A repo-side proof is the owner record itself, not a reusable run-id marker."""
    base = tmp_path / "syncade"
    alpha = _repo(tmp_path / "alpha")
    run_id = "2026-08-24T15-00-00"
    original = _workspace(base, run_id, alpha)
    claim = alpha / ".syncade" / "workspace-claims" / run_id

    assert workspace_claim_matches(alpha, original)
    original_record_identity = (original / OWNER_RECORD_NAME).stat()
    claim_identity = claim.stat()
    assert (original_record_identity.st_dev, original_record_identity.st_ino) == (
        claim_identity.st_dev,
        claim_identity.st_ino,
    )

    # Simulate out-of-band deletion: the trusted link deliberately remains.
    shutil.rmtree(original)
    replacement = base / run_id
    (replacement / "round-0").mkdir(parents=True)
    common = git_common_dir(alpha)
    assert common is not None
    (replacement / OWNER_RECORD_NAME).write_text(
        json.dumps(
            {"version": OWNER_RECORD_VERSION, "repo_common_dir": str(common), "run_id": run_id},
            sort_keys=True,
        )
    )

    assert claim.exists(), "precondition: the stale trusted-side link remains"
    assert not workspace_claim_matches(alpha, replacement)
    assert repo_owned_orphan_trees(alpha, [replacement], known_run_ids=set()) == []


def test_stale_claim_hard_linked_into_replacement_does_not_authenticate(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    """A stale claim hard-linked into a replacement workspace as its record must not authenticate.

    Attack: the workspace is deleted (claim survives), a replacement is created
    at the same path, and the stale claim file is hard-linked into the
    replacement as its .syncade-owner.json.  The record and claim now share the
    same inode, so the record-inode check alone passes.  The root-inode sidecar
    check detects that the claim was bound to the OLD root directory inode — but
    ONLY where the filesystem does not hand that inode back for the replacement.

    **Whether it does is a FILESYSTEM property, not a platform one**, measured
    2026-08-31 over this exact create/delete/create sequence:

    ==========================  ==================
    filesystem                  attack succeeds
    ==========================  ==================
    ext4 (CI)                   20/20
    overlay on ext4 (Docker)    20/20
    tmpfs                       0/20
    macOS APFS                  0/20
    ==========================  ==================

    Overlay has no answer of its own — it inherits the upper filesystem, so Docker's
    default (overlay2 on ext4) recycles. **Most Linux is on the recycling side.**
    Filesystems disagree with each other and one of them disagrees with itself
    depending on the layer, which is why the branch below MEASURES the condition
    instead of testing ``sys.platform``. Both outcomes are pinned: where the inode is
    fresh the guard refuses, and where it is recycled the attack succeeds. Pinning the
    second is deliberate — the limitation stays a stated fact rather than a surprise,
    and if it is ever closed this test fails and whoever closed it has to say so.
    """
    base = tmp_path / "syncade"
    alpha = _repo(tmp_path / "alpha")
    run_id = "2026-08-24T17-00-00"
    original = _workspace(base, run_id, alpha)
    claim = alpha / ".syncade" / "workspace-claims" / run_id

    # NO `workspace_claim_matches(alpha, original)` precondition here, deliberately.
    # Reading the record before the deletion triggers an overlayfs COPY-UP, which changes
    # which layer allocates the replacement and therefore whether the root inode is
    # recycled — measured, that one call flips this test from xfail to pass on the DEFAULT
    # Docker filesystem while a real out-of-band deletion still succeeds 20/20. A test that
    # perturbs the property it measures reports on itself. Fixture validity is asserted by
    # test_missing_sidecar_fails_closed and test_legitimate_recreation_..., neither of which
    # deletes the workspace first.

    # Delete the workspace but keep the stale claim (simulate crash / manual cleanup).
    shutil.rmtree(original)
    assert claim.exists(), "precondition: stale claim survives workspace deletion"

    # Create a replacement workspace at the same shared-base name.
    replacement = base / run_id
    (replacement / "round-0").mkdir(parents=True)

    # Hard-link the stale claim into the replacement workspace as its record.
    # This is the attack: record and claim now share the same inode.
    os.link(claim, replacement / OWNER_RECORD_NAME)

    # Verify the attack would fool a record-inode-only check.
    from syncade.workspace_owner import _regular_file_identity

    assert _regular_file_identity(replacement / OWNER_RECORD_NAME) == _regular_file_identity(
        claim
    ), "precondition: hard-link makes record and claim share the same inode"

    # The predicate reads the SIDECAR rather than re-deriving it, so it is literally
    # check 2's own input: if the recycled inode makes the stored root indistinguishable
    # from the live one, check 2 has nothing left to discriminate with.
    stored = json.loads(_sidecar_path(alpha, run_id).read_text(encoding="utf-8"))
    replacement_stat = replacement.stat()
    if (stored["st_dev"], stored["st_ino"]) == (replacement_stat.st_dev, replacement_stat.st_ino):
        # xfail rather than asserting the attack succeeds. Both keep "if this is ever
        # closed, the test fails" (strict=True turns an xpass into a failure), but xfail
        # also keeps the SPECIFICATION below readable — a cold reader can still see what
        # the guard is supposed to do — and prints the limitation in the run summary
        # instead of rendering it as an ordinary green dot on the platform where it bites.
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason="filesystem recycled the root inode; check 2 cannot discriminate",
            )
        )
    assert not workspace_claim_matches(alpha, replacement), (
        "stale claim hard-linked into replacement must not pass workspace_claim_matches"
    )
    assert repo_owned_orphan_trees(alpha, [replacement], known_run_ids=set()) == [], (
        "replacement with hard-linked stale claim must not be selected for reclamation"
    )


def test_record_replaced_in_place_by_a_copy_does_not_authenticate(tmp_path: Path) -> None:
    """Check 1 ALONE, isolated — and on some filesystems its only regression cover.

    The workspace root is never deleted here, so check 2 passes on every filesystem and
    the verdict rests entirely on the record and the claim being one inode. That matters:
    measured, deleting check 1 outright left `tests/gc` and `tests/workspace` fully green
    on APFS, because every other test that exercises it also disturbs the root and check 2
    caught the mutant instead. The primary guard could have been removed on the
    development platform with a green suite.
    """
    base = tmp_path / "syncade"
    alpha = _repo(tmp_path / "alpha")
    run_id = "2026-08-24T21-00-00"
    workspace = _workspace(base, run_id, alpha)
    record = workspace / OWNER_RECORD_NAME

    assert workspace_claim_matches(alpha, workspace), "precondition: a legitimate claim matches"

    # Byte-identical content, brand-new inode, root untouched.
    contents = record.read_text(encoding="utf-8")
    record.unlink()
    record.write_text(contents, encoding="utf-8")

    assert not workspace_claim_matches(alpha, workspace), (
        "a record that is no longer the claim's inode must not authenticate, even though "
        "the root — and therefore check 2 — is unchanged"
    )
    assert repo_owned_orphan_trees(alpha, [workspace], known_run_ids=set()) == []


def test_missing_sidecar_fails_closed(tmp_path: Path) -> None:
    """Check 2's filesystem-INDEPENDENT half, and on ext4 its only regression cover.

    Where the root inode is recycled, the test above xfails, so nothing there can catch
    check 2 being weakened or deleted — measured: removing check 2 outright leaves the
    whole ownership surface green on ext4, which is what CI runs. A missing sidecar must
    fail closed everywhere, so asserting that keeps a live guard on every filesystem.
    """
    base = tmp_path / "syncade"
    alpha = _repo(tmp_path / "alpha")
    run_id = "2026-08-24T18-00-00"
    workspace = _workspace(base, run_id, alpha)

    assert workspace_claim_matches(alpha, workspace), "precondition: a legitimate claim matches"
    sidecar = _sidecar_path(alpha, run_id)
    assert sidecar.is_file(), "precondition: creation wrote the sidecar"
    sidecar.unlink()

    assert not workspace_claim_matches(alpha, workspace), (
        "a claim with no sidecar must fail closed on every filesystem"
    )


def test_legitimate_recreation_is_not_matched_by_the_stale_claim(tmp_path: Path) -> None:
    """The claim `workspace_claim_matches`'s docstring rests on, pinned.

    That docstring says check 1 alone is enough for every shape syncade itself produces,
    because a replacement THIS code creates writes a new record file, which gets a new
    inode even where the recycled ROOT inode makes check 2 inert. Measured on ext4 and
    APFS; asserted here so the claim has a guard rather than a memory.
    """
    base = tmp_path / "syncade"
    alpha = _repo(tmp_path / "alpha")
    run_id = "2026-08-24T19-00-00"
    workspace = _workspace(base, run_id, alpha)
    claim = alpha / ".syncade" / "workspace-claims" / run_id
    stale_claim_inode = claim.stat().st_ino

    shutil.rmtree(workspace)
    assert claim.exists(), "precondition: the stale claim survives out-of-band deletion"
    create_run_dir(base, f"{run_id}/round-0", alpha)

    assert (workspace / OWNER_RECORD_NAME).stat().st_ino != stale_claim_inode, (
        "a re-created workspace must get a NEW record inode, whatever the root's inode did"
    )
    assert not workspace_claim_matches(alpha, workspace), (
        "so the stale claim does not authenticate it — the tree is left unclaimable"
    )


def test_nested_run_id_claim_survives_child_creation(tmp_path: Path) -> None:
    """create_run_dir with a nested run-id must leave a valid claim after child dirs are made.

    The sidecar binds the workspace root inode.  create_run_dir creates nested
    parts (e.g. round-0) AFTER writing the sidecar, which mutates the root
    directory's ctime.  workspace_claim_matches must still return True, meaning
    the sidecar must not store ctime — only (st_dev, st_ino).
    """
    base = tmp_path / "syncade"
    alpha = _repo(tmp_path / "alpha")
    # Nested run-id: root is "2026-08-24T15-00-00", child is "round-0/producer-worktree"
    run_id = "2026-08-24T15-00-00/round-0/producer-worktree"
    create_run_dir(base, run_id, alpha)
    root = base / "2026-08-24T15-00-00"
    assert workspace_claim_matches(alpha, root), (
        "workspace claim must match after create_run_dir creates nested children"
    )
    assert repo_owned_orphan_trees(alpha, [root], known_run_ids=set()) == [root], (
        "nested-provisioned workspace must be selected as an orphan when the run is gone"
    )


def test_existing_forged_record_does_not_gain_a_trusted_claim(tmp_path: Path) -> None:
    """Nested provisioning must not bless a self-naming record it did not create."""
    base = tmp_path / "syncade"
    alpha = _repo(tmp_path / "alpha")
    run_id = "2026-08-24T16-00-00"
    forged = base / run_id
    forged.mkdir(parents=True)
    common = git_common_dir(alpha)
    assert common is not None
    (forged / OWNER_RECORD_NAME).write_text(
        json.dumps(
            {"version": OWNER_RECORD_VERSION, "repo_common_dir": str(common), "run_id": run_id},
            sort_keys=True,
        )
    )

    create_run_dir(base, f"{run_id}/round-0", alpha)

    assert not workspace_claim_matches(alpha, forged)
    assert repo_owned_orphan_trees(alpha, [forged], known_run_ids=set()) == []
