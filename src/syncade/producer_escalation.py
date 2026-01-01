"""Producer escalation channel.

The producer's commit/stall/subprocess-error outcomes need a separate way to
say *"I did what I could; finding X is an operator decision, not a code defect I
can fix."*

This module adds that channel as data only: a small structured record
(:class:`ProducerEscalation`) and a parser
(:func:`parse_producer_escalation`) that extracts it from the producer's
free-form narrative. The producer signals an escalation by emitting a
sentinel-delimited JSON block:

.. code-block:: text

    <<<SYNCADE-ESCALATE>>>
    {"finding_indices": [0, 2], "finding": "...", "decision": "...",
     "options": ["A", "B"], "rationale": "..."}
    <<<END-SYNCADE-ESCALATE>>>

The evidence bar is enforced *structurally*: every field is required and non-empty, and
``options`` must be a non-empty list of non-empty strings. A malformed or
incomplete block is NOT an escalation — :func:`parse_producer_escalation`
returns ``None`` and the producer run is treated as an ordinary stall.

``finding_indices`` is the set of active-blocker indices into the
round's ``SynthesizerOutput.consolidated_findings`` that this single
operator decision resolves. The parser enforces it structurally — a non-empty list of
unique non-negative ints — but does NOT check the indices against the
round's findings (it has no access to them). The loop terminator does the
in-range / is-active-blocker cross-check and honors the escalation only
when ``finding_indices`` covers *every* active blocker in the round; see
:func:`syncade.orchestrator.escalation_coverage.escalation_covers_active_blockers`.

This module is pure data + parsing; it does not drive the loop. The loop handles
the ``escalated`` producer outcome, ``decision_needed`` termination reason,
``decision-needed.md``, and resume-with-decision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

ESCALATE_OPEN = "<<<SYNCADE-ESCALATE>>>"
"""Opening sentinel for the producer's escalation block. Hardcoded
verbatim in ``templates/producer.md`` — the prompt and this parser MUST
agree, pinned by ``test_prompts.py``'s sentinel-sync assertion."""

ESCALATE_CLOSE = "<<<END-SYNCADE-ESCALATE>>>"
"""Closing sentinel for the producer's escalation block."""


@dataclass(frozen=True)
class ProducerEscalation:
    """One producer escalation: a finding the producer determined is an
    operator decision rather than a code defect it can fix.

    Attributes:
        finding_indices: The indices into the round's
            ``SynthesizerOutput.consolidated_findings`` that this single
            operator decision resolves. Non-empty, unique, each
            ``>= 0``. Structural-only here; the loop terminator checks them
            against the round's actual findings and honors the escalation
            only when they cover every active blocker.
        finding: A one-line reference to the finding being escalated.
        decision: The specific decision the operator must make.
        options: The concrete options the operator chooses among
            (non-empty; each a non-empty string).
        rationale: The reproduction-backed justification for why this is
            an operator decision, not a code fix (the evidence bar).
    """

    finding_indices: list[int]
    finding: str
    decision: str
    options: list[str]
    rationale: str


def parse_producer_escalation(narrative_text: str) -> ProducerEscalation | None:
    """Extract a :class:`ProducerEscalation` from the producer's narrative,
    or ``None`` when there is no well-formed escalation block.

    Finds the first ``ESCALATE_OPEN`` … ``ESCALATE_CLOSE`` pair and parses
    the JSON between them. Returns ``None`` (→ treated as a plain stall)
    on any of: no sentinels, an unterminated block, non-JSON content, a
    non-object payload, a missing/empty required field, an ``options``
    that isn't a non-empty list of non-empty strings, or a
    ``finding_indices`` that isn't a non-empty list of unique
    non-negative ints. The strictness IS the evidence bar — an escalation
    that doesn't carry finding_indices + finding + decision + options +
    rationale is not an escalation.
    """
    start = narrative_text.find(ESCALATE_OPEN)
    if start == -1:
        return None
    body_start = start + len(ESCALATE_OPEN)
    end = narrative_text.find(ESCALATE_CLOSE, body_start)
    if end == -1:
        return None
    blob = narrative_text[body_start:end].strip()
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    finding_indices = data.get("finding_indices")
    finding = data.get("finding")
    decision = data.get("decision")
    options = data.get("options")
    rationale = data.get("rationale")

    if not (isinstance(finding, str) and finding.strip()):
        return None
    if not (isinstance(decision, str) and decision.strip()):
        return None
    if not (isinstance(rationale, str) and rationale.strip()):
        return None
    if not (
        isinstance(options, list)
        and options
        and all(isinstance(o, str) and o.strip() for o in options)
    ):
        return None
    # finding_indices: a non-empty list of unique non-negative ints.
    # ``isinstance(True, int)`` is True, so bools are excluded explicitly
    # (a JSON ``true`` must not masquerade as index 1). In-range and
    # is-active-blocker checks are NOT done here — the parser has no access
    # to the round's findings; the loop terminator does that cross-check.
    if not (
        isinstance(finding_indices, list)
        and finding_indices
        and all(isinstance(i, int) and not isinstance(i, bool) and i >= 0 for i in finding_indices)
        and len(set(finding_indices)) == len(finding_indices)
    ):
        return None

    return ProducerEscalation(
        finding_indices=list(finding_indices),
        finding=finding.strip(),
        decision=decision.strip(),
        options=[o.strip() for o in options],
        rationale=rationale.strip(),
    )
