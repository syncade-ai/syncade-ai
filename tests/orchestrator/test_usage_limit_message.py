"""The operator is told what happened and what to do — PR-h-field-02 item 3.

Three field runs hit a provider quota and the operator saw nothing: no verdict line, no error,
no exit code, and a status file still claiming the run was in progress. The whole cost of the
incident was that silence — the work was intact the entire time.

Asserted on the emitted TEXT rather than an internal call, because the claim is about what a
person reads. Under `--quiet` too: a terminal condition is not progress chatter, and quiet is
exactly when someone is least likely to be watching the scrollback.
"""

from __future__ import annotations

import subprocess

import pytest

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import _factory_returning, _no_ship

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)

_REVIEWERS = [
    {"name": "rv1", "provider": "fake1", "model": "x"},
    {"name": "rv2", "provider": "fake2", "model": "y"},
]


def _quota_run(repo, pr_doc, level: str):
    exc = ReviewerInvocationError(
        "You've hit your usage limit for gpt-5.5", returncode=1, stdout="", stderr=""
    )
    logger = Logger(level)
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=SyncadeConfig(reviewers=_REVIEWERS, loop={"max_rounds": 3}),
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_no_ship()), FakeAdapter(canned_exception=exc)
        ),
        logger=logger,
    )
    logger.summary(result)
    return result


@pytest.mark.parametrize("level", ["normal", "quiet"])
def test_the_operator_is_told_all_four_things(level, repo_with_pr_doc, capsys):
    repo, pr_doc = repo_with_pr_doc
    result = _quota_run(repo, pr_doc, level)
    out = capsys.readouterr().out

    assert "usage limit reached" in out, f"no quota notice at level={level!r}"
    # 1. whose limit — a mixed panel makes this the first question
    assert "fake2" in out
    # 2. the work survived
    assert "preserved" in out
    # 3. the remedy, and that retrying now is pointless
    assert "/usage" in out and "fails the same way" in out
    # 4. the exact command, with the id that makes it usable
    assert f"syncade --resume {result.artifacts.run_dir.name}" in out


def test_it_names_the_JUDGE_not_a_bare_the_provider(repo_with_pr_doc, capsys):
    """The routing fix made the judge and producer common quota victims.

    The message's whole reason for naming an actor is that "which one ran out" is the first
    question on a mixed panel — and a lookup that scans only reviewers answers it with a
    shrug for two of the three actors that can now trigger this line.
    """
    repo, pr_doc = repo_with_pr_doc
    exc = ReviewerInvocationError("You've hit your usage limit", returncode=1, stdout="", stderr="")
    logger = Logger("normal")
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=SyncadeConfig(reviewers=_REVIEWERS, loop={"max_rounds": 3}),
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_no_ship()), FakeAdapter(canned_output=_no_ship())
        ),
        synthesizer_adapter=FakeSynthesizerAdapter(canned_exception=exc),
        logger=logger,
    )
    logger.summary(result)
    out = capsys.readouterr().out

    assert result.termination_reason == "provider_usage_limit"
    assert "usage limit reached" in out
    assert "the provider's usage limit" not in out, (
        "fell back to the generic phrasing — the judge's provider was reachable and unused"
    )


def test_it_does_not_fire_on_an_ordinary_run(repo_with_pr_doc, capsys):
    """A notice on every run is a notice nobody reads."""
    repo, pr_doc = repo_with_pr_doc
    logger = Logger("normal")
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=SyncadeConfig(reviewers=_REVIEWERS, loop={"max_rounds": 1}),
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_no_ship()), FakeAdapter(canned_output=_no_ship())
        ),
        logger=logger,
    )
    logger.summary(result)
    assert "usage limit reached" not in capsys.readouterr().out
    assert result.termination_reason != "provider_usage_limit"


def test_the_run_is_actually_resumable_as_the_message_claims(repo_with_pr_doc):
    """The message promises `--resume` works. Assert the state it needs is on disk.

    A remedy that does not work is worse than silence, because it spends the operator's trust
    as well as their time.
    """
    repo, pr_doc = repo_with_pr_doc
    result = _quota_run(repo, pr_doc, "normal")
    run_dir = result.artifacts.run_dir
    assert (run_dir / "run-init.json").is_file(), "resume needs the run's config snapshot"
    assert (run_dir / "status.json").is_file()
    assert (run_dir / "round-0").is_dir(), "the interrupted round must still be on disk"
