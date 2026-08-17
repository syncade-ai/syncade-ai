"""Run-level commit-safety gates: everything a RUN passes before the loop may spend.

The run-level twin of :mod:`syncade.orchestrator.round_predispatch`, split out of ``loop`` in
PR-h-field-06 after three consecutive dogfoods flagged that module's size while budget state
was being threaded into it. It was 481 of a 500 code-LOC cap — passing, with 19 lines of
headroom, which is the position ``producer.py`` was in when the next change broke the gate.

**The ORDER is load-bearing**, exactly as it is for the round-level gates: the diff is
classified FIRST, because a run that cannot dispatch must not be refused for being on the
default branch or for a dirty tree — there is nothing for it to commit. The block is moved
intact rather than reassembled for that reason.

Refusals RAISE (``WorktreeError``); nothing returns early, which is what makes the extraction
faithful. The four values the loop needs come back on :class:`RunPreflight`.
"""

from __future__ import annotations

import sys

from syncade.worktree import WorktreeError

from .branch_guard import guard_default_branch
from .loop_dispatch_check import _diff_will_dispatch


def run_preflight(
    *,
    config,
    repo_root,
    pr_doc_path,
    snapshot,
    state,
    branch,
    resume_plan,
    logger,
    force_dirty: bool,
    allow_default_branch: bool,
) -> None:
    """Run every run-level gate, in order. Returns nothing; refusals RAISE.

    Measured during the extraction: every value this block computes
    (``_will_dispatch``, ``_dirty_state``, ``effective_max_rounds``, ``will_commit``,
    ``short_sha``) is used only by the gates themselves — the loop needs none of them
    afterwards. That is what makes this a seam rather than a cut: nothing has to be threaded
    back, so there is no carrier object and no chance of the two halves disagreeing.
    """
    short_sha = snapshot.commit_sha[:12]
    # --- Pre-classify diff before commit-safety guards (PR-h-02d) ---
    # A known-empty or malformed diff terminates before any subprocess — no producer
    # runs, so commit-only guards are irrelevant and must not refuse valid no-change runs.
    _will_dispatch = _diff_will_dispatch(
        snapshot, config, repo_root=repo_root, pr_doc_path=pr_doc_path
    )

    # --- Pre-flight dirty-tree refusal in loop mode -----------------
    # max_rounds > 1 → the loop will run a producer that commits to
    # the operator's branch. A tracked-modified WIP would race
    # against the producer's writes (the operator's working tree
    # would interleave with new commits in confusing ways).
    # Untracked-only is fine — those files don't enter any
    # worktree, so they're invisible to both reviewers and the
    # producer. The --force-dirty escape hatch is for operators
    # who understand the consequences.
    #
    # max_rounds == 1 is warning-only because it runs reviewers + synth and
    # exits; nothing writes to the operator branch.
    #
    # Resumed loop-mode runs are refused on the same grounds: a resume
    # still runs the producer and commits to the branch, so a dirty WIP
    # races just as it would on a fresh run. --force-dirty is the only
    # escape (resume does not exempt itself).
    # The refusal must use the EFFECTIVE cap: a resume rehydrates
    # max_rounds to max(config, resume_plan.max_rounds) later (see
    # loop_resume._rehydrate_resume_state), so reading the un-bumped config
    # here would let `--resume --max-rounds 1` — or a config drifted to
    # max_rounds=1 — on a multi-round run slip the gate and then race the
    # producer's commit against the dirty WIP (H3).
    effective_max_rounds = config.loop.max_rounds
    if resume_plan is not None:
        effective_max_rounds = max(effective_max_rounds, resume_plan.max_rounds)
    _dirty_state = state in ("tracked", "both")
    if _will_dispatch and effective_max_rounds > 1 and _dirty_state and not force_dirty:
        raise WorktreeError(
            f"uncommitted tracked changes (dirty_state={state!r}); "
            f"loop mode (max_rounds={effective_max_rounds}) "
            f"commits to your branch and would race against this "
            f"WIP. Commit, stash, or pass --force-dirty to override. "
            f"To run single-pass with warning-only tracked changes, set max_rounds=1 in "
            f"[loop] or pass --max-rounds 1."
        )

    # --- Default-branch commit guard (PR-v2-26) ---------------------
    # A committing run (loop mode) fast-forwards the CURRENT branch, so refuse the
    # default branch unless the operator opted in, and announce the target branch
    # BEFORE any dispatch. Placed at the run-entry choke so a direct run_review call
    # and a --resume are covered too, not only the CLI wrapper.
    # will_commit is False when no dispatch will happen: no producer runs on a no-change
    # or malformed-diff run, so the default-branch guard and commit announcement are moot.
    will_commit = _will_dispatch and effective_max_rounds > 1
    guard_default_branch(
        repo_root, snapshot.branch, allow=allow_default_branch, will_commit=will_commit
    )
    if will_commit:
        # Printed directly to stderr, NOT via logger.event, so it survives --quiet — the
        # same reason the auth block bypasses quiet. Which branch receives commits is a
        # safety disclosure, and it matters MOST under `--quiet --allow-default-branch`.
        print(
            f"[syncade] producer commits will land on: {snapshot.branch or '(detached HEAD)'}",
            file=sys.stderr,
        )

    # Dirty-tree warnings fire on every round-0 snapshot regardless of
    # max_rounds; the loop-mode refusal above is additive.
    if state in ("tracked", "both"):
        logger.warning(
            f"working tree has uncommitted modifications to tracked "
            f"files — reviewers will only see HEAD ({short_sha}); "
            f"your local changes are invisible to them. Commit "
            f"before running syncade if you want them reviewed."
        )
    if state in ("untracked", "both"):
        count = snapshot.untracked_count
        plural = "files" if count != 1 else "file"
        logger.warning(
            f"working tree has untracked files (not reviewed): "
            f"{count} {plural}. These are invisible to reviewers, "
            f"which is usually intentional. Run 'git status' to see them."
        )

    # --- Run-directory layout ---------------------------------------
