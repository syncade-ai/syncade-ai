"""Reviewer subprocess persistence.

Writes ``<round_dir>/<reviewer_name>.{stdout,stderr,parsed.json,error.txt}``
and the matching round-manifest entry. Called once per reviewer in the
fan-out (multiple per round).
"""

from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path

from syncade.dispatcher import ReviewerRunResult
from syncade.process import SubprocessResult
from syncade.usage import usage_fields

from ._atomic import atomic_write_text
from ._validation import _validate_reviewer_filename_basename


def persist_reviewer_result(
    round_dir: Path,
    run_result: ReviewerRunResult,
    raw_subprocess_result: SubprocessResult | None,
) -> None:
    """Write one reviewer's outputs to ``<round_dir>/<name>.*``.

    Files written:

    - ``<name>.stdout`` and ``<name>.stderr`` — always created.
      Contain the captured subprocess streams when available;
      empty when ``raw_subprocess_result`` is ``None`` (the failure
      happened before the subprocess actually ran — e.g. unknown
      provider, auth fail-fast, or build_invocation raising).
    - ``<name>.parsed.json`` — written only when
      ``run_result.output is not None``. Uses
      :meth:`pydantic.BaseModel.model_dump_json(indent=2)` so the
      file diffs cleanly across runs.
    - ``<name>.error.txt`` — written only when
      ``run_result.error is not None``. Contains the exception's
      class name, message, and full traceback (when available).

    Args:
        round_dir: The round directory to write into. Must already
            exist (the orchestrator creates it during run setup).
        run_result: One :class:`ReviewerRunResult` from a
            :class:`DispatchResult`. The ``reviewer_name`` field is
            used as the filename basename and validated against
            path traversal.
        raw_subprocess_result: The :class:`SubprocessResult` from
            the reviewer's subprocess, if one ran. ``None`` when
            the failure happened before any subprocess launched.

    Raises:
        ValueError: If ``run_result.reviewer_name`` is not a safe
            basename.
        FileNotFoundError: If ``round_dir`` does not exist (caller
            bug — orchestrator is responsible for creating it).
    """
    _validate_reviewer_filename_basename(run_result.reviewer_name)
    if not round_dir.is_dir():
        raise FileNotFoundError(f"round_dir does not exist: {round_dir}")

    base = round_dir / run_result.reviewer_name

    stdout_text = raw_subprocess_result.stdout if raw_subprocess_result is not None else ""
    stderr_text = raw_subprocess_result.stderr if raw_subprocess_result is not None else ""
    atomic_write_text(base.with_suffix(".stdout"), stdout_text)
    atomic_write_text(base.with_suffix(".stderr"), stderr_text)

    if run_result.output is not None:
        atomic_write_text(
            base.with_suffix(".parsed.json"),
            run_result.output.model_dump_json(indent=2),
        )

    if run_result.error is not None:
        # Compose: class name, message, traceback. The class name is
        # essential for downstream tooling that wants to bucket
        # failures by type without parsing the message.
        exc = run_result.error
        lines = [
            f"{type(exc).__name__}: {exc}",
            "",
        ]
        # __traceback__ may be None when the exception was constructed
        # (not raised) — e.g. ReviewerInvocationError that the adapter
        # built defensively. Surface that distinction in the file.
        tb = exc.__traceback__
        if tb is not None:
            lines.extend(traceback.format_exception(type(exc), exc, tb))
        else:
            lines.append("(no traceback available — exception was constructed, not raised)")
        atomic_write_text(base.with_suffix(".error.txt"), "\n".join(lines))


def _reviewer_manifest_entry(run_result: ReviewerRunResult) -> dict[str, object]:
    """Build the per-reviewer entry for the manifest's ``reviewers``
    array. Pulls out the fields tooling cares about while keeping the
    full raw output behind the .stdout / .parsed.json files."""
    model = run_result.model or (run_result.usage.model if run_result.usage is not None else "")
    if run_result.output is not None:
        return {
            "name": run_result.reviewer_name,
            "provider": run_result.provider,
            "model": model,
            "verdict": run_result.output.verdict,
            "finding_count": len(run_result.output.findings),
            "duration_seconds": run_result.duration_seconds,
            **usage_fields(run_result.usage),
            "outcome": "success",
            "error_type": None,
        }
    error_type = type(run_result.error).__name__ if run_result.error is not None else None
    return {
        "name": run_result.reviewer_name,
        "provider": run_result.provider,
        "model": model,
        "verdict": None,
        "finding_count": None,
        "duration_seconds": run_result.duration_seconds,
        **usage_fields(run_result.usage),
        "outcome": "failure",
        "error_type": error_type,
    }


def persist_dispatch_record(
    round_dir: Path,
    *,
    round_index: int,
    reviewers: list,
    timeout_seconds: float,
) -> None:
    """Record that reviewers were DISPATCHED, before any of them returns (PR-h-field-02).

    Every other artifact in a round is written after the panel completes, so a run that dies
    mid-dispatch leaves the round directory EMPTY. Three field runs did exactly that — killed in
    ``round-2: reviewing`` with nothing on disk — and the post-mortem had only a log line in a
    terminal and a ``status.json`` phase to work from. The provider error, the child pids, even
    the fact that dispatch had begun, were all unrecoverable.

    This is the cheapest possible fix for that: one small file, written before the panel starts,
    saying what was in flight and since when. It never changes a verdict and is never read by the
    loop — it exists purely so the NEXT unexplained death is diagnosable.

    Deliberately records the parent pid: cross-referenced with ``status.json``'s pid it tells a
    reader whether the parent survived its children, which is the difference between "the
    provider rejected us" and "something killed the process".
    """
    payload = {
        "round": round_index,
        "dispatched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "parent_pid": os.getpid(),
        "timeout_seconds": timeout_seconds,
        "reviewers": [
            {"name": r.name, "provider": r.provider, "model": r.model} for r in reviewers
        ],
    }
    atomic_write_text(round_dir / "dispatch.json", json.dumps(payload, indent=2) + "\n")
