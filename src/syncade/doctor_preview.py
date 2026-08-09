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
from syncade.diff_filter import (
    concealed_destinations,
    elide_binary_hunks,
    filter_diff_for_reviewer,
    unidentifiable_sections,
)
from syncade.doctor_types import _OK, _RED, DoctorCheck
from syncade.findings import get_findings_schema_string
from syncade.metrics.aggregate import backfill
from syncade.metrics.schema import fetch_actor_stats, fetch_runs, open_db
from syncade.orchestrator.branch_guard import current_branch_name
from syncade.orchestrator.round_no_changes import _CODEX_CHAR_CEILING
from syncade.persistence import read_last_reviewed
from syncade.prompts import load_reviewer_template_for, render_reviewer_prompt
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


def reviewer_facing_bytes(diff_text: str, config: SyncadeConfig) -> int:
    """UTF-8 size of what a reviewer would actually be handed.

    The SAME two transforms the round applies, in the same order — repo-context stripping
    then binary elision — so doctor's prediction and `[loop] max_diff_bytes`'s enforcement
    cannot disagree about what "the diff" means. Doctor's row IS the prediction here: it has
    no `run_review` downstream to be authoritative for it, so a false green sends the
    operator on to spend the live auth and producer-commit legs.
    """
    stripped = filter_diff_for_reviewer(diff_text, config.review.strip_repo_context_files)
    return len(elide_binary_hunks(stripped)[0].encode("utf-8"))


def based_diff_classify(
    repo_root: Path,
    config: SyncadeConfig,
    *,
    base_ref: str | None,
    scope: str | None,
    two_dot: bool,
) -> str:
    """Classify a based/scoped diff as ``'dispatch'``, ``'no_changes'``, or ``'malformed'``.

    - ``'dispatch'``: reviewers will run; commit-safety guards apply.
    - ``'no_changes'``: diff is known-empty; real run exits 0 before dispatch, no commit.
    - ``'malformed'``: diff has unidentifiable headers; real run exits 60 (diff_malformed),
      no commit — but this is a failure, not a benign no-op.
    - ``'too_large'``: reviewer-facing diff exceeds ``[loop] max_diff_bytes``; real run
      exits 60 (diff_too_large) before dispatch, no commit. Same shape as malformed.
    - ``'prompt_too_large'``: assembled reviewer prompt exceeds the provider character
      ceiling; real run exits 60 (prompt_too_large) before dispatch, no commit. Rendered
      with a placeholder PR doc ref, so the size is a LOWER BOUND: this classification is
      never wrong when it fires, and its absence promises nothing.

    Returns ``'dispatch'`` (conservative: guard applies) on any resolution or snapshot error
    — the plan check catches those failures with its own red; the branch check must not
    double-fire."""
    try:
        if scope is not None:
            branch = current_branch_name(repo_root)
            last = read_last_reviewed(repo_root, branch) if branch else None
            base = resolve_scope(repo_root, scope, last_reviewed_sha=last).base_sha
        else:
            base = base_ref
        if base is None:
            return "dispatch"  # full-HEAD path (no diff base), always dispatches
        snap = take_snapshot(repo_root, base_ref=base, three_dot=not two_dot)
    except Exception:
        return "dispatch"  # cannot determine; be conservative
    if config.review.strip_repo_context_files and unidentifiable_sections(snap.diff_text):
        return "malformed"
    _filtered = filter_diff_for_reviewer(snap.diff_text, config.review.strip_repo_context_files)
    _filtered_elided, _ = elide_binary_hunks(_filtered)
    if len(_filtered_elided.encode("utf-8")) > config.loop.max_diff_bytes:
        return "too_large"
    if (
        snap.base_oid is not None
        and not _filtered_elided
        and not concealed_destinations(snap.diff_text, config.review.strip_repo_context_files)
    ):
        return "no_changes"
    # Prompt-size check: render the full prompt for each reviewer and see if it exceeds
    # the provider character ceiling. Uses the placeholder "<pr-doc>" (same as check_plan
    # when pr_doc_path is unknown), so this can undercount if the template repeats
    # {pr_doc_path} — but without it the branch check would fire for prompt_too_large runs.
    _diff_text = _filtered_elided or (
        "(diff not provided; review against the full repo state at HEAD)"
    )
    _json_schema = get_findings_schema_string()
    for _reviewer in config.reviewers:
        try:
            _tmpl = load_reviewer_template_for(
                repo_root, provider=_reviewer.provider, template=_reviewer.template
            )
            _rendered = render_reviewer_prompt(
                _tmpl,
                pr_doc_path="<pr-doc>",
                diff=_diff_text,
                master_plan_path=None,
                json_schema=_json_schema,
                adversarial_lens=_reviewer.adversarial_lens,
            )
        except Exception:  # noqa: BLE001 — fail open; check_plan catches template errors
            continue
        if len(_rendered) > _CODEX_CHAR_CEILING:
            return "prompt_too_large"
    return "dispatch"


def based_diff_will_dispatch(
    repo_root: Path,
    config: SyncadeConfig,
    *,
    base_ref: str | None,
    scope: str | None,
    two_dot: bool,
) -> bool:
    """Whether a based/scoped diff will dispatch reviewers.

    Delegates to :func:`based_diff_classify`; ``'dispatch'`` → True, anything else → False."""
    return (
        based_diff_classify(repo_root, config, base_ref=base_ref, scope=scope, two_dot=two_dot)
        == "dispatch"
    )


def check_plan(
    repo_root: Path,
    config: SyncadeConfig,
    *,
    base_ref: str | None,
    scope: str | None,
    two_dot: bool = False,
    max_rounds: int | None,
) -> DoctorCheck:
    """Preview the plan a real run would execute (C1): the resolved diff base + its size,
    the exact actor set (producer runs ONLY on NO-SHIP), and the round budget. Resolves the
    base the same way the CLI does — an unresolvable ``--scope`` or a bad ``--base`` is red,
    matching the real run's exit-60 refusal. Read-only git, so inert.

    **The prompt-size portion is a LOWER BOUND, and does not claim otherwise.** doctor
    cannot know the PR doc: ``--doctor`` is a one-shot mode and the CLI rejects it beside a
    PR_DOC positional, so the reference is rendered as a placeholder. A template that
    repeats ``{pr_doc_path}`` therefore renders longer in the real run than here.

    That asymmetry is why the check is still worth having: exceeding the ceiling on a lower
    bound means the real prompt certainly exceeds it, so a RED is always a true positive.
    The converse is NOT claimed — a green plan row does not promise the prompt will fit, and
    the real run refuses cheaply before provisioning if it does not. Four dogfood rounds
    tried to make this exact (estimate -> render -> real PR-doc path -> unreachable from the
    CLI); saying what it is beats a fourth attempt at what it cannot be."""
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
            snap = take_snapshot(repo_root, base_ref=base, three_dot=not two_dot)
            # A malformed diff exits 60 before dispatch in the real run — surface it as RED
            # so the cheap-red gate skips live spend (auth/producer-commit). Only applies
            # when strip targets are configured, matching the _diff_will_dispatch condition.
            if config.review.strip_repo_context_files and unidentifiable_sections(snap.diff_text):
                return DoctorCheck(
                    "plan",
                    _RED,
                    f"diff against {base!r} has an ambiguous path header"
                    " — real run exits 60 (diff_malformed)",
                    fix=(
                        "use --two-dot if paths contain ' b/';"
                        " check strip_repo_context_files config"
                    ),
                )
            # The cap refuses before dispatch in the real run — RED, same as malformed,
            # so the cheap-red gate skips the live auth/producer-commit spend.
            _reviewed = reviewer_facing_bytes(snap.diff_text, config)
            if _reviewed > config.loop.max_diff_bytes:
                return DoctorCheck(
                    "plan",
                    _RED,
                    f"reviewer-facing diff is {_reviewed:,} bytes, over "
                    f"[loop] max_diff_bytes ({config.loop.max_diff_bytes:,})"
                    " — real run exits 60 (diff_too_large)",
                    fix=(
                        "narrow --base to a smaller range, split the PR, or raise "
                        "[loop] max_diff_bytes"
                    ),
                )
            # Exact assembled-prompt check: render the full round-0 prompt and measure it.
            # The former template+diff estimate omitted the JSON schema (~517 chars) and the
            # adversarial-lens block (~3,316 chars) — a lower bound that let doctor report OK
            # for a run that exits 60 (prompt_too_large). Fails open on template errors.
            _filtered_text = filter_diff_for_reviewer(
                snap.diff_text, config.review.strip_repo_context_files
            )
            _filtered_text, _ = elide_binary_hunks(_filtered_text)
            _diff_for_render = (
                _filtered_text
                if _filtered_text
                else "(diff not provided; review against the full repo state at HEAD)"
            )
            # A PLACEHOLDER ref, because doctor cannot know the real one: `--doctor` is a
            # one-shot mode and the CLI rejects it alongside a PR_DOC positional. That makes
            # the rendered size a LOWER BOUND — a template repeating `{pr_doc_path}` renders
            # longer in the real run than here.
            #
            # A lower bound is still worth checking, because the error is one-directional:
            # if even the lower bound exceeds the ceiling, the real prompt certainly does, so
            # a RED here is always a true positive. The converse does not hold and is not
            # claimed — passing this check does not promise the run will fit. Same idiom the
            # budget surfaces use ("a strict lower bound"), and the honest alternative to
            # predicting exactly, which four dogfood rounds showed doctor cannot do.
            _pr_doc_ref = "<pr-doc>"
            _json_schema = get_findings_schema_string()
            for _reviewer in config.reviewers:
                try:
                    _tmpl = load_reviewer_template_for(
                        repo_root, provider=_reviewer.provider, template=_reviewer.template
                    )
                    _rendered = render_reviewer_prompt(
                        _tmpl,
                        pr_doc_path=_pr_doc_ref,
                        diff=_diff_for_render,
                        master_plan_path=None,
                        json_schema=_json_schema,
                        adversarial_lens=_reviewer.adversarial_lens,
                    )
                except Exception:  # noqa: BLE001 — template errors are checked live
                    continue
                _prompt_chars = len(_rendered)
                if _prompt_chars > _CODEX_CHAR_CEILING:
                    return DoctorCheck(
                        "plan",
                        _RED,
                        f"assembled prompt for reviewer {_reviewer.name!r} is "
                        f"{_prompt_chars:,} chars, over the provider "
                        f"ceiling of {_CODEX_CHAR_CEILING:,}"
                        " — real run exits 60 (prompt_too_large)",
                        fix=(
                            "trim the reviewer template in .syncade/templates/, narrow --base, "
                            "or lower [loop] max_diff_bytes"
                        ),
                    )
            files, changed = _diff_size(snap.diff_text)
            # Name the OID the diff was actually taken against, not the ref the
            # operator typed. Under three-dot they differ whenever the branch is
            # behind its base — exactly the phantom-deletion case PR-h-02
            # increment B fixes — so `diff vs main` read as the advanced tip and
            # understated the preview in the one scenario it most matters.
            origin = base if two_dot else f"branch point of {base}"
            actual = snap.base_oid[:7] if snap.base_oid else base
            diffdesc = (
                f"diff from {actual} ({origin}): {files} file(s), {changed} changed line(s), "
                f"{_reviewed:,}B reviewed of {config.loop.max_diff_bytes:,}B allowed"
            )
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
        try:
            backfill(conn, repo_root / ".syncade" / "runs")
            runs = fetch_runs(conn)
            actor_stats = fetch_actor_stats(conn)
        finally:
            conn.close()
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
