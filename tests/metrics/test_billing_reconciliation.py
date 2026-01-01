"""Billing RECONCILIATION across the two cost tables (PR-v2-24, split from test_cost_honesty).

runs is authoritative; actor_stats is the derived per-auth breakdown. These pin that cost
and tokens present in one table but not the other are reconciled (never dropped, never
double-counted), and that the top --metrics line agrees with the billing block.

Original header:

Cost is money only when it was actually billed (PR-v2-24, issue 4).

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

from syncade.cli.metrics_mode import _billing_totals, _spend_lines


class TestTheWriterCannotDestroyTheSplitEither:
    """Round 2 fixed the READER (`_billing_totals` reads the raw table). Round 3's panel
    pointed out the WRITER still destroyed the split: `actor_stats` was keyed on
    (run_id, role, name, provider, model) with auth_mode as a merged attribute, so one
    actor that ran under two modes — a run RESUMED with a changed env — collapsed to
    "mixed" and its real API spend became unclassified before billing ever saw it.

    auth_mode is now part of the PRIMARY KEY. Two modes produce two rows. The merge is not
    avoided, it is impossible.
    """

    def test_auth_mode_is_part_of_the_primary_key(self) -> None:
        from syncade.metrics.schema import open_db

        conn = open_db(":memory:")
        pk = [r[1] for r in conn.execute("PRAGMA table_info(actor_stats)") if r[5]]
        assert "auth_mode" in pk, "the schema still lets one actor's two modes collapse"

    def test_one_actor_two_modes_keeps_both_dollars(self) -> None:
        from syncade.metrics.schema import ActorStatRow, open_db, upsert_actor_stat

        conn = open_db(":memory:")
        for mode, cost in (("api", 50.0), ("subscription", 20.0)):
            upsert_actor_stat(
                conn,
                ActorStatRow(
                    run_id="R1",
                    role="reviewer",
                    name="rv",
                    provider="openai",
                    model="gpt-5.5",
                    tokens=100,
                    cost_usd=cost,
                    cost_source="estimated",
                    auth_mode=mode,
                ),
            )
        b = _billing_totals(conn)
        billed, api_equiv, unclassified = b.billed, b.api_equiv, b.unclassified
        assert billed == 50.0, "REAL API SPEND collapsed into 'mixed' and vanished"
        assert api_equiv == 70.0
        assert unclassified == 0.0


class TestUnpricedTokensComeFromTheColumnThatTracksThem:
    """`CASE WHEN cost_usd IS NULL THEN tokens` was the wrong test.

    An `actor_stats` row AGGREGATES several rounds, so one row can carry BOTH a real cost
    AND unpriced tokens — a priced round plus an unpriced round. The NULL test then sees a
    non-null cost and reports `billed` as COMPLETE when it is not: $3.00 billed, 500k
    unpriced API tokens silently hidden.

    The schema has carried `cost_incomplete_tokens` for exactly this since PR-v2-04. I
    invented a worse proxy instead of reading the column that already existed."""

    def test_a_row_with_both_a_cost_and_unpriced_tokens(self) -> None:
        from syncade.metrics.schema import ActorStatRow, open_db, upsert_actor_stat

        conn = open_db(":memory:")
        upsert_actor_stat(
            conn,
            ActorStatRow(
                run_id="R",
                role="reviewer",
                name="rv",
                provider="openai",
                model="m",
                tokens=600_000,
                cost_usd=3.0,
                cost_incomplete_tokens=500_000,
                cost_source="estimated",
                auth_mode="api",
            ),
        )
        b = _billing_totals(conn)
        assert b.billed == 3.0
        assert b.billed_unpriced_tokens == 500_000, (
            "500k unpriced API tokens were HIDDEN because cost_usd happened to be non-null"
        )
        assert "AT LEAST" in "\n".join(_spend_lines(600_000, 3.0, 500_000, b))


class TestRunLevelCostIsNeverDroppedWhenActorStatsIsAbsent:
    """`runs.cost_usd` is the authoritative run-level total; `_billing_totals` reads
    `actor_stats`. A run can persist a `runs` cost with NO actor_stats rows (legacy runs; a
    run whose per-actor aggregation produced nothing) -- and that cost then vanished from
    billing entirely: $42 in `runs`, $0 across billed/api-equiv/unclassed.

    It has no auth_mode, so its honest home is `unclassified` -- unknown-but-real, never
    dropped. Same two-sources-of-truth bug; the fix makes the tables agree."""

    def test_runs_cost_without_actor_stats_lands_in_unclassified(self) -> None:
        from syncade.metrics.schema import RunRow, open_db, upsert_run

        conn = open_db(":memory:")
        upsert_run(conn, RunRow(run_id="R", verdict="SHIP", tokens=1_000_000, cost_usd=42.0))
        b = _billing_totals(conn)
        assert b.unclassified == 42.0, "run-level cost vanished when actor_stats was empty"
        assert b.api_equiv == 42.0, "and the API-equivalent total must reflect it"
        assert b.billed == 0.0

    def test_the_normal_case_where_tables_agree_is_untouched(self) -> None:
        """actor_stats is DERIVED from the same manifests, so normally it sums to
        runs.cost_usd and the reconciliation is a no-op."""
        from syncade.metrics.schema import (
            ActorStatRow,
            RunRow,
            open_db,
            upsert_actor_stat,
            upsert_run,
        )

        conn = open_db(":memory:")
        upsert_run(conn, RunRow(run_id="R", verdict="SHIP", tokens=100, cost_usd=10.0))
        upsert_actor_stat(
            conn,
            ActorStatRow(
                run_id="R",
                role="reviewer",
                name="rv",
                provider="openai",
                model="m",
                tokens=100,
                cost_usd=10.0,
                cost_source="estimated",
                auth_mode="api",
            ),
        )
        b = _billing_totals(conn)
        assert b.billed == 10.0
        assert b.unclassified == 0.0, "the reconciliation double-counted a run that agreed"


class TestRunLevelTOKENSAreReconciledToo:
    """Round 13 reconciled runs COST against actor_stats but left TOKENS. A run with 1M
    tokens and no cost and no actor_stats then still read as free -- same bug, one column
    over. Both axes are matched now."""

    def test_runs_tokens_without_cost_or_actor_stats_are_named(self) -> None:
        from syncade.metrics.schema import RunRow, open_db, upsert_run

        conn = open_db(":memory:")
        upsert_run(conn, RunRow(run_id="R", verdict="SHIP", tokens=1_000_000, cost_usd=None))
        b = _billing_totals(conn)
        assert b.unclassified_unpriced_tokens == 1_000_000, "1M real tokens vanished"
        out = "\n".join(_spend_lines(1_000_000, None, 0, b))
        assert "unclassed" in out and "1000000 unpriced tokens" in out

    def test_agreeing_tables_do_not_double_count_tokens(self) -> None:
        from syncade.metrics.schema import (
            ActorStatRow,
            RunRow,
            open_db,
            upsert_actor_stat,
            upsert_run,
        )

        conn = open_db(":memory:")
        upsert_run(conn, RunRow(run_id="R", verdict="SHIP", tokens=100, cost_usd=10.0))
        upsert_actor_stat(
            conn,
            ActorStatRow(
                run_id="R",
                role="reviewer",
                name="rv",
                provider="openai",
                model="m",
                tokens=100,
                cost_usd=10.0,
                cost_source="estimated",
                auth_mode="api",
            ),
        )
        b = _billing_totals(conn)
        assert b.unclassified_unpriced_tokens == 0, "the token reconciliation double-counted"


class TestTopLineIncompleteTokensMatchTheBillingBlock:
    """The top `tokens:` line and the billing block both report cost-incomplete tokens.
    `_cost_incomplete_tokens` summed only actor_stats when any rows existed, so a mixed
    corpus (current rows + a legacy run-level-only row) undercounted the top line while the
    billing block reported the full count. No money misstated -- but two lines in one report
    disagreeing is the same two-sources bug. Both derive from one computation now."""

    def test_mixed_corpus_top_line_equals_billing_block(self) -> None:
        from syncade.cli.metrics_mode import _cost_incomplete_tokens
        from syncade.metrics.schema import (
            ActorStatRow,
            RunRow,
            open_db,
            upsert_actor_stat,
            upsert_run,
        )

        conn = open_db(":memory:")
        upsert_run(conn, RunRow(run_id="R1", verdict="SHIP", tokens=300, cost_usd=1.0))
        upsert_actor_stat(
            conn,
            ActorStatRow(
                run_id="R1",
                role="reviewer",
                name="rv",
                provider="openai",
                model="m",
                tokens=300,
                cost_usd=1.0,
                cost_incomplete_tokens=200,
                cost_source="estimated",
                auth_mode="api",
            ),
        )
        upsert_run(conn, RunRow(run_id="R2", verdict="SHIP", tokens=1_000_000, cost_usd=None))

        b = _billing_totals(conn)
        top = _cost_incomplete_tokens(conn)
        assert top == b.billed_unpriced_tokens + b.unclassified_unpriced_tokens
        assert top == 1_000_200, "the top line undercounted the legacy run-level tokens"


class TestSubscriptionUnpricedTokensAreNotDropped:
    """`from_rows` tracked unpriced tokens only for `api` and unknown modes. A SUBSCRIPTION
    actor with unpriced tokens (cost_usd NULL) was tracked by NEITHER bucket, so it rendered
    `billed: $0` with no signal at all. billed IS $0 (subscription is $0 marginal) -- but the
    API-EQUIVALENT valuation of those tokens is unknown, so API-equiv is a lower bound.

    total_unpriced_tokens (all classes) carries this now."""

    def test_subscription_unpriced_makes_api_equiv_a_lower_bound(self) -> None:
        from syncade.metrics.schema import (
            ActorStatRow,
            RunRow,
            open_db,
            upsert_actor_stat,
            upsert_run,
        )

        conn = open_db(":memory:")
        upsert_run(conn, RunRow(run_id="R", verdict="SHIP", tokens=500_000, cost_usd=None))
        upsert_actor_stat(
            conn,
            ActorStatRow(
                run_id="R",
                role="reviewer",
                name="rv",
                provider="openai",
                model="m",
                tokens=500_000,
                cost_usd=None,
                cost_incomplete_tokens=500_000,
                cost_source="unknown",
                auth_mode="subscription",
            ),
        )
        b = _billing_totals(conn)
        assert b.total_unpriced_tokens == 500_000, "subscription unpriced tokens vanished"
        out = "\n".join(_spend_lines(500_000, None, 0, b))
        assert "LOWER BOUND" in out, "no signal that the api-equivalent valuation is incomplete"
        assert b.billed == 0.0, "subscription is still $0 billed"


class TestReconciliationCountsOnlyUNREPRESENTEDRuns:
    """The reconciliation must reconcile ONLY runs with no actor_stats row. A represented
    run is trusted entirely to actor_stats -- its cost_incomplete_tokens are finer than the
    run-level total (a run can have a KNOWN cost yet some cost-incomplete tokens), so an
    aggregate `runs - actor_stats` delta subtracts across different populations.

    Found by MUTATING my own round-13 reconciliation: the aggregate-delta version returned
    1,000,000 for a mixed corpus whose true count is 1,000,200."""

    def test_incomplete_tokens_in_a_priced_run_are_not_lost_to_the_delta(self) -> None:
        from syncade.metrics.schema import (
            ActorStatRow,
            RunRow,
            open_db,
            upsert_actor_stat,
            upsert_run,
        )

        conn = open_db(":memory:")
        # a PRICED run (cost known) that nonetheless has 200 cost-incomplete tokens
        upsert_run(conn, RunRow(run_id="R1", verdict="SHIP", tokens=300, cost_usd=1.0))
        upsert_actor_stat(
            conn,
            ActorStatRow(
                run_id="R1",
                role="reviewer",
                name="rv",
                provider="openai",
                model="m",
                tokens=300,
                cost_usd=1.0,
                cost_incomplete_tokens=200,
                cost_source="estimated",
                auth_mode="api",
            ),
        )
        # an UNREPRESENTED legacy run: 1M tokens, no cost, no actor_stats
        upsert_run(conn, RunRow(run_id="R2", verdict="SHIP", tokens=1_000_000, cost_usd=None))

        b = _billing_totals(conn)
        assert b.total_unpriced_tokens == 1_000_200, (
            "the aggregate delta subtracted R1's in-run incomplete tokens from R2's shortfall"
        )

    def test_a_priced_unrepresented_run_adds_cost_but_no_unpriced_tokens(self) -> None:
        from syncade.metrics.schema import RunRow, open_db, upsert_run

        conn = open_db(":memory:")
        upsert_run(conn, RunRow(run_id="R", verdict="SHIP", tokens=1000, cost_usd=5.0))
        b = _billing_totals(conn)
        assert b.unclassified == 5.0, "the known run-level cost must surface"
        assert b.unclassified_unpriced_tokens == 0, (
            "a run whose cost IS known must not also mark its tokens unpriced (double signal)"
        )
