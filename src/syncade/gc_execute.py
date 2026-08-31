"""Best-effort destructive execution for ``syncade --gc`` plans."""

from __future__ import annotations

import os
import shutil
import signal
import sys
from pathlib import Path

from syncade.gc_protection import (
    current_protected_run_ids,
    orphan_worktree_still_orphan_now,
    run_dir_slimmable_now,
    run_id_protected_now,
)
from syncade.gc_types import BULK_ARTIFACT_SUFFIXES, GcPlan, GcReport
from syncade.gc_worktrees import tree_contains_repo_root, tree_identity
from syncade.process import SubprocessError, run_subprocess
from syncade.workspace_owner import remove_workspace_claim

_LSOF_TIMEOUT_SECONDS: float = 30.0
_GIT_PRUNE_TIMEOUT_SECONDS: float = 30.0


def execute_gc(plan: GcPlan, *, dry_run: bool, repo_root: Path) -> GcReport:
    """Carry out a :class:`GcPlan`. Best-effort; never raises out."""
    errors: list[str] = []
    pids_reaped: list[int] = []
    worktrees_removed: list[Path] = []
    worktrees_declined: list[Path] = []
    worktrees_failed: list[Path] = []
    worktrees_refused: list[Path] = []
    protected = set(plan.protected_run_ids) | current_protected_run_ids(repo_root)
    # Tier-3 age release (PR-h-12 item 2). Subtracting these from `protected` above does NOT
    # work and the first version tried it: `run_id_protected_now` falls through to a DISK
    # re-check that re-derives protection from the run directory, which has not changed —
    # only the run's AGE releases it. So the bound computed correctly and then removed nothing.
    # Unit tests over `plan_gc` could not see that; the end-to-end CLI run did.
    #
    # Bypassing the re-check here is safe precisely because age is the criterion: the TOCTOU
    # guard exists for a run that became resume-eligible BETWEEN plan and execute, and these
    # were already resume-eligible at plan time and released on an age that cannot change in
    # the seconds since. Transcripts still go through the unmodified guard.
    age_released = set(plan.worktree_age_released)

    pruned_any = False
    for tree in plan.worktree_trees_to_remove:
        if tree.name not in age_released and run_id_protected_now(repo_root, tree.name, protected):
            continue
        if tree_contains_repo_root(tree, repo_root):
            errors.append(f"skipping worktree tree {tree}: it contains repo root {repo_root}")
            worktrees_refused.append(tree)
            continue
        if not _planned_tree_identity_still_matches(plan, tree, errors, dry_run=dry_run):
            worktrees_refused.append(tree)
            continue
        reaped, removed = _reap_and_remove_tree(tree, dry_run=dry_run, errors=errors)
        pids_reaped.extend(reaped)
        if removed is None:
            worktrees_declined.append(tree)
        elif removed:
            worktrees_removed.append(tree)
            pruned_any = True
            if not dry_run:
                remove_workspace_claim(repo_root, tree.name)
        else:
            worktrees_failed.append(tree)

    for tree in plan.orphan_worktree_trees:
        if tree_contains_repo_root(tree, repo_root):
            errors.append(f"skipping worktree tree {tree}: it contains repo root {repo_root}")
            worktrees_refused.append(tree)
            continue
        if not _planned_tree_identity_still_matches(plan, tree, errors, dry_run=dry_run):
            worktrees_refused.append(tree)
            continue
        if not orphan_worktree_still_orphan_now(repo_root, tree, protected):
            continue
        reaped, removed = _reap_and_remove_tree(tree, dry_run=dry_run, errors=errors)
        pids_reaped.extend(reaped)
        if removed is None:
            worktrees_declined.append(tree)
        elif removed:
            worktrees_removed.append(tree)
            pruned_any = True
            if not dry_run:
                remove_workspace_claim(repo_root, tree.name)
        else:
            worktrees_failed.append(tree)

    if pruned_any and not dry_run:
        _git_worktree_prune(repo_root, errors)

    runs_slimmed: list[str] = []
    bytes_freed = 0
    runs_root = repo_root / ".syncade" / "runs"
    for run_id in plan.runs_to_slim:
        run_dir = runs_root / run_id
        if not run_dir_slimmable_now(run_dir, run_id, protected):
            continue
        slimmed = _slim_run_dir(run_dir, runs_root, dry_run=dry_run, errors=errors)
        if slimmed is None:
            continue
        freed, removed = slimmed
        bytes_freed += freed
        # Report on artifacts REMOVED, not bytes freed. Bytes are the wrong proxy for
        # "did anything happen": a run whose transcripts are all zero-byte gets its
        # files unlinked while `freed` stays 0, so keying off bytes made the report
        # claim nothing was slimmed while the tree was mutated — and made --gc-dry-run
        # promise the same. An already-slim run removes nothing, so idempotence still
        # holds.
        if removed > 0:
            runs_slimmed.append(run_id)

    return GcReport(
        runs_slimmed=runs_slimmed,
        worktrees_removed=worktrees_removed,
        worktrees_declined=worktrees_declined,
        worktrees_failed=worktrees_failed,
        worktrees_refused=worktrees_refused,
        pids_reaped=pids_reaped,
        bytes_freed=bytes_freed,
        errors=errors,
        dry_run=dry_run,
    )


def _slim_run_dir(
    run_dir: Path, runs_root: Path, *, dry_run: bool, errors: list[str]
) -> tuple[int, int] | None:
    """Prune only the bulky subprocess transcripts under ``round-*/``.

    The run directory itself, its run-root artifacts (``loop-manifest.json``,
    ``run-init.json``, ``findings.md``, ``handoff.md``, ``status.json`` …) and every
    structured per-round artifact (round manifests, parsed findings, summaries, exit
    codes) all SURVIVE. See :data:`BULK_ARTIFACT_SUFFIXES` for why.

    Returns ``(bytes_freed, artifacts_removed)`` — both 0 if the run was already slim —
    or ``None`` if the run dir could not be walked at all, in which case the caller
    skips it rather than reporting a slim that did not happen.

    The count is returned alongside the bytes because bytes alone cannot answer "did
    anything happen": a zero-byte transcript is still an artifact that gets unlinked.

    **Symlinks are refused, not followed, and containment is anchored to THIS RUN's
    resolved directory — not merely to the corpus root.** Four escapes have been
    reproduced against earlier drafts of this function:

    1. a ``round-*`` entry that is a symlink → refused in the listing filter AND
       rechecked at the top of :func:`_bulk_artifacts_under` (TOCTOU defense);
    2. a symlinked subdirectory beneath a legitimate round dir → ``os.walk`` runs with
       ``followlinks=False``;
    3. **the run directory itself swapped for a symlink between plan and execute** —
       :func:`~syncade.gc_protection.run_dir_slimmable_now` independently refuses a
       symlinked run dir; ``run_dir_resolved.relative_to(corpus_root)`` is the
       second, load-bearing containment layer;
    4. **a round directory swapped for a symlink pointing at a protected run inside the
       same corpus** (intra-corpus TOCTOU, found by round-1 reviewers, 2026-07-12).
       Anchoring containment on ``corpus_root`` alone would allow this: a file from
       another run inside the corpus would pass the corpus-relative check. Anchoring on
       ``run_dir_resolved`` closes it — a file inside a different run is not relative to
       this run's resolved path.
    """
    if run_dir.is_symlink():
        errors.append(f"skipping run dir {run_dir}: it is a symlink")
        return None
    try:
        corpus_root = runs_root.resolve(strict=True)
        # The run dir must genuinely live inside the corpus — not merely claim to.
        run_dir_resolved = run_dir.resolve(strict=True)
        run_dir_resolved.relative_to(corpus_root)
        entries = list(run_dir.iterdir())
    except (OSError, ValueError) as exc:
        errors.append(
            f"skipping run dir {run_dir}: not a real directory inside {runs_root} ({exc})"
        )
        return None
    round_dirs = [
        d for d in entries if d.name.startswith("round-") and not d.is_symlink() and d.is_dir()
    ]

    freed = 0
    removed = 0
    for round_dir in round_dirs:
        for artifact in _bulk_artifacts_under(round_dir, run_dir_resolved, errors):
            try:
                size = artifact.stat().st_size
            except OSError:
                continue
            if dry_run:
                freed += size
                removed += 1
                continue
            try:
                artifact.unlink()
                freed += size
                removed += 1
            except OSError as exc:
                errors.append(f"failed to remove transcript {artifact}: {exc}")
    return freed, removed


def _bulk_artifacts_under(round_dir: Path, run_root: Path, errors: list[str]) -> list[Path]:
    """Transcripts under ``round_dir`` that provably live inside ``run_root``
    (the resolved run directory).

    Walks with ``followlinks=False`` so a symlinked subdirectory cannot widen the
    blast radius, skips symlinked files, and containment-checks every survivor against
    the resolved run root — not the corpus root. Anchoring on the run rather than the
    corpus is the key fix for the intra-corpus round-dir TOCTOU: a symlinked round dir
    pointing at a *different* run inside the same corpus would pass a corpus-anchored
    check, but fails a run-anchored one. **GC never unlinks anything outside this run.**

    The first line is a TOCTOU recheck: ``round_dir`` may have been swapped to a
    symlink after the caller's ``not d.is_symlink()`` filter; ``os.walk`` still
    traverses a top-level symlink even with ``followlinks=False``, so we must recheck
    before walking.
    """
    if round_dir.is_symlink():
        errors.append(f"skipping round dir {round_dir}: became a symlink (TOCTOU)")
        return []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(round_dir, followlinks=False):
        here = Path(dirpath)
        # os.walk(followlinks=False) still *lists* symlinked dirs; don't descend.
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
        for name in filenames:
            candidate = here / name
            if candidate.suffix not in BULK_ARTIFACT_SUFFIXES:
                continue
            if candidate.is_symlink():
                continue
            try:
                candidate.resolve(strict=True).relative_to(run_root)
            except (OSError, ValueError):
                errors.append(f"skipping transcript outside the run directory: {candidate}")
                continue
            found.append(candidate)
    return found


def _planned_tree_identity_still_matches(
    plan: GcPlan, tree: Path, errors: list[str], *, dry_run: bool
) -> bool:
    expected = plan.worktree_tree_identities.get(tree)
    if expected is None:
        if dry_run:
            return True
        if tree_identity(tree) is None:
            return True
        errors.append(f"skipping worktree tree {tree}: GC plan has no recorded directory identity")
        return False
    actual = tree_identity(tree)
    if actual == expected:
        return True
    errors.append(
        f"skipping worktree tree {tree}: it changed since GC planning "
        f"(planned identity={expected!r}, current identity={actual!r})"
    )
    return False


def _reap_and_remove_tree(
    tree: Path, *, dry_run: bool, errors: list[str]
) -> tuple[list[int], bool | None]:
    """Prove the tree is free, reap what is in it, then rmtree it.

    Returns ``(reaped_pids, removed)``, where ``removed`` is ``True`` (gone),
    ``False`` (tried and failed) or ``None`` (DECLINED — the tree may still be in use,
    so it was not removed; anything reaped before that decision is still returned rather
    than discarded). The caller reports the three differently:
    a decline is a first-class outcome an operator must see, not the same silence as
    having had nothing to do. Every non-``True`` case appends its reason to ``errors``.
    """
    try:
        if tree.is_symlink():
            errors.append(f"skipping unsafe symlink worktree tree {tree}")
            return [], False
    except OSError as exc:
        errors.append(f"failed to inspect worktree tree {tree}: {exc}")
        return [], False

    reaped, proven_free = _reap_processes_in_tree(tree, errors, dry_run=dry_run)
    if not proven_free:
        errors.append(
            f"declined worktree tree {tree}: not provably safe to remove — either its "
            f"live processes could not be enumerated, or one of them could not be "
            f"stopped. Preceding warnings say which. Remove the tree yourself if you "
            f"know it is idle."
        )
        return reaped, None

    if dry_run:
        return reaped, True

    # `ignore_errors=True` discarded which path failed and why, leaving `exists()` as
    # the only signal and forcing the report to GUESS ("permission denied?"). Measured,
    # that guess is wrong for a case the stdlib gets right on its own: rmtree refuses a
    # symlinked top entry with "Cannot call rmtree on a symbolic link", and
    # ignore_errors swallowed it.
    #
    # `onerror=` rather than letting rmtree RAISE, for two measured reasons. It keeps
    # deleting around the failure — on a tree with one unwritable subdirectory it
    # reclaimed 3,000,000 of 3,000,001 bytes where raising reclaimed NONE, and a tree
    # that stays at full size recurs on every future GC, which is the accumulation this
    # work exists to end. And it reports FULL paths: `_rmtree_safe_fd` unlinks
    # fd-relatively, so a raised exception carries only the basename.
    #
    # `onerror=` is deprecated in favour of `onexc` from 3.12, but the deprecation is
    # DOCUMENTATION-ONLY — measured on 3.11.13 and 3.14.0 under
    # `warnings.simplefilter("error")`, neither emits anything, so the loop's
    # `pytest -W error` leg is unaffected and no version branch is needed. `onexc` does
    # not exist on this project's 3.11 floor, so it cannot be used unconditionally.
    failures: list[str] = []
    shutil.rmtree(tree, onerror=lambda _fn, path, exc: failures.append(f"{path}: {exc[1]}"))
    if failures or tree.exists():
        # The FIRST failure is the root cause; every later one is an rmdir it blocked,
        # so reporting the head is both bounded and the most informative line available.
        detail = failures[0] if failures else "still present after rmtree"
        errors.append(f"failed to remove worktree tree {tree}: {detail}")
        return reaped, False
    return reaped, True


def _reap_processes_in_tree(
    tree: Path, errors: list[str], *, dry_run: bool = False
) -> tuple[list[int], bool]:
    """``(pids reaped, the tree is proven free)``.

    **The whole proof completes before anything is signalled.** Acting inside the
    proving loop is a defect this function has now had twice: a recheck failing
    halfway, then a kill denied halfway, each left processes dead in service of a
    removal that was then refused — and, because the refusal discarded the list, the
    report said ``0 process(es) reaped`` over a process GC had destroyed. So the two
    questions a removal needs — *who is in here* and *may I stop them* — are both
    answered for every pid first. ``os.kill(pid, 0)`` asks the second without
    signalling, which is also what lets ``dry_run`` reach the same refusals rather
    than promising a removal the real run declines.

    ``reaped`` is returned even when the tree is not proven free, so a race between
    the probe and the signal cannot make the report untrue about what died.
    """
    pids = _lsof_pids_in_tree(tree, errors)
    if pids is None:
        return [], False

    confirmed: list[int] = []
    for pid in pids:
        still_here = _pid_cwd_is_still_in_tree(pid, tree, errors)
        if still_here is None:
            return [], False
        if still_here:
            confirmed.append(pid)

    live: list[int] = []
    for pid in confirmed:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue  # It exited between the recheck and now. Nothing is in the way.
        except OSError as exc:
            # It is alive, it is in this tree, and we may not signal it (EPERM: a setuid
            # child that kept its cwd across the exec, or another user's process on the
            # world-shared default base). An UNANSWERED question refuses, so an answer of
            # "yes, and I cannot act on it" must refuse too.
            _warn(errors, f"cannot signal live pid {pid} in {tree} ({exc}).")
            return [], False
        live.append(pid)
    if dry_run:
        return live, True

    reaped: list[int] = []
    for pid in live:
        try:
            os.kill(pid, signal.SIGKILL)
            reaped.append(pid)
        except ProcessLookupError:
            continue
        except OSError as exc:  # Permissions changed since the probe: rare, still fatal.
            _warn(errors, f"could not reap live pid {pid} in {tree} ({exc}).")
            return reaped, False
    return reaped, True


def reap_processes_in_tree(tree: Path) -> tuple[list[int], bool]:
    """``(pids reaped, the tree is proven free)`` — the seam GC and resume both use.

    Both callers apply the SAME cwd-scoped live-process safety before an ``rmtree``, so
    a directory is never removed out from under a running (orphaned) subprocess. This
    returns the proof rather than discarding it: coercing it away here was item 1's old
    swallow wearing a safer type, and left resume cleanup deleting on an unanswered
    question while GC declined.
    """
    errors: list[str] = []
    return _reap_processes_in_tree(tree, errors)


def _pid_cwd_is_still_in_tree(pid: int, tree: Path, errors: list[str]) -> bool | None:
    """Whether ``pid`` still has a cwd inside ``tree``, or ``None`` if lsof did not say."""
    try:
        result = run_subprocess(
            ["lsof", "-w", "-t", "-a", "-d", "cwd", "-p", str(pid), "+D", str(tree)],
            timeout=_LSOF_TIMEOUT_SECONDS,
        )
    except SubprocessError as exc:
        _warn(errors, f"lsof recheck could not answer for pid {pid} in {tree} ({exc}).")
        return None

    if pid in _parse_lsof_pids(result.stdout):
        return True
    if result.returncode != 0 and result.stderr.strip():
        _warn(
            errors,
            f"lsof recheck errored for pid {pid} in {tree} "
            f"(rc={result.returncode}: {result.stderr.strip()}).",
        )
        return None
    return False


def _lsof_pids_in_tree(tree: Path, errors: list[str]) -> list[int] | None:
    """PIDs with a cwd inside ``tree``, or ``None`` when ``lsof`` could not answer.

    The distinction is the whole point. ``lsof`` exits non-zero with an EMPTY stderr
    when nothing matches — that is an answer, and it means the tree is free. A launch
    failure, a timeout, or an error exit with diagnostics is the ABSENCE of an answer,
    and was previously rendered as "nobody is there": the tree was deleted out from
    under whatever was still using it, and the warning said so out loud
    ("still removing the directory").

    ``-w`` suppresses lsof's WARNING channel, which shares stderr with the fatal one.
    **Measured inert on macOS** (lsof 4.91, Darwin 25.5.0): across an empty tree, a live
    in-tree cwd, an unreadable subdirectory, an unreadable root, a fifo, a dangling and
    an escaping symlink, and a root-owned pid, stderr is empty with or without it — the
    only producer is a ``+D`` argument that is not an existing directory. It is kept for
    Linux, where ``+D``'s documented per-entry "can't stat()" warnings accompany a
    complete answer and would otherwise trip the non-empty-stderr refusal below. That
    Linux behaviour is DOCUMENTED, not measured here.

    **The ceiling, stated rather than implied: an empty answer is not proof of absence.**
    Non-root lsof silently omits processes it may not inspect, and a live cwd inside an
    unreadable subdirectory yields rc=1 with empty stdout AND empty stderr — read here as
    "answered: nobody is there". What this module actually guarantees is narrower and is
    the whole of item 1: a question that was not ANSWERED never reads as "nobody".
    """
    try:
        result = run_subprocess(
            ["lsof", "-w", "-t", "-a", "-d", "cwd", "+D", str(tree)],
            timeout=_LSOF_TIMEOUT_SECONDS,
        )
    except SubprocessError as exc:
        _warn(errors, f"lsof could not answer for {tree} ({exc}).")
        return None

    if result.returncode != 0 and result.stderr.strip():
        _warn(
            errors,
            f"lsof errored for {tree} (rc={result.returncode}: {result.stderr.strip()}).",
        )
        return None

    return _parse_lsof_pids(result.stdout)


def _warn(errors: list[str], message: str) -> None:
    """Record an operator-visible warning on both channels GC reports through."""
    text = f"WARNING: {message}"
    print(text, file=sys.stderr)
    errors.append(text)


def _parse_lsof_pids(stdout: str) -> list[int]:
    """Parse unique PIDs from terse or tabular ``lsof`` output."""
    pids: list[int] = []
    seen: set[int] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        token = line.split()[0]
        if token.startswith("p") and token[1:].isdigit():
            token = token[1:]
        if token.isdigit():
            pid = int(token)
        else:
            parts = line.split()
            if len(parts) < 2 or not parts[1].isdigit():
                continue
            pid = int(parts[1])
        if pid not in seen:
            seen.add(pid)
            pids.append(pid)
    return pids


def _git_worktree_prune(repo_root: Path, errors: list[str]) -> None:
    try:
        run_subprocess(
            ["git", "worktree", "prune"],
            cwd=repo_root,
            timeout=_GIT_PRUNE_TIMEOUT_SECONDS,
        )
    except SubprocessError as exc:
        errors.append(f"git worktree prune failed in {repo_root}: {exc}")
