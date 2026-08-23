"""Tests for the PR-8 producer adapter Protocol + concrete implementations.

Mirrors the structure of ``tests/adapters/test_anthropic.py`` and
``tests/adapters/test_openai.py`` for the reviewer adapters: argv
shape tests against a fixture worktree, parse_output behavior
tests against synthesized :class:`SubprocessResult` instances, and
the headless-deadlock guard for ``permissions="safe"``.
"""

from __future__ import annotations

import json

import pytest

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.fake import FakeProducerAdapter
from syncade.adapters.producer import (
    ProducerAdapter,
    ProducerOutput,
    get_producer_adapter,
    known_producer_providers,
)
from syncade.adapters.producer_anthropic import AnthropicProducerAdapter
from syncade.adapters.producer_openai import OpenAIProducerAdapter
from syncade.adapters.registry import UnknownProviderError
from syncade.config import ProducerConfig
from syncade.findings import ReviewerOutputError
from syncade.process import SubprocessResult
from tests.test_worktree_env import make_worktree_src, resolve_syncade_in_child

# ---------------------------------------------------------------------------
# Protocol + registry
# ---------------------------------------------------------------------------


def test_anthropic_adapter_satisfies_protocol():
    """``@runtime_checkable`` ProducerAdapter check_auth + build +
    parse must all be present on the instance."""
    assert isinstance(AnthropicProducerAdapter(), ProducerAdapter)


def test_openai_adapter_satisfies_protocol():
    assert isinstance(OpenAIProducerAdapter(), ProducerAdapter)


def test_fake_adapter_satisfies_protocol():
    assert isinstance(FakeProducerAdapter(), ProducerAdapter)


def test_known_producer_providers_lists_both():
    """The producer registry ships with anthropic + openai out of the
    box (PR-8). The sorted helper is the public introspection
    surface — a future ``syncade providers --producer`` CLI would
    consume it."""
    providers = known_producer_providers()
    assert "anthropic" in providers
    assert "openai" in providers
    assert providers == sorted(providers)


def test_get_producer_adapter_anthropic_routes_correctly():
    adapter = get_producer_adapter("anthropic")
    assert isinstance(adapter, AnthropicProducerAdapter)
    assert adapter.name == "anthropic"


def test_get_producer_adapter_openai_routes_correctly():
    adapter = get_producer_adapter("openai")
    assert isinstance(adapter, OpenAIProducerAdapter)
    assert adapter.name == "openai"


def test_get_producer_adapter_returns_fresh_instance():
    """Each call returns a fresh adapter so test mutation doesn't
    leak across calls. Same pattern as the reviewer registry."""
    first = get_producer_adapter("anthropic")
    second = get_producer_adapter("anthropic")
    assert first is not second


def test_get_producer_adapter_unknown_provider_raises():
    with pytest.raises(UnknownProviderError) as exc_info:
        get_producer_adapter("nonexistent")
    assert exc_info.value.role == "producer"
    assert exc_info.value.requested == "nonexistent"
    msg = str(exc_info.value)
    # Both the bad input AND the known set are surfaced so a typo
    # in .syncade/config.toml is self-debuggable.
    assert "nonexistent" in msg
    assert "anthropic" in msg
    assert "openai" in msg


# ---------------------------------------------------------------------------
# AnthropicProducerAdapter — build_invocation argv shape
# ---------------------------------------------------------------------------


class TestAnthropicProducerBuildInvocation:
    """Argv-shape tests against the CLI output format's
    documented flag set. The producer's permission mapping is
    distinct from the reviewer adapter's: both `confined` and `yolo` policies are supported."""

    def test_trusted_permissions_rejected_when_schema_bypassed(self, tmp_path):
        adapter = AnthropicProducerAdapter()
        config = ProducerConfig.model_construct(
            provider="anthropic",
            model="claude-sonnet-4-6",
            thinking="high",
            permissions="trusted",
        )
        with pytest.raises(ValueError, match="trusted"):
            adapter.build_invocation(config, tmp_path, "the prompt")

    def test_confined_permissions_use_native_sandbox(self, tmp_path):
        adapter = AnthropicProducerAdapter()
        config = ProducerConfig(
            provider="anthropic",
            model="claude-opus-4-7",
            thinking="medium",
            permissions="confined",
        )
        inv = adapter.build_invocation(config, tmp_path, "prompt")
        assert "dontAsk" in inv.argv
        assert "bypassPermissions" not in inv.argv
        assert "acceptEdits" not in inv.argv
        assert "--safe-mode" in inv.argv
        assert "--no-session-persistence" in inv.argv
        assert "--disable-slash-commands" in inv.argv
        assert "--strict-mcp-config" in inv.argv
        assert '{"mcpServers":{}}' in inv.argv
        assert inv.argv[inv.argv.index("--tools") + 1] == "Bash"
        assert inv.argv[inv.argv.index("--allowedTools") + 1] == "Bash"
        settings = json.loads(inv.argv[inv.argv.index("--settings") + 1])
        assert settings == {
            "sandbox": {
                "enabled": True,
                "failIfUnavailable": True,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
            }
        }
        assert "claude-opus-4-7" in inv.argv
        assert "medium" in inv.argv

    def test_safe_permissions_rejected_at_build_invocation(self, tmp_path):
        """Schema rejects ``safe`` at config-load. This belt-and-
        braces guard catches the case where a caller constructs a
        ``ProducerConfig`` programmatically with the schema
        bypassed (typically in unit tests). Mirrors
        :meth:`AnthropicAdapter._validate_permissions`."""
        adapter = AnthropicProducerAdapter()
        # ``ProducerConfig.model_construct(**...)`` bypasses pydantic
        # validation — the schema's ``Literal`` rejection is what
        # production paths see; this test exercises the adapter-
        # side belt-and-braces.
        config = ProducerConfig.model_construct(
            provider="anthropic",
            model="x",
            thinking="high",
            permissions="safe",
        )
        with pytest.raises(ValueError) as exc_info:
            adapter.build_invocation(config, tmp_path, "prompt")
        msg = str(exc_info.value)
        assert "safe" in msg
        assert "confined" in msg

    def test_invalid_permissions_rejected_before_mapping_lookup(self, tmp_path):
        adapter = AnthropicProducerAdapter()
        config = ProducerConfig.model_construct(
            provider="anthropic",
            model="x",
            thinking="high",
            permissions="trusted-execute",
        )
        with pytest.raises(ValueError, match="trusted-execute") as exc_info:
            adapter.build_invocation(config, tmp_path, "prompt")
        assert "KeyError" not in str(exc_info.value)

    def test_wrong_provider_rejected_at_build_invocation(self, tmp_path):
        """Defensive guard against the registry misrouting a config."""
        adapter = AnthropicProducerAdapter()
        config = ProducerConfig.model_construct(
            provider="openai",  # wrong provider for this adapter
            model="x",
            thinking="high",
            permissions="confined",
        )
        with pytest.raises(ValueError) as exc_info:
            adapter.build_invocation(config, tmp_path, "prompt")
        assert "openai" in str(exc_info.value)
        assert "anthropic" in str(exc_info.value)

    def test_thinking_xhigh_passes_through_to_effort_arg(self, tmp_path):
        """PR-8.5 dogfood QA: ``xhigh`` is a valid value for
        ``claude --effort`` (verified live against claude 2.1.152).
        Same passthrough pattern as
        :class:`~syncade.adapters.anthropic.AnthropicAdapter`. The
        initial Task 4 implementation rejected it based on an
        unverified brief claim; first dogfood found the rejection
        was incorrect."""
        adapter = AnthropicProducerAdapter()
        config = ProducerConfig(
            provider="anthropic",
            model="sonnet",
            thinking="xhigh",
            permissions="confined",
        )
        inv = adapter.build_invocation(config, tmp_path, "prompt")
        idx = inv.argv.index("--effort")
        assert inv.argv[idx + 1] == "xhigh"

    def test_invocation_env_resolves_worktree_src_not_main(self, tmp_path):
        # PR-23: the producer subprocess must import `syncade` from its OWN
        # worktree, not MAIN's editable-install .pth. Real child process proof.
        worktree = make_worktree_src(tmp_path / "wt")
        adapter = AnthropicProducerAdapter()
        config = ProducerConfig(
            provider="anthropic", model="sonnet", thinking="high", permissions="confined"
        )
        inv = adapter.build_invocation(config, worktree, "prompt")
        resolved = resolve_syncade_in_child(inv.env, worktree)
        assert resolved.startswith(str((worktree / "src" / "syncade").resolve()))


# ---------------------------------------------------------------------------
# AnthropicProducerAdapter — parse_output
# ---------------------------------------------------------------------------


def _envelope(result_text: str, is_error: bool = False, api_error_status=None) -> str:
    """Synthesize a claude ``--output-format json`` envelope."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success" if not is_error else "error",
            "is_error": is_error,
            "api_error_status": api_error_status,
            "duration_ms": 1234,
            "result": result_text,
            "stop_reason": "end_turn",
            "session_id": "test-session",
        }
    )


class TestAnthropicProducerParseOutput:
    def test_success_returns_narrative_text(self):
        adapter = AnthropicProducerAdapter()
        result = SubprocessResult(
            returncode=0,
            stdout=_envelope("I edited foo.py and committed."),
            stderr="",
            duration_seconds=0.5,
        )
        out = adapter.parse_output(result)
        assert isinstance(out, ProducerOutput)
        assert out.narrative_text == "I edited foo.py and committed."

    def test_empty_narrative_allowed(self):
        """A producer that committed without narrating is valid —
        the orchestrator's stall detection is SHA-based, not
        narrative-based."""
        adapter = AnthropicProducerAdapter()
        result = SubprocessResult(
            returncode=0,
            stdout=_envelope(""),
            stderr="",
            duration_seconds=0.5,
        )
        out = adapter.parse_output(result)
        assert out.narrative_text == ""

    def test_is_error_envelope_raises_invocation_error(self):
        adapter = AnthropicProducerAdapter()
        result = SubprocessResult(
            returncode=1,
            stdout=_envelope("Not logged in.", is_error=True, api_error_status=401),
            stderr="",
            duration_seconds=0.3,
        )
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.parse_output(result)
        assert exc_info.value.api_error_status == 401
        assert "Not logged in" in str(exc_info.value)

    def test_nonzero_rc_without_envelope_raises_invocation_error(self):
        adapter = AnthropicProducerAdapter()
        result = SubprocessResult(
            returncode=2,
            stdout="not a json envelope",
            stderr="error: unknown flag --foo",
            duration_seconds=0.1,
        )
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.parse_output(result)
        assert "unknown flag" in str(exc_info.value)

    def test_zero_rc_no_envelope_raises_output_error(self):
        adapter = AnthropicProducerAdapter()
        result = SubprocessResult(
            returncode=0,
            stdout="just some prose, no envelope",
            stderr="",
            duration_seconds=0.5,
        )
        with pytest.raises(ReviewerOutputError):
            adapter.parse_output(result)


class TestOpenAIProducerPermissionValidation:
    def test_invalid_permissions_rejected_before_trusted_fallback(self, tmp_path):
        adapter = OpenAIProducerAdapter()
        config = ProducerConfig.model_construct(
            provider="openai",
            model="gpt-5.5",
            thinking="high",
            permissions="trusted-execute",
        )

        with pytest.raises(ValueError, match="trusted-execute") as exc_info:
            adapter.build_invocation(config, tmp_path, "prompt")

        assert "workspace-write" not in str(exc_info.value)
