"""Reviewer-output blob rendering for the synthesizer prompt.

The synthesizer's prompt has a single ``reviewer_outputs_json``
placeholder. This module owns the serialization of the dispatcher's
per-reviewer results into the labeled-blob form the prompt expects.

Cold-input invariant (see package docstring): only structured
:class:`~syncade.findings.ReviewerOutput` payloads enter the blob —
no producer narrative, no test output, no raw stdout prose. The
helper is the single place to enforce that.
"""

from __future__ import annotations

from syncade.dispatcher import ReviewerRunResult


def render_reviewer_outputs_blob(reviewer_results: list[ReviewerRunResult]) -> str:
    """Serialize each reviewer's :class:`ReviewerOutput` into the
    labeled-blob form the synthesizer prompt expects.

    Format: each entry is the reviewer's ``name`` on its own line,
    followed by the pydantic ``model_dump_json(indent=2)`` of its
    output. Entries are separated by a blank line so the model can
    visually parse the boundary::

        claude-reviewer:
        {
          "verdict": "NO-SHIP",
          ...
        }

        codex-reviewer:
        {
          "verdict": "SHIP",
          ...
        }

    Only successful reviewers (those with ``output is not None``) are
    included; the caller should ensure all reviewers succeeded before
    invoking the synthesizer (the orchestrator enforces this), but the
    filter is defensive — a failed reviewer with no output cannot
    contribute consolidation surface.

    Args:
        reviewer_results: The dispatcher's per-reviewer results. Order
            is preserved in the rendered blob so the synthesizer sees
            the same reviewer ordering the operator sees in
            ``summary.md``.

    Returns:
        A single string ready for the prompt's
        ``reviewer_outputs_json`` placeholder. Empty string if no
        reviewer had output — in that case the orchestrator would not
        be calling this function, but the empty fallback keeps the
        helper safe to call.
    """
    sections: list[str] = []
    for r in reviewer_results:
        if r.output is None:
            continue
        sections.append(f"{r.reviewer_name}:\n{r.output.model_dump_json(indent=2)}")
    return "\n\n".join(sections)
