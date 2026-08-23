"""Trusted import boundary for standalone producer repositories."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from syncade.git_object_id import is_full_git_object_id
from syncade.process import SubprocessError, SubprocessResult, run_subprocess

CandidateImportStatus = Literal["imported", "invalid", "error"]

_GIT_TIMEOUT_SECONDS = 30.0
_HEX = re.compile(r"^[0-9a-f]+$")
_SOURCE_REF = "refs/syncade/import-source"


@dataclass(frozen=True)
class CandidateImportResult:
    """Mechanical result of validating and anchoring one candidate range."""

    status: CandidateImportStatus
    recovery_ref: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        """Reject every field COMBINATION the importer could not have produced.

        Validating fields individually is not enough, and a blind panel found why: a malformed
        resumed manifest carried ``status="invalid"`` together with a ``recovery_ref`` all the way
        into persisted operator artifacts, which then rendered a plausible and false claim that a
        rejected candidate was anchored and readable. ``invalid`` is raised during validation,
        strictly BEFORE any ref is created, so that pairing is impossible by construction.

        Enforced HERE rather than in the resume rehydrator, so every construction path is covered
        — the importer, resume, and any future reader — instead of the one that happened to be
        audited. Rehydration wraps the failure as a malformed-manifest error, which is the honest
        classification: it is corrupt on-disk data, not a run outcome.
        """
        if self.status == "imported":
            if self.recovery_ref is None or self.error is not None:
                raise ValueError("an imported candidate requires only a recovery_ref")
            return
        if self.error is None:
            raise ValueError("an invalid/error candidate import requires an error")
        if self.status == "invalid" and self.recovery_ref is not None:
            raise ValueError(
                "an invalid candidate cannot carry a recovery ref: validation fails before any "
                "ref is created, so this combination never occurred"
            )


class _InvalidCandidate(Exception):
    pass


class _ImportFailure(Exception):
    def __init__(self, message: str, *, recovery_ref: str | None = None) -> None:
        super().__init__(message)
        self.recovery_ref = recovery_ref


def _actor_entries(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError as exc:
        raise _InvalidCandidate(f"producer object store is unreadable: {path.name}: {exc}") from exc


def _git_env() -> dict[str, str]:
    """Run trusted Git without inherited routing or ambient configuration."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_GRAFT_FILE": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _run_git(argv: list[str], *, cwd: Path) -> SubprocessResult:
    try:
        return run_subprocess(
            argv,
            cwd=cwd,
            env=_git_env(),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except SubprocessError as exc:
        raise _ImportFailure(f"git command {argv[:2]!r} failed: {exc}") from exc


def _git_output(argv: list[str], *, cwd: Path, operation: str) -> str:
    result = _run_git(argv, cwd=cwd)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise _ImportFailure(f"{operation} failed: {detail or f'exit {result.returncode}'}")
    return result.stdout.strip()


def _validation_output(argv: list[str], *, cwd: Path, operation: str) -> str:
    result = _run_git(argv, cwd=cwd)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise _InvalidCandidate(f"{operation} failed: {detail or f'exit {result.returncode}'}")
    return result.stdout.strip()


def _copy_file(source: Path, destination: Path) -> None:
    try:
        mode = source.stat(follow_symlinks=False).st_mode
    except OSError as exc:
        raise _InvalidCandidate(f"producer object is unreadable: {source.name}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise _InvalidCandidate(f"producer object is not a regular file: {source.name}")
    try:
        with source.open("rb") as incoming:
            data = incoming.read()
    except OSError as exc:
        raise _InvalidCandidate(f"producer object is unreadable: {source.name}: {exc}") from exc
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    except OSError as exc:
        raise _ImportFailure(f"cannot stage producer object {source.name}: {exc}") from exc


def _copy_object_store(actor_repo: Path, quarantine: Path, oid_length: int) -> None:
    """Copy object files only; actor refs, config, hooks, grafts, and alternates are ignored."""
    git_dir = actor_repo / ".git"
    objects = git_dir / "objects"
    if actor_repo.is_symlink() or git_dir.is_symlink() or objects.is_symlink():
        raise _InvalidCandidate("producer repository or object store is a symlink")
    if not git_dir.is_dir() or not objects.is_dir():
        raise _InvalidCandidate("producer repository has no real in-root object store")

    destination = quarantine / "objects"
    for loose_dir in _actor_entries(objects):
        if not loose_dir.is_dir() or loose_dir.is_symlink() or not _HEX.fullmatch(loose_dir.name):
            continue
        if len(loose_dir.name) != 2:
            continue
        for loose in _actor_entries(loose_dir):
            name = loose_dir.name + loose.name
            if len(name) == oid_length and _HEX.fullmatch(name):
                _copy_file(loose, destination / loose_dir.name / loose.name)

    pack_dir = objects / "pack"
    if pack_dir.is_symlink():
        raise _InvalidCandidate("producer pack directory is a symlink")
    if pack_dir.is_dir():
        prefix_length = len("pack-") + oid_length
        for packed in _actor_entries(pack_dir):
            if (
                packed.name.startswith("pack-")
                and packed.name[prefix_length:] in {".pack", ".idx"}
                and len(packed.name) == prefix_length + len(packed.suffix)
                and _HEX.fullmatch(packed.name[len("pack-") : prefix_length])
            ):
                _copy_file(packed, destination / "pack" / packed.name)


def _validate_range(repo: Path, starting_sha: str, ending_sha: str) -> None:
    if starting_sha == ending_sha:
        raise _InvalidCandidate("candidate range is empty")
    for label, oid in (("starting", starting_sha), ("ending", ending_sha)):
        kind = _validation_output(
            ["git", "--no-replace-objects", "cat-file", "-t", oid],
            cwd=repo,
            operation=f"{label} object validation",
        )
        if kind != "commit":
            raise _InvalidCandidate(f"{label} object is not a commit")

    fsck = _run_git(
        [
            "git",
            "--no-replace-objects",
            "fsck",
            "--strict",
            "--no-reflogs",
            "--no-dangling",
            "--no-progress",
            ending_sha,
        ],
        cwd=repo,
    )
    if fsck.returncode != 0:
        detail = fsck.stderr.strip() or fsck.stdout.strip()
        raise _InvalidCandidate(f"candidate object graph is invalid: {detail[:400]}")

    ancestry = _run_git(
        [
            "git",
            "--no-replace-objects",
            "merge-base",
            "--is-ancestor",
            starting_sha,
            ending_sha,
        ],
        cwd=repo,
    )
    if ancestry.returncode != 0:
        raise _InvalidCandidate("candidate ending commit is NOT a descendant of the round start")

    raw_range = _validation_output(
        ["git", "--no-replace-objects", "rev-list", "--parents", ending_sha, f"^{starting_sha}"],
        cwd=repo,
        operation="candidate range enumeration",
    )
    parents: dict[str, str] = {}
    for line in raw_range.splitlines():
        fields = line.split()
        if len(fields) != 2 or not all(is_full_git_object_id(field) for field in fields):
            raise _InvalidCandidate("candidate range must be one linear chain without merges")
        commit, parent = (field.lower() for field in fields)
        if commit in parents:
            raise _InvalidCandidate("candidate range contains a duplicate commit")
        parents[commit] = parent
    cursor = ending_sha
    visited: set[str] = set()
    while cursor != starting_sha:
        if cursor in visited or cursor not in parents:
            raise _InvalidCandidate("candidate range is not rooted at the round start")
        visited.add(cursor)
        cursor = parents[cursor]
    if not parents or visited != parents.keys():
        raise _InvalidCandidate("candidate range is not one linear chain")

    start_tree = _validation_output(
        ["git", "--no-replace-objects", "rev-parse", "--verify", f"{starting_sha}^{{tree}}"],
        cwd=repo,
        operation="starting tree validation",
    )
    end_tree = _validation_output(
        ["git", "--no-replace-objects", "rev-parse", "--verify", f"{ending_sha}^{{tree}}"],
        cwd=repo,
        operation="ending tree validation",
    )
    if start_tree == end_tree:
        raise _InvalidCandidate("candidate range has no net tree change")


def _direct_ref(repo: Path, ref: str) -> str | None:
    symbolic = _run_git(["git", "symbolic-ref", "-q", ref], cwd=repo)
    if symbolic.returncode == 0:
        raise _ImportFailure(f"trusted ref collision is symbolic: {ref}")
    result = _run_git(["git", "rev-parse", "--verify", ref], cwd=repo)
    if result.returncode != 0:
        return None
    oid = result.stdout.strip().lower()
    if not is_full_git_object_id(oid):
        raise _ImportFailure(f"trusted ref has malformed target: {ref}")
    return oid


def _create_immutable_ref(repo: Path, ref: str, ending_sha: str, zero: str) -> None:
    existing = _direct_ref(repo, ref)
    if existing == ending_sha:
        return
    if existing is not None:
        raise _ImportFailure(f"trusted ref collision cannot be overwritten: {ref}")
    create = _run_git(["git", "update-ref", "--no-deref", ref, ending_sha, zero], cwd=repo)
    if create.returncode == 0:
        return
    if _direct_ref(repo, ref) == ending_sha:
        return
    raise _ImportFailure(f"trusted ref collision cannot be overwritten: {ref}")


def import_producer_candidate(
    *,
    repo_root: Path,
    producer_repo: Path,
    starting_sha: str,
    ending_sha: str,
    run_id: str,
    round_idx: int,
) -> CandidateImportResult:
    """Validate one actor range, import it, and create its immutable recovery ref."""
    if not is_full_git_object_id(starting_sha) or not is_full_git_object_id(ending_sha):
        return CandidateImportResult(status="invalid", error="candidate requires full object IDs")
    starting_sha = starting_sha.lower()
    ending_sha = ending_sha.lower()
    recovery_ref = f"refs/syncade/recovery/{run_id}/round-{round_idx}/{ending_sha}"
    quarantine_ref = f"refs/syncade/quarantine/{run_id}/round-{round_idx}/{ending_sha}"

    try:
        object_format = _git_output(
            ["git", "rev-parse", "--show-object-format"],
            cwd=repo_root,
            operation="operator object-format lookup",
        )
        expected_length = 40 if object_format == "sha1" else 64 if object_format == "sha256" else 0
        if (
            not expected_length
            or len(starting_sha) != expected_length
            or len(ending_sha) != expected_length
        ):
            raise _InvalidCandidate("candidate object IDs do not match the operator object format")
        for ref in (recovery_ref, quarantine_ref):
            checked = _run_git(["git", "check-ref-format", ref], cwd=repo_root)
            if checked.returncode != 0:
                raise _ImportFailure(f"generated import ref is invalid: {ref}")

        common_text = _git_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=repo_root,
            operation="operator common-directory lookup",
        )
        common_dir = Path(common_text)
        if not common_dir.is_absolute():
            common_dir = repo_root / common_dir
        common_dir = common_dir.resolve(strict=True)

        with tempfile.TemporaryDirectory(prefix="syncade-import-", dir=common_dir) as raw:
            temp_root = Path(raw)
            quarantine = temp_root / "quarantine.git"
            template = temp_root / "template"
            template.mkdir()
            init = ["git", "init", "--quiet", "--bare", f"--template={template}"]
            if object_format == "sha256":
                init.append("--object-format=sha256")
            init.append(str(quarantine))
            _git_output(init, cwd=temp_root, operation="quarantine initialization")
            _copy_object_store(producer_repo, quarantine, expected_length)
            _validate_range(quarantine, starting_sha, ending_sha)

            zero = "0" * expected_length
            source_create = _run_git(
                ["git", "update-ref", "--no-deref", _SOURCE_REF, ending_sha, zero],
                cwd=quarantine,
            )
            if source_create.returncode != 0:
                raise _ImportFailure("cannot anchor the validated quarantine source")
            fetched = _run_git(
                [
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--no-recurse-submodules",
                    "--no-auto-maintenance",
                    str(quarantine),
                    _SOURCE_REF,
                ],
                cwd=repo_root,
            )
            if fetched.returncode != 0:
                detail = fetched.stderr.strip() or fetched.stdout.strip()
                raise _ImportFailure(f"candidate quarantine import failed: {detail[:400]}")

        _create_immutable_ref(repo_root, quarantine_ref, ending_sha, "0" * expected_length)
        try:
            _validate_range(repo_root, starting_sha, ending_sha)
            if _direct_ref(repo_root, quarantine_ref) != ending_sha:
                raise _ImportFailure("imported quarantine ref does not resolve to the candidate")
            _create_immutable_ref(repo_root, recovery_ref, ending_sha, "0" * expected_length)
        except Exception:
            _run_git(
                ["git", "update-ref", "--no-deref", "-d", quarantine_ref, ending_sha],
                cwd=repo_root,
            )
            raise
        deleted = _run_git(
            ["git", "update-ref", "--no-deref", "-d", quarantine_ref, ending_sha],
            cwd=repo_root,
        )
        if deleted.returncode != 0:
            raise _ImportFailure(
                f"candidate imported and recovery-anchored, but quarantine cleanup failed: "
                f"{deleted.stderr.strip()[:300]}",
                recovery_ref=recovery_ref,
            )
        return CandidateImportResult(status="imported", recovery_ref=recovery_ref)
    except _InvalidCandidate as exc:
        return CandidateImportResult(status="invalid", error=str(exc))
    except (OSError, _ImportFailure) as exc:
        return CandidateImportResult(
            status="error",
            recovery_ref=getattr(exc, "recovery_ref", None),
            error=str(exc),
        )
