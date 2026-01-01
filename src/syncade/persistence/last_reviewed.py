"""Per-branch last-reviewed SHA persistence.

``<repo>/.syncade/last-reviewed.json`` is **cross-run** state (a sibling of
``runs/``, NOT under any single run dir) that records, per branch, the HEAD a
completed review last covered. A subsequent ``--scope since-last-review`` bounds
its diff to commits after that SHA, so each review covers only new work instead
of re-reviewing the whole branch every run.

It is per-machine run state — gitignored, never committed, never cross-branch.
Shape::

    { "<branch>": { "sha": "<reviewed-HEAD>", "run_id": "...", "recorded_at_utc": "..." } }
"""

from __future__ import annotations

import json
from pathlib import Path

from ._atomic import atomic_write_json

LAST_REVIEWED_FILENAME = "last-reviewed.json"
"""Basename under ``<repo>/.syncade/`` of the per-branch last-reviewed record."""


def _path(repo_root: Path) -> Path:
    return repo_root / ".syncade" / LAST_REVIEWED_FILENAME


def _load(repo_root: Path) -> dict:
    path = _path(repo_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def persist_last_reviewed(
    repo_root: Path,
    *,
    branch: str,
    sha: str,
    run_id: str,
    recorded_at_utc: str,
) -> Path:
    """Record ``sha`` as ``branch``'s last-reviewed HEAD, merging into the
    existing file so other branches' records are preserved. Creates
    ``<repo>/.syncade/`` if needed. Returns the written path."""
    data = _load(repo_root)
    data[branch] = {"sha": sha, "run_id": run_id, "recorded_at_utc": recorded_at_utc}
    path = _path(repo_root)
    atomic_write_json(path, data, sort_keys=True)
    return path


def read_last_reviewed(repo_root: Path, branch: str) -> str | None:
    """Return the recorded last-reviewed SHA for ``branch``, or ``None`` when the
    file is absent/unparseable or has no entry for that branch."""
    entry = _load(repo_root).get(branch)
    if isinstance(entry, dict):
        sha = entry.get("sha")
        if isinstance(sha, str) and sha:
            return sha
    return None
