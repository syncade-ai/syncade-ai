"""PR-v2-04 Task 4: manifest builders spread usage_fields (tokens/cost_usd/
cost_source beside duration_seconds); summary.md renders a single
'## Token usage & cost' section with per-actor lines + a run total."""

from __future__ import annotations

import types

from syncade.adapters.producer import ProducerOutput
from syncade.dispatcher import ReviewerRunResult
from syncade.findings import ReviewerOutput
from syncade.persistence.producer import _producer_manifest_entry
from syncade.persistence.reviewer import _reviewer_manifest_entry
from syncade.persistence.run_summary import _cost_section
from syncade.producer import ProducerResult
from syncade.usage import Usage


def _ship_output():
    return ReviewerOutput(
        verdict="SHIP",
        findings=[],
        summary="s",
        priority_order=[],
        coverage_gaps=[],
        dismissed_concerns=[],
    )


def test_reviewer_manifest_entry_includes_usage():
    r = ReviewerRunResult(
        reviewer_name="cdx",
        provider="openai",
        output=_ship_output(),
        error=None,
        duration_seconds=1.0,
        model="gpt-5.5",
        usage=Usage("gpt-5.5", 1000, 500, cost_usd=0.006, cost_source="estimated"),
    )
    entry = _reviewer_manifest_entry(r)
    assert entry["model"] == "gpt-5.5"
    assert entry["tokens"] == 1500
    assert entry["cost_usd"] == 0.006
    assert entry["cost_source"] == "estimated"


def test_reviewer_manifest_entry_usage_null_when_absent():
    r = ReviewerRunResult(
        reviewer_name="x",
        provider="openai",
        output=None,
        error=ValueError("boom"),
        duration_seconds=0.0,
    )
    entry = _reviewer_manifest_entry(r)
    assert entry["tokens"] is None
    assert entry["cost_usd"] is None
    assert entry["cost_source"] is None


def test_producer_manifest_entry_includes_retried():
    """Regression: _producer_manifest_entry must expose ProducerResult.retries as 'retried'.
    Previously the field was omitted, so an operator could not tell from manifest.json whether
    the producer retried (C4 violation per PR-v2-22)."""
    result = ProducerResult(
        outcome="stalled",
        starting_sha="a" * 40,
        ending_sha="a" * 40,
        duration_seconds=1.0,
        output=ProducerOutput(narrative_text="no change"),
        error=None,
        retries=2,
    )
    entry = _producer_manifest_entry(result)
    assert entry["retried"] == 2

    # C5: no-retry producer block must NOT have "retried" at all (byte-identical to pre-PR).
    result0 = ProducerResult(
        outcome="stalled",
        starting_sha="a" * 40,
        ending_sha="a" * 40,
        duration_seconds=1.0,
        output=ProducerOutput(narrative_text="no change"),
        error=None,
    )
    entry0 = _producer_manifest_entry(result0)
    assert "retried" not in entry0


def test_producer_manifest_prefers_result_model_over_current_config():
    result = ProducerResult(
        outcome="stalled",
        starting_sha="a" * 40,
        ending_sha="a" * 40,
        duration_seconds=1.0,
        output=ProducerOutput(narrative_text="no change"),
        error=None,
        provider="anthropic",
        model="artifact-model",
        usage=Usage("artifact-model", 100, 50, cost_usd=0.01, cost_source="estimated"),
    )
    entry = _producer_manifest_entry(
        result,
        producer_config_provider="anthropic",
        producer_config_model="current-config-model",
    )
    assert entry["model"] == "artifact-model"
    assert entry["provider"] == "anthropic"


def test_cost_section_renders_per_actor_and_total():
    dispatch = types.SimpleNamespace(
        results=[
            types.SimpleNamespace(
                reviewer_name="cdx",
                provider="openai",
                usage=Usage("gpt-5.5", 1000, 500, cost_usd=0.006, cost_source="estimated"),
            )
        ]
    )
    synth = types.SimpleNamespace(
        usage=Usage("gpt-5.5", 200, 100, cost_usd=0.001, cost_source="estimated")
    )
    out = "\n".join(_cost_section(dispatch, synth, None))
    assert "## Token usage & cost" in out
    assert "cdx (openai)" in out and "1500 tok" in out
    # `Total:` is gone deliberately — it read as spend, and these dollars are a valuation.
    # The billing block is now rendered by `syncade.billing`, THE SAME code --metrics uses,
    # so the two surfaces cannot drift apart again (tests/metrics/test_billing_surfaces_agree.py).
    assert "tokens:" in out and "estimates" in out
    assert "billed:" in out
    assert "API-equiv:" in out


def test_cost_section_empty_when_no_usage():
    dispatch = types.SimpleNamespace(results=[types.SimpleNamespace(usage=None)])
    assert _cost_section(dispatch, None, None) == []


def test_cost_section_flags_unpriced_not_free():
    # An actor with usage but no price must NOT be totalled as $0/free (finding #4).
    dispatch = types.SimpleNamespace(
        results=[
            types.SimpleNamespace(
                reviewer_name="priced",
                provider="openai",
                usage=Usage("gpt-5.5", 1000, 500, cost_usd=0.01, cost_source="estimated"),
            ),
            types.SimpleNamespace(
                reviewer_name="unpriced",
                provider="mystery",
                usage=Usage("unknown-model", 2000, 0, cost_usd=None, cost_source="unknown"),
            ),
        ]
    )
    out = "\n".join(_cost_section(dispatch, None, None))
    assert "unknown" in out  # the unpriced actor's cost renders as 'unknown', not $0
    assert "unpriced" in out.lower()  # and the Total flags that unpriced usage exists
