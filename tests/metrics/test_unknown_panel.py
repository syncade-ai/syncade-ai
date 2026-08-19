"""PR-h-12 item 4 — a ROUND whose panel was never recorded is reported as unknown, never guessed.

Counted per ROUND rather than per run since the dogfood: a resumed run can change panel
between rounds, and the note now sits OUTSIDE the reviewer-rows guard, because nested
inside it a corpus where EVERY panel was unknown printed no warning at all.

**The write side was already done, which the brief could not have known.** Measured by month over
the real corpus: 2026-05 and 2026-06 record no reviewer model at all, 2026-07 records 406 of 492,
and 2026-08 records 236 of 236. Every reviewer entry written since mid-July carries provider and
model, so "a fresh run's every reviewer entry carries both" holds today.

What was left is the REPORTING half, and the gap was not the one the brief described. No
``reviewer_stats`` row has an empty model — instead 49 runs have no rows at all, so they are
INVISIBLE in the panel breakdown rather than conflated with a single-model panel. That is worse
than being wrong: anyone dividing findings by runs gets a denominator quietly missing 12% of the
corpus. All 49 are the same interrupted runs that item 3 found disagreeing on blocker counts.
"""

from __future__ import annotations

import json
from pathlib import Path

from syncade.cli.metrics_mode import render_report
from syncade.metrics.schema import (
    ReviewerStatRow,
    RoundRow,
    RunRow,
    open_db,
    upsert_reviewer_stat,
    upsert_round,
    upsert_run,
)


def _db(tmp_path: Path):
    return open_db(tmp_path / "m.db")


def test_a_round_with_no_recorded_panel_is_counted_and_named(tmp_path: Path) -> None:
    """Counted from `rounds`, the single authority — the accounting moved there in the dogfood
    fix, so a fixture of runs and reviewer_stats alone no longer exercises it."""
    conn = _db(tmp_path)
    upsert_run(conn, RunRow(run_id="has-panel"))
    upsert_run(conn, RunRow(run_id="interrupted"))
    upsert_reviewer_stat(
        conn, ReviewerStatRow(run_id="has-panel", name="r1", provider="openai", model="gpt-5.5")
    )
    upsert_round(
        conn,
        RoundRow(
            run_id="has-panel",
            round=0,
            panel_size=2,
            blockers_source="artifacts",
            panel_source="recorded",
        ),
    )
    upsert_round(
        conn,
        RoundRow(
            run_id="interrupted",
            round=0,
            panel_size=0,
            blockers_source="unknown",
            panel_source="unknown",
        ),
    )

    report = render_report(conn, last_n=None)

    assert "openai/gpt-5.5" in report
    assert "1 of 2 rounds record no reviewer panel" in report, (
        "an unrecorded panel must be counted in the report, not silently dropped from it"
    )


def test_known_empty_panel_is_not_counted_as_unknown(tmp_path: Path) -> None:
    """A no_changes_to_review round has a manifest with a ``reviewers`` key set to [].

    panel_source='recorded' means a manifest explicitly recorded the (empty) reviewer list —
    the panel being empty is known, not "interrupted before manifest writing." It must not
    appear in the unknown-panel count.
    """
    conn = _db(tmp_path)
    upsert_run(conn, RunRow(run_id="no-dispatch"))
    upsert_round(
        conn,
        RoundRow(
            run_id="no-dispatch",
            round=0,
            panel_size=0,
            blockers_source="manifest",
            panel_source="recorded",
        ),
    )
    report = render_report(conn, last_n=None)
    assert "record no reviewer panel" not in report, (
        "a known-empty panel (panel_source='recorded', 0 reviewers) must not be counted as unknown"
    )


def test_manifest_backed_round_with_unknown_panel_is_counted(tmp_path: Path) -> None:
    """A round where a manifest recorded blocker counts but no reviewer list has unknown panel.

    blockers_source='manifest' and panel_source='unknown' are independent: the manifest has a
    count but no reviewers key. This IS an unknown panel and must appear in the warning.
    """
    conn = _db(tmp_path)
    upsert_run(conn, RunRow(run_id="partial"))
    upsert_round(
        conn,
        RoundRow(
            run_id="partial",
            round=0,
            panel_size=0,
            blockers=3,
            blockers_source="manifest",
            panel_source="unknown",
        ),
    )
    report = render_report(conn, last_n=None)
    assert "1 of 1 rounds record no reviewer panel" in report, (
        "a manifest-backed round with no reviewer list must be counted as an unknown panel"
    )


def test_no_dispatch_round_not_counted_in_incomplete_rounds(tmp_path: Path) -> None:
    """A no_changes_to_review round has no synthesizer by design, not because one is missing.

    panel_source='recorded' + panel_size=0 + blockers=0 is the no-dispatch signature.
    It must not appear in the incomplete-rounds warning that says 'blocker counts come from
    round manifests and carry no provenance' — there are no provenance-carrying blockers here.
    """
    from syncade.metrics.findings import incomplete_rounds

    conn = _db(tmp_path)
    upsert_run(conn, RunRow(run_id="no-dispatch"))
    upsert_round(
        conn,
        RoundRow(
            run_id="no-dispatch",
            round=0,
            panel_size=0,
            blockers=0,
            blockers_source="manifest",
            panel_source="recorded",
        ),
    )
    assert incomplete_rounds(conn) == 0, (
        "a deliberate no-dispatch round must not be counted as missing a synthesizer artifact"
    )
    report = render_report(conn, last_n=None)
    assert "no synthesizer artifact" not in report


def test_real_manifest_round_still_counted_in_incomplete_rounds(tmp_path: Path) -> None:
    """A real dispatch round whose synthesizer artifact is gone IS incomplete."""
    from syncade.metrics.findings import incomplete_rounds

    conn = _db(tmp_path)
    upsert_run(conn, RunRow(run_id="interrupted"))
    upsert_round(
        conn,
        RoundRow(
            run_id="interrupted",
            round=0,
            panel_size=2,
            blockers=3,
            blockers_source="manifest",
            panel_source="recorded",
        ),
    )
    assert incomplete_rounds(conn) == 1


def test_no_note_when_every_run_has_a_panel(tmp_path: Path) -> None:
    """The line must not appear when it would say zero — a permanent '0 unknown' is noise that
    trains the reader to skip the line that matters."""
    conn = _db(tmp_path)
    upsert_run(conn, RunRow(run_id="only"))
    upsert_reviewer_stat(
        conn, ReviewerStatRow(run_id="only", name="r1", provider="openai", model="gpt-5.5")
    )
    upsert_round(
        conn,
        RoundRow(
            run_id="only",
            round=0,
            panel_size=1,
            blockers_source="artifacts",
            panel_source="recorded",
        ),
    )
    assert "record no reviewer panel" not in render_report(conn, last_n=None)


def test_a_missing_model_renders_as_unknown_not_as_a_bare_provider(tmp_path: Path) -> None:
    """`openai` alone reads like a panel that had no model — a claim we cannot make. The brief's
    rule is that legacy rows are reported as unknown and NEVER guessed, so the renderer says so.
    """
    conn = _db(tmp_path)
    upsert_run(conn, RunRow(run_id="legacy"))
    upsert_reviewer_stat(
        conn, ReviewerStatRow(run_id="legacy", name="r1", provider="openai", model="")
    )

    report = render_report(conn, last_n=None)

    assert "openai/unknown" in report
    assert "gpt" not in report, "no model may be inferred from the configured roster"


def test_the_writer_records_provider_and_model_on_every_entry() -> None:
    """The write half, asserted at its source rather than trusted from the corpus.

    `_reviewer_manifest_entry` builds the round manifest's reviewer rows; both the success and
    the failure branch must carry provider and model, because a run that FAILED is exactly the
    one whose panel someone will want to reconstruct.
    """
    import inspect

    from syncade.persistence.reviewer import _reviewer_manifest_entry

    source = inspect.getsource(_reviewer_manifest_entry)
    assert source.count('"provider": run_result.provider') == 2, "success and failure branches"
    assert source.count('"model": model') == 2


def test_a_failed_reviewer_still_records_its_panel(tmp_path: Path) -> None:
    """Behavioural twin for the pin above: an errored reviewer is still a panel member, and a
    manifest that omits it makes the failed run's composition unreconstructible."""
    from syncade.persistence.reviewer import _reviewer_manifest_entry

    class _Result:
        reviewer_name = "r1"
        provider = "openai"
        model = "gpt-5.5"
        output = None
        error = RuntimeError("boom")
        duration_seconds = 1.0
        usage = None

    entry = _reviewer_manifest_entry(_Result())
    assert entry["provider"] == "openai"
    assert entry["model"] == "gpt-5.5"
    assert entry["outcome"] != "success"


def test_the_note_survives_a_round_trip_through_a_real_corpus(tmp_path: Path) -> None:
    """End to end through `backfill`, not just hand-inserted rows: an interrupted run reaches the
    report as an unknown panel rather than vanishing."""
    from syncade.metrics.aggregate import backfill

    runs = tmp_path / ".syncade" / "runs"
    done = runs / "2026-01-01T00-00-00"
    (done / "round-0").mkdir(parents=True)
    (done / "run-init.json").write_text("{}")
    (done / "loop-manifest.json").write_text(
        json.dumps(
            {
                "run_id": done.name,
                "final_exit_code": 0,
                "rounds": [
                    {
                        "reviewers": [
                            {
                                "name": "r1",
                                "provider": "openai",
                                "model": "gpt-5.5",
                                "finding_count": 1,
                            }
                        ]
                    }
                ],
            }
        )
    )
    killed = runs / "2026-01-02T00-00-00"
    (killed / "round-0").mkdir(parents=True)
    (killed / "run-init.json").write_text("{}")  # interrupted: no loop-manifest.json

    conn = _db(tmp_path)
    backfill(conn, runs)
    report = render_report(conn, last_n=None)
    assert "1 of 2 rounds record no reviewer panel" in report
