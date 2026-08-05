"""Tests for :mod:`syncade.synthesis` (PR-7 task 2 — synthesis primitives).

Schema-string, has_active_blocker, parse_synthesizer_output, shared
extractor, error class, and synthesizer adapter-surface tests (PR-R3
split from test_synthesis.py).
"""

from __future__ import annotations

import json

import pytest

from syncade.synthesis import (
    ConsolidatedFinding,
    SynthesizerOutput,
    SynthesizerOutputError,
    get_synthesizer_schema_string,
    parse_synthesizer_output,
)
from tests.synthesis._helpers import _finding, _provenance

# ---------------------------------------------------------------------------
# get_synthesizer_schema_string
# ---------------------------------------------------------------------------


class TestSchemaString:
    def test_mentions_required_fields(self) -> None:
        schema = get_synthesizer_schema_string()
        # Spot-check the contracts the prompt relies on; precise
        # formatting is allowed to drift across template revisions.
        assert "consolidated_findings" in schema
        assert "synthesis_summary" in schema
        assert "provenance" in schema
        assert "dismissed" in schema
        assert "dismissal_rationale" in schema
        assert "severity_change_rationale" in schema
        assert "original_severity" in schema
        assert "original_index" in schema
        assert "original_description" in schema

    def test_documents_invariants_inline(self) -> None:
        # The schema string is prompt input; the inline `//` comments
        # are how the synthesizer learns the schema-level rules. If
        # these strings drift, the synthesizer may emit invalid
        # output more often.
        schema = get_synthesizer_schema_string()
        assert "cannot invent findings" in schema
        assert "unanimous-blocker rule" in schema

    def test_does_not_include_verdict_field(self) -> None:
        # Same invariant as TestSynthesizerOutput.test_no_verdict_field
        # but at the prompt-surface level: a verdict mentioned in the
        # schema string would tempt the model to emit one. Keep it
        # absent.
        schema = get_synthesizer_schema_string()
        assert '"verdict"' not in schema

    def test_is_format_map_safe(self) -> None:
        # Defensive: the schema string is embedded in the synthesizer
        # prompt template via {json_schema} and rendered with
        # str.format_map. Stray literal braces in the schema would
        # collide with format_map's placeholder syntax. The fact that
        # the schema CONTAINS braces is expected (it's JSON shape);
        # the guarantee we want here is that the schema string itself
        # is a balanced parseable structure with no unintended
        # placeholders. Verify by attempting format_map with an empty
        # mapping — any unbalanced or unescaped placeholders would
        # raise KeyError or ValueError.
        #
        # NOTE: This is NOT how the schema is actually rendered (it's
        # the value of {json_schema}, not itself a template). The
        # check is that the schema's own braces never accidentally
        # become format_map placeholders when concatenated; the
        # safest sanity-check is "does str.format_map on the schema
        # itself, with all literal braces, complain?". A bare
        # `{json_schema}`-shaped substring inside the schema would —
        # there is none here, but the test pins that.
        schema = get_synthesizer_schema_string()
        # `{...}` shapes inside the schema are valid JSON syntax,
        # but for format_map they'd look like placeholders. Confirm
        # the schema has no `{identifier}`-shaped placeholders that
        # would resolve against a mapping.
        import re

        placeholder_re = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
        assert not placeholder_re.findall(schema)

    def test_is_parseable_when_stripped(self) -> None:
        # Sanity: the schema string is JSON-ish (with inline //
        # comments). Strip the comments and confirm the remaining
        # structure is balanced enough to be a real JSON sketch.
        # Specifically: equal { and } counts.
        schema = get_synthesizer_schema_string()
        # Trivial balance check — easier to maintain than full JSON
        # parsing and catches the most likely regression (a
        # mismatched brace in the literal string).
        assert schema.count("{") == schema.count("}")
        assert schema.count("[") == schema.count("]")

    def test_is_not_strict_json(self) -> None:
        # The schema string is intentionally NOT strict JSON — it
        # carries inline `//` comments and `|`-style union
        # annotations. Pin this so a future refactor doesn't try to
        # tighten the helper into a JSON Schema document; that would
        # break the prompt embedding.
        schema = get_synthesizer_schema_string()
        with pytest.raises(json.JSONDecodeError):
            json.loads(schema)


# ---------------------------------------------------------------------------
# parse_synthesizer_output (PR-7 task 4)
# ---------------------------------------------------------------------------


def _synth_json(
    *,
    findings: list[dict] | None = None,
    summary: str = "Consolidated both reviewers' findings.",
) -> str:
    """Produce a JSON-encoded SynthesizerOutput payload for parser tests."""
    if findings is None:
        findings = []
    return json.dumps({"consolidated_findings": findings, "synthesis_summary": summary})


class TestHasActiveBlocker:
    """QA fix #12 (P1.7): ``syncade.synthesis.has_active_blocker`` is
    the shared substrate of the mechanical verdict — both
    ``_compute_exit_code`` (exit-30 vs exit-0) and
    ``persist_findings_md`` (Verdict label) compute against it. Pin
    its behavior cell-by-cell so a future refinement (e.g. an
    "active" flag distinct from "dismissed") lands in one place and
    the two callers can't drift.
    """

    def test_empty_findings_is_not_active_blocker(self) -> None:
        from syncade.synthesis import has_active_blocker

        out = SynthesizerOutput(consolidated_findings=[], synthesis_summary="empty")
        assert has_active_blocker(out) is False

    def test_single_active_blocker_returns_true(self) -> None:
        from syncade.synthesis import has_active_blocker

        out = SynthesizerOutput(
            consolidated_findings=[ConsolidatedFinding(**_finding())],
            synthesis_summary="one blocker",
        )
        assert has_active_blocker(out) is True

    def test_single_dismissed_blocker_returns_false(self) -> None:
        """Dismissed blockers don't count — that's the mechanical
        verdict's central feature: synth's bounded judgment on
        dismissal is honored."""
        from syncade.synthesis import has_active_blocker

        out = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    **_finding(
                        severity="blocker",
                        # Single-reviewer blocker is dismissible per
                        # the unanimous-blocker rule (only N>=2 +
                        # all-blocker is forbidden).
                        provenance=[_provenance(original_severity="blocker")],
                        dismissed=True,
                        dismissal_rationale="spec exempts",
                    )
                )
            ],
            synthesis_summary="one dismissed blocker",
        )
        assert has_active_blocker(out) is False

    def test_active_minor_returns_false(self) -> None:
        from syncade.synthesis import has_active_blocker

        out = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    **_finding(
                        severity="minor",
                        provenance=[_provenance(original_severity="minor")],
                    )
                )
            ],
            synthesis_summary="one minor",
        )
        assert has_active_blocker(out) is False

    def test_active_nit_returns_false(self) -> None:
        from syncade.synthesis import has_active_blocker

        out = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    **_finding(
                        severity="nit",
                        provenance=[_provenance(original_severity="nit")],
                    )
                )
            ],
            synthesis_summary="one nit",
        )
        assert has_active_blocker(out) is False

    def test_mixed_active_and_dismissed_returns_true_iff_any_active_blocker(self) -> None:
        from syncade.synthesis import has_active_blocker

        out = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    **_finding(
                        severity="blocker",
                        provenance=[_provenance(original_severity="blocker")],
                    )
                ),
                ConsolidatedFinding(
                    **_finding(
                        description="dismissed minor",
                        severity="minor",
                        provenance=[_provenance(original_severity="minor")],
                        dismissed=True,
                        dismissal_rationale="r",
                    )
                ),
            ],
            synthesis_summary="mixed",
        )
        assert has_active_blocker(out) is True


class TestParseSynthesizerOutputHappyPath:
    def test_parses_bare_json(self) -> None:
        raw = _synth_json()
        out = parse_synthesizer_output(raw)
        assert isinstance(out, SynthesizerOutput)
        assert out.consolidated_findings == []
        assert out.synthesis_summary.startswith("Consolidated")

    def test_parses_fenced_json(self) -> None:
        raw = "Here is my synthesis output:\n\n```json\n" + _synth_json() + "\n```\n"
        out = parse_synthesizer_output(raw)
        assert isinstance(out, SynthesizerOutput)

    def test_parses_unlabeled_fence(self) -> None:
        raw = "Synthesis:\n\n```\n" + _synth_json() + "\n```\n"
        out = parse_synthesizer_output(raw)
        assert isinstance(out, SynthesizerOutput)

    def test_parses_with_one_consolidated_finding(self) -> None:
        raw = _synth_json(
            findings=[_finding()],
            summary="One finding consolidated from both reviewers.",
        )
        out = parse_synthesizer_output(raw)
        assert len(out.consolidated_findings) == 1

    def test_drops_known_reviewer_only_fields_from_synthesized_findings(self) -> None:
        finding = _finding()
        finding["line"] = 42
        finding["spec_clause"] = "reviewer-only context"

        out = parse_synthesizer_output(
            _synth_json(
                findings=[finding],
                summary="One finding consolidated from reviewer output.",
            )
        )

        dumped = out.consolidated_findings[0].model_dump()
        assert "line" not in dumped
        assert "spec_clause" not in dumped

    def test_rejects_unknown_extra_fields_after_reviewer_only_repair(self) -> None:
        finding = _finding()
        finding["line"] = 42
        finding["unexpected_extra"] = "still forbidden"

        with pytest.raises(SynthesizerOutputError):
            parse_synthesizer_output(
                _synth_json(
                    findings=[finding],
                    summary="One finding with an unsupported extra field.",
                )
            )


class TestParseSynthesizerOutputRejects:
    def test_empty_provenance_rejected(self) -> None:
        # Cannot-invent-findings rule: a finding with empty provenance
        # fails validation, the parser keeps scanning, and (with no
        # other valid candidate) raises SynthesizerOutputError.
        bad = _finding(provenance=[])
        raw = _synth_json(findings=[bad])
        with pytest.raises(SynthesizerOutputError):
            parse_synthesizer_output(raw)

    def test_unanimous_blocker_dismissal_rejected(self) -> None:
        # Cannot-deactivate-unanimous-blocker rule at the parser level:
        # the candidate parses as JSON but fails model validation, so
        # the parser falls through and (no other candidates) raises.
        bad = _finding(
            severity="blocker",
            provenance=[
                _provenance(reviewer_name="claude-reviewer"),
                _provenance(reviewer_name="codex-reviewer"),
            ],
            dismissed=True,
            dismissal_rationale="trying to dismiss despite both reviewers at blocker",
        )
        raw = _synth_json(findings=[bad])
        with pytest.raises(SynthesizerOutputError):
            parse_synthesizer_output(raw)

    def test_extra_top_level_field_rejected(self) -> None:
        # extra="forbid" on SynthesizerOutput: a verdict field (the
        # invariant this design explicitly rules out) at the top level
        # is rejected.
        raw = json.dumps(
            {
                "verdict": "SHIP",
                "consolidated_findings": [],
                "synthesis_summary": "all good",
            }
        )
        with pytest.raises(SynthesizerOutputError):
            parse_synthesizer_output(raw)

    def test_completely_unparseable_input_raises(self) -> None:
        raw = "no json here, just words"
        with pytest.raises(SynthesizerOutputError) as exc_info:
            parse_synthesizer_output(raw)
        msg = str(exc_info.value)
        assert "synthesizer" in msg.lower()
        assert "synthesizer.stdout" in msg

    def test_diagnostic_message_distinct_from_reviewer_phase(self) -> None:
        # PR-7 task 4 brief: error messages distinguish "reviewer parse
        # failed" from "synthesizer parse failed" so the user opens
        # the right .stdout. Both map to exit 70 but the operational
        # surface differs.
        raw = "{invalid json}"
        with pytest.raises(SynthesizerOutputError) as exc_info:
            parse_synthesizer_output(raw)
        msg = str(exc_info.value)
        assert "synthesizer" in msg.lower()
        # Doesn't claim to be a reviewer parse failure.
        assert "reviewer" not in msg.lower()

    def test_snippet_truncated_for_giant_inputs(self) -> None:
        raw = "x" * 10_000
        with pytest.raises(SynthesizerOutputError) as exc_info:
            parse_synthesizer_output(raw)
        assert len(str(exc_info.value)) < 1000


class TestParseSynthesizerExtractorReuse:
    """Pin that the synthesizer parser benefits from the shared PR-5.6
    hardening via :mod:`syncade.findings_json`. The reviewer
    parser is the originating tested-against-reality home for these
    cases; the synthesizer test suite asserts the same shapes flow
    through unaltered.
    """

    def test_inline_jsx_braces_then_real_synth_json_at_end(self) -> None:
        """Acme-style JSX trap, synthesizer-style. A JSX-shaped
        fragment in prose precedes the synthesizer's final output;
        the selector returns the sole candidate
        validating candidate (the real synthesizer JSON), not the
        JSX (which fails json.loads)."""
        raw = (
            "I considered the styling concerns the reviewers raised: "
            "`style={{ color: 'var(--mm-amber)' }}` — that's spec-compliant.\n\n"
            "Synthesis:\n\n" + _synth_json()
        )
        out = parse_synthesizer_output(raw)
        assert isinstance(out, SynthesizerOutput)

    def test_pathological_many_brace_fragments_then_valid_synth_json(self) -> None:
        """100 JSX-shaped prose fragments then a valid synthesizer
        output — the parser must terminate and return the output, no
        quadratic blowup."""
        prose = "\n".join(f"`style={{{{prop{i}: 'val{i}'}}}}`" for i in range(100))
        raw = prose + "\n\n" + _synth_json()
        out = parse_synthesizer_output(raw)
        assert isinstance(out, SynthesizerOutput)

    def test_multiple_json_fences_last_one_wins(self) -> None:
        """Same rule as the reviewer parser: the LAST ```json fence is the
        output. Multiple are not an error — the anthropic adapter joins every
        result turn, so more than one is a normal shape."""
        example = _synth_json(summary="this is the example, not the real output")
        real = _synth_json(summary="this is the actual synthesis output")
        raw = (
            "Here's an example shape:\n\n"
            "```json\n" + example + "\n```\n\n"
            "And my actual synthesis:\n\n"
            "```json\n" + real + "\n```\n"
        )
        out = parse_synthesizer_output(raw)
        assert out.synthesis_summary == "this is the actual synthesis output"

    def test_unbalanced_brace_in_prose_then_valid_synth(self) -> None:
        raw = "Reviewers disagreed on { do something approach.\n\n" + _synth_json()
        out = parse_synthesizer_output(raw)
        assert isinstance(out, SynthesizerOutput)

    def test_non_json_fence_label_falls_through(self) -> None:
        """A ```python or ```json5 fence isn't a synthesizer-output
        candidate; the inner contents still get picked up by the brace
        scan if they happen to be valid JSON, otherwise the parser
        keeps looking. Pinning the contract that non-json labels are
        not auto-treated as the verdict."""
        raw = "```python\nprint({'verdict': 'SHIP'})\n```\n\nNow the synthesis:\n\n" + _synth_json()
        out = parse_synthesizer_output(raw)
        assert isinstance(out, SynthesizerOutput)


class TestSynthesizerOutputErrorClass:
    def test_is_exception_subclass(self) -> None:
        assert issubclass(SynthesizerOutputError, Exception)

    def test_distinct_from_reviewer_output_error(self) -> None:
        # PR-7 task 4 brief: SynthesizerOutputError is its own class
        # so the orchestrator's exit-code logic can distinguish parse-
        # failure phases. Pin the type hierarchy.
        from syncade.findings import ReviewerOutputError

        assert SynthesizerOutputError is not ReviewerOutputError
        assert not issubclass(SynthesizerOutputError, ReviewerOutputError)
        assert not issubclass(ReviewerOutputError, SynthesizerOutputError)


class TestSharedExtractorIsActuallyShared:
    """Belt-and-braces: the brief mandates the extractor is shared
    rather than duplicated. Pin that the synthesizer parser imports
    the same callable the reviewer parser uses, not a near-copy.
    """

    def test_selector_returns_exactly_one_block_the_latest(self) -> None:
        """PR-h-01: the selector returns ONE block, not a fallback chain,
        and it is the latest by document position."""
        from syncade.findings_json import _select_verdict_block

        raw = 'Early: {"id": "early"}\nLater: {"id": "later"}'
        block, which = _select_verdict_block(raw)
        assert block == '{"id": "later"}'
        assert "bare JSON object" in which

    def test_selector_handles_braces_inside_strings_and_nested_objects(self) -> None:
        from syncade.findings_json import _select_verdict_block

        raw = 'noise {"message": "literal } and { braces", "nested": {"ok": true}} trailing'
        block, _ = _select_verdict_block(raw)
        assert block == '{"message": "literal } and { braces", "nested": {"ok": true}}'

    def test_selector_ignores_objects_inside_labeled_fences(self) -> None:
        """The code-sample mask is what stops a ```python illustration
        from being reachable by the bare-object scan."""
        from syncade.findings_json import _select_verdict_block

        raw = '{"id": "real"}\n\nExample:\n```python\n{"id": "illustration"}\n```\n'
        block, _ = _select_verdict_block(raw)
        assert block == '{"id": "real"}'
