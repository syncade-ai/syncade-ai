"""Markdown-rendering helpers shared across persistence write paths.

These leaves of the dependency graph carry only narrow domain imports —
``syncade.dispatcher`` (``ReviewerRunResult`` for :func:`_reviewer_file_links`,
``DispatchResult`` for :func:`_consensus_lines`) and
``syncade.synthesis.ConsolidatedFinding`` (for :func:`_consensus_lines`).
Higher-level writers (``findings_md``, ``run_summary``) compose these into
per-document layout.
"""

from __future__ import annotations

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.synthesis import ConsolidatedFinding, distinct_reviewer_count


def _md_inline_code(text: str) -> str:
    """Render ``text`` as a markdown inline-code span.

    Handles content that itself contains backticks.

    The CommonMark inline-code-span rule: wrap the content with
    a run of backticks one longer than any run of backticks in
    the content. If the content begins or ends with a backtick
    (or any whitespace at the edges of the content matters), pad
    with a single space on each side.

    Examples:
    - ``hello`` → ``\\`hello\\```
    - ``pytest --basetemp=\\`mktemp -d\\``` → ``\\`\\`pytest
      --basetemp=\\`mktemp -d\\`\\`\\``` (double-backtick fence
      surrounds the single-backtick content; outer spaces would
      be needed only if content started/ended with a backtick).
    - ``\\`leading-backtick`` → ``\\`\\` \\`leading-backtick \\`\\``

    Why this helper exists: the operator's free-form
    ``test_command`` can contain backticks for command
    substitution (``$()`` is the modern form but backticks still
    appear in legacy or terse configs). Rendering it with a
    single-backtick markdown span would close the code span
    inside the command, producing broken markdown that renders
    half the operator's command as plain text.

    : this helper is for SINGLE-LINE content only. Multi-
    line content (operator's TOML triple-quoted commands) must
    use :func:`_md_command_field` instead, which switches to a
    fenced code block when newlines are present. Calling
    ``_md_inline_code`` with newline content produces broken
    markdown (the inline span doesn't allow newlines).
    """
    if not text:
        # Empty span. Use a minimal valid form rather than emit
        # ``"`"`` (which is an unclosed backtick).
        return "``"
    # Find the longest run of consecutive backticks in the text.
    max_run = 0
    current = 0
    for ch in text:
        if ch == "`":
            current += 1
            if current > max_run:
                max_run = current
        else:
            current = 0
    # Wrap with a fence one longer than the longest internal run.
    fence = "`" * (max_run + 1)
    # If the content starts or ends with a backtick, pad with a
    # single space on each side so the fence doesn't touch the
    # backtick (CommonMark would otherwise interpret the
    # leading/trailing backtick as part of the fence).
    pad = " " if (text.startswith("`") or text.endswith("`")) else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _md_command_lines(
    command: str,
    *,
    prefix: str = "",
    inline_suffix: str = "",
) -> list[str]:
    """Render an operator-configured ``test_command`` as
    one or more markdown lines. Returns a list suitable for
    ``lines.extend(...)``.

    Two rendering forms based on whether ``command`` contains
    newlines:

    1. **Single-line** → ``[f"{prefix}**Command:** {inline}{inline_suffix}"]``
       where ``inline`` is :func:`_md_inline_code`'s
       backtick-safe span. ``inline_suffix`` is appended to the
       end of the line (used for the findings.md trailing
       two-space hard-break convention).
    2. **Multi-line** → a fenced code block on its own
       paragraph::

           {prefix}**Command:**

           ```sh
           {command}
           ```

       The ``inline_suffix`` is intentionally NOT applied to the
       multi-line form — fenced code blocks don't need a hard-
       break suffix, and adding one would create a malformed line.
       The fence length adapts to the longest internal run of
       backticks so commands containing backtick-fenced
       subshells still render correctly.

    Args:
        command: The test command string (possibly multi-line).
        prefix: Leading text on the first line (e.g. ``"- "`` for
            list-item context in summary.md; empty for the bare
            findings.md Test Suite block).
        inline_suffix: Trailing text appended to the single-line
            form (e.g. ``"  "`` for markdown hard-break).
            Ignored for the multi-line form.

    Why this helper exists: TOML triple-quoted strings let
    operators write multi-line test_commands (e.g. setup +
    invocation, one-step-per-line for clarity). Rendering
    multi-line content with the single-line ``**Command:**
    \\`...\\``` form produces broken markdown — the inline span
    doesn't accept newlines, so the second line drops out of the
    code span and renders as paragraph text.
    """
    if "\n" not in command:
        return [f"{prefix}**Command:** {_md_inline_code(command)}{inline_suffix}"]
    # Multi-line: render as a fenced block. Fence length is one
    # longer than the longest run of backticks in the content
    # so any inner backticks pass through unchanged.
    max_run = 0
    current = 0
    for ch in command:
        if ch == "`":
            current += 1
            if current > max_run:
                max_run = current
        else:
            current = 0
    fence = "`" * max(3, max_run + 1)
    return [
        f"{prefix}**Command:**",
        "",
        f"{fence}sh",
        command.rstrip("\n"),
        fence,
    ]


def _file_or_repo_wide(file: str | None) -> str:
    """Markdown for ``ConsolidatedFinding.file`` rendering — ``file``
    as a code-span when present, ``"repo-wide"`` (no backticks) when
    null. Matches the brief's ``findings.md`` layout."""
    if file is None:
        return "repo-wide"
    return f"`{file}`"


def _consensus_lines(
    finding: ConsolidatedFinding,
    dispatch_result: DispatchResult | None,
) -> list[str]:
    """the advisory per-finding reviewer-consensus line for findings.md.

    Returns a single ``**Consensus:** N of M reviewers[ (unanimous)]`` line
    (as a one-element list, for ``lines.extend``), or ``[]`` when
    ``dispatch_result is None``.

    - **N** = distinct ``reviewer_name``s in the finding's ``provenance`` —
      how many reviewers actually raised this finding.
    - **M** = reviewers in the run (``len(dispatch_result.results)``).
    - ``M > 1 and N == M`` → append ``(unanimous)``; ``M == 1`` → the singular
      noun "reviewer" (unanimity is only meaningful with more than one).

    Advisory only: consensus is render-derived and never reaches
    :func:`syncade.orchestrator._compute_exit_code` — the mechanical verdict is
    untouched. Dismissed findings still render their consensus; it describes who
    *raised* the finding, independent of the synthesizer's disposition.
    """
    if dispatch_result is None:
        return []
    m = len(dispatch_result.results)
    n = distinct_reviewer_count(finding.provenance)
    noun = "reviewer" if m == 1 else "reviewers"
    line = f"**Consensus:** {n} of {m} {noun}"
    if m > 1 and n == m:
        line += " (unanimous)"
    return [f"{line}  "]


def _summary_needs_block_form(summary: str) -> bool:
    """Decide whether to render ``summary`` inline or as its own block.

    The reviewer template allows the ``summary`` field to be "one paragraph or a
    short bulleted list". Inline rendering
    (``**Summary:** the prose``) reads cleanly for one-paragraph
    content, but produces malformed markdown when the summary starts
    with a list marker (``- ran pytest`` becomes
    ``**Summary:** - ran pytest`` — the ``-`` reads as inline text,
    not a list bullet) or contains newlines (the second line and
    onward render outside the bold marker's scope and can look
    detached).

    Block form is used when either holds:

    - the summary contains a newline (multi-line content), OR
    - the summary's first non-whitespace characters are a markdown
      list marker (``- ``, ``* ``, or ``+ ``) — the reviewer
      intentionally wrote a bulleted list.

    Otherwise inline form is used.
    """
    if "\n" in summary:
        return True
    stripped_left = summary.lstrip()
    return stripped_left.startswith(("- ", "* ", "+ "))


def _format_summary_block(summary: str) -> list[str]:
    """Render the ``**Summary:**`` block for one reviewer.

    See :func:`_summary_needs_block_form` for the inline-vs-block
    decision rule. Returns a list of lines suitable for
    ``lines.extend(...)``. Inline form is one line; block form is
    the header on its own line, then a blank line, then the summary
    content (which may itself span multiple lines). Trailing
    whitespace is never produced — the inline form ends with the
    summary content directly; the block form's header line is bare.
    """
    if _summary_needs_block_form(summary):
        return ["**Summary:**", "", summary]
    return [f"**Summary:** {summary}"]


def _format_string_list_block(header: str, items: list[str]) -> list[str]:
    """Render a ``**header:**`` block for the narrative sections
    in ``summary.md`` as a list of lines (no trailing whitespace).

    - **Empty list** renders inline as a single line ending with
      ``None.``::

          **Coverage gaps:** None.

      Brief calls this out by name: empty lists should read as
      ``None`` for cleaner human reading, not as an empty bullet.
    - **Non-empty list** renders the header on its own line, then a
      blank line, then a markdown bulleted list, one item per line.
      The header line has NO trailing whitespace (fix #3 from the
      QA round)::

          **Coverage gaps:**

          - did not exercise the staging Postgres
          - skipped playwright on mobile breakpoints

    Returns a list of lines suitable for the caller's
    ``lines.extend(...)`` — the caller is responsible for joining
    with ``\\n`` at the end. Returning a list (rather than a single
    string) is what makes the no-trailing-whitespace guarantee
    achievable: a single-string formulation forces the header onto
    the same return as the first content line, and the seam between
    them needs whitespace one way or the other.
    """
    if not items:
        return [f"**{header}:** None."]
    return [f"**{header}:**", "", *(f"- {item}" for item in items)]


def _reviewer_file_links(run_result: ReviewerRunResult) -> str:
    """Build the ``[.ext](file)`` link list for one reviewer, including
    only the per-reviewer files :func:`persist_reviewer_result` actually
    writes for that outcome.

    ``.stdout`` / ``.stderr`` are always written. A success additionally
    writes ``.parsed.json``; a failure additionally writes
    ``.error.txt``. Deriving the list from the outcome — rather than
    probing the filesystem — keeps this independent of call ordering and
    free of dangling links.
    """
    name = run_result.reviewer_name
    suffixes = [".stdout", ".stderr"]
    if run_result.output is not None:
        suffixes.insert(0, ".parsed.json")
    if run_result.error is not None:
        suffixes.append(".error.txt")
    return " | ".join(f"[{suffix}]({name}{suffix})" for suffix in suffixes)
