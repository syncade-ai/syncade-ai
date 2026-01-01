"""Test re-run leg persistence.

Writes ``<round_dir>/test-run.{stdout,stderr,exit-code.txt}`` and the
matching round-manifest entry. The orchestrator only calls these on
rounds where the operator configured ``[loop] test_command`` AND every
prior phase succeeded AND the synthesizer was clean.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from syncade.test_runner import TestRunResult

from ._atomic import atomic_write_text
from ._validation import _validate_reviewer_filename_basename

# Hardcoded basename for the test-leg artifacts. There is exactly one
# test run per round, so no per-test-command collision risk. The
# hardcoded basename also keeps the operator's ``test_command`` string
# (which can be anything) entirely out of the filesystem layer —
# manifest.json echoes the command but no file is ever named from it.
TEST_RUN_NAME = "test-run"


@dataclass(frozen=True)
class TestRunArtifactPaths:
    """Where the test re-run leg's artifacts land on disk.

    Returned by :func:`persist_test_run_result` and attached to
    :class:`~syncade.orchestrator.RunArtifacts` so the CLI / future
    loop can address the files without re-deriving the layout
    convention.

    All three paths are absolute and rooted at ``<round_dir>``. They
    are always written when the test leg ran (the orchestrator only
    calls :func:`persist_test_run_result` on a non-None
    ``test_result``); the unconfigured-leg path keeps the field
    ``None`` on :class:`~syncade.orchestrator.RunArtifacts` so the
    no-files-written-on-disk signal stays unambiguous.
    """

    stdout: Path
    stderr: Path
    exit_code: Path


def persist_test_run_result(
    round_dir: Path,
    test_result: TestRunResult,
) -> TestRunArtifactPaths:
    """Write the test re-run leg's outputs to
    ``<round_dir>/test-run.{stdout,stderr,exit-code.txt}``.

    Mirrors :func:`persist_reviewer_result` and
    :func:`persist_synthesizer_result`'s file-layout convention so a
    tool inspecting the round directory sees the test artifacts in
    the same shape as the per-reviewer and synthesizer artifacts.

    Files written:

    - ``test-run.stdout`` — captured test command stdout (whatever
      reached the pipe buffer before the subprocess exited or was
      killed). Empty on the subprocess-error path where the
      subprocess never started (binary missing, bad cwd).
    - ``test-run.stderr`` — captured test command stderr.
    - ``test-run.exit-code.txt`` — one-line integer terminated by
      ``\\n`` (``0`` for passed, the actual exit code for failed,
      ``-1`` for subprocess error). Stored as a hardcoded basename
      file rather than embedded only in ``manifest.json`` so a
      shell-script consumer (CI integration, ``grep`` against the
      round directory) can pull it without parsing JSON.

    Args:
        round_dir: The round directory to write into. Must already
            exist (the orchestrator creates it during run setup).
        test_result: The :class:`TestRunResult` from
            :func:`syncade.test_runner.run_tests`. The caller (the
            orchestrator) only calls this on a non-None test_result;
            the unconfigured-leg path never reaches here.

    Returns:
        :class:`TestRunArtifactPaths` naming all three written files.

    Raises:
        FileNotFoundError: If ``round_dir`` does not exist (caller
            bug — the orchestrator creates it during run setup).
    """
    _validate_reviewer_filename_basename(TEST_RUN_NAME)
    if not round_dir.is_dir():
        raise FileNotFoundError(f"round_dir does not exist: {round_dir}")

    base = round_dir / TEST_RUN_NAME
    stdout_path = base.with_suffix(".stdout")
    stderr_path = base.with_suffix(".stderr")
    # ``test-run.exit-code.txt`` — Path.with_suffix would replace the
    # ``.stdout`` suffix wholesale, leaving us with
    # ``test-run.exit-code.txt`` only if we hand-stitch. Build the
    # filename directly rather than relying on with_suffix; the
    # hardcoded basename keeps the layout self-explanatory.
    exit_code_path = round_dir / f"{TEST_RUN_NAME}.exit-code.txt"

    atomic_write_text(stdout_path, test_result.stdout)
    atomic_write_text(stderr_path, test_result.stderr)
    atomic_write_text(exit_code_path, f"{test_result.exit_code}\n")

    return TestRunArtifactPaths(
        stdout=stdout_path,
        stderr=stderr_path,
        exit_code=exit_code_path,
    )


def _test_run_manifest_entry(test_result: TestRunResult | None) -> dict[str, object] | None:
    """Build the ``test_run`` section of the round manifest.

    Returns ``None`` when the leg was skipped (either ``test_command``
    is not configured OR a prior phase failed/produced blockers).
    Returns a dict with the documented synthesizer schema otherwise.

    Schema:

    .. code-block:: json

       {
         "outcome": "passed" | "failed" | "subprocess_error",
         "exit_code": 0 | int | -1,
         "command": "<operator-configured string>",
         "duration_seconds": float,
         "stdout_path": "test-run.stdout",
         "stderr_path": "test-run.stderr",
         "exit_code_path": "test-run.exit-code.txt",
         "error_type": null | "SubprocessTimeoutError" | ...
       }

    ``command`` is echoed verbatim from the operator's config so a
    consumer reading manifest.json knows what was run without
    cross-referencing back to ``.syncade/config.toml``.
    """
    if test_result is None:
        return None
    return {
        "outcome": test_result.outcome,
        "exit_code": test_result.exit_code,
        "command": test_result.command,
        "duration_seconds": test_result.duration_seconds,
        "stdout_path": f"{TEST_RUN_NAME}.stdout",
        "stderr_path": f"{TEST_RUN_NAME}.stderr",
        "exit_code_path": f"{TEST_RUN_NAME}.exit-code.txt",
        "error_type": (type(test_result.error).__name__ if test_result.error is not None else None),
    }
