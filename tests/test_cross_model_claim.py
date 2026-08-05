"""The "cross-model" claim is backed by capability, not by wording — PR-h-03 item 1.

Operator-facing text (README, `CLAUDE.md`, `AGENTS.md`) describes syncade as a cross-model
review loop. That is true because every actor is registry-resolved and independently
configurable (PR-v2-23) — a cross-lab panel is a config change.

This asserts the CAPABILITY rather than the prose. Asserting the wording would only freeze
today's phrasing; asserting the capability is what makes the claim false-able — if a future
change made providers non-configurable, this fails and the docs become a lie that a test
catches.

It also pins the honest half: the DEFAULT is single-lab, so nothing may imply you get
cross-model by doing nothing.
"""

from __future__ import annotations

from syncade.config import SyncadeConfig


def test_a_cross_lab_panel_is_accepted():
    """Mixed-provider reviewers, judge, and producer — the claim's substance."""
    config = SyncadeConfig.model_validate(
        {
            "reviewers": [
                {"name": "codex-rev", "provider": "openai", "model": "gpt-5.5"},
                {"name": "claude-rev", "provider": "anthropic", "model": "claude-sonnet-4-6"},
            ],
            "synthesizer": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            "producer": {"provider": "openai", "model": "gpt-5.6-terra"},
        }
    )
    providers = {r.provider for r in config.reviewers}
    assert providers == {"openai", "anthropic"}, "reviewers are not cross-lab configurable"
    assert config.synthesizer.provider == "anthropic"
    assert config.producer.provider == "openai"


def test_every_actor_is_registry_resolved():
    """The claim covers all actors, not just reviewers. A provider that the adapter registry
    cannot resolve would make the config a promise the runtime breaks."""
    from syncade.adapters.registry import known_providers

    available = set(known_providers())
    assert {"openai", "anthropic"} <= available, f"registry only knows {sorted(available)}"


def test_the_default_panel_is_single_lab():
    """The honest half. If this ever fails, the default became cross-model and the docs
    saying otherwise (README's default-panel sentence, CLAUDE.md's 'known gap') are stale."""
    config = SyncadeConfig()
    labs = {r.provider for r in config.reviewers} | {config.synthesizer.provider}
    assert labs == {"openai"}, (
        f"default panel is no longer single-lab ({sorted(labs)}); the docs that describe it "
        f"as such must be updated"
    )
