# SIZE_OK: 400 pure LOC; codex invocation and JSONL errors share one CLI contract.
# Retained to avoid changing the observed CodexAdapter behavior surface.
# Future split: extract stream/auth parsing behind the same adapter API.
"""Adapter for the OpenAI ``codex`` CLI.

Built against the codex CLI's observed JSONL output — the
actual observed behavior of ``codex-cli 0.130.0``, not the PRD's
example invocation. If you're changing flag strings or the
JSONL-parsing path here, re-read the discovery doc first.
"""

from __future__ import annotations

from pathlib import Path

from syncade.adapters.base import (
    Invocation,
    ReviewerInvocationError,
)
from syncade.auth_preflight import assert_codex_reality_honours_declaration
from syncade.config import ReviewerConfig
from syncade.config_auth import apply_auth_to_env
from syncade.findings import (
    ReviewerOutput,
    ReviewerOutputError,
    parse_reviewer_output,
)
from syncade.process import (
    SubprocessNotFoundError,
    SubprocessResult,
    SubprocessTimeoutError,
    run_subprocess,
)
from syncade.worktree_env import worktree_scoped_env

from .openai_parsing import (
    _extract_failure_message,
    _looks_like_auth_failure,
    _parse_jsonl_events,
)

# Map ``ReviewerConfig.permissions`` (yolo | trusted-execute)
# to the corresponding flag set for ``codex exec``. ``safe`` is
# deliberately NOT mapped — see ``_validate_permissions`` for why.
#
# ``yolo`` uses the combined shorthand
# ``--dangerously-bypass-approvals-and-sandbox`` (sandbox bypass +
# never-prompt in one flag).
#
# ``trusted-execute`` uses ``-s workspace-write`` (writes inside the worktree
# are auto-approved) paired with ``-c approval_policy=never`` (the
# generic config override, since ``codex exec`` has no ``-a`` flag. The
# ``-a/--ask-for-approval`` flag exists on the top-level ``codex`` command but
# not on the ``exec`` subcommand.
_YOLO_FLAG = "--dangerously-bypass-approvals-and-sandbox"
_TRUSTED_SANDBOX = "workspace-write"
_TRUSTED_APPROVAL_CONFIG = "approval_policy=never"

# `codex login status` exit codes per the discovery doc:
#   0 — Logged in (stdout "Logged in using ChatGPT" or similar)
#   1 — Not logged in (stdout "Not logged in")
_CODEX_AUTH_CHECK_ARGV: list[str] = ["codex", "login", "status"]
_CODEX_AUTH_CHECK_TIMEOUT_SECONDS: float = 10.0


def _check_codex_auth(binary_missing_message: str) -> None:
    try:
        result = run_subprocess(
            _CODEX_AUTH_CHECK_ARGV,
            timeout=_CODEX_AUTH_CHECK_TIMEOUT_SECONDS,
        )
    except SubprocessNotFoundError as exc:
        raise ReviewerInvocationError(
            binary_missing_message,
            returncode=-1,
            stdout="",
            stderr="",
            api_error_status=None,
        ) from exc
    except SubprocessTimeoutError as exc:
        raise ReviewerInvocationError(
            f"codex login status timed out after {exc.timeout}s — "
            f"codex may be hung or the auth file may be corrupt. "
            f"Try `codex login` to re-authenticate.",
            returncode=-1,
            stdout=exc.stdout,
            stderr=exc.stderr,
            api_error_status=None,
        ) from exc

    if result.returncode != 0:
        detail = result.stdout.strip() or result.stderr.strip() or "auth check failed"
        raise ReviewerInvocationError(
            f"codex auth check failed: {detail[:200]} — run `codex login` to authenticate",
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            api_error_status=None,
        )


class CodexAdapter:
    """ReviewerAdapter for the OpenAI ``codex`` CLI.

    ``build_invocation`` produces a ``codex exec`` argv that pins the
    model via ``--model``, sets reasoning effort via the
    ``-c model_reasoning_effort=<level>`` config override (codex has
    no dedicated ``--effort`` flag — see the discovery doc), maps
    permissions to the combined
    ``--dangerously-bypass-approvals-and-sandbox`` flag for ``yolo``
    or ``-s workspace-write -c approval_policy=never`` for
    ``trusted-execute``, scopes file access to the worktree via ``-C`` and
    ``--add-dir``, and requests JSONL output via ``--json``.

    ``parse_output`` validates that the subprocess succeeded, parses
    the JSONL event stream, treats terminal failures (``turn.failed``,
    non-zero rc, or a bare ``error`` stream with no recovered
    ``agent_message``) as invocation errors, and on success extracts the LAST
    ``agent_message`` event's ``.item.text`` to hand to
    :func:`~syncade.findings.parse_reviewer_output`. The parser is
    already robust to markdown-fenced JSON, which the model may
    emit regardless of prompt instructions.

    ``check_auth`` runs ``codex login status`` — a fast filesystem
    check (no network) — and raises if auth is missing, so the
    dispatcher's pre-flight phase short-circuits the whole batch
    instead of letting codex burn 10+ seconds of 401 retries.

    The adapter never shells out itself in ``build_invocation`` or
    ``parse_output`` — :class:`Invocation` is data for the dispatcher
    to execute. ``check_auth`` is the one exception; it MUST shell
    out because it's checking subprocess-visible state.
    """

    name = "openai"

    def check_auth(self) -> None:
        """Verify ``codex login status`` for reviewer subprocesses."""
        _check_codex_auth(
            binary_missing_message=(
                "codex binary not found on PATH — install codex-cli to "
                "run reviewer subprocesses (the OpenAI Codex CLI, not a "
                "third-party fork)"
            )
        )

    def build_invocation(
        self,
        reviewer_config: ReviewerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        """Construct the ``codex exec`` invocation for a single reviewer run.

        The argv matches the form documented in
        the CLI output format:

        - ``codex exec``
        - ``--skip-git-repo-check``: reviewer workspaces deliberately contain
          no Git administrative data
        - ``--json``: emit JSONL events on stdout (cleanest parsing target)
        - ``--model <id>``: model from the reviewer config (full name
          like ``gpt-5.5``)
        - ``-c model_reasoning_effort=<level>``: maps from ``thinking``
          (see :attr:`syncade.config.ReviewerConfig.thinking` for the
          canonical list of accepted values). Codex has no dedicated
          ``--effort`` flag; the generic ``-c`` config override is
          the documented path.
        - ``--dangerously-bypass-approvals-and-sandbox`` for ``yolo``,
          or ``-s workspace-write -c approval_policy=never`` for
          ``trusted-execute`` (``codex exec`` has no
          ``-a/--ask-for-approval`` flag — that flag exists on the
          top-level ``codex`` command only — so the ``approval_policy``
          config key is the documented exec-subcommand path).
          ``permissions="safe"`` is **rejected** —
          see :meth:`_validate_permissions`.
        - ``-C <workspace>``: codex's working-root flag (analogous to
          claude's ``cwd`` behavior)
        - ``--add-dir <workspace>``: grants the reviewer tool-access
          to its workspace (explicit is safer than implicit, same as
          AnthropicAdapter)
        - prompt on STDIN (PR-h-field-01 item 1): ``codex exec`` reads the prompt
          from stdin when no positional PROMPT is given; argv is flag-only

        The subprocess runs with ``cwd = worktree_path`` and inherits
        the caller's environment so existing ``codex`` auth (in
        ``~/.codex/auth.json``) flows through.

        Raises:
            ValueError: If ``reviewer_config.provider`` is not
                ``"openai"`` — guards against the dispatcher
                misrouting a non-Codex config to this adapter.
            ValueError: If ``reviewer_config.permissions`` is
                ``"safe"`` — the corresponding
                ``approval_policy=untrusted`` mode prompts for tool use
                and ``codex exec`` is
                non-interactive. Surface loudly rather than hang.
        """
        self._validate_provider(reviewer_config.provider)
        self._validate_permissions(reviewer_config.permissions)
        # Structural backstop: refuse to spawn codex for a non-auto declaration the probed
        # login does not honour, on any path that skipped the CLI auth gate. No-op on the
        # gated CLI path. See syncade.auth_preflight.
        assert_codex_reality_honours_declaration(reviewer_config)

        argv: list[str] = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--json",
            "--model",
            reviewer_config.model,
            "-c",
            f"model_reasoning_effort={reviewer_config.thinking}",
        ]
        # Permission flags — yolo uses the combined shorthand;
        # trusted-execute uses `-s workspace-write` paired with
        # `-c approval_policy=never` (the generic config-override
        # mechanism, since `codex exec` has no `-a/--ask-for-approval`
        # flag). Safe and stale/unknown values are refused above.
        if reviewer_config.permissions == "yolo":
            argv.append(_YOLO_FLAG)
        else:  # trusted-execute
            argv.extend(["-s", _TRUSTED_SANDBOX, "-c", _TRUSTED_APPROVAL_CONFIG])
        # The prompt goes on STDIN, never argv — see AnthropicAdapter.build_invocation.
        # `codex exec` reads instructions from stdin when no positional PROMPT is given.
        argv.extend(
            [
                "-C",
                str(worktree_path),
                "--add-dir",
                str(worktree_path),
            ]
        )
        return Invocation(
            argv=argv,
            cwd=worktree_path,
            env=apply_auth_to_env(worktree_scoped_env(worktree_path), reviewer_config),
            stdin_text=prompt,
            timeout_seconds=None,
        )

    @staticmethod
    def _validate_provider(provider: str) -> None:
        """Refuse a config whose ``provider`` is not ``"openai"``.

        Same defensive guard as
        :meth:`syncade.adapters.anthropic.AnthropicAdapter._validate_provider` —
        fail loudly at build time if the dispatcher misroutes a config,
        rather than letting the subprocess fail with a confusing
        ``codex`` CLI error after the model/effort/permission flags
        from the wrong provider get spliced into argv.
        """
        if provider != "openai":
            raise ValueError(  # GENERIC_ERR_OK: config guard preserves existing ValueError API.
                f"CodexAdapter received a ReviewerConfig with "
                f"provider={provider!r}; expected 'openai'. The "
                f"dispatcher should route configs to the adapter whose "
                f"name matches the config's provider field."
            )

    @staticmethod
    def _validate_permissions(permissions: str) -> None:
        """Refuse ``permissions='safe'`` for the Codex adapter.

        The natural ``approval_policy=untrusted`` mapping prompts for
        any tool use outside a small auto-trusted set, and ``codex
        exec`` is non-interactive so prompts cannot be answered. The
        reviewer subprocess would hang on the first non-trusted command
        until the dispatcher's timeout fires. Raise here so the
        misconfiguration is a fast, legible error rather than a
        20-minute wait.
        """
        if permissions == "safe":
            raise ValueError(  # GENERIC_ERR_OK: config guard preserves existing ValueError API.
                "CodexAdapter cannot run a reviewer with permissions='safe' "
                "headlessly: the corresponding `approval_policy=untrusted` "
                "mode prompts for tool use and `codex exec` cannot answer "
                "prompts. Use 'trusted-execute' or 'yolo'."
            )
        if permissions not in {"trusted-execute", "yolo"}:
            raise ValueError(  # GENERIC_ERR_OK: config guard preserves existing ValueError API.
                "CodexAdapter received unsupported reviewer "
                f"permissions={permissions!r}; expected one of "
                "'trusted-execute', 'yolo'."
            )

    def parse_output(self, result: SubprocessResult) -> ReviewerOutput:
        """Parse a finished ``codex exec --json`` subprocess result.

        Decision tree for codex's JSONL stream:

        1. Extract the final ``agent_message`` text via
           :meth:`extract_final_text` — which itself
           parses the JSONL events, detects subprocess failures
           (``turn.failed`` events, non-zero ``returncode``, unrecovered
           bare ``error`` streams, auth signatures) and raises
           :class:`ReviewerInvocationError` for them. On
           no-``agent_message`` (codex succeeded but emitted nothing
           useful) the helper raises whatever
           ``empty_output_exception_class`` was passed — for
           reviewer dispatch that's :class:`ReviewerOutputError`
           (exit-70 territory).
        2. Hand the extracted text to
           :func:`~syncade.findings.parse_reviewer_output` — which
           handles markdown-fenced JSON, JSON-in-prose, and the
           JSX-shaped prose snippets.

        The reusable JSONL → text extraction lets :mod:`syncade.synthesizer`
        drive the same codex pipeline and parse the result as a
        :class:`~syncade.synthesis.SynthesizerOutput`.
        """
        final_text = self.extract_final_text(
            result,
            empty_output_exception_class=ReviewerOutputError,
        )
        return parse_reviewer_output(final_text)

    def extract_final_text(
        self,
        result: SubprocessResult,
        *,
        empty_output_exception_class: type[Exception],
    ) -> str:
        """Extract the final ``agent_message`` text from a
        ``codex exec --json`` subprocess result.

        Reusable across callers that need codex's output but parse it
        into different typed shapes. The synthesizer uses this to
        feed the text to :func:`syncade.synthesis.parse_synthesizer_output`
        while preserving the existing reviewer-dispatch behavior of
        :meth:`parse_output` (which uses
        :func:`syncade.findings.parse_reviewer_output`).

        Steps:

        1. Parse ``stdout`` as JSONL — one event per line. Skip blank
           lines and non-JSON garbage silently (defensive).
        2. Collect agent messages and terminal failure signals:

           - Any event of type ``"turn.failed"``.
           - A non-zero ``returncode``.
           - A bare ``"error"`` event only when no agent message was
             produced. Codex may emit transient reconnect ``error`` events and
             then recover with a completed turn; those are not fatal.
        3. If a terminal failure signal is present, raise
           :class:`ReviewerInvocationError` — the failure shape is
           codex-side (auth / network / API / process), not phase-
           specific, so the reviewer-invocation exception applies to
           both reviewer and synthesizer dispatch.
        4. Otherwise, return the LAST ``item.completed`` event whose
           ``item.type`` is ``agent_message`` and return its
           ``item.text``.
        5. If no ``agent_message`` event is present (codex succeeded
           but emitted nothing useful), raise an instance of
           ``empty_output_exception_class`` — the caller passes
           the phase-appropriate class so the user's exit-70 diagnostic
           tells them which ``.stdout`` to open.

        Args:
            result: The :class:`SubprocessResult` from running the
                codex subprocess.
            empty_output_exception_class: Exception class to
                instantiate when no agent_message is found. Reviewer
                dispatch passes :class:`ReviewerOutputError`;
                synthesizer dispatch passes
                :class:`syncade.synthesis.SynthesizerOutputError`.
                Both map to exit 70 but the message names the phase.

        Returns:
            The final ``agent_message`` text on success.

        Raises:
            ReviewerInvocationError: On any subprocess-side failure
                (failure events, non-zero rc, auth signature).
            ``empty_output_exception_class``: When no
                ``agent_message`` is present in the JSONL stream.
        """
        events = _parse_jsonl_events(result.stdout)

        turn_failed_events = [e for e in events if e.get("type") == "turn.failed"]
        bare_error_events = [e for e in events if e.get("type") == "error"]
        agent_messages = [
            e
            for e in events
            if e.get("type") == "item.completed"
            and isinstance(e.get("item"), dict)
            and e["item"].get("type") == "agent_message"
            and isinstance(e["item"].get("text"), str)
        ]

        terminal_failure = (
            bool(turn_failed_events)
            or result.returncode != 0
            or (bool(bare_error_events) and not agent_messages)
        )
        if turn_failed_events or result.returncode != 0:
            failure_events = [*bare_error_events, *turn_failed_events]
        elif bare_error_events and not agent_messages:
            failure_events = bare_error_events
        else:
            failure_events = []

        if terminal_failure:
            message = _extract_failure_message(failure_events, result)
            is_auth = _looks_like_auth_failure(message, failure_events)
            api_error_status = 401 if is_auth else None
            if is_auth:
                raised_message = (
                    f"codex auth failed (rc={result.returncode}): "
                    f"{message[:200]} — run `codex login` to reauthenticate"
                )
            else:
                raised_message = f"codex failed (rc={result.returncode}): {message[:300]}"
            raise ReviewerInvocationError(
                raised_message,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                api_error_status=api_error_status,
            )

        if not agent_messages:
            raise empty_output_exception_class(
                f"codex completed but emitted no agent_message event; "
                f"stdout: {result.stdout[:200]!r}"
            )

        # Multiple agent_messages are possible in multi-turn flows; the
        # reviewer's verdict is always the final one.
        return agent_messages[-1]["item"]["text"]

    def extract_response_text(self, raw_stdout: str) -> str:
        """Extract the assistant's response text from a ``codex exec
        --json`` JSONL stdout.

        Thin wrapper around
        :meth:`extract_final_text` that:

        - Synthesizes a :class:`SubprocessResult` with
          ``returncode=0`` (the on-disk artifact doesn't preserve the
          original returncode; we assume success because a failed
          codex round would have terminated the loop before the
          stdout was archived for cross-round replay).
        - Defaults the ``empty_output_exception_class`` argument
          to :class:`~syncade.findings.ReviewerOutputError` — the
          general-purpose exit-70 exception, mirroring what
          :meth:`parse_output` passes when running the reviewer
          pipeline.

        Symmetric with
        :meth:`syncade.adapters.anthropic.AnthropicAdapter.extract_response_text`
        — both adapters expose the same single-method interface for
        the orchestrator's prior-round-context plumbing
        (:mod:`syncade.orchestrator.prior_round`) to dispatch into.

        Args:
            raw_stdout: The raw stdout of a finished ``codex exec
                --json`` invocation. Expected shape: one JSON event
                per line; the final ``item.completed`` event with
                ``item.type == "agent_message"`` carries the response
                text in ``item.text``.

        Returns:
            The assistant's final agent_message text.

        Raises:
            ReviewerInvocationError: If the JSONL stream contains
                explicit terminal failure events (``turn.failed`` or an
                unrecovered bare ``error`` stream).
                In practice this shouldn't happen for cross-round
                replay (the loop terminates on codex subprocess
                failures) but the helper is robust.
            ReviewerOutputError: If no ``agent_message`` event is
                present in the JSONL stream — the default exception
                class.
        """
        synthesized = SubprocessResult(
            returncode=0,
            stdout=raw_stdout,
            stderr="",
            duration_seconds=0.0,
        )
        return self.extract_final_text(
            synthesized,
            empty_output_exception_class=ReviewerOutputError,
        )
