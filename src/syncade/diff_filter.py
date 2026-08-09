"""Reviewer-facing diff filtering.

Reviewer worktrees have ``CLAUDE.md`` / ``AGENTS.md`` removed (the
architectural invariant that reviewers must not see project memory).
But ``git diff <base>..HEAD`` — captured in the operator's repo, where
those files still exist — contains hunks for them. A reviewer whose
worktree lacks ``CLAUDE.md`` but whose diff shows it changed will flag
a "tracked deletion" / missing-file finding. That false positive
persisted across all three rounds of the validation and drove the
loop to exit 20.

:func:`filter_diff_for_reviewer` removes those hunks at the orchestrator
boundary, before the diff reaches ``render_reviewer_prompt``. The
worktree strip is unchanged; the diff strip complements it so the file
is invisible across BOTH surfaces.

:data:`REVIEWER_STRIP_FILES` is the single source of truth for which
files are stripped: it feeds the default of
:attr:`syncade.config.ReviewConfig.strip_repo_context_files`, which the
orchestrator passes to BOTH the worktree-create call AND this filter, so a
customized ``strip_repo_context_files`` reaches both.

The two surfaces share the LIST but not the MATCHING, and that gap is real:
this filter compares BASENAMES, while ``worktree_paths._strip_files`` REFUSES
any entry containing ``/`` as a path-escape guard. So ``docs/CLAUDE.md``
strips the diff hunk here and leaves the file readable in the worktree — a
leak through the surface the other one covers. Bare basenames are the only
shape both handle identically. (An earlier version of this docstring claimed
the two "can never diverge"; that was false — corrected in PR-h-03.)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

_log = logging.getLogger(__name__)

REVIEWER_STRIP_FILES: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")
"""Files removed from reviewer worktrees AND from the reviewer-facing
diff. Single source of truth: the default of
:attr:`syncade.config.ReviewConfig.strip_repo_context_files` is
``list(REVIEWER_STRIP_FILES)``, and the orchestrator passes that config
value to both the worktree strip and :func:`filter_diff_for_reviewer`."""

# The canonical per-file boundary in a unified diff: git emits a
# ``diff --git a/<path> b/<path>`` line for every file — including binary
# files and pure renames/deletions, which carry no ``@@`` chunk markers.
#
# Each side is matched INDEPENDENTLY as either a C-quoted or a bare path,
# because git quotes only the side that needs it — a rename of `plain.md` to
# `naïve.md` emits `diff --git a/plain.md "b/na\303\257ve.md"` (measured). A
# both-quoted-or-both-bare pair of patterns misses that header entirely, and a
# missed header is a repo-context file shown to a blind reviewer.
#
# The quoted alternative is tried FIRST so a genuine `"` in a bare path can
# never be mistaken for an opening quote. Greedy ``.+`` on a bare a-path
# backtracks to the last `` b/`` so `a/my notes.md b/my notes.md` (git does not
# quote spaces) still parses, exactly as before.
_QUOTED = r'"(?:[^"\\]|\\.)*"'
_DIFF_GIT_HEADER = re.compile(rf"^diff --git (?P<qa>{_QUOTED}|a/.+) (?P<qb>{_QUOTED}|b/.+)$")

# Git's C-style escapes. Anything else after a backslash is an octal byte.
_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "f": 0x0C,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "v": 0x0B,
    "\\": 0x5C,
    '"': 0x22,
}


def _unquote_path(token: str) -> str:
    """Decode one ``diff --git`` path token to its real filesystem name.

    A bare token is returned as-is. A C-quoted token (git quotes a path
    containing a non-ASCII byte, a quote, a backslash, or a control character —
    NOT a plain space) is unescaped.

    Octal escapes are BYTES, not code points: `naïve.md` is written
    `na\\303\\257ve.md` because `ï` is UTF-8 `\\xc3\\xaf`. So escapes are
    accumulated into a bytearray and decoded once at the end. Invalid UTF-8
    raises ``UnicodeDecodeError`` (a ``ValueError`` subclass), which the caller
    catches and maps to a fail-closed drop — an invalid-byte path is treated the
    same as an unparseable header rather than silently mismatching any name.
    """
    if not token.startswith('"'):
        return token
    out = bytearray()
    body = token[1:-1]
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.extend(ch.encode("utf-8"))
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in _C_ESCAPES:
            out.append(_C_ESCAPES[nxt])
            i += 2
        else:
            # \nnn octal — git always emits exactly three [0-7] digits; fewer
            # means a truncated/malformed header, which must fail closed.
            digits = body[i + 1 : i + 4]
            if len(digits) != 3 or not all(c in "01234567" for c in digits):
                raise ValueError(f"malformed octal escape at position {i}")
            out.append(int(digits, 8))
            i += 4
    return out.decode("utf-8")


def _split_sections(diff_text: str) -> list[list[str]]:
    """Split a unified diff into per-file sections on ``diff --git`` boundaries.

    Line endings are kept so retained sections reassemble byte-for-byte. Content before
    the first ``diff --git`` (anomalous for git diff, but handled defensively) becomes a
    leading section with no header, which callers always keep — it is the PREAMBLE, not a
    file, and conflating the two would delete legitimate leading text.

    Splits on ``\\n`` only, not Python's universal newlines. ``splitlines()`` treats ``\\r``
    as a line separator, so a binary payload containing ``\\r`` immediately followed by
    ``diff --git …`` would be split into a fake section boundary — either leaking binary
    bytes that follow a syntactically valid fake header, or triggering a spurious
    ``diff_malformed`` refusal when the fake header is malformed. Splitting on ``\\n`` alone
    keeps the ``\\r`` inside its containing line.
    """
    sections: list[list[str]] = []
    current: list[str] = []
    raw_parts = diff_text.split("\n")
    # Reconstruct lines with their \n endings; last fragment has none.
    lines: list[str] = [p + "\n" for p in raw_parts[:-1]]
    if raw_parts[-1]:  # non-empty trailing fragment (diff not ending with \n)
        lines.append(raw_parts[-1])
    for line in lines:
        if line.startswith("diff --git ") and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)
    return sections


def elide_binary_hunks(diff_text: str) -> tuple[str, list[str]]:
    """Replace binary file content with a one-line notice. Returns ``(diff, elided_paths)``.

    ``snapshot`` renders the diff with ``--text`` — the only lever against attribute-driven
    suppression, since a committed ``.gitattributes`` marked ``-diff`` otherwise collapses a
    real source change to "Binary files ... differ". The cost is that a genuine binary is
    emitted as raw text: measured on the reported repo, 12 committed PNG baselines turned
    65,961 B of real diff into 3,129,026 B, which no reviewer can read and which displaces
    the diff it is meant to judge.

    **Detection is by CONTENT, never by git's own report, and that is the whole design.**
    ``git diff --numstat`` looks like the right oracle and is not: measured, it reports
    ``-\t-`` for a plain text file an attacker marked ``*.py -diff``, exactly as it does for
    a real PNG, and ``--text`` does not change its answer. Filtering on it would let a
    committed ``.gitattributes`` erase source changes from the reviewer's diff — reopening
    the hole ``--text`` exists to close.

    So this applies git's OWN heuristic to the bytes instead: a NUL byte means binary.
    That cannot be forged by an attributes file. U+FFFD replacement characters are
    deliberately NOT a signal — a Latin-1-encoded source file is full of them, and dropping
    real source from review is far worse than passing some noise through.

    Both sides are covered by construction: a DELETED binary inflates identically (its
    content is emitted in full as removed lines), so a PR that merely drops a vendored
    asset is as exposed as one that adds it. Nothing here keys on added paths.

    The header lines are preserved so the reviewer still sees that the path changed, and
    the notice names the size withheld — the omission is disclosed, never silent.
    """
    if not diff_text or "\0" not in diff_text:
        return diff_text, []

    kept: list[str] = []
    elided: list[str] = []
    for section in _split_sections(diff_text):
        body_start = _binary_body_start(section)
        if body_start is None:
            kept.extend(section)
            continue
        withheld = sum(len(line.encode("utf-8", errors="replace")) for line in section[body_start:])
        paths, _ = _decode_header(section[0])
        path = paths[1] if paths else "<unparseable path>"
        elided.append(path)
        kept.extend(section[:body_start])
        kept.append(f"[syncade: binary file content omitted — {withheld} bytes not shown]\n")
    # Total contract: no NUL leaves this function. Section-based elision covers every
    # `diff --git` file, but a headerless PREAMBLE is kept by design (it is not a file),
    # so a stray NUL there would otherwise reach a provider that has no reason to accept
    # one. Sectioned binary content is already gone; this only sweeps the remainder.
    return "".join(kept).replace("\x00", ""), elided


def _binary_body_start(section: list[str]) -> int | None:
    """Index of the first content line of a binary section, or ``None`` if it is not binary.

    Only a section with a ``diff --git`` header can be elided; a headerless leading section
    is the preamble. The split point is the first hunk header (``@@``) when there is one, so
    the file-mode/index metadata a reviewer may want survives; otherwise it is the line after
    the header, which covers git's own ``Binary files ... differ`` form.
    """
    if not section or not section[0].startswith("diff --git "):
        return None
    if not any("\0" in line for line in section):
        return None
    for i, line in enumerate(section):
        if line.startswith("@@"):
            return i + 1
    return 1


def filter_diff_for_reviewer(diff_text: str, strip_files: Iterable[str]) -> str:
    """Remove the diff hunks of ``strip_files`` from a unified diff.

    Used by the orchestrator before calling ``render_reviewer_prompt`` so
    reviewers don't see edits to files that are stripped from their
    worktrees. ``snapshot.diff_text`` itself is never modified — callers
    rebind the filtered result locally.

    Matching is by basename on EITHER side of the ``diff --git`` header,
    so ``CLAUDE.md``, ``docs/CLAUDE.md``, and a tracked deletion of
    ``CLAUDE.md`` (which git still writes as ``a/CLAUDE.md b/CLAUDE.md``)
    are all recognized. A ``diff --git`` line whose identity cannot be
    established (unparseable header or malformed path encoding) is
    DROPPED — fail-closed, with a WARNING logged. Over-stripping costs a
    reviewer some context and is visible to them; under-stripping is an
    invisible leak of the property the review is sold on.

    Edge cases (per the design):

    - empty ``diff_text`` → ``""``
    - no hunk matches → original text returned byte-for-byte
    - every hunk matches → ``""``

    Args:
        diff_text: A unified diff string (``git diff <base>..HEAD``).
        strip_files: File names whose hunks to remove. Entries are
            compared by basename, so bare ``"CLAUDE.md"`` matches a
            nested ``docs/CLAUDE.md`` hunk.

    Returns:
        The diff with the matching files' hunks removed; retained hunks
        are preserved byte-for-byte and in their original order.
    """
    if not diff_text:
        return diff_text

    strip_basenames = {name.rsplit("/", 1)[-1] for name in strip_files}
    if not strip_basenames:
        return diff_text

    kept: list[str] = []
    for section in _split_sections(diff_text):
        redacted = _redacted_boundary_rename(section[0], strip_basenames)
        if redacted is not None:
            kept.append(redacted)
            continue
        if _section_targets_stripped_file(section[0], strip_basenames):
            continue
        kept.extend(section)
    return "".join(kept)


def _redacted_boundary_rename(header_line: str, strip_basenames: set[str]) -> str | None:
    """A placeholder section for a rename OUT of a strip target, or ``None``.

    ``git mv CLAUDE.md app.py`` moves content from a path reviewers must not see to one
    they must. Dropping the section conceals that ``app.py`` appeared (PR-h-02c.5);
    keeping it leaks project memory three ways -- the a-path in the header, the
    ``rename from`` line, and, on a rename WITH edits, the stripped file's own lines as
    hunk CONTEXT. Measured: a one-line edit yields ``@@ ... @@ memory line`` plus
    ``memory line`` context rows.

    So the body is not redacted, it is DISCARDED, and the destination is announced
    instead. The file is present in the reviewer's worktree (the strip matches basenames,
    and the destination is not one), so pointing at it costs the reviewer nothing and
    leaks nothing. That reviewers cannot see repo-context files is already stated in
    their prompt, so naming the fact of a rename discloses nothing new -- only the source
    NAME and CONTENT must not survive, and neither does.
    """
    paths, _ = _decode_header(header_line.rstrip("\n"))
    if paths is None:
        return None
    a_path, b_path = paths
    a_base = a_path.rsplit("/", 1)[-1]
    b_base = b_path.rsplit("/", 1)[-1]
    if a_base not in strip_basenames or b_base in strip_basenames:
        return None
    return (
        f"diff --git a/(stripped repo-context file) b/{b_path}\n"
        f"rename to {b_path}\n"
        f"(body withheld: renamed out of a stripped repo-context file. "
        f"Review {b_path} directly in your worktree.)\n"
    )


def _decode_header(header_line: str) -> tuple[tuple[str, str] | None, str]:
    """``((a_path, b_path), "")``, or ``(None, reason)`` when the header's
    identity cannot be established.

    Returns FULL paths with the ``a/``/``b/`` prefix stripped, so a rename
    ``CLAUDE.md -> src/app.py`` yields ``("CLAUDE.md", "src/app.py")``.
    Callers that need basenames compute ``path.rsplit("/", 1)[-1]`` themselves.

    This is the SINGLE definition of "identifiable", shared by the filter (which DROPS
    such a section) and by :func:`unidentifiable_sections` (which reports it). Two
    separate notions would eventually disagree, and the disagreement would be a leak on
    one side and a spurious refusal on the other.

    ``reason`` exists so the warning can say *why* rather than only *that* — the three
    causes are diagnosed differently by an operator.
    """
    match = _DIFF_GIT_HEADER.match(header_line.rstrip("\n"))
    if match is None:
        return None, "unidentifiable header"
    try:
        a = _unquote_path(match.group("qa"))
        b = _unquote_path(match.group("qb"))
    except ValueError:
        # Git's own quoting never emits a malformed escape or invalid UTF-8, but a
        # TRUNCATED diff can (increment E caps diff size).
        return None, "malformed path encoding"
    # Git's trusted prefixes. A bare token gets these from the regex; a quoted token is
    # any syntactically valid quoted string, so `"x/app.py"` must be caught here.
    if not a.startswith("a/"):
        return None, "untrusted a-side prefix"
    if not b.startswith("b/"):
        return None, "untrusted b-side prefix"
    # A bare a-side containing " b/" means the greedy regex consumed part of the b-side
    # path — the header has multiple valid " b/" split points and the a/b boundary is
    # indeterminate. Example: `diff --git a/CLAUDE.md b/foo b/bar.py` is produced by
    # `git mv CLAUDE.md 'foo b/bar.py'`; the regex yields a='CLAUDE.md b/foo' / b='bar.py'
    # instead of a='CLAUDE.md' / b='foo b/bar.py', so the strip check uses the wrong
    # basename and the source content leaks. Fail closed: treat as unidentifiable.
    qa_raw = match.group("qa")
    if not qa_raw.startswith('"') and " b/" in qa_raw:
        return None, "ambiguous bare header (multiple ` b/` separators)"
    return (a[2:], b[2:]), ""


def unidentifiable_sections(diff_text: str) -> list[str]:
    """The ``diff --git`` header lines whose identity cannot be established.

    These are exactly the sections :func:`filter_diff_for_reviewer` drops fail-closed, so
    a caller can tell "the diff filtered to nothing because there was nothing to review"
    from "…because we could not read it" — the difference between an honest no-change
    result and shipping a change BECAUSE it was unreadable (PR-h-02d D2).

    Returns the header lines themselves so the refusal can name what it could not read.
    """
    # split('\n') rather than splitlines() — see _split_sections for why.
    return [
        line
        for line in diff_text.split("\n")
        if line.startswith("diff --git ") and _decode_header(line)[0] is None
    ]


def concealed_destinations(diff_text: str, strip_files: Iterable[str]) -> list[str]:
    """Destination paths that filtering hid even though a reviewer should see them.

    A section is dropped when EITHER side's basename is a strip target. That is right when
    the DESTINATION is a strip target — the content lands somewhere a reviewer must not
    look. It is wrong when only the SOURCE was: ``git mv CLAUDE.md app.py`` drops the
    section, so the reviewer is never told ``app.py`` came into existence.

    Alone that is a blind spot (the file is still in the reviewer's worktree, and a rename
    with a large rewrite falls below git's similarity threshold and is emitted as
    delete+add, which survives). It becomes a FALSE SHIP in composition with PR-h-02d: if
    such a rename is the only change, the filtered diff is empty, the run terminates
    ``no_changes_to_review`` at exit 0, and zero reviewers are dispatched — so nothing
    reads that worktree, because nothing runs.

    Callers use a non-empty result to refuse the known-empty conclusion. Note this is
    deliberately NOT "the unfiltered diff was non-empty": that would revert PR-h-02d's D3,
    where a legitimately all-repo-context change SHOULD be known-empty. The question is
    whether a drop was justified by its destination, not whether anything was dropped.
    """
    strip_basenames = {name.rsplit("/", 1)[-1] for name in strip_files}
    if not strip_basenames:
        return []
    concealed: list[str] = []
    for line in diff_text.split("\n"):  # split('\n') — see _split_sections for why
        if not line.startswith("diff --git "):
            continue
        paths, _ = _decode_header(line)
        if paths is None:
            continue  # unidentifiable — a separate refusal path
        a_path, b_path = paths
        a_base = a_path.rsplit("/", 1)[-1]
        b_base = b_path.rsplit("/", 1)[-1]
        if a_base in strip_basenames and b_base not in strip_basenames:
            concealed.append(b_path)
    return concealed


def _section_targets_stripped_file(header_line: str, strip_basenames: set[str]) -> bool:
    """True iff this section must be dropped from the reviewer's diff.

    Two distinct "we cannot read this" cases, which must NOT be conflated:

    - The section does not begin with ``diff --git`` at all. That is the
      defensive preamble section, not a file — KEEP it, or filtering would
      delete legitimate leading text.
    - The section IS a ``diff --git`` header but cannot be parsed or decoded.
      That is a real file section whose identity is unknown, so it could be a
      strip target — DROP it (D1(a), PR-h-02c).

    The second rule REVERSES a previous deliberate choice to keep unparseable
    headers, justified then by "under-stripping leaves the reviewer-template
    'Stripped files' note as a backstop, whereas over-stripping could drop real
    code from review". That trade is wrong for this failure: over-stripping
    costs a reviewer some context and is visible to them, while under-stripping
    is an INVISIBLE leak of the blindness property the review is sold on. Note
    the caller returns early when no strip targets are configured, so this can
    never drop anything in a run that does no stripping.
    """
    line = header_line.rstrip("\n")
    if not line.startswith("diff --git "):
        return False
    paths, reason = _decode_header(line)
    if paths is None:
        _log.warning("diff_filter: dropping section (%s, fail-closed): %r", reason, line)
        return True
    a_path, b_path = paths
    return (
        a_path.rsplit("/", 1)[-1] in strip_basenames or b_path.rsplit("/", 1)[-1] in strip_basenames
    )
