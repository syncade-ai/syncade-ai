"""Test-double adapters for :class:`~syncade.adapters.base.ReviewerAdapter`,
the synthesizer's :class:`~syncade.adapters.openai.CodexAdapter`
surface, and the producer's
:class:`~syncade.adapters.producer.ProducerAdapter` surface.

The fakes never shell out: ``build_invocation`` returns an
:class:`~syncade.adapters.base.Invocation` pointing at a no-op shell
command (so the dispatcher / synthesizer can still flow through
``process.run_subprocess`` without errors during integration tests),
and the parse / extract methods ignore the
:class:`~syncade.process.SubprocessResult` entirely and return
whatever was configured at construction time.

These aren't features — they're primitives for testing other code.
The dispatcher tests compose :class:`FakeAdapter` instances to
exercise N-reviewer scheduling, partial-failure handling, etc. The
orchestrator tests compose :class:`FakeSynthesizerAdapter`
instances to exercise the synthesizer phase without spawning a real
codex CLI.

Threading: the dispatcher's pre-flight phase calls ``check_auth`` from a
ThreadPoolExecutor and the dispatch phase calls
``build_invocation``/``parse_output`` from one too. ``check_auth_calls``,
``parse_output_calls``, and the ``invocations`` log all use a
:class:`threading.Lock` so concurrent updates from multiple worker
threads don't lose increments or interleave list appends. Tests in
practice use one adapter instance per reviewer (matching production —
each reviewer gets a fresh adapter from the registry), but the lock
makes the test double safe under any thread layout.

The synthesizer fake doesn't take that locking step because the
synthesizer phase is single-threaded (one codex subprocess per round,
not N parallel). If a future PR introduces parallel synthesizer
dispatch, revisit this.

this module was decomposed into ``fake_common`` (the shared
``_noop_argv`` helper), ``fake_reviewer_synth`` (the reviewer + synthesizer
doubles), and ``fake_producer_audit_draft`` (the producer / auditor / drafter
doubles). This module re-exports all of them so the
``syncade.adapters.fake.<name>`` import paths are unchanged.
"""

from __future__ import annotations

from .fake_common import _noop_argv
from .fake_producer_audit_draft import (
    FakeAuditorAdapter,
    FakeDrafterAdapter,
    FakeProducerAdapter,
    _default_canned_audit_output,
    _default_canned_draft_output,
    _default_canned_producer_output,
)
from .fake_reviewer_synth import (
    FakeAdapter,
    FakeSynthesizerAdapter,
    _default_canned_output,
    _default_canned_synth_output,
)

__all__ = [
    "_noop_argv",
    "FakeAdapter",
    "_default_canned_output",
    "FakeSynthesizerAdapter",
    "_default_canned_synth_output",
    "FakeProducerAdapter",
    "_default_canned_producer_output",
    "FakeAuditorAdapter",
    "_default_canned_audit_output",
    "FakeDrafterAdapter",
    "_default_canned_draft_output",
]
