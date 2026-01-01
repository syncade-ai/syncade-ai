"""Resume planning.

Builds the :class:`ResumePlan` for a run directory: classifies per-round phase
failures, reads the operator decision for an escalated round, derives the
expected snapshot SHA, and walks the rounds to find the first incomplete one.
Pure read-from-disk; raises :class:`ResumeError` on malformed/degenerate state.
"""

from __future__ import annotations

import json
from pathlib import Path

from syncade.exit_codes import FINDINGS_PRESENT, SUCCESS
from syncade.git_object_id import is_full_git_object_id
from syncade.persistence import (
    OPERATOR_DECISION_FILENAME,
    RUN_INIT_FILENAME,
    read_operator_decision,
)
from syncade.test_runner import is_blocking_check_subprocess_error

from .resume_types import (
    LOOP_MANIFEST_FILENAME,
    ROUND_MANIFEST_FILENAME,
    ResumeError,
    ResumePlan,
)


def _manifest_block_error(block_name: str, error: Exception) -> ResumeError:
    return ResumeError(f"round manifest has malformed {block_name} block: {error}")


def _round_manifest_indicates_phase_failure(manifest: dict) -> bool:
    """Inspect a round's manifest.json contents and decide whether
    the round had a phase failure (reviewer / synth / test-run /
    producer subprocess or environment error).

    NOT classified as a phase failure:

    - ``test_run.outcome == "failed"`` — the test ran and exited
      non-zero. That's a clean signal the loop terminator uses to
      compute the round's exit code; not a phase failure.
    - ``producer.outcome == "stalled"`` — the producer subprocess
      exited cleanly without committing. Clean signal; the loop
      terminator handles it as ``producer_stalled``.

    Classified as a phase failure (and therefore drop-and-retry):

    - Any reviewer ``outcome`` other than ``"success"``.
    - ``synthesizer.outcome != "success"`` when the synth phase ran.
    - ``test_run.outcome == "subprocess_error"`` when the test phase
      ran.
    - ``producer.outcome == "subprocess_error"`` when the producer
      phase ran.
    - ``producer.outcome == "escalated"`` — not an error, but a
      deliberate checkpoint: the round re-runs from snapshot with the
      operator's recorded decision. (Distinct from ``stalled``.)
    - A BLOCKING ``checks[]`` entry with ``outcome ==
      "subprocess_error"`` — mirrors
      ``verdict._compute_exit_code`` (→ exit 40 / ``REVIEWER_FAILURE``)
      and ``verdict._classify_phase_failure`` (→
      ``"check_subprocess_error"``), an exit code that IS resume-eligible.
      A blocking check that merely FAILED (→ exit 30) is a CLEAN NO-SHIP
      signal like ``test_run == "failed"`` — NOT a phase failure.

    The intent is "should this round be dropped and retried on resume?"
    — for the error cases, because the original loop aborted there; for
    escalation, because the operator's decision now lets the round
    proceed. Either way we drop + retry rather than re-use partial state.
    """
    try:
        for reviewer in manifest.get("reviewers") or []:
            if reviewer["outcome"] != "success":
                return True
    except (KeyError, TypeError) as e:
        raise _manifest_block_error("reviewers", e) from e
    synth = manifest.get("synthesizer")
    try:
        if synth is not None and synth["outcome"] != "success":
            return True
    except (KeyError, TypeError) as e:
        raise _manifest_block_error("synthesizer", e) from e
    test_run = manifest.get("test_run")
    try:
        test_outcome = test_run["outcome"] if test_run is not None else None
        if test_run is not None:
            test_run["exit_code"]
            if test_outcome not in ("passed", "failed", "subprocess_error"):
                raise _manifest_block_error(
                    "test_run", ValueError(f"unexpected outcome {test_outcome!r}")
                )
        if test_outcome == "subprocess_error":
            return True
    except (KeyError, TypeError) as e:
        raise _manifest_block_error("test_run", e) from e
    # A test-worktree provisioning failure is an environment abort: test_run
    # is null (the test never started) but test_skip_reason records the cause.
    if manifest.get("test_skip_reason") == "test_worktree_error":
        return True
    producer = manifest.get("producer")
    try:
        producer_outcome = producer["outcome"] if producer is not None else None
        if producer is not None:
            producer["starting_sha"]
            producer["ending_sha"]
            if producer_outcome not in ("committed", "stalled", "subprocess_error", "escalated"):
                raise _manifest_block_error(
                    "producer", ValueError(f"unexpected outcome {producer_outcome!r}")
                )
    except (KeyError, TypeError) as e:
        raise _manifest_block_error("producer", e) from e
    if producer_outcome == "subprocess_error":
        return True
    # a producer ESCALATION is not an error, but it IS a
    # drop-and-retry trigger — the round re-runs from snapshot with the
    # operator's recorded decision fed to the producer. (Distinct from
    # ``stalled``, which is a clean terminal signal and is NOT retried.)
    if producer_outcome == "escalated":
        return True
    # / §5 drift fix: a BLOCKING mechanical check whose subprocess
    # could not run drives the round to exit 40 (REVIEWER_FAILURE), which
    # is resume-eligible. Mirror verdict._classify_phase_failure's exact
    # predicate so the two surfaces agree — without this the errored round
    # is mis-read as completed cleanly and never re-run. A blocking check
    # that merely FAILED (→ exit 30) is a clean NO-SHIP signal, NOT a
    # phase failure (intentionally excluded, like test_run "failed").
    try:
        for check in manifest.get("checks") or []:
            if is_blocking_check_subprocess_error(check["severity"], check["outcome"]):
                return True
    except (KeyError, TypeError) as e:
        raise _manifest_block_error("checks", e) from e
    return False


def _reject_malformed_completed_round(manifest: dict, manifest_path: Path) -> None:
    """Validate the manifest-level fields a completed round's rehydration
    relies on — ``snapshot.diff_present`` and each reviewer ``name``.

    The plan walk calls this for a round that claims SHIP
    (``round_exit_code == 0``) BEFORE the not-resumable refusal: a SHIPped
    round is never rehydrated, so without this its structural corruption
    would hide behind the generic "already SHIPped" message. NO-SHIP
    completed rounds get the same checks (plus parsed.json validation) in
    :func:`load_completed_round` when they are actually rehydrated, so this
    helper stays manifest-only and does NOT require the parsed.json sidecar
    files a refused SHIP round never persists. The field messages mirror
    ``load_completed_round`` so the surfaced error reads identically
    regardless of which path caught the corruption. Raises
    :class:`ResumeError` (→ exit 60) on a malformed field.
    """
    snapshot = manifest.get("snapshot")
    if isinstance(snapshot, dict):
        diff_present = snapshot.get("diff_present", False)
        if not isinstance(diff_present, bool):
            raise ResumeError(
                f"{manifest_path} has malformed snapshot block: "
                f"snapshot.diff_present must be bool, got {diff_present!r}"
            )
    for reviewer in manifest.get("reviewers") or []:
        name = reviewer.get("name") if isinstance(reviewer, dict) else None
        if not isinstance(name, str) or not name:
            raise ResumeError(
                f"{manifest_path} has malformed reviewers block: "
                f"reviewer name must be a non-empty string, got {name!r}"
            )


def read_resume_decision(run_dir: Path, resumed_round: int) -> str | None:
    """Resolve the operator decision to feed the resumed round's producer.

    Returns ``None`` when the round being resumed did NOT escalate (a
    normal environment-failure resume — there's no decision to inject).

    When the round DID escalate (``producer.outcome == "escalated"`` in
    its manifest), the operator must have recorded a decision in
    ``<run_dir>/decision.txt`` before resuming:

    - decision present → returns its stripped text (fed to the producer
      via the ``{operator_decision}`` prompt field).
    - decision absent / blank → raises :class:`ResumeError` so the CLI
      refuses the resume with a helpful message rather than re-running
      the round only to escalate again.

    Read BEFORE the resumed round's directory is dropped (the manifest
    is gone after the drop).
    """
    manifest_path = run_dir / f"round-{resumed_round}" / ROUND_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    producer = manifest.get("producer")
    if not (isinstance(producer, dict) and producer.get("outcome") == "escalated"):
        return None
    decision = read_operator_decision(run_dir)
    if decision is None:
        raise ResumeError(
            f"run {run_dir.name} stopped for an operator decision — the "
            f"round-{resumed_round} producer escalated a finding. Record your "
            f"decision in {run_dir / OPERATOR_DECISION_FILENAME} before "
            f"resuming (see decision-needed.md for the producer's case + "
            f"options)."
        )
    return decision


def _expected_sha_for_resumed_round(
    *,
    resumed_round: int,
    starting_sha: str,
    run_dir: Path,
) -> str:
    """Derive the snapshot SHA the resumed round should pin against.

    Round 0 uses ``run-init.json::starting_sha``. Round N>0 reads
    the prior round's manifest: if the prior producer committed,
    use ``producer.ending_sha``; otherwise (no producer commit),
    the branch never advanced, so use the prior round's
    ``snapshot.commit_sha``.

    Raises :class:`ResumeError` when the round-(N-1) manifest is
    missing or malformed — that's a degenerate state plan_resume
    can't recover from.
    """
    if resumed_round == 0:
        return starting_sha
    prior_round_dir = run_dir / f"round-{resumed_round - 1}"
    prior_manifest_path = prior_round_dir / ROUND_MANIFEST_FILENAME
    if not prior_manifest_path.is_file():
        raise ResumeError(
            f"resumed round {resumed_round} but prior round directory "
            f"{prior_round_dir} has no {ROUND_MANIFEST_FILENAME} — "
            f"cannot derive expected snapshot SHA"
        )
    try:
        prior_manifest = json.loads(prior_manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ResumeError(f"prior round manifest at {prior_manifest_path} is malformed: {e}") from e
    producer = prior_manifest.get("producer")
    if producer is not None and producer.get("outcome") == "committed":
        ending_sha = producer.get("ending_sha")
        if isinstance(ending_sha, str) and ending_sha:
            return ending_sha
    # No producer commit (stalled, errored, or producer phase didn't
    # run) → the branch was never advanced; resumed round snapshots
    # from the prior round's snapshot SHA.
    snapshot = prior_manifest.get("snapshot") or {}
    commit_sha = snapshot.get("commit_sha")
    if not isinstance(commit_sha, str) or not commit_sha:
        raise ResumeError(
            f"prior round manifest at {prior_manifest_path} has no recoverable snapshot.commit_sha"
        )
    return commit_sha


def plan_resume(
    repo_root: Path,
    run_dir: Path,
    max_rounds_override: int | None = None,
) -> ResumePlan:
    """Build a :class:`ResumePlan` for the given run directory.

    Reads ``run-init.json`` to recover the original-run context,
    walks ``round-0/``, ``round-1/``, ... to find the first
    incomplete round, and derives the expected snapshot SHA for
    that round.

    Args:
        repo_root: Repo root (used to make ``pr_doc_path``
            absolute when run-init.json recorded a relative path).
        run_dir: The ``<runs_root>/<run_id>/`` directory.
        max_rounds_override: When supplied (e.g. from the CLI's
            ``--resume --max-rounds N``), the effective walk range
            is ``max(original_max_rounds, max_rounds_override)``.
            This lets ``--resume --max-rounds 2`` continue at
            round 1 even when the original run used
            ``max_rounds=1`` (all 1 rounds completed cleanly but
            the loop-manifest recorded exit 25/40/60/70).

    Returns:
        A :class:`ResumePlan` populated from on-disk artifacts.

    Raises:
        ResumeError: If ``run-init.json`` is missing or malformed,
            or if no incomplete round can be identified AND the
            effective round cap is exhausted (degenerate state).
    """
    if not run_dir.is_dir():
        raise ResumeError(f"run directory does not exist: {run_dir}")
    run_init_path = run_dir / RUN_INIT_FILENAME
    if not run_init_path.is_file():
        raise ResumeError(f"{RUN_INIT_FILENAME} missing in {run_dir} — cannot resume")
    try:
        run_init = json.loads(run_init_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ResumeError(f"{run_init_path} is malformed: {e}") from e

    try:
        max_rounds = int(run_init["max_rounds"])
        starting_sha = str(run_init["starting_sha"])
        syncade_version = str(run_init["syncade_version"])
        pr_doc_str = str(run_init["pr_doc_path"])
    except (KeyError, TypeError, ValueError) as e:
        raise ResumeError(f"{run_init_path} is missing a required field: {e}") from e
    # Validate starting_sha on read — mirrors the live-git HEAD check in
    # resume_load._current_head_sha. A malformed value would otherwise only
    # surface downstream as a TreeDriftError (kind="sha") when the real HEAD
    # fails to match it, masking the real cause (corrupt run-init.json).
    if not is_full_git_object_id(starting_sha):
        raise ResumeError(
            f"{run_init_path} has a malformed starting_sha {starting_sha!r}; "
            f"expected a full SHA-1/SHA-256 object ID"
        )
    operator_branch = run_init.get("operator_branch")
    base_ref = run_init.get("base_ref")
    if operator_branch is not None and not isinstance(operator_branch, str):
        raise ResumeError(
            f"{run_init_path} has malformed operator_branch "
            f"({operator_branch!r}); expected str or null"
        )

    # Resolve pr_doc_path relative to repo_root when the recorded
    # path is relative (the orchestrator persists str(path); if the
    # original run was invoked via a relative path the absolute
    # form would differ across machines but the relative form is
    # repo-portable).
    pr_doc_path = Path(pr_doc_str)
    if not pr_doc_path.is_absolute():
        pr_doc_path = (repo_root / pr_doc_path).resolve()

    # When the CLI passes --max-rounds N, the effective walk range
    # extends to cover the extra rounds the operator wants to add.
    effective_max_rounds = (
        max(max_rounds, max_rounds_override) if max_rounds_override is not None else max_rounds
    )

    # Walk rounds 0..effective_max_rounds-1 to find the first incomplete one.
    completed_rounds: list[int] = []
    resumed_round: int | None = None
    budget_aborted_before_producer_round: int | None = None
    for round_idx in range(effective_max_rounds):
        round_dir = run_dir / f"round-{round_idx}"
        if not round_dir.is_dir():
            resumed_round = round_idx
            break
        manifest_path = round_dir / ROUND_MANIFEST_FILENAME
        if not manifest_path.is_file():
            # Round started but never persisted its manifest —
            # typical interrupted-mid-round case.
            resumed_round = round_idx
            break
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Malformed manifest → drop + retry the round.
            resumed_round = round_idx
            break
        if _round_manifest_indicates_phase_failure(manifest):
            resumed_round = round_idx
            break
        # A SHIPped round (round_exit_code == SUCCESS) terminates the run:
        # the producer's commit was accepted and the loop ended. Such a run
        # is only "resumable" when the loop crashed after this round
        # persisted but before the terminator wrote its exit-0 loop-manifest.
        # Resuming would re-review the un-advanced tree and could flip a
        # legit SHIP to NO-SHIP and re-advance the branch. Refuse — the
        # operator's next move is a fresh run.
        if manifest.get("round_exit_code") == SUCCESS:
            # A SHIPped round is never rehydrated, so its manifest fields are
            # otherwise never validated. Reject a structurally-malformed
            # manifest first (exit 60 naming the specific corruption) — it
            # can't be trusted to have actually SHIPped, and the generic
            # refusal below would mask the real problem.
            _reject_malformed_completed_round(manifest, manifest_path)
            raise ResumeError(
                f"round-{round_idx} in {run_dir} already SHIPped "
                f"(round_exit_code == {SUCCESS}); the run terminated with a "
                f"SHIP and is not resumable. Start a fresh run."
            )
        # A non-final NO-SHIP round with no producer block means the
        # producer phase never ran. Two sub-cases:
        #
        # (a) Budget abort: the loop manifest exists and records
        #     termination_reason == "budget_exceeded". The review bundle
        #     (reviewers + synth + test) is complete; only the producer
        #     was skipped because the pre-producer budget check tripped.
        #     Treat as completed (rehydrate the review bundle) and set
        #     budget_aborted_before_producer_round so the loop dispatches
        #     only the producer under the fresh budget tally.
        #
        # (b) Genuine interrupt: the process was killed between the first
        #     persist_round_manifest call and the producer dispatch. The
        #     loop manifest is absent or doesn't say budget_exceeded.
        #     Drop and retry the whole round.
        if (
            manifest.get("producer") is None
            and manifest.get("round_exit_code") == FINDINGS_PRESENT
            and round_idx < effective_max_rounds - 1
        ):
            loop_manifest_path = run_dir / LOOP_MANIFEST_FILENAME
            _budget_aborted = False
            if loop_manifest_path.is_file():
                try:
                    lm = json.loads(loop_manifest_path.read_text(encoding="utf-8"))
                    _budget_aborted = lm.get("termination_reason") == "budget_exceeded"
                except (json.JSONDecodeError, OSError):
                    pass
            if _budget_aborted:
                # (a) review bundle complete — rehydrate it, resume at producer only
                completed_rounds.append(round_idx)
                resumed_round = round_idx
                budget_aborted_before_producer_round = round_idx
            else:
                # (b) genuine interrupt — drop and retry whole round
                resumed_round = round_idx
            break
        completed_rounds.append(round_idx)

    if resumed_round is None:
        # All effective_max_rounds rounds completed cleanly but
        # eligibility got us here — the loop terminator must have
        # aborted after the final round's persistence (rare: e.g.
        # loop-manifest write failed). Per the brief:
        # resumed_round = N+1 if not at cap, else degenerate state.
        if len(completed_rounds) < effective_max_rounds:
            # Defensive — shouldn't happen because the walk above
            # adds every successful round to completed_rounds and
            # the iteration count is effective_max_rounds, but kept
            # explicit.
            resumed_round = len(completed_rounds)
        else:
            raise ResumeError(
                f"all {effective_max_rounds} rounds in {run_dir} completed "
                f"cleanly per their per-round manifests, but the run "
                f"is eligible to resume (loop-manifest absent or "
                f"final_exit_code in {{25, 40, 60, 70}}). This is a "
                f"degenerate state — the loop terminator aborted after "
                f"the final round's persistence. Inspect "
                f"{run_dir / LOOP_MANIFEST_FILENAME} (if present) "
                f"and start a fresh run rather than resuming."
            )

    expected_sha = _expected_sha_for_resumed_round(
        resumed_round=resumed_round,
        starting_sha=starting_sha,
        run_dir=run_dir,
    )

    return ResumePlan(
        run_id=run_dir.name,
        run_dir=run_dir,
        pr_doc_path=pr_doc_path,
        operator_branch=operator_branch,
        expected_sha=expected_sha,
        expected_branch=operator_branch,
        resumed_round=resumed_round,
        completed_rounds=completed_rounds,
        max_rounds=max_rounds,
        syncade_version=syncade_version,
        config_snapshot_path=run_init_path,
        base_ref=base_ref,
        budget_aborted_before_producer_round=budget_aborted_before_producer_round,
    )
