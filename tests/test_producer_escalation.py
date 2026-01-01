"""PR-22 T3: producer escalation parsing + the ProducerEscalation record.

The producer escalates a finding that is genuinely an operator decision
(a spec/design conflict it cannot resolve in code) by emitting a
sentinel-delimited JSON block in its narrative. ``parse_producer_escalation``
extracts it; a malformed / incomplete block is NOT an escalation (the
evidence bar is enforced structurally — every field required, non-empty).
"""

from __future__ import annotations

import json

from syncade.producer_escalation import (
    ESCALATE_CLOSE,
    ESCALATE_OPEN,
    ProducerEscalation,
    parse_producer_escalation,
)


def _block(
    finding="spec says X but code does Y",
    decision="Should the contract be X or Y?",
    options=("Make code do X", "Amend the spec to Y"),
    rationale="Reproduced: test_foo asserts Y; the brief line 12 says X. Both cannot hold.",
    finding_indices=(0,),
):
    payload = json.dumps(
        {
            "finding_indices": list(finding_indices),
            "finding": finding,
            "decision": decision,
            "options": list(options),
            "rationale": rationale,
        }
    )
    return f"Some narrative...\n{ESCALATE_OPEN}\n{payload}\n{ESCALATE_CLOSE}\nmore text"


class TestParseProducerEscalation:
    def test_well_formed_block_parses(self):
        esc = parse_producer_escalation(_block())
        assert isinstance(esc, ProducerEscalation)
        assert esc.finding == "spec says X but code does Y"
        assert esc.decision == "Should the contract be X or Y?"
        assert esc.options == ["Make code do X", "Amend the spec to Y"]
        assert "Reproduced" in esc.rationale
        assert esc.finding_indices == [0]

    def test_multiple_finding_indices_parse(self):
        """PR-24: a single escalation may cover multiple blocker indices
        (one operator decision, several downstream blockers)."""
        esc = parse_producer_escalation(_block(finding_indices=(0, 2)))
        assert esc is not None
        assert esc.finding_indices == [0, 2]

    def test_missing_finding_indices_returns_none(self):
        # PR-24: finding_indices is part of the structural evidence bar.
        payload = json.dumps({"finding": "x", "decision": "y", "options": ["a"], "rationale": "z"})
        text = f"{ESCALATE_OPEN}\n{payload}\n{ESCALATE_CLOSE}"
        assert parse_producer_escalation(text) is None

    def test_empty_finding_indices_returns_none(self):
        assert parse_producer_escalation(_block(finding_indices=())) is None

    def test_negative_finding_index_returns_none(self):
        assert parse_producer_escalation(_block(finding_indices=(-1,))) is None

    def test_finding_indices_not_a_list_returns_none(self):
        payload = json.dumps(
            {
                "finding_indices": 0,
                "finding": "x",
                "decision": "y",
                "options": ["a"],
                "rationale": "z",
            }
        )
        text = f"{ESCALATE_OPEN}\n{payload}\n{ESCALATE_CLOSE}"
        assert parse_producer_escalation(text) is None

    def test_finding_indices_non_int_element_returns_none(self):
        payload = json.dumps(
            {
                "finding_indices": ["0"],
                "finding": "x",
                "decision": "y",
                "options": ["a"],
                "rationale": "z",
            }
        )
        text = f"{ESCALATE_OPEN}\n{payload}\n{ESCALATE_CLOSE}"
        assert parse_producer_escalation(text) is None

    def test_duplicate_finding_indices_returns_none(self):
        assert parse_producer_escalation(_block(finding_indices=(0, 0))) is None

    def test_bool_finding_index_returns_none(self):
        # JSON ``true`` parses to Python ``True`` and ``isinstance(True, int)``
        # is True — guard so a bool can't masquerade as an index.
        payload = json.dumps(
            {
                "finding_indices": [True],
                "finding": "x",
                "decision": "y",
                "options": ["a"],
                "rationale": "z",
            }
        )
        text = f"{ESCALATE_OPEN}\n{payload}\n{ESCALATE_CLOSE}"
        assert parse_producer_escalation(text) is None

    def test_no_marker_returns_none(self):
        assert parse_producer_escalation("I fixed it by doing X. No escalation.") is None

    def test_malformed_json_returns_none(self):
        text = f"{ESCALATE_OPEN}\n{{not valid json,,,}}\n{ESCALATE_CLOSE}"
        assert parse_producer_escalation(text) is None

    def test_missing_required_field_returns_none(self):
        # No rationale → not a valid escalation (evidence bar).
        payload = json.dumps({"finding": "x", "decision": "y", "options": ["a"]})
        text = f"{ESCALATE_OPEN}\n{payload}\n{ESCALATE_CLOSE}"
        assert parse_producer_escalation(text) is None

    def test_empty_options_returns_none(self):
        payload = json.dumps({"finding": "x", "decision": "y", "options": [], "rationale": "z"})
        text = f"{ESCALATE_OPEN}\n{payload}\n{ESCALATE_CLOSE}"
        assert parse_producer_escalation(text) is None

    def test_unterminated_block_returns_none(self):
        text = f'{ESCALATE_OPEN}\n{{"finding": "x"}}'  # no close sentinel
        assert parse_producer_escalation(text) is None

    def test_options_must_be_strings(self):
        payload = json.dumps({"finding": "x", "decision": "y", "options": [1, 2], "rationale": "z"})
        text = f"{ESCALATE_OPEN}\n{payload}\n{ESCALATE_CLOSE}"
        assert parse_producer_escalation(text) is None
