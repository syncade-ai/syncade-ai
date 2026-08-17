"""Per-run budget accounting (PR-v2-11): sum actor usage, decide when a ceiling is hit.

A leaf: every import is type-only, and nothing here constructs a ``Usage`` or reads config
beyond two attributes, so it cannot form an import cycle with ``results`` / ``config_loop``.
The loop owns the running tally and calls :func:`over_budget` at each phase boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from syncade.config_loop import LoopConfig
    from syncade.orchestrator.results import RoundResult
    from syncade.usage import Usage


def review_usages(round_result: RoundResult) -> list[Usage]:
    """Usages from the actors that run BEFORE the producer this round: each reviewer + the
    judge. The pre-producer budget check sums these so reviewers that already blew the ceiling
    don't also trigger the expensive producer leg. Test/check legs spawn no model."""
    usages = [r.usage for r in round_result.dispatch_result.results if r.usage is not None]
    synth = round_result.synth_result
    if synth is not None and synth.usage is not None:
        usages.append(synth.usage)
    return usages


def round_usages(round_result: RoundResult) -> list[Usage]:
    """Every model-actor Usage a round produced: reviewers + judge + producer. The full
    per-round contribution the loop accumulates into the run tally."""
    usages = review_usages(round_result)
    producer = round_result.producer_result
    if producer is not None and producer.usage is not None:
        usages.append(producer.usage)
    return usages


def producer_only_usages(round_result: RoundResult) -> list[Usage]:
    """Only the producer Usage from a round.

    Used when resuming a budget-aborted-before-producer run: the review bundle
    was paid for in the prior process and must not count against the fresh tally.
    """
    producer = round_result.producer_result
    if producer is not None and producer.usage is not None:
        return [producer.usage]
    return []


#: Fraction of a configured ceiling at which the loop warns, so an operator watching a long
#: run sees the stop coming while a round is still left to react in. Advisory only — it never
#: changes a verdict or an exit code.
BUDGET_WARN_FRACTION = 0.8


def approaching_budget(usages: list[Usage], loop: LoopConfig) -> str | None:
    """The ceiling the running tally is APPROACHING, or ``None``.

    Fires in the band ``[fraction x ceiling, ceiling)`` — at or past the warning line but still
    under the ceiling — so it always precedes :func:`over_budget` rather than racing it. The
    caller checks this only when ``over_budget`` returned None, so the two can never both speak
    about the same boundary.

    This matters more since the ceiling gained a DEFAULT (PR-h-field-06): without a warning the
    first thing most operators would learn about `budget_tokens` is a run stopping.

    An earlier attempt at this (abandoned on a branch, never merged) compared
    ``tally >= ceiling / FRACTION`` — i.e. 125% of the ceiling, which ``over_budget`` has
    already aborted at. It could never fire. Pinned below by a test that asserts the warning
    band lies BELOW the abort, not merely that some tally warns.
    """
    if loop.budget_tokens:
        tokens = sum(u.total_tokens for u in usages)
        if tokens >= loop.budget_tokens * BUDGET_WARN_FRACTION:
            return "budget_tokens"
    if loop.budget_usd:
        cost = sum(u.cost_usd for u in usages if u.cost_usd is not None)
        if cost >= loop.budget_usd * BUDGET_WARN_FRACTION:
            return "budget_usd"
    return None


def over_budget(usages: list[Usage], loop: LoopConfig) -> str | None:
    """The ceiling the running tally has crossed, or ``None`` (also ``None`` when no ceiling is
    active — ``budget_tokens = 0`` is the opt-out sentinel, and ``budget_usd`` defaults unset).

    Tokens are the TIGHTEST bound: ``usages`` holds only actors whose usage was recorded (the
    norm), so ``total_tokens`` is exact in the normal case and a lower bound only when an actor
    reported no usage at all (a provider envelope with no usage block — rare). The dollar tally
    is looser still: it sums only KNOWN ``cost_usd``, so an actor WITH usage but unpriceable cost
    contributes nothing to it (a LOWER BOUND — the honest limit named in the config/help; use
    ``budget_tokens`` for the hardest cap). Compares ``>=``: at the ceiling, the next phase
    would spend past it, so stop. Tokens are checked first; returns ``"budget_tokens"`` /
    ``"budget_usd"`` naming the crossed ceiling.
    """
    # 0 is the explicit opt-out, not a ceiling of zero (PR-h-field-06): with a DEFAULT
    # ceiling in place an omitted TOML key can no longer mean "unlimited", so a sentinel is
    # the only way left to say it.
    if loop.budget_tokens:
        if sum(u.total_tokens for u in usages) >= loop.budget_tokens:
            return "budget_tokens"
    if loop.budget_usd:
        cost = sum(u.cost_usd for u in usages if u.cost_usd is not None)
        if cost >= loop.budget_usd:
            return "budget_usd"
    return None
