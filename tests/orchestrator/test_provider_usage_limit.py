"""A provider usage limit stops the loop cleanly at exit 25 — PR-h-field-02 item 2.

Quota exhaustion is neither transient nor permanent. Retrying is pointless (the window has not
moved) and dying at exit 40 is wrong (the run is resumable and the operator only has to wait).
It routes to exit 25's existing contract: stopped at a phase boundary, completed rounds intact,
`syncade --resume` continues.

The brief demanded two proofs specifically, and both are here: the classifier must not be
satisfied by an empty marker list, and the dispatcher must issue ZERO further invocations —
`MAX_RETRIES = 2` firing against an exhausted quota is the failure mode this must exclude.
"""

from __future__ import annotations

import subprocess

import pytest

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.fake import (
    FakeAdapter,
    FakeProducerAdapter,
    FakeSynthesizerAdapter,
)
from syncade.config import SyncadeConfig
from syncade.exit_codes import BUDGET_EXCEEDED, REVIEWER_FAILURE
from syncade.orchestrator import run_review
from syncade.retry import MAX_RETRIES, is_transient_api_error, is_usage_limit_error
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _RoundCyclingSynth,
    _synth_with_blocker,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)

_REVIEWERS = [
    {"name": "rv1", "provider": "fake1", "model": "x"},
    {"name": "rv2", "provider": "fake2", "model": "y"},
]

# The provider's own text, read out of the shipped codex binary rather than imagined.
_CODEX_TEXT = "You've hit your usage limit for gpt-5.5. Resets in 3h."


def _quota_error(text: str = _CODEX_TEXT, stderr: str = "") -> ReviewerInvocationError:
    return ReviewerInvocationError(text, returncode=1, stdout="", stderr=stderr)


# FakeAdapter already records every build_invocation when asked; an earlier version of this
# file subclassed it and shadowed `invocations` (a list) with an int, which made the counting
# test pass for the wrong reason — the reviewer was failing with AttributeError, not with the
# quota error under test. Use the facility that exists.
def _counting(exc) -> FakeAdapter:
    return FakeAdapter(canned_exception=exc, record_invocations=True)


# --- classification -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,stderr",
    [
        (_CODEX_TEXT, ""),
        ("reviewer failed", "UsageLimitReached"),
        ("reviewer failed", "QuotaExceeded"),
        ("usage limit reached for this account", ""),
    ],
)
def test_quota_shapes_are_recognised(text, stderr):
    exc = _quota_error(text, stderr)
    assert is_usage_limit_error(exc) is True
    assert is_transient_api_error(exc) is False, (
        "a quota error must NOT be transient — retrying burns attempts against a window that "
        "has not moved"
    )


@pytest.mark.parametrize(
    "text",
    [
        "stream disconnected before completion",
        "could not parse reviewer output",
        "connection reset by peer",
        # Deliberately close to the markers: prose about the reviewed CODE must not match.
        "the service should return 429 when the caller exceeds its rate limit",
    ],
)
def test_non_quota_failures_are_not_misread(text):
    assert is_usage_limit_error(_quota_error(text)) is False


def test_the_classifier_is_not_vacuous(monkeypatch):
    """Empty the marker tuple and the recogniser must go dark.

    A classifier test that still passes against an empty list proves nothing — the brief called
    this out explicitly.
    """
    import syncade.retry as retry

    monkeypatch.setattr(retry, "_USAGE_LIMIT_MARKERS", ())
    assert retry.is_usage_limit_error(_quota_error()) is False


# --- end-to-end: verdict, and no retry storm ------------------------------------------------


def test_a_quota_refusal_exits_25_not_40(repo_with_pr_doc):
    repo, pr_doc = repo_with_pr_doc
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=SyncadeConfig(reviewers=_REVIEWERS, loop={"max_rounds": 3}),
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_exception=_quota_error()),
        ),
    )
    assert result.exit_code == BUDGET_EXCEEDED, (
        f"expected exit 25 (stop cleanly, resume later), got {result.exit_code}"
    )
    assert result.exit_code != REVIEWER_FAILURE
    assert result.termination_reason == "provider_usage_limit", (
        "must be distinguishable from budget_exceeded — the operator's ceiling was never hit"
    )


def test_zero_further_invocations_after_a_quota_refusal(repo_with_pr_doc):
    """The failure mode the brief names: MAX_RETRIES doomed pairs against an exhausted quota."""
    assert MAX_RETRIES >= 1, "if retries were disabled this test would prove nothing"
    repo, pr_doc = repo_with_pr_doc
    quota = _counting(_quota_error())
    run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=SyncadeConfig(reviewers=_REVIEWERS, loop={"max_rounds": 3}),
        adapter_factory=_factory_returning(FakeAdapter(canned_output=_no_ship()), quota),
    )
    assert len(quota.invocations) == 1, (
        f"the quota reviewer was invoked {len(quota.invocations)} times; it must be "
        f"attempted exactly once — retrying an exhausted window is the storm this prevents"
    )


# --- the other two actors -------------------------------------------------------------------
#
# The first cut of this feature only inspected the REVIEWER dispatch results, so a quota that
# hit the judge or the producer still died at exit 40. A quota is account-wide: whichever actor
# reaches the provider first is the one that discovers it, and on a same-provider panel that is
# as likely to be the judge (it runs last, when the window is closest to exhausted).


def test_a_quota_on_the_JUDGE_also_exits_25(repo_with_pr_doc):
    repo, pr_doc = repo_with_pr_doc
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=SyncadeConfig(reviewers=_REVIEWERS, loop={"max_rounds": 3}),
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_no_ship()), FakeAdapter(canned_output=_no_ship())
        ),
        synthesizer_adapter=FakeSynthesizerAdapter(canned_exception=_quota_error()),
    )
    assert result.exit_code == BUDGET_EXCEEDED, (
        f"the judge hit a quota and the run died at {result.exit_code} instead of stopping "
        f"resumably — the reviewers' work was already paid for"
    )
    assert result.termination_reason == "provider_usage_limit"


def test_a_quota_on_the_PRODUCER_also_exits_25(repo_with_pr_doc):
    """The producer is the actor most likely to find the window empty: it runs last, after
    both reviewers and the judge have spent against the same account."""
    repo, pr_doc = repo_with_pr_doc
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=SyncadeConfig(reviewers=_REVIEWERS, loop={"max_rounds": 3}),
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_no_ship()), FakeAdapter(canned_output=_no_ship())
        ),
        synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker()),
        producer_adapter=FakeProducerAdapter(canned_exception=_quota_error()),
    )
    assert result.exit_code == BUDGET_EXCEEDED, (
        f"expected 25, got {result.exit_code} (40 = producer_subprocess_error, which tells the "
        f"operator their producer is broken when the account simply ran out)"
    )
    assert result.termination_reason == "provider_usage_limit"


def test_an_ordinary_producer_failure_is_still_a_subprocess_error(repo_with_pr_doc):
    """Counter-test: the quota branch must not swallow every producer failure into 25."""
    repo, pr_doc = repo_with_pr_doc
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=SyncadeConfig(reviewers=_REVIEWERS, loop={"max_rounds": 3}),
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_no_ship()), FakeAdapter(canned_output=_no_ship())
        ),
        synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker()),
        producer_adapter=FakeProducerAdapter(canned_exception=_quota_error("connection reset")),
    )
    assert result.exit_code != BUDGET_EXCEEDED
    assert result.termination_reason != "provider_usage_limit"


def test_a_transient_failure_still_retries(repo_with_pr_doc):
    """Counter-test: the quota path must not have disabled retry for everything else."""
    repo, pr_doc = repo_with_pr_doc
    flaky = _counting(_quota_error("stream disconnected before completion"))
    run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=SyncadeConfig(reviewers=_REVIEWERS, loop={"max_rounds": 1}),
        adapter_factory=_factory_returning(FakeAdapter(canned_output=_no_ship()), flaky),
    )
    assert len(flaky.invocations) == MAX_RETRIES + 1, (
        f"a transient error should be retried {MAX_RETRIES} times "
        f"(={MAX_RETRIES + 1} invocations), saw {len(flaky.invocations)}"
    )
