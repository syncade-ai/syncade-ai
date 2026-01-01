"""Producer subprocess adapter Protocol — symmetric to
:class:`~syncade.adapters.base.ReviewerAdapter` but for the fix-it
subprocess that runs after a NO-SHIP round.

The producer adapter builds an :class:`~syncade.adapters.base.Invocation`
that runs a fresh ``claude -p`` (or ``codex exec``) inside the producer
worktree. The producer is expected to make file edits AND commit them.
The orchestrator checks the worktree's HEAD after the subprocess
returns to distinguish "committed" from "stalled"; the adapter is NOT
responsible for that check — it only owns argv construction and
output text extraction.

The producer is asymmetric to the reviewers in two ways that ARE the
producer's responsibility surface:

1. **Different default disposition.** The producer is writing code
   and must create git commits from a headless subprocess. Its default
   permission mode is ``yolo`` because sandboxed modes do not let both
   real producer CLIs complete the commit step unattended.
2. **Different output shape.** Producers don't emit structured JSON
   like reviewers do. They emit free-form narrative + make file
   edits + commit. The orchestrator's real source of truth for "did
   the producer do anything" is the worktree's git HEAD after the
   subprocess returns, NOT the parsed narrative. The narrative is
   preserved for operator inspection (especially on stall or
   next-round-regression paths) but it's not part of the verdict.

This module lands the Protocol + :class:`ProducerOutput` dataclass +
the registry lookup. The two concrete adapters live in sibling
modules :mod:`syncade.adapters.producer_anthropic` and
:mod:`syncade.adapters.producer_openai`; the
:class:`~syncade.adapters.fake.FakeProducerAdapter` test double lives
alongside the reviewer fakes in :mod:`syncade.adapters.fake`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from syncade.adapters.base import Invocation
from syncade.adapters.registry import UnknownProviderError
from syncade.config import ProducerConfig
from syncade.process import SubprocessResult


@dataclass(frozen=True)
class ProducerOutput:
    """The producer's parsed output.

    Producers don't emit structured JSON like reviewers do — they
    emit free-form narrative + make file edits + commit. The
    orchestrator's real source of truth for "did the producer do
    anything" is the worktree's git HEAD after the subprocess
    returns, not this dataclass. The narrative is preserved here
    for persistence into ``producer.stdout`` — operators read it to
    understand what the producer was trying to do, especially when
    the producer stalled (no commit) or when next-round reviewers
    flag NEW issues against the producer's diff.

    Attributes:
        narrative_text: Free-form text the producer emitted. For
            claude this is the ``.result`` field of the JSON
            envelope; for codex this is the extracted final
            ``agent_message`` text. Always a string; empty string
            allowed (a producer that committed without narrating
            doesn't violate any invariant — the orchestrator's
            stall detection is SHA-based, not narrative-based).
    """

    narrative_text: str


@runtime_checkable
class ProducerAdapter(Protocol):
    """Per-provider adapter protocol for the producer subprocess.

    Implementations:

    - :class:`syncade.adapters.producer_anthropic.AnthropicProducerAdapter`
      — production adapter for ``claude -p``.
    - :class:`syncade.adapters.producer_openai.OpenAIProducerAdapter`
      — production adapter for ``codex exec``.
    - :class:`syncade.adapters.fake.FakeProducerAdapter` — test
      double; never shells out, optionally writes a fixture
      commit to simulate a real producer.

    :attr:`name` is the stable provider identifier (matches
    :data:`syncade.config.ProducerProvider`); the producer registry
    in :func:`get_producer_adapter` uses it to map a
    :class:`~syncade.config.ProducerConfig` to its adapter.

    Symmetric to :class:`~syncade.adapters.base.ReviewerAdapter`'s
    surface — ``build_invocation`` + ``parse_output`` + optional
    ``check_auth`` — but with the producer-specific config and
    output types in the signatures. The two Protocols are
    separate (not a shared base) because the structural-typing
    distinction prevents a careless caller from passing a
    :class:`ProducerConfig` to a reviewer adapter or vice versa.
    """

    name: str

    def build_invocation(
        self,
        producer_config: ProducerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        """Build a subprocess invocation from a producer's config +
        worktree + already-rendered prompt. No side effects."""
        ...

    def parse_output(self, result: SubprocessResult) -> ProducerOutput:
        """Parse a finished subprocess result into a
        :class:`ProducerOutput`. Raises
        :class:`~syncade.adapters.base.ReviewerInvocationError` on
        any subprocess-side failure (non-zero rc, ``is_error: true``
        envelope, codex ``turn.failed`` event, auth signature).

        Distinct from
        :meth:`syncade.adapters.base.ReviewerAdapter.parse_output`:
        the producer doesn't have a structured-output schema, so
        there's no ``ReviewerOutputError`` shape — every failure is
        either subprocess-side (raised as ``ReviewerInvocationError``)
        or the subprocess succeeded and the narrative text is
        whatever the model emitted. The orchestrator's stall
        detection compares git SHAs, not narrative content.
        """
        ...

    def check_auth(self) -> None:
        """Optional pre-flight: verify the producer's CLI is logged in.

        Mirrors :meth:`ReviewerAdapter.check_auth`. Most production
        deployments share auth between reviewer and producer for the
        same provider — a claude reviewer + claude producer share
        the same keychain/OAuth state — so the orchestrator can call
        this once per provider per run without duplicating the
        ``codex login status`` round-trip.

        ``@runtime_checkable`` requires this attribute on every
        instance even when the body is a no-op; concrete adapters
        define an explicit ``return None`` when the provider has no
        cheap pre-flight (same convention as
        :class:`AnthropicAdapter.check_auth`).
        """
        ...


# ---------------------------------------------------------------------------
# Registry — separate from the reviewer registry so a single provider name
# (e.g. ``"anthropic"``) can route to BOTH a reviewer adapter and a producer
# adapter depending on call context. The two registries share vocabulary
# but not implementations.
# ---------------------------------------------------------------------------


_KNOWN_PRODUCER_PROVIDERS: tuple[str, ...] = ("anthropic", "openai")
"""Tuple of known producer provider names.

Kept as a module-level constant rather than a dict-of-factories
so :func:`get_producer_adapter`'s lookup can use lazy imports
inside the function body — sidestepping the circular-import
problem the original dict-population design ran into when an
adapter module tried to import from this module while this module
was still finishing its load.

The lazy-import path also lets a future ``[producer]`` provider
land by adding one line to this tuple + one branch in
:func:`get_producer_adapter`, without restructuring the
registration machinery."""


def get_producer_adapter(provider: str) -> ProducerAdapter:
    """Return a fresh :class:`ProducerAdapter` for the given provider.

    Symmetric to :func:`syncade.adapters.registry.get_adapter` but
    for the producer's distinct adapter set. The provider string
    must be one of the keys in :data:`_KNOWN_PRODUCER_PROVIDERS`
    (the same vocabulary as
    :data:`syncade.config.ProducerProvider`).

    Uses lazy imports inside the function body so the producer
    adapter modules (which themselves import from this module to
    get :class:`ProducerOutput` + :class:`ProducerAdapter`) don't
    create a circular-import problem when test code imports them
    directly.

    Raises:
        ValueError: When ``provider`` is not a known key. The
            error message names the bad input AND lists the known
            providers, so a typo in ``.syncade/config.toml`` (e.g.
            ``[producer] provider = "anthrpic"``) surfaces with
            both the mistake and the fix visible at once.
    """
    if provider == "anthropic":
        from syncade.adapters.producer_anthropic import AnthropicProducerAdapter

        return AnthropicProducerAdapter()
    if provider == "openai":
        from syncade.adapters.producer_openai import OpenAIProducerAdapter

        return OpenAIProducerAdapter()

    raise UnknownProviderError(
        role="producer",
        requested=provider,
        known=_KNOWN_PRODUCER_PROVIDERS,
    )


def known_producer_providers() -> list[str]:
    """Return the sorted list of registered producer providers.

    Parallel to :func:`syncade.adapters.registry.known_providers` —
    useful for surface-area assertions in tests and any future
    ``syncade providers --producer`` CLI command.
    """
    return sorted(_KNOWN_PRODUCER_PROVIDERS)
