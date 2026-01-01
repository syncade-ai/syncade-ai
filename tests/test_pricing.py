"""pricing_config + usage.priced() (PR-v2-04 Task 2): derive cost for providers
that report tokens but no cost (codex), from a config-driven per-model table.
Provider-reported cost (claude) passes through untouched; unknown models stay
'unknown' (never fabricated)."""

from __future__ import annotations

import pytest

from syncade.pricing_config import DEFAULT_PRICES, ModelPrice, PricingConfig
from syncade.usage import Usage, priced


def _pc():
    return PricingConfig(
        models={
            "gpt-5.5": ModelPrice(
                input_per_mtok=1.0, output_per_mtok=8.0, cached_input_per_mtok=0.1
            )
        }
    )


def test_provider_cost_is_passthrough():
    u = Usage("claude-opus-4-8", 1000, 500, cost_usd=0.09, cost_source="provider")
    assert priced(u, _pc()).cost_usd == 0.09  # unchanged; already provider-priced


def test_estimated_cost_from_table():
    # 1000 fresh input @1/Mtok + 4000 cached @0.1/Mtok + 500 output @8/Mtok
    u = Usage("gpt-5.5", input_tokens=5000, output_tokens=500, cached_input_tokens=4000)
    p = priced(u, _pc())
    assert p.cost_source == "estimated"
    assert round(p.cost_usd, 6) == round(0.001 + 0.0004 + 0.004, 6)


def test_reasoning_tokens_billed_as_output():
    u = Usage("gpt-5.5", input_tokens=0, output_tokens=100, reasoning_output_tokens=900)
    # (100 + 900) output @8/Mtok = 0.008
    assert round(priced(u, _pc()).cost_usd, 6) == 0.008


def test_unknown_model_stays_unknown():
    u = Usage("mystery-model", 1000, 500)
    p = priced(u, _pc())
    assert p.cost_usd is None and p.cost_source == "unknown"


def test_negative_usage_is_not_priced():
    with pytest.raises(ValueError):
        priced(Usage("gpt-5.5", input_tokens=-1000, output_tokens=500), _pc())
    with pytest.raises(ValueError):
        priced(Usage("claude-opus-4-8", 1000, 500, cost_usd=-0.09, cost_source="provider"), _pc())


def test_cached_rate_defaults_to_input_when_unset():
    pc = PricingConfig(models={"m": ModelPrice(input_per_mtok=2.0, output_per_mtok=6.0)})
    u = Usage("m", input_tokens=1000, output_tokens=0, cached_input_tokens=1000)
    # all 1000 input priced at the input rate (cached defaults to input) = 0.002
    assert round(priced(u, pc).cost_usd, 6) == 0.002


@pytest.mark.parametrize(
    "field",
    ["input_per_mtok", "output_per_mtok", "cached_input_per_mtok"],
)
def test_negative_price_override_is_rejected(field):
    model = {"input_per_mtok": 1.0, "output_per_mtok": 2.0, "cached_input_per_mtok": 0.1}
    model[field] = -0.1
    with pytest.raises(ValueError):
        PricingConfig(models={"m": model})


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
@pytest.mark.parametrize(
    "field",
    ["input_per_mtok", "output_per_mtok", "cached_input_per_mtok"],
)
def test_nonfinite_price_override_is_rejected(field, value):
    model = {"input_per_mtok": 1.0, "output_per_mtok": 2.0, "cached_input_per_mtok": 0.1}
    model[field] = value
    with pytest.raises(ValueError):
        PricingConfig(models={"m": model})


def test_syncade_config_rejects_nonfinite_price_override():
    from syncade.config import SyncadeConfig

    with pytest.raises(ValueError):
        SyncadeConfig(
            reviewers=[{"name": "r", "provider": "openai", "model": "m"}],
            pricing={"models": {"m": {"input_per_mtok": float("inf"), "output_per_mtok": 2.0}}},
        )


def test_priced_rejects_nonfinite_internal_price():
    price = ModelPrice.model_construct(input_per_mtok=float("inf"), output_per_mtok=0.0)
    pc = PricingConfig.model_construct(models={"m": price})
    with pytest.raises(ValueError):
        priced(Usage("m", input_tokens=1, output_tokens=0), pc)


def test_default_table_covers_the_models_syncade_uses():
    """Zero-config runs must be able to price the whole default roster, or cost
    degrades to "unknown".

    The required set is DERIVED from the live defaults, not restated here: this
    test used to hardcode gpt-5.5 as "the default codex reviewer model" and went
    stale the moment that changed. Now moving a default without adding its price
    fails the test.
    """
    from syncade.config import _PRODUCER_MODELS, SyncadeConfig
    from syncade.synthesizer.constants import SYNTHESIZER_MODEL

    required = set(_PRODUCER_MODELS.values())  # producer under BOTH harnesses
    required |= {r.model for r in SyncadeConfig().reviewers}
    required.add(SYNTHESIZER_MODEL)  # the judge
    missing = sorted(m for m in required if m not in DEFAULT_PRICES)
    assert not missing, f"zero-config default models with no price entry: {missing}"

    # models an operator commonly pins by hand
    for model in ("gpt-5-codex", "gpt-5.3-codex", "gpt-5.3-codex-spark"):
        assert model in DEFAULT_PRICES
    for model in ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.2"):
        assert model in DEFAULT_PRICES
    assert any("claude" in m for m in DEFAULT_PRICES)


def test_default_config_estimates_documented_codex_model_cost():
    u = Usage("gpt-5-codex", input_tokens=1000, output_tokens=500)
    p = priced(u, PricingConfig())
    assert p.cost_source == "estimated"
    assert p.cost_usd is not None


def test_pricing_wired_into_syncade_config():
    from syncade.config import SyncadeConfig

    # zero-config → default table present (MANDATORY field, else extra="forbid" rejects [pricing])
    c = SyncadeConfig(reviewers=[{"name": "r", "provider": "openai", "model": "gpt-5.5"}])
    assert c.pricing.price_for("gpt-5.5") is not None
    # a user [pricing] table validates + overrides
    c2 = SyncadeConfig(
        reviewers=[{"name": "r", "provider": "openai", "model": "m"}],
        pricing={"models": {"m": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}},
    )
    assert c2.pricing.price_for("m").output_per_mtok == 2.0
