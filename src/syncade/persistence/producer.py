"""Producer subprocess persistence.

Writes ``<round_dir>/producer.{stdout,stderr,commit.txt[,error.txt]}``
and the matching round-manifest entry. The orchestrator only calls
these on rounds where the producer actually ran (NO-SHIP rounds with
``max_rounds > 1``).
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

from syncade.producer import ProducerResult
from syncade.usage import usage_fields

from ._atomic import atomic_write_text
from ._validation import _validate_reviewer_filename_basename

# Hardcoded basename for the producer artifacts. There is exactly one
# producer run per round (where the producer ran at all), so the
# hardcoded basename mirrors the synthesizer's single-instance design.
PRODUCER_NAME = "producer"


@dataclass(frozen=True)
class ProducerArtifactPaths:
    """Where the producer subprocess's artifacts land on disk.

    Returned by :func:`persist_producer_result` and attached to a
    :class:`RoundArtifacts` so the orchestrator + CLI can address
    the files without re-deriving the layout convention.

    All four paths are absolute and rooted at the round directory.
    The producer doesn't emit structured JSON like the reviewer +
    synthesizer phases, so there's no ``parsed.json`` — the
    narrative text goes into ``producer.stdout`` directly.
    """

    stdout: Path
    stderr: Path
    commit_sha: Path
    error: Path | None
    recovery_ref: Path | None
    import_error: Path | None


def persist_producer_result(
    round_dir: Path,
    producer_result: ProducerResult,
) -> ProducerArtifactPaths:
    """Write the producer subprocess's outputs to
    ``<round_dir>/producer.{stdout,stderr,commit.txt[,error.txt]}``.

    Mirrors :func:`persist_synthesizer_result`'s and
    :func:`persist_test_run_result`'s file-layout conventions so a
    tool inspecting the round directory sees producer artifacts in
    the same shape as the per-reviewer, synthesizer, and test-leg
    artifacts.

    Files written:

    - ``producer.stdout`` — the producer's narrative text from
      :attr:`ProducerOutput.narrative_text` on ``"committed"`` /
      ``"stalled"`` outcomes; the raw subprocess stdout when
      ``raw_subprocess_result`` is preserved (timeout, parse failure);
      empty string when the subprocess never ran
      (``SubprocessNotFoundError``).
    - ``producer.stderr`` — the raw subprocess stderr when available;
      empty string when ``raw_subprocess_result is None``.
    - ``producer.commit.txt`` — one-line hex string: the
      :attr:`ProducerResult.ending_sha`. On ``"stalled"`` this equals
      :attr:`ProducerResult.starting_sha`; on ``"committed"`` it
      differs. On ``"subprocess_error"`` it may differ when the
      producer moved HEAD before failing; that file then includes an
      indeterminate-commit note. Same shell-grep pattern as
      ``test-run.exit-code.txt``: a shell-script consumer
      (CI integration, ``grep``) can pull the SHA without parsing
      JSON.
    - ``producer.error.txt`` — written ONLY on
      ``outcome="subprocess_error"``; carries the exception class
      name + message + traceback (when available). Matches the
      reviewer + synthesizer ``.error.txt`` shape so downstream
      tooling treats all subprocess failures uniformly.

    Args:
        round_dir: The round directory to write into. Must already
            exist (the orchestrator creates it during round setup).
        producer_result: The :class:`ProducerResult` from
            :func:`syncade.producer.run_producer`. The caller (the
            orchestrator) only calls this on rounds where the
            producer actually ran; rounds without a producer
            (SHIP at round 0, max-rounds-reached) never reach here.

    Returns:
        :class:`ProducerArtifactPaths` naming all written files
        (``error`` is ``None`` on the success / stall paths).

    Raises:
        FileNotFoundError: If ``round_dir`` does not exist (caller
            bug — the orchestrator creates it during round setup).
    """
    _validate_reviewer_filename_basename(PRODUCER_NAME)
    if not round_dir.is_dir():
        raise FileNotFoundError(f"round_dir does not exist: {round_dir}")

    base = round_dir / PRODUCER_NAME
    stdout_path = base.with_suffix(".stdout")
    stderr_path = base.with_suffix(".stderr")
    # The commit-SHA file uses a compound suffix (.commit.txt) so the
    # ``shell-grep <round_dir>/producer.commit.txt`` recipe stays
    # legible. Path.with_suffix would replace ``.stdout`` whole;
    # build the name directly.
    commit_sha_path = round_dir / f"{PRODUCER_NAME}.commit.txt"

    # stdout: prefer the producer's narrative_text on success/stall;
    # fall back to the raw subprocess stdout on the subprocess-error
    # path (where the adapter never produced a ProducerOutput).
    if producer_result.output is not None:
        stdout_text = producer_result.output.narrative_text
    elif producer_result.raw_subprocess_result is not None:
        stdout_text = producer_result.raw_subprocess_result.stdout
    else:
        stdout_text = ""
    atomic_write_text(stdout_path, stdout_text)

    raw_result = producer_result.raw_subprocess_result
    stderr_text = raw_result.stderr if raw_result is not None else ""
    atomic_write_text(stderr_path, stderr_text)

    # One-line hex string + trailing newline — shell-grep convention
    # matches test-run.exit-code.txt.
    commit_sha_text = f"{producer_result.ending_sha}\n"
    indeterminate_commit = (
        producer_result.outcome == "subprocess_error"
        and producer_result.ending_sha != producer_result.starting_sha
    )
    atomic_write_text(commit_sha_path, commit_sha_text)

    error_path: Path | None = None
    if producer_result.error is not None:
        exc = producer_result.error
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
        if indeterminate_commit:
            lines.extend(
                [
                    "",
                    "Indeterminate producer commit: HEAD moved from "
                    f"{producer_result.starting_sha} to {producer_result.ending_sha} "
                    "before the subprocess_error outcome.",
                ]
            )
        atomic_write_text(error_path, "\n".join(lines))

    candidate_import = producer_result.candidate_import
    recovery_ref_path: Path | None = None
    import_error_path: Path | None = None
    if candidate_import is not None and candidate_import.recovery_ref is not None:
        recovery_ref_path = round_dir / f"{PRODUCER_NAME}.recovery-ref.txt"
        atomic_write_text(recovery_ref_path, f"{candidate_import.recovery_ref}\n")
    if candidate_import is not None and candidate_import.error is not None:
        import_error_path = round_dir / f"{PRODUCER_NAME}.import.error.txt"
        atomic_write_text(import_error_path, f"{candidate_import.error}\n")

    return ProducerArtifactPaths(
        stdout=stdout_path,
        stderr=stderr_path,
        commit_sha=commit_sha_path,
        error=error_path,
        recovery_ref=recovery_ref_path,
        import_error=import_error_path,
    )


def _producer_manifest_entry(
    producer_result: ProducerResult | None,
    producer_config_provider: str | None = None,
    producer_config_model: str | None = None,
) -> dict[str, object] | None:
    """Build the ``producer`` section of the round manifest.

    Returns ``None`` when the producer phase was skipped (round
    SHIPped at the verdict stage; this round was the last round
    of the loop). Returns a dict with the documented synthesizer schema
    otherwise.

    Schema:

    .. code-block:: json

       {
         "outcome": "committed" | "stalled" | "subprocess_error" | "escalated",
         "provider": "anthropic",
         "model": "claude-sonnet-4-6",
         "starting_sha": "<full-sha1-or-sha256-hex>",
         "ending_sha": "<full-sha1-or-sha256-hex>",
         "duration_seconds": float,
         "retried": int,  // OMITTED when retries == 0 (C5: happy-path byte-identical)
         "stdout_path": "producer.stdout",
         "stderr_path": "producer.stderr",
         "commit_sha_path": "producer.commit.txt",
         "error_path": null | "producer.error.txt",
         "error_type": null | "SubprocessTimeoutError" | ...,
         "escalation": { "finding", "decision", "options", "rationale" }
             // present ONLY on outcome=="escalated"; omitted otherwise
       }

    The provider + model are echoed from the producer config so a
    consumer reading manifest.json knows which adapter ran without
    cross-referencing back to ``.syncade/config.toml``.
    """
    if producer_result is None:
        return None
    provider = producer_result.provider or producer_config_provider
    model = producer_result.model or producer_config_model
    entry: dict[str, object] = {
        "outcome": producer_result.outcome,
        "provider": provider,
        "model": model,
        "starting_sha": producer_result.starting_sha,
        "ending_sha": producer_result.ending_sha,
        "duration_seconds": producer_result.duration_seconds,
        **usage_fields(producer_result.usage),
        "stdout_path": f"{PRODUCER_NAME}.stdout",
        "stderr_path": f"{PRODUCER_NAME}.stderr",
        "commit_sha_path": f"{PRODUCER_NAME}.commit.txt",
        "error_path": (f"{PRODUCER_NAME}.error.txt" if producer_result.error is not None else None),
        "error_type": (
            type(producer_result.error).__name__ if producer_result.error is not None else None
        ),
    }
    # C5: omit "retried" entirely when no retries occurred so the happy-path
    # producer block is byte-identical to pre-PR-v2-22. Presence implies > 0.
    if producer_result.retries > 0:
        entry["retried"] = producer_result.retries
    if (
        producer_result.outcome == "subprocess_error"
        and producer_result.ending_sha != producer_result.starting_sha
    ):
        entry["indeterminate_commit"] = True
    # (QA finding 3): on the escalated outcome, carry the structured
    # escalation so a tool reading manifest.json alone can reconstruct WHY the
    # loop paused for a decision (not just decision-needed.md / producer.stdout).
    # The key is OMITTED on every other outcome (escalation is None) → the
    # committed/stalled/subprocess_error manifest entries are byte-identical.
    if producer_result.escalation is not None:
        esc = producer_result.escalation
        entry["escalation"] = {
            "finding": esc.finding,
            "decision": esc.decision,
            "options": list(esc.options),
            "rationale": esc.rationale,
        }
    if producer_result.candidate_import is not None:
        candidate_import = producer_result.candidate_import
        entry["candidate_import"] = {
            "status": candidate_import.status,
            "recovery_ref": candidate_import.recovery_ref,
            "error": candidate_import.error,
            "recovery_ref_path": (
                f"{PRODUCER_NAME}.recovery-ref.txt"
                if candidate_import.recovery_ref is not None
                else None
            ),
            "error_path": (
                f"{PRODUCER_NAME}.import.error.txt" if candidate_import.error is not None else None
            ),
        }
    return entry
