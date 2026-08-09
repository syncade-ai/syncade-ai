"""Producer adapter for the Anthropic ``claude`` CLI.

Symmetric to :class:`syncade.adapters.anthropic.AnthropicAdapter` but
for the fix-it subprocess that runs after a NO-SHIP round. The
producer needs to make file edits AND commit them from a headless
subprocess. Real ``claude -p`` needs ``bypassPermissions`` for that
commit step because ``acceptEdits`` auto-approves file edits but not
bash commands such as ``git commit``.

Built against the claude CLI's observed JSON output —
the actual observed behavior of ``claude 2.1.137 (Claude Code)``. If
you're changing flag strings or the output-parsing path here,
re-read that doc first. The envelope-parsing logic is the SAME as
the reviewer adapter's (``is_error`` / ``result`` / ``api_error_status``);
only the permission-mode mapping and the output type differ.
"""

from __future__ import annotations

import json
from pathlib import Path

from syncade.adapters.base import (
    Invocation,
    ReviewerInvocationError,
)
from syncade.adapters.producer import ProducerOutput
from syncade.config import ProducerConfig
from syncade.config_auth import apply_auth_to_env
from syncade.findings import ReviewerOutputError
from syncade.process import SubprocessResult
from syncade.worktree_env import worktree_scoped_env

# Map :data:`syncade.config.ProducerPermissions` to the corresponding
# claude ``--permission-mode`` value.
#
# ``yolo`` → ``bypassPermissions``: full bypass. This is the default
# because live producer smokes need it to commit from headless mode.
#
# Sandboxed/stale values are intentionally NOT in this mapping. The schema
# rejects them at config-load; the validator below rejects them again for
# callers that bypass Pydantic.
_PRODUCER_PERMISSION_MAPPING: dict[str, str] = {
    "yolo": "bypassPermissions",
}


class AnthropicProducerAdapter:
    """ProducerAdapter for the Anthropic ``claude`` CLI.

    ``build_invocation`` produces a ``claude -p`` argv that:

    - pins the model from :attr:`ProducerConfig.model`
    - sets effort from :attr:`ProducerConfig.thinking`
    - sets ``--permission-mode`` from :attr:`ProducerConfig.permissions`
      (``yolo`` → ``bypassPermissions``; stale/sandboxed values rejected)
    - scopes file access to the producer worktree via ``--add-dir``
    - requests JSON output via ``--output-format json``

    ``parse_output`` re-uses the reviewer adapter's envelope-parsing
    decision tree (``is_error`` / non-zero rc → invocation error;
    success → extract ``.result`` text) but returns a
    :class:`ProducerOutput` rather than a :class:`ReviewerOutput`. The
    text is preserved verbatim — there is no structured-JSON parse on
    top, because the producer doesn't emit one.

    ``check_auth`` is an explicit no-op for the same reason
    :class:`AnthropicAdapter.check_auth` is: Anthropic doesn't expose
    a fast standalone login-status check, and the cost of catching
    an auth failure during the actual run is one wasted subprocess.

    The adapter never shells out itself — :class:`Invocation` is
    data for the orchestrator's
    :func:`syncade.process.run_subprocess` to execute.
    """

    name = "anthropic"

    def check_auth(self) -> None:
        """No pre-flight check — Anthropic auth failures surface via
        the JSON envelope's ``is_error: true`` field in
        :meth:`parse_output`.

        Mirrors :meth:`AnthropicAdapter.check_auth`'s no-op design.
        Defining the method explicitly (not relying on the Protocol's
        default ellipsis) is required because
        :class:`syncade.adapters.producer.ProducerAdapter` is
        ``@runtime_checkable`` — see the reviewer adapter's docstring
        for the full Protocol-conformance rationale.
        """
        return None

    def build_invocation(
        self,
        producer_config: ProducerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        """Construct the ``claude -p`` invocation for one producer round.

        Argv shape matches the reviewer adapter's surface (same
        discovery doc, same flag set) with three differences:

        1. ``--permission-mode`` maps from the producer-specific
           :data:`syncade.config.ProducerPermissions`.
        2. The producer-permission value is ``yolo`` because
           real headless producers must run ``git commit`` unattended.
        3. There is no separate ``-c approval_policy=...`` flag —
           claude's ``--permission-mode`` is the complete control
           surface (compared with codex which needs both ``-s`` and
           the ``-c`` config override).

        Raises:
            ValueError: If ``producer_config.provider`` is not
                ``"anthropic"`` — defensive guard against the
                producer registry misrouting a config to this
                adapter.
            ValueError: If ``producer_config.permissions`` is
                ``"safe"``. The schema already rejects this at
                config-load, but the validator here protects callers
                that construct a :class:`ProducerConfig` directly
                (e.g. test code passing a dict with the field
                bypassed).
        """
        self._validate_provider(producer_config.provider)
        self._validate_permissions(producer_config.permissions)
        # The prompt goes on STDIN, never argv: a reviewer diff can exceed the
        # execve argument ceiling (measured 1,044,422 B on macOS 15/arm64) and the
        # child then never exists — `[Errno 7] Argument list too long`, 0.0s, exit 40,
        # before any review happens. Pass on ONE channel only; both CLIs append a
        # piped stdin as an extra block when a positional prompt is also given.
        argv: list[str] = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--model",
            producer_config.model,
            "--effort",
            producer_config.thinking,
            "--permission-mode",
            _PRODUCER_PERMISSION_MAPPING[producer_config.permissions],
            "--add-dir",
            str(worktree_path),
        ]
        return Invocation(
            argv=argv,
            cwd=worktree_path,
            env=apply_auth_to_env(worktree_scoped_env(worktree_path), producer_config),
            stdin_text=prompt,
            timeout_seconds=None,
        )

    @staticmethod
    def _validate_provider(provider: str) -> None:
        """Refuse a config whose ``provider`` is not ``"anthropic"``.

        Defensive guard mirroring
        :meth:`AnthropicAdapter._validate_provider`. The producer
        registry routes configs by provider name; if a future
        registry update misroutes a codex config to this adapter,
        the build_invocation argv would still LOOK valid (model and
        effort flags pass through) but the spawned subprocess would
        be ``claude`` running with a codex-shaped model string.
        Raising here turns a confusing CLI-side rejection into a
        legible config-side rejection.
        """
        if provider != "anthropic":
            raise ValueError(
                f"AnthropicProducerAdapter received a ProducerConfig "
                f"with provider={provider!r}; expected 'anthropic'. The "
                f"producer registry should route configs to the adapter "
                f"whose name matches the config's provider field."
            )

    @staticmethod
    def _validate_permissions(permissions: str) -> None:
        """Refuse unsupported permissions at build time.

        The :data:`ProducerPermissions` schema-level rejection handles this for configs loaded via
        :mod:`syncade.config_loader`. This validator is the
        belt-and-braces guard for callers that bypass the schema —
        e.g. unit tests that construct a :class:`ProducerConfig`
        from a kwargs dict, or a future plugin that builds configs
        programmatically.

        Same reasoning as the reviewer adapter's analogous guard:
        ``--permission-mode default`` would prompt for every tool
        use, and ``claude -p`` can't answer prompts headlessly. The
        subprocess hangs on the first tool call until the
        orchestrator's timeout fires.
        """
        if permissions == "safe":
            raise ValueError(
                "AnthropicProducerAdapter cannot run a producer with "
                "permissions='safe' headlessly: --permission-mode=default "
                "prompts for every tool use and `claude -p` cannot answer "
                "prompts. Use 'yolo' for headless producer commits."
            )
        if permissions not in _PRODUCER_PERMISSION_MAPPING:
            valid = "', '".join(_PRODUCER_PERMISSION_MAPPING)
            raise ValueError(
                "AnthropicProducerAdapter received unsupported producer "
                f"permissions={permissions!r}; expected one of '{valid}'."
            )

    def parse_output(self, result: SubprocessResult) -> ProducerOutput:
        """Parse a finished ``claude -p`` subprocess result.

        Decision tree (the
        underlying behavior table — same envelope shape as the
        reviewer adapter):

        1. Try to parse ``stdout`` as the JSON envelope ``claude
           -p --output-format json`` emits.
        2. If the envelope parsed AND (``is_error`` truthy OR
           ``returncode != 0``) → raise
           :class:`ReviewerInvocationError` carrying the envelope's
           ``.result`` text as the human-readable message and the
           ``.api_error_status`` for distinguishing transient (5xx,
           429) vs terminal (4xx) provider failures.
        3. If the envelope parsed AND the call succeeded → return
           :class:`ProducerOutput(narrative_text=envelope.result)`.
           No further structured-JSON parsing on top; the producer
           doesn't emit a schema-bound payload.
        4. If the envelope did NOT parse AND ``rc != 0`` → raise
           :class:`ReviewerInvocationError` with the stdout/stderr
           tail as the message (CLI-level failure like unknown
           flag).
        5. If the envelope did NOT parse AND ``rc == 0`` → raise
           :class:`ReviewerOutputError`. Unusual for the producer
           but possible if claude's output format ever changes; the
           orchestrator treats this as a producer-side failure for
           verdict purposes (exit 40), same bucket as a reviewer
           parse failure since the producer can't be re-asked to
           produce a different output format mid-run.
        """
        envelope: dict | None = None
        try:
            parsed = json.loads(result.stdout)
            if isinstance(parsed, dict):
                envelope = parsed
        except json.JSONDecodeError:
            pass

        if envelope is not None:
            is_error = bool(envelope.get("is_error", False))
            api_error_status = envelope.get("api_error_status")
            if not isinstance(api_error_status, int):
                api_error_status = None

            envelope_result = envelope.get("result")

            if is_error or result.returncode != 0:
                if isinstance(envelope_result, str) and envelope_result:
                    msg = envelope_result
                else:
                    msg = (
                        f"claude (producer) failed with no result text "
                        f"(is_error={is_error}, "
                        f"api_error_status={api_error_status})"
                    )
                raise ReviewerInvocationError(
                    f"claude (producer) failed (rc={result.returncode}, "
                    f"api_error_status={api_error_status}): {msg[:300]}",
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    api_error_status=api_error_status,
                )

            # Success path — narrative_text is the envelope's result
            # field. Allow empty strings (a producer that committed
            # without narrating is valid).
            if not isinstance(envelope_result, str):
                raise ReviewerOutputError(
                    f"claude (producer) envelope missing 'result' "
                    f"string field (got {type(envelope_result).__name__}); "
                    f"stdout: {result.stdout[:200]!r}"
                )
            return ProducerOutput(narrative_text=envelope_result)

        # Envelope did NOT parse. rc!=0 → CLI-level invocation
        # failure; rc==0 → output format unexpected.
        if result.returncode != 0:
            stderr_snippet = result.stderr.strip()[:200]
            stdout_snippet = result.stdout.strip()[:200]
            tail = stderr_snippet or stdout_snippet or "(no output)"
            raise ReviewerInvocationError(
                f"claude (producer) exited with code {result.returncode} "
                f"and emitted no parseable envelope: {tail}",
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                api_error_status=None,
            )
        raise ReviewerOutputError(
            f"claude (producer) returned rc=0 but stdout is not a JSON "
            f"envelope; stdout: {result.stdout[:200]!r}"
        )
