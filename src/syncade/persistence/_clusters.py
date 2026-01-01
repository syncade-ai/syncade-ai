"""Root-cause cluster rendering for findings.md.

Renders the descriptive-only ``## Root-cause clusters`` section that
``persist_findings_md`` places ABOVE the individual findings, so the producer
reads "these N findings are one root cause — here is what each reviewer said
about it" before reading them individually.

Lives in its own module (called by ``findings_md.py``) per the LOC discipline —
``findings_md.py`` was already at the ~500-LOC cap. The renderer is purely
descriptive: it surfaces the synth's grouping + the verbatim reviewer quotes and
authors nothing (the synth emits no cause or fix, and neither does this).

Returns ``[]`` when there are no clusters.
"""

from __future__ import annotations

from syncade.synthesis import SynthesizerOutput


def render_cluster_section(output: SynthesizerOutput) -> list[str]:
    """Return the ``## Root-cause clusters`` section as a list of lines, or
    ``[]`` when ``output.root_cause_clusters`` is empty.

    For each cluster: the ``anchor_file`` (+ the optional ``label``, itself a
    verbatim quote excerpt), the member finding indices, and one bullet per
    member with the finding's first description line and that member's verbatim
    reviewer quote. Descriptive-only — no authored cause or fix.

    Member indices are schema-validated in range by the time a
    :class:`SynthesizerOutput` exists, but the render guards defensively so a
    stray index can never raise mid-write.
    """
    clusters = output.root_cause_clusters
    if not clusters:
        return []

    findings = output.consolidated_findings
    lines: list[str] = [
        "## Root-cause clusters",
        "",
        "_Advisory groupings of related findings (descriptive-only — they do "
        "not change the verdict). Each member is quoted verbatim from a "
        "reviewer's original finding; the cause is the reviewers' words, not "
        "the synthesizer's._",
        "",
    ]
    for n, cluster in enumerate(clusters, start=1):
        heading = f"### Cluster {n}: `{cluster.anchor_file}`"
        if cluster.label:
            heading += f" — {cluster.label}"
        lines.append(heading)
        lines.append("")
        member_list = ", ".join(f"#{idx}" for idx in cluster.member_finding_indices)
        lines.append(f"**Member findings:** {member_list}  ")
        lines.append("")
        quote_by_index = {e.finding_index: e.quote for e in cluster.evidence}
        for idx in cluster.member_finding_indices:
            if 0 <= idx < len(findings):
                member = findings[idx]
                desc_first = member.description.strip().splitlines()[0]
                severity = member.severity
            else:  # pragma: no cover - defensive; schema validates in range
                desc_first = "(member index out of range)"
                severity = "?"
            lines.append(f"- **#{idx}** [{severity}] {desc_first}")
            quote = quote_by_index.get(idx, "").strip().replace("\n", " ")
            lines.append(f"  - reviewer quote: {quote!r}")
        lines.append("")
    return lines
