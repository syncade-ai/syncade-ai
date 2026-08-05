"""Spec-audit pydantic schema + parser.

:class:`SpecAuditOutputError`, the :class:`SpecAuditFinding` / :class:`SpecAuditOutput`
models (with the strict ``extra="forbid"`` discipline + permutation validator),
``get_spec_audit_schema_string`` (the prompt's ``{json_schema}`` source), and
the parser (``parse_spec_audit_output`` + its helper). ``spec_audit.py``
re-exports these for the ``syncade.spec_audit.<name>`` import paths.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from syncade.findings import Severity
from syncade.findings_json import decode_and_validate


class SpecAuditOutputError(Exception):
    """Raised when the auditor's stdout can't be parsed as
    :class:`SpecAuditOutput`.

    Distinct from :class:`~syncade.findings.ReviewerOutputError` and
    :class:`~syncade.synthesis.SynthesizerOutputError` so the CLI can
    route to exit 70 and name the spec-audit phase in the diagnostic.
    """


AuditVerdict = Literal["READY", "NEEDS-CLARIFICATION"]
"""Top-level spec audit verdict. ``READY`` requires affirmative verification
that the brief is free of every issue class; ``NEEDS-CLARIFICATION`` is the
default when any blocker-severity finding is present or the audit is
incomplete."""

IssueClass = Literal[
    "unverified_claim",
    "internal_contradiction",
    "ambiguous_acceptance_criteria",
    "missing_reference",
    "scope_drift",
    "missing_structural_sections",
]
"""Enumeration of the six spec-audit issue classes.

Single source of truth — the template, schema string, and pydantic model
all derive from this. Constraining to a ``Literal`` means malformed auditor
output (e.g. ``"typo_class"``) is rejected by pydantic before it reaches
the caller, raising ``ValidationError`` and ultimately ``SpecAuditOutputError``.
"""


class SpecAuditFinding(BaseModel):
    """A single spec-level finding from the auditor.

    Uses ``section`` and ``line`` (not ``file`` and ``line`` like
    :class:`~syncade.findings.Finding`) — spec issues live in the brief
    document, not in source files.

    ``extra="forbid"`` rejects any field not listed here. Schema
    strictness applies: ``location``, ``path``, ``where``,
    and any alias are forbidden.
    """

    model_config = ConfigDict(extra="forbid")

    severity: Severity
    section: str = Field(..., min_length=1)
    line: int | None = None
    issue_class: IssueClass
    finding: str = Field(..., min_length=1)
    suggested_remediation: str | None = None

    @field_validator("section", "finding")
    @classmethod
    def _validate_nonblank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must contain non-whitespace content; got an all-whitespace value")
        return v


class SpecAuditOutput(BaseModel):
    """Top-level output of one spec audit subprocess run.

    Mirrors :class:`~syncade.findings.ReviewerOutput` with the same
    strict-fields-only discipline and the same ``summary``,
    ``priority_order``, ``coverage_gaps``, ``dismissed_concerns`` surface. The
    ``verdict`` field is present here because the spec audit is advisory and
    its verdict drives the CLI exit code, not the loop's mechanical verdict.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: AuditVerdict
    findings: list[SpecAuditFinding] = Field(default_factory=list)
    summary: str = Field(..., min_length=1)
    priority_order: list[int] = Field(...)
    coverage_gaps: list[str] = Field(...)
    dismissed_concerns: list[str] = Field(...)

    @field_validator("summary")
    @classmethod
    def _validate_summary_nonblank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "summary must contain non-whitespace content; got an "
                "all-whitespace value which provides no audit narrative"
            )
        return v

    @model_validator(mode="after")
    def _validate_priority_order(self) -> SpecAuditOutput:
        """``priority_order`` must be a complete permutation of
        ``range(len(findings))``. Empty list iff findings is empty."""
        expected = list(range(len(self.findings)))
        if sorted(self.priority_order) != expected:
            raise ValueError(
                f"priority_order must be a complete permutation of "
                f"range({len(self.findings)}); got {self.priority_order}"
            )
        return self


def get_spec_audit_schema_string() -> str:
    """Return the JSON schema string for :class:`SpecAuditOutput`,
    formatted for inclusion in the spec audit prompt template's
    ``{json_schema}`` placeholder.

    Single source of truth — the prompt template pulls from here. Parallel
    to :func:`syncade.findings.get_findings_schema_string` and
    :func:`syncade.synthesis.get_synthesizer_schema_string`.

    The ``section`` and ``line`` fields are the spec-audit-specific
    location anchor (not ``file`` and ``line`` like reviewer findings).
    Schema strictness is explicit in the comment.
    """
    return (
        "{\n"
        '  "verdict": "READY" | "NEEDS-CLARIFICATION",\n'
        '  "findings": [\n'
        "    {\n"
        '      "severity": "blocker"|"minor"|"nit",\n'
        '      "section": "string (brief section name, required)",\n'
        '      "line": int|null,  '
        "// specific line in the brief if known\n"
        '      "issue_class": "unverified_claim"|"internal_contradiction"|'
        '"ambiguous_acceptance_criteria"|"missing_reference"|'
        '"scope_drift"|"missing_structural_sections",\n'
        '      "finding": "string (description with citation, required)",\n'
        '      "suggested_remediation": "string"|null\n'
        "    }\n"
        "  ],\n"
        '  "summary": "string (required, non-empty audit narrative)",\n'
        '  "priority_order": [int],  '
        "// indices into findings, complete permutation; [] when findings is []\n"
        '  "coverage_gaps": [string],      // required; [] if none\n'
        '  "dismissed_concerns": [string]  // required; [] if none\n'
        "}\n"
        "\n"
        "SCHEMA FIELD NAMES ARE EXACT. Do NOT use 'location', 'path', 'file',\n"
        "'where', or any alias for 'section'. Do NOT use 'line_number' or 'lineno'\n"
        "for 'line'. The parser uses extra='forbid' and will reject your response\n"
        "if you use non-schema field names."
    )


def parse_spec_audit_output(raw: str) -> SpecAuditOutput:
    """Parse an auditor's raw stdout text into a :class:`SpecAuditOutput`.

    Selects exactly ONE verdict block via
    :func:`syncade.findings_json._decode_verdict_object` (last
    ``json``/unlabeled fence, else the whole response) and validates it. No
    fallback to an earlier block — see :mod:`syncade.findings_json`.

    Raises :class:`SpecAuditOutputError` on failure so the CLI can route to
    exit 70 and name the spec-audit phase.
    """
    return decode_and_validate(
        raw,
        validate=SpecAuditOutput.model_validate,
        error=SpecAuditOutputError,
        label="spec audit",
        model_name="SpecAuditOutput",
        artifact="spec-auditor.stdout in the run directory",
    )
