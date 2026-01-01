"""`syncade --metrics` report rendering (PR-v2-03 Task 3).

``render_report`` is pure (Connection -> text) so the cumulative numbers the
operator sees can be asserted directly against the DB rows — the "the report
matches the db" contract, unit-tested.
"""

from __future__ import annotations

from syncade.cli.metrics_mode import render_report
from syncade.metrics.schema import (
    ReviewerStatRow,
    RunRow,
    open_db,
    upsert_reviewer_stat,
    upsert_run,
)


def test_render_report_shows_cumulative_counts(tmp_path):
    conn = open_db(tmp_path / "m.db")
    upsert_run(
        conn, RunRow(run_id="R1", verdict="SHIP", rounds_executed=2, blockers=2, minors=3, nits=1)
    )
    upsert_run(conn, RunRow(run_id="R2", verdict="NO-SHIP", rounds_executed=3, blockers=5))
    report = render_report(conn)
    assert "2 runs" in report
    assert "1/2" in report  # ship-rate
    assert "7 blockers" in report  # 2 + 5
    assert "3 minors" in report
    assert "SHIP" in report
    assert "NO-SHIP" in report


def test_render_report_empty_corpus(tmp_path):
    conn = open_db(tmp_path / "m.db")
    assert "no runs" in render_report(conn).lower()


def test_render_report_groups_reviewers_by_model(tmp_path):
    conn = open_db(tmp_path / "m.db")
    upsert_run(conn, RunRow(run_id="R1", verdict="SHIP"))
    upsert_reviewer_stat(
        conn,
        ReviewerStatRow(
            run_id="R1", name="codex-reviewer", provider="openai", model="gpt-5.5", finding_count=4
        ),
    )
    report = render_report(conn)
    assert "openai/gpt-5.5" in report
    assert "findings=4" in report


def test_render_report_shows_reviewer_wall_clock(tmp_path):
    conn = open_db(tmp_path / "m.db")
    upsert_run(conn, RunRow(run_id="R1", verdict="SHIP"))
    upsert_reviewer_stat(
        conn,
        ReviewerStatRow(
            run_id="R1", name="codex-reviewer", provider="openai", model="gpt-5.5", duration_s=237.6
        ),
    )
    report = render_report(conn)
    assert "wall=" in report
    assert "237" in report  # total wall-clock surfaced per model


def test_render_report_shows_dismissed_total(tmp_path):
    conn = open_db(tmp_path / "m.db")
    upsert_run(conn, RunRow(run_id="R1", verdict="SHIP", blockers=1, dismissed=4))
    report = render_report(conn)
    assert "4 dismissed" in report


def test_render_report_counts_distinct_runs_not_reviewer_appearances(tmp_path):
    # The same model can appear under two reviewer names in ONE run (the
    # same-model default roster). runs= must count distinct runs, not rows.
    conn = open_db(tmp_path / "m.db")
    upsert_run(conn, RunRow(run_id="R1", verdict="SHIP"))
    upsert_reviewer_stat(
        conn,
        ReviewerStatRow(
            run_id="R1", name="codex-reviewer", provider="openai", model="gpt-5.5", finding_count=1
        ),
    )
    upsert_reviewer_stat(
        conn,
        ReviewerStatRow(
            run_id="R1",
            name="codex-reviewer-adv",
            provider="openai",
            model="gpt-5.5",
            finding_count=2,
        ),
    )
    report = render_report(conn)
    assert "findings=3" in report  # summed across both appearances
    assert "runs=1" in report  # one DISTINCT run, not two appearances


def test_render_report_shows_producer_activity(tmp_path):
    conn = open_db(tmp_path / "m.db")
    upsert_run(conn, RunRow(run_id="R1", verdict="SHIP", producer_commits=3))
    report = render_report(conn)
    assert "producer" in report.lower()
    assert "committed=3" in report  # committed producer rounds surfaced


def test_render_report_preserves_distinct_exit_codes(tmp_path):
    # two runs with the SAME verdict label but DIFFERENT exit codes must not collapse
    conn = open_db(tmp_path / "m.db")
    upsert_run(conn, RunRow(run_id="R1", verdict="UNKNOWN", final_exit_code=99))
    upsert_run(conn, RunRow(run_id="R2", verdict="UNKNOWN", final_exit_code=88))
    report = render_report(conn)
    assert "99" in report and "88" in report  # distinct codes both shown, not merged


def test_run_metrics_returns_clean_error_on_corrupt_db(tmp_path, monkeypatch):
    import syncade.cli.metrics_mode as mm
    from syncade.exit_codes import WORKTREE_ERROR

    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "metrics.db").write_text("not a sqlite database")
    (tmp_path / ".syncade" / "runs").mkdir()
    monkeypatch.setattr(mm, "discover_repo_root", lambda hint: tmp_path)
    args = type("Args", (), {"repo_root": str(tmp_path)})()
    # a corrupt metrics.db must yield a clean WORKTREE_ERROR, not an uncaught sqlite traceback
    assert mm._run_metrics(args) == WORKTREE_ERROR
