"""Producer adapter for the OpenAI ``codex`` CLI.

Symmetric to :class:`syncade.adapters.openai.CodexAdapter` but for
the fix-it subprocess (confined or yolo, per config). The output type is
:class:`syncade.adapters.producer.ProducerOutput` (just the narrative text),
not a structured
:class:`syncade.findings.ReviewerOutput`.

Built against the codex CLI's observed JSONL output.
``codex exec``'s JSONL extraction reuses
:meth:`CodexAdapter.extract_final_text` so the
shared envelope-parsing path stays one source of truth (this is
the same pattern used when the synthesizer reuses the
extractor with a different no-agent-message exception class).
"""

from __future__ import annotations

import json
from pathlib import Path

from syncade.adapters.base import Invocation
from syncade.adapters.openai import _YOLO_FLAG, CodexAdapter, _check_codex_auth
from syncade.adapters.producer import ProducerOutput
from syncade.auth_preflight import assert_codex_reality_honours_declaration
from syncade.config import ProducerConfig
from syncade.config_auth import apply_auth_to_env
from syncade.findings import ReviewerOutputError
from syncade.process import SubprocessResult
from syncade.worktree_env import worktree_scoped_env

_PRODUCER_PERMISSION_VALUES: tuple[str, ...] = ("confined", "yolo")


class OpenAIProducerAdapter:
    """ProducerAdapter for the OpenAI ``codex`` CLI.

    ``build_invocation`` produces a ``codex exec --json`` argv that:

    - pins the model from :attr:`ProducerConfig.model`
    - sets reasoning effort via ``-c model_reasoning_effort=<level>``
      (codex has no dedicated ``--effort`` flag — same discovery-doc
      finding the reviewer adapter relies on)
    - uses ``workspace-write`` with only the actor repository and its real
      ``.git`` directory writable when ``permissions == "confined"``; for
      ``permissions == "yolo"`` the sandbox is disabled entirely
    - scopes file access to the standalone producer repository via ``-C`` and
      ``--add-dir``

    ``parse_output`` extracts the final ``agent_message`` text via
    :meth:`CodexAdapter.extract_final_text`, passing
    :class:`ReviewerOutputError` as the no-agent-message exception
    class (the producer doesn't have a phase-specific exception
    type, and exit 40 is the right bucket either way — a producer
    that completed cleanly but emitted no agent message is
    operationally similar to a reviewer that did the same).

    ``check_auth`` runs ``codex login status``. The producer and
    reviewer adapters share auth state (same ``~/.codex/auth.json``), but the
    method remains available for callers that drive the producer phase in
    isolation.

    The adapter never shells out itself in ``build_invocation`` or
    ``parse_output``. ``check_auth`` is the one exception (same
    pattern as :class:`CodexAdapter.check_auth`).
    """

    name = "openai"

    def check_auth(self) -> None:
        _check_codex_auth(
            binary_missing_message=(
                "codex binary not found on PATH — install codex-cli to "
                "run the producer subprocess (the OpenAI Codex CLI, not "
                "a third-party fork)"
            )
        )

    def build_invocation(
        self,
        producer_config: ProducerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        """Construct the ``codex exec --json`` invocation for one
        producer round.

        Same argv shape as :meth:`CodexAdapter.build_invocation` (per
        the codex CLI output notes) with the
        :data:`syncade.config.ProducerPermissions` value-set (``confined`` | ``yolo``).

        Raises:
            ValueError: If ``producer_config.provider`` is not
                ``"openai"`` — defensive guard against misroute.
            ValueError: If ``producer_config.permissions`` is
                ``"safe"`` — belt-and-braces guard for callers that
                bypass the schema's :data:`ProducerPermissions`
                Literal.
        """
        self._validate_provider(producer_config.provider)
        self._validate_permissions(producer_config.permissions)
        # Structural backstop: refuse to spawn codex for a non-auto declaration the probed
        # login does not honour, on any path that skipped the CLI auth gate. No-op on the
        # gated CLI path. See syncade.auth_preflight.
        assert_codex_reality_honours_declaration(producer_config)

        argv: list[str] = [
            "codex",
            "exec",
            "--json",
            "--model",
            producer_config.model,
            "-c",
            f"model_reasoning_effort={producer_config.thinking}",
        ]
        if producer_config.permissions == "confined":
            # The actor's own `.git` is named as an ADDITIONAL writable root. Under the old
            # linked-worktree topology it could not be: the real gitdir lived in the operator
            # repository, outside any workspace root, which is why `workspace-write` could not
            # write `.git/index.lock` and why `yolo` used to be mandatory (PR-8 R2.T7). Item 3's
            # standalone repository is what makes this shape possible at all.
            # `/tmp` and `$TMPDIR` are excluded as IMPLICIT writable roots so the sandbox does
            # not silently grant a shared scratch path outside the actor's store.
            argv += [
                "-s",
                "workspace-write",
                "-c",
                "approval_policy=never",
                "-c",
                "sandbox_workspace_write.writable_roots="
                f"{json.dumps([str(worktree_path / '.git')])}",
                "-c",
                "sandbox_workspace_write.exclude_slash_tmp=true",
                "-c",
                "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
            ]
        else:
            # `yolo`: no sandbox, no approvals. The operator's disclosed opt-out.
            argv.append(_YOLO_FLAG)
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
            env=apply_auth_to_env(worktree_scoped_env(worktree_path), producer_config),
            stdin_text=prompt,
            timeout_seconds=None,
        )

    @staticmethod
    def _validate_provider(provider: str) -> None:
        """Refuse a config whose ``provider`` is not ``"openai"``.

        Mirrors :meth:`CodexAdapter._validate_provider`. The
        producer registry routes by provider name; this guard
        catches a misroute at build time instead of letting the
        subprocess fail with a confusing codex-side error after
        non-codex flags get spliced in.
        """
        if provider != "openai":
            raise ValueError(
                f"OpenAIProducerAdapter received a ProducerConfig with "
                f"provider={provider!r}; expected 'openai'. The "
                f"producer registry should route configs to the adapter "
                f"whose name matches the config's provider field."
            )

    @staticmethod
    def _validate_permissions(permissions: str) -> None:
        """Refuse unsupported permissions at build time.

        Same logic as the reviewer adapter's analogous guard plus
        :meth:`AnthropicProducerAdapter._validate_permissions`. The
        :data:`ProducerPermissions` Literal already rejects ``safe``
        at config-load; this guard catches the case where a caller
        constructs a :class:`ProducerConfig` programmatically and
        bypasses the schema (typically in unit tests).
        """
        if permissions not in _PRODUCER_PERMISSION_VALUES:
            valid = "', '".join(_PRODUCER_PERMISSION_VALUES)
            raise ValueError(
                "OpenAIProducerAdapter received unsupported producer "
                f"permissions={permissions!r}; expected one of '{valid}'."
            )

    def parse_output(self, result: SubprocessResult) -> ProducerOutput:
        """Parse a finished ``codex exec --json`` subprocess result.

        Decision tree:

        1. Extract the final ``agent_message`` text via
           :meth:`CodexAdapter.extract_final_text`,
           passing :class:`ReviewerOutputError` as the
           no-agent-message exception class.
        2. The extractor itself handles subprocess-side failures
           (``turn.failed`` events, non-zero rc, unrecovered bare ``error``
           streams, auth signatures) — it raises
           :class:`ReviewerInvocationError` for those, which the
           orchestrator's producer-phase exception handler maps to
           exit 40 (subprocess failure).
        3. On success, wrap the text in
           :class:`ProducerOutput(narrative_text=...)`. No further
           structured parsing — the producer doesn't emit a
           schema-bound payload (unlike the reviewer adapter where
           the next step is ``parse_reviewer_output`` against the
           extracted text).

        :meth:`CodexAdapter.extract_final_text` is
        reused (rather than re-implemented in this module) for the
        same reason the synthesizer uses it: codex's
        JSONL stream-extraction logic is non-trivial, and a divergent
        implementation in the producer adapter would have its own
        edge cases to discover (turn.failed-vs-error event
        precedence, auth-signature detection, empty-agent-message
        handling).
        """
        # Reuse the codex reviewer adapter's extraction helper via
        # an internal CodexAdapter instance. The instance is
        # stateless across calls, so allocating fresh per parse_output
        # is cheap and avoids stuffing the helper into a static
        # @classmethod (which would need to update CodexAdapter's
        # surface unnecessarily).
        codex_helper = CodexAdapter()
        final_text = codex_helper.extract_final_text(
            result,
            empty_output_exception_class=ReviewerOutputError,
        )
        return ProducerOutput(narrative_text=final_text)
