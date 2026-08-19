"""PR-h-12 item 3 — findings and provenance land in the derived view.

The question that justifies a second reviewer — *which reviewer found what, and would one alone
have shipped?* — used to need a throwaway mining script over raw JSON. These tables make it SQL.

**The count-agreement hazard is the item, not a footnote.** ``runs.blockers`` sums the LOOP
manifest's embedded round summaries; these rows come from each round's synthesizer artifact. Two
paths to one fact drift. Measured over 421 real runs before choosing what to do: 105 agree, 30
disagree, and every one of the 30 lacks a ``loop-manifest.json`` — so its ``runs.blockers`` is 0
because nothing summarised the run, not because it found nothing. Hence a warning scoped to
COMPLETED runs rather than a hard failure, which would have let one interrupted run poison a
whole backfill.
"""

from __future__ import annotations

import json
from pathlib import Path

from syncade.metrics.aggregate import backfill
from syncade.metrics.findings import active_blockers, blocker_curve, read_findings, read_rounds
from syncade.metrics.schema import open_db


def _round(
    run: Path, index: int, findings: list[dict], *, blocker_count: int | None = None
) -> None:
    d = run / f"round-{index}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "synthesizer.parsed.json").write_text(json.dumps({"consolidated_findings": findings}))
    active = (
        blocker_count
        if blocker_count is not None
        else sum(1 for f in findings if f.get("severity") == "blocker" and not f.get("dismissed"))
    )
    (d / "manifest.json").write_text(json.dumps({"synthesizer": {"active_blocker_count": active}}))


def _corpus(tmp_path: Path, *, complete: bool = True, blocker_count: int | None = None) -> Path:
    """A two-round run: one blocker seen by both reviewers, one by a single reviewer, one
    dismissed. That shape is what the consensus query has to distinguish."""
    runs = tmp_path / ".syncade" / "runs"
    run = runs / "2026-01-01T00-00-00"
    _round(
        run,
        0,
        [
            {
                "severity": "blocker",
                "dismissed": False,
                "file": "a.py",
                "description": "both reviewers",
                "provenance": [
                    {"reviewer_name": "r1", "original_severity": "blocker", "original_index": 0},
                    {"reviewer_name": "r2", "original_severity": "blocker", "original_index": 3},
                ],
            },
            {
                "severity": "blocker",
                "dismissed": False,
                "file": "b.py",
                "description": "one reviewer only",
                "provenance": [
                    {"reviewer_name": "r1", "original_severity": "blocker", "original_index": 1}
                ],
            },
        ],
    )
    _round(
        run,
        1,
        [
            {
                "severity": "blocker",
                "dismissed": True,  # dismissed: must not count as active
                "file": "c.py",
                "description": "dismissed",
                "provenance": [
                    {"reviewer_name": "r2", "original_severity": "blocker", "original_index": 0}
                ],
            },
            {"severity": "minor", "dismissed": False, "file": "d.py", "description": "a minor"},
        ],
        blocker_count=blocker_count,
    )
    (run / "run-init.json").write_text("{}")
    if complete:
        rounds = [
            {
                "synthesizer": json.loads((run / f"round-{i}" / "manifest.json").read_text())[
                    "synthesizer"
                ]
            }
            for i in (0, 1)
        ]
        (run / "loop-manifest.json").write_text(
            json.dumps({"run_id": run.name, "final_exit_code": 0, "rounds": rounds})
        )
    return tmp_path


def test_both_tables_are_populated_from_the_synthesizer_artifact(tmp_path: Path) -> None:
    repo = _corpus(tmp_path)
    conn = open_db(tmp_path / "m.db")
    backfill(conn, repo / ".syncade" / "runs")

    rows = conn.execute(
        "SELECT round, idx, severity, dismissed, file FROM findings ORDER BY round, idx"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (0, 0, "blocker", 0, "a.py"),
        (0, 1, "blocker", 0, "b.py"),
        (1, 0, "blocker", 1, "c.py"),
        (1, 1, "minor", 0, "d.py"),
    ]
    prov = conn.execute(
        "SELECT round, idx, reviewer_name, original_index FROM finding_provenance"
        " ORDER BY round, idx, reviewer_name"
    ).fetchall()
    assert [tuple(p) for p in prov] == [
        (0, 0, "r1", 0),
        (0, 0, "r2", 3),
        (0, 1, "r1", 1),
        (1, 0, "r2", 0),
    ]


def test_the_consensus_figure_is_reproducible_as_sql(tmp_path: Path) -> None:
    """The acceptance criterion: no mining script. One active blocker was seen by both reviewers
    and one by a single reviewer, so consensus is 1 of 2 — and the DISMISSED blocker must not
    appear, or the denominator silently inflates."""
    repo = _corpus(tmp_path)
    conn = open_db(tmp_path / "m.db")
    backfill(conn, repo / ".syncade" / "runs")

    total, single = conn.execute(
        """
        WITH active AS (
            SELECT run_id, round, idx FROM findings WHERE severity='blocker' AND dismissed=0
        ), seen AS (
            SELECT a.run_id, a.round, a.idx, COUNT(DISTINCT p.reviewer_name) n
            FROM active a LEFT JOIN finding_provenance p
              ON p.run_id=a.run_id AND p.round=a.round AND p.idx=a.idx
            GROUP BY 1,2,3
        )
        SELECT COUNT(*), SUM(CASE WHEN n=1 THEN 1 ELSE 0 END) FROM seen
        """
    ).fetchone()
    assert (total, single) == (2, 1)


def test_the_count_agrees_with_runs_blockers(tmp_path: Path) -> None:
    """The hazard the brief calls the point of the item: one fact, two writers."""
    repo = _corpus(tmp_path)
    conn = open_db(tmp_path / "m.db")
    backfill(conn, repo / ".syncade" / "runs")

    stored = conn.execute("SELECT blockers FROM runs").fetchone()[0]
    derived = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE severity='blocker' AND dismissed=0"
    ).fetchone()[0]
    assert stored == derived == 2


def test_blockers_derived_from_findings_when_manifest_disagrees(tmp_path: Path) -> None:
    """runs.blockers must reflect findings rows, not the loop-manifest count.

    The loop-manifest's embedded active_blocker_count is a second path to the same fact and
    will drift. backfill() now derives runs.blockers from the findings rows, making them the
    authoritative source and eliminating the disagreement class.
    """
    repo = _corpus(tmp_path, blocker_count=99)  # loop manifest claims 99 in round 1
    conn = open_db(tmp_path / "m.db")
    backfill(conn, repo / ".syncade" / "runs")

    stored = conn.execute("SELECT blockers FROM runs").fetchone()[0]
    # findings rows say 2 active blockers; manifest says 0+99=101 — findings win
    assert stored == 2, "runs.blockers must be derived from findings rows, not the loop-manifest"


def test_interrupted_run_blockers_derived_from_findings(tmp_path: Path) -> None:
    """An interrupted run (no loop-manifest) gets its blocker count from findings rows.

    Before this fix, runs.blockers came from the loop-manifest (_sum) and was 0 for any
    interrupted run, while findings rows captured the real count. Now findings always win."""
    repo = _corpus(tmp_path, complete=False)
    conn = open_db(tmp_path / "m.db")
    backfill(conn, repo / ".syncade" / "runs")

    stored = conn.execute("SELECT blockers FROM runs").fetchone()[0]
    derived = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE severity='blocker' AND dismissed=0"
    ).fetchone()[0]
    assert stored == derived


def test_missing_synth_artifact_preserves_manifest_blocker_count(tmp_path: Path, capsys) -> None:
    """A completed run whose synthesizer artifact is missing must not have blockers zeroed out.

    When backfill() found no findings rows (because synthesizer.parsed.json is absent or corrupt),
    active_blockers([]) returned 0 and overwrote runs.blockers unconditionally, silently converting
    artifact loss into a clean-looking metrics report. The fix: if the derived count is lower than
    the manifest count AND some round dirs lack parseable artifacts, preserve the manifest count
    and emit a warning rather than overwriting with zero.
    """
    runs = tmp_path / ".syncade" / "runs"
    run = runs / "2026-06-06T00-00-00"
    (run / "round-0").mkdir(parents=True)
    (run / "run-init.json").write_text("{}")
    # Synthesizer artifact is MISSING — simulate loss or corruption
    (run / "loop-manifest.json").write_text(
        json.dumps(
            {
                "run_id": run.name,
                "final_exit_code": 30,
                "rounds": [{"synthesizer": {"active_blocker_count": 2}}],
            }
        )
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    stored = conn.execute("SELECT blockers FROM runs").fetchone()[0]
    assert stored == 2, (
        "manifest-reported blockers must be preserved when synthesizer artifact is missing"
    )
    # Artifact loss is surfaced in the REPORT rather than on stderr during backfill: the
    # operator reads --metrics every time, and reads backfill stderr approximately never. Same
    # guarantee, a surface where it is actually seen.
    from syncade.cli.metrics_mode import render_report

    assert "no synthesizer artifact" in render_report(conn, last_n=None), (
        "artifact loss must be visible to the operator"
    )


def test_a_removed_run_takes_its_findings_with_it(tmp_path: Path) -> None:
    """The tables are a derived VIEW. A run that leaves the corpus must not leave rows behind, or
    every consensus query silently counts a run that no longer exists."""
    repo = _corpus(tmp_path)
    runs_root = repo / ".syncade" / "runs"
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs_root)
    assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 4

    import shutil

    shutil.rmtree(runs_root / "2026-01-01T00-00-00")
    backfill(conn, runs_root)
    assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM finding_provenance").fetchone()[0] == 0


def test_a_duplicate_reviewer_on_one_finding_cannot_undercount(tmp_path: Path) -> None:
    """The PK is (run, round, idx, reviewer_name). A repeated reviewer would overwrite its own
    row, so consensus would read 1-of-2 as 1-of-1 — dropping the duplicate explicitly keeps the
    decision here rather than in INSERT OR REPLACE."""
    run = tmp_path / ".syncade" / "runs" / "2026-02-02T00-00-00"
    _round(
        run,
        0,
        [
            {
                "severity": "blocker",
                "dismissed": False,
                "file": "a.py",
                "description": "d",
                "provenance": [
                    {"reviewer_name": "r1", "original_severity": "blocker", "original_index": 0},
                    {"reviewer_name": "r1", "original_severity": "minor", "original_index": 9},
                ],
            }
        ],
    )
    findings, provenance = read_findings(run, run.name)
    assert len(findings) == 1
    assert len(provenance) == 1, "a duplicate reviewer must not create a second row"
    assert provenance[0].original_index == 0, "the first entry wins, not the last"


def test_malformed_artifacts_never_abort_a_run(tmp_path: Path) -> None:
    """Legacy and partial runs are normal in this corpus. A bad artifact yields nothing for that
    round rather than losing the whole run."""
    run = tmp_path / ".syncade" / "runs" / "2026-03-03T00-00-00"
    (run / "round-0").mkdir(parents=True)
    (run / "round-0" / "synthesizer.parsed.json").write_text("{ not json")
    (run / "round-1").mkdir()
    (run / "round-1" / "synthesizer.parsed.json").write_text(
        json.dumps({"consolidated_findings": "nope"})
    )
    _round(
        run, 2, [{"severity": "blocker", "dismissed": False, "file": "z.py", "description": "ok"}]
    )

    findings, _ = read_findings(run, run.name)
    assert [f.round for f in findings] == [2]
    assert active_blockers(findings) == 1


def test_rounds_past_nine_are_ordered_numerically(tmp_path: Path) -> None:
    """A lexical sort puts round-10 before round-2. No run in the corpus has reached it, but the
    ceiling is 10 rounds, so the ordering is reachable."""
    run = tmp_path / ".syncade" / "runs" / "2026-04-04T00-00-00"
    for i in (2, 10):
        _round(
            run,
            i,
            [{"severity": "minor", "dismissed": False, "file": f"{i}.py", "description": "d"}],
        )
    findings, _ = read_findings(run, run.name)
    assert [f.round for f in findings] == [2, 10]


def test_active_blockers_excludes_dismissed_and_non_blockers(tmp_path: Path) -> None:
    """`active_blockers` is what decides count agreement, so its filter needs its own test.

    Every other assertion in this file reaches the same quantity through SQL with its own WHERE
    clause, so a mutation that made this function count dismissed findings survived them all —
    the two would simply disagree in a direction nothing checked.
    """
    run = tmp_path / ".syncade" / "runs" / "2026-05-05T00-00-00"
    _round(
        run,
        0,
        [
            {"severity": "blocker", "dismissed": False, "file": "a.py", "description": "active"},
            {"severity": "blocker", "dismissed": True, "file": "b.py", "description": "dismissed"},
            {
                "severity": "minor",
                "dismissed": False,
                "file": "c.py",
                "description": "not a blocker",
            },
        ],
    )
    findings, _ = read_findings(run, run.name)
    assert len(findings) == 3
    assert active_blockers(findings) == 1, "only the active blocker counts"


# ----------------------------------------------------------- the dogfood's three blockers


def test_the_headline_and_the_curve_cannot_disagree(tmp_path: Path) -> None:
    """Round 3, unanimous: a run could report "2 blockers" in the headline and
    "round 1: 0 blockers" in the curve, because the two read different sources.

    They now read the SAME per-round rows, so the contradiction is unrepresentable rather than
    fixed — which is the point of collapsing three tables into one authority.
    """
    runs = tmp_path / ".syncade" / "runs"
    run = runs / "2026-01-01T00-00-00"
    (run / "round-0").mkdir(parents=True)
    (run / "run-init.json").write_text("{}")
    # The artifact is GONE; only the manifest proves what this round found.
    (run / "round-0" / "manifest.json").write_text(
        json.dumps({"synthesizer": {"active_blocker_count": 2}})
    )
    (run / "loop-manifest.json").write_text(
        json.dumps({"run_id": run.name, "final_exit_code": 30, "rounds": [{}]})
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    headline = conn.execute("SELECT SUM(blockers) FROM runs").fetchone()[0]
    curve = blocker_curve(conn)
    assert headline == 2
    assert curve == [(0, 2, 1)], "the curve must report the same 2, not a false zero"
    assert conn.execute("SELECT blockers_source FROM rounds").fetchone()[0] == "manifest"


def test_a_string_dismissed_value_does_not_dismiss_a_blocker(tmp_path: Path) -> None:
    """`dismissed="false"` is a TRUTHY string. Truth-testing it marked an ACTIVE blocker
    dismissed and quietly shrank every count derived from it."""
    run = tmp_path / ".syncade" / "runs" / "2026-02-02T00-00-00"
    _round(
        run,
        0,
        [{"severity": "blocker", "dismissed": "false", "file": "a.py", "description": "d"}],
    )
    findings, _ = read_findings(run, run.name)
    assert findings[0].dismissed == 0
    assert active_blockers(findings) == 1


def test_provenance_from_a_reviewer_outside_the_panel_is_dropped(tmp_path: Path) -> None:
    """Ghost provenance inflates consensus — the one ratio this feature exists to report. A
    reviewer absent from the round's panel cannot have raised a finding in it."""
    run = tmp_path / ".syncade" / "runs" / "2026-03-03T00-00-00"
    _round(
        run,
        0,
        [
            {
                "severity": "blocker",
                "dismissed": False,
                "file": "a.py",
                "description": "d",
                "provenance": [
                    {"reviewer_name": "r1", "original_severity": "blocker", "original_index": 0},
                    {"reviewer_name": "ghost", "original_severity": "blocker", "original_index": 1},
                ],
            }
        ],
    )
    (run / "round-0" / "manifest.json").write_text(
        json.dumps({"reviewers": [{"name": "r1"}, {"name": "r2"}]})
    )
    _, provenance = read_findings(run, run.name)
    assert [p.reviewer_name for p in provenance] == ["r1"]


def test_a_partially_malformed_artifact_degrades_rather_than_undercounting(tmp_path: Path) -> None:
    """Dropping three of five entries and reporting two is a FALSE count, not a partial one.

    The round falls back to its manifest and records `blockers_source='manifest'`, so the number
    is honest about where it came from. BOTH read_rounds() and read_findings() must agree: the
    latter must yield no rows for that round, not a partial subset that contradicts the former.
    """
    run = tmp_path / ".syncade" / "runs" / "2026-04-04T00-00-00"
    d = run / "round-0"
    d.mkdir(parents=True)
    (d / "synthesizer.parsed.json").write_text(
        json.dumps({"consolidated_findings": [{"severity": "blocker", "dismissed": False}, "junk"]})
    )
    (d / "manifest.json").write_text(json.dumps({"synthesizer": {"active_blocker_count": 5}}))
    (run / "run-init.json").write_text("{}")

    rows = read_rounds(run, run.name)
    assert [(r.blockers, r.blockers_source) for r in rows] == [(5, "manifest")]

    # read_findings() must yield nothing for this round: persisting the valid-looking subset
    # while read_rounds() reports manifest-backed counts leaves metrics.db internally
    # contradictory (rounds says 5 manifest-backed blockers; findings says 1 from the artifact).
    findings, provenance = read_findings(run, run.name)
    assert findings == [], "partial artifact must not produce any finding rows"
    assert provenance == [], "partial artifact must not produce any provenance rows"
