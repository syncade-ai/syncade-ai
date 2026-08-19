"""``syncade --metrics`` mode handler.

A one-shot, read-only pass: aggregate the ``.syncade/runs/`` artifact corpus
into ``.syncade/metrics.db`` (idempotent) and print a cumulative report. Split
into its own module so :mod:`syncade.cli.modes` stays under the blocking
file-length cap; re-exported from ``modes`` so the public import path is
unchanged. Never mutates run artifacts and never touches the review path.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from syncade import billing
from syncade.billing import Billing
from syncade.exit_codes import SUCCESS, WORKTREE_ERROR
from syncade.metrics.aggregate import backfill
from syncade.metrics.findings import blocker_curve, consensus_stats
from syncade.metrics.schema import open_db
from syncade.snapshot import SnapshotError, discover_repo_root


def _spend_lines(
    tokens: object,
    cost: object,
    incomplete_tokens: object,
    totals: Billing,
) -> list[str]:
    """The honest spend block.

    ``billed`` is money. ``API-equiv`` is a VALUATION of the same traffic at API list
    price -- worth showing (it is what a subscription is saving you) but it is NOT spend
    and must never be labelled as such. Whatever cannot be classified is named, never
    quietly counted as free.

    Every pre-existing honesty signal is preserved deliberately: "usage unavailable" must
    NOT collapse to "0 tokens" (unknown is not zero), and cost-incomplete tokens are still
    flagged "not free". Replacing one lie with another would be a poor trade.
    """
    if tokens is None and cost is None and not incomplete_tokens:
        return ["  usage:      unavailable (legacy/no usage recorded)"]

    b = totals
    tok = int(tokens or 0)
    incomplete = int(incomplete_tokens or 0)
    tok_line = f"  tokens:     {tok}"
    if incomplete:
        tok_line += f"  ({incomplete} cost-incomplete tokens — priced at $0, but NOT free)"
    lines = [tok_line]
    lines.extend(billing.render(b, indent="  "))
    return lines


def _usd(value: float) -> str:
    if 0 < value < 0.005:
        return "<$0.01"
    return f"${value:.2f}"


def _merge_auth_mode(current: str, incoming: str) -> str:
    """Aggregate auth modes for one actor/model. Two different modes -> "mixed"."""
    if not incoming:
        return current
    if current in ("", incoming):
        return incoming
    return "mixed"


def _billing_totals(conn: sqlite3.Connection, run_ids: list[str] | None = None) -> Billing:
    """``(billed, api_equivalent, unclassified)`` in USD.

    THE distinction this whole PR exists for. ``cost_usd`` is an API-EQUIVALENT
    VALUATION, not money: it is fiction whenever the call rode a subscription. Before
    this, ``--metrics`` summed it and called the result "spend" — reporting $71.18
    across a corpus whose real marginal cost was ~$0, because every call ran on a
    ChatGPT plan or a claude.ai plan.

    - ``billed``       — auth_mode == "api". Money that actually left the account.
    - ``api_equiv``    — every priced token, regardless of how it was paid for. Still
                         useful: it is what the traffic WOULD cost on API pricing, and
                         it is the number that tells a subscription user what their plan
                         is worth.
    - ``unclassified`` — auth mode not recorded (runs from before this PR). Reported
                         separately and NEVER folded into either: we do not know, and
                         guessing is what got us here.

    **Computed from the RAW table, never from display rows.** The first version summed
    ``_actor_spend_rows()``, which groups by ``(provider, model)`` for the per-model
    lines and merges differing auth modes into ``"mixed"`` on the way. That grouping
    destroyed the api/subscription split BEFORE billing was computed, so a corpus with
    genuine API spend alongside subscription traffic on the same model reported
    **$0.00 billed** — real money, silently unreported. Caught by syncade's own panel,
    unanimously; it is the exact case the PR brief dared them to find.

    Display aggregation and money must never share a code path. SQL, grouped by
    auth_mode, no collapsing.
    """
    where = ""
    params: list[str] = []
    if run_ids:
        where = f"WHERE run_id IN ({','.join('?' * len(run_ids))})"
        params = run_ids
    rows = conn.execute(
        # cost_incomplete_tokens, NOT `CASE WHEN cost_usd IS NULL`. A single actor_stats
        # row aggregates several rounds, so it can carry BOTH a real cost AND unpriced
        # tokens -- one priced round plus one unpriced round. The NULL test then sees a
        # non-null cost and reports `billed` as complete when it is not. The schema has
        # carried cost_incomplete_tokens for exactly this since PR-v2-04; I invented a
        # worse proxy instead of reading it.
        "SELECT auth_mode, COALESCE(SUM(cost_usd), 0), "
        "COALESCE(SUM(cost_incomplete_tokens), 0) "
        f"FROM actor_stats {where} GROUP BY auth_mode",
        params,
    ).fetchall()
    b = billing.from_rows(rows)

    # Reconcile against `runs`, the authoritative run-level total. actor_stats is DERIVED
    # from the same manifests, so normally it sums to runs and the delta is 0. But a run can
    # persist a `runs` row with NO actor_stats rows (legacy runs; a run whose per-actor
    # aggregation produced nothing) -- and then its usage vanished from billing entirely.
    # Whatever `runs` has that actor_stats does not represent has no auth_mode, so its honest
    # home is `unclassified`: unknown-but-real, never dropped.
    #
    # BOTH axes must be reconciled. Round 13 reconciled COST and left TOKENS -- a run with
    # 1M tokens and no cost then still read as free. Same bug, one column over. `runs`
    # carries both; both are matched here.
    # Reconcile ONLY runs with NO actor_stats row at all. A represented run is trusted
    # entirely to actor_stats -- crucially, its cost_incomplete_tokens are finer-grained than
    # runs (a run can have a KNOWN total cost yet some cost-incomplete tokens), so an
    # aggregate `runs - actor_stats` delta subtracts across different populations and is
    # wrong. Per-unrepresented-run is the clean unit: cost known-but-unauthed -> unclassified
    # cost; tokens with no cost -> unclassified unpriced. (Found by mutating my own round-13
    # reconciliation, which used the aggregate delta.)
    unrep = f"{'AND' if where else 'WHERE'} run_id NOT IN (SELECT run_id FROM actor_stats)"
    unrep_cost, unrep_unpriced_tokens = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0), "
        "COALESCE(SUM(CASE WHEN cost_usd IS NULL THEN tokens ELSE 0 END), 0) "
        f"FROM runs {where} {unrep}",
        params,
    ).fetchone()
    if unrep_cost or unrep_unpriced_tokens:
        b = b._replace(
            api_equiv=b.api_equiv + unrep_cost,
            unclassified=b.unclassified + unrep_cost,
            unclassified_unpriced_tokens=b.unclassified_unpriced_tokens + unrep_unpriced_tokens,
            total_unpriced_tokens=b.total_unpriced_tokens + unrep_unpriced_tokens,
        )
    return b


def _merge_cost_source(current: str, incoming: str) -> str:
    if not incoming:
        return current
    if incoming == "unknown" or current == "unknown":
        return "unknown"
    if current in ("", incoming):
        return incoming
    return "estimated"


def _actor_spend_rows(
    conn: sqlite3.Connection, run_ids: list[str] | None = None
) -> list[tuple[str, str, int, float, int, str, str]]:
    where = ""
    params: list[str] = []
    if run_ids:
        where = f"WHERE run_id IN ({','.join('?' * len(run_ids))})"
        params = run_ids
    rows = conn.execute(
        "SELECT provider, model, role, tokens, cost_usd, cost_incomplete_tokens, cost_source, "
        f"auth_mode FROM actor_stats {where}",
        params,
    ).fetchall()
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for provider, model, role, tokens, cost, incomplete_tokens, source, auth in rows:
        key = (provider or "", model or "")
        entry = grouped.setdefault(
            key,
            {
                "tokens": 0,
                "cost": 0.0,
                "incomplete": 0,
                "source": "",
                "auth": "",
                "roles": set(),
            },
        )
        tok = int(tokens or 0)
        entry["tokens"] = int(entry["tokens"]) + tok
        entry["cost"] = float(entry["cost"]) + float(cost or 0.0)
        entry["incomplete"] = int(entry["incomplete"]) + int(incomplete_tokens or 0)
        entry["source"] = _merge_cost_source(str(entry["source"]), source or "")
        entry["auth"] = _merge_auth_mode(str(entry["auth"]), auth or "")
        if role:
            entry["roles"].add(role)
    out = []
    for (provider, model), entry in grouped.items():
        roles = ",".join(sorted(entry["roles"]))
        out.append(
            (
                provider,
                model,
                int(entry["tokens"]),
                float(entry["cost"]),
                int(entry["incomplete"]),
                str(entry["source"]),
                roles,
                str(entry["auth"]),
            )
        )
    return sorted(out, key=lambda r: (r[0], r[1]))


def _cost_incomplete_tokens(conn: sqlite3.Connection, run_ids: list[str] | None = None) -> int:
    """Total tokens whose cost we could not establish, for the top ``tokens:`` line.

    Derived from :func:`_billing_totals` -- the SAME reconciliation the billing block uses --
    so the two CANNOT disagree. The old version summed only ``actor_stats`` whenever any
    actor_stats row existed, so a mixed corpus (current rows + a legacy run-level-only row)
    undercounted the top line while the billing block reported the full count. No money was
    misstated, but two lines in one report disagreeing is the same two-sources bug; the fix
    is the same -- one computation.
    """
    return _billing_totals(conn, run_ids).total_unpriced_tokens


def render_report(conn: sqlite3.Connection, last_n: int | None = None) -> str:
    """Render the cumulative metrics report from the DB (pure). When ``last_n`` is
    set, append a billed / API-equivalent / unclassified breakdown scoped to the
    ``last_n`` most recent runs (see :mod:`syncade.billing`)."""
    runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    if not runs:
        return "syncade metrics — no runs recorded yet"

    ship = conn.execute("SELECT COUNT(*) FROM runs WHERE verdict = 'SHIP'").fetchone()[0]
    blockers, minors, nits, dismissed = conn.execute(
        "SELECT COALESCE(SUM(blockers),0), COALESCE(SUM(minors),0), COALESCE(SUM(nits),0), "
        "COALESCE(SUM(dismissed),0) FROM runs"
    ).fetchone()
    # From `rounds`, not `runs.rounds_executed`: the latter comes from loop-manifest.json, so an
    # interrupted run with perfectly good round artifacts reported "rounds: 0 total" while the
    # blocker curve simultaneously knew round 0 had happened.
    rounds = conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
    handoffs, decisions = conn.execute(
        "SELECT COALESCE(SUM(handoff),0), COALESCE(SUM(decision_needed),0) FROM runs"
    ).fetchone()
    p_committed, p_stalled, p_errors, p_escalated = conn.execute(
        "SELECT COALESCE(SUM(producer_commits),0), COALESCE(SUM(producer_stalled),0), "
        "COALESCE(SUM(producer_errors),0), COALESCE(SUM(producer_escalated),0) FROM runs"
    ).fetchone()
    total_tokens, total_cost = conn.execute(
        "SELECT SUM(tokens), SUM(cost_usd) FROM runs"
    ).fetchone()
    actor_rows = _actor_spend_rows(conn)
    incomplete_tokens = _cost_incomplete_tokens(conn)

    lines = [
        f"syncade metrics — {runs} runs",
        f"  ship-rate:  {ship}/{runs} ({round(100 * ship / runs)}%)",
        f"  findings:   {blockers} blockers, {minors} minors, {nits} nits, {dismissed} dismissed",
        f"  rounds:     {rounds} total",
        f"  handoffs:   {handoffs}    decisions: {decisions}",
        f"  producer:   committed={p_committed} stalled={p_stalled}"
        f" errors={p_errors} escalated={p_escalated}",
    ]
    lines.extend(_spend_lines(total_tokens, total_cost, incomplete_tokens, _billing_totals(conn)))
    lines.append("  verdicts:")
    # group by exit code too, so distinct unrecognized codes are not merged under one label
    for verdict, code, count in conn.execute(
        "SELECT verdict, final_exit_code, COUNT(*) FROM runs "
        "GROUP BY verdict, final_exit_code ORDER BY COUNT(*) DESC, verdict"
    ):
        label = f"{verdict or '?'} ({code if code is not None else '?'})"
        lines.append(f"    {label:20} {count}")

    if actor_rows:
        lines.append("  by model (cost = API-equivalent; see `billed` above for money):")
        for provider, model, tok, cost, incomplete, source, roles, auth in actor_rows:
            label = f"{provider}/{model}" if model else (provider or "?")
            src = f" ({source})" if source else ""
            # The auth mode is what says whether this line is money or a valuation, so it
            # is rendered right next to the dollars rather than buried in a footnote.
            auth_label = f" auth={auth}" if auth else " auth=unrecorded"
            cost_label = "known_cost" if incomplete else "cost"
            incomplete_text = f" cost-incomplete-tok={int(incomplete)}" if incomplete else ""
            lines.append(
                f"    {label:24} {cost_label}={_usd(cost)}{src}{auth_label} "
                f"tok={int(tok)} roles={roles}{incomplete_text}"
            )

    reviewer_rows = conn.execute(
        "SELECT provider, model, COALESCE(SUM(finding_count),0), COUNT(DISTINCT run_id), "
        "COALESCE(SUM(duration_s),0), COALESCE(SUM(tokens),0) "
        "FROM reviewer_stats GROUP BY provider, model ORDER BY provider, model"
    ).fetchall()
    if reviewer_rows:
        lines.append("  reviewers by model (findings/wall-clock):")
        for provider, model, findings, run_count, wall, tok in reviewer_rows:
            # A row with no model is reported as UNKNOWN, never as a bare provider. A bare
            # provider reads like a panel that had no model, which is a claim we cannot make;
            # "unknown" says the thing that is actually true. (No such row exists in this
            # corpus today — every recorded panel carries both — but the renderer must not be
            # the reason a future one silently looks answered.)
            label = f"{provider}/{model}" if model else f"{provider or '?'}/unknown"
            lines.append(
                f"    {label:24} findings={findings} runs={run_count} "
                f"wall={int(wall)}s tok={int(tok)}"
            )
        # Rounds whose panel was never recorded are INVISIBLE in the table above, which is worse
        # than being wrong: anyone dividing findings by runs gets a denominator quietly missing
        # them. They come from runs interrupted before the loop terminator wrote a manifest, so
        # nothing ever summarised their reviewers. Never guessed — the configured roster is not
        # evidence of what a killed run actually dispatched.
        #
        # DO NOT write a measured count here. This comment said "all 49" until the release audit
        # found the line below printing 22: excluding known-empty panels changed the number and
        # the prose kept the old one. A count in a comment is a measurement with no test reading
        # it, which is the same defect this release had to fix in its own CHANGELOG.

    # OUTSIDE the `if reviewer_rows` block on purpose. Nested inside it, a corpus where EVERY
    # panel is unknown printed no warning at all — the one corpus that most needs it, silent.
    # panel_source='unknown' means no manifest had a `reviewers` key for that round. This is
    # independent of blockers_source: a manifest can record a blocker count without a reviewer
    # list, and a no-dispatch round (no_changes_to_review) has panel_source='recorded' and
    # panel_size=0 — explicitly empty, not unknown.
    unknown_rounds, total_rounds = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN panel_source = 'unknown' THEN 1 ELSE 0 END), 0), "
        "COUNT(*) FROM rounds"
    ).fetchone()
    if unknown_rounds:
        lines.append(
            f"  {unknown_rounds} of {total_rounds} rounds record no reviewer panel "
            f"(no manifest evidence; excluded from consensus)"
        )
    manifest_only, unknown_only = conn.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN blockers_source='manifest' THEN 1 ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN blockers_source='unknown' THEN 1 ELSE 0 END), 0) "
        "FROM rounds "
        "WHERE NOT (panel_source='recorded' AND panel_size=0 AND blockers=0)"
    ).fetchone()
    if manifest_only:
        lines.append(
            f"  {manifest_only} of {total_rounds} rounds have no synthesizer artifact; their "
            f"blocker counts come from round manifests and carry no provenance"
        )
    if unknown_only:
        lines.append(
            f"  {unknown_only} of {total_rounds} rounds have no blocker evidence "
            f"(killed/interrupted before any artifact or manifest); "
            f"excluded from the blocker curve"
        )

    consensus = consensus_stats(conn)
    if consensus is not None:
        total, single = consensus
        lines.append(
            f"  consensus: {single} of {total} active blockers ({single / total:.0%}) were "
            f"raised by exactly ONE reviewer"
        )
        lines.append("    (over runs with a multi-reviewer panel; what a second reviewer earns)")

    curve = blocker_curve(conn)
    if curve:
        # Density, never the raw total alone. The blocker count falls steeply across rounds and
        # reads as convergence; it is SURVIVORSHIP — fewer runs reach each round, so the
        # denominator falls with it. This repo published the raw sequence as convergence once
        # and had to correct it, so the renderer shows both numbers and the per-round rate.
        lines.append(
            "  blockers by round (artifact- or manifest-backed only; "
            "killed runs with no evidence excluded):"
        )
        for round_index, blockers, rounds in curve:
            density = blockers / rounds if rounds else 0.0
            lines.append(
                f"    round {round_index}: {blockers} blockers over {rounds} rounds "
                f"({density:.2f}/round)"
            )

    if last_n:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT run_id FROM runs ORDER BY run_id DESC LIMIT ?", (last_n,)
            ).fetchall()
        ]
        if ids:
            ph = ",".join("?" * len(ids))
            w_tok, w_cost = conn.execute(
                f"SELECT SUM(tokens), SUM(cost_usd) FROM runs WHERE run_id IN ({ph})",
                ids,
            ).fetchone()
            window_actor_rows = _actor_spend_rows(conn, ids)
            window_incomplete = _cost_incomplete_tokens(conn, ids)
            # The windowed section gets the SAME honest block as the totals. Fixing the
            # top line and leaving this one summing valuations as "spend" would just move
            # the lie down the page.
            lines.append(f"  last {len(ids)} run(s):")
            lines.extend(_spend_lines(w_tok, w_cost, window_incomplete, _billing_totals(conn, ids)))
            for provider, model, tok, cost, incomplete, source, roles, auth in window_actor_rows:
                label = f"{provider}/{model}" if model else (provider or "?")
                src = f" ({source})" if source else ""
                auth_label = f" auth={auth}" if auth else " auth=unrecorded"
                cost_label = "known_cost" if incomplete else "cost"
                incomplete_text = f" cost-incomplete-tok={int(incomplete)}" if incomplete else ""
                lines.append(
                    f"    {label:24} {cost_label}={_usd(cost)}{src}{auth_label} "
                    f"tok={int(tok)} roles={roles}{incomplete_text}"
                )
    return "\n".join(lines)


def _run_metrics(args) -> int:
    """Dispatch ``syncade --metrics``: aggregate the corpus + print the report.

    Read-only over ``.syncade/runs/``; the DB is a derived, rebuildable view.
    Exit codes follow the CLI mode-handler convention (CLAUDE.md): success → 0;
    repo-discovery failure → 60 (WORKTREE_ERROR).
    """
    repo_root_hint = Path(args.repo_root).expanduser() if args.repo_root else Path.cwd()
    try:
        repo_root = discover_repo_root(repo_root_hint)
    except SnapshotError as exc:
        print(f"[syncade] snapshot error: {exc}", file=sys.stderr)
        return WORKTREE_ERROR

    try:
        conn = open_db(repo_root / ".syncade" / "metrics.db")
    except (sqlite3.Error, OSError) as exc:
        print(f"[syncade] metrics: cannot open metrics DB: {exc}", file=sys.stderr)
        return WORKTREE_ERROR
    try:
        backfill(conn, repo_root / ".syncade" / "runs")
        print(render_report(conn, getattr(args, "metrics_last", None)))
    except (sqlite3.Error, OSError) as exc:
        print(f"[syncade] metrics: aggregation failed: {exc}", file=sys.stderr)
        return WORKTREE_ERROR
    finally:
        conn.close()
    return SUCCESS
