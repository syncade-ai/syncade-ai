"""Resume-target resolution.

``find_resumable_runs`` lists eligible run-ids; ``resolve_resume_target`` turns
a CLI ``--resume`` target ("latest" or a concrete id) into a validated run-id.
Pure read-from-disk; raises :class:`ResumeError` on a non-resolvable target.
"""

from __future__ import annotations

import json
from pathlib import Path

from syncade.persistence import RUN_INIT_FILENAME
from syncade.persistence.decision_needed import DECISION_NEEDED_FILENAME

from .resume_types import _RESUMABLE_EXIT_CODES, LOOP_MANIFEST_FILENAME, ResumeError

# Unique heading written only by persist_deactivated_blockers_decision_needed,
# never by the producer-escalation persist_decision_needed. When loop-manifest.json
# is missing (partial-finalization window: decision-needed.md written before manifest),
# this marker lets us identify the non-resumable shape without the manifest.
_DEACTIVATED_BLOCKERS_MARKER = "## What each reviewer actually said"


def _decision_needed_is_deactivated_shape(run_dir: Path) -> bool:
    """Return True when ``decision-needed.md`` exists and carries the
    blockers-all-deactivated marker (meaning the run is NOT resumable).

    Used when ``loop-manifest.json`` is missing — the normal eligibility
    predicate — to close the partial-finalization window where
    ``decision-needed.md`` is written before ``loop-manifest.json`` is.
    """
    dn_path = run_dir / DECISION_NEEDED_FILENAME
    try:
        text = dn_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return _DEACTIVATED_BLOCKERS_MARKER in text


def find_resumable_runs(runs_root: Path) -> list[str]:
    """Return run-ids under ``runs_root`` that are eligible to resume.

    Newest first (sort by run-id string descending — run-id is a
    UTC timestamp so lexical order matches chronological order).

    Eligibility (per the design's "resume mental model" table):

    - ``run-init.json`` present AND ``loop-manifest.json`` missing
      → ELIGIBLE (interrupted: operator Ctrl-C / crash / SIGTERM).
    - ``run-init.json`` present AND ``loop-manifest.json`` malformed/unreadable
      → ELIGIBLE enough to surface during resume-target resolution; the later
      planning path reports the specific corruption instead of silently hiding
      the run.
    - ``run-init.json`` present AND ``loop-manifest.json::final_exit_code``
      in ``{10, 25, 40, 60, 70}`` → ELIGIBLE (decision-needed checkpoint, a
      budget stop (25), or environment failure mid-loop).
    - Any other state → NOT eligible.

    Args:
        runs_root: The ``<repo>/.syncade/runs/`` directory.

    Returns:
        Sorted list of eligible run-ids, newest first. Empty list
        when ``runs_root`` doesn't exist or contains no eligible
        runs.

    Does NOT raise on a malformed ``loop-manifest.json`` — that run is returned
    as eligible enough to protect it from GC and let the resume planner surface
    the specific corruption. The intent is that this function never surfaces an
    error path the CLI has to handle directly; it just returns what's available.
    The specific-id resume path (:func:`resolve_resume_target`) is responsible
    for surfacing "run-id not eligible" with the SPECIFIC reason.
    """
    if not runs_root.is_dir():
        return []

    eligible: list[str] = []
    for entry in runs_root.iterdir():
        if not entry.is_dir():
            continue
        if not (entry / RUN_INIT_FILENAME).is_file():
            # Not a syncade run, or missing the required resume artifact.
            continue
        loop_manifest_path = entry / LOOP_MANIFEST_FILENAME
        if not loop_manifest_path.is_file():
            # Missing manifest: usually an interrupted run — eligible, UNLESS
            # decision-needed.md already carries the blockers-all-deactivated
            # shape (written before the manifest in that path). That shape is
            # non-resumable even without the manifest to confirm it.
            if _decision_needed_is_deactivated_shape(entry):
                continue
            eligible.append(entry.name)
            continue
        try:
            data = json.loads(loop_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Malformed/unreadable loop-manifest — protect and surface rather
            # than silently skipping. A later specific-plan path names the
            # concrete failure.
            eligible.append(entry.name)
            continue
        exit_code = data.get("final_exit_code")
        if not (isinstance(exit_code, int) and exit_code in _RESUMABLE_EXIT_CODES):
            continue
        # Exit 10 with blockers_all_deactivated is not resumable: no producer
        # ran, no blocker is active, and resume would re-review unchanged code.
        # plan_resume also refuses this shape, but excluding it here prevents
        # it from shadowing an older decision_needed run in --resume latest.
        if exit_code == 10 and data.get("termination_reason") == "blockers_all_deactivated":
            continue
        eligible.append(entry.name)

    eligible.sort(reverse=True)
    return eligible


def _read_operator_branch(run_dir: Path) -> str | None:
    """Best-effort read of ``run-init.json::operator_branch``."""
    run_init = run_dir / RUN_INIT_FILENAME
    try:
        data = json.loads(run_init.read_text(encoding="utf-8"))
        return data.get("operator_branch")
    except (OSError, json.JSONDecodeError):
        return None


def resolve_resume_target(
    runs_root: Path,
    target: str,
    current_branch: str | None = None,
) -> str:
    """Resolve a CLI ``--resume`` target to a concrete run-id.

    Two target shapes:

    - ``"latest"`` → return the newest eligible run on
      ``current_branch`` (when supplied) or the absolute newest
      eligible run (when ``current_branch`` is ``None``, e.g.
      detached HEAD).
    - Any other string → treat as a concrete run-id and validate
      its eligibility. The specific run-id's branch is NOT checked
      here — that's the tree-drift check's job in T3.

    Args:
        runs_root: The ``<repo>/.syncade/runs/`` directory.
        target: ``"latest"`` or a concrete run-id.
        current_branch: The branch the operator is currently on.
            When supplied, ``"latest"`` resolution filters by
            ``operator_branch == current_branch``. When ``None``
            (detached HEAD or no branch info), the absolute newest
            eligible run is returned.

    Returns:
        The concrete run-id to resume.

    Raises:
        ResumeError: On any non-resolvable state. The message is
            operator-facing and names the specific reason (no
            eligible runs, run-id not found, completed normally,
            etc.).
    """
    if target == "latest":
        eligible = find_resumable_runs(runs_root)
        if current_branch is not None:
            eligible = [
                r for r in eligible if _read_operator_branch(runs_root / r) == current_branch
            ]
        if not eligible:
            if current_branch is not None:
                raise ResumeError(
                    f"no eligible runs to resume on branch {current_branch!r} under {runs_root}"
                )
            raise ResumeError(f"no eligible runs to resume under {runs_root}")
        return eligible[0]

    # Specific run-id — validate eligibility with a helpful message.
    run_dir = runs_root / target
    if not run_dir.is_dir():
        raise ResumeError(f"run-id {target!r} not found under {runs_root} (directory missing)")
    if not (run_dir / RUN_INIT_FILENAME).is_file():
        raise ResumeError(
            f"run-id {target!r} is not a syncade run "
            f"(no {RUN_INIT_FILENAME}). The directory is corrupted "
            f"or missing the required resume artifact."
        )
    loop_manifest_path = run_dir / LOOP_MANIFEST_FILENAME
    if loop_manifest_path.is_file():
        try:
            data = json.loads(loop_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ResumeError(
                f"run-id {target!r} has a malformed {LOOP_MANIFEST_FILENAME}: "
                f"{e}. Investigate manually before resuming."
            ) from e
        exit_code = data.get("final_exit_code")
        # Mirror find_resumable_runs's concrete-exit-code predicate: require an
        # int in the resumable set.
        # Without the isinstance guard a JSON float like 40.0 would slip through
        # here (40.0 in {10,40,60,70} is True via numeric ==) while
        # find_resumable_runs would treat it as NOT eligible
        # (isinstance(40.0, int) is False). (bool is an int subclass but no bool
        # is in the set.)
        if not (isinstance(exit_code, int) and exit_code in _RESUMABLE_EXIT_CODES):
            raise ResumeError(
                f"run-id {target!r} completed normally "
                f"(final_exit_code={exit_code}); resume only applies "
                f"to runs paused for a decision (exit 10), stopped at a budget "
                f"ceiling (exit 25), aborted by "
                f"environment failure (exit 40 / 60 / 70), or interrupted "
                f"before the loop terminator wrote {LOOP_MANIFEST_FILENAME}. "
                f"Start a fresh run."
            )
    # loop-manifest missing → interrupted run → eligible. Fall through.
    return target
