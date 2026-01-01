"""Shared synthetic-fixture builders for the GC tests (PR-27).

Every builder writes into a ``tmp_path`` tree — NONE touches the real
``<repo>/.syncade/runs/`` or the real ``/tmp/syncade/``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def make_repo(tmp_path: Path) -> Path:
    """Create a minimal ``.syncade/runs/`` skeleton and return the repo root."""
    runs = tmp_path / ".syncade" / "runs"
    runs.mkdir(parents=True)
    return tmp_path


def write_run(
    repo_root: Path,
    run_id: str,
    *,
    started_at: datetime | None = None,
    final_exit_code: int | None = None,
    with_run_init: bool = True,
    with_loop_manifest: bool = True,
    with_round: bool = False,
    transcript_bytes: int = 1024,
) -> Path:
    """Create a synthetic run dir.

    ``final_exit_code`` writes a ``loop-manifest.json`` with that code.
    ``with_loop_manifest=False`` simulates an interrupted run (resume-eligible).
    ``with_run_init=False`` simulates a run directory created before
    ``run-init.json`` is written. GC treats it as ambiguous and protects it.
    """
    run_dir = repo_root / ".syncade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    if with_run_init:
        payload: dict = {"syncade_version": "0.1.0"}
        if started_at is not None:
            payload["started_at_utc"] = started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        (run_dir / "run-init.json").write_text(json.dumps(payload), encoding="utf-8")
    if with_loop_manifest and final_exit_code is not None:
        (run_dir / "loop-manifest.json").write_text(
            json.dumps({"final_exit_code": final_exit_code}), encoding="utf-8"
        )
    if with_round:
        write_round(run_dir, transcript_bytes=transcript_bytes)
    return run_dir


def write_round(run_dir: Path, *, round_idx: int = 0, transcript_bytes: int = 1024) -> Path:
    """Add a realistic ``round-N/`` to a run: bulky transcripts alongside the
    structured artifacts GC must PRESERVE (round manifest, parsed findings,
    summary, exit code).

    All subprocess transcript types are seeded (reviewer, synthesizer, producer,
    test-run) so tests cover the full set GC must prune, not only reviewer stdout.
    """
    round_dir = run_dir / f"round-{round_idx}"
    round_dir.mkdir(parents=True, exist_ok=True)
    # bulk — GC prunes ALL *.stdout and *.stderr, regardless of subprocess type
    (round_dir / "codex-reviewer.stdout").write_text("X" * transcript_bytes, encoding="utf-8")
    (round_dir / "codex-reviewer.stderr").write_text("E", encoding="utf-8")
    (round_dir / "synthesizer.stdout").write_text(
        "S" * (transcript_bytes // 10 or 1), encoding="utf-8"
    )
    (round_dir / "synthesizer.stderr").write_text("SE", encoding="utf-8")
    (round_dir / "producer.stdout").write_text(
        "P" * (transcript_bytes // 10 or 1), encoding="utf-8"
    )
    (round_dir / "producer.stderr").write_text("PE", encoding="utf-8")
    (round_dir / "test-run.stdout").write_text(
        "T" * (transcript_bytes // 10 or 1), encoding="utf-8"
    )
    (round_dir / "test-run.stderr").write_text("TE", encoding="utf-8")
    # structured — GC must keep these
    (round_dir / "manifest.json").write_text(json.dumps({"round": round_idx}), encoding="utf-8")
    (round_dir / "codex-reviewer.parsed.json").write_text(
        json.dumps({"verdict": "SHIP"}), encoding="utf-8"
    )
    (round_dir / "summary.md").write_text("# summary", encoding="utf-8")
    (round_dir / "test-run.exit-code.txt").write_text("0", encoding="utf-8")
    return round_dir


STRUCTURED_ROUND_ARTIFACTS = (
    "manifest.json",
    "codex-reviewer.parsed.json",
    "summary.md",
    "test-run.exit-code.txt",
)
"""Per-round artifacts that must SURVIVE a slim. If GC ever removes one of these,
`syncade --metrics` and an offline monitor lose history permanently —
the whole point of two-tier retention."""


def snapshot_tree(root: Path) -> set[str]:
    """Return the set of all paths under ``root`` (relative), for diffing."""
    return {str(p.relative_to(root)) for p in root.rglob("*")}
