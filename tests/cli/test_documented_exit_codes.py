"""Documented exit codes match ``syncade.exit_codes`` — BOTH directions. PR-h-03 item 6.

`AGENTS.md` — the Codex operator contract — omitted exit 25 entirely. `budget_exceeded` is
a real terminal state with its own resume semantics, and the contract a Codex operator
reads had no row for it. The skills even claim their list is "mirrored in the repo's
`AGENTS.md` / `CLAUDE.md`", so the omission made that cross-reference false too.

Both directions matter and fail differently:

- **undocumented but reachable** — an operator scripting on `$?` sees a code no doc
  explains (the AGENTS.md bug);
- **documented but unreachable** — a doc promises a code that can never occur, so a script
  branches on a case that never fires.

Two enumeration FORMS are in use and both are checked: the markdown tables in
`AGENTS.md` / `CLAUDE.md` / the PRD appendix, and the bulleted list the four skill copies
render into chat. Checking only tables would have missed every skill copy.
"""

from __future__ import annotations

import re
from pathlib import Path

import syncade.exit_codes as exit_codes

_ROOT = Path(__file__).resolve().parents[2]

#: Files carrying an operator-facing exit-code enumeration, and the pattern that reads it.
#: `| 25 | ... |` for tables; "- `25` — ..." for the skills' chat list.
_TABLE = re.compile(r"^\|\s*(\d+)\s*\|")
_BULLET = re.compile(r"^-\s+`(\d+)`\s+—")

_SOURCES: dict[str, re.Pattern[str]] = {
    "AGENTS.md": _TABLE,
    "CLAUDE.md": _TABLE,
    "the design docs": _TABLE,
    "src/syncade/skills/claude/SKILL.md": _BULLET,
    "src/syncade/skills/codex/SKILL.md": _BULLET,
    ".claude/skills/syncade/SKILL.md": _BULLET,
    ".codex/skills/syncade/SKILL.md": _BULLET,
}

#: Codes deliberately absent from operator-facing enumerations, with the reason.
_NOT_DOCUMENTED = {
    exit_codes.CLI_USAGE_ERROR: (
        "argparse's own usage error for a malformed command; never a verdict, and omitted "
        "consistently from every table"
    ),
}


def _canonical() -> dict[int, str]:
    return {v: n for n, v in vars(exit_codes).items() if n.isupper() and isinstance(v, int)}


def _documented(rel: str, pattern: re.Pattern[str]) -> set[int]:
    return {
        int(m.group(1))
        for line in (_ROOT / rel).read_text().splitlines()
        if (m := pattern.match(line))
    }


def test_every_enumeration_is_complete_and_has_no_phantom_codes():
    canonical = _canonical()
    expected = set(canonical) - set(_NOT_DOCUMENTED)
    problems: list[str] = []

    checked = 0
    for rel, pattern in _SOURCES.items():
        # `tests/` SHIPS but AGENTS.md / CLAUDE.md / the PRD do not, so in the public snapshot
        # only the skill copies are present. Skip what is absent; the counter below keeps that
        # from silently emptying the check.
        if not (_ROOT / rel).exists():
            continue
        checked += 1
        documented = _documented(rel, pattern)
        assert documented, f"{rel}: no exit codes parsed — the file or the pattern moved"

        missing = expected - documented
        phantom = documented - set(canonical)
        if missing:
            problems.append(
                f"{rel}: MISSING {sorted(missing)} "
                f"({', '.join(canonical[c] for c in sorted(missing))}) — reachable, undocumented"
            )
        if phantom:
            problems.append(f"{rel}: PHANTOM {sorted(phantom)} — documented but unreachable")

    assert checked >= 4, (
        f"only {checked} exit-code enumeration(s) found — every source moved or was renamed"
    )
    assert not problems, "exit-code docs drifted from syncade.exit_codes:\n  " + "\n  ".join(
        problems
    )


def test_exclusions_name_real_codes():
    """A stale exclusion would grant a real code a permanent free pass."""
    canonical = set(_canonical())
    unknown = set(_NOT_DOCUMENTED) - canonical
    assert not unknown, f"_NOT_DOCUMENTED names non-existent exit code(s): {sorted(unknown)}"
