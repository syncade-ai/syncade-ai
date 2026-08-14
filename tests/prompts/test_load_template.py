"""Tests for :mod:`syncade.prompts` — reviewer template loading
(packaged default vs. per-repo override) and reviewer prompt rendering.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncade.prompts import (
    load_reviewer_template,
    load_reviewer_template_for,
    load_reviewer_template_for_provider,
    render_reviewer_prompt,
    reviewer_template_name_for_provider,
)


class TestProviderReviewerTemplates:
    """Per-provider reviewer template selection (claude vs codex vs fallback)."""

    def test_basename_mapping(self):
        assert reviewer_template_name_for_provider("anthropic") == "reviewer_adversarial.md"
        assert reviewer_template_name_for_provider("openai") == "reviewer_codex.md"
        # Any other provider (custom configs, test fakes) → generic fallback.
        assert reviewer_template_name_for_provider("fake1") == "reviewer.md"

    def test_distinct_packaged_templates_per_provider(self, tmp_path: Path):
        claude = load_reviewer_template_for_provider(tmp_path, "anthropic")
        codex = load_reviewer_template_for_provider(tmp_path, "openai")
        # The two reviewers get materially different role framing.
        assert "adversarial acceptance audit" in claude
        assert "high-signal adversarial review" in codex
        assert claude != codex
        # Both still carry the placeholders the renderer substitutes.
        for template in (claude, codex):
            for placeholder in ("{pr_doc_path}", "{diff}", "{json_schema}", "{prior_round_output}"):
                assert placeholder in template

    def test_folded_operational_rules_present_in_both(self, tmp_path: Path):
        """The four load-bearing operational rules from the generic template
        are folded into BOTH per-provider templates: worktree confinement,
        the CLAUDE.md/AGENTS.md stripped-files carve-out, the workflow-state
        non-blocker rule, and schema field-name strictness (exit 70)."""
        for provider in ("anthropic", "openai"):
            template = load_reviewer_template_for_provider(tmp_path, provider)
            # Worktree confinement reinforcement.
            assert "relative paths" in template
            # Stripped-files carve-out (the recurring false-positive guard).
            assert "do NOT report it as a tracked deletion" in template
            # Workflow-state findings are coverage_gaps, not blockers.
            assert "Workflow-state findings are not blockers" in template
            # Schema field-name strictness — no aliases, parse failure = exit 70.
            assert "location" in template
            assert "exit 70" in template

    def test_consistency_class_rule_in_provider_templates(self, tmp_path: Path):
        """Both provider-specific templates must carry the 'search the whole repo
        for every instance' enumeration rule. Regression: the rule was only in the
        generic reviewer.md fallback, leaving the default anthropic/openai reviewers
        able to peel one recurring-defect site per round instead of reporting all
        sites as one finding."""
        for provider in ("anthropic", "openai"):
            template = load_reviewer_template_for_provider(tmp_path, provider)
            assert "search the whole repo for every instance" in template, (
                f"reviewer template for provider {provider!r} missing the "
                "consistency-class enumeration rule — default reviewers can "
                "peel one recurring-defect site per round"
            )
            assert "report them all as ONE finding" in template, (
                f"reviewer template for provider {provider!r} missing 'report them "
                "all as ONE finding' — consistency-class rule is incomplete"
            )

    def test_adversarial_lens_block_in_provider_templates(self, tmp_path: Path):
        """Both provider-specific templates must carry {adversarial_lens_block}
        so anthropic/openai reviewers with adversarial_lens=True receive the
        edge-enumeration block. Regression: the placeholder was absent from
        both templates while the generic fallback still had it."""
        for provider in ("anthropic", "openai"):
            template = load_reviewer_template_for_provider(tmp_path, provider)
            assert "{adversarial_lens_block}" in template, (
                f"reviewer template for provider {provider!r} missing "
                "{{adversarial_lens_block}} — adversarial_lens=True reviewers "
                "silently lose the edge-enumeration block"
            )
            rendered_on = render_reviewer_prompt(
                template, pr_doc_path="x", diff="d", json_schema="s", adversarial_lens=True
            )
            assert "Adversarial edge enumeration" in rendered_on
            rendered_off = render_reviewer_prompt(
                template, pr_doc_path="x", diff="d", json_schema="s", adversarial_lens=False
            )
            assert "Adversarial edge enumeration" not in rendered_off

    def test_bug_class_block_in_all_reviewer_templates(self, tmp_path: Path):
        """Every reviewer template — the two provider-specific ones AND the
        generic fallback — must carry {bug_class_block}, so a reviewer with
        bug_class_sweep=True (the default) receives the directed sweep. Guards
        against the placeholder being dropped from one template on a future edit,
        which would silently disable the feature for that provider."""
        templates = {
            "anthropic": load_reviewer_template_for_provider(tmp_path, "anthropic"),
            "openai": load_reviewer_template_for_provider(tmp_path, "openai"),
            "generic": load_reviewer_template(tmp_path),
        }
        for label, template in templates.items():
            assert "{bug_class_block}" in template, (
                f"reviewer template {label!r} missing {{bug_class_block}} — "
                "bug_class_sweep reviewers silently lose the directed sweep"
            )

    def test_bug_class_sweep_default_on_and_toggleable(self, tmp_path: Path):
        """bug_class_sweep defaults on: an omitted flag renders the sweep; passing
        False omits it. Pins the default-ON behavior (unlike the opt-in lens)."""
        template = load_reviewer_template(tmp_path)
        rendered_default = render_reviewer_prompt(
            template, pr_doc_path="x", diff="d", json_schema="s"
        )
        assert "Directed bug-class sweep" in rendered_default
        rendered_off = render_reviewer_prompt(
            template, pr_doc_path="x", diff="d", json_schema="s", bug_class_sweep=False
        )
        assert "Directed bug-class sweep" not in rendered_off

    def test_unknown_provider_uses_generic_fallback(self, tmp_path: Path):
        assert load_reviewer_template_for_provider(tmp_path, "fake1") == load_reviewer_template(
            tmp_path
        )

    def test_generic_reviewer_override_reached_by_default_providers(self, tmp_path: Path):
        """A .syncade/templates/reviewer.md override must be seen by anthropic and
        openai reviewers when no provider-specific override exists. Regression:
        the prior implementation skipped the generic override for these two providers
        and sent them straight to the packaged provider-specific templates."""
        override_dir = tmp_path / ".syncade" / "templates"
        override_dir.mkdir(parents=True)
        generic = "GENERIC OVERRIDE: {pr_doc_path}\n"
        (override_dir / "reviewer.md").write_text(generic)
        assert load_reviewer_template_for_provider(tmp_path, "anthropic") == generic
        assert load_reviewer_template_for_provider(tmp_path, "openai") == generic

    def test_provider_specific_override_beats_generic_override(self, tmp_path: Path):
        """When both a provider-specific and a generic override exist, the
        provider-specific one wins."""
        override_dir = tmp_path / ".syncade" / "templates"
        override_dir.mkdir(parents=True)
        (override_dir / "reviewer.md").write_text("GENERIC\n")
        (override_dir / "reviewer_adversarial.md").write_text("SPECIFIC CLAUDE\n")
        assert load_reviewer_template_for_provider(tmp_path, "anthropic") == "SPECIFIC CLAUDE\n"
        # openai has no specific override → falls back to generic
        assert load_reviewer_template_for_provider(tmp_path, "openai") == "GENERIC\n"

    def test_per_repo_override_per_provider(self, tmp_path: Path):
        override_dir = tmp_path / ".syncade" / "templates"
        override_dir.mkdir(parents=True)
        custom = "CUSTOM CLAUDE: {pr_doc_path}\n"
        (override_dir / "reviewer_adversarial.md").write_text(custom)
        assert load_reviewer_template_for_provider(tmp_path, "anthropic") == custom
        # openai is unaffected by the claude override.
        assert "high-signal adversarial review" in load_reviewer_template_for_provider(
            tmp_path, "openai"
        )


class TestLoadTemplate:
    def test_packaged_default_loads(self, tmp_path: Path):
        """No override in tmp_path — falls through to the packaged
        default. The template must contain the placeholders the
        renderer knows about so the smoke test can produce a usable
        reviewer prompt."""
        template = load_reviewer_template(tmp_path)
        # The bundled template should have all four documented placeholders.
        assert "{pr_doc_path}" in template
        assert "{diff}" in template
        assert "{master_plan_path}" in template
        assert "{json_schema}" in template

    def test_reviewer_template_has_pr17_stripped_files_note(self, tmp_path: Path):
        """PR-17: the reviewer template warns that CLAUDE.md / AGENTS.md
        are stripped from BOTH the worktree and the diff, so reviewers
        don't pattern-match on their absence and report a tracked
        deletion (the recurring PR-16 false positive). Tests pin the
        literal phrases the brief specifies."""
        template = load_reviewer_template(tmp_path)
        assert "Stripped files" in template
        assert "do NOT report it as a tracked deletion" in template

    def test_per_repo_override_wins(self, tmp_path: Path):
        """When `<repo_root>/.syncade/templates/reviewer.md` exists,
        its contents are returned instead of the packaged default."""
        override_dir = tmp_path / ".syncade" / "templates"
        override_dir.mkdir(parents=True)
        custom = "CUSTOM TEMPLATE: pr={pr_doc_path}\n"
        (override_dir / "reviewer.md").write_text(custom)

        loaded = load_reviewer_template(tmp_path)
        assert loaded == custom

    def test_missing_override_falls_back_silently(self, tmp_path: Path):
        # Empty repo root — no .syncade/ at all.
        assert not (tmp_path / ".syncade").exists()
        template = load_reviewer_template(tmp_path)
        # Got the packaged default
        assert "{pr_doc_path}" in template

    def test_override_directory_without_file_falls_back(self, tmp_path: Path):
        """Edge: the .syncade/templates/ directory exists but
        reviewer.md doesn't. Should still fall through to the
        packaged default."""
        (tmp_path / ".syncade" / "templates").mkdir(parents=True)
        template = load_reviewer_template(tmp_path)
        assert "{pr_doc_path}" in template

    def test_packaged_template_has_appendix_a_provenance(self, tmp_path: Path):
        """The packaged template should derive from PRD Appendix A.
        Spot-check by looking for one of its distinctive phrases."""
        template = load_reviewer_template(tmp_path)
        # PRD Appendix A's "kitchen sink" phrase is a fingerprint.
        assert "kitchen sink" in template.lower()

    def test_packaged_template_requires_fenced_verdict(self, tmp_path: Path):
        """PR-5.6: the template must instruct the reviewer to wrap the
        verdict JSON in a ```json fence and to keep all other JSON-shaped
        text out of the response. Belt-and-braces with the parser's
        verdict-block selection."""
        template = load_reviewer_template(tmp_path)
        lower = template.lower()
        # Names the fence and the json label
        assert "```json" in template
        assert "fence" in lower
        # Explicitly forbids JSON outside the fence
        assert "outside this fence" in lower or "outside the fence" in lower
        # Empirically, long real reviews can drift into a prose review
        # with headings and omit the final JSON. The prompt must be
        # explicit that the whole answer is the fenced JSON block.
        assert "entire response" in lower
        assert "nothing else" in lower
        assert "prose preamble" in lower
        assert "the run fails with exit 70" in lower

    def test_load_reviewer_template_for_prefers_explicit_template(self, tmp_path: Path):
        # Explicit template overrides provider-based selection: an anthropic
        # reviewer pinned to the codex template gets codex content, not its
        # provider default. Rename-independent — uses templates that exist now.
        got = load_reviewer_template_for(
            tmp_path, provider="anthropic", template="reviewer_codex.md"
        )
        assert "high-signal adversarial review" in got
        assert "adversarial acceptance audit" not in got
        # No override → anthropic gets its provider-default (adversarial) template.
        fallback = load_reviewer_template_for(tmp_path, provider="anthropic", template=None)
        assert "adversarial acceptance audit" in fallback

    def test_packaged_template_requires_pr6_narrative_fields(self, tmp_path: Path):
        """PR-6 REPLACED the PR-5.6 free-form "Verification summary"
        narrative section with the structured ``summary`` field on
        ``ReviewerOutput`` — the field IS the verification summary now,
        not a separate narrative chunk. The template must instruct the
        reviewer to populate all four PR-6 fields:

        - ``summary`` (replaces the verification-summary section)
        - ``priority_order``
        - ``coverage_gaps``
        - ``dismissed_concerns``
        """
        template = load_reviewer_template(tmp_path)
        # Each of the four required fields must be named in the
        # template body so the reviewer knows what to populate.
        for name in ("summary", "priority_order", "coverage_gaps", "dismissed_concerns"):
            assert name in template, f"template missing required-field instruction for {name!r}"
        # The template must say "required" (non-empty contract for
        # summary, "[] if none" semantics for the lists).
        assert "required" in template.lower()

    def test_packaged_template_does_not_duplicate_verification_summary(self, tmp_path: Path):
        """PR-6 explicitly REPLACES the PR-5.6 "Verification summary"
        narrative section with the structured ``summary`` field — the
        template must not ask for both. A reviewer told to write a
        narrative section AND a structured summary field will either
        duplicate content (operator confusion) or omit one (validator
        failure)."""
        template = load_reviewer_template(tmp_path)
        # The bare phrase as a section heading should be gone — only
        # the field name `summary` (described as "IS the verification
        # summary now") survives. We allow the phrase only when it
        # explicitly says it's the field, not a separate section.
        lower = template.lower()
        # Old PR-5.6 instruction shapes that would indicate duplication
        forbidden_phrases = [
            'before the json fence include a\n"verification summary"',
            'include a "verification summary" section',
            "include a verification summary section",
        ]
        for phrase in forbidden_phrases:
            assert phrase not in lower, (
                f"template still has the PR-5.6 narrative instruction "
                f"{phrase!r}; PR-6 replaced it with the summary field"
            )

    def test_reviewer_template_workflow_state_rule_reinforced(self, tmp_path: Path):
        """PR-10.5 Task 2: the reviewer template carries the
        REINFORCED operator-procedural findings rule. The headline
        phrase changed from PR-9's "Workflow-state findings vs. code
        findings" to "Workflow-state findings are NOT blockers,
        PERIOD" — same PR-9.5-T3-style stronger language pattern.

        The rule itself is prose for the LLM to follow; reviewer
        behavior is verified empirically via the dogfood, not via
        unit tests on the reviewer model.
        """
        template = load_reviewer_template(tmp_path)
        assert "Workflow-state findings are NOT blockers, PERIOD" in template, (
            "PR-10.5 Task 2 rule missing from reviewer.md template — the "
            "REINFORCED operator-procedural rule's headline phrase "
            "'Workflow-state findings are NOT blockers, PERIOD' must be in "
            "the rendered template. See "
            "path/to/pr.md "
            "Task 2 for the full rule text."
        )
        # The rule must also name the alternative classification
        # (coverage_gap) and reserve `blocker` for code-shaped issues,
        # otherwise the rule degenerates into "be lenient" without a
        # concrete reclassification target.
        assert "coverage_gap" in template
        assert "blocker" in template

    def test_reviewer_template_cites_pr_10_dogfood_anchor(self, tmp_path: Path):
        """PR-10.5 Task 2: the reinforced rule cites the PR-10
        dogfood run id (``2026-05-27T11-09-50``) as the empirical
        anchor for the reinforcement. Same anchor pattern as
        PR-9.5 T3, which empirically held across 4 rounds with no
        schema deviations. The run-id literal in the template is
        what makes the rule's strengthening traceable to a
        specific incident — future readers can grep for the run-id
        and find both the dogfood artifacts AND the rule it
        triggered.
        """
        template = load_reviewer_template(tmp_path)
        assert "2026-05-27T11-09-50" in template, (
            "PR-10.5 Task 2 empirical anchor missing — the PR-10 dogfood "
            "run id `2026-05-27T11-09-50` must appear in the rendered "
            "template as the citation for the reinforced rule. See "
            "path/to/pr.md Task 2."
        )

    def test_reviewer_template_has_schema_strictness_rule(self, tmp_path: Path):
        """PR-9.5 Task 3: the reviewer template carries the strict
        schema-enforcement rule. Empirically motivated by PR-9 dogfood
        round 2 — a reviewer emitted ``"location"`` instead of the
        schema's ``"file"`` field; the parser correctly rejected it
        but the round failed at parse with exit 70 and the loop
        terminated.

        Cheap structural assertion: the rule's headline phrase
        ``Schema field names are exact`` must be in the rendered
        template, plus the empirical-anchor reference to the PR-9
        run id and the explicit forbidden-alias list (``location``
        being the canonical example that caused the failure).
        Reviewer behavior is verified empirically via the dogfood,
        not via unit tests on the reviewer model.
        """
        template = load_reviewer_template(tmp_path)
        assert "Schema field names are exact" in template, (
            "PR-9.5 Task 3 rule missing from reviewer.md template — "
            "the schema-strictness rule's headline phrase "
            "'Schema field names are exact' must be in the rendered "
            "template. See "
            "path/to/pr.md "
            "Task 3 for the full rule text."
        )
        # The rule must explicitly name `location` as a forbidden
        # alias — that's the alias that empirically caused the
        # PR-9 round-2 parse failure. Without naming it, the rule
        # degenerates to abstract "be exact" guidance.
        assert "location" in template
        # The PR-9 run id is the empirical anchor; if a future
        # reviewer wonders "what's an example?", the run-id pointer
        # lets them inspect the artifact for themselves.
        assert "2026-05-27T09-06-28" in template
        # The strictness rule must point at actual schema field names.
        # `description` is not a ReviewerOutput/Finding field; naming it
        # here would invite exactly the alias drift this rule prevents.
        assert "`finding`" in template
        assert "`description`" not in template

    def test_reviewer_template_has_unverifiable_carveout(self, tmp_path: Path):
        """PR-20 Task 2: the reviewer template reconciles the
        reproduction-before-SHIP rule with items that are unverifiable
        by construction — environment/sandbox limits, or workflow-state
        / verified-after-review — which must go to ``coverage_gaps`` and
        NOT force NO-SHIP. NO-SHIP stays reserved for genuine defect
        suspicion.

        Empirically motivated by the PR-19 dogfood split verdict: a
        reviewer NO-SHIPped on a sandbox-limited ``--selfcheck`` and a
        not-yet-recorded dogfood — both should have been coverage_gaps.
        Reviewer behavior is verified via the dogfood, not unit tests on
        the model; this is a cheap structural assertion that the rule
        text is present (same pattern as the workflow-state and
        schema-strictness rule tests).
        """
        template = load_reviewer_template(tmp_path)
        # Headline framing: the reproduction rule governs code behavior,
        # which is what carves out unverifiable-by-construction items.
        assert "Reproduction-before-SHIP governs" in template, (
            "PR-20 Task 2 carve-out missing from reviewer.md — the "
            "reconciliation between the reproduction rule and "
            "unverifiable-by-construction items must be present. See "
            "path/to/pr.md Task 2."
        )
        # Environment/sandbox-limited items → coverage_gaps, not NO-SHIP.
        assert "environment/sandbox" in template
        # Workflow-state / verified-after-this-review items → coverage_gaps.
        assert "verified after this review" in template
        # NO-SHIP is reserved for defect suspicion, not unverifiability.
        assert "Reserve NO-SHIP" in template
        # Empirical anchor: the concrete sandbox-limited selfcheck case must
        # stay in the prompt, without relying on PR-ticket labels.
        assert "sandbox-limited `--selfcheck`" in template

    def test_reviewer_template_has_consistency_class_rule(self, tmp_path: Path):
        """The reviewer template instructs enumerating EVERY instance of a
        consistency-class defect (stale doc/contract strings, lingering
        references, a value documented inconsistently across files) in ONE
        finding, rather than reporting the first site and letting the loop
        chase the rest across rounds.

        Empirically motivated by the PR-24 dogfood run
        ``2026-05-30T17-33-17``: a single "exit-10 escalation documented as
        unconditional" inconsistency lived in the artifact renderers, the
        PRD exit-code table, and two source docstrings; the reviewer
        surfaced one site per round, the surgical producer fixed only the
        named site each round, and the loop hit max-rounds (exit 20) without
        converging. Reviewer behavior is verified via the dogfood, not unit
        tests on the model; this is the same cheap structural rule-text
        assertion as the workflow-state / schema-strictness / unverifiable
        carve-out pins above.
        """
        template = load_reviewer_template(tmp_path)
        # The rule's headline directive.
        assert "Consistency-class findings: enumerate every instance" in template, (
            "consistency-class enumeration rule missing from reviewer.md — a "
            "reviewer must report all instances of a recurring defect in one "
            "finding, not one site per round."
        )
        # The producer-myopia rationale (why one-site reports don't converge).
        assert "minimum" in template and "no others" in template
        # Empirical anchor for the consistency-class lesson.
        assert "2026-05-30T17-33-17" in template
        # Must NOT relax the reproduction bar into guessing other sites.
        assert "reproduction bar is unchanged" in template

    def test_reviewer_template_has_worktree_confinement_rule(self, tmp_path: Path):
        """Worktree-escape fix: the reviewer template instructs the reviewer to
        review ONLY within its worktree (cwd) and NOT to cd to / read the
        operator's main repository, even if it discovers the path via the
        worktree's `.git` file.

        Empirically motivated by run ``2026-05-30T21-22-19``: claude-reviewer
        followed the absolute MAIN pr_doc path, cd'd into the operator's repo
        for 25/32 shell commands, and read only main-repo files — reviewing a
        different (un-stripped) tree than the snapshot it was asked to judge.
        The orchestrator fix (worktree-local pr_doc reference) removes the lure;
        this prompt rule is the behavioral guard since `.git` still reveals the
        main path. Reviewer behavior is verified via the dogfood, not unit
        tests on the model — this is the same cheap structural rule-text pin.
        """
        template = load_reviewer_template(tmp_path)
        assert "Your worktree is the only tree to review" in template, (
            "worktree-confinement rule missing from reviewer.md — the reviewer "
            "must be told to operate only within its worktree."
        )
        # The don't-touch-main instruction + the .git escape-route it must resist.
        assert "main repository" in template
        assert ".git" in template
        # Empirical anchor (same "run <id>" citation style as the other pins).
        assert "2026-05-30T21-22-19" in template


class TestRenderPrompt:
    def test_basic_substitution(self):
        template = "PR: {pr_doc_path}\nDiff:\n{diff}\nSchema:\n{json_schema}"
        rendered = render_reviewer_prompt(
            template,
            pr_doc_path="path/to/pr.md",
            diff="--- a\n+++ b\n",
            json_schema='{"verdict": "..."}',
        )
        assert "PR: path/to/pr.md" in rendered
        assert "Diff:\n--- a\n+++ b\n" in rendered
        assert '"verdict"' in rendered

    def test_master_plan_path_none_becomes_none_marker(self):
        template = "plan={master_plan_path}; diff={diff}; pr={pr_doc_path}; schema={json_schema}"
        rendered = render_reviewer_prompt(
            template,
            pr_doc_path="x",
            diff="d",
            json_schema="s",
            master_plan_path=None,
        )
        assert "plan=(none)" in rendered

    def test_master_plan_path_supplied_substitutes(self):
        template = "plan={master_plan_path}; diff={diff}; pr={pr_doc_path}; schema={json_schema}"
        rendered = render_reviewer_prompt(
            template,
            pr_doc_path="x",
            diff="d",
            json_schema="s",
            master_plan_path="docs/master-plan.md",
        )
        assert "plan=docs/master-plan.md" in rendered
        assert "(none)" not in rendered

    def test_unknown_placeholder_raises_key_error(self):
        # A typo in a custom override should NOT silently render — it
        # should raise so the user notices immediately.
        template = "{frobnicate} is not a real placeholder"
        with pytest.raises(KeyError):
            render_reviewer_prompt(
                template,
                pr_doc_path="x",
                diff="d",
                json_schema="s",
            )

    def test_packaged_template_renders_cleanly_end_to_end(self, tmp_path: Path):
        """Sanity: the bundled template + a typical caller's kwargs
        produces a usable string with no unsubstituted placeholders."""
        template = load_reviewer_template(tmp_path)
        rendered = render_reviewer_prompt(
            template,
            pr_doc_path="path/to/pr.md",
            diff="diff --git a b\n",
            master_plan_path=None,
            json_schema='{"verdict": "SHIP | NO-SHIP", "findings": []}',
        )
        # No leftover {placeholder} tokens. Use a tight check: any
        # surviving "{" followed by a letter is suspicious. The PR-6
        # template includes a literal-brace JSON example
        # (`{{"verdict": "SHIP", ...}}`) which renders to `{"verdict":
        # "SHIP", ...}` after format_map; that's the verdict-block
        # demonstration, not an unsubstituted placeholder. Any token
        # of the form `{Letter...}` after rendering would still be a
        # bug — the `"` delimits real placeholder candidates from the
        # demo's quoted JSON keys.
        import re

        leftover = re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", rendered)
        assert leftover is None, f"unsubstituted placeholder: {leftover.group()}"
        assert "(none)" in rendered  # master_plan_path was None
        assert "path/to/pr.md" in rendered

    def test_rendered_prompt_uses_get_findings_schema_string(self, tmp_path: Path):
        """The rendered prompt's {json_schema} placeholder is filled by
        the orchestrator from :func:`get_findings_schema_string`. PR-6
        added four fields to the schema; the rendered template must
        contain them. Pins the round-trip: schema string updates
        propagate through the template into what the reviewer sees."""
        from syncade.findings import get_findings_schema_string

        template = load_reviewer_template(tmp_path)
        rendered = render_reviewer_prompt(
            template,
            pr_doc_path="path/to/pr.md",
            diff="diff --git a b\n",
            master_plan_path=None,
            json_schema=get_findings_schema_string(),
        )
        # Each PR-6-required field name appears in the rendered prompt.
        for name in ("summary", "priority_order", "coverage_gaps", "dismissed_concerns"):
            assert name in rendered, (
                f"rendered prompt missing schema field {name!r} — the "
                "schema-string substitution path regressed"
            )

    def test_diff_with_special_format_chars_is_safe(self):
        """Diffs frequently contain `{` and `}` characters (e.g., in
        code snippets). format_map should treat the substituted value
        as a literal — those braces in the SUBSTITUTED content must not
        re-trigger format-string parsing."""
        template = "diff:\n{diff}\nend"
        diff_text = "+ def f(): return {'a': 1, 'b': 2}"
        rendered = render_reviewer_prompt(
            template,
            pr_doc_path="x",
            diff=diff_text,
            json_schema="s",
        )
        assert diff_text in rendered
