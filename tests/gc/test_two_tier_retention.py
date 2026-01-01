"""PR-18 issue 1 — two-tier retention.

The regression these tests exist for, reproduced on 2026-07-12 against the
pre-PR-18 code: seed 6 runs, `--metrics` reports 6 runs / $0.072; run
`--gc --gc-keep 2`; rebuild the derived view; `--metrics` now reports 2 runs /
$0.024. Four runs of history gone, permanently.

That is not a cleanup, it is data loss. `.syncade/metrics.db` is a *derived,
rebuildable view* over `.syncade/runs/` — and it drop-and-recreates itself on
`user_version` schema drift, so the loss fires without anyone asking for a rebuild.
GC must therefore prune transcripts (90.8% of corpus bytes) and keep everything a
verdict or a metric is computed from.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from syncade.gc import execute_gc, plan_gc
from syncade.metrics.aggregate import backfill
from syncade.metrics.schema import open_db

from ._helpers import STRUCTURED_ROUND_ARTIFACTS, make_repo, write_round, write_run


def _seed_priced_run(repo: Path, run_id: str, *, day: int) -> Path:
    """A run with the artifacts `metrics.aggregate` actually reads: run-init.json +
    loop-manifest.json at the run root, and a bulky round-0 transcript beside them."""
    run_dir = repo / ".syncade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    started = f"2026-01-{day:02d}T00:00:00Z"
    (run_dir / "run-init.json").write_text(
        json.dumps({"run_id": run_id, "started_at_utc": started, "config_snapshot": {}}),
        encoding="utf-8",
    )
    (run_dir / "loop-manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "started_at_utc": started,
                "final_exit_code": 0,
                "final_round": 0,
                "termination_reason": "ship",
                "max_rounds": 3,
                "rounds": [
                    {
                        "round": 0,
                        "reviewers": [
                            {
                                "name": "codex-reviewer",
                                "provider": "openai",
                                "model": "gpt-5.5",
                                "verdict": "SHIP",
                                "finding_count": 0,
                                "duration_seconds": 1.0,
                                "outcome": "success",
                                "tokens": 1000,
                                "cost_usd": 0.01,
                                "cost_source": "estimated",
                            }
                        ],
                        "synthesizer": {
                            "outcome": "success",
                            "provider": "openai",
                            "model": "gpt-5.5",
                            "active_blocker_count": 0,
                            "active_minor_count": 0,
                            "active_nit_count": 0,
                            "dismissed_count": 0,
                            "tokens": 100,
                            "cost_usd": 0.002,
                            "cost_source": "estimated",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    write_round(run_dir, transcript_bytes=50_000)
    return run_dir


def _rebuild_metrics(tmp_path: Path, repo: Path) -> tuple[int, float]:
    """Rebuild the derived view FROM SCRATCH — exactly what a schema bump does —
    and return (run count, total cost) straight out of sqlite."""
    db = tmp_path / f"rebuilt-{id(repo)}.db"
    if db.exists():
        db.unlink()
    conn = open_db(db)
    backfill(conn, repo / ".syncade" / "runs")
    runs, spend = conn.execute("SELECT COUNT(*), COALESCE(SUM(cost_usd), 0.0) FROM runs").fetchone()
    conn.close()
    return runs, spend


def test_metrics_survive_a_gc_that_slims_every_run(tmp_path: Path) -> None:
    """THE acceptance criterion: after a GC, `--metrics` still reports every
    historical run's counts and spend. Only subprocess transcripts (*.stdout /
    *.stderr) are gone; all structured artifacts survive."""
    repo = make_repo(tmp_path)
    for day in range(1, 7):
        _seed_priced_run(repo, f"2026-01-{day:02d}T00-00-00", day=day)

    before_runs, before_spend = _rebuild_metrics(tmp_path, repo)
    assert before_runs == 6
    assert round(before_spend, 3) == 0.072

    # keep=0 → every run is a slim candidate; the harshest possible GC.
    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path / "wt")
    report = execute_gc(plan, dry_run=False, repo_root=repo)
    assert len(report.runs_slimmed) == 6
    assert report.bytes_freed > 250_000  # 6 × ~50 KB of transcript

    after_runs, after_spend = _rebuild_metrics(tmp_path, repo)
    assert after_runs == before_runs, "GC destroyed run history the metrics view needs"
    assert round(after_spend, 3) == round(before_spend, 3), "GC destroyed recorded spend"

    # ...and the bulk really is gone.
    for day in range(1, 7):
        rd = repo / ".syncade" / "runs" / f"2026-01-{day:02d}T00-00-00" / "round-0"
        assert not (rd / "codex-reviewer.stdout").exists()
        for name in STRUCTURED_ROUND_ARTIFACTS:
            assert (rd / name).exists(), f"GC destroyed structured artifact {name}"


def test_all_subprocess_transcripts_are_pruned(tmp_path: Path) -> None:
    """GC removes ALL *.stdout and *.stderr files under round-*/, not only
    reviewer stdout. Synthesizer, producer, and test-run transcripts are also
    bulk artifacts and must be pruned to keep the corpus bounded."""
    repo = make_repo(tmp_path)
    run_dir = write_run(repo, "2026-01-01T00-00-00", final_exit_code=0, with_round=True)
    round_dir = run_dir / "round-0"

    transcript_names = [
        "codex-reviewer.stdout",
        "codex-reviewer.stderr",
        "synthesizer.stdout",
        "synthesizer.stderr",
        "producer.stdout",
        "producer.stderr",
        "test-run.stdout",
        "test-run.stderr",
    ]
    for name in transcript_names:
        assert (round_dir / name).exists(), f"test fixture missing {name}"

    execute_gc(
        plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path / "wt"),
        dry_run=False,
        repo_root=repo,
    )

    for name in transcript_names:
        assert not (round_dir / name).exists(), f"GC left transcript {name} behind"
    for name in STRUCTURED_ROUND_ARTIFACTS:
        assert (round_dir / name).exists(), f"GC wrongly deleted structured artifact {name}"


def test_slimming_is_idempotent(tmp_path: Path) -> None:
    """A second GC over already-slim runs frees nothing, reports nothing, errors
    nothing. Auto-prune (issue 2) will run this on every loop start."""
    repo = make_repo(tmp_path)
    _seed_priced_run(repo, "2026-01-01T00-00-00", day=1)
    wt = tmp_path / "wt"

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt)
    first = execute_gc(plan, dry_run=False, repo_root=repo)
    assert first.runs_slimmed == ["2026-01-01T00-00-00"]
    assert first.bytes_freed > 0

    replan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt)
    second = execute_gc(replan, dry_run=False, repo_root=repo)
    assert second.runs_slimmed == [], "already-slim run reported as slimmed again"
    assert second.bytes_freed == 0
    assert second.errors == []


def test_protected_run_keeps_its_transcripts(tmp_path: Path) -> None:
    """A resume-eligible run (no loop-manifest.json) is protected — GC must not
    touch even its transcripts, because a resume may still need them."""
    repo = make_repo(tmp_path)
    run_dir = write_run(repo, "interrupted", with_loop_manifest=False, with_round=True)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path / "wt")
    assert "interrupted" in plan.protected_run_ids
    assert "interrupted" not in plan.runs_to_slim

    report = execute_gc(plan, dry_run=False, repo_root=repo)
    assert report.runs_slimmed == []
    assert (run_dir / "round-0" / "codex-reviewer.stdout").exists()


def test_dry_run_frees_nothing_but_reports_the_bytes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    run_dir = _seed_priced_run(repo, "2026-01-01T00-00-00", day=1)
    stdout = run_dir / "round-0" / "codex-reviewer.stdout"
    size_before = stdout.stat().st_size

    report = execute_gc(
        plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path / "wt"),
        dry_run=True,
        repo_root=repo,
    )

    assert stdout.exists() and stdout.stat().st_size == size_before
    assert report.runs_slimmed == ["2026-01-01T00-00-00"]
    assert report.bytes_freed >= size_before


def test_symlinked_round_dir_cannot_steer_gc_outside_the_run(tmp_path: Path) -> None:
    """Reproduced against the first draft of `_slim_run_dir`, which DELETED this
    victim file: a `round-*` entry that is a symlink pointing anywhere on disk made
    GC unlink every `*.stdout` under the target. GC must refuse symlinks, not follow
    them."""
    repo = make_repo(tmp_path)
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    victim = victim_dir / "operator.stdout"
    victim.write_text("PRECIOUS", encoding="utf-8")

    run_dir = write_run(repo, "evil", final_exit_code=0, with_round=True)
    (run_dir / "round-evil").symlink_to(victim_dir, target_is_directory=True)

    report = execute_gc(
        plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path / "wt"),
        dry_run=False,
        repo_root=repo,
    )

    assert victim.exists(), "GC followed a symlinked round dir and deleted a file outside the run"
    # ...but the run's own real transcript is still pruned — no over-correction.
    assert not (run_dir / "round-0" / "codex-reviewer.stdout").exists()
    assert report.bytes_freed > 0


def test_symlinked_subdir_under_a_round_cannot_widen_the_blast_radius(tmp_path: Path) -> None:
    """Same escape one level deeper: a symlinked subdirectory *beneath* a legitimate
    round dir. The walk must not descend into it."""
    repo = make_repo(tmp_path)
    victim_dir = tmp_path / "victim2"
    victim_dir.mkdir()
    victim = victim_dir / "nested.stdout"
    victim.write_text("ALSO PRECIOUS", encoding="utf-8")

    run_dir = write_run(repo, "evil2", final_exit_code=0, with_round=True)
    (run_dir / "round-0" / "nested-link").symlink_to(victim_dir, target_is_directory=True)

    execute_gc(
        plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path / "wt"),
        dry_run=False,
        repo_root=repo,
    )

    assert victim.exists(), "GC descended into a symlinked subdir and deleted outside the run"
    assert not (run_dir / "round-0" / "codex-reviewer.stdout").exists()


def test_run_dir_swapped_for_symlink_between_plan_and_execute(tmp_path: Path) -> None:
    """UNANIMOUS BLOCKER, found by both reviewers on the PR-18 dogfood (run
    2026-07-12T13-31-22) and reproduced: plan against a real
    `.syncade/runs/<id>/`, swap it for a symlink to an external directory dressed
    up with valid run markers, then execute the now-stale plan. GC unlinked
    `*.stdout` OUTSIDE the corpus.

    The first symlink fix anchored containment at `run_dir.resolve()`, which is
    self-defeating: resolving a symlinked run dir makes the ATTACKER's directory the
    containment root, so every check inside it passes. Containment is now anchored to
    the resolved `.syncade/runs` corpus root — which a crafted run dir cannot move —
    and `run_dir_slimmable_now` independently refuses a symlinked run dir.
    """
    repo = make_repo(tmp_path)
    evil = tmp_path / "evil"
    (evil / "round-0").mkdir(parents=True)
    victim = evil / "round-0" / "reviewer.stdout"
    victim.write_text("OPERATOR PRECIOUS", encoding="utf-8")
    # dress the external dir up as a valid completed run
    (evil / "run-init.json").write_text('{"started_at_utc": "2026-01-01T00:00:00Z"}', "utf-8")
    (evil / "loop-manifest.json").write_text('{"final_exit_code": 0}', "utf-8")

    attacked = write_run(repo, "2026-01-01T00-00-00", final_exit_code=0, with_round=True)
    control = write_run(repo, "2026-01-02T00-00-00", final_exit_code=0, with_round=True)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path / "wt")
    assert "2026-01-01T00-00-00" in plan.runs_to_slim

    # THE RACE: swap the planned run dir for a symlink after planning.
    shutil.rmtree(attacked)
    attacked.symlink_to(evil, target_is_directory=True)

    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert victim.exists(), "GC followed a symlinked RUN DIR and deleted outside the corpus"
    assert "2026-01-01T00-00-00" not in report.runs_slimmed
    # ...and the legitimate run is still pruned — no over-correction.
    assert not (control / "round-0" / "codex-reviewer.stdout").exists()
    assert report.runs_slimmed == ["2026-01-02T00-00-00"]


def test_round_dir_swapped_for_symlink_inside_corpus_between_filter_and_walk(
    tmp_path: Path,
) -> None:
    """Intra-corpus TOCTOU: a round dir passes the `not d.is_symlink()` filter but is
    then swapped to a symlink pointing at a *protected* run's round dir inside the same
    corpus. Without the recheck at the top of _bulk_artifacts_under, os.walk would
    traverse the protected run's files and the corpus-root containment check would pass
    (the file IS inside .syncade/runs).  Run-anchored containment + TOCTOU recheck
    together block this.
    """
    repo = make_repo(tmp_path)

    # A protected (resume-eligible) run whose transcript must survive.
    protected_run = write_run(
        repo, "2026-01-01T00-00-00", with_loop_manifest=False, with_round=True
    )
    victim = protected_run / "round-0" / "codex-reviewer.stdout"
    assert victim.exists()

    # A slimmable run that will be the target of the plan.
    slimmable = write_run(repo, "2026-01-02T00-00-00", final_exit_code=0, with_round=True)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path / "wt")
    assert "2026-01-02T00-00-00" in plan.runs_to_slim
    assert "2026-01-01T00-00-00" in plan.protected_run_ids

    # THE RACE: swap the slimmable run's round-0 for a symlink to the protected run's
    # round-0, simulating a swap that happens after the listing filter but before the
    # os.walk call inside _bulk_artifacts_under.
    import shutil as _shutil

    real_round = slimmable / "round-0"
    _shutil.rmtree(real_round)
    real_round.symlink_to(protected_run / "round-0", target_is_directory=True)

    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert victim.exists(), (
        "GC followed an intra-corpus round-dir symlink and deleted a protected run's transcript"
    )
    # The slimmable run appears slim even though its round-0 symlink was refused —
    # bytes_freed may be 0 for this run since the real content was already removed.
    assert "2026-01-01T00-00-00" not in report.runs_slimmed


def test_zero_byte_transcripts_are_reported_not_silently_removed(tmp_path: Path) -> None:
    """MINOR from the PR-18 dogfood (run 2026-07-12T14-22-51, codex-reviewer).

    `execute_gc` keyed "was this run slimmed?" off `bytes_freed > 0`. Bytes are the
    wrong proxy for "did anything happen": a run whose transcripts are all zero-byte
    gets its files UNLINKED while `bytes_freed` stays 0 — so the report claimed
    nothing was slimmed while the tree was mutated, and --gc-dry-run promised the
    same. Report on artifacts REMOVED instead.
    """
    repo = make_repo(tmp_path)
    run_dir = write_run(repo, "2026-01-01T00-00-00", final_exit_code=0, with_round=True)
    # Truncate EVERY transcript to zero bytes — they are still artifacts, and unlinking
    # them still mutates the tree.
    transcripts = [
        f
        for f in (run_dir / "round-0").rglob("*")
        if f.is_file() and f.suffix in (".stdout", ".stderr")
    ]
    assert transcripts, "fixture produced no transcripts"
    for f in transcripts:
        f.write_text("", encoding="utf-8")

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path / "wt")

    # dry-run must PROMISE the removal even though it frees no bytes
    dry = execute_gc(plan, dry_run=True, repo_root=repo)
    assert dry.runs_slimmed == ["2026-01-01T00-00-00"], "dry-run hid a removal it would make"
    assert all(f.exists() for f in transcripts), "dry-run mutated the tree"

    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert not any(f.exists() for f in transcripts), "artifacts were not removed"
    assert report.runs_slimmed == ["2026-01-01T00-00-00"], (
        "GC mutated the artifact tree but reported no slimmed runs"
    )
    assert report.bytes_freed == 0  # honest: nothing was freed, but something was removed

    # ...and idempotence still holds: a genuinely slim run reports nothing.
    replan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path / "wt")
    again = execute_gc(replan, dry_run=False, repo_root=repo)
    assert again.runs_slimmed == []
    assert again.bytes_freed == 0
