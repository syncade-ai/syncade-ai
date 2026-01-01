"""Tests for :mod:`syncade.prompts` — synthesizer prompt rendering and
producer template loading / packaged-content checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncade.prompts import (
    load_synthesizer_template,
    render_synthesizer_prompt,
)


class TestRenderSynthesizerPrompt:
    def test_basic_substitution(self):
        template = (
            "pr={pr_doc_path}\nplan={master_plan_path}\n"
            "outputs={reviewer_outputs_json}\nschema={json_schema}\n"
        )
        rendered = render_synthesizer_prompt(
            template,
            pr_doc_path="path/to/pr.md",
            reviewer_outputs_json='claude-reviewer:\n{"verdict": "SHIP"}',
            json_schema='{"consolidated_findings": []}',
        )
        assert "pr=path/to/pr.md" in rendered
        assert "outputs=claude-reviewer:" in rendered
        assert "schema=" in rendered

    def test_master_plan_path_none_becomes_marker(self):
        template = (
            "plan={master_plan_path}|pr={pr_doc_path}"
            "|outputs={reviewer_outputs_json}|schema={json_schema}"
        )
        rendered = render_synthesizer_prompt(
            template,
            pr_doc_path="x",
            reviewer_outputs_json="r",
            master_plan_path=None,
            json_schema="s",
        )
        assert "plan=(none)" in rendered

    def test_master_plan_path_supplied_substitutes(self):
        template = (
            "plan={master_plan_path}|pr={pr_doc_path}"
            "|outputs={reviewer_outputs_json}|schema={json_schema}"
        )
        rendered = render_synthesizer_prompt(
            template,
            pr_doc_path="x",
            reviewer_outputs_json="r",
            master_plan_path="docs/master-plan.md",
            json_schema="s",
        )
        assert "plan=docs/master-plan.md" in rendered
        assert "(none)" not in rendered

    def test_unknown_placeholder_raises_key_error(self):
        # PR-7: same strict-renderer contract as the reviewer.
        template = "{wat} is not a known placeholder"
        with pytest.raises(KeyError):
            render_synthesizer_prompt(
                template,
                pr_doc_path="x",
                reviewer_outputs_json="r",
                json_schema="s",
            )

    def test_diff_placeholder_is_rejected(self):
        # The synthesizer is the cold consolidator: it does not see
        # the diff. A template (or override) that tries to use
        # {diff} should fail loudly via KeyError rather than silently
        # producing an unsubstituted prompt.
        template = (
            "diff={diff}; pr={pr_doc_path}; outputs={reviewer_outputs_json}; schema={json_schema}"
        )
        with pytest.raises(KeyError):
            render_synthesizer_prompt(
                template,
                pr_doc_path="x",
                reviewer_outputs_json="r",
                json_schema="s",
            )

    def test_packaged_template_renders_cleanly_end_to_end(self, tmp_path: Path):
        from syncade.synthesis import get_synthesizer_schema_string

        template = load_synthesizer_template(tmp_path)
        rendered = render_synthesizer_prompt(
            template,
            pr_doc_path="path/to/pr.md",
            reviewer_outputs_json=(
                'claude-reviewer:\n{"verdict": "SHIP", "findings": []}\n\n'
                'codex-reviewer:\n{"verdict": "NO-SHIP", "findings": [...]}'
            ),
            master_plan_path=None,
            json_schema=get_synthesizer_schema_string(),
        )

        import re

        # No leftover placeholders of the form {identifier}. Embedded
        # JSON examples use {{ }} → render as { } and are expected.
        leftover = re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", rendered)
        assert leftover is None, f"unsubstituted placeholder: {leftover.group()}"
        # PR-7 contracts surface in the rendered prompt
        assert "path/to/pr.md" in rendered
        assert "claude-reviewer" in rendered
        assert "codex-reviewer" in rendered
        # Schema string fields propagate through
        for name in ("consolidated_findings", "synthesis_summary", "provenance"):
            assert name in rendered

    def test_rendered_prompt_uses_get_synthesizer_schema_string(self, tmp_path: Path):
        """The rendered prompt's {json_schema} placeholder is filled by
        the orchestrator from
        :func:`get_synthesizer_schema_string`. Pins the round-trip:
        schema updates propagate through the template into what the
        synthesizer sees."""
        from syncade.synthesis import get_synthesizer_schema_string

        template = load_synthesizer_template(tmp_path)
        rendered = render_synthesizer_prompt(
            template,
            pr_doc_path="path/to/pr.md",
            reviewer_outputs_json="(none)",
            master_plan_path=None,
            json_schema=get_synthesizer_schema_string(),
        )
        for name in (
            "consolidated_findings",
            "synthesis_summary",
            "provenance",
            "dismissal_rationale",
            "severity_change_rationale",
        ):
            assert name in rendered, (
                f"rendered prompt missing schema field {name!r} — the "
                "schema-string substitution path regressed"
            )

    def test_reviewer_outputs_with_braces_safe(self):
        """The reviewer outputs are JSON-shaped and contain `{` and
        `}` characters. format_map must treat the substituted value as
        a literal — those braces in the SUBSTITUTED content must not
        re-trigger format-string parsing."""
        template = "outputs:\n{reviewer_outputs_json}\nend"
        payload = '{"verdict": "SHIP", "findings": [{"file": "x.py"}]}'
        rendered = render_synthesizer_prompt(
            template,
            pr_doc_path="x",
            reviewer_outputs_json=payload,
            json_schema="s",
        )
        assert payload in rendered


class TestLoadProducerTemplate:
    """``load_producer_template(p)`` is parallel to the reviewer +
    synthesizer loaders — per-repo override at
    ``.syncade/templates/producer.md`` wins over the packaged
    default, with the same path-traversal protection."""

    def test_packaged_default_loads(self, tmp_path: Path):
        from syncade.prompts import load_producer_template

        template = load_producer_template(tmp_path)
        # All six PR-8 placeholders documented on
        # render_producer_prompt must appear in the bundled template
        # so the orchestrator's strict format_map substitution
        # produces a usable prompt.
        for placeholder in (
            "{pr_doc_path}",
            "{findings_md_path}",
            "{test_run_stdout_path}",
            "{worktree_path}",
            "{round_number}",
            "{max_rounds}",
        ):
            assert placeholder in template, (
                f"bundled producer template missing placeholder {placeholder!r}"
            )

    def test_per_repo_override_wins(self, tmp_path: Path):
        """When ``<repo>/.syncade/templates/producer.md`` exists, its
        contents are returned instead of the packaged default. Same
        precedence rule as the reviewer + synthesizer loaders."""
        from syncade.prompts import load_producer_template

        override_dir = tmp_path / ".syncade" / "templates"
        override_dir.mkdir(parents=True)
        custom = (
            "CUSTOM PRODUCER\npr={pr_doc_path}\n"
            "findings={findings_md_path}\n"
            "tests={test_run_stdout_path}\n"
            "wt={worktree_path}\n"
            "r={round_number}/{max_rounds}\n"
        )
        (override_dir / "producer.md").write_text(custom)

        loaded = load_producer_template(tmp_path)
        assert loaded == custom

    def test_missing_override_falls_back_silently(self, tmp_path: Path):
        from syncade.prompts import load_producer_template

        assert not (tmp_path / ".syncade").exists()
        template = load_producer_template(tmp_path)
        assert "{pr_doc_path}" in template

    def test_operator_decision_placeholder_present(self, tmp_path: Path):
        """PR-22: the bundled producer template carries the
        {operator_decision} placeholder so a resumed escalation round can
        feed the operator's decision to the producer."""
        from syncade.prompts import load_producer_template

        template = load_producer_template(tmp_path)
        assert "{operator_decision}" in template

    def test_escalation_says_commit_fixable_first(self, tmp_path: Path):
        """PR-22 QA finding 1: the escalation guidance tells the producer to
        fix + commit every fixable blocker FIRST and escalate ONLY in a round
        where nothing is left to commit (all remaining blockers are operator-
        decisions). That makes 'escalated' == 'no fixable progress this round',
        so the loop checkpoints for a decision (AC5) only when there is
        genuinely no fixable work left — enforced at the producer because the
        orchestrator can't tell fixable from decision findings. Committing keeps
        the loop going so fixes get blind-re-reviewed before it pauses."""
        from syncade.prompts import load_producer_template

        t = load_producer_template(tmp_path).lower()
        assert "fixable" in t
        assert "nothing left to commit" in t

    def test_escalation_section_present_and_synced(self, tmp_path: Path):
        """PR-22 T3: the bundled producer template carries the escalation
        guidance (an operator-decision channel distinct from a plain stall)
        AND the literal escalation sentinels — kept in sync with the
        producer_escalation parser so the producer's block is parseable."""
        from syncade.producer_escalation import ESCALATE_CLOSE, ESCALATE_OPEN
        from syncade.prompts import load_producer_template

        template = load_producer_template(tmp_path)
        # The section reframes "cannot fix" into the escalation channel.
        assert "escalat" in template.lower()
        assert "operator decision" in template.lower()
        # Evidence bar: a reproduction is required to escalate.
        assert "reproduc" in template.lower()
        # The sentinels MUST match the parser's constants verbatim, or the
        # producer's emitted block won't parse.
        assert ESCALATE_OPEN in template
        assert ESCALATE_CLOSE in template
        # PR-24: the escalation block format carries finding_indices (the
        # active-blocker indices the decision covers) — the field the
        # parser now requires and the terminator's coverage check reads.
        assert "finding_indices" in template

    def test_override_directory_without_file_falls_back(self, tmp_path: Path):
        from syncade.prompts import load_producer_template

        (tmp_path / ".syncade" / "templates").mkdir(parents=True)
        template = load_producer_template(tmp_path)
        assert "{pr_doc_path}" in template

    def test_load_template_producer_via_canonical_entry(self, tmp_path: Path):
        """``load_template(p, "producer.md")`` and the wrapper return
        the same content. Parallel to the reviewer / synthesizer
        equivalence tests."""
        from syncade.prompts import load_producer_template, load_template

        from_param = load_template(tmp_path, "producer.md")
        from_wrapper = load_producer_template(tmp_path)
        assert from_param == from_wrapper

    @pytest.mark.parametrize(
        "bad_name",
        [
            "",
            ".",
            "..",
            "../producer.md",
            "subdir/producer.md",
            "subdir\\producer.md",
            "/etc/passwd",
        ],
    )
    def test_unsafe_template_name_rejected(self, tmp_path: Path, bad_name: str):
        """``load_template`` rejects unsafe basenames regardless of
        which template is being loaded. The path-traversal guard
        is shared via :func:`load_template`."""
        from syncade.prompts import load_template

        with pytest.raises(ValueError):
            load_template(tmp_path, bad_name)


class TestPackagedProducerTemplate:
    """Content-level checks on the bundled template. The producer's
    role / output-discipline / what-NOT-to-do sections are the
    behaviorally-load-bearing parts; structural checks against
    distinctive phrases pin them so a future template edit can't
    silently drop the "must commit" instruction or the
    code-focused-commit-message guidance."""

    def test_template_emphasizes_must_commit_for_stall_detection(self, tmp_path: Path):
        """The orchestrator's stall detection is SHA-based — file
        edits without a commit register as a stall. The template
        MUST instruct the producer to commit; otherwise the loop
        terminates with producer_stalled when the producer made
        edits but forgot to commit."""
        from syncade.prompts import load_producer_template

        template = load_producer_template(tmp_path)
        lower = template.lower()
        assert "must commit" in lower or "you must commit" in lower
        # The stall-detection mechanism must also be named so a
        # producer reading the prompt understands WHY committing
        # is required.
        assert "head move" in lower or "head" in lower

    def test_template_forbids_commit_messages_referencing_syncade(self, tmp_path: Path):
        """The producer's commits land in operator git history.
        The brief is explicit: commit subjects must be reviewable
        as standalone code commits, NOT as "address round 0
        finding #3" / "fix issues flagged by claude-reviewer".
        Pin this disposition in the template."""
        from syncade.prompts import load_producer_template

        template = load_producer_template(tmp_path)
        lower = template.lower()
        # The template names the anti-pattern explicitly.
        assert "syncade" in lower
        assert "code-focused" in lower or "standalone" in lower

    def test_template_says_one_commit_per_logical_change(self, tmp_path: Path):
        from syncade.prompts import load_producer_template

        template = load_producer_template(tmp_path)
        lower = template.lower()
        assert "one commit" in lower or "ONE commit" in template

    def test_template_forbids_editing_pr_spec(self, tmp_path: Path):
        """The producer must implement against the PR spec, not edit
        it. The brief is explicit; the template should call this
        out."""
        from syncade.prompts import load_producer_template

        template = load_producer_template(tmp_path)
        lower = template.lower()
        assert "pr spec" in lower or "{pr_doc_path}" in template
        # The "do not change" disposition appears for the spec
        assert "do not change" in lower or "not edit" in lower

    def test_template_describes_stall_on_cannot_fix(self, tmp_path: Path):
        """When the producer genuinely can't fix a finding, the
        intended behavior is: emit narrative, do NOT commit, let
        the orchestrator's stall detection terminate the loop with
        exit 30 + producer_stalled. The template must describe this
        escape hatch."""
        from syncade.prompts import load_producer_template

        template = load_producer_template(tmp_path)
        lower = template.lower()
        assert "cannot fix" in lower or "can't fix" in lower or "under-specified" in lower
        # "stall" or "producer_stalled" should appear so the
        # producer understands the orchestrator's contract.
        assert "stall" in lower

    def test_producer_template_has_minimum_blast_radius_rule(self, tmp_path: Path):
        """PR-9.5 Task 2: the producer template carries the
        minimum-blast-radius rule for nit-severity findings. Empirically
        motivated by PR-9 dogfood commit ``d6460a3`` — a nit-flagged
        ``del`` idiom was "fixed" via parameter rename that broke
        callers; the correct fix was a ``# noqa`` annotation.

        Cheap structural assertion: the rule's headline phrase
        ``smallest-blast-radius`` must be in the rendered template,
        plus a citation of ``d6460a3`` so the lesson stays concrete.
        Reviewer/producer behavior is verified empirically via the
        re-dogfood in PR-9.5 Task 4, not via unit tests on the model.
        """
        from syncade.prompts import load_producer_template

        template = load_producer_template(tmp_path)
        assert "smallest-blast-radius" in template, (
            "PR-9.5 Task 2 rule missing from producer.md template — the "
            "minimum-blast-radius rule's headline phrase "
            "'smallest-blast-radius' must be in the rendered template. "
            "See "
            "path/to/pr.md "
            "Task 2 for the full rule text."
        )
        # The rule must cite d6460a3 by SHA so the lesson stays
        # concrete — the producer reads this prompt at every round
        # and the SHA-anchored example is the empirical hook.
        assert "d6460a3" in template
        # The annotation-over-rename hierarchy must be named (the
        # rule degenerates to "be careful" without the concrete
        # preference order).
        assert "noqa" in template
