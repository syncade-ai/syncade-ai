"""The producer's Git boundary must NOT depend on ``producer.permissions``.

This is the load-bearing distinction behind PR-h-05's rank-3 closure claim, and it is easy to
lose. Two different things are often conflated:

* **Supplied Git authority** — the operator repository's own object database and refs being
  reachable by the producer's ORDINARY commands. That was the actual rank-3 defect: a linked
  worktree shares the operator's Git common directory, so ``git update-ref refs/heads/main``
  wrote the operator's ref with no path knowledge and no sandbox involved, while the producer's
  own ``HEAD`` stayed put and syncade reported a stall. PR-h-05 removes it by construction — a
  standalone repository, plus one trusted importer that decides which objects cross.
* **Host confinement** — stopping a producer that goes LOOKING for the operator repository on
  the filesystem. That needs an OS sandbox, which `permissions = "confined"` provides and
  `"yolo"` does not. The hardening wave explicitly DEFERRED it (D1: "OS sandbox / container per
  actor ... are host-trust concerns").

So `confined` delivers more than rank 3 asked for, and `yolo` delivers exactly rank 3 — but only
while the standalone store and the importer stay unconditional. The moment either becomes
`if permissions == "confined"`, `yolo` silently reopens the defect this PR exists to close, and
nothing else in the suite would notice. Hence this file.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from syncade import producer_import, producer_workspace
from syncade.orchestrator import loop_round_step, producer_phase


def _permission_branches(module) -> list[str]:
    """Source lines in ``module`` that BRANCH on a producer permission value."""
    source = inspect.getsource(module)
    tree = ast.parse(source)
    lines = source.splitlines()
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.IfExp, ast.Match)):
            continue
        segment = ast.get_source_segment(source, node) or ""
        head = segment.splitlines()[0] if segment else ""
        if "permission" in head.lower() or '"yolo"' in head or '"confined"' in head:
            found.append(f"{module.__name__}:{node.lineno}: {lines[node.lineno - 1].strip()}")
    return found


@pytest.mark.parametrize(
    "module",
    [producer_workspace, producer_import, producer_phase, loop_round_step],
    ids=lambda m: m.__name__,
)
def test_no_module_on_the_boundary_branches_on_permissions(module):
    assert _permission_branches(module) == []


def test_only_the_adapters_may_read_the_permission_value():
    """The permission value selects CLI SANDBOX FLAGS and nothing else.

    Pinned as a whitelist so a new reader has to be added here deliberately. `config_producer`
    defines the type; the two producer adapters map it to argv; `auth_gate` announces it;
    `selfcheck` prints it in its progress line.
    """
    allowed = {
        "src/syncade/config_producer.py",
        "src/syncade/adapters/producer_anthropic.py",
        "src/syncade/adapters/producer_openai.py",
        "src/syncade/cli/auth_gate.py",
        "src/syncade/selfcheck.py",
    }
    root = Path(__file__).resolve().parents[2]
    readers = {
        str(path.relative_to(root))
        for path in (root / "src" / "syncade").rglob("*.py")
        if "producer_config.permissions" in path.read_text(encoding="utf-8")
        or "config.producer.permissions" in path.read_text(encoding="utf-8")
    }
    assert readers <= allowed, f"new reader of producer.permissions: {sorted(readers - allowed)}"
