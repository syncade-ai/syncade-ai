"""Tests for :mod:`syncade.adapters.registry`."""

from __future__ import annotations

import pytest

from syncade.adapters.anthropic import AnthropicAdapter
from syncade.adapters.base import ReviewerAdapter
from syncade.adapters.openai import CodexAdapter
from syncade.adapters.producer import (
    ProducerAdapter,
    get_producer_adapter,
    known_producer_providers,
)
from syncade.adapters.registry import UnknownProviderError, get_adapter, known_providers
from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.exit_codes import CONFIG_ERROR
from syncade.orchestrator.verdict import _compute_exit_code


class TestRegistry:
    def test_returns_anthropic_adapter_for_anthropic(self):
        adapter = get_adapter("anthropic")
        assert isinstance(adapter, AnthropicAdapter)
        # The adapter's name agrees with the lookup key — invariant the
        # dispatcher relies on.
        assert adapter.name == "anthropic"

    def test_returns_codex_adapter_for_openai(self):
        adapter = get_adapter("openai")
        assert isinstance(adapter, CodexAdapter)
        assert adapter.name == "openai"

    def test_each_call_returns_a_fresh_instance(self):
        a = get_adapter("anthropic")
        b = get_adapter("anthropic")
        # Adapters are stateless by design; the registry returns a new
        # instance per call rather than caching a singleton, so tests
        # that mutate adapter state can't leak across calls.
        assert a is not b

    def test_unknown_provider_raises_with_listing(self):
        with pytest.raises(UnknownProviderError) as exc_info:
            get_adapter("anthrpic")  # typo
        assert exc_info.value.role == "reviewer"
        assert exc_info.value.requested == "anthrpic"
        msg = str(exc_info.value)
        # Error names the bad input ...
        assert "'anthrpic'" in msg
        # ... and lists the known providers so the user can fix it
        assert "anthropic" in msg
        assert "openai" in msg
        assert "config.toml" in msg

    def test_unknown_provider_exception_type_maps_to_config_error_without_substring(
        self,
    ):
        error = UnknownProviderError(
            role="reviewer",
            requested="mystery",
            known=("anthropic", "openai"),
        )
        assert "provider" not in str(error).lower()
        dispatch = DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="mystery",
                    output=None,
                    error=error,
                    duration_seconds=0.0,
                )
            ],
            total_duration_seconds=0.0,
        )

        assert _compute_exit_code(dispatch, None) == CONFIG_ERROR

    def test_known_providers_returns_sorted_list(self):
        providers = known_providers()
        assert providers == sorted(providers)
        assert "anthropic" in providers
        assert "openai" in providers

    def test_returned_adapter_implements_protocol(self):
        for provider in known_providers():
            adapter = get_adapter(provider)
            assert isinstance(adapter, ReviewerAdapter)


class TestProducerRegistry:
    """N14: the producer registry keeps two hand-maintained surfaces —
    the ``_KNOWN_PRODUCER_PROVIDERS`` tuple behind
    :func:`known_producer_providers` and :func:`get_producer_adapter`'s
    ``if`` branches — that can drift independently (unlike the reviewer
    registry, whose listing and lookup both read one factory dict). These
    tests pin the two surfaces together, mirroring ``TestRegistry`` above.
    """

    @pytest.mark.parametrize("provider", known_producer_providers())
    def test_every_known_provider_resolves_to_an_adapter(self, provider):
        """Forward direction: every provider the listing advertises
        resolves to a real ``ProducerAdapter`` whose ``name`` agrees with
        the lookup key — the invariant producer dispatch relies on, and
        the mirror of ``test_returned_adapter_implements_protocol``."""
        adapter = get_producer_adapter(provider)
        assert isinstance(adapter, ProducerAdapter)
        assert adapter.name == provider

    def test_resolves_exactly_the_listed_providers(self):
        """Vice-versa direction: the set of adapter names the registry
        actually produces equals the advertised listing — neither surface
        routes or lists a provider the other omits."""
        resolved = {get_producer_adapter(p).name for p in known_producer_providers()}
        assert resolved == set(known_producer_providers())

    def test_unknown_provider_raises_with_producer_role(self):
        """A provider outside the listing is rejected (no silent extra
        adapter), tagged with the producer role, and the error advertises
        the same known set — mirroring the reviewer unknown-provider
        test."""
        with pytest.raises(UnknownProviderError) as exc_info:
            get_producer_adapter("anthrpic")  # typo
        assert exc_info.value.role == "producer"
        assert sorted(exc_info.value.known) == known_producer_providers()
