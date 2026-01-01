"""``syncade --doctor`` run-plan + cost preview (PR-v2-12 check 7).

Split out of :mod:`syncade.doctor` so the readiness checks and this data-heavier preview
(which reaches into base-resolution, the diff snapshot, and the metrics corpus) each stay
under the file-length cap. Both build :class:`~syncade.doctor_types.DoctorCheck` rows; the
shared type lives in :mod:`syncade.doctor_types` to avoid an import cycle.

- **plan:** resolves the diff base the SAME way the CLI does (``--base`` / ``--scope``,
  honoring ``--max-rounds``); an unresolvable scope or bad base is red, matching the real
  run's exit-60 refusal. Reports diff size, the actor set, and the round budget.
- **cost:** a FORWARD estimate for the planned run — reviewers + judge cost per round (they
  run every round) scaled by the round budget, from the local corpus. The producer is NOT
  folded into the per-round figure (it runs only on NO-SHIP rounds); its extra cost is noted
  separately. Runs whose reviewer/judge cost is priced from INCOMPLETE token data are
  excluded, so the figure is not falsely precise. No history → a VERY ROUGH list-price
  fallback. ``cost_usd`` is an API-equivalent VALUATION, not billed money.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from pathlib import Path

from syncade.base_resolution import BaseResolutionError, resolve_scope
from syncade.config import SyncadeConfig
from syncade.doctor_types import _OK, _RED, DoctorCheck
from syncade.metrics.aggregate import backfill
from syncade.metrics.schema import fetch_actor_stats, fetch_runs, open_db
from syncade.orchestrator.branch_guard import current_branch_name
from syncade.persistence import read_last_reviewed
from syncade.snapshot import SnapshotError, take_snapshot

# Coarse fallback ONLY (no local history): price one round from list prices at a NOMINAL
# per-actor token budget. Calibrated to observed runs (~$1-3/round) so the figure is the
# right order of magnitude, but it is a guess — flagged VERY ROUGH — because real cost scales
# with the diff the reviewers actually read.
_NOMINAL_INPUT_TOK = 200_000
_NOMINAL_OUTPUT_TOK = 40_000

# The actor roles that spend on EVERY review round (unlike the producer, which runs only on
# NO-SHIP rounds). The cost estimate is built from these so producer spend is not folded into
# the per-round figure.
_PER_ROUND_ROLES = ("reviewer", "synthesizer")


def _diff_size(diff_text: str) -> tuple[int, int]:
    """(files, changed lines) in a unified diff — ``diff --git`` headers and +/- body
    lines (excluding the ``+++``/``---`` file headers)."""
    files = changed = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            changed += 1
    return files, changed


def check_plan(
    repo_root: Path,
    config: SyncadeConfig,
    *,
    base_ref: str | None,
    scope: str | None,
    max_rounds: int | None,
) -> DoctorCheck:
    """Preview the plan a real run would execute (C1): the resolved diff base + its size,
    the exact actor set (producer runs ONLY on NO-SHIP), and the round budget. Resolves the
    base the same way the CLI does — an unresolvable ``--scope`` or a bad ``--base`` is red,
    matching the real run's exit-60 refusal. Read-only git, so inert."""
    if base_ref == "":
        return DoctorCheck(
            "plan",
            _RED,
            "--base was provided as an empty string — pass a valid commit ref",
            fix="pass a non-empty --base <ref>, or omit --base for a full-HEAD review",
        )
    effective = max_rounds if max_rounds is not None else config.loop.max_rounds
    will_commit = effective > 1
    try:
        if scope is not None:
            branch = current_branch_name(repo_root)
            last = read_last_reviewed(repo_root, branch) if branch else None
            base = resolve_scope(repo_root, scope, last_reviewed_sha=last).base_sha
        else:
            base = base_ref  # may be None: full-HEAD review with no diff base
    except BaseResolutionError as exc:
        return DoctorCheck(
            "plan",
            _RED,
            f"--scope {scope!r} cannot resolve: {exc}",
            fix="pass an explicit --base <ref>",
        )
    try:
        if base is not None:
            files, changed = _diff_size(take_snapshot(repo_root, base_ref=base).diff_text)
            diffdesc = f"diff vs {base[:7]}: {files} file(s), {changed} changed line(s)"
        else:
            # No-diff-base path still probes snapshot to catch a corrupt git index that
            # would fail the real run's take_snapshot before dispatch.
            take_snapshot(repo_root)
            diffdesc = "no diff base (reviewers see full HEAD)"
    except SnapshotError as exc:
        return DoctorCheck(
            "plan",
            _RED,
            f"cannot diff against base {base!r}: {exc}",
            fix="pass a valid --base <ref>",
        )
    actors = f"{len(config.reviewers)} reviewer(s) + judge"
    if will_commit:
        actors += ", producer on non-final NO-SHIP"
    if config.loop.test_command:
        actors += ", test-leg"
    if config.checks:
        actors += f", {len(config.checks)} check(s)"
    return DoctorCheck("plan", _OK, f"{diffdesc}; {actors}; up to {effective} round(s)")


def _review_actors(config: SyncadeConfig) -> list:
    """The every-round actors whose cost the estimate is built from: each reviewer + the
    judge. The producer is excluded (it runs only on NO-SHIP rounds and is noted separately);
    the drafter/auditor belong to other modes and never run here."""
    return [*config.reviewers, config.synthesizer]


def _unpriced_models(config: SyncadeConfig) -> list[str]:
    """Every-round models with no ``[pricing]`` entry, first-seen order. Their cost reads as
    *unknown*, never $0 — the PR-v2-24 valuation-honesty ethic applied forward."""
    unpriced: list[str] = []
    for actor in _review_actors(config):
        if actor.model not in unpriced and config.pricing.price_for(actor.model) is None:
            unpriced.append(actor.model)
    return unpriced


def _coarse_round_estimate(config: SyncadeConfig) -> float | None:
    """List-price cost of ONE reviewers + judge round at nominal per-actor token volumes, or
    ``None`` if any every-round actor is unpriced (then no honest figure exists)."""
    total = 0.0
    for actor in _review_actors(config):
        price = config.pricing.price_for(actor.model)
        if price is None:
            return None
        total += (
            _NOMINAL_INPUT_TOK / 1e6 * price.input_per_mtok
            + _NOMINAL_OUTPUT_TOK / 1e6 * price.output_per_mtok
        )
    return total


def _planned_roster(config: SyncadeConfig) -> Counter:
    """The planned every-round roster as a multiset of ``(role, model)`` — each reviewer's
    model plus the judge's. A historical run's cost is comparable only if its own per-round
    roster matches this exactly."""
    roster: Counter = Counter()
    for reviewer in config.reviewers:
        roster[("reviewer", reviewer.model)] += 1
    roster[("synthesizer", config.synthesizer.model)] += 1
    return roster


def _reviewer_judge_per_round(
    runs, actor_stats, *, expected_roster: Counter
) -> tuple[float | None, int]:
    """(reviewers + judge cost per round, n matching runs) from history, or ``(None, 0)``.

    Sums only the :data:`_PER_ROUND_ROLES` actors (they run every round; the producer is
    excluded). A run is counted only when its per-round ``(role, model)`` multiset EXACTLY
    matches ``expected_roster`` (the planned reviewers + judge). Matching on identity — not a
    bare actor COUNT — is what excludes:

    - unrelated old-model history (a run whose roster changed models — e.g. a prior gpt-5.6
      panel counted toward a gpt-5.5 estimate),
    - partial coverage (a run missing a planned reviewer),
    - a duplicate reviewer row standing in for a missing different-model one, and
    - a reviewer-only run with no judge (its multiset lacks the ``synthesizer`` entry).

    Runs with any unknown-cost (``cost_usd is None``) or incomplete-token
    (``cost_incomplete_tokens > 0``) per-round actor are also excluded, so the figure is never
    a falsely-precise number built on missing data.
    """
    rounds = {r.run_id: r.rounds_executed for r in runs}
    per_run: dict[str, list] = {}  # run_id -> [cost_sum, has_incomplete, roster_counter]
    for actor in actor_stats:
        if actor.role not in _PER_ROUND_ROLES:
            continue
        entry = per_run.setdefault(actor.run_id, [0.0, False, Counter()])
        entry[2][(actor.role, actor.model)] += 1
        if actor.cost_usd is None:
            entry[1] = True  # unknown cost is incomplete, not $0
        else:
            entry[0] += actor.cost_usd
        if actor.cost_incomplete_tokens > 0:
            entry[1] = True
        # If this actor has per-round usage tracking and appeared in fewer rounds than the
        # run executed, the run's cost average would be understated (the missing rounds are
        # treated as $0 contribution). Exclude such runs. Legacy rows (rounds_with_usage=0)
        # bypass this check so pre-schema-12 history is not silently dropped.
        expected = rounds.get(actor.run_id, 0)
        if actor.rounds_with_usage > 0 and actor.rounds_with_usage != expected:
            entry[1] = True
    total_cost = 0.0
    total_rounds = 0
    n = 0
    for run_id, (cost, incomplete, roster) in per_run.items():
        executed = rounds.get(run_id, 0)
        if incomplete or executed <= 0 or roster != expected_roster:
            continue
        total_cost += cost
        total_rounds += executed
        n += 1
    if not n:
        return None, 0
    return total_cost / total_rounds, n


def check_cost(config: SyncadeConfig, repo_root: Path, *, max_rounds: int | None) -> DoctorCheck:
    """Forward cost estimate for the PLANNED run (C4), scaled by the round budget. Built from
    the reviewers + judge cost-per-round in the local corpus (they run every round); the
    producer is NOT folded in (it runs only on NO-SHIP rounds) — its extra cost is noted for a
    committing run. With no clean history, falls back to a VERY ROUGH list-price estimate.
    Always green — a preview must not gate a run — and reads an IN-MEMORY DB so the on-disk
    ``metrics.db`` is never written (inert). Any every-round model absent from ``[pricing]``
    is named, never silently $0. Costs are an API-equivalent VALUATION, not billed money."""
    effective = max_rounds if max_rounds is not None else config.loop.max_rounds
    producer_note = "; the producer adds more on non-final NO-SHIP rounds" if effective > 1 else ""
    unpriced = _unpriced_models(config)
    if effective > 1:
        # Producer can run in loop mode — name it if its model is also unpriced.
        pmodel = config.producer.model
        if pmodel not in unpriced and config.pricing.price_for(pmodel) is None:
            unpriced.append(pmodel)
    note = (
        f"; unpriced model(s): {', '.join(unpriced)} (cost would read unknown)" if unpriced else ""
    )
    try:
        conn = open_db(":memory:")  # ephemeral: reads the corpus, writes no file
        backfill(conn, repo_root / ".syncade" / "runs")
        runs = fetch_runs(conn)
        actor_stats = fetch_actor_stats(conn)
    except (sqlite3.Error, OSError) as exc:
        return DoctorCheck("cost", _OK, f"metrics unavailable ({exc}); no forward estimate{note}")
    per_round, n = _reviewer_judge_per_round(
        runs, actor_stats, expected_roster=_planned_roster(config)
    )
    if per_round is not None:
        return DoctorCheck(
            "cost",
            _OK,
            f"~${per_round * effective:.2f} for up to {effective} round(s) — reviewers + judge "
            f"(est. ${per_round:.2f}/round over {n} prior run(s), API-equivalent; ~$0 marginal "
            f"on a subscription{producer_note}){note}",
        )
    coarse = _coarse_round_estimate(config)
    if coarse is None:
        return DoctorCheck(
            "cost",
            _OK,
            f"no clean run history and an unpriced model — cannot estimate{note}",
        )
    nominal = coarse * effective
    # Coarse range: floor(0.5×) to ceil(3×), minimum $1 for the low bound.
    # A single two-decimal amount falsely implies precision we don't have without history.
    low = max(1, math.floor(nominal * 0.5))
    high = max(low + 1, math.ceil(nominal * 3.0))
    return DoctorCheck(
        "cost",
        _OK,
        f"~${low}–${high} for up to {effective} round(s) — reviewers + judge (VERY "
        f"ROUGH: no local history, coarse range at list price ~{_NOMINAL_INPUT_TOK // 1000}K in/"
        f"{_NOMINAL_OUTPUT_TOK // 1000}K out per actor{producer_note}){note}",
    )
