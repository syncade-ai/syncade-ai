"""Tests for :mod:`syncade.prompts` — spec-audit template rendering,
verification-discipline prompts, and the adversarial-lens block.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from syncade.prompts import (
    load_reviewer_template,
    load_spec_audit_template,
    render_reviewer_prompt,
    render_spec_audit_prompt,
)


def test_spec_audit_template_has_skeptical_default(tmp_path: Path):
    """The packaged spec_audit.md template must contain the skeptical-
    default phrase used by the reviewer template (PR-7 pattern):
    the default verdict is NEEDS-CLARIFICATION."""
    template = load_spec_audit_template(tmp_path)
    assert "default verdict" in template.lower() or "NEEDS-CLARIFICATION" in template


def test_spec_audit_template_lists_six_issue_classes(tmp_path: Path):
    """The template must enumerate all six issue classes so the auditor
    knows what to look for."""
    template = load_spec_audit_template(tmp_path)
    expected_markers = [
        "Unverified claims",
        "Internal contradictions",
        "Ambiguous acceptance criteria",
        "Missing references",
        "Scope drift",
        "Missing structural sections",
    ]
    for marker in expected_markers:
        assert marker in template, f"Expected marker not found in spec_audit.md: {marker!r}"


def test_spec_audit_template_cites_empirical_anchors(tmp_path: Path):
    """The template must cite the canonical xhigh-on-claude empirical
    anchor so the auditor learns from historical examples."""
    template = load_spec_audit_template(tmp_path)
    # The anchor is the PR-8.5 unverified xhigh claim
    assert "xhigh" in template or "PR-8.5" in template


def test_render_spec_audit_prompt_substitutes_placeholders(tmp_path: Path):
    """render_spec_audit_prompt substitutes {pr_doc_path} and
    {json_schema} correctly."""
    template = "{pr_doc_path} -- {json_schema}"
    rendered = render_spec_audit_prompt(
        template,
        pr_doc_path="/tmp/pr-test.md",
        json_schema="<schema>",
    )
    assert "/tmp/pr-test.md" in rendered
    assert "<schema>" in rendered


def test_render_spec_audit_prompt_rejects_unknown_placeholder(tmp_path: Path):
    """An unknown placeholder in the template raises KeyError — same
    surface-typos-loudly contract as the reviewer and synthesizer renderers."""
    template = "{pr_doc_path} -- {unknown_key}"
    with pytest.raises(KeyError):
        render_spec_audit_prompt(
            template,
            pr_doc_path="/tmp/pr-test.md",
            json_schema="<schema>",
        )


class TestVerificationDisciplinePrompts:
    """PR-19 T4: producer.md gains a "Fix discipline" section and reviewer.md
    extends "a clean read is not verification" to dispositions (dismissals +
    SHIP). Prompt INSTRUCTIONS only — no response-schema changes."""

    def test_producer_fix_discipline_section_present(self, tmp_path: Path):
        from syncade.prompts import load_producer_template

        template = load_producer_template(tmp_path)
        lower = template.lower()
        assert "## fix discipline" in lower
        # (a) test-per-fix, fails-before/passes-after
        assert "regression test" in lower
        assert "fails" in lower and "passes after" in lower
        # (b) update invalidated comments / docs
        assert "update every artifact the change invalidates" in lower
        assert "comment" in lower and "docstring" in lower
        # (c) reproduce safety claims, do not assert
        assert "reproduce safety claims" in lower
        assert "fails safely" in lower

    def test_producer_no_longer_forbids_regression_tests(self, tmp_path: Path):
        """The old 'no new tests unless a finding asks' rule conflicted with
        test-per-fix; it must now permit the regression test that pins a fix
        while still forbidding speculative/unrelated tests."""
        from syncade.prompts import load_producer_template

        template = load_producer_template(tmp_path)
        lower = template.lower()
        # Reconciled rule: no speculative/unrelated tests (scope discipline kept)
        assert "do not add speculative or unrelated tests" in lower
        # The contradictory absolute "no new tests" wording is gone.
        assert "no new tests" not in lower

    def test_producer_fix_discipline_renders(self, tmp_path: Path):
        from syncade.prompts import load_producer_template, render_producer_prompt

        template = load_producer_template(tmp_path)
        rendered = render_producer_prompt(
            template,
            pr_doc_path="/x/pr.md",
            findings_md_path="/y/findings.md",
            test_run_stdout_path="(no test failure this round)",
            worktree_path="/z/wt",
            round_number=1,
            max_rounds=3,
        )
        # Static instruction survives render (no new placeholder added).
        assert "Fix discipline" in rendered
        assert "regression test" in rendered.lower()

    def test_producer_template_constrains_commits_to_worktree_path(self, tmp_path: Path):
        """A producer must not infer the repo to edit from run-artifact paths.

        Historical stall 2026-06-26T17-41-18: Claude committed the correct
        fix in the pytest temp repo that contained .syncade/runs instead of
        the detached producer worktree, so stall detection saw HEAD unchanged.
        """
        from syncade.prompts import load_producer_template

        template = load_producer_template(tmp_path)
        match = re.search(
            r"(?ms)^## Repository boundary\s*\n(?P<section>.*?)(?=^## |\Z)",
            template,
        )
        assert match is not None

        section = re.sub(r"[*`]+", "", match.group("section")).casefold()
        section = re.sub(r"\s+", " ", section)

        assert re.search(r"\bonly\b.{0,80}\bedit\b.{0,80}\bfiles?\b", section)
        assert re.search(r"\bunder\b.{0,80}\{worktree_path\}", section)
        assert re.search(
            r"\bonly\b.{0,120}\bgit commit\b.{0,120}\bfrom\b.{0,80}\{worktree_path\}",
            section,
        )

        for artifact_path in ("{findings_md_path}", "{pr_doc_path}"):
            assert artifact_path in section
            assert re.search(
                rf"\bdo not\b.{{0,120}}\bedit\b.{{0,80}}\bcommit\b.{{0,200}}"
                rf"{re.escape(artifact_path)}",
                section,
            )

    def test_reviewer_dispositions_require_evidence_section_present(self, tmp_path: Path):
        template = load_reviewer_template(tmp_path)
        lower = template.lower()
        assert "dispositions require reproduction, not reasoning" in lower
        # Dismissals require reproduction, not reasoning.
        assert "dismissing a potential bug requires a reproduction" in lower
        assert "dismissed_concerns" in template
        # SHIP requires affirmative reproduction-backed verification.
        assert "ship requires affirmative" in lower
        # Mirrors the finding evidence bar onto dispositions.
        assert "evidence_cmd" in template

    def test_reviewer_dispositions_section_renders(self, tmp_path: Path):
        template = load_reviewer_template(tmp_path)
        rendered = render_reviewer_prompt(
            template,
            pr_doc_path="/tmp/pr.md",
            diff="diff --git a/x b/x",
            json_schema="<schema>",
        )
        assert "Dispositions require reproduction, not reasoning" in rendered
        assert "SHIP requires affirmative" in rendered


class TestAdversarialLens:
    """Per-reviewer adversarial-edge-enumeration lens (claude-reviewer
    confirmatory-review fix; dogfood run ``2026-05-31T14-07-08`` had claude
    SHIP a PR while codex caught two real blockers in undocumented flag/input
    edges). ``render_reviewer_prompt`` includes the block IFF
    ``adversarial_lens=True``; default + ``False`` render byte-identically to
    today (codex, the control, is unchanged). The packaged template carries the
    placeholder, positioned in the verification-discipline region (before the
    output-format section, so the strict JSON-fence instruction is not diluted).
    """

    _ANCHOR = "Adversarial edge enumeration"

    def test_lens_true_includes_block(self):
        template = "pre {adversarial_lens_block} post {pr_doc_path}{diff}{json_schema}"
        rendered = render_reviewer_prompt(
            template,
            pr_doc_path="x",
            diff="d",
            json_schema="s",
            adversarial_lens=True,
        )
        assert self._ANCHOR in rendered

    def test_lens_false_excludes_block(self):
        template = "pre {adversarial_lens_block} post {pr_doc_path}{diff}{json_schema}"
        rendered = render_reviewer_prompt(
            template,
            pr_doc_path="x",
            diff="d",
            json_schema="s",
            adversarial_lens=False,
        )
        assert self._ANCHOR not in rendered

    def test_lens_defaults_false(self):
        template = "pre {adversarial_lens_block} post {pr_doc_path}{diff}{json_schema}"
        rendered = render_reviewer_prompt(
            template,
            pr_doc_path="x",
            diff="d",
            json_schema="s",
        )
        assert self._ANCHOR not in rendered

    def test_packaged_template_carries_placeholder(self, tmp_path: Path):
        template = load_reviewer_template(tmp_path)
        assert "{adversarial_lens_block}" in template

    def test_packaged_template_block_on_off_and_positioned(self, tmp_path: Path):
        template = load_reviewer_template(tmp_path)
        on = render_reviewer_prompt(
            template,
            pr_doc_path="p",
            diff="d",
            master_plan_path=None,
            json_schema="s",
            adversarial_lens=True,
        )
        off = render_reviewer_prompt(
            template,
            pr_doc_path="p",
            diff="d",
            master_plan_path=None,
            json_schema="s",
            adversarial_lens=False,
        )
        assert self._ANCHOR in on
        assert self._ANCHOR not in off
        # The block sits BEFORE the output-format section — verification
        # discipline belongs with the other disposition rules, and the strict
        # JSON-fence instruction must stay near the end.
        assert on.index(self._ANCHOR) < on.index("## Output format")

    def test_no_lens_render_is_byte_identical_to_pre_lens_template(self, tmp_path: Path):
        """Rendering with adversarial_lens=False must produce byte-identical
        output to what the template yields without the placeholder line (the
        control-path acceptance criterion: no-lens reviewers and every
        pre-existing config must not see drifted prompt bytes)."""
        template = load_reviewer_template(tmp_path)
        off = render_reviewer_prompt(
            template,
            pr_doc_path="p",
            diff="d",
            master_plan_path=None,
            json_schema="s",
            adversarial_lens=False,
        )
        # Simulate pre-lens: remove the placeholder line while preserving
        # the surrounding blank line that separated the two sections.
        pre_lens_template = template.replace("\n{adversarial_lens_block}\n", "\n\n")
        pre_lens = render_reviewer_prompt(
            pre_lens_template,
            pr_doc_path="p",
            diff="d",
            master_plan_path=None,
            json_schema="s",
        )
        assert off == pre_lens

    def test_block_v2_forces_falsification_beyond_acceptance_criteria(self):
        """v2 strengthening (dogfood datapoint 1, run ``2026-05-31T15-19-39``):
        claude mapped "enumerate edges" onto "verify the brief's stated ACs" and
        confirmed the code PATH (the no-lens branch fires) rather than the
        invariant CLAIM (it never diffed the bytes the brief called
        "byte-identical"). The v2 block must (a) distinguish the brief's
        acceptance criteria from the edges it omits — ACs are the floor, not the
        ceiling — and (b) require constructing the comparison that would FALSIFY
        each brief-asserted invariant, not merely confirm the mechanism exists."""
        from syncade.prompts import ADVERSARIAL_LENS_BLOCK

        block = ADVERSARIAL_LENS_BLOCK.lower()
        # (a) ACs are the floor, not the ceiling — probing must go beyond them.
        assert "acceptance criteria" in block
        # (b) falsify the asserted invariants (don't confirm the code path).
        assert "falsif" in block

    def test_block_v3_severity_discipline_and_end_to_end(self):
        """v3 strengthening (dogfood datapoint 4, run ``2026-06-02T13-45-00``):
        v2-claude probed hard (539s) but still SHIPped over two real logic
        blockers codex caught — (1) a `--base` correctly passed to the drafter but
        DROPPED from the printed ratification handoff (a unit verified, the
        end-to-end journey not), and (2) malformed input silently producing a
        plausible-but-wrong partial result (happy path verified, the consequence
        of bad input not). And it downgraded its own findings to nits and shipped.
        v3 must force (a) end-to-end verification at EVERY surface, and (b)
        severity discipline — a real defect is a NO-SHIP blocker, not a nit you
        ship over."""
        from syncade.prompts import ADVERSARIAL_LENS_BLOCK

        block = ADVERSARIAL_LENS_BLOCK.lower()
        # (a) trace the full journey, check invariants at every surface.
        assert "every surface" in block
        # (b) severity discipline — don't downgrade a real defect to ship.
        assert "do not downgrade" in block
