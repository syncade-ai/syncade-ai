"""Cross-input provenance validation for synthesizer output.

The synthesizer's prompt can fabricate ``provenance`` entries that
point at a non-existent ``reviewer_name`` or an out-of-range
``original_index``. The pydantic schema can't catch this — it has no
view of the input reviewer set — so this module owns the post-parse
sanity check.

Failures map to :class:`~syncade.synthesis.SynthesizerOutputError`,
which the driver re-raises so the orchestrator's exit-code table
turns it into exit 70 (parse failure). The error message identifies
the specific consolidated finding + provenance entry so the persisted
``synthesizer.error.txt`` tells the operator exactly which finding
misattributed its source.
"""

from __future__ import annotations

from syncade.dispatcher import ReviewerRunResult
from syncade.findings import Severity
from syncade.synthesis import SynthesizerOutput, SynthesizerOutputError


def _validate_provenance_against_reviewers(
    output: SynthesizerOutput,
    reviewer_results: list[ReviewerRunResult],
) -> None:
    """Cross-input validation: every ``provenance`` entry must point
    at a real reviewer + a real finding within that reviewer's
    output.

    Schema-level checks (``min_length=1`` on ``provenance``) prevent
    zero-provenance findings, but the schema can't see the input
    reviewer set, so it can't catch the model fabricating a
    ``reviewer_name`` that doesn't correspond to a real reviewer, an
    ``original_index`` outside the source reviewer's findings list, or
    an ``original_severity`` that misreports what the source reviewer
    actually assigned. All three shapes would render into
    ``findings.md`` as if they had real attribution — exactly the
    "cannot invent findings" invariant design set out to enforce. The
    severity cross-check additionally closes the unanimous-blocker
    bypass: a synth that records a genuinely-unanimous blocker as
    ``"minor"`` for one reviewer would defeat the schema's all-blocker
    dismissal guard (which keys on ``original_severity``); verifying
    each entry's severity against its source reviewer makes that
    impossible.

    Args:
        output: The parsed :class:`SynthesizerOutput`.
        reviewer_results: The dispatcher's per-reviewer results.
            Only successful reviewers contribute to the reviewer-name
            and findings-length lookup tables.

    Raises:
        SynthesizerOutputError: When any provenance entry references
            an unknown reviewer name, an out-of-range
            ``original_index``, OR an ``original_severity`` that does
            not match the source reviewer's actual finding severity.
            The message names the specific violation so the persisted
            ``synthesizer.error.txt`` tells the operator exactly which
            finding misattributed its source. The error maps to exit
            70 via the existing ``_compute_exit_code`` decision table.

    ``original_description`` is also cross-checked: a synthesizer that may
    restate the source can narrow a blocker into something it can then honestly
    dismiss, and the operator reading ``findings.md`` would see the fabrication
    quoted as the reviewer's verbatim framing. Provenance whose text the
    synthesizer may author is not provenance.

    The comparison normalizes runs of whitespace (so reflow and line-wrapping
    survive) but preserves case and every character otherwise — a summarized,
    truncated, or reworded quote is a hard exit 70.
    """
    # Build lookup: reviewer_name → number of original findings.
    # Only successful reviewers contribute — failed reviewers have
    # no findings list to reference, and the orchestrator's
    # synth-skipped-on-any-reviewer-failure rule means we shouldn't
    # see provenance pointing at a failed reviewer here anyway.
    findings_count_by_name: dict[str, int] = {}
    severities_by_name: dict[str, list[Severity]] = {}
    texts_by_name: dict[str, list[str]] = {}
    for r in reviewer_results:
        if r.output is None:
            continue
        findings_count_by_name[r.reviewer_name] = len(r.output.findings)
        severities_by_name[r.reviewer_name] = [f.severity for f in r.output.findings]
        texts_by_name[r.reviewer_name] = [f.finding for f in r.output.findings]

    for consolidated_idx, finding in enumerate(output.consolidated_findings):
        for prov_idx, prov in enumerate(finding.provenance):
            # Unknown reviewer name → ghost provenance.
            if prov.reviewer_name not in findings_count_by_name:
                known = sorted(findings_count_by_name.keys())
                raise SynthesizerOutputError(
                    f"synthesizer provenance references unknown reviewer "
                    f"{prov.reviewer_name!r} (consolidated_findings[{consolidated_idx}]"
                    f".provenance[{prov_idx}]); known reviewer names from this "
                    f"run: {known}. The synthesizer may have fabricated a "
                    f"provenance entry; check synthesizer.stdout for the raw "
                    f"output."
                )
            # Out-of-range original_index → ghost source finding. The
            # zero-findings case needs a distinct message.
            max_index = findings_count_by_name[prov.reviewer_name]
            if max_index == 0:
                raise SynthesizerOutputError(
                    f"synthesizer provenance original_index "
                    f"{prov.original_index} is invalid for reviewer "
                    f"{prov.reviewer_name!r}: that reviewer produced 0 "
                    f"findings, so no original_index value is valid. "
                    f"consolidated_findings[{consolidated_idx}]"
                    f".provenance[{prov_idx}]. The synthesizer may have "
                    f"fabricated a provenance entry; check "
                    f"synthesizer.stdout for the raw output."
                )
            if prov.original_index >= max_index:
                raise SynthesizerOutputError(
                    f"synthesizer provenance original_index "
                    f"{prov.original_index} is out of range for reviewer "
                    f"{prov.reviewer_name!r} (which produced {max_index} "
                    f"finding(s); valid indices are 0..{max_index - 1}). "
                    f"consolidated_findings[{consolidated_idx}]"
                    f".provenance[{prov_idx}]. The synthesizer may have "
                    f"fabricated a provenance entry; check "
                    f"synthesizer.stdout for the raw output."
                )
            # original_severity must match the SOURCE reviewer's actual
            # finding severity. The schema accepts any valid Severity
            # enum value, so a synth could record a TRUTHFUL reviewer +
            # in-range index but LIE about the severity (claim a
            # genuinely-blocker finding was "minor") to slip a unanimous
            # blocker past the schema's all-blocker dismissal guard.
            # Cross-check it here so that bypass cannot pass.
            actual_severity = severities_by_name[prov.reviewer_name][prov.original_index]
            if prov.original_severity != actual_severity:
                raise SynthesizerOutputError(
                    f"synthesizer provenance original_severity "
                    f"{prov.original_severity!r} does not match reviewer "
                    f"{prov.reviewer_name!r}'s actual severity "
                    f"{actual_severity!r} for finding {prov.original_index} "
                    f"(consolidated_findings[{consolidated_idx}]"
                    f".provenance[{prov_idx}]). The synthesizer may have "
                    f"misreported a reviewer's severity to evade the "
                    f"unanimous-blocker rule; check synthesizer.stdout for "
                    f"the raw output."
                )
            # original_description must be the reviewer's OWN words. A
            # synthesizer that may restate the source can narrow a blocker into
            # something it can then honestly dismiss, and the operator sees the
            # fabrication quoted as the reviewer's verbatim framing in
            # findings.md. Whitespace runs are normalized so reflow survives;
            # nothing else is.
            actual_text = texts_by_name[prov.reviewer_name][prov.original_index]
            if _normalize_whitespace(prov.original_description) != _normalize_whitespace(
                actual_text
            ):
                raise SynthesizerOutputError(
                    f"synthesizer provenance original_description does not match "
                    f"reviewer {prov.reviewer_name!r}'s actual finding "
                    f"{prov.original_index} text (consolidated_findings"
                    f"[{consolidated_idx}].provenance[{prov_idx}]).\n"
                    f"  reviewer wrote: {actual_text!r}\n"
                    f"  synthesizer recorded: {prov.original_description!r}\n"
                    f"Provenance must quote the reviewer verbatim — a restated "
                    f"source can be narrowed into something dismissible, and it "
                    f"renders in findings.md as the reviewer's own words. Check "
                    f"synthesizer.stdout for the raw output."
                )


def _validate_reviewer_blockers_passed_through(
    output: SynthesizerOutput,
    reviewer_results: list[ReviewerRunResult],
) -> None:
    """Cross-input validation (finding R): every reviewer-surfaced BLOCKER
    finding must appear in ``consolidated_findings`` — referenced by at least
    one provenance entry, whether the synth kept it active OR dismissed it with
    rationale.

    This is the symmetric partner of
    :func:`_validate_provenance_against_reviewers`. That function enforces
    *cannot-invent* (every emitted provenance entry traces to a real reviewer
    finding). This one enforces *cannot-omit* for blockers: anything substantive
    that ships was either caught by no reviewer or dismissed with rationale. A
    reviewer blocker must not silently vanish.

    The pydantic schema deliberately allows ``consolidated_findings == []`` (it
    means "the reviewers found nothing"), but the schema has no view of the input
    reviewer set, so it cannot tell a legitimate empty set from a synth that
    DROPPED two reviewers' blockers. With the blockers omitted,
    ``has_active_blocker`` is False and the mechanical verdict returns exit 0
    SHIP on a tree two reviewers blocked — a false ship (reproduced). This check
    closes that hole.

    Scope: BLOCKER findings only. An omitted advisory (minor/nit) finding is an
    auditability loss but cannot cause a false ship — the mechanical verdict
    reads only blockers — so enforcing pass-through for blockers is the
    correctness-critical, false-positive-free subset of pass-through rule. Runs
    after :func:`_validate_provenance_against_reviewers`, so every referenced
    ``(reviewer_name, original_index)`` pair is already known-valid.

    Args:
        output: The parsed :class:`SynthesizerOutput`.
        reviewer_results: The dispatcher's per-reviewer results. Only successful
            reviewers contribute (a failed reviewer has no findings, and the
            orchestrator skips synthesis on any reviewer failure).

    Raises:
        SynthesizerOutputError: when a reviewer blocker is referenced by no
            consolidated finding's provenance. The message names the orphaned
            reviewer finding(s) so the persisted ``synthesizer.error.txt`` points
            the operator at the dropped blocker. Maps to exit 70.
    """
    # Required: every (reviewer_name, index) the reviewers themselves flagged at
    # blocker — sourced from the reviewers' actual findings, not from what the
    # synth claimed (the severity cross-check already proved any claim truthful).
    required_blockers: set[tuple[str, int]] = set()
    for r in reviewer_results:
        if r.output is None:
            continue
        for i, finding in enumerate(r.output.findings):
            if finding.severity == "blocker":
                required_blockers.add((r.reviewer_name, i))

    # Covered: every reviewer finding referenced by SOME consolidated finding
    # (active or dismissed — a dismissed-with-rationale blocker is still a
    # legitimate pass-through; the unanimous-blocker guard handles the rest).
    covered: set[tuple[str, int]] = set()
    for finding in output.consolidated_findings:
        for prov in finding.provenance:
            covered.add((prov.reviewer_name, prov.original_index))

    missing = sorted(required_blockers - covered)
    if missing:
        detail = ", ".join(f"{name}#{idx}" for name, idx in missing)
        raise SynthesizerOutputError(
            f"synthesizer omitted reviewer-surfaced blocker finding(s) "
            f"[{detail}] from consolidated_findings. Every reviewer blocker must "
            f"be passed through (kept active OR dismissed with rationale), never "
            f"dropped. The mechanical verdict reads only "
            f"consolidated_findings, so a dropped blocker would falsely SHIP; "
            f"check synthesizer.stdout for the raw output."
        )


def _normalize_whitespace(text: str) -> str:
    """Collapse whitespace runs for provenance-quote comparison.

    Deliberately case- and content-PRESERVING, unlike
    :func:`_normalize_finding_text`: this one asks "did the synthesizer quote
    the reviewer verbatim", where a case change is a rewrite. Only reflow and
    line-wrapping are forgiven.
    """
    return " ".join(text.split())


def _normalize_finding_text(text: str) -> str:
    """Normalize reviewer finding text for exact duplicate split detection."""
    return " ".join(text.casefold().split())


def _validate_duplicate_blockers_not_split_deactivated(
    output: SynthesizerOutput,
    reviewer_results: list[ReviewerRunResult],
) -> None:
    """Defense-in-depth split guard for exact duplicate reviewer blockers.

    The schema-level unanimous-blocker rule fires only when the synth merges two
    reviewers' blocker findings into one consolidated finding. A malicious or
    confused synth can otherwise split the same concern into separate
    single-reviewer findings and downgrade/dismiss each one; pass-through is
    satisfied, but the mechanical verdict sees no active blocker.

    Full semantic concern matching is intentionally out of scope here. This guard
    takes the narrow mechanically defensible case: two or more DISTINCT reviewers
    emitted blocker findings with the same source ``file`` and normalized
    ``finding`` text. That exact duplicate must be represented by at least one
    ACTIVE consolidated blocker that carries provenance for two or more of those
    source findings. Anything else is a split/deactivate shape and maps to exit
    70.
    """
    duplicate_blockers: dict[tuple[str | None, str], set[tuple[str, int]]] = {}
    for r in reviewer_results:
        if r.output is None:
            continue
        for i, finding in enumerate(r.output.findings):
            if finding.severity != "blocker":
                continue
            key = (finding.file, _normalize_finding_text(finding.finding))
            duplicate_blockers.setdefault(key, set()).add((r.reviewer_name, i))

    for (file, text), required_sources in duplicate_blockers.items():
        distinct_reviewers = {name for name, _ in required_sources}
        if len(distinct_reviewers) < 2:
            continue

        has_merged_active_blocker = False
        for finding in output.consolidated_findings:
            covered_here = {
                (prov.reviewer_name, prov.original_index)
                for prov in finding.provenance
                if (prov.reviewer_name, prov.original_index) in required_sources
            }
            if (
                len({name for name, _ in covered_here}) >= 2
                and finding.severity == "blocker"
                and not finding.dismissed
            ):
                has_merged_active_blocker = True
                break

        if not has_merged_active_blocker:
            detail = ", ".join(f"{name}#{idx}" for name, idx in sorted(required_sources))
            location = f"file={file!r}, " if file is not None else ""
            raise SynthesizerOutputError(
                f"synthesizer split/deactivated duplicate reviewer blocker "
                f"({location}finding={text!r}) across [{detail}]. Exact duplicate "
                f"blocker findings from two or more distinct reviewers must be "
                f"merged into an active blocker, not split into single-reviewer "
                f"downgrades/dismissals; check synthesizer.stdout for the raw output."
            )


def _validate_cluster_quotes_against_reviewers(
    output: SynthesizerOutput,
    reviewer_results: list[ReviewerRunResult],
) -> None:
    """Cross-input validation: every root-cause cluster's evidence
    ``quote`` must be a VERBATIM substring of its member finding's
    reviewer-original text.

    Mirrors :func:`_validate_provenance_against_reviewers`' ``reviewer_results``
    plumbing but deliberately sets a HIGHER bar. That function intentionally
    does NOT substring-validate ``original_description`` — paraphrase is
    legitimate there ("how each reviewer framed the same concern"). Cluster
    quotes go the other way ON PURPOSE: verbatim grounding is exactly what makes
    a cluster zero-invention (the synth groups + quotes; it authors no cause or
    fix). Do not loosen this to match provenance's looser check.

    For each cluster member, the quote must be a verbatim substring of at least
    one of that member's source reviewer findings' ``finding`` text (located via
    the member's ``provenance``). Runs after
    :func:`_validate_provenance_against_reviewers` (so each provenance entry is
    known-valid) and after
    schema validation (so the cluster's member indices are in range and match
    its evidence set).

    Args:
        output: The parsed :class:`SynthesizerOutput`.
        reviewer_results: The dispatcher's per-reviewer results (same plumbing
            as the provenance check). Only successful reviewers contribute.

    Raises:
        SynthesizerOutputError: when a quote is not a verbatim substring of any
            of its member's reviewer-original finding texts. The message names
            the cluster + finding so the persisted ``synthesizer.error.txt``
            points the operator at the offending quote. Maps to exit 70.
    """
    # reviewer_name → list of original finding texts (the verbatim source).
    findings_text_by_name: dict[str, list[str]] = {}
    for r in reviewer_results:
        if r.output is None:
            continue
        findings_text_by_name[r.reviewer_name] = [f.finding for f in r.output.findings]

    for cluster_idx, cluster in enumerate(output.root_cause_clusters):
        for ev in cluster.evidence:
            member = output.consolidated_findings[ev.finding_index]
            # Gather the reviewer-original texts this member traces to.
            sources: list[str] = []
            for prov in member.provenance:
                texts = findings_text_by_name.get(prov.reviewer_name, [])
                if 0 <= prov.original_index < len(texts):
                    sources.append(texts[prov.original_index])
            if not any(ev.quote in src for src in sources):
                raise SynthesizerOutputError(
                    f"root_cause_clusters[{cluster_idx}] evidence quote for "
                    f"consolidated finding {ev.finding_index} is not a verbatim "
                    f"substring of any of that finding's reviewer-original texts "
                    f"(quote={ev.quote!r}). A cluster quote must be grounded in "
                    f"the reviewers' own words (zero-invention); the synthesizer "
                    f"may have paraphrased or fabricated it. Check "
                    f"synthesizer.stdout for the raw output."
                )
