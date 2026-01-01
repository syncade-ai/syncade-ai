"""Tests for the three cold-actor config blocks (PR-v2-23).

``[synthesizer]`` / ``[drafter]`` / ``[auditor]`` — the judge, the ``--draft-spec``
drafter, and the ``--audit`` auditor. All three were hardcoded to ``CodexAdapter``
with module-constant models, which made ``codex`` mandatory even for an
all-Anthropic user. They are now registry-resolved and configurable.
"""

from __future__ import annotations

import tomllib

import pytest
from pydantic import ValidationError

from syncade.config import AuditorConfig, DrafterConfig, SyncadeConfig, SynthesizerConfig
from syncade.spec_audit import (
    AUDITOR_MODEL,
    AUDITOR_PERMISSIONS,
    AUDITOR_PROVIDER,
    AUDITOR_THINKING,
)
from syncade.spec_draft import (
    DRAFTER_MODEL,
    DRAFTER_PERMISSIONS,
    DRAFTER_PROVIDER,
    DRAFTER_THINKING,
)
from syncade.synthesizer.constants import (
    SYNTHESIZER_MODEL,
    SYNTHESIZER_PERMISSIONS,
    SYNTHESIZER_PROVIDER,
    SYNTHESIZER_THINKING,
)

_COLD_BLOCKS = ("synthesizer", "drafter", "auditor")


def _load(toml: str) -> SyncadeConfig:
    return SyncadeConfig.model_validate(tomllib.loads(toml))


class TestDefaultsAreTheHistoricalConstants:
    """Making these configurable must not change what anyone already gets. Every
    default has to reproduce the pre-PR-v2-23 hardcoded constant EXACTLY."""

    @pytest.mark.parametrize(
        ("block", "expected"),
        [
            (
                "synthesizer",
                (
                    SYNTHESIZER_PROVIDER,
                    SYNTHESIZER_MODEL,
                    SYNTHESIZER_THINKING,
                    SYNTHESIZER_PERMISSIONS,
                ),
            ),
            ("drafter", (DRAFTER_PROVIDER, DRAFTER_MODEL, DRAFTER_THINKING, DRAFTER_PERMISSIONS)),
            ("auditor", (AUDITOR_PROVIDER, AUDITOR_MODEL, AUDITOR_THINKING, AUDITOR_PERMISSIONS)),
        ],
    )
    def test_zero_config_matches_historical_constants(self, block, expected) -> None:
        cfg = getattr(SyncadeConfig(), block)
        assert (cfg.provider, cfg.model, cfg.thinking, cfg.permissions) == expected

    def test_judge_and_reviewers_stay_on_the_same_model(self) -> None:
        """The PR-28/29 trap: reviewers and judge moved to gpt-5.6-sol together and
        back together. Leaving the two pins disagreeing sets a trap, because the
        edit that 'reconciles' them in the wrong direction drags the reviewers onto
        the lenient model. Keep them equal by default."""
        cfg = SyncadeConfig()
        assert {r.model for r in cfg.reviewers} == {cfg.synthesizer.model}


class TestProviderAndModelMoveAsAPair:
    """The footgun this PR introduced by making ``provider`` configurable at all:
    setting it alone would keep the gpt-5.5 default and hand a codex model to
    ``claude`` — a 404 at dispatch, naming a model the user never typed.

    Mirrors ProducerConfig._keep_provider_and_model_paired, which guards the same
    thing for the producer.
    """

    @pytest.mark.parametrize("block", _COLD_BLOCKS)
    def test_provider_alone_rederives_the_model(self, block) -> None:
        cfg = getattr(_load(f'[{block}]\nprovider = "anthropic"\n'), block)
        assert cfg.provider == "anthropic"
        assert cfg.model == "claude-sonnet-4-6"
        assert not cfg.model.startswith("gpt-"), "a codex model would reach claude"

    @pytest.mark.parametrize("block", _COLD_BLOCKS)
    def test_explicit_model_always_wins(self, block) -> None:
        """The re-derive only FILLS IN a model the user didn't give. It must never
        override one they did — pinning a new release or a fine-tune has to work."""
        cfg = getattr(
            _load(f'[{block}]\nprovider = "anthropic"\nmodel = "claude-opus-4-6"\n'), block
        )
        assert (cfg.provider, cfg.model) == ("anthropic", "claude-opus-4-6")

    @pytest.mark.parametrize("block", _COLD_BLOCKS)
    def test_off_map_model_is_still_pinnable(self, block) -> None:
        cfg = getattr(_load(f'[{block}]\nmodel = "some-unreleased-model"\n'), block)
        assert cfg.model == "some-unreleased-model"


class TestBlockSurface:
    @pytest.mark.parametrize("block", _COLD_BLOCKS)
    def test_thinking_is_configurable(self, block) -> None:
        cfg = getattr(_load(f'[{block}]\nthinking = "max"\n'), block)
        assert cfg.thinking == "max"

    @pytest.mark.parametrize("block", _COLD_BLOCKS)
    def test_permissions_yolo_is_rejected(self, block) -> None:
        """Cold actors must stay on trusted-execute; yolo disables the OS sandbox."""
        with pytest.raises(ValidationError):
            _load(f'[{block}]\npermissions = "yolo"\n')

    @pytest.mark.parametrize("block", _COLD_BLOCKS)
    def test_permissions_safe_is_rejected(self, block) -> None:
        with pytest.raises(ValidationError):
            _load(f'[{block}]\npermissions = "safe"\n')

    @pytest.mark.parametrize("block", _COLD_BLOCKS)
    def test_permissions_trusted_execute_is_accepted(self, block) -> None:
        cfg = getattr(_load(f'[{block}]\npermissions = "trusted-execute"\n'), block)
        assert cfg.permissions == "trusted-execute"

    @pytest.mark.parametrize("block", _COLD_BLOCKS)
    def test_unknown_provider_is_rejected_at_config_load(self, block) -> None:
        """A typo'd provider must fail at config load (ValidationError), not at
        adapter dispatch after reviewers have already run and billed."""
        with pytest.raises(ValidationError):
            _load(f'[{block}]\nprovider = "typo-provider"\n')

    @pytest.mark.parametrize("block", _COLD_BLOCKS)
    def test_unknown_key_is_rejected(self, block) -> None:
        """extra="forbid" — a typo'd key must surface as a config error, not be
        silently ignored (which would leave the user believing they'd set it)."""
        with pytest.raises(ValidationError):
            _load(f'[{block}]\nmodle = "gpt-5.5"\n')

    @pytest.mark.parametrize("block", _COLD_BLOCKS)
    def test_bad_thinking_value_is_rejected(self, block) -> None:
        with pytest.raises(ValidationError):
            _load(f'[{block}]\nthinking = "ludicrous"\n')

    def test_the_three_blocks_are_independent(self) -> None:
        """Configuring one cold actor must not move the others."""
        cfg = _load('[drafter]\nprovider = "anthropic"\n')
        assert cfg.drafter.provider == "anthropic"
        assert cfg.synthesizer.provider == "openai"
        assert cfg.auditor.provider == "openai"

    @pytest.mark.parametrize("model_cls", [SynthesizerConfig, DrafterConfig, AuditorConfig])
    def test_constructible_standalone(self, model_cls) -> None:
        """The actor entrypoints default to `Config()` when handed no block, so the
        models must construct with no arguments."""
        assert model_cls().provider == "openai"


class TestEveryColdActorResolvesFromTheRegistry:
    """The whole point of PR-v2-23, pinned for all three at once.

    Each actor used to do ``adapter = adapter or CodexAdapter()``, which is why a
    box without ``codex`` on PATH could not run ANY of them. Each must now resolve
    ``get_adapter(config.provider)`` — at its OWN module's lookup site (the
    decomposition rule: each does ``from ... import get_adapter``, binding the name
    into its own globals).

    Without this test, a future refactor could quietly re-hardcode CodexAdapter in
    the drafter or auditor and every unit test would still pass, because they all
    inject a fake adapter.
    """

    @pytest.mark.parametrize(
        ("module_path", "runner"),
        [
            ("syncade.synthesizer.driver", "run_synthesizer"),
            ("syncade.spec_draft", "run_spec_draft"),
            ("syncade.spec_audit", "run_spec_audit"),
        ],
    )
    def test_actor_module_resolves_get_adapter_not_a_hardcoded_class(
        self, module_path, runner, monkeypatch
    ) -> None:
        import importlib

        module = importlib.import_module(module_path)

        # The lookup must EXIST at the module's own site...
        assert hasattr(module, "get_adapter"), (
            f"{module_path} does not import get_adapter — it is resolving its adapter "
            "some other way (a hardcoded class?), which is the PR-v2-23 bug."
        )
        # ...and no adapter class may be hardcoded there any more.
        assert not hasattr(module, "CodexAdapter"), (
            f"{module_path} still names CodexAdapter — a codex-less box cannot run it."
        )
        assert callable(getattr(module, runner))
