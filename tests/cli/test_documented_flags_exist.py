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
    # The pre-0.6.3 upgrade recipes in README's Updating section. Those releases have no
    # update check and no `--update`, so the only way out is the operator's own package
    # manager — which means foreign flags in a syncade doc, by necessity.
    "--force": "uv tool install --force (reinstall over an existing tool install)",
    # NOT `--upgrade`: the README uses `-U` instead, deliberately. `--upgrade` sits one
    # letter from syncade's real `--update`, so allowlisting it forever would let a future
    # "syncade --upgrade" ship a command that exits 2.
    "--user": "pip install --user (macOS user-site installs)",
    "--dangerously-bypass-approvals-and-sandbox": "codex",
    "--git": "git diff --git (the diff header)",
    "--hard": "git reset --hard",
    "--into": "scripts/oss-release.sh",
    "--is-ancestor": "git merge-base",
    # CLAUDE.md's pre-push-hook paragraph warns that a slow hook trains you to reach for
    # this. It is git's, and naming it is the point of the sentence.
    "--no-verify": "git push --no-verify (skipping the pre-push hook)",
    "--show-toplevel": 'git rev-parse (PR-h-15 records why `cd "$(...)"` cannot fail closed)',
    "--no-ff": "git merge",
    "--numstat": "git diff --numstat (the binary-report oracle PR-h-field-01 rejected)",
    "--no-replace-objects": "git",
    "--pretty": "git log",
    "--resolution": "uv pip install --resolution lowest-direct (the PR-h-10 floor leg)",
    "--safe-mode": "claude",
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


def test_every_changelog_release_has_a_link_ref() -> None:
    """A release heading with no `[x.y.z]:` ref renders as literal brackets on the public repo.

    Not hypothetical: 0.6.3 shipped with no link ref at all AND left `[Unreleased]` comparing
    from v0.6.2, so the compare link skipped a whole release. Both were found by hand while
    cutting 0.7.0 — nothing failed, because nothing was reading this file. Same class as the
    documented-flag drift above: prose that no test reads is prose that drifts.
    """
    import re

    text = (Path(__file__).resolve().parents[2] / "CHANGELOG.md").read_text()
    headings = re.findall(r"^## \[([^\]]+)\]", text, re.M)
    refs = set(re.findall(r"^\[([^\]]+)\]:", text, re.M))

    assert [h for h in headings if headings.count(h) > 1] == [], "duplicate release heading"
    assert [h for h in headings if h not in refs] == [], (
        "every CHANGELOG release heading needs a matching [x.y.z]: link ref at the bottom"
    )
    # Assert the SHAPE before indexing into it. Without this, `headings[1]` silently means
    # "second heading" instead of "newest release": renaming the Unreleased heading, or dropping
    # it at release time, both produced a confident failure naming the SUPERSEDED tag as the
    # newest release — a message that directs the maintainer to the wrong fix. Found by an
    # adversarial review of this very test.
    assert headings[0] == "Unreleased", (
        f"the first CHANGELOG heading must be `## [Unreleased]`, not {headings[0]!r} — the "
        "compare-link check below identifies the newest release by position"
    )
    compare = re.search(r"^\[Unreleased\]: .*compare/v([\d.]+)\.\.\.HEAD", text, re.M)
    assert compare and compare.group(1) == headings[1], (
        f"[Unreleased] must compare from the newest release ({headings[1]}), not "
        f"{compare.group(1) if compare else 'nothing'} — otherwise the link skips a release"
    )
