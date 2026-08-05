"""Every git invocation must route through :func:`syncade.process.run_subprocess`.

``run_subprocess`` forces ``GIT_NO_REPLACE_OBJECTS=1`` on git children, because
``refs/replace/*`` is producer-writable and substitutes objects on read (see
:func:`syncade.process._git_hardened_env` for the three reproduced attacks).

This is a DRIFT test, and it exists because pinning the flag per call site
failed three dogfood rounds running: each round the blind panel found the next
unflagged invocation, and two were still open when the choke point landed. A
call that bypasses ``run_subprocess`` gets no protection, so the bypass must
fail CI rather than wait for a reviewer to notice.

Two pre-existing bypasses are allowed, and ONLY for subcommands that provably
cannot be poisoned: ``rev-parse`` and ``symbolic-ref`` resolve a ref NAME to a
SHA without reading the object, which was measured, not assumed. Adding a
commit-READING subcommand (``log``, ``merge-base``, ``reset``, ``diff``, …) at
these sites fails this test.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "syncade"

# module (relative to src/syncade) -> subcommands allowed to bypass.
_ALLOWED_BYPASSES = {
    # Resolve a ref NAME to a SHA without reading the object, so a replacement
    # cannot substitute anything. Measured, not assumed.
    "doctor.py": {"rev-parse"},
    "orchestrator/branch_guard.py": {"symbolic-ref"},
    # A test double. It deliberately stays out of the production subprocess
    # machinery (see its own comment) and only ever runs against a fixture
    # worktree in tests — there is no untrusted producer to plant a
    # replacement ref. `commit` DOES read objects, so this entry is justified
    # by the absence of an adversary, not by the subcommand.
    "adapters/fake_producer_audit_draft.py": {"add", "commit"},
}


def _git_argv_literals(tree: ast.AST) -> list[list[str]]:
    """Every ``subprocess.run/Popen/...([...])`` argv list that starts with git."""
    found: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        if func.value.id != "subprocess" or func.attr not in {
            "run",
            "Popen",
            "call",
            "check_output",
            "check_call",
        }:
            continue
        if not (node.args and isinstance(node.args[0], ast.List)):
            continue
        # Keep POSITION: a non-literal element (``str(repo_root)``) becomes a
        # placeholder rather than vanishing, else a preceding ``-C`` would
        # consume the real subcommand and the check would read the wrong token.
        parts = [
            str(e.value) if isinstance(e, ast.Constant) else "<dyn>" for e in node.args[0].elts
        ]
        if parts and Path(parts[0]).name == "git":
            found.append(parts)
    return found


# Global flags git accepts BEFORE the subcommand that consume the next token.
_OPERAND_FLAGS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def _subcommand(argv: list[str]) -> str:
    """The git subcommand in ``argv``, skipping global flags and their operands."""
    rest = iter(argv[1:])
    for tok in rest:
        if tok in _OPERAND_FLAGS:
            next(rest, None)  # consume its operand
        elif not tok.startswith("-"):
            return tok
    return "?"


def test_no_git_invocation_bypasses_the_choke_point():
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "process.py":
            continue  # the choke point itself
        rel = path.relative_to(SRC).as_posix()
        for argv in _git_argv_literals(ast.parse(path.read_text())):
            sub = _subcommand(argv)
            allowed = _ALLOWED_BYPASSES.get(rel, set())
            if sub not in allowed:
                violations.append(f"{rel}: subprocess.* running `git {sub}`")

    assert not violations, (
        "git invoked outside syncade.process.run_subprocess, so "
        "GIT_NO_REPLACE_OBJECTS=1 is NOT applied:\n  "
        + "\n  ".join(violations)
        + "\n\nRoute it through run_subprocess. If the subcommand provably reads no "
        "commit object, add it to _ALLOWED_BYPASSES with that justification."
    )
