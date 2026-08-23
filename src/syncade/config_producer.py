"""Producer subprocess configuration, and the harness detection that drives its
defaults.

Split out of ``config.py`` (PR-v2-23): that module was 462 LOC against a **blocking**
500 cap, and the judge/drafter blocks this PR adds would have pushed it over. Everything
producer-shaped lives here — the harness probe, the provider→model pairing, and
``ProducerConfig`` itself. ``config.py`` re-exports these names, so every existing
import path keeps working.
"""

from __future__ import annotations

import math
import os
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from syncade.config_auth import AuthedActor
from syncade.config_types import Thinking

Harness = Literal["claude-code", "codex"]
"""Ambient coding harness syncade was invoked from.

Inferred from the process environment, not from the syncade CLI itself:
Claude Code sets ``CLAUDE_CODE_SESSION_ID`` and Codex sets
``CODEX_THREAD_ID`` in the shell they hand to their tools."""


def _invoking_harness() -> Harness:
    """The ambient coding harness, for producer default selection.

    Claude Code wins if both markers are present — it is the more specific
    signal (a Codex thread id can linger in an exported environment).

    With neither marker (plain terminal, CI, cron) we fall back to the Codex
    shape. That is the safe side: the reviewers and the judge are OpenAI
    regardless of harness, so codex auth is required to run syncade at all,
    while Anthropic auth is only implied when Claude Code is the harness.
    """
    if os.getenv("CLAUDE_CODE_SESSION_ID"):
        return "claude-code"
    if os.getenv("CODEX_THREAD_ID"):
        return "codex"
    return "codex"


# The producer follows the harness the operator is already coding in; the
# reviewers and the judge do not (see _default_reviewers / SYNTHESIZER_MODEL).
# provider → the model that provider's producer runs. Kept as a pair because
# the two must agree: handing claude-sonnet-4-6 to ``codex exec`` is an
# unknown-model failure at dispatch, not a config error.
_PRODUCER_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5.6-terra",
}


def _default_producer_provider() -> str:
    return "anthropic" if _invoking_harness() == "claude-code" else "openai"


def _default_producer_model() -> str:
    return _PRODUCER_MODELS[_default_producer_provider()]


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


ProducerProvider = Literal["anthropic", "openai"]
"""Provider names the producer adapter registry recognizes.
The producer subprocess is a fresh LLM (claude or codex) — not the
operator's Claude Code session. Same provider-string vocabulary as
``ReviewerConfig.provider`` so the same adapter routing applies."""

ProducerPermissions = Literal["confined", "yolo"]
"""Producer confinement policy. ``confined`` is the DEFAULT; ``yolo`` is an opt-out.

``confined`` runs each provider's live-verified filesystem sandbox around the standalone
producer repository: writes outside it are denied by enforcement, not by prompt text
(PR-h-05 Item 2's live matrix). On the Anthropic path the sandbox can only auto-approve
SANDBOXED Bash, so a confined ``claude`` producer runs Bash-only — it edits through the
shell rather than through Edit/Write.

``yolo`` is the pre-PR-h-05 behaviour and stays available because that tool restriction is a
real cost and the operator, not this schema, owns that trade. It disables the sandbox
outright (claude ``bypassPermissions`` / codex ``--dangerously-bypass-approvals-and-sandbox``),
removing the enforced write boundary around the standalone producer repository. The standalone
store and trusted importer are UNCONDITIONAL — neither is gated on ``producer.permissions`` —
so rank 3 closes in both modes; what ``yolo`` forgoes is the host-confinement layer ``confined``
adds on top (the deferred host-trust half of audit rank 2). It is therefore DISCLOSED on every
run that can dispatch a producer, on a channel ``--quiet`` does not suppress. Neither mode ever
prompts; both run fully unattended."""


class ProducerConfig(AuthedActor):
    """Producer subprocess configuration.

    The producer is the fresh LLM subprocess that receives
    ``findings.md`` after a NO-SHIP round and makes the fix. Symmetric
    to :class:`ReviewerConfig`'s adapter-routing surface (``provider``
    + ``model`` + ``thinking`` + ``permissions``) but with
    code-writing defaults rather than auditing defaults.

    Same provider-name keys as ``[[reviewers]]`` so the adapter
    registry can route both via the same vocabulary. The empty
    ``[producer]`` section (or absent section) in
    ``.syncade/config.toml`` instantiates this with all defaults;
    operators wanting codex as producer write
    ``[producer]\\nprovider = "openai"\\nmodel = "gpt-5-codex"\\n``.

    Defaults follow the invoking harness (:func:`_invoking_harness`) —
    ``anthropic`` / ``claude-sonnet-4-6`` under Claude Code, ``openai`` /
    ``gpt-5.6-terra`` otherwise — with ``thinking="medium"`` /
    ``permissions="confined"`` either way. The producer is the one role that
    tracks the operator's toolchain; the reviewers and the judge stay pinned
    to a cold OpenAI tier so verdicts stay comparable across harnesses. Other
    permission values are rejected at the schema level (see
    :data:`ProducerPermissions`).

    The producer's ``timeout_seconds`` is per-round wall-clock; when
    unset (the default), the orchestrator reuses
    :attr:`LoopConfig.timeout_seconds` (i.e. the reviewer timeout).
    The same :func:`math.isfinite` guard also applies to
    :attr:`LoopConfig.timeout_seconds` applies here so a runaway
    operator config can't produce an unkillable producer subprocess.

    The producer is a fresh ``claude -p`` or ``codex exec`` subprocess for each
    round. It receives prior producer context as replayed prompt input from
    persisted artifacts, not as a resumable process session."""

    model_config = ConfigDict(extra="forbid")

    provider: ProducerProvider = Field(
        default_factory=_default_producer_provider,
        description=(
            "Producer adapter provider. ``anthropic`` routes to "
            ":class:`AnthropicProducerAdapter` (``claude -p`` with its "
            "native sandbox); ``openai`` routes "
            "to :class:`OpenAIProducerAdapter` (``codex exec``). "
            "Defaults to the invoking harness (see :func:`_invoking_harness`): "
            "``anthropic`` under Claude Code, ``openai`` otherwise. "
            "Uses the same provider names as ``ReviewerConfig.provider``, "
            "but producer and reviewer adapters are registered separately; "
            "a future provider must add each supported role explicitly."
        ),
    )
    model: str = Field(
        default_factory=_default_producer_model,
        description=(
            "Model identifier within the provider. Defaults to the model that "
            "matches ``provider`` (see :data:`_PRODUCER_MODELS`): "
            "``claude-sonnet-4-6`` for ``anthropic``, ``gpt-5.6-terra`` for "
            "``openai``. Setting ``provider`` alone re-derives this, so the "
            "pair cannot disagree; set ``model`` explicitly to pin a "
            "different tier within the provider."
        ),
    )
    thinking: Thinking = Field(
        default="medium",
        description=(
            "Reasoning effort budget. ``medium`` is the default — the "
            "producer is applying the synthesizer's consolidated findings "
            "to code it can see, which is a narrower task than the "
            "reviewers' open-ended audit or the judge's cross-reviewer "
            "consolidation. Accepted values are enumerated by the "
            ":data:`syncade.config.Thinking` Literal — see "
            ":attr:`ReviewerConfig.thinking` for the canonical-source "
            "note."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _keep_provider_and_model_paired(cls, data: object) -> object:
        """An explicit ``provider`` with no ``model`` re-derives the model.

        Without this, ``[producer]\\nprovider = "openai"`` on a Claude Code
        box would inherit the harness's ``claude-sonnet-4-6`` default and hand
        it to ``codex exec`` — an unknown-model failure at dispatch, and one
        that only reproduces on that harness. The pair moves together.
        """
        if not isinstance(data, dict):
            return data
        provider = data.get("provider")
        if provider and "model" not in data and provider in _PRODUCER_MODELS:
            data = {**data, "model": _PRODUCER_MODELS[provider]}
        return data

    permissions: ProducerPermissions = Field(
        default="confined",
        description=(
            "Producer confinement policy. ``confined`` (default) runs the "
            "provider's live-verified filesystem sandbox around the standalone "
            "producer repository; exterior writes are denied by enforcement. "
            "``yolo`` disables that sandbox (host confinement only — the trusted "
            "importer boundary and standalone producer store are unconditional in "
            "both modes) — supported, never the default, and disclosed on every run "
            "that can dispatch a producer. A single-pass review dispatches none and "
            "is not warned about one. Both run unattended; neither prompts."
        ),
    )

    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Per-producer-round wall-clock timeout in seconds. "
            "``None`` (default) reuses :attr:`LoopConfig.timeout_seconds` "
            "(the reviewer timeout). The orchestrator does that "
            "resolution; keeping the field nullable means 'use the "
            "default' is distinguishable from 'explicitly set to "
            "the same value as ``timeout_seconds``'. Must be > 0 "
            "and finite when set; NaN and infinity are rejected via "
            "a ``field_validator`` (same ``math.isfinite`` guard "
            "this applies to :attr:`LoopConfig.timeout_seconds`)."
        ),
    )

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_seconds_isfinite(cls, value: float | None) -> float | None:
        """Reject NaN / inf — pydantic's ``gt=0`` admits both unaided.
        Same field-validator pattern as
        :meth:`LoopConfig._test_timeout_seconds_isfinite`. Without
        this, a producer subprocess that hits an unkillable timeout
        would hang the orchestrator's loop indefinitely."""
        if value is not None and not math.isfinite(value):
            raise ValueError(  # GENERIC_ERR_OK: Pydantic validator expects ValueError.
                f"producer.timeout_seconds must be a finite positive number; got {value!r}"
            )
        return value
