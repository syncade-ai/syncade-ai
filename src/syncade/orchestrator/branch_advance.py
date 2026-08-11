"""Branch-advance helper + status Literal.

Holds the fast-forward ``git update-ref`` discipline that promotes the
producer's detached-HEAD commit to the operator's named branch, plus the
categorical ``BranchAdvanceStatus`` type the loop terminator switches on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from syncade.logging import Logger
from syncade.process import SubprocessError, SubprocessResult, run_subprocess
from syncade.producer import ProducerResult
from syncade.snapshot import Snapshot

BranchAdvanceStatus = Literal[
    "advanced",
    "skipped_detached_head",
    "non_descendant",
    "update_ref_failed",
]
"""Outcome of :func:`_advance_branch_ref`.

- ``advanced`` — branch ref was successfully fast-forwarded to the
  producer's ending SHA. The loop can continue safely; the next
  round's snapshot will pick up the new HEAD.
- ``skipped_detached_head`` — the operator was on detached HEAD at
  invocation; no named branch to advance. The producer's commits
  are in ``.git`` but unreachable from any branch. The orchestrator
  MUST terminate the loop: the next round's snapshot would read the
  same starting SHA again, so reviewers could SHIP stale code.
- ``non_descendant`` — the producer's ending SHA is NOT a
  descendant of the starting SHA. The orchestrator MUST terminate the loop;
  continuing would dispatch the next round against the original
  starting SHA (since update-ref didn't fire), causing the
  reviewers to see stale code and potentially declare SHIP.
- ``update_ref_failed`` — ``git update-ref`` returned non-zero
  (ref moved out from under us, permissions, etc.). The
  orchestrator MUST terminate. Same stale-SHIP risk as
  ``non_descendant``.
"""

_GIT_TIMEOUT_SECONDS = 30.0


def _run_git(argv: list[str], *, cwd: Path, logger: Logger) -> SubprocessResult | None:
    try:
        return run_subprocess(argv, cwd=cwd, timeout=_GIT_TIMEOUT_SECONDS)
    except SubprocessError as exc:
        logger.safety(
            f"orchestrator: git command {argv[:2]!r} failed before completion: {exc}. "
            "The branch ref was not advanced. The loop will terminate as "
            "producer_stalled to prevent the next round from dispatching reviewers "
            "against stale code at the unadvanced ref."
        )
        return None


def _advance_branch_ref(
    *,
    repo_root: Path,
    snapshot: Snapshot,
    producer_result: ProducerResult,
    logger: Logger,
) -> BranchAdvanceStatus:
    """Fast-forward the operator's branch to the producer's ending SHA.

    Runs ``git update-ref refs/heads/<branch> <ending_sha>
    <starting_sha>`` from the repo root. Refuses to advance when
    the ending SHA isn't a descendant of the starting SHA (the
    producer did something unusual — ``git reset --hard
    <unrelated>``, force-update, orphan branch checkout).

    Returns a :data:`BranchAdvanceStatus` so the caller can fold
    advance failure into the loop terminator. Without that status, the loop
    could continue past advance failure and create a stale-SHIP risk (the next
    round's snapshot would read the original branch SHA and dispatch reviewers
    against stale
    code).

    For ``snapshot.branch is None`` (detached HEAD at invocation),
    the orchestrator can't advance any named branch — emits a
    warning and returns ``skipped_detached_head``. The caller treats
    this as a terminal non-SHIP state so the next reviewers never
    inspect stale input. The producer's commits are still in ``.git``
    (worktrees share ``.git``), so the operator can manually advance
    their branch via ``git log <producer-sha>`` + ``git update-ref``
    if they want to keep the commits.
    """
    if snapshot.branch is None:
        logger.safety(
            f"orchestrator: producer committed {producer_result.ending_sha[:12]} "
            f"on a detached HEAD; the operator was on detached HEAD at "
            f"invocation, so no named branch ref to advance. The "
            f"producer's commits are in .git — inspect with `git log "
            f"{producer_result.ending_sha[:12]}` and manually advance "
            f"the branch you want them on."
        )
        return "skipped_detached_head"

    # Verify the ending SHA is a descendant of the starting SHA. If
    # not, the producer did something unusual (reset, force-update,
    # etc.) and we shouldn't blindly fast-forward — AND we shouldn't
    # continue the loop, because the next round would see stale
    # code at the original starting SHA.
    # `--no-replace-objects` prevents a refs/replace/* ref on the producer's
    # ending SHA from substituting a fake commit whose parent is starting_sha,
    # which would make --is-ancestor return 0 for an actual non-descendant.
    is_ancestor = _run_git(
        [
            "git",
            "--no-replace-objects",
            "merge-base",
            "--is-ancestor",
            producer_result.starting_sha,
            producer_result.ending_sha,
        ],
        cwd=repo_root,
        logger=logger,
    )
    if is_ancestor is None:
        return "update_ref_failed"
    if is_ancestor.returncode != 0:
        logger.safety(
            f"orchestrator: producer's ending SHA "
            f"({producer_result.ending_sha[:12]}) is NOT a descendant "
            f"of the round-start SHA ({producer_result.starting_sha[:12]}); "
            f"refusing to fast-forward {snapshot.branch}. The "
            f"producer may have done something unusual "
            f"(git reset, force-update). The commits exist in .git "
            f"under the producer's SHA — manual intervention required. "
            f"The loop will terminate as producer_stalled (per loop policy "
            f"brief: 'treats the round as a stall') to prevent the "
            f"next round from dispatching reviewers against stale "
            f"code at the unadvanced ref."
        )
        return "non_descendant"

    update = _run_git(
        [
            "git",
            "update-ref",
            f"refs/heads/{snapshot.branch}",
            producer_result.ending_sha,
            producer_result.starting_sha,  # OLDVALUE — fail if ref moved out from under us
        ],
        cwd=repo_root,
        logger=logger,
    )
    if update is None:
        return "update_ref_failed"
    if update.returncode != 0:
        logger.safety(
            f"orchestrator: git update-ref refs/heads/{snapshot.branch} "
            f"failed: {update.stderr.strip()[:200]!r}. The producer's "
            f"commits are in .git but the branch ref wasn't advanced. "
            f"The loop will terminate as producer_stalled to prevent "
            f"the next round from dispatching reviewers against stale "
            f"code at the unadvanced ref."
        )
        return "update_ref_failed"

    return "advanced"
