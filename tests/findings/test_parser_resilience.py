"""Tests for :mod:`syncade.findings` — the reviewer-output schema and
parser. Schema must match PRD Appendix B exactly; the parser must be
robust to the JSON-in-markdown-fences shape we documented in
the CLI-format notes as the actual ``claude -p`` output style.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncade.findings import (
    Finding,
    ReviewerOutput,
    ReviewerOutputError,
    parse_reviewer_output,
)
from tests.findings._helpers import _verdict_json


class TestSchema:
    def test_finding_minimal_required_fields(self):
        f = Finding(
            severity="blocker",
            file="src/x.py",
            spec_clause="G2",
            finding="something wrong",
        )
        assert f.line is None
        assert f.evidence_cmd is None
        assert f.evidence_output is None

    def test_finding_all_fields(self):
        f = Finding(
            severity="minor",
            file="src/y.py",
            line=142,
            spec_clause="G3",
            finding="another problem",
            evidence_cmd="pytest -xvs tests/test_y.py",
            evidence_output="FAILED: AssertionError",
        )
        assert f.line == 142

    def test_finding_unknown_field_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Finding(
                severity="blocker",
                file="x.py",
                spec_clause="G1",
                finding="...",
                surprise_field="not allowed",  # type: ignore[call-arg]
            )

    def test_finding_invalid_severity_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Finding(
                severity="critical",  # type: ignore[arg-type]
                file="x.py",
                spec_clause="G1",
                finding="...",
            )

    def test_reviewer_output_defaults_to_empty_findings(self):
        # The PR-6 narrative-surface fields are required; ``findings``
        # still defaults to an empty list.
        ro = ReviewerOutput(
            verdict="SHIP",
            summary="ok",
            priority_order=[],
            coverage_gaps=[],
            dismissed_concerns=[],
        )
        assert ro.findings == []

    def test_reviewer_output_invalid_verdict_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ReviewerOutput(  # type: ignore[arg-type]
                verdict="MAYBE",
                summary="x",
                priority_order=[],
                coverage_gaps=[],
                dismissed_concerns=[],
            )

    def test_finding_file_is_optional_for_repo_wide_findings(self):
        # PR-5.6: real reviewers (Acme 2026-05-15 run) emit `file:
        # null` for repo-wide concerns — commit message, file-tree
        # hygiene, etc. Schema must accept these or legitimate blocker
        # findings get rejected at parse time.
        f = Finding(
            severity="blocker",
            file=None,
            spec_clause="commit hygiene",
            finding="commit bundles 7,378 unrelated files",
        )
        assert f.file is None


class TestParserResilience:
    """PR-5.6 regression suite: tests for parse_reviewer_output's
    robustness against JSON-shaped fragments in reviewer prose.

    The Acme run (2026-05-15T08-44-26) surfaced a parser failure
    where claude's narrative explanation of a JSX style snippet —
    ``style={{ color: 'var(--mm-amber)' }}`` — was greedily matched as
    the "first JSON block" by the old first-`{`-only extractor, and
    exploded on json.loads. The actual NO-SHIP verdict at the end of
    the response was lost.

    The PR-5.6 parser contract (post-review-round-2):

    - Reviewers are instructed to wrap the final verdict in a
      ``` ```json ``` fence and avoid JSON-looking fragments elsewhere.
    - Parser behavior is defensive: it collects every fenced
      ``json``/unlabeled block AND every balanced top-level
      ``{...}`` block as ``(start_pos, content)`` candidates, sorts
      by start_pos descending, and returns the LATEST candidate that
      validates as a ``ReviewerOutput``. Whole-string fallback as a
      final defensive attempt.
    - Therefore the verdict JSON should be the last JSON-like block
      in the response — fenced or bare; whichever appears later
      wins. An earlier fenced example does NOT mask a later real
      verdict (review fix P1.4).
    - The discriminator at every candidate is twofold: it must parse
      as JSON AND validate against ``ReviewerOutput``. JSX braces
      fail both, schema illustrations fail validation, the verdict
      block passes both — the latest validating one wins.
    """

    _FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "pr-5.6-parser-regression"
    _ACME_FIXTURE = _FIXTURE_DIR / "claude-reviewer-prose-with-jsx.stdout"

    def test_acme_regression_jsx_prose_with_real_verdict_at_end(self):
        """Canonical regression case: real claude stdout from the
        Acme 2026-05-15T08-44-26 run. The narrative contains
        ``style={{ color: 'var(--mm-amber)' }}`` which the old
        first-`{`-only parser greedy-matched and exploded on. The
        actual NO-SHIP verdict with 8 findings is at the end of the
        response; the new parser must extract it correctly.

        The fixture is the claude `-p` JSON envelope; ``parse_reviewer_output``
        receives the unwrapped ``result`` field — that's what we feed it.
        PR-6 backfilled ``summary`` / ``priority_order`` / ``coverage_gaps``
        / ``dismissed_concerns`` into the verdict so the post-PR-6
        :class:`ReviewerOutput` schema accepts it; the JSX trap and the
        verdict's position at the end of the document are unchanged —
        those are what this test actually pins.
        """
        envelope = json.loads(self._ACME_FIXTURE.read_text())
        result_text = envelope["result"]

        # Sanity-check the fixture itself: the JSX trap and the real
        # verdict are both present, and the JSX comes first in the
        # text. If this ever stops being true, the fixture has drifted
        # from the regression case it's supposed to pin.
        jsx_idx = result_text.find("{{ color: 'var(--mm-amber)' }}")
        verdict_idx = result_text.find('{"verdict"')
        assert jsx_idx != -1, "fixture lost its JSX trap"
        assert verdict_idx != -1, "fixture lost its verdict block"
        assert jsx_idx < verdict_idx, "JSX must come before verdict in fixture"

        out = parse_reviewer_output(result_text)
        assert out.verdict == "NO-SHIP"
        # 4 blocker + 2 minor + 2 nit per the actual stdout
        assert len(out.findings) == 8
        blockers = [f for f in out.findings if f.severity == "blocker"]
        assert len(blockers) == 4

    def test_fenced_json_with_narrative_around_it(self):
        """Fenced JSON parses cleanly when surrounded by free-form
        narrative. With only one valid candidate (the fence content),
        the position-sorted scan returns it regardless of any
        non-validating brace fragments in the surrounding prose."""
        raw = (
            "Here is my analysis of the diff.\n\n"
            "Some prose with `{inline}` markers.\n\n"
            "```json\n" + _verdict_json("SHIP") + "\n```\n\n"
            "Let me know if you have questions."
        )
        out = parse_reviewer_output(raw)
        assert out.verdict == "SHIP"

    def test_unlabeled_fence_also_recognized(self):
        """The brief allows ``​``​``​`` ... ``​``​``​``
        (no language label) as well as the json-labeled form. Models
        sometimes drop the label."""
        raw = "Verdict below:\n\n```\n" + _verdict_json("SHIP") + "\n```\n"
        out = parse_reviewer_output(raw)
        assert out.verdict == "SHIP"

    def test_multiple_fences_last_one_wins(self):
        """When multiple JSON fences appear, the last one is the
        verdict — earlier fences are typically illustrative examples
        in the narrative. Last-to-first iteration finds the verdict
        without the parser having to understand the prose around them."""
        raw = (
            "Here's an example of what the schema looks like:\n\n"
            "```json\n" + _verdict_json("SHIP") + "\n```\n\n"
            "And now my actual verdict:\n\n"
            "```json\n" + _verdict_json("NO-SHIP") + "\n```\n"
        )
        out = parse_reviewer_output(raw)
        assert out.verdict == "NO-SHIP"

    def test_first_fence_invalid_second_fence_valid(self):
        """If the first fence's content fails ReviewerOutput
        validation, the parser falls through to the next candidate
        rather than raising on the first failure."""
        raw = (
            "```json\n"
            '{"not_a_verdict": "field"}\n'
            "```\n\n"
            "```json\n" + _verdict_json("SHIP") + "\n```\n"
        )
        out = parse_reviewer_output(raw)
        assert out.verdict == "SHIP"

    def test_inline_jsx_braces_then_real_json_at_end(self):
        """Synthetic minimal version of the Acme regression case —
        a single line of JSX-shaped prose followed by the verdict
        JSON at the end. Without the last-position-first scan, the
        JSX ``{{...}}`` would be the first balanced block and explode
        json.loads."""
        raw = (
            "I checked the component and the styling looks correct: "
            "`style={{ color: 'var(--mm-amber)' }}` — uses scoped var.\n\n"
            "Now the verdict:\n\n" + _verdict_json("SHIP")
        )
        # Document-order sanity: the JSX trap precedes the real verdict.
        # If this ever flips, the test loses the property it pins —
        # matches the Acme fixture test's discipline.
        jsx_idx = raw.find("{{ color: 'var(--mm-amber)' }}")
        verdict_idx = raw.rfind('{"verdict"')
        assert jsx_idx != -1 and verdict_idx != -1
        assert jsx_idx < verdict_idx, "synthetic JSX must come before verdict"

        out = parse_reviewer_output(raw)
        assert out.verdict == "SHIP"

    def test_pathological_many_brace_fragments_then_valid_json(self):
        """Defense against runaway scanning: 100 JSX-shaped fragments
        in prose, valid verdict at the end. The parser must terminate
        and return the verdict — no hangs, no exceptions, no
        accidental quadratic behavior."""
        prose_parts = [f"`style={{{{prop{i}: 'val{i}'}}}}`" for i in range(100)]
        raw = "\n".join(prose_parts) + "\n\n" + _verdict_json("SHIP")
        out = parse_reviewer_output(raw)
        assert out.verdict == "SHIP"

    def test_pathological_deeply_nested_braces_in_prose(self):
        """Defense against weird input: 50 levels of nested but
        garbage braces in narrative, valid verdict at the end. The
        nested fragment is a single balanced block that fails
        json.loads; the verdict block follows and validates."""
        deep = "{" * 50 + "}" * 50  # balanced but not JSON
        raw = f"Strange syntax in the model output: {deep}\n\n" + _verdict_json("SHIP")
        out = parse_reviewer_output(raw)
        assert out.verdict == "SHIP"

    def test_no_valid_json_anywhere_raises_with_useful_message(self):
        """When NO candidate block validates as a ReviewerOutput, the
        error message must include: (a) the count of fenced + brace
        candidates attempted, (b) a snippet of the first attempted
        candidate, (c) a hint pointing at the reviewer's .stdout file
        for the raw response."""
        raw = (
            "I tried to find issues but there is no JSON here.\n"
            "Just prose. With some {curly braces} and `code samples`."
        )
        with pytest.raises(ReviewerOutputError) as exc_info:
            parse_reviewer_output(raw)
        msg = str(exc_info.value)
        assert "attempted" in msg.lower()
        assert ".stdout" in msg

    def test_diagnostic_snippet_is_the_first_attempted_candidate(self):
        """Round-2 review fix: the diagnostic message says "first
        attempted snippet". Under the position-sorted strategy, the
        FIRST attempted candidate is the one with the LATEST start
        position (we sort descending and try in order). This test
        pins that the snippet text matches the latest-position
        candidate's content, not the earliest.

        Two JSON object candidates: an early one with content `EARLY`
        and a later one with content `LATER`. Neither validates as
        ReviewerOutput, so both fail and the parser raises. The error snippet
        must be from the LATER block (the one we tried first), not the earlier one.
        """
        raw = (
            'Early junk: {"not_a_verdict": "EARLY-MARKER"}\n\n'
            'Later junk: {"not_a_verdict": "LATER-MARKER"}'
        )
        with pytest.raises(ReviewerOutputError) as exc_info:
            parse_reviewer_output(raw)
        msg = str(exc_info.value)
        assert "LATER-MARKER" in msg, (
            "diagnostic snippet must be the first-attempted (latest-position) "
            f"candidate, got: {msg!r}"
        )
        assert "EARLY-MARKER" not in msg, (
            "diagnostic snippet must NOT be the earliest-position candidate "
            "— that's the LAST attempted, not the first"
        )

    def test_pure_whole_string_json_no_fence_no_narrative(self):
        """When the input is just bare JSON (no fence, no narrative)
        the raw-decode candidate scan finds it as the sole candidate and the
        position-sorted parser returns it. The whole-string
        fallback is also available defensively."""
        raw = _verdict_json("SHIP")
        out = parse_reviewer_output(raw)
        assert out.verdict == "SHIP"

    def test_whole_string_json_with_leading_trailing_whitespace(self):
        """Whole-string parse tolerates leading/trailing whitespace
        — claude often emits a trailing newline."""
        raw = "\n  " + _verdict_json("SHIP") + "\n  "
        out = parse_reviewer_output(raw)
        assert out.verdict == "SHIP"

    def test_fenced_example_earlier_does_not_beat_later_real_verdict(self):
        """PR-5.6 review fix (P1.4): position-sorted extraction must NOT
        return an EARLIER fenced ReviewerOutput-shaped example when a
        REAL verdict is later in the document. Latest position wins.

        Pre-fix: parse_reviewer_output collected fences and tried them
        last-to-first; if any fence validated, it returned without
        considering brace blocks. A schema example wrapped in
        ```json early in a reviewer's response would be returned as
        the verdict, hiding the real bare-JSON verdict at the end."""
        raw = (
            "Here's the schema I'll output to:\n\n"
            "```json\n" + _verdict_json("SHIP") + "\n```\n\n"
            "Now my actual verdict:\n\n" + _verdict_json("NO-SHIP")
        )
        # Sanity: the example fence comes before the real verdict.
        fence_idx = raw.find("```json")
        verdict_idx = raw.rfind('"verdict": "NO-SHIP"')
        assert fence_idx != -1 and verdict_idx != -1
        assert fence_idx < verdict_idx, "fixture: example must precede real verdict"

        out = parse_reviewer_output(raw)
        assert out.verdict == "NO-SHIP", (
            f"parser returned the early example fence {out.verdict!r} "
            "instead of the later real verdict"
        )

    def test_unmatched_brace_in_prose_before_valid_verdict(self):
        """PR-5.6 review fix (P1.5): an unmatched ``{`` in prose must
        not swallow the rest of the document and prevent a later
        valid verdict from being found.

        Pre-fix: the old object extractor's single depth counter went 0 -> 1
        on the unmatched prose ``{``, never returned to 0, and emitted
        zero candidate blocks even though the real verdict JSON was
        balanced and at the end."""
        raw = (
            "Looking at the diff: the code includes "
            "`if (x) { do something` without closing the brace. "
            "Then later:\n\n" + _verdict_json("NO-SHIP")
        )
        out = parse_reviewer_output(raw)
        assert out.verdict == "NO-SHIP"

    def test_unmatched_brace_in_prose_then_fenced_verdict(self):
        """Companion to the unmatched-brace test: when the verdict is
        fenced AFTER an unmatched prose ``{``, we still recover."""
        raw = (
            "Code has `pseudocode { unfinished` syntax.\n\n"
            "Verdict:\n\n"
            "```json\n" + _verdict_json("SHIP") + "\n```\n"
        )
        out = parse_reviewer_output(raw)
        assert out.verdict == "SHIP"

    def test_crlf_fence_is_recognized(self):
        """PR-5.6 review fix (P1.6): CRLF line endings inside fences
        should match the same as LF. Common when models echo back
        Windows-style content."""
        raw = "```json\r\n" + _verdict_json("SHIP") + "\r\n```"
        out = parse_reviewer_output(raw)
        assert out.verdict == "SHIP"

    def test_json5_fence_falls_through_to_raw_decode_scan(self):
        """PR-5.6 review fix (P1.6): a ```json5 (or other label) fence
        is not a JSON-labeled fence per our rules. Its content should
        still be findable via the raw-decode candidate scan, and an EARLIER ```json
        example should NOT win against the real verdict inside the
        json5 fence."""
        raw = (
            "Example schema:\n\n"
            "```json\n" + _verdict_json("SHIP") + "\n```\n\n"
            "Real verdict (json5-style trailing content):\n\n"
            "```json5\n" + _verdict_json("NO-SHIP") + "\n```\n"
        )
        out = parse_reviewer_output(raw)
        assert out.verdict == "NO-SHIP"

    def test_hyphenated_label_fence_falls_through(self):
        """A ```my-language fence: regex doesn't match (hyphen not in
        [a-zA-Z]), content found via the raw-decode candidate scan if valid."""
        raw = "```my-language\n" + _verdict_json("SHIP") + "\n```\n"
        out = parse_reviewer_output(raw)
        assert out.verdict == "SHIP"

    def test_python_fence_is_not_treated_as_json_candidate(self):
        """A ``​``​``​`` python (or other-language) fence
        is not a JSON-labeled fence — it should not be tried as a
        verdict candidate. Falls through to the raw-decode candidate scan, which
        finds the real verdict at the end."""
        raw = "```python\ndef foo():\n    return {'a': 1}\n```\n\n" + _verdict_json("SHIP")
        out = parse_reviewer_output(raw)
        assert out.verdict == "SHIP"
