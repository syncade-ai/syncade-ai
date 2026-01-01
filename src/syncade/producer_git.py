"""Git-side helpers for the producer phase.

Split out of ``producer.py`` when it crossed the blocking 500-LOC cap (PR-v2-24). These
three are one cohesive concern -- reading the worktree's HEAD and staging the producer's
inputs -- and are entirely separable from the run loop that drives the subprocess.

``_read_worktree_head`` is the load-bearing one: HEAD before vs after the subprocess is
how the orchestrator distinguishes "committed" from "stalled", which is the invariant
that producer work is accepted ONLY through commits.
"""

from __future__ import annotations

import dataclasses
import filecmp
import hashlib
import shutil
from pathlib import Path

from syncade.adapters.producer import ProducerOutput
from syncade.git_object_id import is_full_git_object_id
from syncade.process import (
    SubprocessError,
    SubprocessNotFoundError,
    SubprocessTimeoutError,
    run_subprocess,
)
from syncade.producer_result import ProducerResult


def _read_worktree_head(worktree_path: Path) -> str:
    """Return ``git rev-parse HEAD`` in ``worktree_path``.

    Internal helper used at both ends of the producer run to derive
    the (starting_sha, ending_sha) tuple stall detection compares.
    Failures bubble as :class:`SubprocessError` — the orchestrator
    treats them as subprocess-error outcomes (exit 40) since a
    worktree that can't be queried for HEAD isn't usable for the
    next round either.

    Uses :func:`syncade.process.run_subprocess` (not
    :func:`subprocess.run` directly) so the shared subprocess
    machinery (timeout handling, error classification, process-
    group cleanup) is exercised consistently with the rest of
    syncade.
    """
    try:
        result = run_subprocess(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree_path,
            timeout=10.0,
        )
    except SubprocessNotFoundError as exc:
        # ``git`` itself missing is an environment failure — bubble.
        raise SubprocessError(
            f"producer: git binary missing while reading worktree HEAD at {worktree_path}: {exc}"
        ) from exc
    except SubprocessTimeoutError as exc:
        raise SubprocessError(
            f"producer: git rev-parse HEAD timed out at {worktree_path} (timeout={exc.timeout}s)"
        ) from exc
    if result.returncode != 0:
        raise SubprocessError(
            f"producer: git rev-parse HEAD failed at {worktree_path}: "
            f"rc={result.returncode}, stderr: {result.stderr.strip()[:200]!r}"
        )
    sha = result.stdout.strip()
    if not is_full_git_object_id(sha):
        raise SubprocessError(
            f"producer: git rev-parse HEAD at {worktree_path} returned "
            f"unexpected value {sha!r} (expected full SHA-1/SHA-256 object ID)"
        )
    return sha


def _best_effort_head_after_failure(worktree_path: Path, fallback_sha: str) -> str:
    try:
        return _read_worktree_head(worktree_path)
    except SubprocessError:
        return fallback_sha


def _authoritative_head(worktree_path: Path) -> str | None:
    """The worktree's real HEAD, or ``None`` when it cannot be read.

    Unlike the ``ending_sha`` an error-path :class:`ProducerResult` carries — which
    :func:`_best_effort_head_after_failure` may have COLLAPSED to ``starting_sha`` when its own
    read failed — this reports the truth: a moved HEAD as itself, an unreadable HEAD as ``None``
    (never silently as ``starting_sha``). That distinction is load-bearing for the retry gate
    (PR-v2-22 Q3): resetting on a HEAD we merely failed to observe would destroy a real commit.
    """
    try:
        return _read_worktree_head(worktree_path)
    except SubprocessError:
        return None


def _accept_committed_after_error(result: ProducerResult, *, ending_sha: str) -> ProducerResult:
    """Re-classify a ``subprocess_error`` whose producer ACTUALLY committed (HEAD moved to
    ``ending_sha``) into a ``committed`` outcome, so the orchestrator fast-forwards the branch
    instead of dropping the work at exit 40 (PR-v2-22 C1 — *never discard a made commit*).

    ``committed`` requires ``output is not None`` and ``error is None`` (:class:`ProducerResult`
    invariant), so the trailing error is folded into a synthesized narrative rather than kept on
    ``.error``. The branch-advance path independently re-gates descendant-ness (``git merge-base
    --is-ancestor``), so a non-descendant move is still caught downstream.
    """
    err = result.error
    detail = f"{type(err).__name__}: {err}" if err is not None else "an unspecified error"
    narrative = (
        f"[syncade] The producer committed (HEAD -> {ending_sha[:12]}) and its session then ended "
        f"with {detail}. The commit is accepted; the trailing error did not discard it."
    )
    return dataclasses.replace(
        result,
        outcome="committed",
        ending_sha=ending_sha,
        output=ProducerOutput(narrative_text=narrative),
        error=None,
    )


def _reset_worktree(worktree_path: Path, starting_sha: str) -> None:
    """Discard whatever a crashed producer attempt left in the worktree, so a transient RETRY
    starts from EXACTLY the state the first attempt saw (PR-v2-22 C2). ``git reset --hard
    <starting_sha>`` drops tracked/staged edits + any partial commit; ``git clean -ffd`` drops
    untracked files + dirs, including nested git repositories (the second ``-f`` is required;
    a single ``-f`` silently leaves nested repos in place with exit code 0).

    NOT ``git clean -ffdx``: a producer only edits TRACKED source, and ``-x`` would ALSO nuke the
    gitignored worktree-env scaffolding (the ``sitecustomize`` shim + ``PYTHONPATH=<wt>/src``
    layout) the retried subprocess needs to import ``syncade`` — a self-inflicted failure. Only
    reached with ``HEAD == starting_sha`` (the caller's HEAD-gate never resets over a real
    commit, C1).

    Raises :class:`SubprocessError` (or its subclasses) on any failure — a non-zero exit code,
    launch error, or timeout — so the caller aborts the retry rather than proceeding on a dirty
    worktree. A silently-ignored reset failure would allow attempt 1's partial edits to survive
    into attempt 2, violating C2.
    """
    for argv in (["git", "reset", "--hard", starting_sha], ["git", "clean", "-ffd"]):
        result = run_subprocess(argv, cwd=worktree_path, timeout=10.0)
        if result.returncode != 0:
            raise SubprocessError(
                f"producer: {argv[1]} failed in {worktree_path}: "
                f"rc={result.returncode}, stderr={result.stderr.strip()[:200]!r}"
            )


def _stage_producer_input(src: Path, *, worktree_path: Path, repo_root: Path) -> str:
    """Stage one producer input inside the worktree; return a
    worktree-relative reference to it.

    Confinement (H4): the producer runs ``yolo`` with cwd = its own
    worktree. Handing it an ABSOLUTE main-repo or run-artifact path
    lures it OUT of that isolated worktree to read or edit the live
    operator repo — the same escape the reviewer path was closed
    against structurally (see ``orchestrator/round.py``). So every input
    is copied INTO the worktree and referenced by a path relative to the
    worktree root, which the producer resolves against its own cwd.

    Mirrors the reviewer PR-doc staging in ``round.py``: an input that is
    a tracked in-repo file is already present at the same relative path
    (the copy is a no-op); a run artifact (``findings.md``,
    ``test-run.stdout``) is copied in; an out-of-repo PR doc is staged
    under a reserved, collision-free ``.syncade-inputs/<digest>-<name>``
    path so a same-named tracked file cannot shadow it (H2). Run
    artifacts carry their repo-relative ``.syncade/runs/...`` path, so
    they land under the worktree's gitignored ``.syncade/runs/`` and
    cannot be swept into a producer commit. ``Path.relative_to`` yields a
    forward-only subpath (never ``..``), so the staged ref can never
    escape the worktree.
    """
    try:
        ref = str(src.relative_to(repo_root))
    except ValueError:
        # Out-of-repo input (e.g. an external PR doc): a bare basename
        # would be SHADOWED by a tracked worktree file of the same name —
        # the no-op-if-exists copy below would be skipped and the producer
        # would read the WRONG (tracked) file. Stage it under a reserved,
        # collision-free ref and copy unconditionally, mirroring the
        # reviewer PR-doc staging in round.py (H2).
        digest = hashlib.sha256(str(src).encode("utf-8")).hexdigest()[:16]
        ref = f".syncade-inputs/{digest}-{src.name}"
        dest = worktree_path / ref
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return ref
    dest = worktree_path / ref
    # Refresh from the pristine source when the staged copy is MISSING or has been MUTATED (PR-v2-22
    # F8): a run-artifact input corrupted by a crashed prior attempt SURVIVES the gitignored reset
    # (`.syncade/runs/` is untouched by `git clean -ffd`), so a retry must re-copy it or read
    # corrupted input (a C2 violation). The content compare keeps the happy path byte-identical (no
    # copy when it already matches — including the same-root case where dest IS src, which also
    # avoids SameFileError).
    if not dest.exists() or not filecmp.cmp(src, dest, shallow=False):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return ref
