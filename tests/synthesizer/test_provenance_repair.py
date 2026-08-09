"""A miscopied provenance quotation is repaired, not fatal — PR-h-field-01 item 5 (bug 2).

The reported failure, from dogfooding syncade on an unrelated repo:

    <- codex-reviewer     finished in 467.5s — NO-SHIP, 3 finding(s)
    <- codex-reviewer-adv finished in 245.6s — NO-SHIP, 3 finding(s)
    synthesizer finished in 29.9s — FAILED (SynthesizerOutputError)
    exit code: 70

The synthesizer had transcribed a reviewer's finding into `provenance[0]
.original_description` and dropped ONE backtick:

    reviewer wrote:      ...`backdropBlur = 'blur(24px)'` applied as `style={{ ... }}`...
    synthesizer recorded: ...`backdropBlur = 'blur(24px)' applied as `style={{ ... }}`...

Cost: 713 seconds of reviewer wall-clock, six valid findings, and three unrun rounds —
for one character in a QUOTATION of a finding, not in the finding itself.

**The validator was right; the blast radius was not.** A missing backtick is a copy error,
and the ground truth is sitting in `reviewer_results` — which is how the validator knows it
is wrong in the first place. So it copies rather than aborts. The abort is RESERVED for
attribution fabrication (item 6), which is what the check was written to stop.
"""

from __future__ import annotations

from syncade.dispatcher import ReviewerRunResult
from syncade.findings import Finding, ReviewerOutput
from syncade.persistence.synth import _synthesizer_manifest_entry
from syncade.synthesis import ConsolidatedFinding, FindingProvenance, SynthesizerOutput
from syncade.synthesizer.validation import _validate_provenance_against_reviewers

# The finding text from the run that died, verbatim — the shape that is most exposed,
# because findings that quote code are exactly the ones full of backticks and braces.
_SOURCE = (
    "The overlay sets `backdropBlur = 'blur(24px)'` applied as `style={{ ... }}`, "
    "which Safari ignores without a -webkit prefix."
)
_DROPPED_BACKTICK = (
    "The overlay sets `backdropBlur = 'blur(24px)' applied as `style={{ ... }}`, "
    "which Safari ignores without a -webkit prefix."
)


def _reviewers(*texts: str) -> list[ReviewerRunResult]:
    return [
        ReviewerRunResult(
            reviewer_name="rv1",
            provider="openai",
            output=ReviewerOutput(
                verdict="NO-SHIP",
                findings=[
                    Finding(severity="blocker", file="src/Overlay.tsx", spec_clause="§2", finding=t)
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


def _output(quote: str, *, index: int = 0) -> SynthesizerOutput:
    return SynthesizerOutput(
        consolidated_findings=[
            ConsolidatedFinding(
                description="unprefixed backdrop-filter",
                file="src/Overlay.tsx",
                severity="blocker",
                provenance=[
                    FindingProvenance(
                        reviewer_name="rv1",
                        original_severity="blocker",
                        original_index=index,
                        original_description=quote,
                    )
                ],
            )
        ],
        synthesis_summary="consolidated",
    )


def test_the_exact_reported_failure_now_completes(tmp_path):
    """One dropped backtick must not end a run that produced six valid findings."""
    output = _output(_DROPPED_BACKTICK)

    repairs = _validate_provenance_against_reviewers(output, _reviewers(_SOURCE))

    assert len(repairs) == 1
    assert output.consolidated_findings[0].provenance[0].original_description == _SOURCE


def test_the_rendered_quote_is_verbatim_BY_CONSTRUCTION(tmp_path):
    """The strengthening. Before, the rendered text was verbatim only because the check
    happened to pass — the synthesizer's copy was trusted whenever it matched. Now it is
    copied from the source, so no string a synthesizer authors can reach findings.md."""
    for quote in ("totally invented text", _DROPPED_BACKTICK, "", " "):
        if not quote.strip():
            continue  # the schema rejects blank quotes before this check runs
        output = _output(quote)
        _validate_provenance_against_reviewers(output, _reviewers(_SOURCE))
        assert output.consolidated_findings[0].provenance[0].original_description == _SOURCE


def test_a_correct_quote_produces_no_repair(tmp_path):
    """The control. Without it, a function that always reports a repair would pass."""
    output = _output(_SOURCE)

    repairs = _validate_provenance_against_reviewers(output, _reviewers(_SOURCE))

    assert repairs == [], "a verbatim quote was reported as repaired"


def test_reflow_is_still_not_a_repair(tmp_path):
    """Whitespace normalization predates this change and must not start counting as a
    repair — a model that line-wraps a long quote has not miscopied anything."""
    reflowed = "\n   ".join(_SOURCE.split(" ", 3))
    output = _output(reflowed)

    repairs = _validate_provenance_against_reviewers(output, _reviewers(_SOURCE))

    assert repairs == [], "reflow was misreported as a transcription error"


def test_the_repair_targets_the_finding_the_index_points_at(tmp_path):
    """The repair must copy from `original_index`, not from the first finding.

    Copying the wrong source would attribute reviewer text to the wrong finding — a
    fabricated quote produced by the repair itself, which is worse than the bug it fixes.
    """
    other = "A completely different finding about caching headers."
    output = _output("mangled", index=1)

    repairs = _validate_provenance_against_reviewers(output, _reviewers(other, _SOURCE))

    assert len(repairs) == 1
    assert output.consolidated_findings[0].provenance[0].original_description == _SOURCE
    assert repairs[0].reviewer_text == _SOURCE


def test_every_repair_is_recorded_with_both_strings(tmp_path):
    """Repair must not be silent. The operator needs to see that a model rewrote a source,
    both as a fidelity signal and because quietly rewriting a model's output is not
    something to hide."""
    output = _output(_DROPPED_BACKTICK)

    (repair,) = _validate_provenance_against_reviewers(output, _reviewers(_SOURCE))

    assert repair.synthesizer_text == _DROPPED_BACKTICK
    assert repair.reviewer_text == _SOURCE
    assert repair.reviewer_name == "rv1"
    assert repair.original_index == 0
    assert repair.consolidated_index == 0
    assert repair.provenance_index == 0


def test_the_repair_reaches_the_round_manifest(tmp_path):
    """End-to-end through persistence: the record has to survive to disk, or the operator
    never sees it."""
    import json

    from syncade.synthesizer import SynthesizerResult
    from syncade.synthesizer.validation import ProvenanceRepair

    output = _output(_DROPPED_BACKTICK)
    repairs = _validate_provenance_against_reviewers(output, _reviewers(_SOURCE))
    entry = _synthesizer_manifest_entry(
        SynthesizerResult(
            output=output,
            error=None,
            duration_seconds=1.0,
            provenance_repairs=tuple(repairs),
        )
    )

    assert entry is not None
    recorded = entry["provenance_repairs"]
    assert len(recorded) == 1
    assert recorded[0]["reviewer_text"] == _SOURCE
    assert recorded[0]["synthesizer_text"] == _DROPPED_BACKTICK
    # Must be JSON-serializable — the manifest is written with json.dump.
    json.dumps(entry)
    assert isinstance(repairs[0], ProvenanceRepair)


def test_a_clean_run_records_an_empty_list_not_a_missing_key(tmp_path):
    """A consumer must never have to distinguish "absent" from "none happened"."""
    from syncade.synthesizer import SynthesizerResult

    entry = _synthesizer_manifest_entry_for(_output(_SOURCE))
    assert entry["provenance_repairs"] == []

    failure = _synthesizer_manifest_entry(
        SynthesizerResult(output=None, error=ValueError("x"), duration_seconds=1.0)
    )
    assert failure["provenance_repairs"] == []


def _synthesizer_manifest_entry_for(output):
    from syncade.synthesizer import SynthesizerResult

    return _synthesizer_manifest_entry(
        SynthesizerResult(output=output, error=None, duration_seconds=1.0)
    )


# ── The wiring, which the calibration proved nothing covered ────────────────
#
# Every test above builds a SynthesizerResult by hand, so removing
# `provenance_repairs=tuple(_prov_repairs)` from the driver left them ALL green. A repair
# that never reaches the result is a silent rewrite with extra steps.


def test_the_driver_threads_the_repairs_onto_its_result(tmp_path, monkeypatch):
    """Drives the real `run_synthesizer` with a faked subprocess, so the wiring is under
    test rather than the pieces."""
    import json

    import syncade.synthesizer as synth
    import syncade.synthesizer.driver as synth_driver
    from syncade.process import SubprocessResult

    synth_output = {
        "consolidated_findings": [
            {
                "description": "unprefixed backdrop-filter",
                "file": "src/Overlay.tsx",
                "severity": "blocker",
                "provenance": [
                    {
                        "reviewer_name": "rv1",
                        "original_severity": "blocker",
                        "original_index": 0,
                        "original_description": _DROPPED_BACKTICK,
                    }
                ],
            }
        ],
        "synthesis_summary": "consolidated",
    }

    def fake_run_subprocess(argv, *, cwd, env, timeout, input_text):
        del argv, cwd, env, timeout, input_text
        # The real codex envelope: JSONL events, the verdict inside an agent_message.
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t"}),
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "i0",
                            "type": "agent_message",
                            "text": json.dumps(synth_output),
                        },
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            ]
        )
        return SubprocessResult(returncode=0, stdout=stdout, stderr="", duration_seconds=1.0)

    monkeypatch.setattr(synth_driver, "run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(synth_driver, "_init_workspace_git", lambda workspace: None)

    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")

    result = synth.run_synthesizer(
        reviewer_results=_reviewers(_SOURCE),
        repo_root=tmp_path,
        pr_doc_path=pr_doc,
        timeout_seconds=60.0,
        adapter=None,
    )

    assert result.error is None, f"the run did not complete: {result.error!r}"
    assert len(result.provenance_repairs) == 1, "the driver discarded the repair record"
    assert result.provenance_repairs[0].reviewer_text == _SOURCE
    # And the repaired text is what a consumer would render.
    assert result.output.consolidated_findings[0].provenance[0].original_description == _SOURCE


def test_a_blank_quote_is_repaired_rather_than_fatal():
    """The other half of the schema change (dogfood round 4).

    `FindingProvenance` no longer rejects a blank `original_description`, and this is why:
    the repair replaces it with the reviewer's text. Asserted here so the schema relaxation
    can never outlive the guarantee that justifies it.
    """
    for blank in ("", "   ", "\n\t "):
        output = _output(blank)
        repairs = _validate_provenance_against_reviewers(output, _reviewers(_SOURCE))
        assert len(repairs) == 1, f"blank quote {blank!r} was not repaired"
        assert output.consolidated_findings[0].provenance[0].original_description == _SOURCE
