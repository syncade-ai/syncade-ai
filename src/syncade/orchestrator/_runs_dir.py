"""Runs-directory provisioning helper.

Writes the one-line ``.gitignore`` that drops a ``*`` rule alongside
``.syncade/runs/`` on first run. Pure file I/O.
"""

from __future__ import annotations

from pathlib import Path

_RUNS_GITIGNORE_CONTENT: str = "*\n"


def _ensure_runs_gitignore(runs_root: Path) -> None:
    """Auto-write ``<repo>/.syncade/runs/.gitignore`` on first run.

    Every run that creates a new ``.syncade/runs/`` directory drops a ``*``
    .gitignore alongside so an accidental ``git add -A`` doesn't sweep run
    artifacts into source control. Idempotent.
    """
    gitignore_path = runs_root / ".gitignore"
    if gitignore_path.exists():
        return
    gitignore_path.write_text(_RUNS_GITIGNORE_CONTENT, encoding="utf-8")
