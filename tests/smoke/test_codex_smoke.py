"""End-to-end smoke tests for :class:`CodexAdapter` against the real
``codex`` CLI.

These tests are the integration check that catches "the adapter
compiles and unit-tests but doesn't actually work" — exactly the
failure mode the rest of the test surface can't catch.

Gated behind ``pytest -m smoke``. The default ``pytest`` run
(configured in ``[tool.pytest.ini_options]`` with
``addopts = "-m 'not smoke'"``) explicitly deselects them so CI and
pre-commit runs stay hermetic.

Coverage:
1. ``test_minimal_round_trip_against_real_codex`` — hand-rolled
   JSON prompt, yolo permissions.
2. ``test_full_template_round_trip_against_real_codex`` — production
   reviewer template, yolo permissions.
3. ``test_default_config_round_trip_against_real_codex`` — exercises
   the model the shipped ``SyncadeConfig()`` defaults Codex to, so
   the smoke can't pass while the actual production default is
   broken (the gpt-5-codex / gpt-5.5 drift caught by review fix 3).
4. ``test_trusted_execute_permissions_round_trip_against_real_codex`` —
   exercises the ``permissions="trusted-execute"`` argv shape against real
   codex, so a regression in the ``-c approval_policy=never``
   mapping (review fix 4) gets caught by smoke.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from syncade.adapters.openai import CodexAdapter
from syncade.config import ReviewerConfig, SyncadeConfig
from syncade.findings import ReviewerOutput, get_findings_schema_string
from syncade.process import run_subprocess
from syncade.prompts import load_reviewer_template_for_provider, render_reviewer_prompt

# The cheapest model the user's codex install is known to authorize
# against, per the discovery doc / live verification on
# ``codex-cli 0.130.0``. This must match the Codex reviewer's default
# model in :func:`syncade.config._default_reviewers` — see
# :func:`test_smoke_model_matches_default_config` below.
_CHEAP_MODEL = "gpt-5.5"


def _skip_if_no_codex() -> None:
    if shutil.which("codex") is None:
        pytest.skip("codex CLI not on PATH — install codex-cli to run smoke tests")


def _git_init_worktree(path) -> None:
    """Make ``path`` a real git worktree.

    Trusted-mode codex runs (``-s workspace-write`` without the
    yolo combined bypass) refuse to start unless cwd is a git
    working tree, failing with ``Not inside a trusted directory and
    --skip-git-repo-check was not specified.`` In production
    ``WorktreeManager`` (PR-2) produces real git worktrees, so the
    adapter never needs ``--skip-git-repo-check``. Smoke tests that
    use ``tmp_path`` directly have to set up a git repo themselves.

    Yolo's ``--dangerously-bypass-approvals-and-sandbox`` implicitly
    skips the git check, so the yolo smokes work fine against bare
    ``tmp_path``. Every smoke that runs a NON-yolo reviewer needs this —
    which now includes the default-config smoke, because the shipped
    reviewer default is ``trusted-execute``, not ``yolo``.
    """
    subprocess.run(
        ["git", "init", "-q"],
        cwd=path,
        check=True,
    )


def test_smoke_model_matches_default_config():
    """The smoke tests run against ``_CHEAP_MODEL``; that constant
    must match the Codex reviewer's default model in
    ``SyncadeConfig()``.

    If somebody bumps one without the other, this test fails and
    points at the divergence. Without this guard, the smoke could
    keep passing against a model the shipped default doesn't use,
    which is exactly the escape-hatch failure mode review fix 3
    corrected.
    """
    cfg = SyncadeConfig()
    codex_rev = next(r for r in cfg.reviewers if r.provider == "openai")
    assert codex_rev.model == _CHEAP_MODEL, (
        f"Codex smoke model {_CHEAP_MODEL!r} does not match "
        f"SyncadeConfig() default {codex_rev.model!r}. Either update "
        f"_CHEAP_MODEL or update syncade.config._default_reviewers."
    )


@pytest.mark.smoke
def test_minimal_round_trip_against_real_codex(tmp_path):
    """Tightest smoke check: hand-rolled "return this exact JSON"
    prompt.

    Verifies the wire is connected — build_invocation produces a
    valid argv, run_subprocess executes codex successfully, the
    --json event stream is parsed, and the LAST agent_message's text
    is handed to parse_reviewer_output. The prompt is deterministic
    enough that even a flaky model run should produce the requested
    verdict.

    Skips cleanly if ``codex`` is not on PATH. Does NOT check auth:
    codex's missing-auth shape (rc=1, JSONL stream with multiple
    ``error`` events whose messages contain ``401 Unauthorized`` /
    ``Missing bearer or basic authentication``, terminated by a
    ``turn.failed`` event — per the codex CLI output notes) raises
    ``ReviewerInvocationError`` loudly with the actual codex output
    rather than failing the assertion silently. The dispatcher's
    pre-flight ``check_auth`` phase would normally catch this
    earlier in production via ``codex login status``.
    """
    _skip_if_no_codex()

    config = ReviewerConfig(
        name="smoke-codex-minimal",
        provider="openai",
        model=_CHEAP_MODEL,
        thinking="low",  # cheap; this is a smoke test, not a real review
        permissions="yolo",
    )
    adapter = CodexAdapter()

    # PR-6: ReviewerOutput requires summary / priority_order /
    # coverage_gaps / dismissed_concerns. The hand-rolled prompt has
    # to spell out the full minimal-valid shape; otherwise the parser's
    # ReviewerOutput.model_validate discriminator skips the verdict
    # block and the smoke fails with exit-70-style ReviewerOutputError.
    prompt = (
        "Respond with ONLY valid JSON matching this exact schema and "
        "nothing else. No prose, no markdown fences, no preamble.\n\n"
        "For this smoke test, return: "
        '{"verdict": "SHIP", "findings": [], '
        '"summary": "smoke test minimal SHIP", '
        '"priority_order": [], "coverage_gaps": [], '
        '"dismissed_concerns": []}'
    )

    invocation = adapter.build_invocation(config, tmp_path, prompt)
    result = run_subprocess(
        invocation.argv,
        cwd=invocation.cwd,
        env=invocation.env,
        timeout=120,
        input_text=invocation.stdin_text,
    )
    output = adapter.parse_output(result)

    assert isinstance(output, ReviewerOutput)
    assert output.verdict == "SHIP"
    # findings list may be empty or non-empty — we don't assert on it,
    # only that parse succeeded.


@pytest.mark.smoke
def test_full_template_round_trip_against_real_codex(tmp_path):
    """Rigorous smoke check: render the actual production reviewer
    template against a synthetic PR doc + tiny diff, then assert that
    SOMETHING well-formed comes back.

    This catches the "the adapter compiles and the wire is connected,
    but the model refuses to comply with the real reviewer prompt"
    failure mode — same shape as the Anthropic smoke's
    test_full_template_round_trip_against_real_claude.

    Assertion is on shape only — ``isinstance(output, ReviewerOutput)``
    — not on a specific verdict. The model may decide "trivial change,
    SHIP" or "no real spec to compare against, NO-SHIP" and either
    answer is acceptable for a smoke test.
    """
    _skip_if_no_codex()

    pr_doc = tmp_path / "pr-doc.md"
    pr_doc.write_text(
        "# Synthetic PR\n\n"
        "**Goal:** Add a comment to README.md noting the project is in "
        "active development.\n\n"
        "**Acceptance:** README.md ends with a `# WIP` marker line.\n"
    )

    diff = (
        "diff --git a/README.md b/README.md\n"
        "index 0000001..0000002 100644\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1,2 @@\n"
        " syncade\n"
        "+# WIP\n"
    )

    # Schema comes from syncade.findings — single source of truth so
    # this smoke test stays in sync with what the orchestrator renders.
    schema = get_findings_schema_string()

    template = load_reviewer_template_for_provider(tmp_path, "openai")
    prompt = render_reviewer_prompt(
        template,
        pr_doc_path=str(pr_doc),
        diff=diff,
        master_plan_path=None,
        json_schema=schema,
    )

    config = ReviewerConfig(
        name="smoke-codex-full-template",
        provider="openai",
        model=_CHEAP_MODEL,
        thinking="low",
        permissions="yolo",
    )
    adapter = CodexAdapter()
    invocation = adapter.build_invocation(config, tmp_path, prompt)
    result = run_subprocess(
        invocation.argv,
        cwd=invocation.cwd,
        env=invocation.env,
        timeout=180,  # full template is heavier; give it more headroom
        input_text=invocation.stdin_text,
    )
    output = adapter.parse_output(result)

    assert isinstance(output, ReviewerOutput)
    assert output.verdict in ("SHIP", "NO-SHIP")

    # PR-6: same narrative-surface assertions as the Anthropic smoke.
    # Both providers must populate the four required fields when given
    # the production reviewer template; this catches the "template
    # asked, model ignored" failure mode for codex specifically.
    assert len(output.summary) >= 50, (
        f"codex summary too short to be a real verification narrative: {output.summary!r}"
    )
    assert sorted(output.priority_order) == list(range(len(output.findings))), (
        f"codex priority_order {output.priority_order!r} is not a complete "
        f"permutation of range({len(output.findings)})"
    )
    assert isinstance(output.coverage_gaps, list)
    assert isinstance(output.dismissed_concerns, list)


@pytest.mark.smoke
def test_default_config_round_trip_against_real_codex(tmp_path):
    """The Codex reviewer that ships in ``SyncadeConfig()`` defaults
    must actually round-trip against real codex.

    Catches the ``gpt-5-codex`` / ``gpt-5.5`` regression class: the
    pre-review-fix-3 default rejected with HTTP 400 on ChatGPT auth,
    but the smokes ran against a different model and didn't notice.
    This test pulls the reviewer config straight from
    ``SyncadeConfig()`` — no model override — so a broken default
    breaks the smoke.
    """
    _skip_if_no_codex()
    # The shipped reviewer default is trusted-execute, so codex enforces its
    # git-repo trust check. Production hands reviewers real git worktrees;
    # this test has to build one itself or codex refuses to start.
    _git_init_worktree(tmp_path)

    cfg = SyncadeConfig()
    codex_rev = next(r for r in cfg.reviewers if r.provider == "openai")

    adapter = CodexAdapter()
    # PR-6: hand-rolled prompt must include the four narrative-surface
    # fields or the parser's model_validate discriminator rejects the
    # verdict block and the smoke fails with ReviewerOutputError.
    prompt = (
        "Respond with ONLY valid JSON and nothing else. Return exactly: "
        '{"verdict": "SHIP", "findings": [], '
        '"summary": "smoke test default-config SHIP", '
        '"priority_order": [], "coverage_gaps": [], '
        '"dismissed_concerns": []}'
    )
    invocation = adapter.build_invocation(codex_rev, tmp_path, prompt)
    result = run_subprocess(
        invocation.argv,
        cwd=invocation.cwd,
        env=invocation.env,
        timeout=120,
        input_text=invocation.stdin_text,
    )
    output = adapter.parse_output(result)
    assert isinstance(output, ReviewerOutput)
    assert output.verdict == "SHIP"


@pytest.mark.smoke
def test_trusted_execute_permissions_round_trip_against_real_codex(tmp_path):
    """``permissions="trusted-execute"`` must actually work end-to-end.

    Catches the ``-a never`` regression class: pre-review-fix-4
    adapter emitted ``-s workspace-write -a never``, which
    ``codex exec`` rejects because ``-a/--ask-for-approval`` is a
    top-level-only flag. This smoke exercises the corrected
    ``-s workspace-write -c approval_policy=never`` mapping against
    the real CLI.

    Trusted mode does NOT get yolo's implicit
    ``--skip-git-repo-check``, so we git-init the worktree the way
    ``WorktreeManager`` would in production.
    """
    _skip_if_no_codex()
    _git_init_worktree(tmp_path)

    config = ReviewerConfig(
        name="smoke-codex-trusted-execute",
        provider="openai",
        model=_CHEAP_MODEL,
        thinking="low",
        permissions="trusted-execute",
    )
    adapter = CodexAdapter()
    # PR-6: hand-rolled prompt must include the four narrative-surface
    # fields or the parser rejects the verdict block.
    prompt = (
        "Respond with ONLY valid JSON and nothing else. Return exactly: "
        '{"verdict": "SHIP", "findings": [], '
        '"summary": "smoke test trusted-execute-mode SHIP", '
        '"priority_order": [], "coverage_gaps": [], '
        '"dismissed_concerns": []}'
    )
    invocation = adapter.build_invocation(config, tmp_path, prompt)
    # Sanity-check the corrected argv shape before sending it to codex
    assert "approval_policy=never" in invocation.argv
    assert "-a" not in invocation.argv
    result = run_subprocess(
        invocation.argv,
        cwd=invocation.cwd,
        env=invocation.env,
        timeout=120,
        input_text=invocation.stdin_text,
    )
    output = adapter.parse_output(result)
    assert isinstance(output, ReviewerOutput)
    assert output.verdict == "SHIP"
