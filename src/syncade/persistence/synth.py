"""Synthesizer subprocess persistence.

Writes ``<round_dir>/synthesizer.{stdout,stderr,parsed.json[,error.txt]}``
and the matching round-manifest entry. There is exactly one
synthesizer per round, so the basename is hardcoded (``SYNTHESIZER_NAME``
from :mod:`syncade.synthesizer`).

The module is named ``synth.py`` (not ``synthesizer.py``) to
disambiguate from :mod:`syncade.synthesizer`, which contains the actual
synthesizer driver.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

from syncade.synthesizer import (
    SYNTHESIZER_MODEL,
    SYNTHESIZER_NAME,
    SYNTHESIZER_PROVIDER,
    SynthesizerResult,
)
from syncade.usage import usage_fields

from ._atomic import atomic_write_text
from ._validation import _validate_reviewer_filename_basename


@dataclass(frozen=True)
class SynthesizerArtifactPaths:
    """Where the synthesizer subprocess's artifacts land on disk.

    All four paths are absolute and rooted at ``<round_dir>``.
    ``parsed`` is ``None`` when the synthesizer failed (no
    :class:`SynthesizerOutput` to serialize); ``error`` is ``None``
    when it succeeded (no exception to record).

    Returned by :func:`persist_synthesizer_result` and attached to
    :class:`~syncade.orchestrator.RunArtifacts` so the CLI / future
    loop can address the files without re-deriving the layout
    convention.
    """

    stdout: Path
    stderr: Path
    parsed: Path | None
    error: Path | None


def persist_synthesizer_result(
    round_dir: Path, synth_result: SynthesizerResult
) -> SynthesizerArtifactPaths:
    """Write the synthesizer subprocess's outputs to
    ``<round_dir>/synthesizer.*``.

    Mirrors :func:`persist_reviewer_result`'s file-layout convention
    so a tool inspecting the round directory sees the synthesizer
    artifacts in the same shape as the per-reviewer artifacts. The
    only difference is the fixed ``"synthesizer"`` basename — there
    is exactly one synthesizer per round, so no per-reviewer-name
    collision risk.

    Files written:

    - ``synthesizer.stdout`` and ``synthesizer.stderr`` — always
      created. Carry the captured codex subprocess streams when
      :attr:`SynthesizerResult.raw_subprocess_result` is present;
      empty when the failure happened before any subprocess output
      (binary missing).
    - ``synthesizer.parsed.json`` — written only when
      ``synth_result.output is not None``. Pretty-printed via
      :meth:`pydantic.BaseModel.model_dump_json(indent=2)` so the
      file diffs cleanly across runs.
    - ``synthesizer.error.txt`` — written only when
      ``synth_result.error is not None``. Class name, message, and
      traceback (when available).

    Args:
        round_dir: The round directory to write into. Must already
            exist.
        synth_result: The :class:`SynthesizerResult` from
            :func:`syncade.synthesizer.run_synthesizer`.

    Returns:
        :class:`SynthesizerArtifactPaths` naming all written files.

    Raises:
        FileNotFoundError: If ``round_dir`` does not exist (caller
            bug — the orchestrator creates it during run setup).
    """
    _validate_reviewer_filename_basename(SYNTHESIZER_NAME)
    if not round_dir.is_dir():
        raise FileNotFoundError(f"round_dir does not exist: {round_dir}")

    base = round_dir / SYNTHESIZER_NAME

    raw_result = synth_result.raw_subprocess_result
    stdout_text = raw_result.stdout if raw_result is not None else ""
    stderr_text = raw_result.stderr if raw_result is not None else ""
    stdout_path = base.with_suffix(".stdout")
    stderr_path = base.with_suffix(".stderr")
    atomic_write_text(stdout_path, stdout_text)
    atomic_write_text(stderr_path, stderr_text)

    parsed_path: Path | None = None
    if synth_result.output is not None:
        parsed_path = base.with_suffix(".parsed.json")
        atomic_write_text(parsed_path, synth_result.output.model_dump_json(indent=2))

    error_path: Path | None = None
    if synth_result.error is not None:
        exc = synth_result.error
        lines = [
            f"{type(exc).__name__}: {exc}",
            "",
        ]
        tb = exc.__traceback__
        if tb is not None:
            lines.extend(traceback.format_exception(type(exc), exc, tb))
        else:
            lines.append("(no traceback available — exception was constructed, not raised)")
        error_path = base.with_suffix(".error.txt")
        atomic_write_text(error_path, "\n".join(lines))

    return SynthesizerArtifactPaths(
        stdout=stdout_path,
        stderr=stderr_path,
        parsed=parsed_path,
        error=error_path,
    )


def _synthesizer_manifest_entry(synth_result: SynthesizerResult | None) -> dict[str, object] | None:
    """Build the ``synthesizer`` section of the round manifest.

    Returns ``None`` when the synthesizer phase was skipped (any
    reviewer failed); a dict otherwise.

    Schema:

    .. code-block:: json

       {
         "outcome": "success" | "failure",
         "stdout_path": "synthesizer.stdout",
         "stderr_path": "synthesizer.stderr",
         "parsed_path": "synthesizer.parsed.json" | null,
         "error_path": "synthesizer.error.txt" | null,
         "duration_seconds": float,
         "error_type": null | "ExceptionClassName",
         "dismissed_count": int | null,
         "active_blocker_count": int | null,
         "active_minor_count": int | null,
         "active_nit_count": int | null
       }

    On success: counts populated, ``error_path`` AND ``error_type``
    are null (no .error.txt was written; no exception class to
    record).

    On failure: counts are null, ``error_path`` names the .error.txt
    artifact IFF persistence will actually write one — i.e. when
    ``synth_result.error is not None``. ``SynthesizerResult`` enforces
    exactly one of output/error at construction, so the null-error
    fallback is defensive only.
    """
    if synth_result is None:
        return None

    paths = {
        "provider": synth_result.provider or SYNTHESIZER_PROVIDER,
        "model": synth_result.model
        or (synth_result.usage.model if synth_result.usage is not None else SYNTHESIZER_MODEL),
        "stdout_path": f"{SYNTHESIZER_NAME}.stdout",
        "stderr_path": f"{SYNTHESIZER_NAME}.stderr",
    }

    if synth_result.output is not None:
        consolidated = synth_result.output.consolidated_findings
        dismissed = sum(1 for f in consolidated if f.dismissed)
        active_by_sev = {"blocker": 0, "minor": 0, "nit": 0}
        for f in consolidated:
            if not f.dismissed:
                active_by_sev[f.severity] += 1
        return {
            "outcome": "success",
            **paths,
            "parsed_path": f"{SYNTHESIZER_NAME}.parsed.json",
            "error_path": None,
            "duration_seconds": synth_result.duration_seconds,
            **usage_fields(synth_result.usage),
            # Include error_type on success (null) for schema symmetry;
            # downstream tools don't have to KeyError-guard or treat absence as
            # a separate signal.
            "error_type": None,
            "dismissed_count": dismissed,
            "active_blocker_count": active_by_sev["blocker"],
            "active_minor_count": active_by_sev["minor"],
            "active_nit_count": active_by_sev["nit"],
        }

    # Failure path. error_path is null when no .error.txt will be
    # written — that's the contract-violation case (output=None AND
    # error=None) where persist_synthesizer_result intentionally
    # skips the error.txt write. Without this guard the manifest
    # would point at a file that doesn't exist on disk.
    error_type = type(synth_result.error).__name__ if synth_result.error is not None else None
    error_path = f"{SYNTHESIZER_NAME}.error.txt" if synth_result.error is not None else None
    return {
        "outcome": "failure",
        **paths,
        "parsed_path": None,
        "error_path": error_path,
        "duration_seconds": synth_result.duration_seconds,
        **usage_fields(synth_result.usage),
        "error_type": error_type,
        "dismissed_count": None,
        "active_blocker_count": None,
        "active_minor_count": None,
        "active_nit_count": None,
    }
