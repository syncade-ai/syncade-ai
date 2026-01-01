"""The two billing surfaces cannot disagree, because they share one classifier.

**This file is the fix for a bug I made eight times.**

`cost_usd` is a valuation, not spend. Syncade renders that judgement on TWO surfaces —
`--metrics` and each round's `summary.md` — and every time I fixed one I left the other
telling the original lie. Three separate review rounds caught exactly that:

    round 5  --metrics learned billed-vs-valuation.        summary.md said "Total: $0.14".
    round 7  --metrics learned unpriced API = lower bound.  summary.md did not.
    round 8  --metrics learned unpriced+unknown != free.    summary.md did not.

Each fix was correct. Each landed on one of two twins. The bug was never the logic — it
was that the logic lived in two places, so "fixed" only ever meant "fixed here".

So instead of a ninth patch, `syncade.billing` owns the rules AND the words, and both
surfaces call it. This test pins that: for every interesting input, the two surfaces make
the SAME CLAIMS. If someone re-forks the logic, this fails.
"""

from __future__ import annotations

import re

import pytest

from syncade import billing
from syncade.cli.metrics_mode import _billing_totals, _spend_lines
from syncade.metrics.schema import ActorStatRow, open_db, upsert_actor_stat
from syncade.persistence.run_summary import _cost_section
from syncade.usage import Usage

# (label, cost_usd, auth_mode) — every class of traffic that has ever been mis-reported
_CASES = [
    ("subscription, priced", 12.50, "subscription"),
    ("api, priced (real money)", 12.50, "api"),
    ("legacy, priced", 12.50, ""),
    ("unknown, priced", 12.50, "unknown"),
    ("subscription, unpriced", None, "subscription"),
    ("api, UNPRICED (lower bound)", None, "api"),
    ("unknown, UNPRICED (not free)", None, "unknown"),
]


def _claims(text: str) -> dict[str, str]:
    """The dollar/token claims a surface makes, stripped of decoration."""
    out = {}
    for key in ("billed", "API-equiv", "unclassed"):
        m = re.search(rf"\*?\*?{re.escape(key)}:?\*?\*?\s+(\$[\d.]+[^(\n]*)", text)
        if m:
            out[key] = m.group(1).strip()
    return out


@pytest.mark.parametrize(("label", "cost", "auth"), _CASES)
def test_both_surfaces_make_the_same_claims(label, cost, auth) -> None:
    u = Usage("m", 1000, 0, cost_usd=cost, cost_source="estimated", auth_mode=auth)

    dispatch = type(
        "D",
        (),
        {"results": [type("R", (), {"usage": u, "reviewer_name": "rv", "provider": "openai"})()]},
    )()
    summary = "\n".join(_cost_section(dispatch, None, None))

    conn = open_db(":memory:")
    upsert_actor_stat(
        conn,
        ActorStatRow(
            run_id="R",
            role="reviewer",
            name="rv",
            provider="openai",
            model="m",
            tokens=u.total_tokens,
            cost_usd=cost,
            # aggregate() records unpriced tokens HERE. A fixture the real writer would
            # never produce compares the two surfaces on a fiction.
            cost_incomplete_tokens=0 if cost is not None else u.total_tokens,
            cost_source="estimated",
            auth_mode=auth,
        ),
    )
    metrics = "\n".join(_spend_lines(u.total_tokens, cost, 0, _billing_totals(conn)))

    assert _claims(summary) == _claims(metrics), (
        f"the two surfaces DISAGREE about `{label}`.\n"
        f"summary.md: {_claims(summary)}\n--metrics : {_claims(metrics)}\n"
        f"That divergence is the bug this module exists to make impossible."
    )


class TestTheSharedClassifier:
    def test_from_usages_and_from_rows_agree(self) -> None:
        """The two entry points must classify identically — they ARE the same rules."""
        u = Usage("m", 1000, 0, cost_usd=None, cost_source="unknown", auth_mode="api")
        from_usage = billing.from_usages([u])
        from_row = billing.from_rows([("api", 0.0, 1000)])
        assert from_usage == from_row

    def test_api_unpriced_is_a_lower_bound_everywhere(self) -> None:
        b = billing.from_rows([("api", 3.0, 500_000)])
        assert b.billed == 3.0
        assert b.billed_unpriced_tokens == 500_000
        assert "AT LEAST" in "\n".join(billing.render(b))

    def test_unknown_unpriced_is_never_free(self) -> None:
        b = billing.from_rows([("unknown", 0.0, 500_000)])
        out = "\n".join(billing.render(b))
        assert "unclassed" in out and "500000 unpriced tokens" in out


class TestRetryUsageIsNotTreatedAsFullyPriced:
    """`from_usages` derived "priced" from `cost_usd is not None`, but `from_rows` (the DB
    twin) reads `cost_incomplete_tokens`. A retry (`_add_usage` of a priced + an unpriced
    attempt) keeps a PARTIAL cost while marking `cost_source="unknown"`, so the two entry
    points to the ONE billing module disagreed on what "priced" means -- which is the whole
    thing the module was supposed to make impossible. Caught by the panel.

    Fixed by giving Usage the SAME predicate the DB column is written from
    (Usage.cost_incomplete_tokens), and having from_usages read it."""

    def test_retry_combined_usage_is_a_lower_bound(self) -> None:
        from syncade.usage import Usage, _add_usage

        combined = _add_usage(
            Usage("m", 100, 10, cost_usd=3.0, cost_source="estimated", auth_mode="api"),
            Usage("m", 500_000, 0, cost_usd=None, cost_source="unknown", auth_mode="api"),
        )
        assert combined.cost_source == "unknown", "the retry marks the merge unknown"
        assert combined.cost_usd == 3.0, "but keeps the partial cost"
        assert combined.cost_incomplete_tokens == combined.total_tokens, (
            "a partial-cost Usage must count as carrying unpriced tokens"
        )

        b = billing.from_usages([combined])
        assert b.billed == 3.0
        assert b.billed_unpriced_tokens > 0, "summary.md would drop the lower-bound hedge"
        assert "AT LEAST" in "\n".join(billing.render(b))

    def test_the_usage_predicate_matches_the_aggregate_write_predicate(self) -> None:
        """The two must never drift: aggregate marks incomplete on
        `cost_usd is None or cost_source == "unknown"`; Usage must say the same."""
        from syncade.usage import Usage

        def agg_rule(cost, src, tok):
            return tok if (cost is None or src == "unknown") else 0

        for cost, src in [
            (3.0, "estimated"),
            (None, "unknown"),
            (3.0, "unknown"),
            (3.0, "provider"),
        ]:
            u = Usage("m", 100, 0, cost_usd=cost, cost_source=src, auth_mode="api")
            assert u.cost_incomplete_tokens == agg_rule(cost, src, u.total_tokens), (cost, src)
