"""The 80%-of-ceiling heads-up — PR-h-field-06 item 3.

Advisory only: it never changes a verdict or an exit code. It matters more since the ceiling
gained a DEFAULT, because without it the first thing most operators would learn about
`budget_tokens` is a run stopping.

An earlier attempt at this was abandoned on a branch and never merged. It compared
`tally >= ceiling / FRACTION` — 125% of the ceiling, which `over_budget` has already aborted
at — so it could never fire. The band tests below are written to catch exactly that: they
assert the warning fires BELOW the abort, not merely that some tally warns.
"""

from __future__ import annotations

import argparse
import subprocess

import pytest

from syncade.adapters.fake import FakeAdapter, FakeProducerAdapter, FakeSynthesizerAdapter
from syncade.config import LoopConfig, SyncadeConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from syncade.orchestrator.budget import BUDGET_WARN_FRACTION, approaching_budget, over_budget
from syncade.usage import Usage
from tests.orchestrator._helpers import _factory_returning, _no_ship, _synth_with_blocker

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def _u(tokens: int = 0, cost: float | None = None) -> Usage:
    return Usage(
        model="m",
        input_tokens=tokens,
        output_tokens=0,
        cached_input_tokens=0,
        reasoning_output_tokens=0,
        cost_usd=cost,
        cost_source="estimated" if cost is not None else "unknown",
        auth_mode="subscription",
    )


# --- the band: the whole point ----------------------------------------------------------------


@pytest.mark.parametrize(
    "tally,warns,aborts",
    [
        (700, False, False),  # below the warning line
        (800, True, False),  # AT the line — warns, does not abort
        (999, True, False),  # inside the band
        (1000, True, True),  # at the ceiling — over_budget takes over
    ],
)
def test_the_warning_band_lies_below_the_abort(tally, warns, aborts):
    """If these ever invert, the warning is unreachable — the abandoned version's exact bug."""
    loop = LoopConfig(budget_tokens=1000)
    assert (approaching_budget([_u(tokens=tally)], loop) is not None) is warns
    assert (over_budget([_u(tokens=tally)], loop) is not None) is aborts


def test_there_is_a_tally_that_warns_without_aborting():
    """States the property directly rather than relying on the table above: the band must be
    NON-EMPTY. A warning that only fires once the run has already stopped is not a warning."""
    loop = LoopConfig(budget_tokens=1000)
    at_line = int(1000 * BUDGET_WARN_FRACTION)
    assert approaching_budget([_u(tokens=at_line)], loop) is not None
    assert over_budget([_u(tokens=at_line)], loop) is None


def test_no_ceiling_no_warning():
    assert approaching_budget([_u(tokens=10**9, cost=10.0**9)], LoopConfig(budget_tokens=0)) is None


def test_the_dollar_ceiling_warns_too():
    loop = LoopConfig(budget_tokens=0, budget_usd=10.0)
    assert approaching_budget([_u(cost=8.0)], loop) == "budget_usd"
    assert over_budget([_u(cost=8.0)], loop) is None


# --- end to end -------------------------------------------------------------------------------


def _run(repo, pr_doc, monkeypatch, per_actor_tokens: int, *, rounds_of_adapters: int = 2, **loop):
    from syncade import dispatcher as disp_mod
    from syncade import producer_attempt as prod_mod
    from syncade.synthesizer import driver as synth_mod

    # producer_attempt, NOT producer: usage_for is CALLED in the attempt module since the
    # field-03 split, so a patch on `producer` silently does nothing. The Decomposition Rule.
    for mod in (disp_mod, synth_mod, prod_mod):
        monkeypatch.setattr(mod, "usage_for", lambda *a, **k: _u(tokens=per_actor_tokens))
    logger = Logger("normal")
    return run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 3, **loop},
        ),
        # The factory CONSUMES adapters in order, so a multi-round run needs two per round —
        # otherwise round 1's reviewers fail and the loop ends before the band is reached.
        adapter_factory=_factory_returning(
            *(FakeAdapter(canned_output=_no_ship()) for _ in range(2 * rounds_of_adapters))
        ),
        synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_with_blocker()),
        # A COMMITTING producer, so round 0 is not terminal and the loop reaches round 1's
        # pre-dispatch boundary — which is where the check lives. Without it the run stalls at
        # round 0 and the warning has nowhere to fire.
        producer_adapter=FakeProducerAdapter(commit_message="fix: keep the loop going"),
        logger=logger,
    )


def test_the_operator_is_warned_before_the_run_stops(repo_with_pr_doc, monkeypatch, capsys):
    """4 actors x 10k = 40k in round 0 (2 reviewers, judge, producer). A 50k ceiling puts the
    round-1 boundary inside the band — 40k >= 40k warns, 40k < 50k does not abort — which is
    the sequence the whole item exists to produce."""
    repo, pr_doc = repo_with_pr_doc
    _run(repo, pr_doc, monkeypatch, 10_000, budget_tokens=50_000)
    out = "".join(capsys.readouterr())  # warnings go to stderr; read both streams
    assert "approaching your token ceiling" in out
    assert "left" in out, "the warning must state the remaining headroom, not just that it is near"


def test_the_warning_fires_once_not_once_per_round(repo_with_pr_doc, monkeypatch, capsys):
    """A warning repeated every round is a warning nobody reads.

    Sized so the run passes through the band TWICE before the ceiling: 4 actors x 1k = 4k a
    round against a 40k ceiling warns from 32k (round 8's boundary) and does not abort until
    40k (round 10, beyond max_rounds). Two opportunities to warn, one warning.

    The sizing is the test. An earlier version used a tight ceiling where the abort followed
    the warning immediately, so "warn once" and "warn every round" were indistinguishable and
    the mutation survived.
    """
    repo, pr_doc = repo_with_pr_doc
    _run(
        repo, pr_doc, monkeypatch, 1_000, budget_tokens=40_000, max_rounds=10, rounds_of_adapters=10
    )
    assert "".join(capsys.readouterr()).count("approaching your token ceiling") == 1


def test_an_unceilinged_run_is_never_warned(repo_with_pr_doc, monkeypatch, capsys):
    repo, pr_doc = repo_with_pr_doc
    _run(repo, pr_doc, monkeypatch, 10_000, budget_tokens=0)
    assert "approaching" not in "".join(capsys.readouterr())


def test_warning_fires_at_pre_producer_boundary_when_reviews_enter_the_band(
    repo_with_pr_doc, monkeypatch, capsys
):
    """The warning fires at the pre-producer boundary even when prior_usages was below the band.

    Sized so the start-of-round check cannot fire:
      - 3 review actors (2 reviewers + judge) × 14k = 42k — above 80% of 50k (= 40k)
      - Prior spend at round-0 start is 0 — the start-of-round check stays silent
      - Producer spends 14k → 56k total, which aborts at start of round 1 BEFORE the
        start-of-round check there. Without the pre-producer boundary check the warning
        never fires.
    """
    repo, pr_doc = repo_with_pr_doc
    _run(repo, pr_doc, monkeypatch, 14_000, budget_tokens=50_000)
    out = "".join(capsys.readouterr())
    assert "approaching your token ceiling" in out


# --- the dollar opt-out (dogfood blocker) ------------------------------------------------------


def test_the_dollar_ceiling_has_the_same_opt_out_as_tokens():
    """A blind reviewer caught the asymmetry, and it made the stop message LIE.

    item 1 gave `budget_tokens` a 0 = no-ceiling sentinel and left `budget_usd` without one, so
    the dollar stop could only offer "remove `[loop] budget_usd`". That advice is false on the
    very path the message prints: `--resume` RE-INHERITS a ceiling the current config omits, so
    removing the key leaves the same ceiling active. The fix is symmetry, not wording — an
    explicit 0 lands in `model_fields_set`, which is exactly how resume tells a decision from
    an absence.
    """
    from syncade.cli.parser_types import _positive_usd

    off = LoopConfig(budget_usd=0, budget_tokens=0)
    assert over_budget([_u(cost=1000.0)], off) is None
    assert approaching_budget([_u(cost=1000.0)], off) is None
    # the distinction resume relies on
    assert "budget_usd" in off.model_fields_set
    assert _positive_usd("0") == 0.0
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_usd("nan")  # widening the floor must not widen anything else


def test_the_dollar_stop_offers_a_remedy_that_exists(repo_with_pr_doc, monkeypatch, capsys):
    """The message must name the opt-out that actually works for the ceiling that crossed."""
    from tests.orchestrator.test_budget_stop_message import _run_into_the_ceiling

    repo, pr_doc = repo_with_pr_doc
    _run_into_the_ceiling(repo, pr_doc, "normal", monkeypatch, budget_usd=0.5, budget_tokens=0)
    out = "".join(capsys.readouterr())
    assert "budget_usd = 0" in out
    assert "remove `[loop] budget_usd`" not in out, "offers an opt-out that does not exist"
