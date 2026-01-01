"""The synthesizer parser's retry-repair tier.

:func:`parse_synthesizer_output` validates a candidate STRICTLY first. Only if that
fails does it apply this module — a narrow, closed list of KNOWN, information-free
ways a model deviates from the synthesizer schema — and validate once more.

This is a repair LIST, not a tolerance. Every repair here must be provably lossless:
it may only discard data that carries no information and can never affect the
verdict. Any deviation NOT on the list still fails to exit 70, and that is the
point — a schema we quietly bend is a schema we no longer have.

The two entries, and where each came from:

- **Reviewer-only ``line`` / ``spec_clause``** copied into a consolidated finding or
  a cluster evidence quote. Neither is in the synthesizer schema, both models are
  ``extra="forbid"``, so a stray copied coordinate turned a valid consolidation into
  exit 70. (Seen live on Codex.)
- **EMPTY ``root_cause_clusters``** — no members, no evidence. A cluster GROUPS
  findings; one that references nothing groups nothing, so discarding it is provably
  lossless. (Seen live on Anthropic in PR-v2-23: handed a single finding — nothing to
  cluster — ``claude-sonnet-4-6`` emitted an empty cluster where ``gpt-5.5`` emits
  ``[]``. The synth prompt had been tuned to one model, and only running a second
  provider surfaced it.)

  Note EMPTY, not "degenerate". A ONE-member cluster is NOT repaired: it named a
  finding, so it makes a claim, and a wrong claim must be loud. That distinction is
  the difference between a lossless repair and a silent one — see
  :class:`_EmptyClusterProbe`.

Eligibility is decided by validating against a relaxed model, never by a checklist of
what might be wrong. Enumerating defects is unbounded; inverting the question is not.
"""

from __future__ import annotations

from pydantic import Field, ValidationError

from syncade.synthesis_clusters import ClusterMemberEvidence, RootCauseCluster

_REVIEWER_ONLY_FINDING_FIELDS = frozenset({"line", "spec_clause"})


class _EmptyClusterProbe(RootCauseCluster):
    """A :class:`RootCauseCluster` with ONLY the ``>=2`` length floors relaxed.

    Used by :func:`_drop_empty_clusters` to decide a cluster is safe to
    discard. Every OTHER constraint of the real model is inherited and enforced —
    ``extra="forbid"`` at both levels, field types, non-blank ``anchor_file`` /
    ``quote``, and ``_validate_self_consistency`` — so it keeps being enforced when
    someone adds a constraint to ``RootCauseCluster`` and never opens this file.

    **Two rounds of syncade's own panel shaped this, and both lessons are load-bearing.**

    *Enumerating defects is unbounded.* The first version gated on cluster key names.
    A reviewer found unknown keys nested in ``evidence``; a nested-key check was
    added; the next reviewer found unknown value TYPES going through the same hole.
    Keys, then nested keys, then types — each patch was answered by the next level
    down. Inverting the question ("does this validate as a relaxed cluster?" rather
    than "can I list what's wrong with it?") is bounded and total.

    *And the repair must match the OBSERVED deviation, not a generalisation of it.*
    Even inverted, the gate first allowed anything with ``<2`` members — which
    quietly included the ONE-member case. A reviewer then pointed out that a
    cluster's real invariants include cross-checks against ``consolidated_findings``
    (member indices in range, ``anchor_file`` equal to the members' file, clusters
    disjoint) that a per-cluster probe cannot see. Dead right: a 1-member cluster
    with an out-of-range index would validate here and be dropped.

    The bug was the generalisation. What ``claude-sonnet-4-6`` actually emits is an
    EMPTY cluster — no members, no evidence — where ``gpt-5.5`` emits ``[]``. A
    1-member cluster is a different animal: it NAMED a finding, so it makes a claim,
    and a wrong claim must be loud. Narrowing the drop to genuinely-empty clusters
    ends the whole line of attack, because a cluster that references nothing cannot
    violate a cross-check — there is nothing to cross-check. The probe's blindness
    to ``consolidated_findings`` stops mattering rather than being papered over.
    """

    member_finding_indices: list[int] = Field(...)
    evidence: list[ClusterMemberEvidence] = Field(...)


def _is_empty_cluster(cluster: object) -> bool:
    """Whether ``cluster`` is well-formed but references NOTHING.

    Both conditions matter. The probe rules out any defect other than the empty
    lists; the emptiness itself is what makes dropping provably lossless and
    context-free — no members means no index to be out of range, no file to
    mismatch ``anchor_file``, and no member to overlap another cluster.
    """
    try:
        probe = _EmptyClusterProbe.model_validate(cluster)
    except ValidationError:
        return False  # something ELSE is wrong — keep it, let the strict pass fail
    return not probe.member_finding_indices and not probe.evidence


def _strip_reviewer_only_keys(items: list) -> tuple[list[object], bool]:
    """Drop known reviewer-only keys from each dict in ``items``.

    Returns the repaired list plus whether any key was removed. Non-dict
    entries pass through untouched. Shared by the consolidated-finding and
    cluster-evidence repairs so both strip the same field set identically.
    """
    repaired: list[object] = []
    changed = False
    for item in items:
        if not isinstance(item, dict):
            repaired.append(item)
            continue
        stripped = {k: v for k, v in item.items() if k not in _REVIEWER_ONLY_FINDING_FIELDS}
        if len(stripped) != len(item):
            changed = True
        repaired.append(stripped)
    return repaired, changed


def _strip_cluster_evidence(clusters: list) -> tuple[list[object], bool]:
    """Mirror the finding repair onto each cluster's ``evidence`` entries.

    A model that copies ``line``/``spec_clause`` next to a cluster ``quote``
    would otherwise be rejected by ``ClusterMemberEvidence`` (``extra="forbid"``).
    Clusters/evidence not shaped as dict/list pass through untouched — the
    strict validation pass already rejects those.
    """
    repaired: list[object] = []
    changed = False
    for cluster in clusters:
        evidence = cluster.get("evidence") if isinstance(cluster, dict) else None
        if not isinstance(evidence, list):
            repaired.append(cluster)
            continue
        new_evidence, ev_changed = _strip_reviewer_only_keys(evidence)
        if not ev_changed:
            repaired.append(cluster)
            continue
        new_cluster = dict(cluster)
        new_cluster["evidence"] = new_evidence
        repaired.append(new_cluster)
        changed = True
    return repaired, changed


def _drop_empty_clusters(clusters: list) -> tuple[list[object], bool]:
    """Drop clusters that reference NOTHING — no members, no evidence.

    A root-cause cluster GROUPS findings — ``member_finding_indices`` requires
    ``>=2``. A cluster with NO members therefore asserts nothing at all: it groups
    nothing and references nothing. But it fails ``min_length=2`` validation, and it
    takes the whole (otherwise perfect) consolidation to exit 70 with it.

    Observed live on ``claude-sonnet-4-6`` (PR-v2-23), which — handed a single
    finding, i.e. nothing to cluster — emitted one cluster with empty
    ``member_finding_indices`` and empty ``evidence`` rather than the ``[]`` the
    schema asks for. gpt-5.5 emits ``[]`` correctly, which is exactly why this
    never surfaced while the judge was hardwired to codex.

    Dropping is safe, and specifically it cannot launder a finding away:
    ``root_cause_clusters`` is ADVISORY and never affects the verdict (see
    :class:`SynthesizerOutput`), and a degenerate cluster names no findings to
    begin with. The alternative — burning a run, and both reviewers' spend, over
    a null-valued optional field — is strictly worse.

    Only genuinely EMPTY clusters are dropped — see :class:`_EmptyClusterProbe` for
    why the earlier "<2 members" rule was too broad, and why an empty cluster is the
    only one that can be discarded without reasoning about the findings list.

    Every other defect — an unknown key at either level, a wrong value type, a blank
    ``anchor_file``, evidence inconsistent with its members, and now a 1-member
    cluster (which NAMED a finding, so it makes a claim) — leaves the cluster in
    place and the strict pass rejects the whole output at exit 70. Unexplained drift
    stays loud even in data we would have discarded: that is what keeps this a
    repair LIST and not a tolerance.

    (The known-repairable ``line``/``spec_clause`` keys are already gone by this
    point — :func:`_strip_cluster_evidence` runs first — so anything the probe
    still rejects is genuinely unexplained.)

    Returns the repaired list plus whether anything was dropped.
    """
    repaired: list[object] = []
    changed = False
    for cluster in clusters:
        if _is_empty_cluster(cluster):
            changed = True
            continue
        repaired.append(cluster)
    return repaired, changed


def _repair_known_model_deviations(parsed: object) -> object:
    """Repair the known, information-free ways a model deviates from the schema.

    Two repairs, both narrow and both losing nothing:

    1. Reviewer-only ``line`` / ``spec_clause`` fields copied into
       ``consolidated_findings`` entries or into a
       ``root_cause_clusters[*].evidence[*]`` quote object. Neither is part of
       the synthesizer schema and neither is used for verdicts, but
       ``ConsolidatedFinding`` and ``ClusterMemberEvidence`` are
       ``extra="forbid"`` — so a stray copied coordinate turns an otherwise valid
       consolidation into exit 70. (Seen live on Codex.)
    2. Degenerate ``root_cause_clusters`` with fewer than two members — see
       :func:`_drop_empty_clusters`. (Seen live on Anthropic.)

    Everything else still fails validation: this is a repair list, not a
    tolerance. Returns ``parsed`` unchanged (same object) when nothing was
    repaired, so the caller can skip the retry.
    """
    if not isinstance(parsed, dict):
        return parsed

    repaired_parsed = dict(parsed)
    repaired_any = False

    findings = parsed.get("consolidated_findings")
    if isinstance(findings, list):
        repaired_findings, changed = _strip_reviewer_only_keys(findings)
        if changed:
            repaired_parsed["consolidated_findings"] = repaired_findings
            repaired_any = True

    clusters = parsed.get("root_cause_clusters")
    if isinstance(clusters, list):
        repaired_clusters, stripped = _strip_cluster_evidence(clusters)
        repaired_clusters, dropped = _drop_empty_clusters(repaired_clusters)
        if stripped or dropped:
            repaired_parsed["root_cause_clusters"] = repaired_clusters
            repaired_any = True

    return repaired_parsed if repaired_any else parsed
