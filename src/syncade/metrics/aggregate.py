"""Aggregate the ``.syncade/runs/`` artifact corpus into metrics rows.

Read-only over the artifacts: reads ``loop-manifest.json`` + ``run-init.json``
and the presence of ``handoff.md`` / ``decision-needed.md``, mapping each run to
one :class:`RunRow` plus per-reviewer :class:`ReviewerStatRow`s. Legacy/partial
runs (missing fields or whole files) degrade to defaults rather than crashing —
older runs in a real corpus predate current fields.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

from syncade.metrics.schema import (
    ActorStatRow,
    ReviewerStatRow,
    RunRow,
    upsert_actor_stat,
    upsert_reviewer_stat,
    upsert_run,
)
from syncade.synthesizer.constants import (
    SYNTHESIZER_NAME,
    SYNTHESIZER_PROVIDER,
)

# syncade exit code -> coarse verdict label (see CLAUDE.md "Exit Codes").
_VERDICT_BY_EXIT = {
    0: "SHIP",
    10: "DECISION-NEEDED",
    20: "MAX-ROUNDS",
    25: "BUDGET",
    30: "NO-SHIP",
    40: "ERROR",
    50: "CONFIG-ERROR",
    60: "ENV-ERROR",
    70: "PARSE-ERROR",
}


def _int(value: object) -> int:
    """Coerce to int, defaulting non-ints (bool/str/list/…) to 0 — a malformed
    historical artifact must not crash arithmetic or sqlite binding."""
    return value if type(value) is int else 0


def _float(value: object) -> float:
    return float(value) if type(value) in (int, float) else 0.0


def _float_or_none(value: object) -> float | None:
    return float(value) if type(value) in (int, float) else None


def _int_or_none(value: object) -> int | None:
    return value if type(value) is int else None


def _usage_int_or_none(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _usage_float_or_none(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    cost = float(value)
    return cost if math.isfinite(cost) and cost >= 0 else None


def _usage_pair(tokens_value: object, cost_value: object) -> tuple[int | None, float | None]:
    """Validate persisted usage fields from manifests.

    Tokens are the anchor for a usage block. If tokens are present but malformed
    (wrong type or negative), ignore the whole usage entry; if tokens are valid
    but cost is malformed, keep tokens and mark cost unknown downstream.
    """
    if tokens_value is None and cost_value is None:
        return None, None
    tokens = _usage_int_or_none(tokens_value)
    if tokens is None:
        return None, None
    return tokens, _usage_float_or_none(cost_value)


def _text_or_none(value: object) -> str | None:
    return value if type(value) is str else None


def _reviewer_models(init: dict) -> dict[str, str]:
    config = init.get("config_snapshot")
    roster = config.get("reviewers") if isinstance(config, dict) else None
    models: dict[str, str] = {}
    if isinstance(roster, list):
        for r in roster:
            if isinstance(r, dict) and isinstance(r.get("name"), str):
                models[r["name"]] = _text_or_none(r.get("model")) or ""
    return models


def _load_init(run_dir: Path) -> dict:
    """Load ``run-init.json`` as a dict; warn (don't silently blank) when it is
    present but unreadable — a broken model-join source should be visible, not
    quietly yield blank-model reviewer stats. A *missing* run-init is normal for
    partial/legacy runs and returns ``{}`` silently."""
    path = run_dir / "run-init.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        print(
            f"[syncade] metrics: {run_dir.name} has an unreadable run-init.json "
            "— model join skipped for this run",
            file=sys.stderr,
        )
        return {}
    return data


def read_run(run_dir: Path | str) -> tuple[RunRow, list[ReviewerStatRow]] | None:
    """Map one run directory to a :class:`RunRow` + per-reviewer stats.

    Returns ``None`` when ``loop-manifest.json`` is present but unparseable — a
    *corrupt* run, which the caller skips with a warning rather than counting it as
    a bogus UNKNOWN run. A *missing* or *parseable-but-partial* manifest is still
    tolerated — scalar fields are coerced and non-list rounds/reviewers ignored, so
    a single malformed historical artifact never crashes the whole aggregation.
    """
    run_dir = Path(run_dir)
    try:
        manifest = json.loads((run_dir / "loop-manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        manifest = {}
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    init = _load_init(run_dir)
    rounds = manifest.get("rounds")
    rounds = rounds if isinstance(rounds, list) else []
    exit_code = _int_or_none(manifest.get("final_exit_code"))

    def _sum(key: str) -> int:
        total = 0
        for r in rounds:
            synth = r.get("synthesizer") if isinstance(r, dict) else None
            if isinstance(synth, dict):
                total += _int(synth.get(key, 0))
        return total

    run_id = manifest.get("run_id")
    run_id = run_id if isinstance(run_id, str) and run_id else run_dir.name

    def _producer_outcome_count(outcome: str) -> int:
        return sum(
            1
            for r in rounds
            if isinstance(r, dict)
            and isinstance(r.get("producer"), dict)
            and r["producer"].get("outcome") == outcome
        )

    def _sum_usage() -> tuple[int | None, float | None]:
        # Sum usage across every actor — reviewers, synthesizer, producer — in
        # every round. None when NO actor reported valid usage, so legacy runs
        # stay NULL rather than showing a misleading 0.
        tokens_total = 0
        cost_total = 0.0
        tokens_present = False
        cost_present = False
        for r in rounds:
            if not isinstance(r, dict):
                continue
            reviewers = r.get("reviewers")
            actors = list(reviewers) if isinstance(reviewers, list) else []
            for key in ("synthesizer", "producer"):
                a = r.get(key)
                if isinstance(a, dict):
                    actors.append(a)
            for a in actors:
                if not isinstance(a, dict):
                    continue
                tokens, cost_usd = _usage_pair(a.get("tokens"), a.get("cost_usd"))
                if tokens is not None:
                    tokens_total += tokens
                    tokens_present = True
                if cost_usd is not None:
                    cost_total += cost_usd
                    cost_present = True
        return (
            tokens_total if tokens_present else None,
            cost_total if cost_present else None,
        )

    _tok, _cost = _sum_usage()

    row = RunRow(
        run_id=run_id,
        verdict=_VERDICT_BY_EXIT.get(exit_code, "UNKNOWN"),
        rounds_executed=len(rounds),
        blockers=_sum("active_blocker_count"),
        minors=_sum("active_minor_count"),
        nits=_sum("active_nit_count"),
        dismissed=_sum("dismissed_count"),
        final_exit_code=exit_code,
        termination_reason=_text_or_none(manifest.get("termination_reason")),
        operator_branch=_text_or_none(init.get("operator_branch")),
        handoff=1 if (run_dir / "handoff.md").exists() else 0,
        decision_needed=1 if (run_dir / "decision-needed.md").exists() else 0,
        producer_commits=_producer_outcome_count("committed"),
        producer_stalled=_producer_outcome_count("stalled"),
        producer_errors=_producer_outcome_count("subprocess_error"),
        producer_escalated=_producer_outcome_count("escalated"),
        tokens=int(_tok) if _tok is not None else None,
        cost_usd=_cost,
    )
    return row, _reviewer_stats(run_id, rounds, init)


def read_actor_stats(run_dir: Path | str) -> list[ActorStatRow]:
    """Return per-actor usage stats for one run directory.

    This is intentionally separate from :func:`read_run` so the long-standing
    ``read_run() -> (RunRow, reviewer_stats)`` contract remains stable.
    """
    run_dir = Path(run_dir)
    try:
        manifest = json.loads((run_dir / "loop-manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        manifest = {}
    except (OSError, ValueError):
        return []
    if not isinstance(manifest, dict):
        return []
    rounds = manifest.get("rounds")
    rounds = rounds if isinstance(rounds, list) else []
    run_id = manifest.get("run_id")
    run_id = run_id if isinstance(run_id, str) and run_id else run_dir.name
    return _actor_stats(run_id, rounds, _load_init(run_dir))


def _merge_cost_source(
    current: str,
    incoming: str | None,
    *,
    tokens: int | None,
    cost_usd: float | None,
) -> str:
    source = incoming or ""
    if tokens is not None and (cost_usd is None or source == "unknown"):
        return "unknown"
    if not source:
        return current
    if current in ("", source):
        return source
    if current == "unknown":
        return "unknown"
    return "estimated"


def _merge_sources(current: str, incoming: str) -> str:
    if not incoming:
        return current
    if current == "unknown" or incoming == "unknown":
        return "unknown"
    if current in ("", incoming):
        return incoming
    return "estimated"


def _actor_stats(run_id: str, rounds: list, init: dict) -> list[ActorStatRow]:
    """Aggregate usage across reviewers, synthesizer, and producer."""
    models = _reviewer_models(init)
    agg: dict[tuple[str, str, str, str], dict[str, object]] = {}

    def remember(
        *,
        role: str,
        name: str,
        provider: str,
        model: str,
        tokens_value: object,
        cost_value: object,
        cost_source_value: object,
        auth_mode_value: object = None,
    ) -> None:
        tokens, cost_usd = _usage_pair(tokens_value, cost_value)
        cost_source = _text_or_none(cost_source_value)
        auth_mode = _text_or_none(auth_mode_value)
        if tokens is None and cost_usd is None:
            return
        entry = agg.setdefault(
            (role, name, provider, model, auth_mode or ""),
            {
                "provider": provider,
                "model": model,
                "tokens": None,
                "cost_usd": None,
                "cost_incomplete_tokens": 0,
                "cost_source": "",
                "auth_mode": "",
                "rounds_with_usage": 0,
            },
        )
        # No merging: auth_mode is part of the aggregation key, so two modes for one actor
        # produce two rows and each keeps its own dollars. Merging them to "mixed" made
        # real API spend unclassified.
        entry["auth_mode"] = auth_mode or ""
        entry["rounds_with_usage"] = int(entry["rounds_with_usage"]) + 1
        if tokens is not None:
            entry["tokens"] = (entry["tokens"] or 0) + tokens
        if cost_usd is not None:
            entry["cost_usd"] = (entry["cost_usd"] or 0.0) + cost_usd
        if tokens is not None and (cost_usd is None or cost_source == "unknown"):
            entry["cost_incomplete_tokens"] = int(entry["cost_incomplete_tokens"]) + tokens
        entry["cost_source"] = _merge_cost_source(
            str(entry["cost_source"]),
            cost_source,
            tokens=tokens,
            cost_usd=cost_usd,
        )

    for rnd in rounds:
        if not isinstance(rnd, dict):
            continue
        reviewers = rnd.get("reviewers")
        if isinstance(reviewers, list):
            for rev in reviewers:
                if not isinstance(rev, dict):
                    continue
                name = _text_or_none(rev.get("name"))
                if not name:
                    continue
                remember(
                    role="reviewer",
                    name=name,
                    provider=_text_or_none(rev.get("provider")) or "",
                    model=_text_or_none(rev.get("model")) or models.get(name, ""),
                    tokens_value=rev.get("tokens"),
                    cost_value=rev.get("cost_usd"),
                    cost_source_value=rev.get("cost_source"),
                    auth_mode_value=rev.get("auth_mode"),
                )

        synth = rnd.get("synthesizer")
        if isinstance(synth, dict):
            remember(
                role="synthesizer",
                name=SYNTHESIZER_NAME,
                provider=_text_or_none(synth.get("provider")) or SYNTHESIZER_PROVIDER,
                # "" (unknown), NOT the current SYNTHESIZER_MODEL: a synth
                # artifact that recorded no model predates model provenance, and
                # attributing it to today's pin would silently rewrite historical
                # spend-by-model every time the pin moves.
                model=_text_or_none(synth.get("model")) or "",
                tokens_value=synth.get("tokens"),
                cost_value=synth.get("cost_usd"),
                cost_source_value=synth.get("cost_source"),
                auth_mode_value=synth.get("auth_mode"),
            )

        producer = rnd.get("producer")
        if isinstance(producer, dict):
            remember(
                role="producer",
                name="producer",
                provider=_text_or_none(producer.get("provider")) or "",
                model=_text_or_none(producer.get("model")) or "",
                tokens_value=producer.get("tokens"),
                cost_value=producer.get("cost_usd"),
                cost_source_value=producer.get("cost_source"),
                auth_mode_value=producer.get("auth_mode"),
            )

    return [
        ActorStatRow(
            run_id=run_id,
            role=role,
            name=name,
            provider=str(e["provider"]),
            model=str(e["model"]),
            tokens=e["tokens"] if type(e["tokens"]) is int else None,
            cost_usd=e["cost_usd"] if type(e["cost_usd"]) in (int, float) else None,
            cost_incomplete_tokens=int(e["cost_incomplete_tokens"]),
            cost_source=str(e["cost_source"]),
            auth_mode=str(e["auth_mode"]),
            rounds_with_usage=int(e["rounds_with_usage"]),
        )
        for (role, name, _provider, _model, _auth), e in agg.items()
    ]


def _reviewer_stats(run_id: str, rounds: list, init: dict) -> list[ReviewerStatRow]:
    """Aggregate per-reviewer finding counts across rounds by artifact model.

    New manifests carry ``model`` beside reviewer usage. Legacy manifests fall
    back to the run-init config snapshot keyed by reviewer name.
    """
    models = _reviewer_models(init)
    agg: dict[tuple[str, str, str], dict] = {}
    for rnd in rounds:
        if not isinstance(rnd, dict):
            continue
        reviewers = rnd.get("reviewers")
        if not isinstance(reviewers, list):
            continue
        for rev in reviewers:
            if not isinstance(rev, dict):
                continue
            name = rev.get("name")
            if not isinstance(name, str) or not name:
                continue
            provider = _text_or_none(rev.get("provider")) or ""
            model = _text_or_none(rev.get("model")) or models.get(name, "")
            entry = agg.setdefault(
                (name, provider, model),
                {
                    "provider": provider,
                    "model": model,
                    "finding_count": 0,
                    "duration_s": 0.0,
                    "tokens": None,
                    "cost_usd": None,
                    "cost_source": "",
                },
            )
            entry["finding_count"] += _int(rev.get("finding_count", 0))
            entry["duration_s"] += _float(rev.get("duration_seconds", 0))
            tokens, cost_usd = _usage_pair(rev.get("tokens"), rev.get("cost_usd"))
            if tokens is not None:
                entry["tokens"] = (entry["tokens"] or 0) + tokens
            if cost_usd is not None:
                entry["cost_usd"] = (entry["cost_usd"] or 0.0) + cost_usd
            if tokens is not None or cost_usd is not None:
                if tokens is not None and cost_usd is None:
                    source = "unknown"
                else:
                    source = _text_or_none(rev.get("cost_source")) or ""
                entry["cost_source"] = _merge_sources(str(entry["cost_source"]), source)
    return [
        ReviewerStatRow(
            run_id=run_id,
            name=name,
            provider=e["provider"],
            model=e["model"],
            finding_count=e["finding_count"],
            duration_s=e["duration_s"],
            tokens=e["tokens"],
            cost_usd=e["cost_usd"],
            cost_source=e["cost_source"],
        )
        for (name, _provider, _model), e in agg.items()
    ]


def _candidate_run_dirs(runs_root: Path | str) -> list[Path]:
    runs_root = Path(runs_root)
    if not runs_root.is_dir():
        return []
    return [
        d
        for d in sorted(runs_root.iterdir())
        if d.is_dir() and ((d / "loop-manifest.json").exists() or (d / "run-init.json").exists())
    ]


def read_runs(runs_root: Path | str) -> list[tuple[RunRow, list[ReviewerStatRow]]]:
    """Read every run directory that looks like a syncade run.

    A directory qualifies if it has ``loop-manifest.json`` OR ``run-init.json``;
    partial/interrupted runs with only ``run-init.json`` degrade to UNKNOWN rows
    rather than being silently omitted. Corrupt runs (manifest present but
    unparseable) are skipped with a stderr warning.
    """
    results: list[tuple[RunRow, list[ReviewerStatRow]]] = []
    for d in _candidate_run_dirs(runs_root):
        parsed = read_run(d)
        if parsed is None:
            print(
                f"[syncade] metrics: skipping {d.name} — unparseable loop-manifest.json",
                file=sys.stderr,
            )
            continue
        results.append(parsed)
    return results


def backfill(conn: sqlite3.Connection, runs_root: Path | str) -> None:
    """Sync the DB to the current corpus (idempotent).

    Upserts every present run, refreshes each run's reviewer set (so a changed
    roster drops stale rows), and prunes runs no longer in ``.syncade/runs/`` — so
    the DB stays a faithful derived view even after ``--gc`` slims runs.
    """
    present: list[str] = []
    for d in _candidate_run_dirs(runs_root):
        parsed = read_run(d)
        if parsed is None:
            print(
                f"[syncade] metrics: skipping {d.name} — unparseable loop-manifest.json",
                file=sys.stderr,
            )
            continue
        row, stats = parsed
        upsert_run(conn, row)
        conn.execute("DELETE FROM reviewer_stats WHERE run_id = ?", (row.run_id,))
        conn.execute("DELETE FROM actor_stats WHERE run_id = ?", (row.run_id,))
        for stat in stats:
            upsert_reviewer_stat(conn, stat)
        for stat in read_actor_stats(d):
            upsert_actor_stat(conn, stat)
        present.append(row.run_id)
    _prune_absent(conn, present)
    conn.commit()


def _prune_absent(conn: sqlite3.Connection, present_ids: list[str]) -> None:
    """Delete rows for runs no longer present in the corpus (row-at-a-time, so
    there is no SQL parameter-count ceiling on corpus size)."""
    present = set(present_ids)
    for (run_id,) in conn.execute("SELECT run_id FROM runs").fetchall():
        if run_id not in present:
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM reviewer_stats WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM actor_stats WHERE run_id = ?", (run_id,))
