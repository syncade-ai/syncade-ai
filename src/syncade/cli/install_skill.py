"""``syncade --install-skill`` — lay the bundled Agent Skill into the harness dirs.

The skill is shipped as package data (``src/syncade/skills/{claude,codex}/``), so a
``pip install``ed user with no repo checkout can still install the ``/syncade`` skill.
Read via :func:`importlib.resources.files` so it works from a wheel or an editable install
alike. Copies (self-contained), matching ``scripts/install-skill.sh``.
"""

from __future__ import annotations

import os
import shutil
import sys
from importlib.resources import files
from pathlib import Path

from syncade.exit_codes import CLI_USAGE_ERROR, SUCCESS

_VALID = ("all", "claude", "codex")


def _dest(harness: str, home: Path, codex_home: Path) -> Path:
    if harness == "claude":
        return home / ".claude" / "skills" / "syncade"
    return codex_home / "skills" / "syncade"


def install_skill(
    target: str = "all",
    *,
    home: Path | None = None,
    codex_home: Path | None = None,
) -> int:
    """Copy the bundled skill into the harness skill directories. Returns a CLI exit code."""
    if target not in _VALID:
        print(
            f"[syncade] unknown --install-skill target {target!r}; expected one of {_VALID}",
            file=sys.stderr,
        )
        return CLI_USAGE_ERROR

    home = home or Path.home()
    if codex_home is None:
        env_cx = os.environ.get("CODEX_HOME")
        codex_home = Path(env_cx) if env_cx else home / ".codex"

    harnesses = ["claude", "codex"] if target == "all" else [target]
    for harness in harnesses:
        src = files("syncade") / "skills" / harness
        dest = _dest(harness, home, codex_home)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # A prior install may be a SYMLINK (the README documents a symlink-to-checkout
        # option). shutil.rmtree refuses symlinks, so unlink those (and stray files) first;
        # only a real directory goes to rmtree. Broken symlinks are caught by is_symlink().
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for entry in src.iterdir():
            if entry.is_file():
                (dest / entry.name).write_bytes(entry.read_bytes())
        print(f"[syncade] installed {harness} skill -> {dest}", file=sys.stderr)

    print("[syncade] done — restart your harness to pick up the skill.", file=sys.stderr)
    return SUCCESS
