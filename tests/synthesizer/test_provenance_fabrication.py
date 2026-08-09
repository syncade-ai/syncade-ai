"""Attribution fabrication still aborts — PR-h-field-01 item 6, the partner of item 5.

Item 5 made a miscopied QUOTATION recoverable. This file pins the other direction: a
synthesizer that invents WHO said something, or WHICH finding it was, or HOW SEVERE the
source called it, still ends the run at exit 70. The brief is explicit that the two must be
tested together — *"a repair path that also swallows fabrication is worse than the abort it
replaces"* — because the repair and the aborts now live in the same loop, and the repair
mutates the object the aborts are still validating.

The distinction is not a judgement about intent, which nothing here can see. It is about
whether the ground truth exists:

  a miscopied quote  -> `reviewer_results` holds the real text; copy it (item 5)
  a ghost reviewer   -> there is nothing to copy FROM; refuse (item 6)

The severity check belongs on this side despite being a single enum: misreporting a
blocker as "minor" is how a synthesizer would slip a unanimous blocker past the
all-blocker dismissal guard, which is attribution, not transcription.
"""

from __future__ import annotations

import pytest

from syncade.dispatcher import ReviewerRunResult
from syncade.findings import Finding, ReviewerOutput
from syncade.synthesis import (
    ConsolidatedFinding,
    FindingProvenance,
    SynthesizerOutput,
    SynthesizerOutputError,
)
from syncade.synthesizer.validation import _validate_provenance_against_reviewers

_A = "auth query interpolates the username without parameterization"
_B = "cache headers allow stale reads across tenants"


def _reviewers(*texts: str, name: str = "rv1", severity: str = "blocker"):
    texts = texts or (_A,)
    return [
        ReviewerRunResult(
            reviewer_name=name,
            provider="openai",
            output=ReviewerOutput(
                verdict="NO-SHIP",
                findings=[
                    Finding(severity=severity, file="a.py", spec_clause="§1", finding=t)
                    for t in texts
                ],
                summary="s",
                priority_order=list(range(len(texts))),
                coverage_gaps=[],
                dismissed_concerns=[],
            ),
            error=None,
            duration_seconds=1.0,
        )
    ]


def _prov(**kw):
    d = {
        "reviewer_name": "rv1",
        "original_severity": "blocker",
        "original_index": 0,
        "original_description": _A,
    }
    d.update(kw)
    return FindingProvenance(**d)


def _finding(*provs, severity: str = "blocker"):
    return ConsolidatedFinding(
        description="d", file="a.py", severity=severity, provenance=list(provs)
    )


def _out(*findings):
    return SynthesizerOutput(consolidated_findings=list(findings), synthesis_summary="s")


# ── The fabrication set: no ground truth exists, so refuse ──────────────────


def test_a_fabricated_reviewer_name_aborts():
    with pytest.raises(SynthesizerOutputError) as exc:
        _validate_provenance_against_reviewers(
            _out(_finding(_prov(reviewer_name="ghost-reviewer"))), _reviewers(_A)
        )
    assert "unknown reviewer" in str(exc.value)


def test_an_out_of_range_original_index_aborts():
    with pytest.raises(SynthesizerOutputError) as exc:
        _validate_provenance_against_reviewers(
            _out(_finding(_prov(original_index=7))), _reviewers(_A)
        )
    assert "out of range" in str(exc.value)


def test_an_index_against_a_reviewer_with_no_findings_aborts():
    """Distinct message: 0 findings means NO index is valid, which reads differently from
    'you asked for 7 of 2'."""
    with pytest.raises(SynthesizerOutputError) as exc:
        _validate_provenance_against_reviewers(
            _out(_finding(_prov(original_index=0))),
            [
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="openai",
                    output=ReviewerOutput(
                        verdict="SHIP",
                        findings=[],
                        summary="clean",
                        priority_order=[],
                        coverage_gaps=[],
                        dismissed_concerns=[],
                    ),
                    error=None,
                    duration_seconds=1.0,
                )
            ],
        )
    assert "produced 0" in str(exc.value)


def test_a_misreported_original_severity_aborts():
    """Attribution, not transcription: downgrading a source blocker to 'minor' is how a
    synthesizer would slip a unanimous blocker past the all-blocker dismissal guard.

    Both the consolidated severity and the provenance severity say 'minor' so the SCHEMA
    accepts the object — this cross-input check is the only thing standing between it and
    a disarmed blocker.
    """
    with pytest.raises(SynthesizerOutputError) as exc:
        _validate_provenance_against_reviewers(
            _out(_finding(_prov(original_severity="minor"), severity="minor")), _reviewers(_A)
        )
    assert "original_severity" in str(exc.value)


# ── The item-5 interaction: repair must not become a hole ───────────────────


def test_the_range_check_runs_BEFORE_the_repair():
    """An out-of-range index with mangled text must abort with the RANGE error.

    If the text repair ran first it would index `texts_by_name[name][7]` and raise a bare
    IndexError — an unhandled crash instead of syncade's message, and on the very input
    the fabrication guard exists to catch.
    """
    with pytest.raises(SynthesizerOutputError) as exc:
        _validate_provenance_against_reviewers(
            _out(_finding(_prov(original_index=7, original_description="mangled"))),
            _reviewers(_A),
        )
    assert "out of range" in str(exc.value)


def test_a_fabrication_AFTER_a_repair_in_the_same_finding_still_aborts():
    """The repair appends and mutates but must not short-circuit the loop."""
    with pytest.raises(SynthesizerOutputError):
        _validate_provenance_against_reviewers(
            _out(
                _finding(
                    _prov(original_description="mangled"),
                    _prov(reviewer_name="ghost", original_index=1),
                )
            ),
            _reviewers(_A, _B),
        )


def test_a_fabrication_in_a_LATER_finding_still_aborts():
    """Findings are a separate loop level from provenance entries; both must keep going."""
    with pytest.raises(SynthesizerOutputError):
        _validate_provenance_against_reviewers(
            _out(
                _finding(_prov(original_description="mangled")),
                _finding(_prov(reviewer_name="ghost")),
            ),
            _reviewers(_A, _B),
        )


def test_a_half_repaired_output_never_reaches_disk(tmp_path):
    """The repair mutates in place, so an aborted run leaves the object partly rewritten.

    That is fine ONLY because the driver returns `output=None` on SynthesizerOutputError
    and `persist_synthesizer_result` writes `parsed.json` only when output is not None. If
    either changed, syncade would persist a half-repaired artifact as if it were the
    model's own output. Pinned end-to-end rather than reasoned about.
    """
    from syncade.persistence.synth import persist_synthesizer_result
    from syncade.synthesizer import SynthesizerResult

    output = _out(_finding(_prov(original_description="mangled"), _prov(reviewer_name="ghost")))
    with pytest.raises(SynthesizerOutputError):
        _validate_provenance_against_reviewers(output, _reviewers(_A, _B))

    # The object IS mutated — this is the risk being contained, not a hypothetical.
    assert output.consolidated_findings[0].provenance[0].original_description == _A

    paths = persist_synthesizer_result(
        tmp_path,
        SynthesizerResult(output=None, error=SynthesizerOutputError("boom"), duration_seconds=1.0),
    )
    assert paths.parsed is None, "an aborted synth wrote a parsed.json"
    assert not (tmp_path / "synthesizer.parsed.json").exists()


def test_on_success_the_record_is_three_way(tmp_path):
    """raw in stdout, repaired in parsed.json, the delta in the manifest.

    An operator auditing a repair must be able to see BOTH strings and which one syncade
    used. Losing the raw would make the rewrite unfalsifiable.
    """
    import json

    from syncade.persistence.synth import _synthesizer_manifest_entry, persist_synthesizer_result
    from syncade.process import SubprocessResult
    from syncade.synthesizer import SynthesizerResult

    output = _out(_finding(_prov(original_description="mangled quote")))
    repairs = _validate_provenance_against_reviewers(output, _reviewers(_A))
    result = SynthesizerResult(
        output=output,
        error=None,
        duration_seconds=1.0,
        raw_subprocess_result=SubprocessResult(
            returncode=0, stdout="RAW MODEL OUTPUT mangled quote", stderr="", duration_seconds=1.0
        ),
        provenance_repairs=tuple(repairs),
    )

    paths = persist_synthesizer_result(tmp_path, result)

    assert "mangled quote" in paths.stdout.read_text(), "the raw model output was lost"
    parsed = json.loads(paths.parsed.read_text())
    assert parsed["consolidated_findings"][0]["provenance"][0]["original_description"] == _A, (
        "parsed.json must show what syncade USED, which is the reviewer's text"
    )
    entry = _synthesizer_manifest_entry(result)
    assert entry["provenance_repairs"][0]["synthesizer_text"] == "mangled quote"
    assert entry["provenance_repairs"][0]["reviewer_text"] == _A


def test_a_clean_output_still_passes():
    """The control. Without it, a validator that rejected everything would pass this file."""
    assert _validate_provenance_against_reviewers(_out(_finding(_prov())), _reviewers(_A)) == []
