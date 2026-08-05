"""Tests for :mod:`syncade.findings` — the reviewer-output schema and
parser. Schema must match PRD Appendix B exactly; the parser must be
robust to the JSON-in-markdown-fences shape we documented in
the CLI-format notes as the actual ``claude -p`` output style.
"""

from __future__ import annotations

import pytest

from syncade.findings import (
    Finding,
    ReviewerOutput,
    ReviewerOutputError,
    get_findings_schema_string,
    parse_reviewer_output,
)
from tests.findings._helpers import _verdict_json


class TestParser:
    def test_bare_json_parses(self):
        raw = _verdict_json("SHIP")
        out = parse_reviewer_output(raw)
        assert out.verdict == "SHIP"
        assert out.findings == []

    def test_markdown_fenced_json_parses(self):
        # The exact shape `claude -p` returns even when told not to use
        # markdown fences (verified in the CLI-format notes).
        raw = "```json\n" + _verdict_json("NO-SHIP") + "\n```"
        out = parse_reviewer_output(raw)
        assert out.verdict == "NO-SHIP"

    def test_json_embedded_in_prose_parses(self):
        raw = (
            "Here are my findings after a thorough review:\n\n"
            + _verdict_json("SHIP")
            + "\n\nLet me know if anything else."
        )
        out = parse_reviewer_output(raw)
        assert out.verdict == "SHIP"

    def test_json_with_nested_braces_in_strings_parses(self):
        # A finding text containing literal braces ("...{...}...")
        # must not throw off the brace-depth scan.
        raw = _verdict_json(
            "NO-SHIP",
            findings=[
                {
                    "severity": "minor",
                    "file": "a.py",
                    "spec_clause": "G1",
                    "finding": "code uses {literal} brace tokens that fail to render",
                }
            ],
        )
        out = parse_reviewer_output(raw)
        assert len(out.findings) == 1
        assert "{literal}" in out.findings[0].finding

    def test_findings_with_optional_fields_omitted_parses(self):
        raw = _verdict_json(
            "NO-SHIP",
            findings=[
                {
                    "severity": "blocker",
                    "file": "x.py",
                    "spec_clause": "G2",
                    "finding": "blah",
                }
            ],
        )
        out = parse_reviewer_output(raw)
        assert out.findings[0].line is None
        assert out.findings[0].evidence_cmd is None

    def test_missing_required_field_raises_with_snippet(self):
        # The block parses as JSON but fails ReviewerOutput validation
        # (missing verdict, summary, etc.). With the "must validate
        # against ReviewerOutput" discriminator, a partial-JSON block
        # is not the verdict — there are no other candidates here, so
        # the parser raises. Same surface PR-5.6 pinned, just more
        # required fields to be missing as of PR-6.
        raw = '{"findings": []}'  # missing verdict (and PR-6 narrative fields)
        with pytest.raises(ReviewerOutputError) as exc_info:
            parse_reviewer_output(raw)
        msg = str(exc_info.value)
        assert ".stdout" in msg
        # PR-h-01: the diagnostic names the SELECTED block and states that
        # nothing earlier was tried, rather than a candidate-attempt count.
        assert "schema error" in msg.lower()
        assert "no earlier block is considered" in msg

    def test_unknown_field_at_top_level_raises(self):
        raw = _verdict_json("SHIP", extras={"extra": "nope"})
        with pytest.raises(ReviewerOutputError):
            parse_reviewer_output(raw)

    def test_unknown_field_inside_finding_raises(self):
        raw = _verdict_json(
            "NO-SHIP",
            findings=[
                {
                    "severity": "blocker",
                    "file": "x.py",
                    "spec_clause": "G1",
                    "finding": "...",
                    "bogus": "field",
                }
            ],
        )
        with pytest.raises(ReviewerOutputError):
            parse_reviewer_output(raw)

    def test_invalid_severity_in_finding_raises(self):
        raw = _verdict_json(
            "NO-SHIP",
            findings=[
                {
                    "severity": "critical",
                    "file": "x.py",
                    "spec_clause": "G1",
                    "finding": "...",
                }
            ],
        )
        with pytest.raises(ReviewerOutputError):
            parse_reviewer_output(raw)

    def test_whitespace_only_summary_in_parsed_output_raises(self):
        """A reviewer that emits otherwise-valid JSON with a
        whitespace-only ``summary`` fails the field_validator inside
        :meth:`ReviewerOutput.model_validate`. The parser's two-step
        discriminator (``json.loads`` + ``model_validate``) treats this
        the same as any other validation failure: it skips the
        candidate and, with no other valid block in the response,
        raises :class:`ReviewerOutputError` → exit 70."""
        raw = _verdict_json("SHIP", summary="   ")
        with pytest.raises(ReviewerOutputError) as exc_info:
            parse_reviewer_output(raw)
        msg = str(exc_info.value)
        assert ".stdout" in msg

    def test_completely_unparseable_input_raises_with_snippet(self):
        raw = "this is not json and never will be"
        with pytest.raises(ReviewerOutputError) as exc_info:
            parse_reviewer_output(raw)
        msg = str(exc_info.value)
        # Per PR-5.6: error message includes attempt counts + a hint
        # pointing the user at the reviewer's .stdout file.
        assert "no parseable" in msg.lower() or "no JSON" in msg.lower()
        assert ".stdout" in msg

    def test_unbalanced_braces_raises(self):
        # An unclosed `{` produces no completed top-level brace blocks,
        # so the parser ends with zero candidates and raises. PR-6's
        # narrative fields don't change this — the input is structurally
        # broken before validation could care about which fields are
        # required.
        raw = '{"verdict": "SHIP", "findings": ['
        with pytest.raises(ReviewerOutputError) as exc_info:
            parse_reviewer_output(raw)
        assert ".stdout" in str(exc_info.value)

    def test_malformed_json_after_extraction_raises(self):
        # A balanced {} pair but not valid JSON inside — one candidate
        # block, fails json.loads, no other candidates, raises.
        raw = "{this is not valid json}"
        with pytest.raises(ReviewerOutputError) as exc_info:
            parse_reviewer_output(raw)
        msg = str(exc_info.value)
        assert "no earlier block is considered" in msg
        assert "this is not valid json" in msg  # the selected block is quoted
        assert ".stdout" in msg

    def test_snippet_is_truncated_for_giant_inputs(self):
        # The error message should not include unbounded raw input
        raw = "x" * 10_000  # no { at all
        with pytest.raises(ReviewerOutputError) as exc_info:
            parse_reviewer_output(raw)
        # Confirm we didn't dump the entire 10k-char input into the
        # exception message
        assert len(str(exc_info.value)) < 1000


class TestPr6NarrativeFields:
    """PR-6: ``ReviewerOutput`` grew four required narrative-surface
    fields — ``summary``, ``priority_order``, ``coverage_gaps``,
    ``dismissed_concerns``. Each is required so the reviewer must
    consciously assert the answer (gaps and dismissals can be ``[]``,
    but the omission is no longer silent). The ``priority_order``
    permutation validator is strict on purpose — partial orderings
    are harder to consume downstream and easier to get wrong silently.
    """

    def _ro(self, **overrides):
        """Build a valid :class:`ReviewerOutput` with sensible
        PR-6-compliant defaults so each test only asserts on what it
        cares about."""
        body = {
            "verdict": "SHIP",
            "findings": [],
            "summary": "verified the trivial diff",
            "priority_order": [],
            "coverage_gaps": [],
            "dismissed_concerns": [],
        }
        body.update(overrides)
        return ReviewerOutput(**body)

    @staticmethod
    def _finding(**overrides) -> Finding:
        body = {
            "severity": "blocker",
            "file": "src/x.py",
            "spec_clause": "G1",
            "finding": "missing thing",
        }
        body.update(overrides)
        return Finding(**body)

    def test_minimal_ship_with_no_findings_validates(self):
        ro = self._ro()
        assert ro.summary == "verified the trivial diff"
        assert ro.priority_order == []
        assert ro.coverage_gaps == []
        assert ro.dismissed_concerns == []

    def test_populated_gaps_and_dismissals_are_preserved(self):
        ro = self._ro(
            coverage_gaps=["could not reach staging DB", "no playwright on mobile"],
            dismissed_concerns=["considered: types/index.ts retains old type — spec exempts"],
        )
        assert len(ro.coverage_gaps) == 2
        assert len(ro.dismissed_concerns) == 1

    def test_no_ship_with_three_findings_and_explicit_priority_validates(self):
        findings = [
            self._finding(spec_clause="G1"),
            self._finding(severity="minor", spec_clause="G2"),
            self._finding(severity="nit", spec_clause="G3"),
        ]
        # An out-of-input-order permutation — the reviewer's judgment
        # call about which to fix first.
        ro = self._ro(verdict="NO-SHIP", findings=findings, priority_order=[2, 0, 1])
        assert ro.priority_order == [2, 0, 1]
        assert len(ro.findings) == 3

    def test_empty_summary_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            self._ro(summary="")

    def test_whitespace_only_summary_rejected(self):
        """``Field(min_length=1)`` alone would accept ``'   '`` — but a
        whitespace-only summary carries no verification narrative and
        defeats the whole purpose of the field. The field_validator
        rejects it with a message naming the issue."""
        from pydantic import ValidationError

        for blank in ("   ", "\t\t", "\n\n", " \t \n ", "  "):  # incl NBSP
            with pytest.raises(ValidationError) as exc_info:
                self._ro(summary=blank)
            # The error must clearly name summary as the offender so a
            # reviewer who hits this knows exactly which field to fix.
            assert "summary" in str(exc_info.value), (
                f"validation error for blank={blank!r} must name `summary`: {exc_info.value}"
            )

    def test_summary_with_leading_trailing_whitespace_accepted(self):
        """A summary that's mostly whitespace but contains some
        narrative content is valid — only PURE whitespace is rejected.
        The reviewer's `'  I verified X  '` is allowed; we don't try to
        normalize content."""
        ro = self._ro(summary="  I verified the trivial diff.  ")
        # Preserved verbatim — no auto-trim, no normalization.
        assert ro.summary == "  I verified the trivial diff.  "

    def test_priority_order_wrong_length_rejected_with_field_name(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            self._ro(findings=[self._finding()], priority_order=[])
        assert "priority_order" in str(exc_info.value)

    def test_priority_order_with_duplicates_rejected(self):
        from pydantic import ValidationError

        findings = [self._finding(spec_clause=f"G{i}") for i in range(3)]
        with pytest.raises(ValidationError) as exc_info:
            # 0,0,1 has the right length but duplicates 0 and skips 2.
            self._ro(verdict="NO-SHIP", findings=findings, priority_order=[0, 0, 1])
        assert "priority_order" in str(exc_info.value)

    def test_priority_order_with_out_of_range_index_rejected(self):
        from pydantic import ValidationError

        findings = [self._finding(spec_clause=f"G{i}") for i in range(2)]
        with pytest.raises(ValidationError) as exc_info:
            # Index 5 doesn't refer to any finding; len=2 means valid
            # indices are 0, 1.
            self._ro(verdict="NO-SHIP", findings=findings, priority_order=[0, 5])
        assert "priority_order" in str(exc_info.value)

    def test_priority_order_must_be_empty_when_findings_empty(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            # An ordering against zero findings still has to be the
            # empty permutation — the validator treats this as a
            # duplicate-or-out-of-range case.
            self._ro(priority_order=[0])
        assert "priority_order" in str(exc_info.value)

    def test_coverage_gaps_required_field_omitted_rejected(self):
        from pydantic import ValidationError

        # The model is constructed via direct kwargs; omitting
        # coverage_gaps must surface as a missing-field error, not
        # silently fall back to a default. This test pins the
        # required-field contract so a future "make it default to []"
        # change fails loudly.
        with pytest.raises(ValidationError):
            ReviewerOutput(
                verdict="SHIP",
                findings=[],
                summary="ok",
                priority_order=[],
                # coverage_gaps intentionally omitted
                dismissed_concerns=[],
            )

    def test_dismissed_concerns_required_field_omitted_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ReviewerOutput(
                verdict="SHIP",
                findings=[],
                summary="ok",
                priority_order=[],
                coverage_gaps=[],
                # dismissed_concerns intentionally omitted
            )

    def test_summary_required_field_omitted_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ReviewerOutput(
                verdict="SHIP",
                findings=[],
                # summary intentionally omitted
                priority_order=[],
                coverage_gaps=[],
                dismissed_concerns=[],
            )

    def test_priority_order_required_field_omitted_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ReviewerOutput(
                verdict="SHIP",
                findings=[],
                summary="ok",
                # priority_order intentionally omitted
                coverage_gaps=[],
                dismissed_concerns=[],
            )

    def test_serializes_cleanly_via_model_dump_json(self):
        ro = self._ro(
            verdict="NO-SHIP",
            findings=[self._finding()],
            priority_order=[0],
            coverage_gaps=["did not exercise the staging DB"],
            dismissed_concerns=["considered the types file — exempt by spec"],
        )
        text = ro.model_dump_json(indent=2)
        # Round-trip assertion (the strongest possible "schema serializes
        # cleanly" check): the dumped JSON re-validates to an equal model.
        round_tripped = ReviewerOutput.model_validate_json(text)
        assert round_tripped == ro
        # And the new fields appear in the JSON output (not hidden by
        # exclude rules or alias mismatches).
        for name in ("summary", "priority_order", "coverage_gaps", "dismissed_concerns"):
            assert f'"{name}"' in text, f"new field {name!r} missing from model_dump_json"


class TestSchemaString:
    """get_findings_schema_string is the single source of truth for
    the JSON schema text embedded in the reviewer prompt. Tests assert
    its surface (every Finding/ReviewerOutput field name is present)
    rather than the exact byte layout — that lets the docstring
    formatting evolve without breaking these tests."""

    def test_schema_string_is_stable_non_empty(self):
        s = get_findings_schema_string()
        assert isinstance(s, str)
        assert s  # non-empty
        # Idempotent: two calls return identical strings.
        assert get_findings_schema_string() == s

    def test_schema_string_names_every_reviewer_output_field(self):
        s = get_findings_schema_string()
        # PR-6 expanded ReviewerOutput from {verdict, findings} to also
        # include the four narrative-surface fields. The reviewer prompt
        # template substitutes this schema into the {json_schema}
        # placeholder, so every required field name has to appear here
        # or reviewers won't know to populate it.
        for name in (
            "verdict",
            "findings",
            "summary",
            "priority_order",
            "coverage_gaps",
            "dismissed_concerns",
        ):
            assert name in s, f"schema missing ReviewerOutput field {name!r}"

    def test_schema_string_documents_priority_order_permutation_rule(self):
        # The brief calls out the permutation contract — the schema
        # string must mention it so the reviewer doesn't need to
        # re-discover the rule from validator errors.
        s = get_findings_schema_string()
        assert "permutation" in s.lower(), (
            "schema string must document the priority_order permutation rule"
        )

    def test_schema_string_documents_required_empty_ok_semantics(self):
        # coverage_gaps and dismissed_concerns are required but can be
        # `[]`. The schema string flags this so reviewers don't omit
        # them when they have nothing to flag.
        s = get_findings_schema_string()
        assert "required" in s.lower()
        # The "[] if none" / "non-empty" notation tells the reviewer
        # which empty values are valid.
        assert "[] if none" in s or "non-empty" in s

    def test_schema_string_names_every_finding_field(self):
        s = get_findings_schema_string()
        for name in (
            "severity",
            "file",
            "line",
            "spec_clause",
            "finding",
            "evidence_cmd",
            "evidence_output",
        ):
            assert name in s, f"schema missing Finding field {name!r}"

    def test_schema_string_names_verdict_values(self):
        s = get_findings_schema_string()
        assert "SHIP" in s
        assert "NO-SHIP" in s

    def test_schema_string_names_severity_values(self):
        s = get_findings_schema_string()
        for sev in ("blocker", "minor", "nit"):
            assert sev in s, f"schema missing severity {sev!r}"
