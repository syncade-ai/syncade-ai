"""Tree-drift checks + completed-round rehydration.

``check_tree_drift`` (with the ``_current_head_*`` helpers) validates the
operator's HEAD/branch against the expected resume target; ``load_completed_round``
rebuilds an in-memory :class:`RoundResult` from a completed round's artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from syncade.git_object_id import is_full_git_object_id
from syncade.process import SubprocessError, run_subprocess
from syncade.usage import usage_from_fields

from .resume_types import (
    _GIT_TIMEOUT_SECONDS,
    ROUND_MANIFEST_FILENAME,
    ResumeError,
    TreeDriftError,
)

_REHYDRATED_DIFF_PRESENT_SENTINEL = "[syncade resume: persisted diff was present]\n"


def _current_head_sha(repo_root: Path) -> str:
    """Return current HEAD as a full object ID. Wraps git rev-parse."""
    try:
        result = run_subprocess(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except SubprocessError as e:
        raise ResumeError(f"could not resolve HEAD in {repo_root} for tree-drift check: {e}") from e
    if result.returncode != 0:
        raise ResumeError(f"git rev-parse HEAD failed in {repo_root}: {result.stderr.strip()}")
    sha = result.stdout.strip()
    if not is_full_git_object_id(sha):
        raise ResumeError(
            f"git rev-parse HEAD returned unexpected value {sha!r} in "
            f"{repo_root} (expected a full SHA-1/SHA-256 object ID)"
        )
    return sha


def _current_head_branch(repo_root: Path) -> str | None:
    """Return current branch name, or ``None`` for detached HEAD.

    ``git rev-parse --abbrev-ref HEAD`` returns the literal string
    ``"HEAD"`` when detached; this helper normalizes that to
    ``None`` so the caller can compare against
    :attr:`Snapshot.branch` (which uses the same convention).
    """
    try:
        result = run_subprocess(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except SubprocessError as e:
        raise ResumeError(
            f"could not resolve branch in {repo_root} for tree-drift check: {e}"
        ) from e
    if result.returncode != 0:
        raise ResumeError(
            f"git rev-parse --abbrev-ref HEAD failed in {repo_root}: {result.stderr.strip()}"
        )
    branch = result.stdout.strip()
    if branch == "HEAD" or not branch:
        return None
    return branch


def check_tree_drift(
    repo_root: Path,
    expected_sha: str,
    expected_branch: str | None,
) -> None:
    """Validate that ``repo_root``'s current HEAD matches the
    expected resume target.

    Branch check FIRST, then SHA check — the rationale (from the
    the design): "Refusing on SHA alone misses the case where the
    operator switched branches between abort and resume (the new
    branch might happen to share a SHA with the run's recorded
    SHA, which would silently resume against the wrong tree)."

    Args:
        repo_root: The git repository root to check.
        expected_sha: The full object ID the resumed round should
            snapshot from (derived by :func:`plan_resume`).
        expected_branch: The branch the original run was started
            on. ``None`` when the original run was on detached
            HEAD; in that case the operator must still be on a
            detached HEAD (or pass ``--force-drift``).

    Returns:
        ``None`` when no drift is detected.

    Raises:
        TreeDriftError: With ``kind="branch"`` for branch drift or
            ``kind="sha"`` for SHA drift. ``kind="branch"`` is
            checked first so a branch mismatch isn't masked by a
            coincidental SHA match.
        ResumeError: When git itself can't answer (binary missing,
            timeout, non-zero exit code from rev-parse). The CLI
            maps this to exit 60 the same way TreeDriftError does
            without --force-drift, since the operator's environment
            is in a state where we can't validate.
    """
    current_branch = _current_head_branch(repo_root)
    # Branch check first. Treat both sides as a comparable string:
    # detached HEAD is encoded as ``None`` on both the expected and
    # current sides so they compare equal when both are detached.
    if current_branch != expected_branch:
        # str() to coerce None → "None" for the operator message;
        # the kind="branch" marker tells the CLI which case to
        # surface.
        raise TreeDriftError(
            kind="branch",
            expected=expected_branch if expected_branch is not None else "(detached HEAD)",
            actual=current_branch if current_branch is not None else "(detached HEAD)",
        )

    current_sha = _current_head_sha(repo_root)
    if current_sha != expected_sha:
        raise TreeDriftError(
            kind="sha",
            expected=expected_sha,
            actual=current_sha,
        )


def _safe_read(path: Path) -> str:
    """Read a text file, returning empty string if missing."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return ""


def load_completed_round(round_dir: Path):
    """Rebuild an in-memory :class:`RoundResult` from a completed
    round's on-disk artifacts.

    Called by :func:`run_review` (the resume entry path) to
    pre-populate ``round_results`` with each completed round's state
    so the loop's downstream consumers (loop-summary rendering,
    handoff rendering, cross-round context disk reads) see a
    consistent picture.

    The rehydration is LOSSY by design: ``Snapshot.diff_text``,
    ``Snapshot.dirty_state``, raw subprocess returncodes (for
    reviewer / synth) are not persisted as such and cannot be
    recovered. They get reasonable placeholder values (empty diff,
    ``"clean"`` dirty state, ``None`` for raw_subprocess_result on
    reviewer / synth). What MUST round-trip cleanly: per-reviewer
    ``ReviewerOutput`` (from ``.parsed.json``), per-synth
    ``SynthesizerOutput`` (from ``synthesizer.parsed.json``),
    per-test outcome + exit code, per-producer outcome + SHAs +
    narrative.

    PRECONDITION: this round is in :attr:`ResumePlan.completed_rounds`
    — i.e., :func:`_round_manifest_indicates_phase_failure` returned
    False. Every reviewer.outcome is ``"success"``, every synth.outcome
    (when present) is ``"success"``, no test_run / producer
    ``subprocess_error``. The function does NOT handle the failure
    paths because they would have been dropped by ``plan_resume``.

    Args:
        round_dir: ``<run_dir>/round-N/``. Must contain a clean
            ``manifest.json``.

    Returns:
        A :class:`RoundResult` matching the rehydrated state, or
        ``None`` if the manifest is missing or unreadable (caller
        should treat this as an incomplete round and drop the
        directory).

    Raises:
        ResumeError: When the manifest exists but is malformed
            (e.g. missing a required field). Distinct from "missing
            manifest" so the caller can distinguish "round was
            never persisted" (None) from "round was persisted but
            corrupt" (raise).
    """
    # Imports deferred to call time to avoid a circular import:
    # syncade.orchestrator.results imports from syncade.persistence,
    # which doesn't import back, but the dispatch / synthesizer /
    # test_runner / producer modules form a heavier graph the resume
    # module shouldn't touch at module load.
    from syncade.adapters.producer import ProducerOutput
    from syncade.dispatcher import DispatchResult, ReviewerRunResult
    from syncade.findings import ReviewerOutput
    from syncade.producer import ProducerResult
    from syncade.snapshot import Snapshot
    from syncade.synthesis import SynthesizerOutput
    from syncade.synthesizer import SynthesizerResult
    from syncade.test_runner import TestRunResult

    from .results import RoundArtifacts, RoundResult

    manifest_path = round_dir / ROUND_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ResumeError(f"completed-round manifest at {manifest_path} is malformed: {e}") from e

    try:
        round_idx = int(manifest["round"])
        snap_data = manifest["snapshot"]
        reviewers_data = manifest["reviewers"]
        round_exit_code = int(manifest["round_exit_code"])
    except (KeyError, TypeError, ValueError) as e:
        raise ResumeError(
            f"{manifest_path} is missing a required field for rehydration: {e}"
        ) from e

    # --- Snapshot ---------------------------------------------------
    # repo_root inferred from round_dir's grandparent-grandparent. The
    # convention is .syncade/runs/<run-id>/round-N → 4 .parents up is
    # the repo root. Best-effort; downstream consumers of rehydrated
    # rounds should read snapshot.commit_sha + branch + base_ref but
    # NOT operate on snapshot.repo_root (which is the running
    # orchestrator's responsibility).
    inferred_repo_root = round_dir.parent.parent.parent.parent
    try:
        diff_present = snap_data.get("diff_present", False)
        if not isinstance(diff_present, bool):
            raise ResumeError(
                f"{manifest_path} has malformed snapshot block: "
                f"snapshot.diff_present must be bool, got {diff_present!r}"
            )
        snapshot = Snapshot(
            repo_root=inferred_repo_root,
            commit_sha=str(snap_data["commit_sha"]),
            branch=snap_data.get("branch"),
            base_ref=snap_data.get("base_ref"),
            base_oid=snap_data.get("base_oid"),
            diff_text=_REHYDRATED_DIFF_PRESENT_SENTINEL if diff_present else "",
            dirty_state="clean",  # Not persisted; assume clean (completed round).
        )
    except ResumeError:
        raise
    except (KeyError, TypeError, ValueError) as e:
        raise ResumeError(f"{manifest_path} has malformed snapshot block: {e}") from e

    # --- Reviewer dispatch ------------------------------------------
    reviewer_results: list[ReviewerRunResult] = []
    try:
        for r in reviewers_data:
            name = r["name"]
            if not isinstance(name, str) or not name:
                raise ResumeError(
                    f"{manifest_path} has malformed reviewers block: "
                    f"reviewer name must be a non-empty string, got {name!r}"
                )
            provider = str(r["provider"])
            model = r.get("model") if isinstance(r.get("model"), str) else ""
            duration = float(r.get("duration_seconds", 0.0))
            parsed_path = round_dir / f"{name}.parsed.json"
            if not parsed_path.is_file():
                raise ResumeError(
                    f"reviewer {name!r} in {round_dir} has no "
                    f"{parsed_path.name}; cannot rehydrate ReviewerOutput"
                )
            try:
                output = ReviewerOutput.model_validate_json(parsed_path.read_text(encoding="utf-8"))
            except (OSError, ValidationError) as e:
                raise ResumeError(
                    f"{parsed_path} could not be parsed as ReviewerOutput: {e}"
                ) from e
            reviewer_results.append(
                ReviewerRunResult(
                    reviewer_name=name,
                    provider=provider,
                    output=output,
                    error=None,
                    duration_seconds=duration,
                    raw_subprocess_result=None,  # not recoverable; not read downstream
                    # Rehydrate persisted usage so a resumed run's loop-manifest +
                    # metrics keep the prior round's spend (finding #2).
                    usage=usage_from_fields(
                        r.get("tokens"),
                        r.get("cost_usd"),
                        r.get("cost_source"),
                        model=model,
                        auth_mode=r.get("auth_mode"),
                    ),
                    model=model,
                )
            )
    except ResumeError:
        raise
    except (KeyError, TypeError, ValueError) as e:
        raise ResumeError(f"{manifest_path} has malformed reviewers block: {e}") from e

    total_duration = sum(r.duration_seconds for r in reviewer_results)
    dispatch_result = DispatchResult(
        results=reviewer_results,
        total_duration_seconds=total_duration,
    )

    # --- Synthesizer ------------------------------------------------
    synth_block = manifest.get("synthesizer")
    synth_result = None
    if synth_block is not None:
        synth_duration = float(synth_block.get("duration_seconds", 0.0))
        synth_provider = (
            synth_block.get("provider") if isinstance(synth_block.get("provider"), str) else None
        )
        synth_model = (
            synth_block.get("model") if isinstance(synth_block.get("model"), str) else None
        )
        parsed_path = round_dir / "synthesizer.parsed.json"
        if not parsed_path.is_file():
            raise ResumeError(
                f"synthesizer success recorded but {parsed_path} missing "
                f"in {round_dir}; cannot rehydrate SynthesizerOutput"
            )
        try:
            synth_output = SynthesizerOutput.model_validate_json(
                parsed_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as e:
            raise ResumeError(f"{parsed_path} could not be parsed as SynthesizerOutput: {e}") from e
        synth_result = SynthesizerResult(
            output=synth_output,
            error=None,
            duration_seconds=synth_duration,
            raw_subprocess_result=None,
            usage=usage_from_fields(
                synth_block.get("tokens"),
                synth_block.get("cost_usd"),
                synth_block.get("cost_source"),
                model=synth_model or "",
                auth_mode=synth_block.get("auth_mode"),
            ),
            provider=synth_provider,
            model=synth_model,
        )

    # --- Test re-run ------------------------------------------------
    test_block = manifest.get("test_run")
    test_result: TestRunResult | None = None
    if test_block is not None:
        try:
            outcome = str(test_block["outcome"])
            # Rehydration contract: outcome is "passed" or "failed". The
            # "subprocess_error" outcome would have flagged the round as
            # a phase failure and plan_resume would not have admitted it
            # to completed_rounds. Defensive guard below.
            if outcome == "subprocess_error":
                raise ResumeError(
                    f"{manifest_path}: test_run.outcome=='subprocess_error' "
                    f"but plan_resume admitted this round to "
                    f"completed_rounds — contract violation"
                )
            if outcome not in ("passed", "failed"):
                raise ResumeError(
                    f"{manifest_path} has malformed test_run block: unexpected outcome {outcome!r}"
                )
            stdout = _safe_read(round_dir / "test-run.stdout")
            stderr = _safe_read(round_dir / "test-run.stderr")
            test_result = TestRunResult(
                exit_code=int(test_block["exit_code"]),
                outcome=outcome,
                duration_seconds=float(test_block.get("duration_seconds", 0.0)),
                stdout=stdout,
                stderr=stderr,
                error=None,
                command=str(test_block.get("command", "")),
            )
        except ResumeError:
            raise
        except (KeyError, TypeError, ValueError) as e:
            raise ResumeError(f"{manifest_path} has malformed test_run block: {e}") from e

    # --- Producer ---------------------------------------------------
    producer_block = manifest.get("producer")
    producer_result: ProducerResult | None = None
    if producer_block is not None:
        try:
            outcome = str(producer_block["outcome"])
            if outcome == "subprocess_error":
                raise ResumeError(
                    f"{manifest_path}: producer.outcome=='subprocess_error' "
                    f"but plan_resume admitted this round to "
                    f"completed_rounds — contract violation"
                )
            starting_sha = str(producer_block["starting_sha"])
            ending_sha = str(producer_block["ending_sha"])
            producer_provider = (
                producer_block.get("provider")
                if isinstance(producer_block.get("provider"), str)
                else None
            )
            producer_model = (
                producer_block.get("model")
                if isinstance(producer_block.get("model"), str)
                else None
            )
            if not starting_sha or not ending_sha:
                raise ResumeError(
                    f"{manifest_path} has malformed producer block: "
                    "missing starting_sha or ending_sha"
                )
            if outcome not in ("committed", "stalled"):
                raise ResumeError(
                    f"{manifest_path} has malformed producer block: unexpected outcome {outcome!r}"
                )
            narrative = _safe_read(round_dir / "producer.stdout")
            producer_result = ProducerResult(
                outcome=outcome,
                starting_sha=starting_sha,
                ending_sha=ending_sha,
                duration_seconds=float(producer_block.get("duration_seconds", 0.0)),
                output=ProducerOutput(narrative_text=narrative),
                error=None,
                raw_subprocess_result=None,
                retries=int(producer_block.get("retried", 0)),
                usage=usage_from_fields(
                    producer_block.get("tokens"),
                    producer_block.get("cost_usd"),
                    producer_block.get("cost_source"),
                    model=producer_model or "",
                    auth_mode=producer_block.get("auth_mode"),
                ),
                provider=producer_provider,
                model=producer_model,
            )
        except ResumeError:
            raise
        except (KeyError, TypeError, ValueError) as e:
            raise ResumeError(f"{manifest_path} has malformed producer block: {e}") from e

    # --- Artifacts (paths only) -------------------------------------
    findings_md = round_dir / "findings.md"
    artifacts = RoundArtifacts(
        round_idx=round_idx,
        round_dir=round_dir,
        manifest_path=manifest_path,
        summary_path=round_dir / "summary.md",
        findings_md_path=findings_md if findings_md.is_file() else None,
        # synthesizer_paths / test_run_paths / producer_paths: omitted
        # for rehydrated rounds. Resumed rounds don't re-run persistence, so
        # these stay None. Nothing downstream of run_review reads them for
        # rehydrated rounds.
    )

    return RoundResult(
        round_idx=round_idx,
        snapshot=snapshot,
        dispatch_result=dispatch_result,
        synth_result=synth_result,
        test_result=test_result,
        test_skip_reason=manifest.get("test_skip_reason"),
        test_worktree_error=None,
        producer_result=producer_result,
        round_exit_code=round_exit_code,
        artifacts=artifacts,
        # check_results: intentionally left at its empty-list default —
        # not rehydrated, by the same lossy contract as raw_subprocess_result
        # above. Resumed rounds don't re-run persistence, and nothing
        # downstream of run_review reads a rehydrated round's check_results
        # (loop-manifest.json / loop-summary.md derive their per-round entries
        # without it). The subprocess_error outcome also can't round-trip
        # faithfully — only error_type is serialized, but TestRunResult
        # requires a live error object — so a partial rehydration would lie.
    )
