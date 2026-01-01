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


def over_budget(usages: list[Usage], loop: LoopConfig) -> str | None:
    """The ceiling the running tally has crossed, or ``None`` (also ``None`` when no budget is
    configured — both fields default unset, so this is a no-op for the vast majority of runs).

    Tokens are the TIGHTEST bound: ``usages`` holds only actors whose usage was recorded (the
    norm), so ``total_tokens`` is exact in the normal case and a lower bound only when an actor
    reported no usage at all (a provider envelope with no usage block — rare). The dollar tally
    is looser still: it sums only KNOWN ``cost_usd``, so an actor WITH usage but unpriceable cost
    contributes nothing to it (a LOWER BOUND — the honest limit named in the config/help; use
    ``budget_tokens`` for the hardest cap). Compares ``>=``: at the ceiling, the next phase
    would spend past it, so stop. Tokens are checked first; returns ``"budget_tokens"`` /
    ``"budget_usd"`` naming the crossed ceiling.
    """
    if loop.budget_tokens is not None:
        if sum(u.total_tokens for u in usages) >= loop.budget_tokens:
            return "budget_tokens"
    if loop.budget_usd is not None:
        cost = sum(u.cost_usd for u in usages if u.cost_usd is not None)
        if cost >= loop.budget_usd:
            return "budget_usd"
    return None
