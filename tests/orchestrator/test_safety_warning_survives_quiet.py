"""A REAL branch advance warns the operator even under ``--quiet`` — PR-h-13 item 2.

``tests/test_safety_disclosures_survive_quiet.py`` proves ``Logger.safety`` emits at every
level. That is a unit fact. It does not prove the warning reaches someone running the actual
product, which is the claim that matters — and is exactly the coverage that can rot silently
while the unit test stays green.

This drives ``run_review`` with fake adapters (no model calls) over REAL git: the fake producer
writes a genuine fixture commit, so the branch actually advances and the operator's working tree
is actually left holding the pre-advance content. Lives here rather than at ``tests/`` top level
because ``repo_with_pr_doc`` and the isolated worktree base are this package's fixtures.
"""

from __future__ import annotations

import subprocess

import pytest

from syncade.adapters.fake import FakeAdapter
from syncade.adapters.fake_producer_audit_draft import FakeProducerAdapter
from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _RoundCyclingSynth,
    _synth_clean,
    _synth_with_blocker,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def _head(repo) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("level", ["normal", "quiet"])
def test_a_real_branch_advance_warns_the_operator_even_when_quiet(level, repo_with_pr_doc, capsys):
    repo, pr_doc = repo_with_pr_doc
    config = SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_rounds": 2},
    )
    before = _head(repo)

    run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=config,
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_no_ship()), FakeAdapter(canned_output=_no_ship())
        ),
        synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_clean()),
        producer_adapter=FakeProducerAdapter(commit_message="fix: the producer's work"),
        logger=Logger(level),
    )

    assert _head(repo) != before, "the branch did not advance; this test would prove nothing"

    err = capsys.readouterr().err
    assert "automatically synced" in err, (
        f"at level={level!r} the operator was NOT told their working tree is stale after a real "
        "branch advance — their next `git commit` would silently revert the producer's work"
    )
    assert "git stash" in err, "the non-destructive recovery was not offered"
