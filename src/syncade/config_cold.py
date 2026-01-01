"""Config surfaces + defaults for syncade's three COLD actors.

Cold = runs in an isolated git-init'd tempdir, never a repo worktree; sandboxed at
``trusted-execute``; sees only what it is handed, and returns a strict schema:

- **synthesizer** (the judge) — consolidates reviewer findings. Sees structured
  reviewer outputs, never the diff.
- **drafter** (``--draft-spec``) — turns a session transcript into a spec.
- **auditor** (``--spec-audit``) — checks a spec for ambiguity before a review runs.

All three were hardcoded to ``CodexAdapter`` with module-constant models, so
``codex`` was a hard requirement for every one of them (PR-v2-23). They are now
registry-resolved, and each gets a config block. Same shape, so they live together.

This is a LEAF module (it imports only :mod:`syncade.config_types`) and it has to
be: both ``config.py`` (which mounts the models) and the actor modules (which
re-export the defaults) need these values, and ``config.py`` cannot import the
actors — ``syncade.synthesizer``'s ``__init__`` imports ``driver``, which imports
``config``. The defaults sit at the bottom of the graph; everyone imports down.

The defaults are ALSO the historical values, which is why ``persistence`` and
``metrics`` still read the ``SYNTHESIZER_*`` constants directly: for a legacy run
whose artifacts never recorded a model, the honest fallback is "the default as it
was", NOT whatever the operator has configured today.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from syncade.config_auth import AuthedActor
from syncade.config_types import Thinking

_COLD_MODELS: dict[str, str] = {
    "openai": "gpt-5.5",
    "anthropic": "claude-sonnet-4-6",
}
"""Default model per provider for a cold actor. See :class:`_ColdActorConfig`."""


class _ColdActorConfig(AuthedActor):
    """Shared base for the three cold actors: ``provider`` and ``model`` move as a
    PAIR.

    Setting ``provider`` alone re-derives ``model``. Without this,
    ``[auditor]\\nprovider = "anthropic"`` would keep the ``gpt-5.5`` default and
    hand a codex model to ``claude`` — a 404 at dispatch, and a baffling one, since
    the user never typed "gpt-5.5" anywhere.

    This mirrors :meth:`syncade.config_producer.ProducerConfig._keep_provider_and_model_paired`
    exactly; the producer has had the same footgun guarded since it became
    harness-aware. It only became POSSIBLE for the cold actors in PR-v2-23, which
    is what made their provider configurable in the first place.

    An explicit ``model`` always wins — this only fills in a model the user did not
    give, so pinning an off-map model (a new release, a fine-tune) still works.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _keep_provider_and_model_paired(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        provider = data.get("provider")
        if provider and "model" not in data and provider in _COLD_MODELS:
            data = {**data, "model": _COLD_MODELS[provider]}
        return data

    @field_validator("provider", check_fields=False)
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        # Function-local import: config_cold is a leaf that must not import
        # adapters at module level (import cycle via config → synthesizer → driver).
        from syncade.adapters.registry import known_providers  # noqa: PLC0415

        providers = known_providers()
        if v not in providers:
            raise ValueError(
                f"unknown cold-actor provider {v!r}; known providers: {', '.join(providers)}"
            )
        return v


SYNTHESIZER_PROVIDER = "openai"
SYNTHESIZER_MODEL = "gpt-5.5"
SYNTHESIZER_THINKING: Thinking = "high"
SYNTHESIZER_PERMISSIONS: Literal["trusted-execute"] = "trusted-execute"


class SynthesizerConfig(_ColdActorConfig):
    """``[synthesizer]`` — the cold judge that consolidates reviewer outputs.

    Until PR-v2-23 these were module constants and the driver imported
    :class:`~syncade.adapters.openai.CodexAdapter` by name, which made ``codex`` a
    hard requirement for EVERY run — even an all-Anthropic one, where the user paid
    for both reviewers and then lost the round to a CLI they never configured.

    The judge is deliberately NOT harness-aware (unlike the producer): a verdict has
    to stay comparable across runs regardless of which harness the operator happened
    to be coding in.
    """

    provider: str = Field(
        default=SYNTHESIZER_PROVIDER,
        description="Model provider for the judge. Resolved against the adapter "
        "registry, exactly like a reviewer's.",
    )
    model: str = Field(
        default=SYNTHESIZER_MODEL,
        description=(
            "Model identifier for the judge. Defaults to the same model as the "
            "shipped reviewers, and they should be changed together: PR-28 moved "
            "both to gpt-5.6-sol and PR-29 moved both back (the reviewers audited "
            "too leniently on it —. Leaving the two pins "
            "disagreeing sets a trap, because the edit that 'reconciles' them in "
            "the wrong direction is the one that drags the reviewers onto the "
            "lenient model."
        ),
    )
    thinking: Thinking = Field(
        default=SYNTHESIZER_THINKING,
        description="Reasoning-effort budget. The judge reasons across every "
        "reviewer's findings at once, so it gets the full budget by default.",
    )
    permissions: Literal["trusted-execute"] = Field(
        default=SYNTHESIZER_PERMISSIONS,
        description=(
            "Tool-permission tier. Cold actors are locked to ``trusted-execute``: "
            "the OS sandbox must stay ACTIVE and scoped to the synth's temp "
            "workspace so isolation is structural, not prompt-dependent. ``yolo`` "
            "maps to ``--dangerously-bypass-approvals-and-sandbox`` and is "
            "rejected at config load."
        ),
    )


DRAFTER_PROVIDER = "openai"
DRAFTER_MODEL = "gpt-5.5"
DRAFTER_THINKING: Thinking = "xhigh"
DRAFTER_PERMISSIONS: Literal["trusted-execute"] = "trusted-execute"


class DrafterConfig(_ColdActorConfig):
    """``[drafter]`` — the cold spec drafter behind ``syncade --draft-spec``.

    Turns a session transcript into a PR spec. Same codex-hardcoding bug as the
    judge (PR-v2-23): a Claude-Code-only user could not draft a spec at all.
    """

    provider: str = Field(
        default=DRAFTER_PROVIDER,
        description="Model provider for the drafter. Resolved against the adapter "
        "registry, exactly like a reviewer's.",
    )
    model: str = Field(
        default=DRAFTER_MODEL,
        description="Model identifier for the drafter.",
    )
    thinking: Thinking = Field(
        default=DRAFTER_THINKING,
        description=(
            "Reasoning-effort budget. Higher than the judge's by default: drafting a "
            "spec from a rambling session transcript is the hardest inference syncade "
            "asks for, and a vague spec silently degrades every downstream review."
        ),
    )
    permissions: Literal["trusted-execute"] = Field(
        default=DRAFTER_PERMISSIONS,
        description="Tool-permission tier. Locked to ``trusted-execute`` — cold "
        "actors may not disable the OS sandbox. ``yolo`` is rejected at config load.",
    )


AUDITOR_PROVIDER = "openai"
AUDITOR_MODEL = "gpt-5.5"
AUDITOR_THINKING: Thinking = "xhigh"
AUDITOR_PERMISSIONS: Literal["trusted-execute"] = "trusted-execute"


class AuditorConfig(_ColdActorConfig):
    """``[auditor]`` — the cold spec auditor behind ``syncade --spec-audit``.

    Flags an ambiguous spec BEFORE a review burns reviewer spend against it. Same
    codex-hardcoding bug as the judge and drafter (PR-v2-23).
    """

    provider: str = Field(
        default=AUDITOR_PROVIDER,
        description="Model provider for the auditor. Resolved against the adapter "
        "registry, exactly like a reviewer's.",
    )
    model: str = Field(
        default=AUDITOR_MODEL,
        description="Model identifier for the auditor.",
    )
    thinking: Thinking = Field(
        default=AUDITOR_THINKING,
        description="Reasoning-effort budget. High by default: a missed ambiguity "
        "here is paid for by every reviewer in every round downstream.",
    )
    permissions: Literal["trusted-execute"] = Field(
        default=AUDITOR_PERMISSIONS,
        description="Tool-permission tier. Locked to ``trusted-execute`` — cold "
        "actors may not disable the OS sandbox. ``yolo`` is rejected at config load.",
    )
