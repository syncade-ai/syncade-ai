"""Finding-level capture for the derived view — PR-h-12 item 3.

Answering *"which reviewer found what, and would one reviewer alone have shipped?"* — the
question that justifies a second reviewer at all — previously required mining raw JSON, because
``metrics.db`` recorded only counts. These rows make it SQL.

A SEPARATE MODULE rather than more of :mod:`syncade.metrics.aggregate`, which sits at 550
physical lines against a 600 ceiling; extraction there would have breached it on arrival.

**The source is tier-1** (``round-N/synthesizer.parsed.json``, never deleted), so these tables
rebuild on schema drift exactly like ``runs``. The derived-view rule is intact: nothing here is
the only copy of anything.

**The count-agreement hazard is the reason this module owns the per-round authority.** Every
finding count travels as one vector with one recorded source; consumers sum those rows rather
than choosing independently between artifacts and manifests.
"""

from __future__ import annotations

import json
from pathlib import Path

from syncade.metrics.schema import FindingProvenanceRow, FindingRow, RoundRow

_SYNTH_ARTIFACT = "synthesizer.parsed.json"
_KNOWN_EMPTY_REASONS = {"no_changes_to_review", "producer_emptied_diff"}
_KNOWN_FINDING_SEVERITIES = {"blocker", "minor", "nit"}
_CountVector = tuple[int, int, int, int]
_ArtifactEvidence = tuple[list[dict[str, object]], _CountVector]
_RoundArtifacts = dict[int, tuple[Path, _ArtifactEvidence | None]]


def round_dirs(run_dir: Path) -> list[tuple[int, Path]]:
    """``(round_index, dir)`` for every ``round-N/``, numerically ordered.

    Sorting numerically rather than lexically matters past round 9; the corpus has none today,
    but a lexical sort would silently mis-order them the moment it does.
    """
    found: list[tuple[int, Path]] = []
    try:
        entries = list(run_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.startswith("round-"):
            continue
        try:
            found.append((int(entry.name.removeprefix("round-")), entry))
        except ValueError:
            continue  # not a round dir; ignore rather than guess
    return sorted(found)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _normalize_artifact(container: object) -> _ArtifactEvidence | None:
    """One complete finding list and its count vector, or no artifact evidence."""
    if not isinstance(container, dict):
        return None
    consolidated = container.get("consolidated_findings")
    if not isinstance(consolidated, list):
        return None
    normalized: list[dict[str, object]] = []
    active = {severity: 0 for severity in _KNOWN_FINDING_SEVERITIES}
    dismissed = 0
    for finding in consolidated:
        if not isinstance(finding, dict):
            return None
        severity = finding.get("severity")
        is_dismissed = finding.get("dismissed")
        if not isinstance(severity, str) or severity not in active:
            return None
        if type(is_dismissed) is not bool:
            return None
        normalized.append(finding)
        if is_dismissed:
            dismissed += 1
        else:
            active[severity] += 1
    return normalized, (active["blocker"], active["minor"], active["nit"], dismissed)


def read_finding_artifacts(run_dir: Path | str) -> _RoundArtifacts:
    """Parse and count each round's count-critical artifact exactly once."""
    artifacts: _RoundArtifacts = {}
    for round_index, round_dir in round_dirs(Path(run_dir)):
        try:
            data = json.loads((round_dir / _SYNTH_ARTIFACT).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        artifacts[round_index] = (round_dir, _normalize_artifact(data))
    return artifacts


def read_findings(
    run_dir: Path | str, run_id: str, artifacts: _RoundArtifacts
) -> tuple[list[FindingRow], list[FindingProvenanceRow]]:
    """Every consolidated finding in a run, with its provenance. Never raises.

    A missing, unreadable or malformed artifact yields nothing for that round rather than
    aborting the run — legacy and partial runs are normal in this corpus and must aggregate.
    """
    run_dir = Path(run_dir)
    findings: list[FindingRow] = []
    provenance: list[FindingProvenanceRow] = []
    try:
        _loop = json.loads((run_dir / "loop-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _loop = {}
    _loop_rounds = _loop.get("rounds") if isinstance(_loop, dict) else None

    for round_index, (round_dir, evidence) in sorted(artifacts.items()):
        # A reviewer absent from the round's PANEL cannot have raised a finding in it. Ghost
        # provenance inflates consensus, and the ratio is the number the whole feature exists
        # to report. An unrecorded panel (empty) cannot convict anyone, so it filters nothing.
        _panel, _ = _round_panel(_loop_rounds, round_dir, round_index)
        if evidence is None:
            continue
        consolidated, _counts = evidence

        for idx, finding in enumerate(consolidated):
            findings.append(
                FindingRow(
                    run_id=run_id,
                    round=round_index,
                    idx=idx,
                    severity=_text(finding.get("severity")),
                    dismissed=1 if finding["dismissed"] else 0,
                    file=_text(finding.get("file")),
                    description=_text(finding.get("description")),
                )
            )
            entries = finding.get("provenance")
            if not isinstance(entries, list):
                continue
            seen: set[str] = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = _text(entry.get("reviewer_name"))
                # The PK is (run, round, idx, reviewer_name), so a duplicate reviewer on one
                # finding would silently overwrite its own row and undercount consensus. Keep the
                # first and drop the rest, rather than letting INSERT OR REPLACE decide.
                if not name or name in seen or (_panel and name not in _panel):
                    continue
                seen.add(name)
                original_index = entry.get("original_index")
                provenance.append(
                    FindingProvenanceRow(
                        run_id=run_id,
                        round=round_index,
                        idx=idx,
                        reviewer_name=name,
                        original_severity=_text(entry.get("original_severity")),
                        original_index=(
                            original_index if isinstance(original_index, int) else None
                        ),
                    )
                )
    return findings, provenance


_ROUND_MANIFEST = "manifest.json"


def _round_panel(loop_rounds: object, round_dir: Path, round_index: int) -> tuple[set[str], str]:
    """Distinct reviewer names for one round, and whether the panel was explicitly recorded.

    Returns ``(names, panel_source)`` where ``panel_source`` is ``'recorded'`` when any
    manifest had a ``reviewers`` key (even an empty list — a deliberate no-dispatch round
    like ``no_changes_to_review`` records ``reviewers: []``) and ``'unknown'`` when no
    manifest had the key and the panel composition cannot be determined.

    Panel provenance is independent from finding-count provenance: a manifest can carry
    ``active_blocker_count`` without a ``reviewers`` list, giving
    ``counts_source='manifest'`` and ``panel_source='unknown'``.
    """
    names: set[str] = set()
    recorded = False
    if isinstance(loop_rounds, list) and round_index < len(loop_rounds):
        entry = loop_rounds[round_index]
        if isinstance(entry, dict) and "reviewers" in entry:
            recorded = True
            for rev in entry.get("reviewers") or []:
                if isinstance(rev, dict) and isinstance(rev.get("name"), str) and rev["name"]:
                    names.add(rev["name"])
    if names:
        return names, "recorded"
    try:
        data = json.loads((round_dir / _ROUND_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return names, "recorded" if recorded else "unknown"
    if isinstance(data, dict):
        if "reviewers" in data:
            recorded = True
        for rev in data.get("reviewers") or []:
            if isinstance(rev, dict) and isinstance(rev.get("name"), str) and rev["name"]:
                names.add(rev["name"])
    return names, "recorded" if recorded else "unknown"


_COUNT_KEYS = (
    "active_blocker_count",
    "active_minor_count",
    "active_nit_count",
    "dismissed_count",
)


def _synth_counts(container: object) -> _CountVector | None:
    """The complete synthesizer count vector from a manifest-shaped dict, if valid."""
    synth = container.get("synthesizer") if isinstance(container, dict) else None
    if not isinstance(synth, dict):
        return None
    values = tuple(synth.get(key) for key in _COUNT_KEYS)
    if not all(type(value) is int and value >= 0 for value in values):
        return None
    return values


def _manifest_counts(round_dir: Path, loop_rounds: object, round_index: int) -> _CountVector | None:
    """A complete manifest-recorded count vector for this round, or ``None``.

    BOTH manifests are consulted, in that order, for the same reason the round set is a union:
    either alone is incomplete. The round manifest exists for interrupted runs the loop manifest
    never summarised; the loop manifest's embedded round summary exists when the round directory
    is gone. Reading only one silently reports zero for the other's case — which is exactly the
    false zero this whole table replaces.
    """
    try:
        data = json.loads((round_dir / _ROUND_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    counts = _synth_counts(data)
    if counts is not None:
        return counts
    if isinstance(loop_rounds, list) and round_index < len(loop_rounds):
        return _synth_counts(loop_rounds[round_index])
    return None


def read_rounds(run_dir: Path | str, run_id: str, artifacts: _RoundArtifacts) -> list[RoundRow]:
    """One row per round that happened — THE authority, replacing three disagreeing derivations.

    Precedence for the complete finding-count vector is explicit and recorded, never inferred:

    1. ``artifacts`` — the synthesizer artifact parsed and every entry was well-formed.
    2. ``manifest`` — the artifact is missing or malformed but a manifest recorded all four
       counts. The round ran and found something; only the evidence of WHAT is gone.
    3. ``unknown`` — the round ran and nothing credible says what it found. Counted as a round,
       contributing zeroes by convention, because inventing counts is worse.

    A partially-malformed artifact degrades to ``manifest``/``unknown`` rather than silently
    reporting the entries that happened to parse: dropping three of five findings and calling
    the result two is a false count, not a partial one.
    """
    run_dir = Path(run_dir)
    try:
        loop = json.loads((run_dir / "loop-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loop = {}
    loop_rounds = loop.get("rounds") if isinstance(loop, dict) else None

    # The union of BOTH kinds of evidence, because either alone is incomplete: a directory can
    # exist for a round the manifest never summarised (interrupted), and a manifest can prove a
    # round ran whose directory is absent. Taking one source was the mistake this table replaces.
    on_disk = {index: entry[0] for index, entry in artifacts.items()}
    indices = set(on_disk)
    if isinstance(loop_rounds, list):
        indices |= set(range(len(loop_rounds)))

    rows: list[RoundRow] = []
    for round_index in sorted(indices):
        round_dir = on_disk.get(round_index, run_dir / f"round-{round_index}")
        panel, panel_source = _round_panel(loop_rounds, round_dir, round_index)
        known_no_dispatch = (
            isinstance(loop_rounds, list)
            and round_index == len(loop_rounds) - 1
            and isinstance(loop, dict)
            and loop.get("termination_reason") in _KNOWN_EMPTY_REASONS
            and panel_source == "recorded"
            and not panel
        )
        counts, source = _round_counts(
            round_dir,
            loop_rounds,
            round_index,
            artifacts.get(round_index, (round_dir, None))[1],
            known_no_dispatch=known_no_dispatch,
        )
        blockers, minors, nits, dismissed = counts
        rows.append(
            RoundRow(
                run_id=run_id,
                round=round_index,
                blockers=blockers,
                minors=minors,
                nits=nits,
                dismissed=dismissed,
                counts_source=source,
                panel_size=len(panel),
                panel_source=panel_source,
            )
        )
    return rows


def _round_counts(
    round_dir: Path,
    loop_rounds: object,
    round_index: int,
    artifact: _ArtifactEvidence | None,
    *,
    known_no_dispatch: bool,
) -> tuple[_CountVector, str]:
    if artifact is not None:
        return artifact[1], "artifacts"
    fallback = _manifest_counts(round_dir, loop_rounds, round_index)
    if fallback is not None:
        return fallback, "manifest"
    if known_no_dispatch:
        return (0, 0, 0, 0), "manifest"
    return (0, 0, 0, 0), "unknown"


def active_blockers(findings: list[FindingRow]) -> int:
    """Active (non-dismissed) blockers — the same quantity ``runs.blockers`` reports."""
    return sum(1 for f in findings if f.severity == "blocker" and not f.dismissed)


def consensus_stats(conn) -> tuple[int, int] | None:
    """``(active_blockers, seen_by_exactly_one)``, or ``None`` when nothing can be said.

    ``None`` means no round in the corpus had a known multi-reviewer panel. The caller must
    print NOTHING in that case: a single-reviewer corpus would report 100% consensus, which
    looks like a finding and is an artifact of the panel size.

    Multi-reviewer is detected per ROUND from ``rounds.panel_size``, not per run: a resumed run
    can change panel between rounds, so run-level detection puts single-reviewer rounds in the
    denominator. Rounds whose findings are not artifact-backed are excluded too — provenance
    lives in the artifact, so a manifest-backed round has no evidence of WHO raised what and
    would count every blocker as single-reviewer by default.

    Active blockers with no provenance rows are included in the denominator via LEFT JOIN so
    the total count matches what ``--metrics`` reports as active blockers, not a provenance-
    backed subset of it.
    """
    row = conn.execute(
        """
        WITH multi_rounds AS (
            SELECT run_id, round FROM rounds
            WHERE panel_size > 1 AND counts_source = 'artifacts'
        ),
        active AS (
            SELECT f.run_id, f.round, f.idx FROM findings f
            JOIN multi_rounds mr ON mr.run_id = f.run_id AND mr.round = f.round
            WHERE f.severity = 'blocker' AND f.dismissed = 0
        ),
        seen AS (
            SELECT a.run_id, a.round, a.idx, COUNT(DISTINCT p.reviewer_name) n
            FROM active a
            LEFT JOIN finding_provenance p
              ON p.run_id = a.run_id AND p.round = a.round AND p.idx = a.idx
            GROUP BY 1, 2, 3
        )
        SELECT COUNT(*), COALESCE(SUM(CASE WHEN n = 1 THEN 1 ELSE 0 END), 0) FROM seen
        """
    ).fetchone()
    total, single = int(row[0]), int(row[1])
    return (total, single) if total else None


def blocker_curve(conn) -> list[tuple[int, int, int]]:
    """``(round_index, active_blockers, rounds_that_reached_it)``, ascending.

    Numerator AND denominator come from the same ``rounds`` rows, so the headline and the curve
    cannot contradict each other — a run could previously report "2 blockers" and
    "round 1: 0 blockers" together, because the two read different sources.

    The denominator is not decoration. The raw total falls sharply across rounds and reads as
    convergence; it is SURVIVORSHIP, since fewer runs reach each round. Returning both forces a
    renderer to show density, which this repo published wrongly once already.

    ``counts_source='unknown'`` rounds are EXCLUDED. Their blocker count is 0 by convention
    (no evidence either way), not by measurement — including them fabricates zeroes in the curve
    for killed/interrupted runs that left no manifest or artifact. Manifest-backed rounds ARE
    included: they have a measured count, just no per-finding provenance.
    """
    rows = conn.execute(
        "SELECT round, COALESCE(SUM(blockers), 0), COUNT(*) FROM rounds "
        "WHERE counts_source != 'unknown' AND NOT (panel_source = 'recorded' "
        "AND panel_size = 0 AND blockers = 0 AND minors = 0 AND nits = 0 AND dismissed = 0) "
        "GROUP BY round ORDER BY round"
    ).fetchall()
    return [(int(r), int(n), int(d)) for r, n, d in rows]


def incomplete_rounds(conn) -> int:
    """Rounds whose finding counts are not artifact-backed — reported, never silently zeroed.

    Excludes deliberate no-dispatch rounds (panel explicitly recorded as empty with no blockers):
    those have no synthesizer by design, not because one is missing. The signature is
    ``panel_source='recorded' AND panel_size=0`` plus an all-zero vector — a round where a
    manifest explicitly recorded zero reviewers and zero findings, such as
    ``no_changes_to_review``.
    """
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM rounds WHERE counts_source != 'artifacts' "
            "AND NOT (counts_source = 'manifest' AND panel_source = 'recorded' AND panel_size = 0 "
            "AND blockers = 0 AND minors = 0 AND nits = 0 AND dismissed = 0)"
        ).fetchone()[0]
    )
