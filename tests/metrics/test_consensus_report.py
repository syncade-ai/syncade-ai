"""PR-h-12 item 5 — consensus rate and the blocker curve, surfaced honestly.

Two numbers, one command. The consensus rate is the number that justifies a second reviewer at
all; the curve is the one this repo has already published WRONG once.

**The brief's own warning is the hard part.** Blockers per round fall steeply and read as
convergence. They are SURVIVORSHIP: fewer runs reach each round, so the denominator falls too,
and per-round density is roughly flat. A renderer that prints the raw sequence alone repeats a
correction this project already had to make in public.
"""

from __future__ import annotations

import json
from pathlib import Path

from syncade.cli.metrics_mode import render_report
from syncade.metrics.aggregate import backfill
from syncade.metrics.findings import blocker_curve, consensus_stats
from syncade.metrics.schema import open_db


def _run(runs: Path, run_id: str, reviewers: list[str], rounds: list[list[dict]]) -> None:
    d = runs / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "run-init.json").write_text("{}")
    for i, findings in enumerate(rounds):
        rd = d / f"round-{i}"
        rd.mkdir(exist_ok=True)
        (rd / "synthesizer.parsed.json").write_text(json.dumps({"consolidated_findings": findings}))
    (d / "loop-manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "final_exit_code": 0,
                "rounds": [
                    {
                        "reviewers": [
                            {"name": n, "provider": "openai", "model": "m", "finding_count": 1}
                            for n in reviewers
                        ],
                        "synthesizer": {
                            "active_blocker_count": sum(
                                1
                                for f in findings
                                if f.get("severity") == "blocker" and not f.get("dismissed")
                            )
                        },
                    }
                    for findings in rounds
                ],
            }
        )
    )


def _blocker(*reviewers: str) -> dict:
    return {
        "severity": "blocker",
        "dismissed": False,
        "file": "a.py",
        "description": "d",
        "provenance": [
            {"reviewer_name": r, "original_severity": "blocker", "original_index": 0}
            for r in reviewers
        ],
    }


def test_consensus_counts_blockers_seen_by_exactly_one_reviewer(tmp_path: Path) -> None:
    runs = tmp_path / ".syncade" / "runs"
    _run(runs, "2026-01-01T00-00-00", ["r1", "r2"], [[_blocker("r1"), _blocker("r1", "r2")]])
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    assert consensus_stats(conn) == (2, 1)
    report = render_report(conn, last_n=None)
    assert "1 of 2 active blockers (50%) were raised by exactly ONE reviewer" in report


def test_a_single_reviewer_corpus_prints_nothing(tmp_path: Path) -> None:
    """The acceptance criterion. A one-reviewer corpus would report 100% consensus — every
    blocker was seen by exactly one reviewer, trivially — which looks like a finding and is an
    artifact of the panel size. Silence is the only honest output.
    """
    runs = tmp_path / ".syncade" / "runs"
    _run(runs, "2026-01-01T00-00-00", ["solo"], [[_blocker("solo"), _blocker("solo")]])
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    assert consensus_stats(conn) is None
    report = render_report(conn, last_n=None)
    assert "consensus" not in report
    # Scoped to the consensus line: the report legitimately prints "1/1 (100%)" as a ship-rate,
    # and a blanket "100%" check fails on that instead of on the thing under test.
    assert "raised by exactly ONE reviewer" not in report


def test_a_corpus_with_no_blockers_prints_no_consensus(tmp_path: Path) -> None:
    """Zero active blockers means the rate is 0/0. Printing "0%" would assert that a second
    reviewer caught nothing, when in fact nothing was there to catch."""
    runs = tmp_path / ".syncade" / "runs"
    _run(
        runs,
        "2026-01-01T00-00-00",
        ["r1", "r2"],
        [[{"severity": "minor", "dismissed": False, "file": "a.py", "description": "d"}]],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    assert consensus_stats(conn) is None
    assert "consensus" not in render_report(conn, last_n=None)


def test_the_curve_reports_its_denominator(tmp_path: Path) -> None:
    """Survivorship: round 1 has HALF the raw blockers of round 0 while being denser per round.
    A renderer showing only the raw fall would call that convergence — the error this project
    published once and had to correct.
    """
    runs = tmp_path / ".syncade" / "runs"
    # Two runs reach round 0 (2 blockers each = 4); only one reaches round 1 (3 blockers).
    _run(runs, "2026-01-01T00-00-00", ["r1", "r2"], [[_blocker("r1"), _blocker("r2")]])
    _run(
        runs,
        "2026-01-02T00-00-00",
        ["r1", "r2"],
        [
            [_blocker("r1"), _blocker("r2")],
            [_blocker("r1"), _blocker("r2"), _blocker("r1", "r2")],
        ],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    assert blocker_curve(conn) == [(0, 4, 2), (1, 3, 1)]
    report = render_report(conn, last_n=None)
    assert "round 0: 4 blockers over 2 rounds (2.00/round)" in report
    assert "round 1: 3 blockers over 1 rounds (3.00/round)" in report, (
        "density RISES while the raw total falls — the renderer must show both"
    )


def test_an_unknown_panel_run_is_excluded_from_consensus(tmp_path: Path) -> None:
    """A run whose panel was never recorded cannot say whether its findings were single-reviewer.
    Counting it would inflate the denominator with unknowns; item 4 reports it separately."""
    runs = tmp_path / ".syncade" / "runs"
    _run(runs, "2026-01-01T00-00-00", ["r1", "r2"], [[_blocker("r1")]])
    killed = runs / "2026-01-02T00-00-00"
    (killed / "round-0").mkdir(parents=True)
    (killed / "run-init.json").write_text("{}")  # interrupted: no loop-manifest, no panel
    (killed / "round-0" / "synthesizer.parsed.json").write_text(
        json.dumps({"consolidated_findings": [_blocker("r1"), _blocker("r2")]})
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    assert consensus_stats(conn) == (1, 1), "only the run with a known panel counts"


def test_curve_denominator_includes_zero_blocker_rounds(tmp_path: Path) -> None:
    """A run that reaches a round but has zero blockers must still count in the denominator.

    The old implementation derived the denominator from finding rows, so it only counted
    runs that HAD a blocker — repeating the survivorship error. If two runs both reach round 0
    but only one has a blocker, the density is 1/2, not 1/1.
    """
    runs = tmp_path / ".syncade" / "runs"
    # run1 reaches round 0 with 1 blocker
    _run(runs, "2026-01-01T00-00-00", ["r1", "r2"], [[_blocker("r1")]])
    # run2 reaches round 0 with zero blockers — no rows in findings for this run
    _run(runs, "2026-01-02T00-00-00", ["r1", "r2"], [[]])
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    # denominator must be 2 (both runs reached round 0), not 1 (only the one with a blocker)
    assert blocker_curve(conn) == [(0, 1, 2)]


def test_interrupted_run_counts_in_blocker_curve_denominator(tmp_path: Path) -> None:
    """An interrupted run (no loop-manifest.json) has rounds_executed=0 in the runs table.

    The old denominator query used ``runs.rounds_executed``, so an interrupted run that reached
    round 0 (with a synthesizer artifact present) contributed its blocker to the numerator but
    zero to the denominator, yielding the impossible output "1 blockers over 0 rounds".
    The fix: derive the denominator from ``reached_rounds``, populated from the synthesizer
    artifact's presence, independent of loop-manifest state.
    """
    runs = tmp_path / ".syncade" / "runs"
    # Interrupted run: has a synth artifact but no loop-manifest
    interrupted = runs / "2026-01-01T00-00-00"
    (interrupted / "round-0").mkdir(parents=True)
    (interrupted / "run-init.json").write_text("{}")
    (interrupted / "round-0" / "synthesizer.parsed.json").write_text(
        json.dumps({"consolidated_findings": [_blocker("r1")]})
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    curve = blocker_curve(conn)
    assert curve == [(0, 1, 1)], (
        f"interrupted run must count in denominator; got {curve!r} — "
        "'1 blockers over 0 rounds' is impossible output"
    )


def test_dismissed_blockers_count_toward_neither_figure(tmp_path: Path) -> None:
    """A blocker the synthesizer dismissed is not something the panel caught.

    Including it inflates the consensus denominator and the curve alike, overstating what a
    second reviewer earned. Mutation-checked: dropping `dismissed = 0` from `consensus_stats`
    passed every other test in this file, because none of their fixtures had a dismissed
    finding — the filter was asserted nowhere.
    """
    runs = tmp_path / ".syncade" / "runs"
    dismissed = {
        "severity": "blocker",
        "dismissed": True,
        "file": "b.py",
        "description": "ruled out",
        "provenance": [
            {"reviewer_name": "r1", "original_severity": "blocker", "original_index": 0}
        ],
    }
    _run(runs, "2026-01-01T00-00-00", ["r1", "r2"], [[_blocker("r1"), dismissed]])
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    assert consensus_stats(conn) == (1, 1), "the dismissed blocker must not reach the denominator"
    assert blocker_curve(conn) == [(0, 1, 1)], "nor the curve"


def test_manifest_proven_round_with_missing_dir_preserves_blocker_count(tmp_path: Path) -> None:
    """A run whose loop manifest records blockers in round 1, but round-1 directory is absent.

    Before the fix, the preservation guard compared len(reached) against len(round_dirs(d)).
    With the round-1 directory missing, total_rds=1 (only round-0 dir) and len(reached)=1
    (only round-0 synth artifact), so the guard never fired and runs.blockers was overwritten
    to the derived lower bound of 0 (derived from round-0 alone, which had no blockers).
    """
    runs = tmp_path / ".syncade" / "runs"
    run_id = "2026-01-01T00-00-00"
    d = runs / run_id
    (d / "round-0").mkdir(parents=True)
    (d / "run-init.json").write_text("{}")
    # Round 0: no blockers, synth artifact present
    (d / "round-0" / "synthesizer.parsed.json").write_text(
        json.dumps({"consolidated_findings": []})
    )
    # Round 1: 2 blockers reported in the loop manifest, but the round-1 directory is absent
    (d / "loop-manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "final_exit_code": 30,
                "rounds": [
                    {"synthesizer": {"active_blocker_count": 0}},
                    {"synthesizer": {"active_blocker_count": 2}},
                ],
            }
        )
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    stored = conn.execute("SELECT blockers FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]
    assert stored == 2, (
        f"manifest-reported blocker count must be preserved when round dir is absent; got {stored}"
    )


def test_zero_blocker_round_with_missing_synth_artifact_counts_in_curve(tmp_path: Path) -> None:
    """A round that the manifest proves ran but whose synth artifact is missing still counts
    in the blocker-curve denominator.

    If two runs both reach round 0 but only one's synth artifact survives, the denominator
    must still be 2 — the curve must not silently omit the round with missing artifact.
    """
    runs = tmp_path / ".syncade" / "runs"
    # run1: round 0 present with 1 blocker (synth artifact exists)
    _run(runs, "2026-01-01T00-00-00", ["r1", "r2"], [[_blocker("r1")]])
    # run2: round 0 ran (manifest says so) but synth artifact is missing
    run2 = runs / "2026-01-02T00-00-00"
    (run2 / "round-0").mkdir(parents=True)
    (run2 / "run-init.json").write_text("{}")
    # No synthesizer.parsed.json for round 0 — artifact absent
    (run2 / "loop-manifest.json").write_text(
        json.dumps(
            {
                "run_id": "2026-01-02T00-00-00",
                "final_exit_code": 0,
                "rounds": [{"synthesizer": {"active_blocker_count": 0}}],
            }
        )
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    curve = blocker_curve(conn)
    assert curve == [(0, 1, 2)], (
        f"both runs must count in denominator even when one's synth artifact is absent; "
        f"got {curve!r}"
    )


def test_provenance_less_blocker_counts_in_consensus_denominator(tmp_path: Path) -> None:
    """An active blocker with no provenance rows must count in the consensus denominator.

    The old implementation used INNER JOIN between active findings and finding_provenance,
    so provenance-less blockers were dropped from both numerator and denominator. A report
    could then say "findings: 2 blockers" alongside "consensus: 1 of 1 active blockers
    (100%)", silently overstating the second-reviewer signal. With LEFT JOIN the
    provenance-less blocker is in the denominator (n=0, not n=1, so not counted as
    single-reviewer), yielding "1 of 2 active blockers (50%)".
    """
    runs = tmp_path / ".syncade" / "runs"
    # One blocker seen by r1 only (has provenance), one with no provenance at all (legacy artifact)
    no_prov = {"severity": "blocker", "dismissed": False, "file": "b.py", "description": "no prov"}
    _run(runs, "2026-01-01T00-00-00", ["r1", "r2"], [[_blocker("r1"), no_prov]])
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    result = consensus_stats(conn)
    assert result is not None, "multi-reviewer run must contribute to consensus"
    total, single = result
    assert total == 2, (
        f"both blockers (with and without provenance) must be in denominator; got {total}"
    )
    assert single == 1, (
        f"only the provenance-backed single-reviewer blocker counts as single; got {single}"
    )


def test_single_reviewer_round_excluded_from_consensus_in_mixed_panel_run(tmp_path: Path) -> None:
    """A round with only one reviewer must not count in the consensus denominator, even when
    the run as a whole had two reviewers (e.g. a resumed run with a changed panel).

    Run-level multi-reviewer detection (from reviewer_stats) would incorrectly include round 1's
    finding. Round-level detection (from round_panels) correctly excludes it.
    """
    runs = tmp_path / ".syncade" / "runs"
    run_id = "2026-01-01T00-00-00"
    d = runs / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "run-init.json").write_text("{}")
    # Round 0: two reviewers, one blocker seen by both
    rd0 = d / "round-0"
    rd0.mkdir()
    (rd0 / "synthesizer.parsed.json").write_text(
        json.dumps({"consolidated_findings": [_blocker("r1", "r2")]})
    )
    # Round 1: only one reviewer (resumed with different config), one blocker seen by r1 only
    rd1 = d / "round-1"
    rd1.mkdir()
    (rd1 / "synthesizer.parsed.json").write_text(
        json.dumps({"consolidated_findings": [_blocker("r1")]})
    )
    (d / "loop-manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "final_exit_code": 30,
                "rounds": [
                    {
                        "reviewers": [
                            {"name": "r1", "provider": "openai", "model": "m", "finding_count": 1},
                            {"name": "r2", "provider": "openai", "model": "m", "finding_count": 1},
                        ],
                        "synthesizer": {"active_blocker_count": 1},
                    },
                    {
                        "reviewers": [
                            {"name": "r1", "provider": "openai", "model": "m", "finding_count": 1},
                        ],
                        "synthesizer": {"active_blocker_count": 1},
                    },
                ],
            }
        )
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    result = consensus_stats(conn)
    assert result is not None, "round 0 has two reviewers and contributes to consensus"
    total, single = result
    # Only round 0's blocker (seen by both r1 and r2) is in the denominator.
    # Round 1 had only one reviewer so its finding is excluded from consensus entirely.
    assert total == 1, f"only round-0 blocker (multi-reviewer round) in denominator; got {total}"
    assert single == 0, f"round-0 blocker seen by both reviewers, not single; got {single}"


def test_unknown_source_rounds_excluded_from_blocker_curve(tmp_path: Path) -> None:
    """A killed/interrupted run with no manifest and no artifact records blockers_source='unknown'.

    Before the fix, blocker_curve() summed ALL rounds, so these runs appeared as "round 0: 0
    blockers" — a fabricated zero for a missing-data path, not a measured one.

    The fixture: two runs. Run 1 has a synth artifact (1 blocker, blockers_source='artifacts').
    Run 2 was killed before any artifact or manifest was written (blockers_source='unknown', 0
    blockers by convention).

    Expected curve: [(0, 1, 1)] — run 2 must not appear in the denominator.
    """
    runs = tmp_path / ".syncade" / "runs"
    # run 1: has a synthesizer artifact (artifact-backed)
    _run(runs, "2026-01-01T00-00-00", ["r1", "r2"], [[_blocker("r1")]])
    # run 2: killed before any manifest or artifact — blockers_source='unknown'
    killed = runs / "2026-01-02T00-00-00"
    (killed / "round-0").mkdir(parents=True)
    (killed / "run-init.json").write_text("{}")
    # No synthesizer.parsed.json, no loop-manifest.json, no round manifest.json

    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    row = conn.execute(
        "SELECT blockers_source FROM rounds WHERE run_id='2026-01-02T00-00-00'"
    ).fetchone()
    assert row is not None and row[0] == "unknown", (
        f"killed run must record blockers_source='unknown', got {row!r}"
    )

    curve = blocker_curve(conn)
    assert curve == [(0, 1, 1)], (
        f"unknown-source rounds must be excluded from blocker_curve(); got {curve!r} — "
        "a fabricated 0 in the denominator misrepresents a killed run as a measured zero"
    )
