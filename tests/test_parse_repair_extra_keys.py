"""A forbidden extra key no longer discards a whole review — PR-h-field-05.

Measured, run `2026-08-13T16-58-59`: a reviewer returned a complete, correct verdict with one
advisory `recommended_fix` key on one finding. `Finding` is `extra="forbid"`, so the output was
rejected — 1,325,087 tokens and 399s discarded. The judge is skipped when any reviewer fails,
so the OTHER reviewer's 936,113 tokens went with it: 2.26M tokens for one key.

The repair is eligible only when EVERY validation error is `extra_forbidden`. That single
condition is what keeps it from becoming a tolerance, and the tests below are organised around
proving the boundary rather than the happy path — a repair that quietly swallowed a real schema
break would be worse than the failure it replaces.

The fixture is the ACTUAL payload from that run, not a hand-made one, so the acceptance is
"the review that was thrown away would now be used".
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from syncade.findings import ReviewerOutput, parse_reviewer_output
from syncade.findings_json import validate_dropping_forbidden_extras

_FIXTURE = Path(__file__).parent / "fixtures" / "field05_extra_key_verdict.json"


def _payload() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _validate(payload):
    return validate_dropping_forbidden_extras(payload, ReviewerOutput.model_validate, label="rv")


def test_the_real_discarded_review_now_parses():
    """THE acceptance criterion, on the exact bytes that were thrown away."""
    payload = _payload()
    with pytest.raises(ValidationError):
        ReviewerOutput.model_validate(payload)  # today's strict behaviour, for contrast

    out = _validate(payload)
    assert out.verdict == "NO-SHIP"
    assert len(out.findings) == 1
    finding = out.findings[0]
    assert finding.severity == "blocker"
    assert finding.file == "src/syncade/process.py"
    assert finding.evidence_cmd and finding.evidence_output, "evidence must survive the repair"


def test_the_caller_s_payload_is_not_mutated():
    """The repair works on a copy: a caller that inspects the raw object afterwards (or retries)
    must not find it silently edited."""
    payload = _payload()
    before = copy.deepcopy(payload)
    _validate(payload)
    assert payload == before


def test_the_dropped_key_is_named_in_a_warning(caplog):
    """Dropping is a real loss — an extra key may carry content, unlike the synthesizer's
    provably information-free repairs. The trade is right; a SILENT trade would not be."""
    with caplog.at_level("WARNING"):
        _validate(_payload())
    assert "findings.0.recommended_fix" in caplog.text
    assert "dropped 1 key" in caplog.text


# --- the boundary: what must STILL fail -------------------------------------------------------


def test_a_renamed_required_field_still_raises():
    """The dangerous case, excluded BY CONSTRUCTION rather than by a rule that remembers it.

    Renaming `finding` to `description` produces a `missing` error BESIDE the `extra_forbidden`
    one. Mixed error types are ineligible, so the payload raises instead of being silently
    accepted with a finding whose text vanished.
    """
    payload = _payload()
    payload["findings"][0]["description"] = payload["findings"][0].pop("finding")
    with pytest.raises(ValidationError):
        _validate(payload)


def test_a_wrong_type_still_raises():
    payload = _payload()
    payload["findings"][0]["severity"] = 12345
    with pytest.raises(ValidationError):
        _validate(payload)


def test_an_invalid_enum_value_still_raises():
    payload = _payload()
    payload["verdict"] = "MAYBE"
    with pytest.raises(ValidationError):
        _validate(payload)


def test_a_missing_required_field_still_raises():
    payload = _payload()
    del payload["findings"][0]["severity"]
    with pytest.raises(ValidationError):
        _validate(payload)


def test_a_valid_verdict_does_not_trip_the_repair(caplog):
    """No warning, no copy, no repair path — a clean payload must be untouched."""
    payload = _payload()
    del payload["findings"][0]["recommended_fix"]
    with caplog.at_level("WARNING"):
        out = _validate(payload)
    assert out.verdict == "NO-SHIP"
    assert caplog.text == ""


def test_extra_keys_at_the_top_level_are_repaired_too():
    payload = _payload()
    payload["reviewer_notes"] = "chatty"
    out = _validate(payload)
    assert out.verdict == "NO-SHIP"


def test_the_reviewer_entry_point_uses_the_repair():
    """Wiring: parse_reviewer_output is what the dispatcher calls. A repair the reviewer path
    does not use would be inert in production while every unit test above stayed green."""
    fenced = "```json\n" + json.dumps(_payload()) + "\n```"
    out = parse_reviewer_output(fenced)
    assert out.verdict == "NO-SHIP" and len(out.findings) == 1


def test_a_bad_value_in_an_OPTIONAL_field_still_raises():
    """The case that separates the eligibility rule from its safety net, found by mutation.

    `line` is optional, so if the repair dropped keys for ANY error type, a non-integer `line`
    would be deleted and the payload would then revalidate CLEAN — silently discarding a value
    the model got wrong. Every other boundary test here survives that mutation, because their
    error locations either do not exist (`missing`) or name a REQUIRED field whose removal
    fails on the second pass. This one does not, so it is the test that actually pins the
    "every error must be extra_forbidden" rule rather than the `if not dropped` fallback.
    """
    payload = _payload()
    payload["findings"][0]["line"] = "not-an-int"
    with pytest.raises(ValidationError):
        _validate(payload)


# --- item 2: the same class, the other two strict actors --------------------------------------
#
# Only the synthesizer had a repair tier before field-05 — the actor that had already been
# burned. The reviewer, drafter and auditor were each one stray key from the same outcome. The
# reviewer is where it cost 2.26M tokens, so it went first; these two are cheaper legs (one cold
# subprocess, ~20-60s) but the failure mode and the eligibility rule are identical.

_DRAFT = {
    "proposal": "p" * 40,
    "acceptance_criteria": [{"text": "t" * 30, "origin": "transcribed"}],
}
_AUDIT = {
    "verdict": "READY",
    "summary": "s" * 40,
    "priority_order": [],
    "coverage_gaps": [],
    "dismissed_concerns": [],
}


def _fenced(payload: dict) -> str:
    return "```json\n" + json.dumps(payload) + "\n```"


def test_the_drafter_repairs_extra_keys_at_both_levels(caplog):
    """Nested as well as top-level: the drafter's schema is strict at three levels, so a repair
    that only handled the root would leave most of the surface exposed."""
    from syncade.spec_draft import parse_spec_draft_output

    payload = copy.deepcopy(_DRAFT)
    payload["model_notes"] = "chatty"
    payload["acceptance_criteria"][0]["why"] = "nested chatty"

    with caplog.at_level("WARNING"):
        out = parse_spec_draft_output(_fenced(payload))
    assert len(out.acceptance_criteria) == 1
    assert out.acceptance_criteria[0].origin == "transcribed"
    assert "acceptance_criteria.0.why" in caplog.text and "model_notes" in caplog.text


def test_the_auditor_repairs_an_extra_key():
    from syncade.spec_audit_schema import parse_spec_audit_output

    payload = dict(_AUDIT, model_notes="chatty")
    assert parse_spec_audit_output(_fenced(payload)).verdict == "READY"


@pytest.mark.parametrize(
    "actor,payload,mutate",
    [
        ("draft", _DRAFT, lambda p: p.__setitem__("proposal", 12345)),
        ("draft", _DRAFT, lambda p: p["acceptance_criteria"][0].__setitem__("origin", "invented")),
        ("audit", _AUDIT, lambda p: p.__setitem__("verdict", "MAYBE")),
        ("audit", _AUDIT, lambda p: p.__setitem__("priority_order", "not-a-list")),
    ],
)
def test_the_other_actors_still_reject_a_real_schema_break(actor, payload, mutate):
    """The boundary has to hold for every actor, not just the one it was written against."""
    from syncade.spec_audit_schema import SpecAuditOutputError, parse_spec_audit_output
    from syncade.spec_draft import SpecDraftOutputError, parse_spec_draft_output

    broken = copy.deepcopy(payload)
    mutate(broken)
    broken["model_notes"] = "an extra key must not rescue a genuinely broken payload"
    parse, err = (
        (parse_spec_draft_output, SpecDraftOutputError)
        if actor == "draft"
        else (parse_spec_audit_output, SpecAuditOutputError)
    )
    with pytest.raises(err):
        parse(_fenced(broken))
