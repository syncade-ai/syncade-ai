"""Heuristic blocker classification for handoff.md.

Holds the disposition-category labels/descriptions, the phrase tables, and
``_classify_handoff_finding`` — the heuristic that buckets a remaining active
blocker into one of ``M | F | P | A | D``. ``handoff.py`` imports the classifier
and the two category dicts it renders.
"""

from __future__ import annotations

from pathlib import Path

from syncade.synthesis import ConsolidatedFinding

# Heuristic categories for auto-classifying remaining active blockers
# when the loop terminates with work left. The labels are surfaced in
# the rendered handoff.md so the operator knows what each category
# means without consulting external docs. The handoff also states
# explicitly that this is HEURISTIC — the operator's judgment owns
# the final disposition.
_HANDOFF_CATEGORY_LABELS: dict[str, str] = {
    "M": "Manual fix needed",
    "F": "False positive / convention mismatch",
    "P": "Operator-procedural / self-resolving",
    "A": "Operator-attested",
    "D": "Dismiss with rationale",
}

_HANDOFF_CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "M": ("Real code defect. Operator addresses in a new commit."),
    "F": (
        "Implementation correctly follows established convention; brief "
        "was imprecise. Operator amends the brief or adds the "
        "convention to CLAUDE.md."
    ),
    "P": (
        "Workflow-state finding (completion record absent, brief still "
        "DRAFT, status header not yet updated, commit hashes still "
        "`(to fill)`). Resolves when the operator commits the "
        "completion record."
    ),
    "A": (
        "Reviewer couldn't verify due to sandbox limitation (real-CLI "
        "smoke, network, etc.). Operator runs the gate locally and "
        "attests in the completion record with an "
        "``Operator-attested: <run-id>`` rationale line."
    ),
    "D": ("Finding is structurally noise — empty repo state, harness-induced artifact, etc."),
}

# Phrase substrings (lowercase match). Order in the classification
# helper matters because the first match wins; the rule set below
# documents that order. See ``_classify_handoff_finding`` for the priority
# order.
_HANDOFF_OPERATOR_PROCEDURAL_PHRASES = (
    "completion record",
    "pr brief",
    "status header",
    "status line",
    "commit hashes",
    "to fill",
)
_HANDOFF_OPERATOR_ATTESTED_PHRASES = (
    "sandbox",
    "couldn't run",
    "could not run",
    "sandboxed environment",
    "smoke not affirmatively verified",
    "recursive `claude -p`",
    "recursive `codex exec`",
)
_HANDOFF_CONVENTION_PHRASES = (
    "convention mismatch",
    "brief was imprecise",
    "implementation correctly follows",
    "implementation is more correct",
    "intentional convention",
)
# Worktree-strip artifact phrases. Reviewer worktrees deliberately
# strip CLAUDE.md (architectural invariant — reviewers must not see
# project memory). Any PR that legitimately edits CLAUDE.md produces
# a reviewer-side phantom "tracked deletion" finding that the cold
# synth cannot dismiss (cannot-invent invariant blocks the dismissal
# rationale). Routing these phrases to category F lets the operator see the
# structural false positive at the top of the handoff rather than buried under
# M-category defects.
_HANDOFF_WORKTREE_STRIP_PHRASES = (
    "deleted in the reviewed worktree",
    "deleted from the reviewed worktree",
    "tracked deletion",
    "tracked file deletion",
    "claude.md is deleted",
    "claude.md is still deleted",
)
_HANDOFF_TEST_REGRESSION_PHRASES = (
    "test caller broke",
    "kwarg mismatch",
)


def _classify_handoff_finding(
    finding: ConsolidatedFinding,
    pr_doc_path: Path | None = None,
) -> str:
    """Heuristically classify one ``ConsolidatedFinding`` into a
    disposition category for the handoff artifact.

    Returns one of ``"M" | "F" | "P" | "A" | "D"`` (see
    :data:`_HANDOFF_CATEGORY_LABELS` for the human-readable
    labels). This is HEURISTIC — the operator's judgment owns the
    final disposition. The handoff itself states this explicitly so
    the categorization is treated as a hint, not a decision.

    Priority order (first match wins):

    1. ``file`` equals the PR brief itself (matched on basename to
       tolerate relative-vs-absolute path mismatches) → ``"P"``.
    2. Description (synth-consolidated + every provenance entry's
       original_description) contains a workflow-state phrase
       (``"completion record"``, ``"pr brief"``, ``"status
       header"``, etc.) → ``"P"``.
    3. Description contains an operator-attested phrase
       (``"sandbox"``, ``"couldn't run"``, etc.) → ``"A"``.
    4. Description contains a convention-mismatch phrase
       (``"convention mismatch"``, ``"implementation correctly
       follows"``, etc.) → ``"F"``.
    5. Description contains a worktree-strip artifact phrase
       (``"deleted in the reviewed worktree"``, ``"tracked
       deletion"``, etc.) → ``"F"``. Reviewer worktrees strip
       CLAUDE.md per the architectural invariant; any PR that
       legitimately edits CLAUDE.md surfaces a phantom "tracked
       deletion" finding the cold synth cannot dismiss.
    6. ``file`` is under ``tests/`` AND description references a
       producer-regression phrase (``"test caller broke"``, ``"kwarg
       mismatch"``) → ``"M"``.
    7. Default → ``"M"``.

    The phrase lists cover observed reviewer-output shapes for each handoff
    category and the worktree-strip pattern (rule 5). Future updates may extend the
    phrase lists as new patterns emerge; the priority order above
    is part of the contract and shouldn't be reordered without
    re-checking the test fixtures.

    The ``"D"`` (dismiss) category exists for completeness but is
    never assigned by the heuristic — the cold synthesizer can't
    dismiss findings (cannot-invent invariant), so a category-D
    blocker would only ever land in the handoff if a future PR adds
    a "dismiss this in the completion record" workflow.
    """
    file = finding.file or ""

    # Combine synth description with every provenance's
    # original_description so phrase matches catch the original
    # reviewer's wording (which the synth may paraphrase). Lower-
    # cased once for the substring scan.
    desc_parts = [finding.description or ""]
    for p in finding.provenance:
        desc_parts.append(p.original_description or "")
    combined_desc = " ".join(desc_parts).lower()

    # Rule 1: modified path (basename match) → P.
    if pr_doc_path is not None and file:
        if Path(file).name == pr_doc_path.name:
            return "P"

    # Rule 2: workflow-state phrase → P.
    if any(phrase in combined_desc for phrase in _HANDOFF_OPERATOR_PROCEDURAL_PHRASES):
        return "P"

    # Rule 3: operator-attested phrase → A.
    if any(phrase in combined_desc for phrase in _HANDOFF_OPERATOR_ATTESTED_PHRASES):
        return "A"

    # Rule 4: convention-mismatch phrase → F.
    if any(phrase in combined_desc for phrase in _HANDOFF_CONVENTION_PHRASES):
        return "F"

    # Rule 5: worktree-strip artifact phrase → F. Reviewer worktrees
    # strip CLAUDE.md per the architectural invariant; any PR that
    # legitimately edits CLAUDE.md surfaces a phantom "tracked
    # deletion" finding the cold synth cannot dismiss. Route to F so
    # the operator's disposition path is "annotate-as-FP in the
    # completion record" rather than "fix CLAUDE.md" (which would
    # double-write or no-op against the already-present file).
    if any(phrase in combined_desc for phrase in _HANDOFF_WORKTREE_STRIP_PHRASES):
        return "F"

    # Rule 6: tests/ + producer-regression phrase → M (explicit
    # path; same as the default but documented separately).
    if file.startswith("tests/") and any(
        phrase in combined_desc for phrase in _HANDOFF_TEST_REGRESSION_PHRASES
    ):
        return "M"

    # Rule 7: default → M.
    return "M"
