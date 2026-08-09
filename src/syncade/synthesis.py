"""Pydantic v2 models for the cold synthesizer's output.

The synthesizer consolidates reviewer outputs into one
:class:`SynthesizerOutput`. It does not see the diff or producer narrative; its
inputs are structured reviewer outputs only. It cannot invent findings; every
:class:`ConsolidatedFinding` must trace to at least one original reviewer
finding via :class:`FindingProvenance`. The final ship/no-ship verdict is
computed mechanically from this output, not by an LLM judgment field.

Key schema-level invariants:

- **Cannot invent findings.** ``ConsolidatedFinding.provenance`` is
  required and ``min_length=1`` — a finding with no provenance is an
  invented finding, schema-rejected.
- **Cannot deactivate unanimous blockers.** If two or more distinct
  reviewers flagged the same consolidated concern AT
  ``severity="blocker"``, the synthesizer cannot dismiss it OR downgrade it
  off ``severity="blocker"``. Coverage is counted over the blocker-severity
  provenance entries only, so merging in a third, lower-severity entry
  cannot disarm the guard. This is the hard guardrail from the design
  discussion: two independent blind reviewers reaching blocker on the same
  concern is the strongest signal we get; overriding it would cost more than
  trusting it. Enforced in the schema
  (``_validate_unanimous_blocker_not_deactivated``) rather than only in the
  prompt, so a conforming :class:`SynthesizerOutput` cannot violate it.
- **Dismissal rationale required when dismissed.** Forces the
  synthesizer to write down WHY it ruled a finding out, instead of
  silently dropping it.
- **Severity-update rationale required when the synthesizer overrode
  every reviewer.** Moving off one reviewer's call when another agrees
  is judgment within reviewer signal; moving off all reviewers' calls
  is independent judgment and needs explicit reason.
- **Whitespace-only string rejection** on every always-required string
  field. ``Field(min_length=1)`` alone is insufficient: pure-whitespace
  values pass length but defeat the field's purpose.

:func:`parse_synthesizer_output` turns raw stdout into a typed
:class:`SynthesizerOutput`. It reuses :mod:`syncade.findings_json` so
verdict-block selection — fence authority, code-sample masking, unmatched-brace
tolerance, CRLF handling, duplicate-key rejection — is identical to reviewer
parsing and cannot drift from it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from syncade.findings import Severity
from syncade.findings_json import decode_and_validate
from syncade.synthesis_clusters import (
    RootCauseCluster,
    validate_clusters_against_findings,
)

# get_synthesizer_schema_string moved to synthesis_schema; redundant-alias
# re-export (X as X) marks it intentional so it survives ruff's F401 autofix —
# syncade.synthesis.get_synthesizer_schema_string stays importable.
from syncade.synthesis_repair import _repair_known_model_deviations
from syncade.synthesis_schema import get_synthesizer_schema_string as get_synthesizer_schema_string


class SynthesizerOutputError(Exception):
    """Raised when synthesizer stdout can't be parsed as a :class:`SynthesizerOutput`.

    Distinct from :class:`syncade.findings.ReviewerOutputError` so the
    orchestrator's exit-code logic can distinguish "reviewer parse
    failed" from "synthesizer parse failed" in error messaging. Both
    still map to exit 70 (``REVIEWER_OUTPUT_UNPARSEABLE``) — same
    operational fix path (inspect the raw .stdout, tighten the prompt
    if needed) — but the message names which phase so the user knows
    which file to open.
    """


class FindingProvenance(BaseModel):
    """Per-reviewer attribution for a :class:`ConsolidatedFinding`.

    Records which reviewer flagged a given concern, with the index
    into that reviewer's original ``findings`` list, the severity the
    reviewer assigned, and the reviewer's verbatim one-line
    description of the finding. Preserving the original description
    lets the operator see how each reviewer framed the same concern —
    useful when the two reviewers diverge on what they think the
    problem actually is.

    ``original_index`` is informational provenance — it lets the
    operator (or a future tool) jump from a consolidated finding back
    to the source reviewer's parsed.json. It is NOT validated against
    the actual size of the source reviewer's findings list, which
    would require cross-input plumbing this module deliberately
    avoids; that check lives in the orchestrator if it lives anywhere.
    """

    model_config = ConfigDict(extra="forbid")

    reviewer_name: str = Field(..., min_length=1)
    original_severity: Severity
    original_index: int = Field(..., ge=0)
    # NO length or non-blank constraint, deliberately (PR-h-field-01, dogfood round 4).
    # Every provenance entry passes through
    # `synthesizer.validation._validate_provenance_against_reviewers`, which OVERWRITES this
    # field with the source reviewer's own text. Whatever the model wrote here is discarded,
    # so validating it rejects runs over a value that was never going to be used: a blank or
    # whitespace-only quote aborted the whole run at exit 70 — the exact failure class the
    # repair exists to eliminate, surviving through the schema instead of the validator.
    # The guarantee did not weaken; it moved from "the model must write it correctly" to
    # "syncade copies it from the source", which is strictly stronger.
    original_description: str

    @field_validator("reviewer_name")
    @classmethod
    def _validate_reviewer_name_nonblank(cls, v: str) -> str:
        """``reviewer_name`` must contain at least one non-whitespace
        character.

        Whitespace-only validation: ``min_length=1`` accepts
        whitespace-only values like ``"   "`` which would pass the
        length check but carry no provenance information.
        """
        if not v.strip():
            raise ValueError(
                "reviewer_name must contain non-whitespace content; got "
                "an all-whitespace value which provides no provenance "
                "attribution"
            )
        return v


def distinct_reviewer_count(provenance: list[FindingProvenance]) -> int:
    """Number of DISTINCT ``reviewer_name``s in a ``provenance`` list.

    Single source of truth for "how many reviewers actually raised this
    finding". Used by BOTH the unanimous-blocker deactivation guard
    (:meth:`ConsolidatedFinding._validate_unanimous_blocker_not_deactivated`)
    and the findings.md consensus renderer
    (:func:`syncade.persistence._markdown._consensus_lines`), so the two
    views of consensus cannot drift: a single reviewer listed twice is
    ONE reviewer, never a unanimous blocker.
    """
    return len({p.reviewer_name for p in provenance})


class ConsolidatedFinding(BaseModel):
    """One concern in the synthesizer's consolidated finding set.

    Built by merging one or more reviewers' original findings into a single
    entry keyed on the underlying concern (different reviewers may word the same
    issue differently). ``provenance`` carries the per-reviewer attribution;
    ``severity`` is the synthesizer's final call,
    possibly differing from one or more reviewers'
    ``original_severity``.

    Validators (all ``@model_validator(mode="after")``):

    - :meth:`_validate_dismissal_rationale_present_when_dismissed` —
      if ``dismissed=True``, ``dismissal_rationale`` must be a
      non-whitespace string. Forces the synthesizer to record WHY a
      finding was ruled out.
    - :meth:`_validate_unanimous_blocker_not_deactivated` — if two or more
      DISTINCT reviewers flagged the concern at
      ``original_severity="blocker"`` (counted over the blocker-severity
      entries, so an extra lower-severity entry cannot disarm the
      guard), the finding cannot be deactivated — neither dismissed
      nor downgraded off ``"blocker"`` (finding A2). This is the hard
      schema-level guardrail: two blind reviewers reaching blocker on
      the same concern is the strongest signal we get; the
      synthesizer cannot silently override that.
    - :meth:`_validate_severity_change_rationale` — if ``severity``
      differs from every ``original_severity`` in ``provenance`` (the
      synthesizer moved off ALL reviewers' calls),
      ``severity_change_rationale`` is required.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = Field(..., min_length=1)
    file: str | None = None
    severity: Severity
    provenance: list[FindingProvenance] = Field(..., min_length=1)
    dismissed: bool = False
    dismissal_rationale: str | None = None
    severity_change_rationale: str | None = None

    @field_validator("description")
    @classmethod
    def _validate_description_nonblank(cls, v: str) -> str:
        """``description`` must contain at least one non-whitespace
        character. Whitespace-only validation — a pure-whitespace
        description renders as an empty bullet in ``findings.md``
        and carries no information.
        """
        if not v.strip():
            raise ValueError(
                "description must contain non-whitespace content; got "
                "an all-whitespace value which carries no description "
                "of the consolidated finding"
            )
        return v

    # Validator order matters for the operator's error-message experience. The
    # structural-impossibility check for unanimous-blocker deactivation fires
    # first; if dismissal is structurally allowed, we then check rationale.

    @model_validator(mode="after")
    def _validate_unanimous_blocker_not_deactivated(self) -> ConsolidatedFinding:
        """A unanimous reviewer blocker cannot be DEACTIVATED — neither
        dismissed NOR downgraded off ``severity="blocker"`` (finding A2).

        Two independent blind reviewers reaching 'blocker' on the same concern is
        the strongest signal we get; the synthesizer's consolidation pass cannot
        override it. Both deactivation paths slip it past the mechanical verdict
        (``has_active_blocker`` counts only non-dismissed 'blocker' findings) — a
        false SHIP — so both are schema-rejected.

        Keys on DISTINCT reviewers among the BLOCKER-severity provenance
        entries — not on every entry being a blocker. That distinction is
        load-bearing (2026-07-27 audit rank 1 / A C-01 / B C1): the guard
        used to require ``all(p.original_severity == "blocker")`` over raw
        provenance, so appending one non-blocker entry from an already-
        counted reviewer — ``[r1:blocker, r2:blocker, r1:minor]`` — silently
        disabled it and let a two-reviewer blocker be dismissed into a
        false SHIP. Merging a reviewer's lower-severity note about the same
        concern is normal, model-reachable consolidation behavior, so the
        predicate must be monotone: extra provenance can only ever ADD
        coverage, never remove it.

        Distinctness still comes from the shared ``distinct_reviewer_count``
        helper (one reviewer listed twice isn't unanimity); the findings.md
        consensus renderer calls the same helper over UNFILTERED provenance
        because it answers a different question — who *raised* the finding,
        at any severity. Provenance ``original_severity`` is cross-checked
        truthful in ``synthesizer/validation.py``, so the schema can trust
        it here. Runs first among the model validators so the
        structural-impossibility error fires before the rationale checks.
        """
        blocker_provenance = [p for p in self.provenance if p.original_severity == "blocker"]
        distinct_reviewers = distinct_reviewer_count(blocker_provenance)
        if distinct_reviewers < 2:
            return self
        if self.dismissed or self.severity != "blocker":
            how = "dismiss" if self.dismissed else f"downgrade to severity={self.severity!r}"
            raise ValueError(
                f"cannot {how} a finding flagged at severity='blocker' by "
                f"{distinct_reviewers} reviewers (unanimous-blocker rule): two or "
                "more independent blind reviewers reaching 'blocker' is the "
                "strongest available signal; the synthesizer cannot override it "
                "(neither dismissal nor downgrade off 'blocker'). Provenance: "
                + ", ".join(f"{p.reviewer_name}@blocker" for p in blocker_provenance)
            )
        return self

    @model_validator(mode="after")
    def _validate_dismissal_rationale_present_when_dismissed(self) -> ConsolidatedFinding:
        """When ``dismissed=True``, ``dismissal_rationale`` must contain
        non-whitespace content.

        The synthesizer's prompt instructs it to provide rationale, but
        a model that silently drops the rationale would otherwise pass
        validation. Whitespace-only is also rejected so the field
        carries actual narrative the operator can audit.

        Runs after the unanimous-blocker check so the
        operator sees the structural-impossibility error first when
        both rules apply.
        """
        if self.dismissed:
            if self.dismissal_rationale is None or not self.dismissal_rationale.strip():
                raise ValueError(
                    "dismissal_rationale is required (and must contain "
                    "non-whitespace content) when dismissed=True; got "
                    f"{self.dismissal_rationale!r}. Dismissing a finding "
                    "without rationale leaves the operator no way to "
                    "audit the synthesizer's decision."
                )
        return self

    @model_validator(mode="after")
    def _validate_severity_change_rationale(self) -> ConsolidatedFinding:
        """When ``severity`` differs from EVERY
        ``original_severity`` in ``provenance``,
        ``severity_change_rationale`` is required (and must be
        non-whitespace).

        The synthesizer is allowed to override individual reviewer
        severity calls without justification when at least one
        reviewer agrees with the final severity — that's normal
        disagreement-arbitration. Moving off all reviewers' calls is
        independent judgment and needs explicit rationale so the
        operator can audit it.
        """
        original_severities = {p.original_severity for p in self.provenance}
        if self.severity in original_severities:
            return self
        if self.severity_change_rationale is None or not self.severity_change_rationale.strip():
            raise ValueError(
                f"severity_change_rationale is required when severity "
                f"({self.severity!r}) differs from every reviewer's "
                f"original_severity ({sorted(original_severities)!r}); the "
                "synthesizer moved off ALL reviewers' calls and needs to "
                "record why"
            )
        return self


class SynthesizerOutput(BaseModel):
    """Top-level output of one synthesizer subprocess run.

    Holds the consolidated finding set and a headline
    :attr:`synthesis_summary` narrating how the consolidation went: how many
    findings merged, how many were dismissed, and where reviewers disagreed.

    There is intentionally no ``verdict`` field. The verdict is a
    mechanical function of ``consolidated_findings``: any non-dismissed blocker
    means NO-SHIP (exit 30); otherwise SHIP (exit 0). Adding a verdict
    field here would re-introduce LLM judgment at the verdict step,
    which is exactly what this design moves away from.

    Empty ``consolidated_findings`` is valid: it means both reviewers
    surfaced nothing and the synthesizer has nothing to consolidate.
    :attr:`synthesis_summary` is still required so even the empty case carries a
    narrative. The :attr:`root_cause_clusters` field is an optional descriptive-only
    grouping of consolidated findings that share a file, each grounded by a
    verbatim quote from a reviewer's original text. It is **advisory**
    — clusters never reach the mechanical verdict (``_compute_exit_code``
    reads :func:`has_active_blocker` only) — and authors no cause or fix.
    Defaults to ``[]``; the schema and cluster models live in
    :mod:`syncade.synthesis_clusters`. Cross-checks that need
    ``consolidated_findings`` run in :meth:`_validate_root_cause_clusters`.
    """

    model_config = ConfigDict(extra="forbid")

    consolidated_findings: list[ConsolidatedFinding] = Field(default_factory=list)
    synthesis_summary: str = Field(..., min_length=1)
    root_cause_clusters: list[RootCauseCluster] = Field(default_factory=list)

    @field_validator("synthesis_summary")
    @classmethod
    def _validate_synthesis_summary_nonblank(cls, v: str) -> str:
        """``synthesis_summary`` must contain at least one
        non-whitespace character. Whitespace-only validation.
        """
        if not v.strip():
            raise ValueError(
                "synthesis_summary must contain non-whitespace content; "
                "got an all-whitespace value which provides no narrative "
                "about how the consolidation went"
            )
        return v

    @model_validator(mode="after")
    def _validate_root_cause_clusters(self) -> SynthesizerOutput:
        """Run the cluster cross-checks that need the consolidated-finding
        list (in-range member indices, disjoint clusters, ``anchor_file`` ==
        each member's ``.file``).

        Delegated to :func:`syncade.synthesis_clusters.validate_clusters_against_findings`
        so the bulk of the cluster logic stays in its companion module; a
        violation raises ``ValueError`` → ``ValidationError`` → exit 70, the
        same path as every other synth-output schema failure. The empty default
        (``[]``) is a no-op.
        """
        validate_clusters_against_findings(self.root_cause_clusters, self.consolidated_findings)
        return self


def has_active_blocker(output: SynthesizerOutput) -> bool:
    """Return True iff ``output.consolidated_findings`` contains any
    non-dismissed finding with ``severity == "blocker"``.

    This is the mechanical verdict's substrate: any non-dismissed blocker means
    NO-SHIP (30), else SHIP (0), and ``persist_findings_md`` renders the
    operator-facing Verdict line from the same condition. Centralized
    here so the two callers can't drift — a future update to "what
    counts as an active blocker" (e.g. an additional "active" flag
    on ConsolidatedFinding) lands in one place.

    Dismissed blockers do not count, by design: dismissal-with-
    rationale is the synthesizer's bounded-judgment surface for
    ruling out false positives. The schema's unanimous-blocker rule
    ensures a dismissed blocker is always single-reviewer.
    """
    return any(f.severity == "blocker" and not f.dismissed for f in output.consolidated_findings)


def _validate_synthesizer_object(parsed: object) -> SynthesizerOutput:
    """Validate an already-decoded verdict object, strictly first and then
    through the known-deviation repair list.

    Raises :class:`pydantic.ValidationError` when both passes fail, so
    :func:`~syncade.findings_json.decode_and_validate` reports it exactly as it
    reports every other actor's schema rejection. The repair pass is a fixed
    LIST of information-free provider deviations
    (:func:`_repair_known_model_deviations`), not a tolerance — any other
    deviation still fails.
    """
    try:
        return SynthesizerOutput.model_validate(parsed)
    except ValidationError:
        repaired = _repair_known_model_deviations(parsed)
        if repaired is parsed:
            raise
    return SynthesizerOutput.model_validate(repaired)


def parse_synthesizer_output(raw: str) -> SynthesizerOutput:
    """Parse a synthesizer's raw stdout text into a :class:`SynthesizerOutput`.

    Selects exactly ONE verdict block via
    :func:`syncade.findings_json._decode_verdict_object` — the last
    ``json``/unlabeled fence, or the whole response when no fence is present —
    and validates it, strictly first and then through the known-deviation
    repair list. If that block does not decode or does not validate, this
    raises; it never falls back to an earlier block that happens to validate
    (see :mod:`syncade.findings_json`).

    Raises :class:`SynthesizerOutputError` rather than
    :class:`ReviewerOutputError` so the orchestrator's error messaging can name
    the phase the operator needs to debug, even though both map to exit 70.
    """
    return decode_and_validate(
        raw,
        validate=_validate_synthesizer_object,
        error=SynthesizerOutputError,
        label="synthesizer",
        model_name="SynthesizerOutput",
        artifact="synthesizer.stdout in the round directory",
    )
