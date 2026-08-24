"""Surface enumeration and text scanning for the authority-claim drift guard.

Split out of ``tests/test_authority_claim_drift.py`` (PR-h-15 item 5). The seam is
**what counts as a surface and how it is read** versus **what we assert about it** — the tests
import these; nothing here asserts anything. Same shape as ``tests/persistence_sweep_helpers.py``.

The split was forced by measurement rather than taste: the test file sat at 499 of a 500
code-LOC cap and had been patched at that boundary for three review rounds. The PR-h-15 brief
ordered its items 4-then-5 on the assumption that item 4 would create this seam; item 4 turned
out to be ADDITIVE, so the order was wrong and this landed first.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

# --- Rule 1: item numbering must not reach text an OPERATOR reads. -------------------------
#
# The first attempt at this was a verb list (`checkpoint|will own|owns|...`) that caught 1 of the
# 4 spellings a blind panel found, and which a producer "fixed" by adding more verbs — the fifth
# enumeration in this repo beaten by the next spelling. The second attempt banned `Item <N>`
# outright, and that was wrong in the other direction: it flagged
# `"(PR-h-05 Item 2's live matrix)"` in a source comment, which is legitimate PROVENANCE — a
# developer citing which item established a fact.
#
# The distinction is not the phrasing and not the file type; it is the AUDIENCE. A developer
# reading CLAUDE.md knows what Item 2 is. An operator reading `loop-summary.md` does not, and
# every instance the panel found was an operator-facing one. So: in operator-facing documents,
# no item numbering at all; in source, none inside STRING LITERALS (which become operator
# output), while comments and docstrings stay free for provenance.
#
# The polarity is deliberate — a document is operator-facing unless it is declared otherwise,
# so a new doc gets the strict rule the day it is added rather than the day someone remembers.
_ITEM_NUMBERING = re.compile(r"\bItem [0-9]+\b")

_DEVELOPER_DOCS = frozenset(
    {
        "CLAUDE.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "docs/hardening-wave-plan-2026-07-27.md",
        "the design docs",
    }
)


def _operator_facing_docs() -> list[str]:
    return [p for p in _tracked_surfaces() if p.endswith(".md") and p not in _DEVELOPER_DOCS]


def _rendered_strings(rel: str) -> list[tuple[int, str]]:
    """String CONSTANTS in a module that are values rather than statements.

    A cheap tripwire, and deliberately nothing more. An earlier version grew a 60-line AST
    evaluator approximating f-strings, ``%``, ``.format()`` and ``+`` concatenation, because each
    review round named one more construction mechanism — and Python has no last one
    (``Template``, ``str.join``, building a list, ``textwrap``...). Deciding statically which
    strings reach an operator is undecidable in general, so any static answer is a list that the
    next spelling walks past. Five of this file's tests were testing that evaluator rather than
    the product.

    The guarantee lives instead in `test_operator_text_tables_carry_no_item_numbering` and
    `test_rendered_loop_summaries_carry_no_item_numbering` below, which read the actual runtime
    strings and the actual rendered artifacts. Those are exact, and they cover every construction
    mechanism at once because they look at the OUTPUT rather than at how it was built.
    """

    source = _read_tracked(rel)
    if source is None:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    documentation = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documentation
    ]


# --- Rule 2: BEST EFFORT, and labelled as such. ---------------------------------------------
#
# These are semantic claims, and a pattern set over natural language is incomplete by
# construction — there is no vocabulary trick that makes "the producer can move your refs" a
# closed set. They are kept because each one has actually shipped and a cheap tripwire beats
# none, but the docstring must not imply they close the class: a guard that OVERSTATES its
# coverage is how the last version passed while four false claims were live. Rule 1 is what
# mechanically catches most of this family; these catch the rest only when phrased as before.
_RETIRED_CLAIMS: dict[str, re.Pattern[str]] = {
    "candidate is not imported": re.compile(
        r"no (?:producer )?candidate(?: object| or ref)? is (?:not )?imported"
        r"|candidates? (?:remains?|stay) isolated"
        r"|not imported into the operator repository"
        r"|candidates? (?:preserved )?outside the operator repository"
    ),
    # Present tense only. "`yolo` USED TO BE the only supported producer policy" is a true
    # historical note in CLAUDE.md, and a drift guard that forces true statements to be deleted
    # is worse than no guard — it trains people to weaken the guard rather than fix the claim.
    "confined is the only producer policy": re.compile(
        r"`?confined`? is the only (?:supported|accepted)"
        r"|is the only (?:supported|accepted) producer (?:policy|permission)"
    ),
    "yolo restores operator-ref mutation": re.compile(
        r"yolo`?[^.\n]{0,80}(?:re-?opens?|restores?)[^.\n]{0,60}"
        r"(?:rank 3|ref mutation|direct (?:operator[- ])?ref)"
        r"|yolo`?[^.\n]{0,80}(?:removes?)[^.\n]{0,60}"
        r"(?:sandbox (?:and|or) (?:ref|operator)|ref.write enforcement)"
        r"|restoring pre-PR-h-05"
    ),
    # The producer runs UNSANDBOXED as a default claim: false since `confined` became the
    # default, and it shipped in the how-to-use default-roster table for exactly one round.
    "the default producer is unsandboxed": re.compile(
        r"(?:producer|Producer)[^.\n]{0,60}[Rr]uns unsandboxed"
    ),
    # The sandbox notice fires only where a producer can actually be dispatched. An unqualified
    # "every run" was true until the single-pass suppression landed and then sat false in four
    # places at once, including the docstring of the function that changed.
    "the yolo notice fires on every run": re.compile(
        r"(?:announced|disclosed|printed|warned)[^.\n]{0,40}on every run(?!\s+that can dispatch)"
        r"|[Ss]ay out loud, every run"
    ),
}

# Documents whose PURPOSE is to preserve a past state. Rewriting these would falsify the record.
_HISTORICAL = (
    "your repo",
    "docs/docs-archive/",
    "the dogfood history",
)
_HISTORICAL_DATED = re.compile(
    r"docs/[^/]*(?:audit|review|synthesis|wave-plan)[^/]*\d{4}-\d{2}-\d{2}"
)


def _tracked_surfaces() -> list[str]:
    """Every tracked text surface an operator or developer can read, derived from git.

    Falls back to a filesystem walk when git is unavailable (no .git directory, stripped
    workspace, or git not on PATH) so the drift assertions are exercisable in review
    environments that intentionally remove the .git directory. The fallback includes
    untracked files that git would exclude, but that is acceptable for a drift guard:
    false positives (untracked files with retired claims) are better than silent skips.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "*.md", "src/syncade/**/*.py", "src/syncade/*.py"],
            cwd=_REPO,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        paths = [p for p in out.split("\0") if p]
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        paths = sorted(str(p.relative_to(_REPO)) for p in _REPO.rglob("*.md")) + sorted(
            str(p.relative_to(_REPO)) for p in (_REPO / "src" / "syncade").rglob("*.py")
        )
    return [
        p
        for p in paths
        if not p.startswith(_HISTORICAL)
        and not _HISTORICAL_DATED.search(p)
        and p != "tests/test_authority_claim_drift.py"
    ]


def _read_tracked(rel: str) -> str | None:
    """Text of a tracked path, or ``None`` when it is legitimately absent.

    **A repo-context file can be TRACKED and ABSENT at the same time, and this scan runs in
    workspaces where that is normal.** Stated once, here, because it was patched three times as
    a special case before the rule was written down:

      1. actor workspaces      ``REVIEWER_STRIP_FILES`` removes CLAUDE.md / AGENTS.md from every
                               reviewer export and trusted test/check worktree
      2. the loop's test leg   same strip -- ``git ls-files`` lists the file, the disk does not
                               have it, and the read raised FileNotFoundError mid-suite, turning
                               a unanimous reviewer SHIP into exit 30
      3. the public snapshot   ``scripts/oss-stage.sh``'s allowlist excludes dev-only docs

    Two of the three readers were previously safe only by COINCIDENCE: ``_DEVELOPER_DOCS`` and
    ``REVIEWER_STRIP_FILES`` happen to name the same two files. ``strip_repo_context_files`` is
    operator-configurable, so that agreement is a coincidence rather than an invariant -- adding
    any other ``.md`` to it reproduces the same crash at a different site.

    ``try``/``except`` rather than ``.exists()``: one syscall instead of two, and no window
    between the check and the read.
    """
    try:
        return (_REPO / rel).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None


def _flatten(text: str) -> str:
    """Collapse whitespace so a claim split across two lines still matches.

    Line-based matching let `"announced on every run "` / `"that can dispatch a producer"` read
    as an unqualified claim in one file and a qualified one in another, purely by where the
    author wrapped. Prose does not respect line boundaries, so neither can the guard.
    """
    return " ".join(text.split())


def _hits(pattern) -> list[str]:
    """Search what a reader actually sees.

    For Python that means the rendered string VALUES, not the source: matching source text made
    an implicit-concatenation boundary (`"...every run " "that can dispatch..."`) look like an
    unqualified claim. For Markdown it means whitespace-flattened prose, since a claim wrapped
    across two lines is still one sentence.
    """
    found: list[str] = []
    for rel in _tracked_surfaces():
        if rel.endswith(".py"):
            haystacks = [(f"{rel}:{n}", _flatten(v)) for n, v in _rendered_strings(rel)]
        else:
            text = _read_tracked(rel)
            if text is None:
                continue
            haystacks = [(rel, _flatten(text))]
        for label, flat in haystacks:
            for match in pattern.finditer(flat):
                start = max(0, match.start() - 50)
                found.append(f"  {label}: ...{flat[start : match.end() + 50]}...")
    return found
