"""Per-subprocess token usage + cost (PR-v2-04).

Both CLIs emit usage in the envelope syncade already captures; the adapters
discard it. These pure extractors pull it back out. Claude reports cost directly
(``total_cost_usd``); codex reports tokens only, so its cost is derived from a
pricing table (see :func:`priced` + :mod:`syncade.pricing_config`). Missing or
malformed usage degrades to ``None`` / ``"unknown"`` — never a fabricated number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from syncade.pricing_config import PricingConfig


def _required_int_field(payload: dict, key: str) -> int | None:
    """Return a required int field, or None when absent/malformed."""
    value = payload.get(key)
    if type(value) is int and value >= 0:
        return value
    return None


def _optional_int_field(payload: dict, key: str) -> int | None:
    """Return an optional int field, 0 when absent, or None when malformed."""
    if key not in payload:
        return 0
    value = payload.get(key)
    if type(value) is int and value >= 0:
        return value
    return None


def _cost_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        cost = float(value)
        if math.isfinite(cost) and cost >= 0:
            return cost
    return None


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _valid_cost(value: object) -> bool:
    if value is None:
        return True
    return _cost_or_none(value) is not None


def _has_valid_numbers(usage: Usage) -> bool:
    return (
        _nonnegative_int(usage.input_tokens)
        and _nonnegative_int(usage.output_tokens)
        and _nonnegative_int(usage.cached_input_tokens)
        and _nonnegative_int(usage.reasoning_output_tokens)
        and _valid_cost(usage.cost_usd)
    )


@dataclass(frozen=True)
class Usage:
    """Token usage + optional cost for one reviewer / synth / producer call."""

    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    cost_usd: float | None = None
    cost_source: str = "unknown"  # "provider" | "estimated" | "unknown"
    auth_mode: str = "unknown"  # "subscription" | "api" | "none" | "unknown"

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.reasoning_output_tokens

    @property
    def cost_incomplete_tokens(self) -> int:
        """Tokens whose cost we could NOT establish — 0 when fully priced, else all of them.

        The live-Usage twin of the persisted ``actor_stats.cost_incomplete_tokens`` column,
        and it MUST use the same predicate the writer does
        (:mod:`syncade.metrics.aggregate`: ``cost_usd is None or cost_source == "unknown"``).
        A retry combines a priced attempt with an unpriced one via :func:`_add_usage`,
        which keeps the partial cost but marks ``cost_source="unknown"``; a `cost_usd is
        None`-only test then calls it fully priced and drops the lower-bound hedge.

        This exists so ``billing.from_usages`` and ``billing.from_rows`` read ONE rule.
        Having the classifier in one module (PR-v2-24) was worthless while its two entry
        points still disagreed on what "priced" means — which the panel caught.
        """
        if self.cost_usd is not None and self.cost_source != "unknown":
            return 0
        return self.total_tokens

    @property
    def billed_usd(self) -> float | None:
        """Money that actually left the user's account. ``0.0`` on a subscription.

        **``cost_usd`` is not spend.** It is an API-EQUIVALENT VALUATION, and it is
        fiction whenever the call rode a subscription — which is the common case, and
        which syncade reported as spend for its entire history:

        - ``cost_source="estimated"`` (codex) prices tokens from a table. Those calls run
          on a ChatGPT plan unless the user logged in with an API key. Marginal cost: $0.
        - ``cost_source="provider"`` is no better, and that surprised me. ``claude`` emits
          ``total_cost_usd`` even on an OAuth subscription session — measured $0.1426 for
          a two-token reply that cost the user nothing. Syncade trusted it *most*.

        So billing reality is orthogonal to where the number came from, and only
        ``auth_mode`` knows it. ``None`` when we cannot tell — never a guess, per I5
        (cost is never fabricated).
        """
        if self.auth_mode == "subscription":
            return 0.0
        if self.auth_mode == "api":
            return self.cost_usd
        return None


def usage_from_claude_envelope(envelope: dict) -> Usage | None:
    """Extract usage from a ``claude -p`` terminal ``{"type":"result"}`` envelope.

    Claude reports ``total_cost_usd`` directly, so ``cost_source="provider"``.
    """
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return None
    model = envelope.get("model") if isinstance(envelope.get("model"), str) else ""
    model_usage = envelope.get("modelUsage")
    if not model and isinstance(model_usage, dict):
        model = next(iter(model_usage), "")
    cost_usd = _cost_or_none(envelope.get("total_cost_usd"))
    # claude's usage.input_tokens counts ONLY uncached input; the full input the
    # model processed also includes cache reads + cache-creation writes. Fold them
    # into input_tokens so total_tokens doesn't drop them (dogfood finding #1);
    # cached_input_tokens keeps just the cache-read subset for the cost split.
    input_tokens = _required_int_field(usage, "input_tokens")
    output_tokens = _required_int_field(usage, "output_tokens")
    cache_read = _optional_int_field(usage, "cache_read_input_tokens")
    cache_creation = _optional_int_field(usage, "cache_creation_input_tokens")
    if (
        input_tokens is None
        or output_tokens is None
        or cache_read is None
        or cache_creation is None
    ):
        return None
    return Usage(
        model=model or "",
        input_tokens=input_tokens + cache_read + cache_creation,
        output_tokens=output_tokens,
        cached_input_tokens=cache_read,
        cost_usd=cost_usd,
        cost_source="provider" if cost_usd is not None else "unknown",
    )


def usage_from_codex_events(events: list) -> Usage | None:
    """Extract usage from parsed ``codex exec --json`` JSONL events (the last
    ``turn.completed``). Tokens only — cost is derived later via :func:`priced`.
    """
    completed = [
        e
        for e in events
        if isinstance(e, dict)
        and e.get("type") == "turn.completed"
        and isinstance(e.get("usage"), dict)
    ]
    if not completed:
        return None
    usage = completed[-1]["usage"]
    input_tokens = _required_int_field(usage, "input_tokens")
    output_tokens = _required_int_field(usage, "output_tokens")
    cached_input_tokens = _optional_int_field(usage, "cached_input_tokens")
    reasoning_output_tokens = _optional_int_field(usage, "reasoning_output_tokens")
    if (
        input_tokens is None
        or output_tokens is None
        or cached_input_tokens is None
        or reasoning_output_tokens is None
    ):
        return None
    return Usage(
        model="",  # the event carries no model; the caller sets it from config
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        cost_usd=None,
        cost_source="unknown",
    )


def priced(usage: Usage, pricing: PricingConfig) -> Usage:
    """Fill ``cost_usd`` / ``cost_source``.

    Provider-reported cost (claude) passes through untouched. Otherwise cost is
    estimated from the pricing table (``cost_source="estimated"``); an unknown model
    stays ``"unknown"`` — never fabricated. Reasoning tokens bill as output (that is
    how providers bill them); cached input bills at ``cached_input_per_mtok`` or, if
    unset, the input rate.
    """
    if usage.cost_source == "provider":
        if not _has_valid_numbers(usage):
            raise ValueError("usage contains invalid token or cost fields")
        return usage
    if not _has_valid_numbers(usage):
        raise ValueError("usage contains invalid token or cost fields")
    price = pricing.price_for(usage.model)
    if price is None:
        return usage
    fresh_input = max(0, usage.input_tokens - usage.cached_input_tokens)
    cached_rate = (
        price.cached_input_per_mtok
        if price.cached_input_per_mtok is not None
        else price.input_per_mtok
    )
    cost = (
        fresh_input * price.input_per_mtok
        + usage.cached_input_tokens * cached_rate
        + (usage.output_tokens + usage.reasoning_output_tokens) * price.output_per_mtok
    ) / 1_000_000
    if not math.isfinite(cost) or cost < 0:
        raise ValueError("pricing produced invalid cost")
    return replace(usage, cost_usd=cost, cost_source="estimated")


def _add_usage(left: Usage | None, right: Usage | None) -> Usage | None:
    """Add usage from multiple paid attempts of the same subprocess actor."""
    if left is None:
        return right
    if right is None:
        return left

    if left.cost_usd is None and right.cost_usd is None:
        cost_usd = None
        cost_source = "unknown"
    else:
        cost_usd = (left.cost_usd or 0.0) + (right.cost_usd or 0.0)
        if left.cost_usd is None or right.cost_usd is None:
            cost_source = "unknown"
        elif left.cost_source == right.cost_source:
            cost_source = left.cost_source
        else:
            cost_source = "estimated"

    return Usage(
        model=left.model or right.model,
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        cached_input_tokens=left.cached_input_tokens + right.cached_input_tokens,
        reasoning_output_tokens=left.reasoning_output_tokens + right.reasoning_output_tokens,
        cost_usd=cost_usd,
        cost_source=cost_source,
        auth_mode=_merge_auth_mode(left.auth_mode, right.auth_mode),
    )


def _merge_auth_mode(left: str, right: str) -> str:
    """Combine the auth modes of two paid attempts of the SAME actor.

    Dropping this is how a RETRY silently loses its billing classification: the
    dispatcher and the synth driver both accumulate attempts through
    :func:`_add_usage`, so a single transient 429 was enough to turn a
    fully-classified cost into an unclassified one. Caught by syncade's own panel,
    unanimously — the cost was right, the label was gone.

    Both attempts are the same actor resolved from the same config and env, so they
    agree by construction. The merge is therefore defensive rather than clever: a known
    mode beats an unrecorded one; two genuinely different modes degrade to ``"mixed"``,
    which reports as neither billed nor free rather than picking a side.
    """
    if left == right:
        return left
    known = {"subscription", "api"}
    if left in known and right not in known:
        return left
    if right in known and left not in known:
        return right
    return "mixed"


def usage_for(
    raw: object,
    provider: str,
    model: str,
    pricing: PricingConfig | None,
    auth_mode: str = "unknown",
) -> Usage | None:
    """Extract + price usage from a completed subprocess result, by provider.

    ``raw`` is the ``SubprocessResult`` whose ``.stdout`` holds the provider
    envelope (the adapters already parse it, they just discard the usage). The
    authoritative ``model`` (from config; codex events carry none) is stamped on
    the result. Returns ``None`` when ``pricing`` is ``None`` (usage disabled), or
    for no result / no usage / an unknown provider — usage is best-effort
    telemetry, never load-bearing on the verdict.

    ``auth_mode`` is the RESOLVED mode this call actually ran in (not the declared one),
    and it is what turns ``cost_usd`` from a number into a fact: on a subscription the
    money billed is $0 and the priced figure is an API-equivalent valuation. Defaults to
    ``"unknown"``, which reports as neither — cost is never fabricated.
    """
    if pricing is None:
        return None
    stdout = getattr(raw, "stdout", "") or ""
    if not stdout:
        return None
    if provider == "anthropic":
        try:
            from syncade.adapters.anthropic import _extract_claude_results

            envelope, _ = _extract_claude_results(stdout)
            u = usage_from_claude_envelope(envelope) if envelope else None
        except Exception:  # noqa: BLE001 - usage telemetry must never affect verdicts
            return None
    elif provider == "openai":
        try:
            from syncade.adapters.openai_parsing import _parse_jsonl_events

            u = usage_from_codex_events(_parse_jsonl_events(stdout))
        except Exception:  # noqa: BLE001 - usage telemetry must never affect verdicts
            return None
    else:
        return None
    if u is None:
        return None
    if not u.model:
        u = replace(u, model=model)
    u = replace(u, auth_mode=auth_mode)
    try:
        return priced(u, pricing)
    except Exception:  # noqa: BLE001 - pricing is best-effort telemetry
        return None


def usage_fields(usage: Usage | None) -> dict[str, object]:
    """The persistence/manifest dict fields for a usage record — all ``None`` when
    usage is absent. Keeps the reviewer / synth / producer manifest builders DRY and
    the metrics aggregator's ``rev.get("tokens")`` reads uniform across entries.

    ``auth_mode`` rides along with the cost so an artifact is self-describing: a run
    persisted today can be re-read years later and still say whether its dollars were
    money or an API-equivalent valuation. Without it on disk, ``--metrics`` would have to
    guess retroactively — and guessing is what this PR exists to stop.
    """
    if usage is None or not _has_valid_numbers(usage):
        return {"tokens": None, "cost_usd": None, "cost_source": None, "auth_mode": None}
    return {
        "tokens": usage.total_tokens,
        "cost_usd": usage.cost_usd,
        "cost_source": usage.cost_source,
        "auth_mode": usage.auth_mode,
    }


def usage_from_fields(
    tokens: object,
    cost_usd: object,
    cost_source: object,
    model: str = "",
    auth_mode: object = None,
) -> Usage | None:
    """Reconstruct a (partial) Usage from persisted manifest fields, for resume
    rehydration (finding #2). Persistence keeps only the total tokens + cost +
    source; the input/output split doesn't survive, so the total is folded into
    ``input_tokens`` — enough for the loop-manifest / metrics, which read only
    :func:`usage_fields`. Returns ``None`` when no usage was recorded.

    ``auth_mode`` MUST be rehydrated with the rest. Omitting it silently rewrote every
    completed round of a resumed run as ``"unknown"``: the dollars came back, the billing
    classification did not, and a fully-classified run became unclassified just by being
    resumed. The artifact on disk was correct; the reload destroyed it. Caught by
    syncade's own panel, unanimously.
    """
    if tokens is None and cost_usd is None:
        return None
    if type(tokens) is not int or tokens < 0:
        return None
    cost = _cost_or_none(cost_usd)
    return Usage(
        model=model,
        input_tokens=tokens,
        output_tokens=0,
        cost_usd=cost,
        cost_source=str(cost_source) if cost is not None and cost_source else "unknown",
        auth_mode=str(auth_mode) if auth_mode else "unknown",
    )


def _auth_mode(actor: object) -> str:
    """Resolved auth mode for ``actor``, for stamping onto its cost record.

    Function-local import: `auth_preflight` imports `config`, which would cycle if this
    module (imported by the adapters) pulled it in at module scope. Same rule the GC
    follows for `orchestrator`.
    """
    import os

    from syncade.auth_preflight import resolve_auth_mode

    return resolve_auth_mode(actor, dict(os.environ))  # type: ignore[arg-type]
