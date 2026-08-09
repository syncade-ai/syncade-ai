"""Adapter for the Anthropic ``claude`` CLI.

Built against the claude CLI's observed JSON output —
the actual observed behavior of ``claude 2.1.137 (Claude Code)``, not
the PRD's example invocation. If you're changing flag strings or the
output-parsing path here, re-read the discovery doc first.
"""

from __future__ import annotations

import json
from pathlib import Path

from syncade.adapters.base import (
    Invocation,
    ReviewerInvocationError,
)
from syncade.config import ReviewerConfig
from syncade.config_auth import apply_auth_to_env
from syncade.findings import (
    ReviewerOutput,
    ReviewerOutputError,
    parse_reviewer_output,
)
from syncade.process import SubprocessResult
from syncade.worktree_env import worktree_scoped_env

# Map ``ReviewerConfig.permissions`` to the corresponding
# ``--permission-mode`` value for ``claude``. ``safe`` is deliberately
# NOT mapped — see ``_validate_permissions`` for why.
#
# ``trusted-execute`` maps to ``bypassPermissions`` (no prompts,
# full worktree access) — the provider-symmetric mode.
_PERMISSION_MAPPING: dict[str, str] = {
    "yolo": "bypassPermissions",
    "trusted-execute": "bypassPermissions",
}


def _extract_claude_results(raw_stdout: str) -> tuple[dict | None, str | None]:
    """Return ``(terminal_envelope, response_text)`` from a reviewer's stdout,
    handling BOTH the single-object ``json`` format and the ``stream-json``
    JSONL transcript the reviewer adapter now requests.

    The reviewer adapter requests ``--output-format stream-json --verbose`` so
    the persisted ``.stdout`` is the full tool-call transcript (every
    ``system`` / ``assistant`` / ``user`` / ``tool_use`` / ``tool_result``
    event), terminated by ``{"type":"result",...}`` line(s) carrying the SAME
    keys (``result``, ``is_error``, ``api_error_status``) used by single-object
    ``json`` envelopes. Preserving the stream is what lets an
    operator audit whether a reviewer actually ran verification vs.
    read-and-reasoned.

    - ``terminal_envelope`` — the LAST ``{"type":"result"}`` object (or the
      single object). Used for is_error / returncode / error-message checks:
      it is the session's terminal status.
    - ``response_text`` — the ``.result`` text of EVERY result event, joined
      in stream order. claude occasionally emits a SPURIOUS extra result turn
      AFTER delivering its verdict (observed live: a late async tool result
      nudged a "my review is already complete" epilogue with no JSON). Taking
      only the terminal turn lost that verdict and aborted the run at exit 70;
      joining all turns and letting :func:`~syncade.findings.parse_reviewer_output`
      pick the FINAL valid ``ReviewerOutput`` block recovers a verdict buried
      before a chatty epilogue. ``None`` when no result event carries a string
      ``result``.

    A single-object stdout yields ``(obj, obj["result"])`` for non-streaming
    JSON envelopes, the producer adapter, and fixtures. Returns ``(None,
    None)`` when no envelope is present at all (CLI-level failure / garbage);
    the caller maps that to ``ReviewerInvocationError`` (rc!=0) or
    ``ReviewerOutputError`` (rc==0).
    """
    stripped = raw_stdout.strip()
    if not stripped:
        return None, None
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            res = parsed.get("result")
            return parsed, (res if isinstance(res, str) else None)
    except json.JSONDecodeError:
        pass
    terminal: dict | None = None
    texts: list[str] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            terminal = obj
            res = obj.get("result")
            if isinstance(res, str):
                texts.append(res)
    return terminal, ("\n".join(texts) if texts else None)


class AnthropicAdapter:
    """ReviewerAdapter for the Anthropic ``claude`` CLI.

    ``build_invocation`` produces a ``claude -p`` argv that pins the
    model, sets effort/permissions per the ReviewerConfig, scopes file
    access to the worktree via ``--add-dir``, and requests the
    ``stream-json`` transcript (``--output-format stream-json --verbose``)
    so the reviewer's full tool-call stream is captured in the persisted
    ``.stdout`` (operator auditability — did the reviewer actually run
    verification?).

    ``parse_output`` validates that the subprocess succeeded, extracts the
    terminal ``{"type":"result"}`` envelope from the JSONL stream (via
    :func:`_extract_claude_results`, which also accepts single-object
    ``json`` stdout), pulls the ``.result`` field (the
    assistant's final text), and hands it to
    :func:`~syncade.findings.parse_reviewer_output`. The parser is robust
    to JSON-in-markdown-fences, which the CLI emits routinely (see the
    discovery doc).

    The adapter never shells out itself — :class:`Invocation` is data
    for the dispatcher to execute.
    """

    name = "anthropic"

    def check_auth(self) -> None:
        """No pre-flight check.

        Anthropic auth failures surface via the JSON envelope's
        ``is_error: true`` field in :meth:`parse_output` (see
        the claude CLI output — both bad-model and
        missing-auth shapes set ``is_error: true`` with the actionable
        message in ``.result``). The cost of catching an auth failure
        during the actual run is one wasted subprocess invocation,
        which is acceptable because Anthropic's ``claude login status``
        analog doesn't exist as a fast/standalone command — running
        the real reviewer is the cheapest auth probe available.

        See :meth:`ReviewerAdapter.check_auth` for the protocol
        contract.
        """
        return None

    def build_invocation(
        self,
        reviewer_config: ReviewerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        """Construct the ``claude -p`` invocation for a single reviewer run.

        The argv matches the form documented in
        the CLI output format:

        - ``claude -p`` with prompt on STDIN (PR-h-field-01 item 1): avoids the execve
          argument-list limit; stdin is read when no positional prompt is given
        - ``--output-format stream-json --verbose``: JSONL transcript on
          stdout (every tool_use/tool_result event), terminated by the
          ``{"type":"result"}`` envelope. ``--verbose`` is required by
          ``claude`` when ``stream-json`` is combined with ``-p``.
        - ``--model <id>``: model from the reviewer config (full name
          like ``claude-opus-4-6`` or alias like ``haiku``)
        - ``--effort <level>``: maps from ``thinking`` (see
          :attr:`syncade.config.ReviewerConfig.thinking` for the
          canonical list of accepted values)
        - ``--permission-mode <mode>``: maps from ``permissions``
          (``yolo`` / ``trusted-execute`` → ``bypassPermissions``).
          ``permissions="safe"`` is **rejected** —
          see :meth:`_validate_permissions`.
        - ``--add-dir <worktree>``: grants the reviewer tool-access to
          its worktree

        The subprocess runs with ``cwd = worktree_path`` and inherits
        the caller's environment so existing ``claude`` auth (keychain,
        OAuth, ``ANTHROPIC_API_KEY``) flows through.

        Raises:
            ValueError: If ``reviewer_config.provider`` is not
                ``"anthropic"`` — guards against the dispatcher
                accidentally sending a Codex or other-provider config
                to this adapter.
            ValueError: If ``reviewer_config.permissions`` is ``"safe"``
                — the corresponding ``--permission-mode default`` mode
                prompts for every tool use, and ``-p`` (non-interactive)
                can't answer prompts. The combination is operationally
                broken; surface it loudly at build time rather than
                letting the dispatcher hang waiting on a never-answered
                permission prompt.
        """
        self._validate_provider(reviewer_config.provider)
        self._validate_permissions(reviewer_config.permissions)
        # The prompt goes on STDIN, never argv: a reviewer diff can exceed the
        # execve argument ceiling (measured 1,044,422 B on macOS 15/arm64) and the
        # child then never exists — `[Errno 7] Argument list too long`, 0.0s, exit 40,
        # before any review happens. Pass on ONE channel only; both CLIs append a
        # piped stdin as an extra block when a positional prompt is also given.
        argv: list[str] = [
            "claude",
            "-p",
            # stream-json (+ required --verbose under -p) so the reviewer's
            # full tool-call transcript lands in the persisted .stdout for
            # auditability; the terminal {"type":"result"} line carries the
            # same envelope shape as single-object JSON output. See
            # see _extract_claude_results.
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            reviewer_config.model,
            "--effort",
            reviewer_config.thinking,
            "--permission-mode",
            _PERMISSION_MAPPING[reviewer_config.permissions],
            "--add-dir",
            str(worktree_path),
        ]
        return Invocation(
            argv=argv,
            cwd=worktree_path,
            env=apply_auth_to_env(worktree_scoped_env(worktree_path), reviewer_config),
            stdin_text=prompt,
            timeout_seconds=None,
        )

    @staticmethod
    def _validate_provider(provider: str) -> None:
        """Refuse a config whose ``provider`` is not ``"anthropic"``.

        The dispatcher routes each ReviewerConfig to the
        adapter whose :attr:`name` matches the config's ``provider``
        field. Defensive guard: if the dispatcher misroutes a config —
        say, a Codex reviewer to this adapter — fail loudly at
        build_invocation time rather than building a malformed
        ``claude`` argv and letting the subprocess fail with a
        confusing CLI error.
        """
        if provider != "anthropic":
            raise ValueError(
                f"AnthropicAdapter received a ReviewerConfig with "
                f"provider={provider!r}; expected 'anthropic'. The "
                f"dispatcher should route configs to the adapter whose "
                f"name matches the config's provider field."
            )

    @staticmethod
    def _validate_permissions(permissions: str) -> None:
        """Refuse unsupported reviewer permissions for the Anthropic adapter.

        ``--permission-mode default`` (the only sane CLI mapping for
        "safe") asks for confirmation on every tool use. In ``-p``
        mode there's no human to confirm, so the subprocess hangs on
        the first tool call until syncade's own timeout fires. Raising
        here turns a 2-minute hang into a 2-line stack trace.
        """
        if permissions == "safe":
            raise ValueError(
                "AnthropicAdapter cannot run a reviewer with "
                "permissions='safe' headlessly: --permission-mode=default "
                "prompts for every tool use and `claude -p` cannot answer "
                "prompts. Use 'trusted-execute' or 'yolo'."
            )
        if permissions not in _PERMISSION_MAPPING:
            valid = "', '".join(_PERMISSION_MAPPING)
            raise ValueError(
                "AnthropicAdapter received unsupported reviewer "
                f"permissions={permissions!r}; expected one of '{valid}'."
            )

    def parse_output(self, result: SubprocessResult) -> ReviewerOutput:
        """Parse a finished ``claude -p`` subprocess result into a reviewer
        verdict.

        Thin wrapper. :meth:`extract_final_text` does the envelope-strip and
        every failure check (that method's docstring carries the decision
        tree); :func:`~syncade.findings.parse_reviewer_output` then reads a
        verdict out of the text — it is robust to markdown-fenced JSON and
        picks the FINAL valid block, recovering a verdict even when claude
        appends a chatty no-JSON epilogue turn after it.

        Symmetric with
        :meth:`syncade.adapters.openai.CodexAdapter.parse_output`: both
        adapters are now ``extract_final_text`` → parse.
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
        """Extract claude's final response text from a finished ``claude -p``
        subprocess result.

        Implements
        :meth:`syncade.adapters.base.ReviewerAdapter.extract_final_text`; see
        that method for the contract. Distinct from
        :meth:`extract_response_text`, which does ONLY the envelope-strip on a
        raw stdout string with no failure checks at all.

        Decision tree (see the CLI-format notes for the underlying
        behavior table):

        1. Try to parse ``stdout`` as a JSON envelope. ``claude -p`` emits one
           even on the failures we care about. On ``claude 2.1.137`` both
           observed failure shapes (bad model, ``--bare`` missing auth) set
           ``is_error: true`` AND ``rc=1`` — the envelope ``.result`` field is
           where the human-readable error message lives, NOT stderr. The
           adapter ALSO handles ``rc=0`` with ``is_error: true`` as a
           defensive hedge against future CLI / auth-helper variants.
        2. If the envelope parsed:

           - ``envelope.is_error`` truthy OR ``result.returncode`` non-zero →
             :class:`ReviewerInvocationError`. The ``envelope.result`` text
             becomes the exception message; ``envelope.api_error_status`` (if
             present) is exposed on the exception's ``.api_error_status``
             attribute so the caller can distinguish transient (5xx, 429) from
             terminal (4xx) provider errors.
           - Otherwise (success path): return the joined text of every result
             turn. No text → ``empty_output_exception_class``.
        3. If the envelope did NOT parse:

           - ``rc`` non-zero → :class:`ReviewerInvocationError` with stdout /
             stderr in the message. CLI-level failures (unknown flag, missing
             prompt) land here — they don't produce a JSON envelope.
           - ``rc`` zero → ``empty_output_exception_class``. The subprocess
             ostensibly succeeded but didn't produce a parseable envelope —
             exit 70 territory.
        """
        # Extract the terminal result envelope (for is_error / returncode)
        # AND the joined text of every result turn (for the verdict parse)
        # from either single-object JSON stdout or the stream-json JSONL
        # transcript.
        envelope, response_text = _extract_claude_results(result.stdout)

        if envelope is not None:
            is_error = bool(envelope.get("is_error", False))
            api_error_status = envelope.get("api_error_status")
            # The CLI uses int or None; coerce anything else to None so
            # the documented attribute contract holds.
            if not isinstance(api_error_status, int):
                api_error_status = None

            envelope_result = envelope.get("result")

            if is_error or result.returncode != 0:
                # Provider-level failure. Surface the envelope's
                # message field (the human-readable error) so the user
                # sees "Not logged in" or "model not available" rather
                # than "claude exited with code 1:" with nothing after.
                if isinstance(envelope_result, str) and envelope_result:
                    msg = envelope_result
                else:
                    # Neutral message — this branch fires both when
                    # is_error=true with no result text AND when rc!=0
                    # with is_error=false (a defensive path that
                    # shouldn't happen in practice but is covered for
                    # future CLI-shape variability). Don't claim
                    # is_error=true unconditionally.
                    msg = (
                        f"claude failed with no result text "
                        f"(is_error={is_error}, "
                        f"api_error_status={api_error_status})"
                    )
                raise ReviewerInvocationError(
                    f"claude failed (rc={result.returncode}, "
                    f"api_error_status={api_error_status}): {msg[:300]}",
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    api_error_status=api_error_status,
                )

            # Success path. ``response_text`` is the joined text of every
            # result turn.
            if response_text is None:
                raise empty_output_exception_class(
                    f"claude result envelope carried no string 'result' text; "
                    f"stdout: {result.stdout[:200]!r}"
                )
            return response_text

        # Envelope did NOT parse. CLI-level failure if rc!=0 (unknown
        # flag, missing prompt — message is on stderr). rc=0 with no
        # envelope is "rc looked fine but output is garbage" → output
        # parse error.
        if result.returncode != 0:
            stderr_snippet = result.stderr.strip()[:200]
            stdout_snippet = result.stdout.strip()[:200]
            tail = stderr_snippet or stdout_snippet or "(no output)"
            raise ReviewerInvocationError(
                f"claude exited with code {result.returncode} and emitted "
                f"no parseable envelope: {tail}",
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                api_error_status=None,
            )
        raise empty_output_exception_class(
            f"claude returned rc=0 but stdout is not a JSON envelope; "
            f"stdout: {result.stdout[:200]!r}"
        )

    def extract_response_text(self, raw_stdout: str) -> str:
        """Extract the assistant's response text from a ``claude -p``
        envelope stdout.

        Reusable per-adapter helper that does ONLY the envelope-strip,
        without the surrounding ReviewerOutput parse or the
        is_error / returncode checks :meth:`parse_output` does on top.
        The orchestrator's prior-round-context plumbing
        (:mod:`syncade.orchestrator.prior_round`) calls this method
        with the raw stdout read off disk from
        ``<run-id>/round-(N-1)/<reviewer_name>.stdout`` — the round-N
        reviewer's ``{prior_round_output}`` placeholder gets the
        extracted text.

        Symmetric with
        :meth:`syncade.adapters.openai.CodexAdapter.extract_response_text`
        — both adapters expose the same single-method interface for
        the orchestrator to dispatch into.

        Args:
            raw_stdout: The raw stdout of a finished ``claude -p
                --output-format stream-json --verbose`` invocation (a JSONL
                transcript terminated by a ``{"type":"result"}`` line); a
                single-object ``json`` stdout is also accepted. The
                terminal result envelope's ``"result"`` field is returned.

        Returns:
            The assistant's final response text (the envelope's
            ``.result`` field).

        Raises:
            ReviewerOutputError: If ``raw_stdout`` isn't a parseable
                JSON dict, OR the dict's ``.result`` field is missing
                / not a string. Per the
                :class:`~syncade.findings.ReviewerOutputError` contract,
                this maps to exit 70 if it bubbles up through
                ``parse_output``; the orchestrator's prior-round
                loader catches it and falls back to passing the raw
                stdout through to the prompt (since cross-round
                context is best-effort).
        """
        _, response_text = _extract_claude_results(raw_stdout)
        if response_text is None:
            raise ReviewerOutputError(
                f"claude stdout has no parseable result text (neither a "
                f"single JSON object with a string 'result' nor a stream-json "
                f"result line): {raw_stdout[:200]!r}"
            )
        return response_text
