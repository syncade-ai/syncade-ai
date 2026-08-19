"""`syncade --help` must document the GC four-tier retention contract.

The README used to embed a verbatim copy of `syncade --help`, and this file guarded that
copy against drift. The README is now an onboarding front-door that defers CLI detail to
`--help`, so the embed-drift guard retires with it. What stays load-bearing is the one
contract an operator MUST NOT get wrong — that `--gc` never deletes a run directory — so we
assert it remains in the help text itself, where the flag is documented.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys


def _live_help() -> str:
    # Pin COLUMNS: argparse wraps to terminal width, so an unpinned width would make this
    # test depend on the shape of the developer's terminal.
    env = {**os.environ, "COLUMNS": "80"}
    return subprocess.run(
        [sys.executable, "-m", "syncade", "--help"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()


def test_gc_help_does_not_advertise_nonexistent_flag() -> None:
    """``syncade --help`` must not reference ``--gc-worktree-max-age-days``.

    That flag never existed. The worktree retention bound is config-only via
    ``gc.worktree_max_age_days``; following the help text as written previously caused
    an argparse error. The help now names the config key instead.
    """
    assert "--gc-worktree-max-age-days" not in _live_help(), (
        "help references a nonexistent flag; use gc.worktree_max_age_days config key instead"
    )


def test_help_documents_four_tier_retention() -> None:
    """The retention contract is the one thing an operator MUST not get wrong: it
    determines whether they believe `--gc` destroys their run history. Assert the
    substance is in `syncade --help`, not merely that two copies match."""
    block = " ".join(line.strip() for line in _live_help().split("\n"))
    # argparse wraps long words across the hyphen ("FOUR-" / "TIER"); rejoin them.
    block = re.sub(r"-\s+", "-", block)
    for claim in ("Retention is FOUR-TIER", "kept FOREVER", "NO run directory is ever deleted"):
        assert claim in block, f"`syncade --help` no longer documents: {claim!r}"
