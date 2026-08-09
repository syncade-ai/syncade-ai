"""SynthesizerResult — outcome of one synthesizer subprocess run.

Mirrors :class:`~syncade.dispatcher.ReviewerRunResult`'s shape so
persistence and the exit-code decision table can treat reviewer
outcomes and synthesizer outcomes with the same vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from syncade.process import SubprocessResult
from syncade.synthesis import SynthesizerOutput
from syncade.usage import Usage


@dataclass(frozen=True)
class SynthesizerResult:
    """Outcome of one synthesizer subprocess run.

    Mirrors :class:`~syncade.dispatcher.ReviewerRunResult`'s shape so
    persistence and the exit-code decision table can treat reviewer
    outcomes and synthesizer outcomes with the same vocabulary.

    Attributes:
        output: The parsed :class:`SynthesizerOutput` on success;
            ``None`` on failure.
        error: The exception that fired on failure
            (:class:`SynthesizerOutputError`,
            :class:`ReviewerInvocationError`, a
            :class:`~syncade.process.SubprocessError` subclass, or any
            unexpected exception). ``None`` on success.
        duration_seconds: Wall-clock duration of the synthesizer
            subprocess. ``0.0`` for failures that happened before any
            subprocess ran (none today, but kept for symmetry with
            :class:`ReviewerRunResult`).
        raw_subprocess_result: The :class:`SubprocessResult` from the
            codex subprocess, preserved so persistence can write
            ``synthesizer.stdout`` / ``synthesizer.stderr`` even on
            timeouts and parse failures. ``None`` only when the
            subprocess never produced output (binary missing — a
            ``SubprocessNotFoundError`` from ``run_subprocess``). On
            timeout this is NOT ``None``: synthesized from
            :class:`SubprocessTimeoutError`'s partial stdout/stderr
            with sentinel ``returncode=-1``, same convention as
            :class:`ReviewerRunResult`.
        provenance_repairs: Provenance quotations corrected from the reviewer's
            own text (PR-h-field-01 item 5). Empty on the normal path. Non-empty means
            the synthesizer miscopied a source it had correctly attributed; the
            rendered text is the reviewer's, and this records that it happened.
        retries: Number of EXTRA synth subprocess attempts consumed
            riding out transient provider blips (429/5xx/dropped
            socket) before this outcome. ``0`` when the first attempt
            settled it. Mirrors :attr:`ReviewerRunResult.retries` so
            persistence can sum one round-level ``retried`` count.
    """

    output: SynthesizerOutput | None
    error: Exception | None
    duration_seconds: float
    raw_subprocess_result: SubprocessResult | None = field(default=None)
    retries: int = 0
    usage: Usage | None = field(default=None)
    provider: str | None = None
    provenance_repairs: tuple[object, ...] = ()
    model: str | None = None

    def __post_init__(self) -> None:
        """Enforce the success/failure contract before persistence sees it."""
        if (self.output is None) == (self.error is None):
            raise ValueError(
                "SynthesizerResult requires exactly one of output or error to be non-None"
            )
