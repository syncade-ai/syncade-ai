"""``[retry]`` config block (PR-v2-9): the shared transient-retry bound.

A LEAF module — pydantic only. It deliberately does NOT import :mod:`syncade.retry`: that module
pulls in ``adapters.base`` → ``config`` → back here, so importing it would form a cycle. Instead the
default is a LITERAL mirror of :data:`syncade.retry.MAX_RETRIES`, kept honest by a drift test
(``tests/config/test_retry_config.py::test_default_reproduces_the_runtime_bound_exactly``) that
fails loudly if the two ever diverge — the same leaf-plus-drift-guard idiom as ``config_cold``.

The runtime bound is ``retry.MAX_RETRIES``; this exposes it as config so an operator can raise it (a
flaky provider) or drop it to 0 (fail fast, no retries) without editing code. The value is threaded
to the three legs that retry a transient provider error — reviewer dispatch, cold synthesizer, and
producer — so they cannot drift on the bound.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Drift-guarded literal mirror of syncade.retry.MAX_RETRIES (see module docstring).
_DEFAULT_MAX_RETRIES = 2


class RetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_retries: int = Field(
        default=_DEFAULT_MAX_RETRIES,
        ge=0,
        description=(
            "Extra attempts (AFTER the first) each model leg — reviewer, synthesizer, producer — "
            "rides out a TRANSIENT provider error (429/5xx/dropped socket) before failing. Default "
            f"{_DEFAULT_MAX_RETRIES}. 0 disables retries (fail fast); raise for a flaky provider. "
            "Only transient errors retry — timeouts, parse failures, non-transient errors never do."
        ),
    )

    @field_validator("max_retries", mode="before")
    @classmethod
    def _strict_int(cls, value: object) -> object:
        # Pydantic's lax mode coerces a quoted number ("5"→5), an exact float (5.0→5), and a
        # boolean (bool subclasses int: True→1) silently. In a TOML integer knob each is a config
        # mistake that would quietly change retry behavior — reject them (exit 50), accept only a
        # plain int.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"max_retries must be a plain integer (got {value!r}); quoted numbers, floats, "
                "and booleans are rejected"
            )
        return value
