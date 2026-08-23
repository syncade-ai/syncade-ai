"""A resumed round must carry its trusted-import outcome (PR-h-05 Item 4).

Every operator-facing surface that tells you where a candidate went — the loop summary, the
handoff, the next-steps block — renders the recovery ref out of
``ProducerResult.candidate_import``. ``load_completed_round`` rebuilt the producer block without
it, so a resumed run announced that a candidate had landed and gave the operator no ref to find
it with. That is the exact disclosure PR-h-05 exists to make truthful, silently dropped by the
one code path where an operator is most likely to need it.

The manifest is VALIDATED on the way back in, never trusted: this is arbitrary on-disk state, and
the same rule the round manifest's `diff_bytes` follows (a value is written only by the path that
measured it) means a shape the importer could not have produced is a malformed manifest.
"""

from __future__ import annotations

import json

import pytest

from syncade.orchestrator.resume import ResumeError, load_completed_round
from syncade.producer_import import CandidateImportResult
from tests.resume._helpers import _write_round_manifest

_ENDING = "b" * 40
_RECOVERY = "refs/syncade/candidates/run-1/round-0"


def _round_with_import(tmp_path, block):
    """A completed committed-producer round whose manifest carries ``block``."""
    run_dir = tmp_path / "run"
    round_dir = _write_round_manifest(
        run_dir,
        0,
        round_exit_code=30,
        producer_outcome="committed",
        producer_ending_sha=_ENDING,
    )
    # The rehydrator needs each succeeded reviewer's parsed output on disk, and the manifest
    # helper writes only the manifest.
    (round_dir / "claude-reviewer.parsed.json").write_text(
        json.dumps(
            {
                "verdict": "NO-SHIP",
                "findings": [],
                "summary": "clean",
                "priority_order": [],
                "coverage_gaps": [],
                "dismissed_concerns": [],
            }
        )
    )
    (round_dir / "synthesizer.parsed.json").write_text(
        json.dumps(
            {
                "consolidated_findings": [],
                "synthesis_summary": "clean",
                "root_cause_clusters": [],
            }
        )
    )
    manifest_path = round_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if block is not None:
        manifest["producer"]["candidate_import"] = block
    manifest_path.write_text(json.dumps(manifest))
    return round_dir


def test_recovery_ref_survives_rehydration(tmp_path):
    round_dir = _round_with_import(
        tmp_path,
        {"status": "imported", "recovery_ref": _RECOVERY, "error": None},
    )
    rehydrated = load_completed_round(round_dir)
    assert rehydrated is not None
    assert rehydrated.producer_result.candidate_import == CandidateImportResult(
        status="imported", recovery_ref=_RECOVERY
    )


def test_a_failed_import_keeps_its_reason(tmp_path):
    """`invalid` and `error` are what the operator needs to know a candidate did NOT land."""
    round_dir = _round_with_import(
        tmp_path,
        {"status": "invalid", "recovery_ref": None, "error": "candidate is not a descendant"},
    )
    rehydrated = load_completed_round(round_dir)
    assert rehydrated.producer_result.candidate_import.status == "invalid"
    assert "not a descendant" in rehydrated.producer_result.candidate_import.error


def test_a_round_predating_the_importer_rehydrates_as_none(tmp_path):
    """Legacy rounds have no block at all and must stay loadable — absence is not corruption."""
    rehydrated = load_completed_round(_round_with_import(tmp_path, None))
    assert rehydrated.producer_result is not None
    assert rehydrated.producer_result.candidate_import is None


@pytest.mark.parametrize(
    "block",
    [
        pytest.param({"status": "landed", "recovery_ref": _RECOVERY}, id="unknown-status"),
        pytest.param({"status": "imported", "recovery_ref": None}, id="imported-without-ref"),
        pytest.param({"status": "invalid", "error": None}, id="failure-without-reason"),
        pytest.param(
            {"status": "imported", "recovery_ref": _RECOVERY, "error": "both"},
            id="imported-with-error",
        ),
        pytest.param({"status": "imported", "recovery_ref": 7}, id="non-string-ref"),
        pytest.param("refs/whatever", id="not-an-object"),
    ],
)
def test_a_shape_the_importer_could_not_have_produced_is_refused(tmp_path, block):
    with pytest.raises(ResumeError):
        load_completed_round(_round_with_import(tmp_path, block))


def test_an_impossible_status_and_ref_combination_is_refused(tmp_path):
    """`invalid` + a recovery ref cannot have happened, so a manifest claiming it is corrupt.

    Field-by-field validation admitted this: status was a known value and recovery_ref was a
    string, so rehydration accepted it and persisted artifacts then told the operator a REJECTED
    candidate was anchored and readable. The check lives on `CandidateImportResult` itself, so
    every construction path inherits it rather than only the audited one.
    """
    round_dir = _round_with_import(
        tmp_path,
        {"status": "invalid", "recovery_ref": _RECOVERY, "error": "not a descendant"},
    )
    with pytest.raises(ResumeError):
        load_completed_round(round_dir)


def test_the_type_itself_refuses_it(tmp_path):
    """Not merely the resume path — the invariant belongs to the type."""
    with pytest.raises(ValueError, match="cannot carry a recovery ref"):
        CandidateImportResult(status="invalid", recovery_ref=_RECOVERY, error="not a descendant")
