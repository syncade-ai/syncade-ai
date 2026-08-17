"""Pydantic v2 models for reviewer-emitted findings.

Schema mirrors PRD Appendix B exactly: a top-level ``ReviewerOutput`` with
a ``SHIP``/``NO-SHIP`` ``verdict`` and a list of ``Finding`` records
grouped by ``severity``. Any field not listed here is rejected
(``extra="forbid"``) so reviewer drift surfaces as a parse error instead
of being silently absorbed.

``parse_reviewer_output`` is the main entry point. It accepts bare JSON,
markdown-fenced JSON, or JSON embedded in prose. Verdict-block selection and
its failure semantics live in :mod:`syncade.findings_json`, shared with the
synthesizer, spec-audit, and spec-draft parsers so they cannot drift; every
clause of the rule there is the scar of a reproduced false SHIP.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from syncade.findings_json import decode_and_validate, validate_dropping_forbidden_extras

Severity = Literal["blocker", "minor", "nit"]
"""Per-finding severity classification, per PRD Appendix B."""

Verdict = Literal["SHIP", "NO-SHIP"]
"""Top-level reviewer verdict, per PRD Appendix B."""


class ReviewerOutputError(Exception):
    """Raised when reviewer stdout can't be parsed as a :class:`ReviewerOutput`.

    The message names which block was selected as the verdict (the last
    ``json`` fence, or the whole response), why it was rejected, and the
    reviewer's ``.stdout`` artifact — so the CLI can surface a useful error via
    exit code 70 (``REVIEWER_OUTPUT_UNPARSEABLE``) without further
    introspection.
    """


class Finding(BaseModel):
    """A single reviewer-emitted finding, per PRD Appendix B.

    ``file`` is optional: ``None`` means the finding is repo-wide
    rather than tied to a specific file (e.g., a commit-message
    issue, a hygiene problem about which files are committed,
    repository-level configuration). Rejecting repo-wide findings on schema
    would discard legitimate blocker-level concerns.

    ``line`` is also optional: ``None`` means the finding is
    file-level rather than line-specific.

    ``evidence_cmd`` and ``evidence_output`` are optional —
    reviewers should populate them when they ran a concrete repro
    command, but bare assertions (e.g., a doc-level concern) don't
    need them.
    """

    model_config = ConfigDict(extra="forbid")

    severity: Severity
    file: str | None = None
    line: int | None = None
    spec_clause: str
    finding: str
    evidence_cmd: str | None = None
    evidence_output: str | None = None


class ReviewerOutput(BaseModel):
    """A complete reviewer-run output: a verdict, a list of findings, and
    narrative-surface fields that capture the reviewer's summary,
    prioritization, coverage gaps, and dismissed concerns.

    Reviewers MAY return ``verdict="SHIP"`` with a non-empty findings list
    (e.g., minor / nit findings that don't block ship). The orchestrator
    decides what to do; the schema doesn't enforce verdict↔findings
    consistency.

    ``summary``, ``priority_order``, ``coverage_gaps``, and
    ``dismissed_concerns`` are required so the reviewer must consciously assert
    each answer rather than silently omit it. ``coverage_gaps`` and
    ``dismissed_concerns`` may be ``[]`` (explicit "nothing to flag").
    ``priority_order`` is constrained to be a complete permutation of
    ``range(len(findings))`` by :meth:`_validate_priority_order`.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    findings: list[Finding] = Field(default_factory=list)

    summary: str = Field(
        ...,
        min_length=1,
        description=(
            "Reviewer's headline narrative: what was verified, what stood out, "
            "why this verdict. Must contain non-whitespace content even when "
            "verdict=SHIP and findings is empty — a SHIP without verification "
            "narrative is not useful to the operator or to the synthesizer. "
            "This structured field is the verification summary. ``min_length=1`` "
            "catches pure-empty strings; :meth:`_validate_summary_nonblank` "
            "catches whitespace-only strings (``'   '``, ``'\\n\\t '``, etc.)."
        ),
    )

    priority_order: list[int] = Field(
        ...,
        description=(
            "Indices into ``findings`` in priority order — most urgent first. "
            "Must be a complete permutation of range(len(findings)): every "
            "finding gets exactly one priority position. The orchestrator and "
            "synthesizer use this to rank within-severity-tier; a list of 3 "
            "blockers without explicit ordering is much less actionable than "
            "the same 3 with the reviewer's judgment about which to fix first. "
            "Empty list iff ``findings`` is empty."
        ),
    )

    coverage_gaps: list[str] = Field(
        ...,
        description=(
            "What the reviewer did NOT verify, and why. Surfaces honest "
            "operational limits ('I couldn't reach the staging DB', 'I "
            "didn't test mobile viewports', 'I trusted the producer's "
            "claim about backend tests'). Empty list means 'I verified "
            "everything the spec asked for' — forces the reviewer to "
            "consciously assert that rather than silently omit."
        ),
    )

    dismissed_concerns: list[str] = Field(
        ...,
        description=(
            "Issues the reviewer noticed but ruled out as non-issues, with "
            "rationale. Surfaces the rigor of the review — a NO-SHIP with "
            "zero dismissed concerns is suspicious; a SHIP with several "
            "dismissed concerns suggests the reviewer actively looked for "
            "issues rather than pattern-matching against the spec."
        ),
    )

    @field_validator("summary")
    @classmethod
    def _validate_summary_nonblank(cls, v: str) -> str:
        """``summary`` must contain at least one non-whitespace character.

        ``Field(min_length=1)`` rejects empty strings but accepts
        ``'   '`` / ``'\\n\\t '`` / other pure-whitespace values, which
        carry no narrative information and defeat the field's whole
        purpose (forcing the reviewer to assert what they actually
        verified). Brief explicitly calls this out — whitespace-only
        summary must fail validation.
        """
        if not v.strip():
            raise ValueError(
                "summary must contain non-whitespace content; got an "
                "all-whitespace value which provides no verification "
                "narrative to the operator or the synthesizer"
            )
        return v

    @model_validator(mode="after")
    def _validate_priority_order(self) -> ReviewerOutput:
        """``priority_order`` must be a complete permutation of
        ``range(len(findings))``.

        Strict on purpose. A partial ordering is harder to consume
        downstream and easier to get wrong silently. Forcing reviewers to
        permute all findings means they consciously rank each one.

        - ``len(priority_order)`` must equal ``len(findings)``
        - ``sorted(priority_order)`` must equal ``list(range(len(findings)))``
        - no duplicates, no out-of-range indices, no missing positions
        - empty list iff findings is empty
        """
        expected = list(range(len(self.findings)))
        if sorted(self.priority_order) != expected:
            raise ValueError(
                f"priority_order must be a complete permutation of "
                f"range({len(self.findings)}); got {self.priority_order}"
            )
        return self


def get_findings_schema_string() -> str:
    """Return the JSON schema string for :class:`ReviewerOutput`,
    formatted for inclusion in a reviewer prompt.

    Single source of truth — the orchestrator and any future
    prompt-rendering caller pulls from here rather than hand-rolling
    the schema. If :class:`Finding` or :class:`ReviewerOutput` evolves
    shape, this function changes once and every caller picks up the
    update.

    Output is a JSON-ish skeleton with type annotations as comments,
    NOT a strict JSON Schema document — it's prompt input, intended
    for the model to read, not a validator. The shape mirrors
    :class:`ReviewerOutput` and :class:`Finding`, which are the canonical
    pydantic models. Inline ``//`` comments call out the
    contracts the reviewer prompt relies on (which fields are
    required, which can be ``[]``, what permutation rule
    ``priority_order`` must satisfy).
    """
    return (
        "{\n"
        '  "verdict": "SHIP" | "NO-SHIP",\n'
        '  "findings": [\n'
        '    {"severity": "blocker"|"minor"|"nit", '
        '"file": "path"|null, "line": int|null, '
        '"spec_clause": "string", "finding": "string", '
        '"evidence_cmd": "string"|null, '
        '"evidence_output": "string"|null}\n'
        "  ],\n"
        '  "summary": "string (required, non-empty narrative)",\n'
        '  "priority_order": [int],  // indices into findings, '
        "complete permutation; [] when findings is []\n"
        '  "coverage_gaps": [string],     // required; [] if none\n'
        '  "dismissed_concerns": [string] // required; [] if none\n'
        "}"
    )


def parse_reviewer_output(raw: str) -> ReviewerOutput:
    """Parse a reviewer's raw stdout text into a :class:`ReviewerOutput`.

    Selection, failure semantics, and the reasons for both live in
    :mod:`syncade.findings_json`; this is the reviewer's binding of them.
    """
    return decode_and_validate(
        raw,
        # A verdict whose only defect is a forbidden extra key is repaired rather than
        # discarded (PR-h-field-05). Narrow by construction: see the function's docstring.
        validate=lambda payload: validate_dropping_forbidden_extras(
            payload, ReviewerOutput.model_validate, label="reviewer"
        ),
        error=ReviewerOutputError,
        label="reviewer",
        model_name="ReviewerOutput",
        artifact="the reviewer's .stdout in the round directory",
    )
