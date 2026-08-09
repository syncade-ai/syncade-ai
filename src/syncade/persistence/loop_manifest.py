"""Loop-level loop-manifest.json persistence.

Writes ``<run_dir>/loop-manifest.json`` — the top-level
machine-readable manifest with the multi-round rounds[] array. Per-
round manifests still live at ``round-N/manifest.json``; this top-
level loop-manifest aggregates the per-round entries and adds the
loop-level fields (``final_exit_code``, ``final_round``,
``termination_reason``, ``max_rounds``).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from syncade import __version__

from ._atomic import atomic_write_json
from .checks import _check_manifest_entry
from .producer import _producer_manifest_entry
from .reviewer import _reviewer_manifest_entry
from .synth import _synthesizer_manifest_entry
from .test_run import _test_run_manifest_entry


def persist_loop_manifest(
    run_dir: Path,
    *,
    final_exit_code: int,
    final_round: int,
    termination_reason: str,
    rounds: list,  # list[RoundResult]
    max_rounds: int,
    started_at: datetime,
    producer_provider: str | None,
    producer_model: str | None,
) -> Path:
    """Write ``<run_dir>/loop-manifest.json`` — the top-level
    machine-readable manifest with the multi-round rounds[] array.

    Per-round manifests still live at ``round-N/manifest.json``; the top-level
    loop-manifest aggregates the per-round entries and adds the
    loop-level fields (``final_exit_code``, ``final_round``,
    ``termination_reason``, ``max_rounds``).

    Schema:

    .. code-block:: json

       {
         "syncade_version": "0.x.y",
         "run_id": "...",
         "started_at_utc": "...",
         "max_rounds": 3,
         "final_exit_code": 0,
         "final_round": 1,
         "termination_reason": "ship",
         "rounds": [
           {
             "round": 0,
             "snapshot": {...},
             "reviewers": [...],
             "synthesizer": {...},
             "test_run": null | {...},
             "test_skip_reason": null | "...",
             "producer": null | {...},
             "round_exit_code": 30
           },
           {... round 1 ...}
         ]
       }

    Args:
        run_dir: Top-level run directory.
        final_exit_code: The loop's final exit code.
        final_round: 0-indexed round the loop terminated at.
        termination_reason: Categorical termination label.
        rounds: List of RoundResult objects from the orchestrator.
        max_rounds: Configured ``[loop] max_rounds`` ceiling.
        started_at: Run start instant.
        producer_provider: ``config.producer.provider``, echoed
            into each round's producer section.
        producer_model: ``config.producer.model``.

    Returns:
        Path of the written ``loop-manifest.json``.
    """
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run_dir does not exist: {run_dir}")

    run_id = run_dir.name
    round_entries: list[dict[str, object]] = []
    for r in rounds:
        entry: dict[str, object] = {
            "round": r.round_idx,
            "snapshot": {
                "commit_sha": r.snapshot.commit_sha,
                "branch": r.snapshot.branch,
                "base_ref": r.snapshot.base_ref,
                "base_oid": r.snapshot.base_oid,
                "diff_present": bool(r.snapshot.diff_text),
                # Mirror the per-round manifest's size history fields.
                # Both are None when not measured (diff_malformed, resume); see
                # round_manifest.py for the full rationale.
                "diff_bytes": r.raw_diff_bytes,
                "diff_bytes_reviewed": r.filtered_diff_bytes,
            },
            "reviewers": (
                [_reviewer_manifest_entry(rr) for rr in r.dispatch_result.results]
                if r.dispatch_result is not None
                else []
            ),
            "synthesizer": _synthesizer_manifest_entry(r.synth_result),
            "test_run": _test_run_manifest_entry(r.test_result),
            "test_skip_reason": r.test_skip_reason if r.test_result is None else None,
            "producer": _producer_manifest_entry(
                r.producer_result,
                producer_config_provider=producer_provider,
                producer_config_model=producer_model,
            ),
            "round_exit_code": r.round_exit_code,
        }
        # Mirror the per-round manifest (round_manifest.py): append the
        # checks array ONLY when checks ran, so a zero-check round's
        # loop entry stays byte-identical and the two surfaces agree on
        # checks[] presence/shape.
        if r.check_results:
            entry["checks"] = [_check_manifest_entry(c) for c in r.check_results]
        # Mirror the per-round manifest's refusal discriminator fields so the loop
        # manifest is self-sufficient for tooling that wants to classify per-round
        # refusal causes without reading individual round-N/manifest.json files.
        if r.fail_closed_headers is not None:
            entry["diff_filter_refusal_headers"] = r.fail_closed_headers
        # Derive refusal_reason the same way round_no_changes.py does: fail_closed_headers
        # → diff_malformed; oversize_diff_bytes → diff_too_large; oversize_prompt_chars →
        # prompt_too_large. Only one is set per run; written only when non-None.
        _refusal_reason: str | None
        if r.fail_closed_headers is not None:
            _refusal_reason = "diff_malformed"
        elif r.oversize_diff_bytes is not None:
            _refusal_reason = "diff_too_large"
        elif r.oversize_prompt_chars is not None:
            _refusal_reason = "prompt_too_large"
        else:
            _refusal_reason = None
        if _refusal_reason is not None:
            entry["refusal_reason"] = _refusal_reason
        if r.oversize_prompt_chars is not None:
            entry["oversize_prompt_chars"] = r.oversize_prompt_chars
        round_entries.append(entry)

    manifest = {
        "syncade_version": __version__,
        "run_id": run_id,
        "started_at_utc": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "max_rounds": max_rounds,
        "final_exit_code": final_exit_code,
        "final_round": final_round,
        "termination_reason": termination_reason,
        "rounds": round_entries,
    }

    path = run_dir / "loop-manifest.json"
    atomic_write_json(path, manifest, sort_keys=False)
    return path
