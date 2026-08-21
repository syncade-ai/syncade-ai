"""PR-h-12 item 1b — no test may write into the SHARED worktree base.

The isolation fixture lived in ``tests/orchestrator/conftest.py`` and was package-scoped, so six
other packages ran against the real ``/tmp/syncade``. Measured before the move, with the
directory removed first: ``tests/cli`` and ``tests/persistence`` each recreated it. Both were
EMPTY — no worktrees leaked — which is why the brief's original 4.4 GB attribution was withdrawn.
The gap was real, the magnitude was not, and this file exists so the gap cannot reopen silently.

Asserting "the suite left nothing behind" from inside the suite is circular, so these check the
mechanism instead: the redirect is active for THIS test, and it is installed where every package
inherits it rather than where one does.
"""

from __future__ import annotations

import ast
from pathlib import Path

from syncade.config import SyncadeConfig
from syncade.worktree import DEFAULT_WORKTREE_BASE

_ROOT = Path(__file__).resolve().parent


def test_a_bare_config_never_resolves_to_the_shared_base(tmp_path: Path) -> None:
    """The fixture patches the default factory's source, because `run_review` falls back to
    `config.worktree_base` (PR-v2-9). Patching only an explicit argument would leave a bare
    `SyncadeConfig()` pointing at the shared base — which is how six packages kept reaching it.
    """
    assert SyncadeConfig().worktree_base != DEFAULT_WORKTREE_BASE
    assert not str(SyncadeConfig().worktree_base).startswith(str(DEFAULT_WORKTREE_BASE))


def test_the_isolation_fixture_is_installed_repo_wide_not_per_package() -> None:
    """Position is the fix. The fixture was correct and scoped to one package for months; what
    failed was WHERE it lived, so that is what this pins — parsed, not grepped, so a mention in a
    docstring or comment cannot satisfy it.
    """
    root_conftest = ast.parse((_ROOT / "conftest.py").read_text())
    defined_here = {n.name for n in ast.walk(root_conftest) if isinstance(n, ast.FunctionDef)}
    assert "_isolated_worktree_base" in defined_here, (
        "the worktree isolation fixture must live in tests/conftest.py so EVERY package inherits "
        "it. Package scope is the defect this item exists to close."
    )

    package_conftests = [p for p in _ROOT.rglob("conftest.py") if p != _ROOT / "conftest.py"]
    offenders = [
        p.relative_to(_ROOT)
        for p in package_conftests
        if any(
            isinstance(n, ast.FunctionDef) and n.name == "_isolated_worktree_base"
            for n in ast.walk(ast.parse(p.read_text()))
        )
    ]
    assert not offenders, (
        f"a package-scoped copy is back in {offenders}. Two definitions means one of them wins "
        "silently, and the losing packages are exactly the ones that leaked before."
    )
