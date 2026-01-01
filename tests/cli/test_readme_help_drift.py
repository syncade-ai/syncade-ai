"""The README's CLI block must be the real ``syncade --help``, not a copy of it.

It had drifted silently: the block still advertised ``--gc`` as "(PR-27): prune
accumulated .syncade/runs/<run-id>/ history" long after PR-v2-18 made GC keep that
history, and its usage line never mentioned ``--metrics`` at all. Nobody noticed,
because nothing compared them.

Mirrors ``tests/skills/test_skill_drift.py``: the fix for a hand-maintained copy is
not to hand-maintain it more carefully, it is to assert it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_README = Path(__file__).resolve().parents[2] / "README.md"


def _readme_help_block() -> str:
    lines = _README.read_text(encoding="utf-8").split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("usage: syncade"))
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "```")
    return "\n".join(lines[start:end]).strip()


def _live_help() -> str:
    # Pin COLUMNS: argparse wraps to terminal width, so an unpinned width would make
    # this test pass or fail on the shape of the developer's terminal.
    env = {**os.environ, "COLUMNS": "80"}
    out = subprocess.run(
        [sys.executable, "-m", "syncade", "--help"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout
    return out.strip()


def test_readme_cli_block_matches_live_help() -> None:
    readme, live = _readme_help_block(), _live_help()
    assert readme == live, (
        "README.md's CLI block has drifted from `syncade --help`.\n"
        "Regenerate it (COLUMNS=80 syncade --help) rather than hand-editing."
    )


def test_readme_documents_two_tier_retention() -> None:
    """The retention contract is the one thing an operator MUST not get wrong: it
    determines whether they believe `--gc` destroys their run history. Assert the
    substance, not just that the strings match each other."""
    block = " ".join(line.strip() for line in _readme_help_block().split("\n"))
    # argparse wraps long words across the hyphen ("TWO-" / "TIER"); rejoin them.
    block = re.sub(r"-\s+", "-", block)
    for claim in (
        "Retention is TWO-TIER",
        "kept FOREVER",
        "NO run directory is ever deleted",
    ):
        assert claim in block, f"README no longer documents: {claim!r}"
