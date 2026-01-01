"""Tests for :mod:`syncade.prompts` — producer prompt rendering and the
producer template's prior-round-attempt section.
"""

from __future__ import annotations

from pathlib import Path

import pytest


class TestRenderProducerPrompt:
    """``render_producer_prompt`` is a strict format_map against
    six placeholders. Same surface-typos-loudly contract as the
    reviewer + synthesizer renderers."""

    def test_basic_substitution(self):
        from syncade.prompts import render_producer_prompt

        template = (
            "PR: {pr_doc_path}\n"
            "Findings: {findings_md_path}\n"
            "Test: {test_run_stdout_path}\n"
            "Worktree: {worktree_path}\n"
            "Round: {round_number} of {max_rounds}\n"
        )
        rendered = render_producer_prompt(
            template,
            pr_doc_path="/x/pr.md",
            findings_md_path="/y/findings.md",
            test_run_stdout_path="(no test failure this round)",
            worktree_path="/z/wt",
            round_number=1,
            max_rounds=3,
        )
        assert "PR: /x/pr.md" in rendered
        assert "Findings: /y/findings.md" in rendered
        assert "Test: (no test failure this round)" in rendered
        assert "Worktree: /z/wt" in rendered
        assert "Round: 1 of 3" in rendered

    def test_operator_decision_substituted(self):
        """PR-22: the operator's recorded decision (from a resumed
        escalation) substitutes into {operator_decision}."""
        from syncade.prompts import render_producer_prompt

        rendered = render_producer_prompt(
            "decision: {operator_decision}",
            pr_doc_path="x",
            findings_md_path="y",
            test_run_stdout_path=None,
            worktree_path="z",
            round_number=1,
            max_rounds=3,
            operator_decision="Chose option A: omit empty checks.",
        )
        assert "decision: Chose option A: omit empty checks." in rendered

    def test_operator_decision_defaults_to_sentinel(self):
        from syncade.prompts import _NO_OPERATOR_DECISION_SENTINEL, render_producer_prompt

        rendered = render_producer_prompt(
            "decision: {operator_decision}",
            pr_doc_path="x",
            findings_md_path="y",
            test_run_stdout_path=None,
            worktree_path="z",
            round_number=0,
            max_rounds=3,
        )
        assert f"decision: {_NO_OPERATOR_DECISION_SENTINEL}" in rendered

    def test_no_test_failure_sentinel_substitutes_literally(self):
        """The renderer accepts the explicit
        ``"(no test failure this round)"`` sentinel as a regular
        string — same path callers used pre-R2.T4 by pre-
        converting None themselves."""
        from syncade.prompts import render_producer_prompt

        template = "tests: {test_run_stdout_path}"
        rendered = render_producer_prompt(
            template,
            pr_doc_path="x",
            findings_md_path="y",
            test_run_stdout_path="(no test failure this round)",
            worktree_path="z",
            round_number=0,
            max_rounds=3,
        )
        assert "tests: (no test failure this round)" in rendered

    def test_none_test_run_stdout_path_substitutes_sentinel(self):
        """PR-8 R2.T4: the renderer accepts ``None`` for
        ``test_run_stdout_path`` and substitutes the
        ``(no test failure this round)`` sentinel itself.

        Pre-R2.T4 the orchestrator handled the None → sentinel
        mapping before calling the renderer; the renderer
        signature required str. The brief's task 4 acceptance
        explicitly says: "``{test_run_stdout_path}`` substitution
        works with None → renders the literal '(no test failure
        this round)' string". Moved the None→sentinel logic
        into the renderer so the public API contract handles it
        directly."""
        from syncade.prompts import render_producer_prompt

        template = "tests: {test_run_stdout_path}"
        rendered = render_producer_prompt(
            template,
            pr_doc_path="x",
            findings_md_path="y",
            test_run_stdout_path=None,
            worktree_path="z",
            round_number=0,
            max_rounds=3,
        )
        assert "tests: (no test failure this round)" in rendered

    def test_none_distinguishable_from_explicit_sentinel(self):
        """Passing None and passing the literal sentinel both
        produce the same rendered output — the renderer's
        None-handling is purely substitution, not behavioral
        change."""
        from syncade.prompts import render_producer_prompt

        template = "{test_run_stdout_path}"
        from_none = render_producer_prompt(
            template,
            pr_doc_path="x",
            findings_md_path="y",
            test_run_stdout_path=None,
            worktree_path="z",
            round_number=0,
            max_rounds=3,
        )
        from_sentinel = render_producer_prompt(
            template,
            pr_doc_path="x",
            findings_md_path="y",
            test_run_stdout_path="(no test failure this round)",
            worktree_path="z",
            round_number=0,
            max_rounds=3,
        )
        assert from_none == from_sentinel

    def test_unknown_placeholder_raises_key_error(self):
        """A typo in a per-repo override surfaces loudly rather
        than silently rendering an empty string. Same
        format_map(strict-mapping) discipline as the reviewer and
        synthesizer renderers."""
        from syncade.prompts import render_producer_prompt

        template = "{frobnicate} is not a real placeholder"
        with pytest.raises(KeyError):
            render_producer_prompt(
                template,
                pr_doc_path="x",
                findings_md_path="y",
                test_run_stdout_path="z",
                worktree_path="w",
                round_number=0,
                max_rounds=3,
            )

    def test_packaged_template_renders_cleanly_end_to_end(self, tmp_path: Path):
        """Sanity: the bundled template + a typical caller's kwargs
        produces a usable string with no unsubstituted
        placeholders. Mirrors the reviewer-template equivalence."""
        from syncade.prompts import load_producer_template, render_producer_prompt

        template = load_producer_template(tmp_path)
        rendered = render_producer_prompt(
            template,
            pr_doc_path="/abs/pr-8.md",
            findings_md_path="/abs/.syncade/runs/r/round-0/findings.md",
            test_run_stdout_path="(no test failure this round)",
            worktree_path="/tmp/syncade/r/producer",
            round_number=1,
            max_rounds=3,
        )
        # No leftover unsubstituted {identifier} placeholders.
        import re

        leftover = re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", rendered)
        assert leftover is None, f"unsubstituted placeholder: {leftover.group()}"
        assert "/abs/pr-8.md" in rendered
        assert "/tmp/syncade/r/producer" in rendered
        assert "(no test failure this round)" in rendered
        # round/max_rounds rendered as integers
        assert "1" in rendered
        assert "3" in rendered

    def test_round_number_zero_substitutes(self):
        """round_number=0 must substitute as the literal "0", not be
        treated as falsy and elided. Belt-and-braces against a
        future renderer change that conflates None and 0."""
        from syncade.prompts import render_producer_prompt

        template = "round: {round_number}"
        rendered = render_producer_prompt(
            template,
            pr_doc_path="x",
            findings_md_path="y",
            test_run_stdout_path="z",
            worktree_path="w",
            round_number=0,
            max_rounds=3,
        )
        assert "round: 0" in rendered

    def test_path_with_braces_in_content_safe(self):
        """Paths with `{` / `}` in them (unusual but possible) should
        substitute literally — format_map treats values as opaque
        strings."""
        from syncade.prompts import render_producer_prompt

        template = "wt: {worktree_path}"
        weird_path = "/tmp/{role}/producer"
        rendered = render_producer_prompt(
            template,
            pr_doc_path="x",
            findings_md_path="y",
            test_run_stdout_path="z",
            worktree_path=weird_path,
            round_number=0,
            max_rounds=3,
        )
        assert weird_path in rendered


class TestProducerTemplatePriorRoundContext:
    """PR-14 Task 2: the packaged producer template carries the
    "Your prior round's attempt" section with the
    ``{prior_round_output}`` and ``{prior_round_commits}``
    placeholders. The renderer accepts the two new kwargs with
    documented default sentinels for round 0.

    The cross-round-context flow itself is wired in PR-14 Task 3 (in
    the orchestrator's producer phase); these tests pin the prompt-
    rendering surface only.
    """

    def test_producer_template_has_prior_round_attempt_section(self, tmp_path: Path):
        """The bundled producer template must contain the headline
        phrase ``Your prior round's attempt`` plus both PR-14
        placeholders. Without the placeholders, ``format_map`` would
        silently ignore the kwargs; without the headline, the producer
        wouldn't know what the substituted text represents."""
        from syncade.prompts import load_producer_template

        template = load_producer_template(tmp_path)
        assert "Your prior round's attempt" in template, (
            "PR-14 Task 2: producer.md must carry the headline phrase "
            "'Your prior round's attempt' so the producer knows what "
            "the substituted text represents."
        )
        assert "{prior_round_output}" in template, (
            "PR-14 Task 2: producer.md must declare the "
            "{prior_round_output} placeholder so the renderer's "
            "format_map substitution lands."
        )
        assert "{prior_round_commits}" in template, (
            "PR-14 Task 2: producer.md must declare the "
            "{prior_round_commits} placeholder so the renderer's "
            "format_map substitution lands."
        )

    def test_render_producer_prompt_substitutes_prior_round_kwargs(self):
        """Passing both PR-14 kwargs substitutes the supplied text into
        the rendered prompt verbatim. The prior-round response can be
        large (5-20k tokens); prior_round_commits is typically a short
        list of subjects. Both must pass through unchanged."""
        from syncade.prompts import render_producer_prompt

        template = "before\n{prior_round_output}\n---\ncommits:\n{prior_round_commits}\nafter"
        sample_prior = (
            "Round 0 producer narrative: addressed blocker #1 by adding "
            "null check at src/foo.py:42. Did not address nit #2 "
            "(stylistic) per the brief's smallest-blast-radius rule.\n"
        )
        sample_commits = "fix: add null check in compute_money_flow_snapshot\n"
        rendered = render_producer_prompt(
            template,
            pr_doc_path="pr.md",
            findings_md_path="findings.md",
            test_run_stdout_path=None,
            worktree_path="/tmp/wt",
            round_number=1,
            max_rounds=3,
            prior_round_output=sample_prior,
            prior_round_commits=sample_commits,
        )
        assert sample_prior in rendered, (
            "the prior round response text must appear in the rendered "
            "prompt verbatim — the renderer passes it through without "
            "truncation"
        )
        assert sample_commits in rendered, (
            "the prior round commit subjects must appear in the rendered "
            "prompt verbatim — the renderer passes them through without "
            "truncation"
        )

    def test_render_producer_prompt_default_sentinels_when_round_zero(self):
        """Round 0 callers omit both PR-14 kwargs; the renderer
        substitutes the documented sentinels:
        ``"(no prior round — this is round 0)"`` for prior_round_output,
        ``"(no prior commits)"`` for prior_round_commits. Two distinct
        sentinels so the prompt prose can explicitly name each rather
        than rendering a single ambiguous ``(none)``."""
        from syncade.prompts import render_producer_prompt

        template = "ro={prior_round_output}|rc={prior_round_commits}"
        rendered = render_producer_prompt(
            template,
            pr_doc_path="pr.md",
            findings_md_path="findings.md",
            test_run_stdout_path=None,
            worktree_path="/tmp/wt",
            round_number=0,
            max_rounds=3,
        )
        assert "ro=(no prior round — this is round 0)" in rendered, (
            "round-0 calls (no prior_round_output kwarg) must substitute the documented sentinel"
        )
        assert "rc=(no prior commits)" in rendered, (
            "round-0 calls (no prior_round_commits kwarg) must substitute the documented sentinel"
        )
