"""End-to-end smoke tests for :class:`AnthropicAdapter` against the
real ``claude`` CLI.

These tests are the integration check that catches "the adapter
compiles and unit-tests but doesn't actually work" — exactly the
failure mode the rest of the test surface can't catch.

Gated behind ``pytest -m smoke``. The default ``pytest`` run
(configured in ``[tool.pytest.ini_options]`` with
``addopts = "-m 'not smoke'"``) explicitly deselects them so CI and
pre-commit runs stay hermetic.
"""

from __future__ import annotations

import shutil

import pytest

from syncade.adapters.anthropic import AnthropicAdapter
from syncade.config import ReviewerConfig
from syncade.findings import ReviewerOutput, get_findings_schema_string
from syncade.process import run_subprocess
from syncade.prompts import load_reviewer_template_for_provider, render_reviewer_prompt

_HAIKU = "haiku"
"""The cheapest model from the discovery doc — adequate for smoke
round-trips; cost should stay under a cent per invocation."""


def _skip_if_no_claude() -> None:
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not on PATH — install it to run smoke tests")


@pytest.mark.smoke
def test_minimal_round_trip_against_real_claude(tmp_path):
    """Tightest smoke check: hand-rolled "return this exact JSON" prompt.

    This verifies the wire is connected — build_invocation produces a
    valid argv, run_subprocess executes claude successfully, and
    parse_output extracts a ReviewerOutput from the envelope. The
    prompt is deterministic enough that even a flaky model run should
    produce the requested verdict.

    Skips cleanly if ``claude`` is not on PATH. Does NOT check auth:
    if auth is missing, the live ``claude 2.1.137`` returns rc=1
    with ``is_error: true`` and a "Please run /login" message in the
    envelope's ``.result`` (see the CLI-format notes). The
    adapter surfaces that as :class:`ReviewerInvocationError` and the
    test fails loudly with that actionable message rather than
    asserting silently.
    """
    _skip_if_no_claude()

    config = ReviewerConfig(
        name="smoke-claude-minimal",
        provider="anthropic",
        model=_HAIKU,
        thinking="low",
        permissions="yolo",
    )
    adapter = AnthropicAdapter()

    # PR-6: ReviewerOutput requires summary / priority_order /
    # coverage_gaps / dismissed_concerns. The hand-rolled prompt has
    # to spell out the full minimal-valid shape; otherwise the parser's
    # ReviewerOutput.model_validate discriminator skips the verdict
    # block and the smoke fails with exit-70-style ReviewerOutputError.
    prompt = (
        "Respond with only valid JSON matching this exact schema and "
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
def test_full_template_round_trip_against_real_claude(tmp_path):
    """Rigorous smoke check: render the actual production reviewer
    template against a synthetic PR doc + tiny diff, then assert that
    SOMETHING well-formed comes back.

    This catches the "the adapter compiles and the wire is connected,
    but the model refuses to comply with the real reviewer prompt"
    failure mode — which the minimal-prompt smoke test can't catch.
    QA against real claude saw the model ask clarifying questions
    instead of returning findings JSON when given a similar prompt
    shape; if that happens systematically here, the smoke test will
    fail loudly with the actual model output as the failure context.

    Assertion is on shape only — ``isinstance(output, ReviewerOutput)``
    — not on a specific verdict. The model may decide "trivial change,
    SHIP" or "no real spec to compare against, NO-SHIP" and either
    answer is acceptable for a smoke test.
    """
    _skip_if_no_claude()

    # Synthetic PR doc the rendered template will reference. Keep it
    # tiny so haiku doesn't burn budget reading it.
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

    template = load_reviewer_template_for_provider(tmp_path, "anthropic")
    prompt = render_reviewer_prompt(
        template,
        pr_doc_path=str(pr_doc),
        diff=diff,
        master_plan_path=None,
        json_schema=schema,
    )

    config = ReviewerConfig(
        name="smoke-claude-full-template",
        provider="anthropic",
        model=_HAIKU,
        thinking="low",
        permissions="yolo",
    )
    adapter = AnthropicAdapter()
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
    # No verdict assertion — the model decides whether the synthetic
    # diff satisfies the synthetic spec. Both SHIP and NO-SHIP are
    # acceptable smoke-test outcomes. We just need the round-trip
    # to produce a well-formed ReviewerOutput.
    assert output.verdict in ("SHIP", "NO-SHIP")

    # PR-6: the four narrative-surface fields are required on
    # ReviewerOutput. Validation already guarantees `summary`'s
    # min_length=1 and the priority_order permutation rule, so this
    # test catches the OTHER failure mode: the template asked for
    # them but the model produced a one-character `summary` or
    # otherwise low-quality content. The thresholds are loose on
    # purpose — this is a smoke test, not a content-quality grader.
    assert len(output.summary) >= 50, (
        f"summary too short to be a real verification narrative: {output.summary!r}"
    )
    # priority_order shape contract: complete permutation of indices.
    assert sorted(output.priority_order) == list(range(len(output.findings))), (
        f"priority_order {output.priority_order!r} is not a complete "
        f"permutation of range({len(output.findings)})"
    )
    # coverage_gaps and dismissed_concerns are required lists; either
    # may be empty, but the field has to be present (validator
    # enforces this — the model_validate would have failed otherwise).
    assert isinstance(output.coverage_gaps, list)
    assert isinstance(output.dismissed_concerns, list)
