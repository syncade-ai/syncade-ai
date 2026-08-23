"""Per-provider adapter contract for reviewer dispatch.

An adapter translates a :class:`~syncade.config.ReviewerConfig`, a
workspace path, and a rendered prompt into a concrete
:class:`Invocation` that :func:`syncade.process.run_subprocess` can
execute. It then translates the resulting :class:`SubprocessResult`
back into a typed :class:`~syncade.findings.ReviewerOutput`.

Adapters never touch the network or filesystem themselves — they only build and
parse. The dispatcher actually runs subprocesses. This split lets adapters be
pure-ish and unit-testable without spawning reviewer CLIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from syncade.config import ReviewerConfig
from syncade.findings import ReviewerOutput
from syncade.process import SubprocessResult


@dataclass(frozen=True)
class Invocation:
    """Resolved subprocess invocation for a single reviewer run.

    Attributes:
        argv: The argument vector to pass to
            :func:`syncade.process.run_subprocess`. ``argv[0]`` is the
            binary to execute.
        cwd: Working directory the subprocess runs from. Typically the
            reviewer's Git-less workspace path.
        env: Full environment for the subprocess. Adapters usually
            inherit the parent's env so the user's existing auth
            (keychain, OAuth, ``ANTHROPIC_API_KEY``) flows through.
        stdin_text: If not ``None``, written to the subprocess's stdin
            and stdin is then closed.
        timeout_seconds: Maximum wall-clock seconds the dispatcher
            should wait. ``None`` means no timeout — the dispatcher
            may apply its own ceiling regardless.
    """

    argv: list[str]
    cwd: Path
    env: dict[str, str]
    stdin_text: str | None = None
    timeout_seconds: float | None = None


class ReviewerInvocationError(Exception):
    """Raised by :meth:`ReviewerAdapter.parse_output` when a reviewer
    subprocess failed.

    This is the "subprocess itself blew up" path — auth failure, model
    unavailable, network error, CLI usage bug. Distinct from
    :class:`~syncade.findings.ReviewerOutputError` (the subprocess ran
    fine but its output didn't parse). The orchestrator maps this to
    syncade exit code 40 (``REVIEWER_FAILURE``); parse failures map to
    exit code 70 (``REVIEWER_OUTPUT_UNPARSEABLE``).

    "Failed" here means either a non-zero ``returncode`` OR a zero
    return code with the provider's structured-failure signal set
    (``claude``'s ``is_error: true`` envelope field, for example).
    Adapters that surface a structured failure should populate
    :attr:`api_error_status` so the dispatcher can distinguish
    transient (5xx, 429) from terminal (4xx) provider errors.

    Attributes:
        returncode: The subprocess's exit code.
        stdout: Captured stdout (may include partial reviewer output
            or, for Anthropic, the failure envelope).
        stderr: Captured stderr.
        api_error_status: Provider HTTP-style status code if available
            (e.g. 404 for a missing model). ``None`` when not
            applicable, when the failure happened before reaching the
            provider, or when the adapter doesn't surface this.
    """

    def __init__(
        self,
        message: str,
        *,
        returncode: int,
        stdout: str,
        stderr: str,
        api_error_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.api_error_status = api_error_status


@runtime_checkable
class ReviewerAdapter(Protocol):
    """Per-provider adapter protocol.

    Implementations:

    - :class:`syncade.adapters.anthropic.AnthropicAdapter` — production
      adapter for the ``claude`` CLI.
    - :class:`syncade.adapters.openai.CodexAdapter` — production
      adapter for the OpenAI ``codex`` CLI.
    - :class:`syncade.adapters.fake.FakeAdapter` — test double; never
      shells out.

    :attr:`name` is the stable identifier that must match
    :attr:`syncade.config.ReviewerConfig.provider` so the dispatcher
    can pick the right adapter for each configured reviewer.
    """

    name: str

    def build_invocation(
        self,
        reviewer_config: ReviewerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        """Build a subprocess invocation from a reviewer's config + workspace
        + already-rendered prompt. No side effects."""
        ...

    def parse_output(self, result: SubprocessResult) -> ReviewerOutput:
        """Parse a finished subprocess result into a typed reviewer
        output. Raises :class:`ReviewerInvocationError` on non-zero
        returncode; :class:`~syncade.findings.ReviewerOutputError` on
        unparseable output."""
        ...

    def extract_final_text(
        self,
        result: SubprocessResult,
        *,
        empty_output_exception_class: type[Exception],
    ) -> str:
        """Return the model's final response text, stripped of whatever
        envelope the provider's CLI wraps it in.

        This is the provider-agnostic seam. :meth:`parse_output` is only
        one thing you can do with a finished subprocess: parse the text as
        a :class:`~syncade.findings.ReviewerOutput`. The cold synthesizer
        parses the SAME text as a
        :class:`~syncade.synthesis.SynthesizerOutput`, the spec drafter as
        a ``SpecDraftOutput``, the producer as free narrative. Each needs
        the text without the schema, so the text extraction lives here and
        ``parse_output`` is a thin wrapper over it.

        Without this on the Protocol, any caller that wants raw text has to
        name a concrete adapter class — which is exactly how the judge ended
        up hardwired to codex.

        Args:
            result: The finished subprocess.
            empty_output_exception_class: Raised when the subprocess
                SUCCEEDED but produced no usable text. Callers pass the
                exception type matching the shape they were going to parse
                into (``ReviewerOutputError``, ``SynthesizerOutputError``,
                …) so a content failure lands in that caller's error
                taxonomy rather than the adapter's. All map to exit 70.

        Returns:
            The model's final response text. Never empty.

        Raises:
            ReviewerInvocationError: The subprocess itself failed — non-zero
                returncode, or a zero returncode carrying the provider's
                structured-failure signal (claude's ``is_error: true``
                envelope, codex's ``turn.failed`` event). Exit 40, and the
                dispatcher/synthesizer consult
                :mod:`syncade.retry` on it to decide whether to retry.
            empty_output_exception_class: Ran fine, said nothing usable.
        """
        ...

    def check_auth(self) -> None:
        """Optional pre-flight: verify the provider's CLI is logged in
        and ready for headless invocation.

        Adapters with a cheap, deterministic auth-check command (e.g.
        ``codex login status``) override this to short-circuit the
        whole dispatch batch when one reviewer's auth is broken — the
        dispatcher then fails fast rather than letting the broken
        reviewer chew through tokens or retry loops before its sibling
        reviewers finish.

        Implementations should:

        - Be fast (no network call if possible).
        - Raise :class:`ReviewerInvocationError` with a message that
          tells the user EXACTLY what to fix (e.g. "run ``codex login``
          to authenticate").
        - When the provider has no cheap pre-flight, define an
          explicit no-op (``def check_auth(self) -> None: return None``)
          so the dispatcher's auth phase skips this adapter cleanly
          and :func:`isinstance(adapter, ReviewerAdapter)` keeps
          working. See the note below on Protocol conformance.

        **No-op via inheritance is NOT enough.** This is a
        ``@runtime_checkable`` Protocol, so
        ``isinstance(adapter, ReviewerAdapter)`` checks that the
        attribute exists on the *instance*. A class that omits
        ``check_auth`` entirely does NOT pick up the ellipsis body
        below as a real implementation — the isinstance check fails
        outright. Every concrete adapter must define ``check_auth``,
        even if it's only ``return None``.
        :class:`syncade.adapters.anthropic.AnthropicAdapter` defines
        this explicit no-op; its auth failures surface via the
        ``is_error: true`` envelope in :meth:`parse_output` during
        the actual run.
        :class:`syncade.adapters.openai.CodexAdapter` overrides with
        a real ``codex login status`` check (see
        the codex CLI output notes).

        The dispatcher always calls ``check_auth`` before starting parallel
        runs.
        """
        ...
