"""Run-init persistence.

Writes ``<run_dir>/run-init.json`` — the top-level "what was this run
about" record, captured at the very start of every fresh ``syncade
<pr-doc>`` invocation (before round 0's directory exists).

The file exists so ``--resume <run-id>`` can
reconstruct enough of the original invocation's context to decide
eligibility (was the operator on this branch? did the original run
start from this SHA?) and to validate against tree drift between
abort and resume.

Schema:

.. code-block:: json

   {
     "syncade_version": "0.1.0",
     "started_at_utc": "2026-05-28T16:43:59Z",
     "pr_doc_path": "design docs",
     "base_ref": "1dbbca3",
     "base_oid": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
     "starting_sha": "3442e72ab60b43153cdbee83f3187203b289f14a",
     "operator_branch": "main",
     "max_rounds": 3,
     "config_snapshot": { ... }
   }

The ``config_snapshot`` is informational only — the resumed loop
reads the CURRENT ``.syncade/config.toml`` from disk; the snapshot
exists so the operator (or a future agent) inspecting an aborted run
knows what config produced it.

It is a ``model_dump`` of :class:`SyncadeConfig` echoing every field at
its serialized value, with ONE carve-out: a
default-empty ``checks`` list is omitted, so a zero-config run-init.json does
not carry an empty optional section. A configured ``[[checks]]`` list is still
echoed; only the empty default is dropped.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from syncade.config import SyncadeConfig

from ._atomic import atomic_write_json

RUN_INIT_FILENAME: str = "run-init.json"
"""Filename used for the run-init artifact. Single source of
truth so the persistence writer, the resume detector, and any future
tooling all agree on the path."""


def _config_snapshot(config: SyncadeConfig) -> dict:
    """Serialize the config for the diagnostic ``config_snapshot`` block.

    A default-empty ``checks`` list is omitted so zero-config runs do not carry
    an empty optional section. A configured ``[[checks]]`` list is still echoed
    for diagnostics. Every other field is echoed verbatim.
    """
    snapshot = config.model_dump(mode="json")
    if snapshot.get("checks") == []:
        del snapshot["checks"]
    return snapshot


def persist_run_init(
    run_dir: Path,
    *,
    syncade_version: str,
    started_at: datetime,
    pr_doc_path: Path,
    base_ref: str | None,
    base_oid: str | None,
    starting_sha: str,
    operator_branch: str | None,
    max_rounds: int,
    config: SyncadeConfig,
) -> Path:
    """Write ``<run_dir>/run-init.json`` capturing the run's initial
    state.

    Called ONCE per run, at the very start of
    :func:`syncade.orchestrator.loop.run_review` (after the run directory
    exists, before round 0's directory is created). A
    resumed run does NOT rewrite this file — the snapshot captured
    at the original run start is the authoritative record across
    resume boundaries.

    Args:
        run_dir: Top-level run directory (``<repo>/.syncade/runs/<id>/``).
            Must already exist; the run-id mkdir happens before this
            call in ``run_review``.
        syncade_version: The :data:`syncade.__version__` at run start.
            Recorded so a resume across a syncade upgrade can emit a
            stderr warning.
        started_at: The :func:`datetime.now(tz=UTC)` instant captured
            at run start. Same value threaded into manifest /
            loop-manifest / summary so they all agree on when the
            run began.
        pr_doc_path: Path to the PR doc the run was invoked against.
            Serialized as the ``str(...)`` of the path so the file
            stays JSON-safe.
        base_ref: The ``--base`` value the CLI received, or ``None``
            if no diff was requested. Distinct from ``starting_sha``
            (the resolved HEAD SHA at run start).
        base_oid: The resolved full object ID of ``base_ref`` at
            snapshot time, or ``None`` when ``base_ref`` is ``None``.
            Unlike ``base_ref``, this is immutable even if the ref
            moves after the run starts.
        starting_sha: The full HEAD object ID the orchestrator
            snapshotted at round 0. The authoritative tree state
            the run began against.
        operator_branch: The branch the operator was on at run
            start (``Snapshot.branch``). ``None`` for detached
            HEAD. Captured so resume can validate the operator
            hasn't switched branches between abort and resume.
        max_rounds: The ``config.loop.max_rounds`` cap the run was
            launched with. Captured because a future ``--resume
            --max-rounds N`` may override the original cap; the
            original is preserved here for diagnostic comparison.
        config: The :class:`SyncadeConfig` instance loaded at run
            start. Serialized via :meth:`pydantic.BaseModel.model_dump`
            with ``mode="json"`` so enums + paths flatten to
            JSON-safe primitives.

    Returns:
        Path of the written ``run-init.json``.

    Raises:
        FileNotFoundError: If ``run_dir`` does not exist.
    """
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist: {run_dir}")

    record = {
        "syncade_version": syncade_version,
        "started_at_utc": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pr_doc_path": str(pr_doc_path),
        "base_ref": base_ref,
        "base_oid": base_oid,
        "starting_sha": starting_sha,
        "operator_branch": operator_branch,
        "max_rounds": max_rounds,
        # Full config echo. See module docstring.
        "config_snapshot": _config_snapshot(config),
    }

    run_init_path = run_dir / RUN_INIT_FILENAME
    atomic_write_json(run_init_path, record, sort_keys=False)
    return run_init_path
