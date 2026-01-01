"""Tests for :mod:`syncade.prompts` — the parameterized loader, the
reviewer prior-round-output section, and the synthesizer template.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncade.prompts import (
    load_reviewer_template,
    load_synthesizer_template,
    load_template,
    render_reviewer_prompt,
    render_synthesizer_prompt,
)
from syncade.synthesis import get_synthesizer_schema_string


class TestReviewerTemplatePriorRoundOutput:
    """PR-14 Task 1: the packaged reviewer template carries the
    "Your prior round's review" section with the
    ``{prior_round_output}`` placeholder. The renderer accepts the
    new kwarg with a documented default sentinel for round 0.

    The cross-round-context flow itself is wired in PR-14 Task 3 (in
    the orchestrator); these tests pin the prompt-rendering surface
    only.
    """

    def test_reviewer_template_has_prior_round_output_section(self, tmp_path: Path):
        """The bundled reviewer template must contain the headline
        phrase ``Your prior round's review`` and the
        ``{prior_round_output}`` placeholder. Without the placeholder,
        ``format_map`` would silently ignore the kwarg; without the
        headline, the reviewer wouldn't know what the substituted
        text is."""
        template = load_reviewer_template(tmp_path)
        assert "Your prior round's review" in template, (
            "PR-14 Task 1: reviewer.md must carry the headline phrase "
            "'Your prior round's review' so the reviewer knows what "
            "the substituted text represents."
        )
        assert "{prior_round_output}" in template, (
            "PR-14 Task 1: reviewer.md must declare the "
            "{prior_round_output} placeholder so the renderer's "
            "format_map substitution lands."
        )

    def test_render_reviewer_prompt_substitutes_prior_round_output(self):
        """Passing ``prior_round_output`` substitutes the supplied text
        into the rendered prompt verbatim. Cross-round content can be
        large (5-20k tokens of reasoning + structured output); the
        renderer must pass it through unchanged."""
        template = "before\n{prior_round_output}\nafter"
        sample_prior = (
            "Round 0 verdict: NO-SHIP. Findings: \n"
            "- src/foo.py:42 — missing null check on user.name\n"
            "- src/bar.py:7 — unused import (nit)\n"
        )
        rendered = render_reviewer_prompt(
            template,
            pr_doc_path="pr.md",
            diff="(no diff)",
            json_schema="(schema)",
            prior_round_output=sample_prior,
        )
        assert sample_prior in rendered, (
            "the prior round response text must appear in the rendered "
            "prompt verbatim — the renderer should pass it through "
            "without truncation"
        )

    def test_render_reviewer_prompt_default_when_round_zero(self):
        """Round 0 callers omit ``prior_round_output``; the renderer
        substitutes the documented sentinel
        ``"(no prior round — this is round 0)"``. The sentinel is the
        documented "no prior context" signal so the round-0 prompt
        still reads cleanly under strict ``format_map``."""
        template = "before\n{prior_round_output}\nafter"
        rendered = render_reviewer_prompt(
            template,
            pr_doc_path="pr.md",
            diff="(no diff)",
            json_schema="(schema)",
        )
        assert "(no prior round — this is round 0)" in rendered, (
            "round-0 calls (no prior_round_output kwarg) must substitute "
            "the documented sentinel so the prompt still reads cleanly"
        )


class TestLoadTemplateParameterized:
    """``load_template(repo_root, basename)`` — the canonical entry
    point the per-template wrappers now delegate to."""

    def test_load_template_reviewer(self, tmp_path: Path):
        """``load_template(p, "reviewer.md")`` and
        ``load_reviewer_template(p)`` return identical content — the
        wrapper is a thin delegate."""
        from_param = load_template(tmp_path, "reviewer.md")
        from_wrapper = load_reviewer_template(tmp_path)
        assert from_param == from_wrapper

    def test_load_template_synthesizer(self, tmp_path: Path):
        """The synthesizer template is shipped and loadable by name."""
        template = load_template(tmp_path, "synthesizer.md")
        # Contains the four documented placeholders.
        assert "{pr_doc_path}" in template
        assert "{master_plan_path}" in template
        assert "{reviewer_outputs_json}" in template
        assert "{json_schema}" in template

    @pytest.mark.parametrize(
        "bad_name",
        [
            "",
            ".",
            "..",
            "../reviewer.md",
            "subdir/reviewer.md",
            "subdir\\reviewer.md",
            "/etc/passwd",
        ],
    )
    def test_load_template_rejects_unsafe_name(self, tmp_path: Path, bad_name: str):
        """Unsafe basenames raise ``ValueError`` rather than reaching
        into the filesystem. A template loader that accepts arbitrary
        paths could be tricked into reading anywhere on disk."""
        with pytest.raises(ValueError):
            load_template(tmp_path, bad_name)

    def test_load_template_override_resolution_synthesizer(self, tmp_path: Path):
        """Per-repo override at
        ``.syncade/templates/synthesizer.md`` wins over the packaged
        default — same rule as the reviewer template."""
        override_dir = tmp_path / ".syncade" / "templates"
        override_dir.mkdir(parents=True)
        custom = "CUSTOM SYNTH: pr={pr_doc_path}\n"
        (override_dir / "synthesizer.md").write_text(custom)

        loaded = load_synthesizer_template(tmp_path)
        assert loaded == custom

    def test_load_synthesizer_template_wrapper_matches_loader(self, tmp_path: Path):
        """The ``load_synthesizer_template`` wrapper and
        ``load_template(p, "synthesizer.md")`` agree — the wrapper is
        a thin delegate, just like the reviewer wrapper."""
        from_param = load_template(tmp_path, "synthesizer.md")
        from_wrapper = load_synthesizer_template(tmp_path)
        assert from_param == from_wrapper

    def test_load_template_missing_raises(self, tmp_path: Path):
        """A request for a template that isn't packaged and has no
        override raises so the caller doesn't silently get an empty
        string."""
        with pytest.raises((FileNotFoundError, OSError)):
            load_template(tmp_path, "no-such-template.md")

    def test_load_template_rejects_symlink_escape(self, tmp_path: Path):
        """QA fix #15 (P1.10): an override file that's a symlink
        pointing OUTSIDE the template directory is refused. Without
        this guard, someone (or some misconfig) writing to
        ``.syncade/templates/reviewer.md`` could symlink it to
        ``/etc/passwd`` or any other file the syncade process can
        read — load_template would happily return that content as
        the "template" and feed it into the reviewer prompt.
        """
        # Set up a target file OUTSIDE the template directory.
        target_dir = tmp_path / "outside"
        target_dir.mkdir()
        target_file = target_dir / "leak.txt"
        target_file.write_text("PRETEND THIS IS /etc/passwd\n")

        # Plant a symlink inside the template dir pointing at the
        # external file.
        template_dir = tmp_path / ".syncade" / "templates"
        template_dir.mkdir(parents=True)
        symlink_path = template_dir / "reviewer.md"
        symlink_path.symlink_to(target_file)

        # is_file() returns True for the symlink (follows it).
        assert symlink_path.is_file()
        # But load_template must refuse it.
        with pytest.raises(ValueError) as exc_info:
            load_template(tmp_path, "reviewer.md")
        message = str(exc_info.value)
        assert "outside the template directory" in message
        assert "Symlinks that escape" in message

    def test_load_template_rejects_symlinked_templates_dir(self, tmp_path: Path):
        """R2.3: P1.10's file-level containment check is BYPASSABLE
        when ``.syncade/templates`` is itself a symlink to an outside
        directory. With a symlinked templates dir, the
        ``relative_to(template_dir.resolve())`` check passed because
        both the override and the template dir resolved to the same
        outside location.

        Fix: parent-dir guard. Reject when
        ``<repo>/.syncade/templates`` is a symlink, regardless of
        where it points. Operator with a legitimate need for a
        symlinked-templates-dir (rare) should make the parent a
        real dir and link the individual template file.
        """
        # Set up an outside dir with a real reviewer.md.
        outside_dir = tmp_path / "elsewhere"
        outside_dir.mkdir()
        (outside_dir / "reviewer.md").write_text("PRETEND THIS IS /etc/...\n")

        # Plant .syncade/ as a real dir, but templates/ as a symlink
        # to the outside dir.
        (tmp_path / ".syncade").mkdir()
        (tmp_path / ".syncade" / "templates").symlink_to(outside_dir)

        # is_file() on the override returns True (the symlink chain
        # resolves to the real file), but our new guard catches the
        # symlinked PARENT.
        assert (tmp_path / ".syncade" / "templates" / "reviewer.md").is_file()
        with pytest.raises(ValueError) as exc_info:
            load_template(tmp_path, "reviewer.md")
        message = str(exc_info.value)
        assert "symlinked parent directory" in message
        # The error names the symlinked parent specifically.
        assert "templates" in message

    def test_load_template_rejects_symlinked_syncade_dir(self, tmp_path: Path):
        """R2.3: same regression at the ``.syncade`` parent level —
        if ``.syncade`` itself is a symlink to an outside dir
        containing ``templates/reviewer.md``, the file-level check
        would pass. Reject."""
        outside_syncade = tmp_path / "outside-syncade"
        outside_syncade.mkdir()
        (outside_syncade / "templates").mkdir()
        (outside_syncade / "templates" / "reviewer.md").write_text("LEAK\n")

        # Plant .syncade as a symlink to outside-syncade.
        (tmp_path / ".syncade").symlink_to(outside_syncade)

        with pytest.raises(ValueError) as exc_info:
            load_template(tmp_path, "reviewer.md")
        message = str(exc_info.value)
        assert "symlinked parent directory" in message
        assert ".syncade" in message

    def test_load_template_accepts_symlink_inside_dir(self, tmp_path: Path):
        """A symlink that resolves to a real file INSIDE the
        template directory is allowed — e.g. a user might
        symlink reviewer.md → reviewer-v2.md within
        ``.syncade/templates/`` as part of a versioning scheme.
        Only ESCAPES are refused."""
        template_dir = tmp_path / ".syncade" / "templates"
        template_dir.mkdir(parents=True)
        # Real file inside the template dir.
        real = template_dir / "reviewer-v2.md"
        real.write_text("CONTENT FROM REAL FILE\n")
        # Symlink pointing at the real file, still inside the
        # template dir.
        symlink = template_dir / "reviewer.md"
        symlink.symlink_to(real)

        content = load_template(tmp_path, "reviewer.md")
        assert content == "CONTENT FROM REAL FILE\n"


class TestPackagedSynthesizerTemplate:
    """The bundled ``synthesizer.md`` must encode the design's
    invariants in instruction form — these are belt-and-braces with
    the schema validators, but the prompt is the model's primary
    teacher."""

    def test_documents_cold_inputs(self, tmp_path: Path):
        template = load_synthesizer_template(tmp_path)
        lower = template.lower()
        # The synthesizer's inputs are reviewer outputs only — no diff,
        # no test output, no producer narrative. The template must say
        # this explicitly so the model doesn't go looking for context.
        assert "do not see the diff" in lower or "do not receive" in lower

    def test_documents_cannot_invent_findings(self, tmp_path: Path):
        template = load_synthesizer_template(tmp_path)
        lower = template.lower()
        assert "do not invent" in lower
        assert "provenance" in lower

    def test_documents_no_verdict_field(self, tmp_path: Path):
        template = load_synthesizer_template(tmp_path)
        lower = template.lower()
        # Critical invariant: the synthesizer does not emit a verdict.
        # If the prompt is unclear on this, the model will helpfully
        # add one. Allow backticks around the field name (the template
        # writes `verdict` to flag it as a schema field name).
        assert "do not emit a `verdict`" in lower or "no `verdict` field" in lower

    def test_documents_unanimous_blocker_rule(self, tmp_path: Path):
        template = load_synthesizer_template(tmp_path)
        lower = template.lower()
        # The rule is schema-enforced but the prompt should also state
        # it — the model gets fewer rejected outputs when it knows the
        # rule up-front than when it learns by failed validation.
        assert "unanimous" in lower or "both reviewers" in lower
        assert "blocker" in lower

    def test_requires_fenced_json_output(self, tmp_path: Path):
        template = load_synthesizer_template(tmp_path)
        assert "```json" in template
        lower = template.lower()
        assert "fence" in lower

    def test_documents_dismissal_rationale_requirement(self, tmp_path: Path):
        template = load_synthesizer_template(tmp_path)
        lower = template.lower()
        # dismissal_rationale appears in the schema and the body
        assert "dismissal_rationale" in template
        assert "rationale" in lower

    def test_documents_severity_change_rationale_requirement(self, tmp_path: Path):
        template = load_synthesizer_template(tmp_path)
        assert "severity_change_rationale" in template

    def test_documents_root_cause_clustering(self, tmp_path: Path):
        """PR-19: the clustering section must teach group-and-quote-only —
        no authored cause, no prescribed fix, verbatim quotes, shared file,
        advisory (never changes the verdict)."""
        template = load_synthesizer_template(tmp_path)
        lower = template.lower()
        assert "root_cause_clusters" in template
        assert "clustering" in lower
        # Cannot-invent: no authored cause, no prescribed fix.
        assert "do not author a cause" in lower
        assert "do not prescribe a fix" in lower
        # Verbatim grounding, not paraphrase.
        assert "do not paraphrase" in lower or "verbatim" in lower
        # Shared-file fence + advisory framing.
        assert "anchor_file" in template
        assert "never changes the verdict" in lower or "advisory" in lower

    def test_real_template_with_clustering_renders(self, tmp_path: Path):
        """The packaged template (now carrying the clustering section) still
        renders via format_map — the clustering instruction adds NO new
        placeholder, so the cold synth input is unchanged. The schema's
        cluster block flows through too."""
        template = load_synthesizer_template(tmp_path)
        rendered = render_synthesizer_prompt(
            template,
            pr_doc_path="path/to/pr.md",
            reviewer_outputs_json='{"verdict": "NO-SHIP"}',
            master_plan_path=None,
            json_schema=get_synthesizer_schema_string(),
        )
        assert "path/to/pr.md" in rendered
        assert "root_cause_clusters" in rendered  # both prose + schema
        assert "Do NOT author a cause" in rendered
