"""usage.py contract (PR-v2-04): extract token usage from the provider envelopes
syncade already captures. Fixtures mirror the verified shapes in
the CLI-format notes (claude result envelope) and the CLI-format notes
(codex turn.completed) — codex gives tokens only (no cost), claude gives both.
"""

from __future__ import annotations

import json

from syncade.usage import Usage, usage_from_claude_envelope, usage_from_codex_events

_CLAUDE = {
    "type": "result",
    "is_error": False,
    "result": "ok",
    "total_cost_usd": 0.0849,
    "model": "claude-opus-4-8",
    "usage": {
        "input_tokens": 1200,  # claude counts ONLY uncached input here
        "output_tokens": 340,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 100,
    },
}

_CODEX = [
    {"type": "turn.started"},
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 13478,
            "cached_input_tokens": 12160,
            "output_tokens": 38,
            "reasoning_output_tokens": 31,
        },
    },
]


def test_claude_envelope_uses_provider_cost():
    u = usage_from_claude_envelope(_CLAUDE)
    assert u.model == "claude-opus-4-8"
    # input_tokens is the FULL input: uncached (1200) + cache reads (900) + cache
    # creation (100) — claude's usage.input_tokens omits the cached halves, so a
    # naive read undercounts total spend (dogfood finding #1).
    assert u.input_tokens == 2200 and u.output_tokens == 340
    assert u.cached_input_tokens == 900  # the cached subset, for the cost split
    assert u.total_tokens == 2540  # 2200 input + 340 output — no cache tokens dropped
    assert u.cost_usd == 0.0849 and u.cost_source == "provider"


def test_claude_malformed_model_usage_metadata_does_not_raise():
    u = usage_from_claude_envelope(
        {
            "type": "result",
            "modelUsage": 123,
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
    )
    assert u is not None
    assert u.model == ""
    assert u.total_tokens == 3


def test_codex_events_tokens_only_no_cost():
    u = usage_from_codex_events(_CODEX)
    assert u.input_tokens == 13478 and u.output_tokens == 38
    assert u.cached_input_tokens == 12160 and u.reasoning_output_tokens == 31
    assert u.total_tokens == 13547
    assert u.cost_usd is None and u.cost_source == "unknown"


def test_missing_or_malformed_usage_returns_none():
    assert usage_from_claude_envelope({"type": "result"}) is None
    assert usage_from_claude_envelope({"usage": "not-a-dict"}) is None
    assert usage_from_claude_envelope({"usage": {}}) is None
    assert usage_from_codex_events([{"type": "turn.started"}]) is None
    assert usage_from_codex_events([{"type": "turn.completed", "usage": {}}]) is None
    assert usage_from_codex_events([]) is None


def test_malformed_claude_scalar_usage_degrades_to_none():
    assert (
        usage_from_claude_envelope({"usage": {"input_tokens": "oops", "output_tokens": 2}}) is None
    )
    assert (
        usage_from_claude_envelope({"usage": {"input_tokens": 1, "output_tokens": "oops"}}) is None
    )
    assert (
        usage_from_claude_envelope(
            {"usage": {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": "oops"}}
        )
        is None
    )


def test_malformed_codex_scalar_usage_degrades_to_none():
    # Wrong-typed token counts must not fabricate zero-token usage or $0.00 cost.
    u = usage_from_codex_events([{"type": "turn.completed", "usage": {"input_tokens": "oops"}}])
    assert u is None


def test_negative_core_usage_tokens_degrade_to_none():
    claude = {
        "usage": {
            "input_tokens": -1,
            "output_tokens": 2,
        }
    }
    assert usage_from_claude_envelope(claude) is None
    codex = [{"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": -2}}]
    assert usage_from_codex_events(codex) is None


def test_negative_optional_usage_tokens_degrade_to_none():
    claude = {
        "usage": {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": -1,
        }
    }
    assert usage_from_claude_envelope(claude) is None
    codex = [
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "output_tokens": 2, "reasoning_output_tokens": -1},
        }
    ]
    assert usage_from_codex_events(codex) is None


def test_negative_provider_cost_degrades_to_unknown_cost_not_negative_spend():
    u = usage_from_claude_envelope(
        {
            "total_cost_usd": -0.01,
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
    )
    assert u is not None
    assert u.total_tokens == 3
    assert u.cost_usd is None
    assert u.cost_source == "unknown"


def test_nonfinite_provider_cost_degrades_to_unknown_cost_not_invalid_json():
    from syncade.usage import usage_fields

    u = usage_from_claude_envelope(
        {
            "total_cost_usd": float("inf"),
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
    )
    assert u is not None
    assert u.total_tokens == 3
    assert u.cost_usd is None
    assert u.cost_source == "unknown"
    fields = usage_fields(u)
    assert fields == {
        "tokens": 3,
        "cost_usd": None,
        "cost_source": "unknown",
        "auth_mode": "unknown",
    }
    assert json.dumps(fields, allow_nan=False)


def test_partial_core_codex_usage_degrades_to_none():
    assert (
        usage_from_codex_events([{"type": "turn.completed", "usage": {"input_tokens": 1}}]) is None
    )
    assert (
        usage_from_codex_events([{"type": "turn.completed", "usage": {"output_tokens": 2}}]) is None
    )


def test_optional_codex_token_fields_may_be_absent():
    u = usage_from_codex_events(
        [{"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}}]
    )
    assert u is not None
    assert u.total_tokens == 3
    assert u.cached_input_tokens == 0
    assert u.reasoning_output_tokens == 0


def test_malformed_optional_codex_token_usage_degrades_to_none():
    u = usage_from_codex_events(
        [
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cached_input_tokens": "oops",
                },
            }
        ]
    )
    assert u is None


def test_total_tokens():
    assert Usage("m", 100, 40).total_tokens == 140


def test_total_tokens_includes_reasoning_tokens():
    assert Usage("m", 100, 40, reasoning_output_tokens=20).total_tokens == 160


def test_usage_fields_present_and_absent():
    from syncade.usage import usage_fields

    assert usage_fields(None) == {
        "tokens": None,
        "cost_usd": None,
        "cost_source": None,
        "auth_mode": None,
    }
    u = Usage("m", 100, 40, reasoning_output_tokens=20, cost_usd=0.01, cost_source="estimated")
    assert usage_fields(u) == {
        "tokens": 160,
        "cost_usd": 0.01,
        "cost_source": "estimated",
        "auth_mode": "unknown",
    }
    assert usage_fields(Usage("m", -1, 0)) == {
        "tokens": None,
        "cost_usd": None,
        "cost_source": None,
        "auth_mode": None,
    }
    assert usage_fields(Usage("m", 1, 0, cost_usd=float("inf"), cost_source="provider")) == {
        "tokens": None,
        "cost_usd": None,
        "cost_source": None,
        "auth_mode": None,
    }


def test_add_usage_sums_tokens_and_marks_partial_unknown_cost():
    from syncade.usage import _add_usage

    known = Usage("m", 10, 2, cached_input_tokens=1, cost_usd=0.01, cost_source="estimated")
    unknown = Usage("m", 3, 4, reasoning_output_tokens=5)
    u = _add_usage(known, unknown)
    assert u is not None
    assert u.total_tokens == 24
    assert u.cached_input_tokens == 1
    assert u.cost_usd == 0.01
    assert u.cost_source == "unknown"


def test_usage_from_fields_roundtrips_manifest_values():
    # Resume rehydration (finding #2): the persisted tokens/cost/source must
    # reconstruct a Usage that re-persists to the SAME manifest fields.
    from syncade.usage import usage_fields, usage_from_fields

    u = usage_from_fields(1500, 0.02, "estimated")
    assert u.total_tokens == 1500 and u.cost_usd == 0.02 and u.cost_source == "estimated"
    assert usage_fields(u) == {
        "tokens": 1500,
        "cost_usd": 0.02,
        "cost_source": "estimated",
        "auth_mode": "unknown",
    }
    assert usage_from_fields(None, None, None) is None  # no usage recorded → None


def test_usage_from_fields_rejects_malformed_persisted_tokens():
    from syncade.usage import usage_from_fields

    assert usage_from_fields(None, 0.02, "estimated") is None
    assert usage_from_fields("1500", 0.02, "estimated") is None
    assert usage_from_fields(-1, 0.02, "estimated") is None


def test_usage_from_fields_drops_nonfinite_persisted_cost():
    from syncade.usage import usage_from_fields

    u = usage_from_fields(1500, float("inf"), "estimated")
    assert u is not None
    assert u.total_tokens == 1500
    assert u.cost_usd is None
    assert u.cost_source == "unknown"


def test_usage_for_codex_extracts_and_prices():
    import types

    from syncade.pricing_config import PricingConfig
    from syncade.usage import usage_for

    stdout = "\n".join(
        [
            '{"type": "turn.started"}',
            '{"type": "turn.completed", "usage": {"input_tokens": 1000, "output_tokens": 500}}',
        ]
    )
    raw = types.SimpleNamespace(stdout=stdout)
    u = usage_for(raw, "openai", "gpt-5.5", PricingConfig())
    assert u is not None
    assert u.model == "gpt-5.5"  # stamped from config (codex event carries no model)
    assert u.input_tokens == 1000 and u.output_tokens == 500
    assert u.cost_source == "estimated" and u.cost_usd and u.cost_usd > 0


def test_usage_for_codex_negative_usage_is_best_effort_none():
    import types

    from syncade.pricing_config import PricingConfig
    from syncade.usage import usage_for

    raw = types.SimpleNamespace(
        stdout='{"type":"turn.completed","usage":{"input_tokens":-1,"output_tokens":500}}'
    )
    assert usage_for(raw, "openai", "gpt-5.5", PricingConfig()) is None


def test_usage_for_none_raw_empty_stdout_unknown_provider():
    import types

    from syncade.pricing_config import PricingConfig
    from syncade.usage import usage_for

    pc = PricingConfig()
    assert usage_for(None, "openai", "gpt-5.5", pc) is None
    assert usage_for(types.SimpleNamespace(stdout=""), "openai", "gpt-5.5", pc) is None
    assert usage_for(types.SimpleNamespace(stdout="x"), "mystery", "m", pc) is None
    # pricing=None disables usage entirely (the no-config / pre-wiring path)
    assert usage_for(types.SimpleNamespace(stdout="x"), "openai", "gpt-5.5", None) is None


def test_usage_for_is_best_effort_when_extractor_raises(monkeypatch):
    import types

    import syncade.adapters.anthropic as anthropic
    from syncade.pricing_config import PricingConfig
    from syncade.usage import usage_for

    def boom(stdout):
        del stdout
        raise RuntimeError("bad envelope")

    monkeypatch.setattr(anthropic, "_extract_claude_results", boom)
    assert usage_for(types.SimpleNamespace(stdout="{}"), "anthropic", "m", PricingConfig()) is None
