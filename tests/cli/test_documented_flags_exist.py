"""Every syncade flag named in operator-facing docs must exist — PR-h-03 items 4 & 5.

Two of this PR's eight items were the same defect: a documented flag the parser has never
had (`--audit`, which is `--spec-audit`; `--offline`, which is `--quick`). Reading the docs
found two. Enumerating them against ``build_parser()`` found FOUR — the extra two were
`CLAUDE.md`'s own module map and the `--verbose` in all four skill copies' unsupported-flag
list. That is the argument for a mechanical check: prose review misses instances, and an
operator who types a documented flag gets `unrecognized arguments`.

Scope note: the docs describe MANY tools, so most `--flags` in them belong to git, uv,
ruff, codex or the release script. Those are allowlisted BY NAME below with their owner —
an allowlist of other people's flags is stable (git's do not churn) and each entry is a
statement a reader can check.
"""

from __future__ import annotations

import re
from pathlib import Path

from syncade.cli.parser import build_parser

_ROOT = Path(__file__).resolve().parents[2]

#: Docs that describe syncade's CURRENT behaviour. your repo and audit docs are
#: deliberately OUT — they record past claims on purpose, and `CHANGELOG.md` may name a
#: flag that has since been removed.
_DOCS = [
    "README.md",
    "CLAUDE.md",
    "AGENTS.md",
    "docs/config-reference.md",
    "src/syncade/skills/claude/SKILL.md",
    "src/syncade/skills/codex/SKILL.md",
    ".claude/skills/syncade/SKILL.md",
    ".codex/skills/syncade/SKILL.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
]

#: Flags belonging to OTHER tools the docs quote. Each names its owner so a future reader
#: can confirm the exemption rather than trust it.
_FOREIGN = {
    "--check": "ruff format --check / git diff --check",
    "--dangerously-bypass-approvals-and-sandbox": "codex",
    "--git": "git diff --git (the diff header)",
    "--hard": "git reset --hard",
    "--into": "scripts/oss-release.sh",
    "--is-ancestor": "git merge-base",
    "--no-ff": "git merge",
    "--numstat": "git diff --numstat (the binary-report oracle PR-h-field-01 rejected)",
    "--no-replace-objects": "git",
    "--pretty": "git log",
    "--resolution": "uv pip install --resolution lowest-direct (the PR-h-10 floor leg)",
    "--text": "git diff --text (forces binary content out as text)",
    "--seed": "uv venv",
    "--verify": "git rev-parse --verify",
}

# A flag token. The trailing `[a-z0-9*]` lets `--reviewer-*` (a documented family, written
# with a glob in prose) match as one token instead of a truncated `--reviewer-`.
_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]{2,}[a-z0-9*]")


def _parser_flags() -> set[str]:
    flags: set[str] = set()
    for action in build_parser()._actions:
        flags.update(action.option_strings)
    return flags


def test_every_documented_syncade_flag_exists_in_the_parser():
    real = _parser_flags()
    #: Flag FAMILIES the docs write with a glob, e.g. "the `--reviewer-*` flags".
    families = {"--reviewer-*"}
    offenders: list[str] = []

    for rel in _DOCS:
        path = _ROOT / rel
        if not path.exists():  # SECURITY.md etc. are optional in a scrubbed snapshot
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for flag in _FLAG.findall(line):
                if flag in real or flag in _FOREIGN:
                    continue
                if flag in families:
                    continue
                offenders.append(f"{rel}:{lineno}: {flag}  in: {line.strip()[:88]}")

    assert not offenders, (
        "operator-facing docs name syncade flags the parser does not define — an operator "
        "who types one gets 'unrecognized arguments':\n  "
        + "\n  ".join(offenders)
        + "\n\nIf the flag belongs to another tool (git, uv, ruff, codex), add it to _FOREIGN "
        "with its owner."
    )


def test_the_check_is_not_vacuous():
    """A guard that never sees a flag would pass forever."""
    seen = sum(
        len(_FLAG.findall(line))
        for rel in _DOCS
        if (_ROOT / rel).exists()
        for line in (_ROOT / rel).read_text().splitlines()
    )
    assert seen > 50, f"the scanner found only {seen} flag tokens — the regex or _DOCS broke"
