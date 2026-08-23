"""`--quiet` suppresses progress, never the fate of committed work — PR-h-13 item 2.

After a producer commit the branch ref advances but the operator's index does not, so
`git status` shows a fully STAGED REVERT of the producer's work and the next `git commit`
silently undoes the run. One warning stands between the operator and that commit, and it went
through `Logger.warning`, which `--quiet` drops on the floor.

It is not hypothetical. It happened twice in one session on 2026-08-10 with the warning VISIBLE
and an experienced operator driving; the instruction that round was "commit the producer fixes",
which would have reverted five producer commits. Under `--quiet` there would have been no
warning at all.

`Logger.safety` is the named rule. This pins both halves — that safety disclosures survive
quiet, and that ordinary progress still does not, because a "fix" that makes `--quiet` print
everything is not a fix.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from syncade.logging import Logger

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "syncade"

# Every warning that answers "where did the producer's commits go, and is my tree consistent
# with that?". Deliberately NOT the dirty-tree, resume-drift or auto-prune notes: those describe
# the run, not the fate of work already committed.
_SAFETY_SITES = {
    "orchestrator/branch_advance.py": 4,
    # 2 since PR-h-05: the branch-advanced working-tree warning, plus the notice naming a
    # committed candidate that did NOT reach the operator repository — where the preserved
    # standalone repository is the ONLY copy of that work, which is this class exactly.
    "orchestrator/loop_finalize.py": 2,
}


@pytest.mark.parametrize("level", ["normal", "quiet"])
def test_safety_disclosures_are_emitted_at_every_level(level, capsys):
    Logger(level).safety("branch advanced; your working tree is NOT synced")
    err = capsys.readouterr().err
    assert "NOT synced" in err, f"safety disclosure vanished at level={level!r}"
    assert "warning:" in err


def test_quiet_still_suppresses_ordinary_progress(capsys):
    """The counter-test: if this passes only because --quiet stopped suppressing, it is no fix."""
    log = Logger("quiet")
    log.event("dispatching 2 reviewers")
    log.warning("untracked files present")
    captured = capsys.readouterr()
    assert captured.out == "", "quiet mode leaked progress output"
    assert captured.err == "", "quiet mode leaked an ordinary warning"


@pytest.mark.parametrize("rel,expected", sorted(_SAFETY_SITES.items()))
def test_the_committed_work_disclosures_use_safety(rel: str, expected: int):
    """Pin the call sites, so a future edit cannot quietly downgrade one back to `warning`."""
    source = (_SRC / rel).read_text(encoding="utf-8")
    found = len(re.findall(r"logger\.safety\(", source))
    assert found == expected, (
        f"{rel}: expected {expected} logger.safety call(s), found {found}. "
        "A disclosure about the disposition of committed work must not be suppressible."
    )


def test_the_branch_advance_warning_names_the_recovery():
    """A disclosure the operator cannot act on is only half a disclosure."""
    source = (_SRC / "orchestrator" / "loop_finalize.py").read_text(encoding="utf-8")
    assert "NOT" in source and "automatically synced" in source
    assert "git stash" in source, "the non-destructive recovery must be offered, not just reset"
