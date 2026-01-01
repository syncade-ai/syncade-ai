"""The ONE place that decides whether a dollar is money.

**This module exists because I got the same thing wrong eight times.**

``cost_usd`` is an API-EQUIVALENT VALUATION, not spend. It is fiction whenever the call
rode a subscription, and that is orthogonal to ``cost_source`` (``claude`` reports
``total_cost_usd`` even on an OAuth session). Only ``auth_mode`` knows.

Syncade renders that judgement on TWO surfaces — ``--metrics`` and each round's
``summary.md`` — and every single time I fixed one, I left the other telling the original
lie. Not once: repeatedly, over four review rounds, in the same PR:

    round 5  --metrics learned billed-vs-valuation.   summary.md still said "Total: $0.14".
    round 7  --metrics learned unpriced API = lower bound. summary.md did not.
    round 8  --metrics learned unpriced+unknown != free.   summary.md did not.

Each fix was correct. Each was applied to one of two twins. The bug was never the logic —
it was that the logic lived in two places, so "fixed" only ever meant "fixed here".

So the rules and the WORDS both live here now, and both surfaces call this. They cannot
disagree, because there is no longer anywhere for them to disagree from.

The rules, once:

- **billed**       — ``auth_mode == "api"``. Money that actually left the account.
- **api_equiv**    — every priced token, however it was paid for. What the traffic would
                     cost at API list price; on a subscription that is what the plan is
                     worth, not what was spent.
- **unclassified** — auth mode unrecorded. NEVER folded into either: we do not know, and
                     guessing is what produced the original $71.18-of-nothing.
- **unpriced tokens** are tracked PER CLASS, because ``SUM()`` coalesces a NULL cost to 0
  and a zero here is a claim, not a fact. Unpriced API tokens make ``billed`` a LOWER
  BOUND. Unpriced unknown tokens must not read as free.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple, Protocol

_KNOWN = ("api", "subscription")


class _HasUsage(Protocol):
    auth_mode: str
    cost_usd: float | None
    total_tokens: int


class Billing(NamedTuple):
    """The numbers that must never be conflated, plus the two that qualify them."""

    billed: float = 0.0
    api_equiv: float = 0.0
    unclassified: float = 0.0
    billed_unpriced_tokens: int = 0
    unclassified_unpriced_tokens: int = 0
    # ALL unpriced tokens, every auth class. billed_unpriced (api) and
    # unclassified_unpriced (unknown) are subsets; the remainder is SUBSCRIPTION-mode
    # unpriced, which was tracked by neither and so vanished from the report entirely.
    total_unpriced_tokens: int = 0


def from_rows(rows: Iterable[tuple[str, float, int]]) -> Billing:
    """``(auth_mode, summed_cost, summed_unpriced_tokens)`` → :class:`Billing`.

    The shape ``--metrics`` gets from SQL (``GROUP BY auth_mode``, never from display
    aggregates — grouping for display once merged two modes into "mixed" and made real API
    spend vanish).
    """
    rows = list(rows)
    return Billing(
        billed=sum(c for mode, c, _t in rows if mode == "api"),
        api_equiv=sum(c for _mode, c, _t in rows),
        unclassified=sum(c for mode, c, _t in rows if mode not in _KNOWN),
        billed_unpriced_tokens=sum(t for mode, _c, t in rows if mode == "api"),
        unclassified_unpriced_tokens=sum(t for mode, _c, t in rows if mode not in _KNOWN),
        total_unpriced_tokens=sum(t for _mode, _c, t in rows),
    )


def from_usages(usages: Iterable[_HasUsage]) -> Billing:
    """Live :class:`~syncade.usage.Usage` records → :class:`Billing`.

    The shape ``summary.md`` gets. Same rules as :func:`from_rows` — because they ARE the
    same rules, and keeping them in one function is the entire point of this module.
    """
    # Read Usage.cost_incomplete_tokens -- the SAME rule the DB column is written from --
    # rather than re-deriving "priced" here. A non-null cost_usd is NOT sufficient: a retry
    # (_add_usage) can carry a partial cost with cost_source="unknown", and treating that as
    # fully priced dropped the lower-bound hedge on summary.md.
    rows = [(u.auth_mode, u.cost_usd or 0.0, u.cost_incomplete_tokens) for u in usages]
    return from_rows(rows)


def render(b: Billing, *, indent: str = "  ", bullet: bool = False) -> list[str]:
    """The billing block, worded once.

    Both surfaces render from here. When the wording of "what is money" changes, it changes
    in one place — which is what stops ``summary.md`` from quietly saying "Total: $0.14"
    for a run that cost nothing while ``--metrics`` says $0.00 billed.
    """

    # `bullet` = markdown context (summary.md). Plain = terminal (--metrics). Only the
    # decoration differs; the NUMBERS and the CLAIMS are identical by construction.
    def label(text: str) -> str:
        return f"{indent}- **{text}:**" if bullet else f"{indent}{text + ':':<11}"

    lines: list[str] = []

    billed_note = "(money that left your account)"
    if b.billed_unpriced_tokens:
        # A lower bound is never presented as a total.
        billed_note = (
            f"(AT LEAST — {b.billed_unpriced_tokens} API tokens are unpriced, "
            f"so real spend is HIGHER)"
        )
    lines.append(f"{label('billed')} ${b.billed:.4f}  {billed_note}")

    # Unpriced tokens that are neither api (surfaced on `billed`) nor unknown (surfaced on
    # `unclassed`) -- i.e. SUBSCRIPTION-mode unpriced. billed is genuinely $0 for these
    # (subscription is $0 marginal), but their API-EQUIVALENT valuation is unknown, so
    # API-equiv is a lower bound. This was the dropped case: billed $0, no signal at all.
    sub_unpriced = (
        b.total_unpriced_tokens - b.billed_unpriced_tokens - b.unclassified_unpriced_tokens
    )
    if b.api_equiv > b.billed or sub_unpriced:
        api_note = "(same traffic at API list price; subscription traffic is $0 marginal"
        if sub_unpriced:
            api_note += f"; {sub_unpriced} tokens unpriced, so this is a LOWER BOUND"
        lines.append(f"{label('API-equiv')} ${b.api_equiv:.4f}  {api_note})")

    if b.unclassified or b.unclassified_unpriced_tokens:
        detail = f"${b.unclassified:.4f}"
        if b.unclassified_unpriced_tokens:
            detail += f" + {b.unclassified_unpriced_tokens} unpriced tokens"
        lines.append(
            f"{label('unclassed')} {detail}  "
            f"(auth mode not recorded — cannot say whether this was billed)"
        )
    return lines
