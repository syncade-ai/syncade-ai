"""Provider-string-to-adapter lookup for the dispatcher.

The dispatcher takes a list of :class:`ReviewerConfig`s and
routes each to the adapter whose :attr:`ReviewerAdapter.name` matches
the config's ``provider`` field. This module is that routing table.

Intentionally a tiny dict and not a plugin system. Adding a new
provider lands by appending one line here; nothing dynamic until we
have evidence we need it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from syncade.adapters.anthropic import AnthropicAdapter
from syncade.adapters.base import ReviewerAdapter
from syncade.adapters.openai import CodexAdapter

ProviderRole = Literal["reviewer", "producer"]


@dataclass(frozen=True)
class UnknownProviderError(ValueError):
    role: ProviderRole
    requested: str
    known: tuple[str, ...]

    def __str__(self) -> str:
        known = ", ".join(sorted(self.known))
        return (
            f"unknown {self.role} adapter {self.requested!r} in "
            f".syncade/config.toml. Known adapters: {known}"
        )


# Mapping: provider string → zero-arg factory that constructs the
# adapter. Using factories (not pre-built instances) so each call to
# ``get_adapter`` returns a fresh adapter — keeps tests that mutate
# adapter state from leaking across calls.
_ADAPTER_FACTORIES: dict[str, Callable[[], ReviewerAdapter]] = {
    "anthropic": AnthropicAdapter,
    "openai": CodexAdapter,
}


def get_adapter(provider: str) -> ReviewerAdapter:
    """Return a fresh adapter instance for the given provider string.

    The provider name must match a key in the registry. The known
    providers are tied to ``ReviewerAdapter.name`` for each shipped
    adapter; the dispatcher uses this lookup to route each
    :class:`~syncade.config.ReviewerConfig` to its matching adapter.

    Raises:
        ValueError: When ``provider`` is not a known key. The error
            message names the bad input AND lists the known
            providers, so a user with a typo in
            ``.syncade/config.toml`` (``provider = "anthrpic"``) sees
            both their mistake and the fix at once.
    """
    try:
        factory = _ADAPTER_FACTORIES[provider]
    except KeyError:
        raise UnknownProviderError(
            role="reviewer",
            requested=provider,
            known=tuple(_ADAPTER_FACTORIES),
        ) from None
    return factory()


def known_providers() -> list[str]:
    """Return the sorted list of registered provider names.

    Useful for surface-area assertions in tests and for any future
    `syncade providers` CLI command that lists what's available.
    """
    return sorted(_ADAPTER_FACTORIES)
