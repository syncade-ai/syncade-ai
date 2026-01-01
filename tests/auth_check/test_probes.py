"""Tests for :mod:`syncade.auth_check` per-provider probes (PR-9 Task 4).

The ``_probe_anthropic`` / ``_probe_openai`` paths: JSON-envelope
parsing for anthropic and ``CodexAdapter.check_auth`` delegation
for openai. These inject fakes via ``monkeypatch`` so the suite
never shells out to a real CLI.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Anthropic probe — JSON envelope parsing paths
# ---------------------------------------------------------------------------


def test_probe_anthropic_success_path(monkeypatch):
    """Happy path through ``_probe_anthropic``: subprocess returns
    a clean JSON envelope with ``is_error=false`` and the sentinel
    in ``.result``. Verifies the argv shape passed to
    ``run_subprocess`` and the parsed result."""
    from syncade import auth_check
    from syncade.process import SubprocessResult

    captured_argv: dict[str, object] = {}

    def fake_run_subprocess(argv, **kwargs):
        captured_argv["argv"] = argv
        captured_argv["kwargs"] = kwargs
        return SubprocessResult(
            returncode=0,
            stdout='{"is_error": false, "result": "AUTH OK"}',
            stderr="",
            duration_seconds=1.4,
        )

    monkeypatch.setattr(auth_check, "run_subprocess", fake_run_subprocess)

    result = auth_check._probe_anthropic("claude-opus-4-6", timeout_seconds=30.0)
    assert result.ok is True
    assert result.provider == "anthropic"
    assert result.model == "claude-opus-4-6"
    assert "OK" in result.detail

    argv = captured_argv["argv"]
    assert argv[0] == "claude"
    assert argv[1] == "-p"
    # The probe prompt with sentinel directive.
    assert argv[2] == "respond with exactly: AUTH OK"
    assert "--output-format" in argv
    assert "json" in argv
    assert "--model" in argv
    assert "claude-opus-4-6" in argv


def test_probe_anthropic_is_error_returns_failure(monkeypatch):
    """``is_error=true`` envelope → failure result with the
    envelope's ``.result`` text in the detail (operator-visible
    remediation guidance)."""
    from syncade import auth_check
    from syncade.process import SubprocessResult

    def fake_run_subprocess(argv, **kwargs):
        return SubprocessResult(
            returncode=1,
            stdout=('{"is_error": true, "result": "Invalid API key", "api_error_status": 401}'),
            stderr="",
            duration_seconds=0.5,
        )

    monkeypatch.setattr(auth_check, "run_subprocess", fake_run_subprocess)

    result = auth_check._probe_anthropic("claude-opus-4-6", timeout_seconds=30.0)
    assert result.ok is False
    assert "Invalid API key" in result.detail
    assert "401" in result.detail
    assert "claude" in result.detail.lower()


def test_probe_anthropic_sentinel_missing_returns_failure(monkeypatch):
    """``is_error=false`` envelope but the sentinel isn't in
    ``.result`` → failure. Distinguishes the "auth works but the
    model didn't follow the instruction" case from the auth-fail
    case so the operator knows to retry rather than re-authenticate."""
    from syncade import auth_check
    from syncade.process import SubprocessResult

    def fake_run_subprocess(argv, **kwargs):
        return SubprocessResult(
            returncode=0,
            stdout='{"is_error": false, "result": "Sure thing!"}',
            stderr="",
            duration_seconds=0.8,
        )

    monkeypatch.setattr(auth_check, "run_subprocess", fake_run_subprocess)

    result = auth_check._probe_anthropic("claude-opus-4-6", timeout_seconds=30.0)
    assert result.ok is False
    assert "AUTH OK" in result.detail  # the missing sentinel is named
    assert "retry once" in result.detail


def test_probe_anthropic_sentinel_with_wrapping_passes(monkeypatch):
    """The brief: parse with ``in`` not ``==`` to tolerate
    provider-side wrapping. ``"Sure: AUTH OK"`` should pass."""
    from syncade import auth_check
    from syncade.process import SubprocessResult

    def fake_run_subprocess(argv, **kwargs):
        return SubprocessResult(
            returncode=0,
            stdout='{"is_error": false, "result": "Sure thing: AUTH OK done"}',
            stderr="",
            duration_seconds=1.1,
        )

    monkeypatch.setattr(auth_check, "run_subprocess", fake_run_subprocess)

    result = auth_check._probe_anthropic("claude-opus-4-6", timeout_seconds=30.0)
    assert result.ok is True


def test_probe_anthropic_non_json_returns_failure(monkeypatch):
    """Stdout isn't valid JSON → failure with operator-actionable
    message (CLI version mismatch suggestion)."""
    from syncade import auth_check
    from syncade.process import SubprocessResult

    def fake_run_subprocess(argv, **kwargs):
        return SubprocessResult(
            returncode=2,
            stdout="usage: claude [options]",
            stderr="error: unknown flag",
            duration_seconds=0.05,
        )

    monkeypatch.setattr(auth_check, "run_subprocess", fake_run_subprocess)

    result = auth_check._probe_anthropic("claude-opus-4-6", timeout_seconds=30.0)
    assert result.ok is False
    assert "non-JSON" in result.detail


def test_probe_anthropic_binary_not_found(monkeypatch):
    """``SubprocessNotFoundError`` from ``run_subprocess`` →
    failure naming the missing binary."""
    from syncade import auth_check
    from syncade.process import SubprocessNotFoundError

    def fake_run_subprocess(argv, **kwargs):
        raise SubprocessNotFoundError("claude")

    monkeypatch.setattr(auth_check, "run_subprocess", fake_run_subprocess)

    result = auth_check._probe_anthropic("claude-opus-4-6", timeout_seconds=30.0)
    assert result.ok is False
    assert "claude binary not found" in result.detail


def test_probe_anthropic_timeout(monkeypatch):
    """``SubprocessTimeoutError`` from ``run_subprocess`` →
    failure naming the timeout duration."""
    from syncade import auth_check
    from syncade.process import SubprocessTimeoutError

    def fake_run_subprocess(argv, **kwargs):
        raise SubprocessTimeoutError("timed out", stdout="", stderr="", timeout=30.0)

    monkeypatch.setattr(auth_check, "run_subprocess", fake_run_subprocess)

    result = auth_check._probe_anthropic("claude-opus-4-6", timeout_seconds=30.0)
    assert result.ok is False
    assert "timed out" in result.detail
    assert "30" in result.detail


# ---------------------------------------------------------------------------
# Openai probe — delegates to CodexAdapter.check_auth
# ---------------------------------------------------------------------------


def test_probe_openai_success(monkeypatch):
    """``CodexAdapter.check_auth`` returns None → OK result.
    Verifies the probe delegates to the adapter rather than
    duplicating ``codex login status`` parsing."""
    from syncade import auth_check

    check_calls: list[None] = []

    def fake_check_auth(self) -> None:
        check_calls.append(None)

    monkeypatch.setattr("syncade.adapters.openai.CodexAdapter.check_auth", fake_check_auth)

    result = auth_check._probe_openai("gpt-5.5", timeout_seconds=30.0)
    assert result.ok is True
    assert result.provider == "openai"
    assert result.model == "gpt-5.5"
    assert len(check_calls) == 1


def test_probe_openai_failure_surfaces_adapter_message(monkeypatch):
    """``CodexAdapter.check_auth`` raises
    :class:`ReviewerInvocationError` → failure with the adapter's
    message verbatim. Keeps the auth-check from duplicating the
    'run codex login' remediation copy."""
    from syncade import auth_check
    from syncade.adapters.base import ReviewerInvocationError

    def fake_check_auth(self) -> None:
        raise ReviewerInvocationError(
            "codex auth check failed: Not logged in — run `codex login`",
            returncode=1,
            stdout="Not logged in",
            stderr="",
            api_error_status=None,
        )

    monkeypatch.setattr("syncade.adapters.openai.CodexAdapter.check_auth", fake_check_auth)

    result = auth_check._probe_openai("gpt-5.5", timeout_seconds=30.0)
    assert result.ok is False
    assert "codex login" in result.detail
    assert "Not logged in" in result.detail
