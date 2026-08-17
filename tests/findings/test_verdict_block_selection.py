"""PR-h-01 increment A — adversarial cases for verdict-block selection.

Every case here was reproduced as a false SHIP (or a lost verdict) against
`2ca8a74` before the fix. The property under test is narrow and absolute:
**exactly one block is the verdict, and if it fails, the parse fails.** No
scan backwards for something that validates.

See `path/to/pr.md` and the module docstring of
`syncade.findings_json`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncade.findings import ReviewerOutputError, parse_reviewer_output

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "pr-5.6-parser-regression"
    / "claude-reviewer-prose-with-jsx.stdout"
)


def _out(verdict: str, summary: str, findings: list[dict] | None = None, **extra: object) -> str:
    findings = findings or []
    body: dict = {
        "verdict": verdict,
        "summary": summary,
        "findings": findings,
        "priority_order": list(range(len(findings))),
        "coverage_gaps": [],
        "dismissed_concerns": [],
    }
    body.update(extra)
    return json.dumps(body)


_BLOCKER = {
    "severity": "blocker",
    "spec_clause": "§3.1",
    "finding": "unsanitized input reaches the auth query",
}


def _fence(body: str, label: str = "json") -> str:
    return f"```{label}\n{body}\n```"


class TestNoFallbackToAnEarlierBlock:
    """The core rule: an invalid intended-final verdict is exit 70, never a
    silent downgrade to an earlier block that happens to validate."""

    def test_invalid_final_fence_does_not_fall_back_to_earlier_ship(self) -> None:
        raw = (
            "The shape I'll emit:\n\n"
            + _fence(_out("SHIP", "example only"))
            + "\n\nMy real verdict:\n\n"
            # Schema-invalid but syntactically valid JSON. Uses a WRONG TYPE rather than an
            # extra key: since PR-h-field-05 an extra key is repaired, so it would no longer
            # make this block invalid and the test would pass for the wrong reason.
            + _fence(_out("NO-SHIP", "sql injection", [dict(_BLOCKER, line="not-an-int")]))
        )
        with pytest.raises(ReviewerOutputError) as exc:
            parse_reviewer_output(raw)
        assert "no earlier block is considered" in str(exc.value)

    def test_a_REPAIRED_final_fence_still_beats_an_earlier_ship(self) -> None:
        """The interaction PR-h-field-05 introduces, pinned deliberately.

        Repair makes a previously-rejected final block usable — so it must yield THAT block's
        verdict, never resurrect the earlier example SHIP. Getting this wrong would turn a
        recall improvement into the exact false-SHIP the no-fallback rule exists to prevent.
        """
        raw = (
            "The shape I'll emit:\n\n"
            + _fence(_out("SHIP", "example only"))
            + "\n\nMy real verdict:\n\n"
            + _fence(_out("NO-SHIP", "sql injection", [dict(_BLOCKER, bogus_key=1)]))
        )
        assert parse_reviewer_output(raw).verdict == "NO-SHIP"

    def test_malformed_final_fence_does_not_fall_back_to_earlier_ship(self) -> None:
        """Harder case than the above: the final fence is not even valid JSON,
        so a bare-object scan cannot find it. The earlier valid SHIP must
        still not win."""
        raw = (
            _fence(_out("SHIP", "example only"))
            + "\n\nMy real verdict:\n\n"
            + _fence('{"verdict": "NO-SHIP", "summary":')
        )
        with pytest.raises(ReviewerOutputError) as exc:
            parse_reviewer_output(raw)
        assert "no earlier block is considered" in str(exc.value)

    def test_KNOWN_RESIDUAL_malformed_trailing_bare_object_is_not_detectable(self) -> None:
        """Documented limit of the UNFENCED path, deliberately pinned.

        When the intended final verdict is bare (no fence) AND malformed, the
        bare-object scan cannot see it — malformed text is indistinguishable
        from prose containing braces — so an earlier valid object still wins.
        Closing this would mean failing on any brace that follows the verdict,
        which breaks ordinary trailing prose.

        This residual is exactly why every template mandates a ```json fence:
        on the fenced path the same case fails closed
        (`test_malformed_final_fence_does_not_fall_back_to_earlier_ship`).
        If a dogfood ever shows a real reviewer hitting this, the answer is
        the sentinel envelope (brief D4 option b), not a smarter scan.
        """
        raw = _out("SHIP", "example only") + "\n\nActually, my verdict:\n\n" + "{not valid json}"
        assert parse_reviewer_output(raw).verdict == "SHIP"


class TestDuplicateKeysAreRejected:
    """Increment B. ``json.loads`` resolves a repeated key last-wins, so a
    duplicated ``verdict`` flipped NO-SHIP to SHIP invisibly — pydantic only
    ever saw one key, so no downstream check could catch it."""

    def test_duplicate_verdict_key_is_rejected_not_last_wins(self) -> None:
        raw = (
            '{"verdict": "NO-SHIP", "verdict": "SHIP", "summary": "s", '
            '"priority_order": [], "coverage_gaps": [], "dismissed_concerns": [], '
            '"findings": []}'
        )
        with pytest.raises(ReviewerOutputError) as exc:
            parse_reviewer_output(raw)
        assert "duplicate JSON key" in str(exc.value)

    def test_duplicate_key_nested_inside_a_finding_is_rejected(self) -> None:
        """The hook applies at every object depth, so a duplicated `severity`
        inside a finding cannot downgrade a blocker either."""
        raw = (
            '{"verdict": "NO-SHIP", "summary": "s", "priority_order": [0], '
            '"coverage_gaps": [], "dismissed_concerns": [], "findings": ['
            '{"severity": "blocker", "severity": "nit", "spec_clause": "x", '
            '"finding": "y"}]}'
        )
        with pytest.raises(ReviewerOutputError) as exc:
            parse_reviewer_output(raw)
        assert "duplicate JSON key" in str(exc.value)

    def test_every_parser_shares_the_rejection(self) -> None:
        """The hook lives in the shared decoder, so the synth/auditor/drafter
        parsers cannot drift from the reviewer one."""
        from syncade.spec_audit_schema import (
            SpecAuditOutputError,
            parse_spec_audit_output,
        )
        from syncade.spec_draft import SpecDraftOutputError, parse_spec_draft_output
        from syncade.synthesis import SynthesizerOutputError, parse_synthesizer_output

        raw = '{"a": 1, "a": 2}'
        for parse, err in [
            (parse_synthesizer_output, SynthesizerOutputError),
            (parse_spec_audit_output, SpecAuditOutputError),
            (parse_spec_draft_output, SpecDraftOutputError),
        ]:
            with pytest.raises(err) as exc:
                parse(raw)
            assert "duplicate JSON key" in str(exc.value)


class TestLabeledFencesAreNeverVerdicts:
    """A labeled fence is an illustration. Its contents must be unreachable —
    including via the bare-object scan, which was the hole."""

    @pytest.mark.parametrize("label", ["python", "js", "json5", "text", "javascript"])
    def test_trailing_illustration_cannot_override_a_real_verdict(self, label: str) -> None:
        """The worst reproduced case, and the one needing no adversarial model:
        a reviewer emits its real NO-SHIP verdict, then helpfully shows what a
        passing run looks like. Pre-fix the illustration won on position."""
        raw = (
            _fence(_out("NO-SHIP", "real verdict", [_BLOCKER]))
            + "\n\nFor reference, a passing run looks like:\n\n"
            + _fence(_out("SHIP", "illustration"), label=label)
        )
        out = parse_reviewer_output(raw)
        assert out.verdict == "NO-SHIP"
        assert out.summary == "real verdict"

    @pytest.mark.parametrize("label", ["python", "js", "json5"])
    def test_trailing_illustration_cannot_override_a_bare_verdict(self, label: str) -> None:
        raw = (
            _out("NO-SHIP", "real verdict", [_BLOCKER])
            + "\n\nExample of a clean run:\n\n"
            + _fence(_out("SHIP", "illustration"), label=label)
        )
        assert parse_reviewer_output(raw).verdict == "NO-SHIP"

    def test_verdict_only_inside_a_labeled_fence_is_exit_70_not_a_guess(self) -> None:
        """Fail closed. A mislabeled verdict fence is an honest parse failure,
        not a silently-accepted code sample."""
        with pytest.raises(ReviewerOutputError):
            parse_reviewer_output(_fence(_out("SHIP", "mislabeled verdict"), label="python"))


class TestJsonFenceIsAuthoritative:
    """A ```json fence outranks everything else in the response. Deciding by
    POSITION instead let three separate things a reviewer wrote AFTER its
    verdict replace that verdict — see TestNothingAfterTheVerdictCanReplaceIt."""

    def test_KNOWN_RESIDUAL_json_example_beats_a_later_bare_verdict(self) -> None:
        """The accepted cost of rule 2, deliberately pinned.

        An actor that labels its EXAMPLE ```json and leaves its real verdict
        BARE loses to the example. That inverts every template instruction and
        cannot happen to an actor following the contract, whereas the three
        position-order bugs it buys out hit compliant actors.
        """
        raw = (
            "Schema I'll use:\n\n"
            + _fence(_out("SHIP", "schema example"))
            + "\n\nActual verdict:\n\n"
            + _out("NO-SHIP", "real verdict", [_BLOCKER])
        )
        assert parse_reviewer_output(raw).verdict == "SHIP"

    def test_early_bare_example_does_not_beat_later_fenced_verdict(self) -> None:
        raw = (
            "Roughly: "
            + _out("SHIP", "rough example")
            + "\n\nFinal:\n\n"
            + _fence(_out("NO-SHIP", "real verdict", [_BLOCKER]))
        )
        assert parse_reviewer_output(raw).verdict == "NO-SHIP"


class TestHonestPathsStillWork:
    """The fix must not cost any shape a real reviewer actually emits."""

    def test_narrative_then_fenced_verdict(self) -> None:
        raw = "I reviewed the diff.\n\n" + _fence(_out("NO-SHIP", "real", [_BLOCKER]))
        assert parse_reviewer_output(raw).verdict == "NO-SHIP"

    def test_bare_json_no_narrative(self) -> None:
        assert parse_reviewer_output(_out("SHIP", "clean")).verdict == "SHIP"

    def test_narrative_then_bare_verdict(self) -> None:
        raw = "Some prose first.\n\nNow the verdict.\n\n" + _out("SHIP", "clean")
        assert parse_reviewer_output(raw).verdict == "SHIP"

    def test_real_recorded_claude_output_with_jsx_trap(self) -> None:
        """The load-bearing reason the bare-object scan is kept: genuine claude
        stdout, no fences at all, JSX braces in prose, bare verdict at the end.
        """
        result_text = json.loads(_FIXTURE.read_text())["result"]
        assert "```" not in result_text, "fixture drifted: it must have no fences"
        out = parse_reviewer_output(result_text)
        assert out.verdict == "NO-SHIP"
        assert len(out.findings) == 8


class TestFenceBoundaryDefects:
    """Two defects found by adversarial probe AFTER the increment-A commit.
    Both lost a real verdict; both are now impossible rather than unlikely."""

    def test_backticks_inside_a_json_string_do_not_close_the_fence(self) -> None:
        """A reviewer whose own finding text mentions ``` closed the fence
        mid-string; the truncated block failed to parse and a real NO-SHIP
        became exit 70.

        The closing fence must be preceded by a newline. JSON forbids a raw
        newline inside a string value (it must be the two-character \\n
        escape), so every newline in fence content is necessarily outside a
        string — which makes this impossible, not merely unlikely.
        """
        raw = (
            "```json\n" + _out("NO-SHIP", "the docstring uses ``` wrongly", [_BLOCKER]) + "\n```\n"
        )
        out = parse_reviewer_output(raw)
        assert out.verdict == "NO-SHIP"
        assert out.summary == "the docstring uses ``` wrongly"

    def test_trailing_unlabeled_fence_cannot_override_a_labeled_verdict(self) -> None:
        """Mirror of the trailing-illustration bug. Unlabeled fences are
        verdict-eligible, so an illustration in a bare ``` fence after a proper
        ```json verdict won on position."""
        raw = (
            _fence(_out("NO-SHIP", "real verdict", [_BLOCKER]))
            + "\n\nfor reference:\n\n"
            + _fence(_out("SHIP", "illustration"), label="")
        )
        assert parse_reviewer_output(raw).verdict == "NO-SHIP"

    def test_unlabeled_fence_still_works_when_no_labeled_fence_exists(self) -> None:
        """The asymmetry must not punish a reviewer that simply omitted the
        label — unlabeled stays eligible when there is no ```json fence."""
        raw = "narrative\n\n" + _fence(_out("NO-SHIP", "unlabeled verdict", [_BLOCKER]), label="")
        assert parse_reviewer_output(raw).verdict == "NO-SHIP"


class TestKnownResidualsOfFencePrecedence:
    """Two shapes that still lose a verdict. Both require the actor to violate
    its template; both are pinned so they cannot be mistaken for closed."""

    def test_KNOWN_RESIDUAL_trailing_json_illustration_still_wins(self) -> None:
        """The `json`-labeled twin of the trailing-illustration bug.

        Every OTHER label is closed (see TestLabeledFencesAreNeverVerdicts).
        Closing this one too — by failing when a response carries more than one
        ```json fence — was implemented and then REVERTED on evidence: the
        anthropic adapter deliberately joins the text of every result turn
        (claude emits spurious epilogue turns, and taking only the terminal turn
        lost real verdicts), so a normal multi-turn response legitimately
        carries two differing ```json fences with the real verdict last. A
        recorded 2026-05-30 run has exactly that shape, and refusing it would
        burn real rounds to close a residual every reviewer template already
        warns against in as many words.
        """
        raw = (
            _fence(_out("NO-SHIP", "real verdict", [_BLOCKER]))
            + "\n\nFor reference, a passing run looks like:\n\n"
            + _fence(_out("SHIP", "illustration"))
        )
        assert parse_reviewer_output(raw).verdict == "SHIP"

    def test_multi_turn_shape_that_residual_buys_keeps_working(self) -> None:
        """The reason the residual is accepted: two DIFFERING json fences whose
        last is the real verdict must parse, because that is what a normal
        claude multi-turn response looks like after the adapter joins turns."""
        raw = (
            _fence(_out("SHIP", "draft from an intermediate turn"))
            + "\n"
            + _fence(_out("NO-SHIP", "final turn verdict", [_BLOCKER]))
        )
        out = parse_reviewer_output(raw)
        assert out.verdict == "NO-SHIP"
        assert out.summary == "final turn verdict"


class TestUnclosedFenceHandling:
    """Unclosed fences — those with an opener but no closing ``` line — must
    not produce false SHIPs. Two distinct failure paths, both reproduced before
    the fix was written."""

    def test_unclosed_non_json_fence_interior_does_not_reach_bare_object_scan(self) -> None:
        """An unclosed ```python fence's content must be masked before the
        bare-object scan runs. Without the fix: the bare-object scan found the
        SHIP JSON inside the unclosed python block (because _FENCE_RE never
        matched the unclosed opener and never masked its interior), and it beat
        the earlier real NO-SHIP verdict by document position."""
        real_verdict = _out("NO-SHIP", "found a bug", [_BLOCKER])
        raw = (
            real_verdict  # bare NO-SHIP object first
            + "\n\n```python\n"
            + _out("SHIP", "illustration inside unclosed python fence")
            # deliberately no closing ``` — simulates truncated output
        )
        result = parse_reviewer_output(raw)
        assert result.verdict == "NO-SHIP"

    def test_unclosed_final_json_fence_fails_closed_not_fallback(self) -> None:
        """An unclosed final ```json fence must be exit 70, not a silent
        fallback to an earlier valid block. Without the fix: _select_verdict_block
        only examined fences matched by _FENCE_RE (which requires a closing
        line), so the unclosed NO-SHIP envelope was invisible and the earlier
        SHIP fence was returned — a false SHIP on a truncated/broken verdict."""
        raw = (
            _fence(_out("SHIP", "earlier valid verdict")) + "\n\nMy real verdict:\n\n"
            "```json\n" + _out("NO-SHIP", "truncated", [_BLOCKER])
            # deliberately no closing ``` — unclosed final json fence
        )
        with pytest.raises(ReviewerOutputError) as exc:
            parse_reviewer_output(raw)
        assert "unclosed" in str(exc.value).lower()

    def test_bare_object_after_unlabeled_fence_does_not_override(self) -> None:
        """A bare JSON object appearing AFTER a closed unlabeled fence must not
        override the fence verdict. Before the fix: unlabeled fences and bare
        objects competed by document position, so a later bare SHIP overrode
        a real NO-SHIP verdict in an unlabeled fence."""
        real_verdict = _out("NO-SHIP", "found a bug", [_BLOCKER])
        raw = (
            _fence(real_verdict, label="")  # unlabeled fence with real verdict
            + "\n\nFor example: "
            + _out("SHIP", "bare object in closing prose")  # bare SHIP after
        )
        result = parse_reviewer_output(raw)
        assert result.verdict == "NO-SHIP", (
            "unlabeled fence verdict must win over a later bare object"
        )

    def test_unclosed_final_unlabeled_fence_fails_closed_not_fallback(self) -> None:
        """An unclosed final unlabeled fence must be exit 70, not a silent
        fallback to an earlier valid bare object. Without the fix: only
        unclosed ```json fences were detected; an unclosed unlabeled fence
        fell through to the bare-object scan and returned an earlier SHIP."""
        raw = (
            _out("SHIP", "earlier bare verdict")  # bare SHIP before
            + "\n\nMy real verdict:\n\n"
            "```\n" + _out("NO-SHIP", "truncated unlabeled", [_BLOCKER])
            # deliberately no closing ``` — unclosed final unlabeled fence
        )
        with pytest.raises(ReviewerOutputError) as exc:
            parse_reviewer_output(raw)
        assert "unclosed" in str(exc.value).lower()
