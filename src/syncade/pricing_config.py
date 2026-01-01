"""Config-driven per-model pricing (PR-v2-04).

Codex reports tokens but no cost, so cost is derived here. Ships a conservative
default table so zero-config runs still estimate cost (labeled ``"estimated"``);
operators override via a ``[pricing]`` table in ``.syncade/config.toml``. Prices
are list prices as of 2026-07 (USD per 1M tokens) and go stale — override for
accuracy. Split into its own module (not ``config.py``, which is at its LOC cap).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelPrice(BaseModel):
    """USD per 1M tokens for one model."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    cached_input_per_mtok: float | None = Field(
        default=None, ge=0
    )  # unset → priced at the input rate


# List prices as of 2026-07 (USD / 1M tokens). Estimates — override via [pricing].
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-opus-4-8": ModelPrice(
        input_per_mtok=15.0, output_per_mtok=75.0, cached_input_per_mtok=1.5
    ),
    "claude-sonnet-4-6": ModelPrice(
        input_per_mtok=3.0, output_per_mtok=15.0, cached_input_per_mtok=0.3
    ),
    # No default uses gpt-5.6-sol any more (PR-28 put the reviewers + judge on it;
    # PR-29 moved both back to gpt-5.5). It STAYS priced: runs 2026-07-11T15-36-39
    # and 2026-07-12T10-04-35 ran on it, and `syncade --metrics` reads the whole
    # historical corpus — dropping the entry would silently turn those runs' cost
    # into "unknown". Prices are append-mostly for exactly this reason.
    "gpt-5.6-sol": ModelPrice(input_per_mtok=5.0, output_per_mtok=30.0, cached_input_per_mtok=0.5),
    # Codex-harness producer default.
    "gpt-5.6-terra": ModelPrice(
        input_per_mtok=2.5, output_per_mtok=15.0, cached_input_per_mtok=0.25
    ),
    "gpt-5.5": ModelPrice(input_per_mtok=1.25, output_per_mtok=10.0, cached_input_per_mtok=0.125),
    "gpt-5.4": ModelPrice(input_per_mtok=1.25, output_per_mtok=10.0, cached_input_per_mtok=0.125),
    "gpt-5.4-mini": ModelPrice(
        input_per_mtok=1.25, output_per_mtok=10.0, cached_input_per_mtok=0.125
    ),
    "gpt-5.3-codex": ModelPrice(
        input_per_mtok=1.25, output_per_mtok=10.0, cached_input_per_mtok=0.125
    ),
    "gpt-5.3-codex-spark": ModelPrice(
        input_per_mtok=1.25, output_per_mtok=10.0, cached_input_per_mtok=0.125
    ),
    "gpt-5.2": ModelPrice(input_per_mtok=1.25, output_per_mtok=10.0, cached_input_per_mtok=0.125),
    "gpt-5-codex": ModelPrice(
        input_per_mtok=1.25, output_per_mtok=10.0, cached_input_per_mtok=0.125
    ),
}


class PricingConfig(BaseModel):
    """The ``[pricing]`` config section. A user-supplied ``models`` table replaces
    the packaged default (provide the full table to override)."""

    model_config = ConfigDict(extra="forbid")

    models: dict[str, ModelPrice] = Field(default_factory=lambda: dict(DEFAULT_PRICES))

    def price_for(self, model: str) -> ModelPrice | None:
        return self.models.get(model)
