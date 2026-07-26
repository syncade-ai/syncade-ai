"""Tests for :class:`syncade.adapters.openai.CodexAdapter`.

``build_invocation`` and ``parse_output`` are pure-data tests with
canned :class:`SubprocessResult` objects. ``check_auth`` requires real
subprocess calls (we test against the actual ``codex login status``
command) and skips if ``codex`` is not on PATH — same discipline as
``tests/test_process.py``.

End-to-end smoke tests live in ``tests/smoke/test_codex_smoke.py`` and
run only via ``pytest -m smoke``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncade.adapters.base import (
    Invocation,
    ReviewerAdapter,
    ReviewerInvocationError,
)
from syncade.adapters.openai import CodexAdapter
from syncade.findings import ReviewerOutput, ReviewerOutputError
from syncade.process import SubprocessResult
from tests.adapters._openai_helpers import _jsonl_envelope, _make_config
from tests.test_worktree_env import make_worktree_src, resolve_syncade_in_child

# ---------------------------------------------------------------------------
# build_invocation — argv / cwd / env construction
# ---------------------------------------------------------------------------


class TestBuildInvocation:
    def test_basic_yolo_high_invocation(self, tmp_path):
        adapter = CodexAdapter()
        config = _make_config()
        prompt = "review this please"
        inv = adapter.build_invocation(config, tmp_path, prompt)

        assert isinstance(inv, Invocation)
        # argv[0..1] is "codex exec"
        assert inv.argv[0] == "codex"
        assert inv.argv[1] == "exec"
        # Output is JSON for clean parsing
        assert "--json" in inv.argv
        # Model + effort + worktree paths
        assert "--model" in inv.argv
        assert "gpt-5-codex" in inv.argv
        # Effort is a config override, not a flag
        assert "-c" in inv.argv
        assert "model_reasoning_effort=high" in inv.argv
        # yolo uses the combined bypass flag, NOT the two-flag form
        assert "--dangerously-bypass-approvals-and-sandbox" in inv.argv
        assert "-s" not in inv.argv  # combined flag obviates this
        # cwd flags
        assert "-C" in inv.argv
        assert "--add-dir" in inv.argv
        assert str(tmp_path) in inv.argv
        # Prompt is positional, LAST in argv (clap parsing)
        assert inv.argv[-1] == prompt
        # cwd is the worktree; env is inherited; nothing on stdin.
        assert inv.cwd == tmp_path
        assert inv.env  # not empty
        assert inv.stdin_text is None
        assert inv.timeout_seconds is None

    def test_trusted_execute_permissions_use_sandbox_plus_config_override(self, tmp_path):
        """trusted-execute maps to `-s workspace-write -c approval_policy=never`.

        `codex exec` does NOT accept `-a/--ask-for-approval` (the flag
        exists on the top-level `codex` command but not the `exec`
        subcommand — verified live on codex-cli 0.130.0). The generic
        `-c` config override is the documented exec-subcommand path
        for the same effect."""

        adapter = CodexAdapter()
        config = _make_config(permissions="trusted-execute")
        inv = adapter.build_invocation(config, tmp_path, "x")
        # No yolo bypass flag, no bogus -a flag
        assert "--dangerously-bypass-approvals-and-sandbox" not in inv.argv
        assert "-a" not in inv.argv
        assert "--ask-for-approval" not in inv.argv
        # -s workspace-write
        assert "-s" in inv.argv
        idx_s = inv.argv.index("-s")
        assert inv.argv[idx_s + 1] == "workspace-write"
        # approval_policy=never comes through as a -c config override.
        # There are two -c flags in trusted-execute argv
        # (model_reasoning_effort is the other) so search for the value
        # explicitly.
        assert "approval_policy=never" in inv.argv

    @pytest.mark.parametrize("thinking", ["low", "medium", "high", "xhigh", "max"])
    def test_thinking_to_effort_config_override(self, tmp_path, thinking):
        """PR-8.5 Task 4 extends the parametrization with ``xhigh``:
        codex's reasoning-effort tier above ``high``, verified live
        against codex 0.134.0. CodexAdapter does no value-mapping —
        the effort string passes through verbatim as
        ``-c model_reasoning_effort=<thinking>``, so adding to the
        schema's Literal automatically routes xhigh through the
        existing argv builder without any adapter-side changes."""
        adapter = CodexAdapter()
        config = _make_config(thinking=thinking)
        inv = adapter.build_invocation(config, tmp_path, "x")
        # Effort comes through as the -c flag's value
        assert f"model_reasoning_effort={thinking}" in inv.argv

    def test_model_pins_directly_from_config(self, tmp_path):
        adapter = CodexAdapter()
        for model in ("gpt-5-codex", "gpt-5", "o4-mini"):
            config = _make_config(model=model)
            inv = adapter.build_invocation(config, tmp_path, "x")
            idx = inv.argv.index("--model")
            assert inv.argv[idx + 1] == model

    def test_env_inherits_parent_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYNCADE_CODEX_TEST_SENTINEL", "yes")
        adapter = CodexAdapter()
        inv = adapter.build_invocation(_make_config(), tmp_path, "x")
        assert inv.env.get("SYNCADE_CODEX_TEST_SENTINEL") == "yes"
        # PATH must also be present so subprocess can find codex
        assert "PATH" in inv.env

    def test_invocation_env_resolves_worktree_src_not_main(self, tmp_path):
        # PR-23: the reviewer subprocess must import `syncade` from its OWN
        # worktree, not MAIN's editable-install .pth. Real child process —
        # the .pth wins on sys.path order, so an env-dict check is insufficient.
        worktree = make_worktree_src(tmp_path / "wt")
        adapter = CodexAdapter()
        inv = adapter.build_invocation(_make_config(), worktree, "x")
        resolved = resolve_syncade_in_child(inv.env, worktree)
        assert resolved.startswith(str((worktree / "src" / "syncade").resolve()))

    def test_invocation_is_immutable(self, tmp_path):
        adapter = CodexAdapter()
        inv = adapter.build_invocation(_make_config(), tmp_path, "x")
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            inv.cwd = Path("/elsewhere")  # type: ignore[misc]

    def test_implements_reviewer_adapter_protocol(self):
        adapter = CodexAdapter()
        assert isinstance(adapter, ReviewerAdapter)
        assert adapter.name == "openai"

    def test_wrong_provider_is_refused(self, tmp_path):
        adapter = CodexAdapter()
        config = _make_config(provider="anthropic")
        with pytest.raises(ValueError) as exc_info:
            adapter.build_invocation(config, tmp_path, "x")
        msg = str(exc_info.value)
        assert "CodexAdapter" in msg
        assert "openai" in msg
        assert "anthropic" in msg

    def test_permissions_safe_is_refused(self, tmp_path):
        adapter = CodexAdapter()
        # ReviewerPermissions rejects 'safe' at config-load; model_copy bypasses validation so we
        # can still exercise the adapter's belt-and-braces guard against a schema-bypassing caller.
        config = _make_config().model_copy(update={"permissions": "safe"})
        with pytest.raises(ValueError) as exc_info:
            adapter.build_invocation(config, tmp_path, "x")
        msg = str(exc_info.value)
        assert "safe" in msg
        assert "trusted-execute" in msg or "yolo" in msg

    def test_permissions_trusted_is_refused_when_schema_bypassed(self, tmp_path):
        adapter = CodexAdapter()
        config = _make_config(permissions="yolo").model_copy(update={"permissions": "trusted"})
        with pytest.raises(ValueError) as exc_info:
            adapter.build_invocation(config, tmp_path, "x")
        assert "trusted" in str(exc_info.value)


# ---------------------------------------------------------------------------
# parse_output — happy and unhappy paths against canned JSONL streams
# ---------------------------------------------------------------------------


class TestParseOutput:
    def _result(
        self,
        stdout: str,
        *,
        returncode: int = 0,
        stderr: str = "",
        duration: float = 0.5,
    ) -> SubprocessResult:
        return SubprocessResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
        )

    def test_happy_path_bare_json_in_agent_message(self):
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(
            agent_messages=[
                '{"verdict": "SHIP", "findings": [], '
                '"summary": "verified the trivial diff", '
                '"priority_order": [], "coverage_gaps": [], '
                '"dismissed_concerns": []}'
            ]
        )
        out = adapter.parse_output(self._result(stdout))
        assert isinstance(out, ReviewerOutput)
        assert out.verdict == "SHIP"
        assert out.findings == []

    def test_happy_path_markdown_fenced_json_in_agent_message(self):
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(
            agent_messages=[
                '```json\n{"verdict": "NO-SHIP", "findings": [], '
                '"summary": "saw the diff but disagreed", '
                '"priority_order": [], "coverage_gaps": [], '
                '"dismissed_concerns": []}\n```'
            ]
        )
        out = adapter.parse_output(self._result(stdout))
        assert out.verdict == "NO-SHIP"

    def test_multiple_agent_messages_takes_the_last(self):
        """Multi-turn flow safety: if the stream has multiple
        agent_message events, the adapter must take the LAST one (the
        verdict), not the first."""
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(
            agent_messages=[
                "Some intermediate prose; the model thinking out loud.",
                '{"verdict": "SHIP", "findings": [], '
                '"summary": "verified the trivial diff", '
                '"priority_order": [], "coverage_gaps": [], '
                '"dismissed_concerns": []}',
            ]
        )
        out = adapter.parse_output(self._result(stdout))
        assert out.verdict == "SHIP"

    def test_error_event_raises_invocation_error(self):
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(
            error_messages=["Model invocation failed: invalid_request_error"],
            turn_failed_message=(
                '{"type":"error","status":400,'
                '"error":{"type":"invalid_request_error",'
                '"message":"The \'no-such-model\' model is not supported"}}'
            ),
        )
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.parse_output(self._result(stdout, returncode=1))
        assert exc_info.value.returncode == 1
        # The turn.failed.error.message is preferred over bare errors
        msg = str(exc_info.value)
        assert "not supported" in msg or "invalid_request_error" in msg

    def test_auth_failure_pattern_mentions_codex_login(self):
        """The 401-Unauthorized / Missing-bearer pattern is the
        live-verified auth-failure shape (see the CLI output format).
        The exception message must mention `codex login` so the user
        sees the exact remediation."""
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(
            error_messages=[
                "Reconnecting... 1/5 (unexpected status 401 Unauthorized: "
                "Missing bearer or basic authentication in header)",
                "Reconnecting... 2/5 (unexpected status 401 Unauthorized)",
            ],
            turn_failed_message="401 Unauthorized: Missing bearer or basic authentication",
        )
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.parse_output(self._result(stdout, returncode=1))
        msg = str(exc_info.value)
        assert "codex login" in msg
        # api_error_status is the 401 we detected
        assert exc_info.value.api_error_status == 401

    def test_bare_401_failure_is_not_auth_without_auth_signature(self):
        adapter = CodexAdapter()
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.parse_output(
                self._result(
                    "",
                    returncode=1,
                    stderr="worker crashed while processing fixture case 401",
                )
            )
        msg = str(exc_info.value)
        assert "codex failed" in msg
        assert "codex login" not in msg
        assert exc_info.value.api_error_status is None

    def test_nonzero_rc_no_failure_events_uses_stderr(self):
        """CLI-level failure shape: unknown flag, missing prompt.
        rc != 0, no JSONL events on stdout, message on stderr.
        Adapter surfaces stderr in the exception message."""
        adapter = CodexAdapter()
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.parse_output(
                self._result(
                    "",
                    returncode=2,
                    stderr="error: unexpected argument '--blah'",
                )
            )
        assert exc_info.value.returncode == 2
        assert "unexpected argument" in str(exc_info.value)
        assert exc_info.value.api_error_status is None

    def test_no_agent_message_on_success_raises_output_error(self):
        """codex succeeded (rc=0, no error events) but never emitted an
        agent_message — defensive; not observed in practice. Surfaces
        as ReviewerOutputError (exit 70 territory)."""
        adapter = CodexAdapter()
        # JSONL with only thread.started + turn.started + turn.completed
        stdout = _jsonl_envelope(agent_messages=[])
        with pytest.raises(ReviewerOutputError) as exc_info:
            adapter.parse_output(self._result(stdout))
        assert "agent_message" in str(exc_info.value)

    def test_unparseable_inner_text_raises_output_error(self):
        """Stream parses fine, an agent_message exists, but its text
        isn't JSON the findings parser can handle. ReviewerOutputError
        bubbles up via parse_reviewer_output (already tested in
        test_findings.py; sanity-confirm here)."""
        adapter = CodexAdapter()
        stdout = _jsonl_envelope(agent_messages=["just some prose, no JSON anywhere"])
        with pytest.raises(ReviewerOutputError):
            adapter.parse_output(self._result(stdout))

    def test_garbage_in_stdout_is_silently_skipped(self):
        """Defensive: a non-JSON line in the middle of the JSONL stream
        shouldn't break parsing. Real codex --json doesn't do this,
        but a future CLI change shouldn't cause adapter failures."""
        adapter = CodexAdapter()
        body = _jsonl_envelope(
            agent_messages=[
                '{"verdict": "SHIP", "findings": [], '
                '"summary": "verified the trivial diff", '
                '"priority_order": [], "coverage_gaps": [], '
                '"dismissed_concerns": []}'
            ]
        )
        # Inject garbage between thread.started and the rest
        lines = body.splitlines()
        lines.insert(1, "this is not json")
        lines.insert(2, "")  # blank line — also tolerated
        stdout = "\n".join(lines) + "\n"
        out = adapter.parse_output(self._result(stdout))
        assert out.verdict == "SHIP"


def test_zero_config_default_reviewer_never_prompts(tmp_path):
    """The shipped default reviewer must run fully unattended.

    The default is ``trusted-execute`` (sandbox ON, scoped to the worktree)
    rather than ``yolo`` (sandbox off). That is only acceptable if it still
    never asks a human anything: a reviewer that prompts hangs the headless
    subprocess until the loop's timeout kills it. Assert the real argv the
    default config produces — ``approval_policy=never``, and none of codex's
    approval-prompting flags.
    """
    from syncade.adapters.openai import CodexAdapter
    from syncade.config import SyncadeConfig

    for reviewer in SyncadeConfig().reviewers:
        assert reviewer.permissions == "trusted-execute"
        argv = CodexAdapter().build_invocation(reviewer, tmp_path, "x").argv
        assert "approval_policy=never" in argv
        assert "-a" not in argv
        assert "--ask-for-approval" not in argv
        # sandbox stays ON — that is the point of preferring it over yolo
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv
        assert argv[argv.index("-s") + 1] == "workspace-write"
