"""Cost is money only when it was actually billed (PR-v2-24, issue 4).

``--metrics`` reported **$71.18 of "spend"** across a 280-run corpus whose real marginal
cost was **~$0** — every call rode a ChatGPT plan or a claude.ai plan. Two separate
mistakes fed that number, and only one of them was in the spec:

1. codex tokens priced from a table (``cost_source="estimated"``) — a valuation, not spend.
2. claude's OWN ``total_cost_usd`` (``cost_source="provider"``) — which syncade trusted
   MOST. Measured live: a two-token reply on an OAuth subscription reports $0.1426.
   The provider hands you a dollar figure for money you did not spend.

So billing reality is ORTHOGONAL to where the number came from. Only the resolved
``auth_mode`` knows it. These tests exist because all three of the obvious mutations
(count subscription as billed / treat legacy as free / fabricate a billed figure when the
mode is unknown) initially passed the entire 1994-test suite.
"""

from __future__ import annotations

import pytest

from syncade.cli.metrics_mode import _billing_totals, _spend_lines
from syncade.usage import Usage


def _db(*costs_and_modes: tuple[float, str]):
    """An in-memory metrics DB holding one actor_stats row per (cost, auth_mode).

    `_billing_totals` takes a CONNECTION, not display rows, and that is the fix: it must
    compute money from the raw table, never from rows the display layer has already
    grouped and auth-merged. Building a real DB here keeps the test honest about that.
    """
    from syncade.metrics.schema import ActorStatRow, open_db, upsert_actor_stat

    conn = open_db(":memory:")
    for i, (cost, auth) in enumerate(costs_and_modes):
        upsert_actor_stat(
            conn,
            ActorStatRow(
                run_id="R1",
                role="reviewer",
                name=f"a{i}",
                provider="openai",
                model="gpt-5.5",
                tokens=1000,
                cost_usd=cost,
                cost_source="estimated",
                auth_mode=auth,
            ),
        )
    return conn


class TestBilledUsd:
    def test_subscription_billed_nothing(self) -> None:
        """The headline. A priced figure on subscription traffic is a VALUATION."""
        u = Usage("m", 100, 10, cost_usd=12.50, cost_source="estimated", auth_mode="subscription")
        assert u.billed_usd == 0.0
        assert u.cost_usd == 12.50, "the valuation is kept — it is what the plan is worth"

    def test_api_billed_the_cost(self) -> None:
        u = Usage("m", 100, 10, cost_usd=12.50, cost_source="estimated", auth_mode="api")
        assert u.billed_usd == 12.50

    @pytest.mark.parametrize("mode", ["unknown", "none", ""])
    def test_unknown_mode_is_none_never_a_number(self, mode) -> None:
        """I5: cost is never fabricated. If we cannot say, we say nothing — we do NOT
        default to $0 (which would understate) or to the cost (which would overstate)."""
        u = Usage("m", 100, 10, cost_usd=12.50, cost_source="provider", auth_mode=mode)
        assert u.billed_usd is None

    def test_provider_reported_cost_is_not_exempt(self) -> None:
        """The one the spec missed. claude emits total_cost_usd even on an OAuth session,
        and syncade recorded that as cost_source="provider" — its MOST trusted source. A
        provider-reported dollar is still not a billed dollar."""
        u = Usage("m", 100, 10, cost_usd=0.1426, cost_source="provider", auth_mode="subscription")
        assert u.billed_usd == 0.0


class TestBillingTotals:
    def test_subscription_traffic_is_not_billed(self) -> None:
        b = _billing_totals(_db((10.0, "subscription")))
        billed, api_equiv, unclassified = b.billed, b.api_equiv, b.unclassified
        assert billed == 0.0, "this is the bug: valuations summed and called spend"
        assert api_equiv == 10.0, "the valuation still surfaces — just not as money"
        assert unclassified == 0.0

    def test_api_traffic_is_billed(self) -> None:
        b = _billing_totals(_db((10.0, "api")))
        assert b.billed == 10.0
        assert b.api_equiv == 10.0

    def test_legacy_rows_are_unclassified_not_free(self) -> None:
        """Pre-PR runs recorded no auth mode. Quietly counting them as $0 billed would be
        the same lie pointing the other way: we do not know."""
        b = _billing_totals(_db((10.0, "")))
        assert b.billed == 0.0
        assert b.unclassified == 10.0, "must be NAMED, not silently absorbed"

    def test_mixed_corpus_splits_correctly(self) -> None:
        b = _billing_totals(_db((10.0, "api"), (20.0, "subscription"), (5.0, "")))
        billed, api_equiv, unclassified = b.billed, b.api_equiv, b.unclassified
        assert billed == 10.0, "only the api row is money"
        assert api_equiv == 35.0, "everything is worth something at list price"
        assert unclassified == 5.0


class TestReportWording:
    def test_subscription_run_says_zero_billed(self) -> None:
        out = "\n".join(_spend_lines(1000, 20.0, 0, _billing_totals(_db((20.0, "subscription")))))
        assert "billed:     $0.00" in out
        assert "API-equiv:  $20.00" in out
        assert "spend" not in out.lower(), "the word `spend` is what made the old report lie"

    def test_api_run_says_it_billed(self) -> None:
        out = "\n".join(_spend_lines(1000, 20.0, 0, _billing_totals(_db((20.0, "api")))))
        assert "billed:     $20.00" in out

    def test_legacy_corpus_names_the_unclassified_money(self) -> None:
        out = "\n".join(_spend_lines(1000, 20.0, 0, _billing_totals(_db((20.0, "")))))
        assert "billed:     $0.00" in out
        assert "unclassed:  $20.00" in out
        assert "cannot say" in out, "the user must be told we do not know"

    def test_unknown_usage_is_not_rendered_as_zero(self) -> None:
        """Unknown is not zero. A legacy run with no usage at all must not be printed as
        `tokens: 0 / billed: $0.00` — that invents a fact."""
        from syncade.cli.metrics_mode import Billing

        out = "\n".join(_spend_lines(None, None, 0, Billing(0.0, 0.0, 0.0, 0, 0)))
        assert "unavailable" in out
        assert "billed:" not in out


class TestRetriesKeepTheirBillingLabel:
    """`_add_usage` combines the attempts of a retried subprocess. It rebuilt `Usage`
    WITHOUT `auth_mode`, so a single transient 429 was enough to turn a fully-classified
    cost into an unclassified one — the dollars survived, the label did not.

    Caught by syncade's own panel, unanimously. The dispatcher accumulates through this on
    every reviewer retry; the synth driver does the same.
    """

    def test_two_subscription_attempts_stay_subscription(self) -> None:
        from syncade.usage import _add_usage

        a = Usage("m", 100, 10, cost_usd=1.0, cost_source="estimated", auth_mode="subscription")
        b = Usage("m", 50, 5, cost_usd=0.5, cost_source="estimated", auth_mode="subscription")
        combined = _add_usage(a, b)
        assert combined.auth_mode == "subscription", "a retry silently lost its billing label"
        assert combined.billed_usd == 0.0, "and the cost would have been reported as unclassed"
        assert combined.cost_usd == 1.5

    def test_two_api_attempts_stay_billed(self) -> None:
        from syncade.usage import _add_usage

        a = Usage("m", 100, 10, cost_usd=1.0, cost_source="estimated", auth_mode="api")
        b = Usage("m", 50, 5, cost_usd=0.5, cost_source="estimated", auth_mode="api")
        assert _add_usage(a, b).billed_usd == 1.5, "real money would have gone unreported"

    def test_a_known_mode_beats_an_unrecorded_one(self) -> None:
        """Both attempts are the SAME actor resolved from the same config and env, so they
        agree by construction. An `unknown` on one side means the extractor did not stamp
        it, not that the billing changed mid-retry."""
        from syncade.usage import _add_usage

        a = Usage("m", 100, 10, cost_usd=1.0, cost_source="estimated", auth_mode="api")
        b = Usage("m", 50, 5, cost_usd=0.5, cost_source="estimated", auth_mode="unknown")
        assert _add_usage(a, b).auth_mode == "api"

    def test_genuinely_conflicting_modes_degrade_to_mixed(self) -> None:
        """Should be impossible, so it must not silently pick a side: `mixed` reports as
        neither billed nor free."""
        from syncade.usage import _add_usage

        a = Usage("m", 100, 10, cost_usd=1.0, cost_source="estimated", auth_mode="api")
        b = Usage("m", 50, 5, cost_usd=0.5, cost_source="estimated", auth_mode="subscription")
        combined = _add_usage(a, b)
        assert combined.auth_mode == "mixed"
        assert combined.billed_usd is None, "must not guess which account paid"


class TestBillingIsNeverComputedFromDisplayAGGREGATES:
    """The one that under-reported REAL MONEY.

    `_billing_totals` used to sum `_actor_spend_rows()` — which groups by
    (provider, model) for the per-model display lines and merges differing auth modes into
    "mixed" on the way. That grouping destroyed the api/subscription split BEFORE billing
    was computed, so a corpus with genuine API spend alongside subscription traffic on the
    same model reported **$0.00 billed**.

    Display aggregation and money must never share a code path. Caught unanimously by
    syncade's own panel — and it is the exact case the PR brief dared them to find.
    """

    def test_api_spend_alongside_subscription_on_the_same_model(self) -> None:
        from syncade.metrics.schema import ActorStatRow, open_db, upsert_actor_stat

        conn = open_db(":memory:")
        common = {"provider": "openai", "model": "gpt-5.5", "tokens": 100}
        # same provider AND model -- the display layer groups these into one row
        upsert_actor_stat(
            conn,
            ActorStatRow(
                run_id="R1",
                role="reviewer",
                name="paid",
                cost_usd=50.0,
                cost_source="estimated",
                auth_mode="api",
                **common,
            ),
        )
        upsert_actor_stat(
            conn,
            ActorStatRow(
                run_id="R1",
                role="reviewer",
                name="free",
                cost_usd=20.0,
                cost_source="estimated",
                auth_mode="subscription",
                **common,
            ),
        )

        b = _billing_totals(conn)
        billed, api_equiv, unclassified = b.billed, b.api_equiv, b.unclassified

        assert billed == 50.0, "REAL API SPEND WAS SILENTLY UNREPORTED"
        assert api_equiv == 70.0
        assert unclassified == 0.0, "nothing here is unknown; both modes were recorded"


class TestResumePreservesTheBillingClassification:
    """Resume rehydrated persisted usage WITHOUT auth_mode, rewriting every completed
    round as "unknown". The dollars came back; the classification did not — so a
    fully-classified run became unclassified just by being resumed. The artifact on disk
    was right, the reload destroyed it. Caught unanimously."""

    def test_persisted_api_mode_survives_a_reload(self) -> None:
        from syncade.usage import usage_from_fields

        u = usage_from_fields(1000, 12.5, "estimated", model="m", auth_mode="api")
        assert u.auth_mode == "api"
        assert u.billed_usd == 12.5, "real money would have vanished on resume"

    def test_persisted_subscription_mode_survives_a_reload(self) -> None:
        from syncade.usage import usage_from_fields

        u = usage_from_fields(1000, 12.5, "estimated", model="m", auth_mode="subscription")
        assert u.billed_usd == 0.0, "would have been reported as unclassified instead of $0"

    def test_a_legacy_row_with_no_auth_mode_stays_unknown(self) -> None:
        from syncade.usage import usage_from_fields

        assert usage_from_fields(1000, 12.5, "estimated", model="m").auth_mode == "unknown"


class TestBilledIsNeverSilentlyALowerBound:
    """API traffic whose model is not in the pricing table has cost_usd = NULL. SQL SUM()
    coalesces that to 0, so `billed` silently UNDERSTATED REAL MONEY — 500k tokens of real
    API spend rendered as "$0.00 billed". It is not zero, it is unknown.

    Caught by the panel. `billed` now says AT LEAST when it cannot see all of it."""

    def test_unpriced_api_traffic_makes_billed_a_lower_bound(self) -> None:
        from syncade.metrics.schema import ActorStatRow, open_db, upsert_actor_stat

        conn = open_db(":memory:")
        upsert_actor_stat(
            conn,
            ActorStatRow(
                run_id="R",
                role="reviewer",
                name="rv",
                provider="openai",
                model="unpriced",
                tokens=500_000,
                cost_usd=None,
                cost_incomplete_tokens=500_000,
                cost_source="unknown",
                auth_mode="api",
            ),
        )
        upsert_actor_stat(
            conn,
            ActorStatRow(
                run_id="R",
                role="synthesizer",
                name="s",
                provider="openai",
                model="gpt-5.5",
                tokens=1000,
                cost_usd=3.0,
                cost_source="estimated",
                auth_mode="api",
            ),
        )
        b = _billing_totals(conn)
        billed, unpriced_tok = b.billed, b.billed_unpriced_tokens
        assert billed == 3.0
        assert unpriced_tok == 500_000, "the unpriced API tokens were not tracked"

        out = "\n".join(_spend_lines(501_000, 3.0, 0, _billing_totals(conn)))
        assert "AT LEAST" in out, "a lower bound was presented as a total"
        assert "real spend is HIGHER" in out

    def test_fully_priced_api_traffic_is_not_hedged(self) -> None:
        """The hedge must appear only when it is true, or it becomes noise."""
        from syncade.metrics.schema import ActorStatRow, open_db, upsert_actor_stat

        conn = open_db(":memory:")
        upsert_actor_stat(
            conn,
            ActorStatRow(
                run_id="R",
                role="reviewer",
                name="rv",
                provider="openai",
                model="gpt-5.5",
                tokens=1000,
                cost_usd=3.0,
                cost_source="estimated",
                auth_mode="api",
            ),
        )
        out = "\n".join(_spend_lines(1000, 3.0, 0, _billing_totals(conn)))
        assert "billed:     $3.00" in out
        assert "AT LEAST" not in out


class TestSummaryMdTellsTheSameTruthAsMetrics:
    """`summary.md` still rendered `Usage.cost_usd` as a dollar total with no reference to
    auth_mode, so a subscription run printed "Total: $0.1426" for money nobody spent —
    re-introducing the exact bug ONE SURFACE OVER from the one I had just fixed.

    Fixing --metrics and leaving the per-round artifact lying is not fixing it."""

    def test_subscription_run_shows_zero_billed(self) -> None:
        from syncade.persistence.run_summary import _cost_section

        u = Usage(
            "claude-sonnet-4-6",
            1000,
            100,
            cost_usd=0.1426,
            cost_source="provider",
            auth_mode="subscription",
        )
        dispatch = type(
            "D",
            (),
            {
                "results": [
                    type("R", (), {"usage": u, "reviewer_name": "rv", "provider": "anthropic"})()
                ]
            },
        )()
        out = "\n".join(_cost_section(dispatch, None, None))
        assert "**billed:** $0.0000" in out, "summary.md called a valuation `spend`"
        assert "API-equiv" in out, "the valuation must still be shown, just labelled"
        assert "auth=subscription" in out


class TestUnpricedUnknownAuthTrafficIsNotFree:
    """`billed: $0.00 (money that left your account)` was printed for 500k UNPRICED tokens
    of UNKNOWN auth — no hedge at all. It reads as "you spent nothing" when the truth is
    "we can price none of it and classify none of it."

    Same "unknown is not zero" lie, one row-class over. The unpriced tokens are tracked per
    class now: unpriced API tokens make `billed` a LOWER BOUND; unpriced unknown tokens are
    named in `unclassed`."""

    def test_unpriced_unknown_traffic_is_named(self) -> None:
        from syncade.metrics.schema import ActorStatRow, open_db, upsert_actor_stat

        conn = open_db(":memory:")
        upsert_actor_stat(
            conn,
            ActorStatRow(
                run_id="R",
                role="reviewer",
                name="rv",
                provider="openai",
                model="unpriced",
                tokens=500_000,
                cost_usd=None,
                cost_incomplete_tokens=500_000,
                cost_source="unknown",
                auth_mode="unknown",
            ),
        )
        b = _billing_totals(conn)
        assert b.unclassified_unpriced_tokens == 500_000

        out = "\n".join(_spend_lines(500_000, None, 0, b))
        assert "unclassed:" in out, "500k unpriced unknown tokens rendered as simply free"
        assert "500000 unpriced tokens" in out
