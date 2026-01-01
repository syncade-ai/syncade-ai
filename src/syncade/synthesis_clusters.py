"""Root-cause cluster schema for the cold synthesizer.

Descriptive-only, zero-invention grouping: the synth groups ≥2 consolidated
findings that share a file and cites a **verbatim** quote from each member's
reviewer-original text. It authors **no cause and no fix** — it groups and
quotes; the producer infers the cause from the reviewers' own words. Clusters
are advisory: they never reach the mechanical verdict
(:func:`syncade.orchestrator.verdict._compute_exit_code` reads
:func:`syncade.synthesis.has_active_blocker` only).

This lives in its own module rather than ``synthesis.py`` (already at the
~500-LOC cap — see the design / the LOC discipline). ``synthesis.py`` imports
:class:`RootCauseCluster` for the ``SynthesizerOutput.root_cause_clusters``
field and calls :func:`validate_clusters_against_findings` from a delegating
``model_validator`` (the cross-checks need the ``consolidated_findings`` list,
which lives on ``SynthesizerOutput``). The verbatim-quote-against-reviewer-source
check lives in :mod:`syncade.synthesizer.validation` (it needs the input
``ReviewerOutput`` set).

**Cannot-invent, strengthened.** A cluster's only content is a grouping
(checkable: shared ``anchor_file``) + verbatim quotes (checkable: real
substrings of the reviewers' original findings) + an optional ``label`` that
must itself be a verbatim substring of one of the quotes. No field carries
authored causal prose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from syncade.synthesis import ConsolidatedFinding


class ClusterMemberEvidence(BaseModel):
    """One member finding's verbatim grounding in a root-cause cluster.

    ``finding_index`` indexes into ``SynthesizerOutput.consolidated_findings``;
    ``quote`` is a VERBATIM substring of that member's reviewer-original finding
    text (validated against the input ``ReviewerOutput``s in
    :mod:`syncade.synthesizer.validation` — NOT the synth's consolidated
    rephrasing).
    """

    model_config = ConfigDict(extra="forbid")

    finding_index: int = Field(..., ge=0)
    quote: str = Field(..., min_length=1)

    @field_validator("quote")
    @classmethod
    def _validate_quote_nonblank(cls, v: str) -> str:
        """A whitespace-only quote is a substring of almost anything and
        grounds nothing — reject it."""
        if not v.strip():
            raise ValueError(
                "quote must contain non-whitespace content; an all-whitespace quote grounds nothing"
            )
        return v


class RootCauseCluster(BaseModel):
    """A descriptive-only grouping of ≥2 consolidated findings sharing a file.

    Authors no cause and no fix. ``label``, if present, must be a verbatim
    substring of one of the evidence quotes (never authored prose).

    Self-contained validators run here; the cross-checks that need the
    ``consolidated_findings`` list (member indices in range, disjoint across
    clusters, ``anchor_file`` == each member's ``.file``) run in
    :func:`validate_clusters_against_findings`, invoked from a
    ``SynthesizerOutput`` ``model_validator``.
    """

    model_config = ConfigDict(extra="forbid")

    member_finding_indices: list[int] = Field(..., min_length=2)
    anchor_file: str = Field(..., min_length=1)
    evidence: list[ClusterMemberEvidence] = Field(..., min_length=2)
    label: str | None = None

    @field_validator("anchor_file")
    @classmethod
    def _validate_anchor_file_nonblank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("anchor_file must contain non-whitespace content")
        return v

    @model_validator(mode="after")
    def _validate_self_consistency(self) -> RootCauseCluster:
        """Self-contained cluster invariants (no consolidated_findings needed):

        - member indices unique within the cluster;
        - exactly one ``evidence`` per member (evidence ``finding_index`` set
          equals the member-index set);
        - ``label``, if present, is non-blank AND a verbatim substring of one
          of the evidence quotes (a quote excerpt, not authored prose).
        """
        members = self.member_finding_indices
        if len(set(members)) != len(members):
            raise ValueError(
                f"member_finding_indices must be unique within a cluster; got {members}"
            )
        ev_indices = [e.finding_index for e in self.evidence]
        if len(set(ev_indices)) != len(ev_indices):
            raise ValueError(
                f"evidence finding_index values must be unique within a cluster; got {ev_indices}"
            )
        if set(ev_indices) != set(members):
            raise ValueError(
                "evidence must have exactly one entry per member finding: the "
                f"evidence finding_index set {sorted(set(ev_indices))} must equal the "
                f"member_finding_indices set {sorted(set(members))}"
            )
        if self.label is not None:
            if not self.label.strip():
                raise ValueError("label, when present, must contain non-whitespace content")
            if not any(self.label in e.quote for e in self.evidence):
                raise ValueError(
                    f"label {self.label!r}, when present, must be a verbatim substring "
                    "of one of the cluster's evidence quotes — it is a quote excerpt, "
                    "not authored prose"
                )
        return self


def validate_clusters_against_findings(
    clusters: list[RootCauseCluster],
    consolidated_findings: list[ConsolidatedFinding],
) -> None:
    """Cross-checks that need the ``consolidated_findings`` list.

    Invoked from a ``SynthesizerOutput`` ``@model_validator(mode="after")`` so a
    violation raises ``ValueError`` → ``ValidationError`` → exit 70 (same path as
    the other synth-output schema failures). Invariants:

    1. every ``member_finding_indices`` value is in range ``[0, len(findings))``;
    2. findings are disjoint across clusters (each finding in ≤1 cluster);
    3. ``anchor_file`` equals the ``.file`` of every member finding.

    (≥2 members + one-evidence-per-member + label-is-a-quote are self-contained
    on :class:`RootCauseCluster`; verbatim-quote-vs-reviewer-source is in
    :mod:`syncade.synthesizer.validation`.)

    A finding with ``file=None`` cannot be clustered: ``anchor_file`` is a
    required non-blank string, so the check #3 equality fails — the intended
    precision floor (a cluster requires a shared file).
    """
    n = len(consolidated_findings)
    seen: set[int] = set()
    for ci, cluster in enumerate(clusters):
        for idx in cluster.member_finding_indices:
            if not (0 <= idx < n):
                raise ValueError(
                    f"root_cause_clusters[{ci}] member_finding_indices contains {idx}, "
                    f"out of range for {n} consolidated finding(s)"
                )
            if idx in seen:
                raise ValueError(
                    f"root_cause_clusters[{ci}] member finding {idx} already belongs to "
                    "another cluster; clusters must be disjoint (each finding in at most "
                    "one cluster)"
                )
            seen.add(idx)
        for idx in cluster.member_finding_indices:
            member_file = consolidated_findings[idx].file
            if member_file != cluster.anchor_file:
                raise ValueError(
                    f"root_cause_clusters[{ci}] anchor_file {cluster.anchor_file!r} does "
                    f"not match the file of member finding {idx} ({member_file!r}); a "
                    "cluster requires a shared file (every member must be in anchor_file)"
                )


def cluster_schema_fragment() -> str:
    """The ``root_cause_clusters`` block for the synthesizer prompt's JSON-schema
    skeleton (concatenated into
    :func:`syncade.synthesis.get_synthesizer_schema_string`).

    Descriptive-only: the inline comments stress group-and-quote, no authored
    cause/fix, and that clusters never affect the verdict.
    """
    return (
        '  "root_cause_clusters": [  // OPTIONAL, default []; ADVISORY only — '
        "never affects the verdict\n"
        "    {\n"
        '      "member_finding_indices": [int, int],  // >=2 indices into '
        "consolidated_findings; same file; disjoint across clusters\n"
        '      "anchor_file": "path",  // MUST equal the .file of every member finding\n'
        '      "evidence": [  // exactly one per member\n'
        "        {\n"
        '          "finding_index": int,  // a member index\n'
        '          "quote": "string"  // VERBATIM substring of that member\'s '
        "reviewer-original finding text — do NOT paraphrase\n"
        "        }\n"
        "      ],\n"
        '      "label": "string"|null  // OPTIONAL; if present, a verbatim substring '
        "of one quote — NOT an authored cause or fix\n"
        "    }\n"
        "  ]"
    )
