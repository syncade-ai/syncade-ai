"""Producer usage wiring (PR-v2-04): run_producer, given pricing, populates a
priced Usage from the producer subprocess stdout. Exercised via the stalled
outcome — producer_usage is computed once after parse and fed to the
committed / stalled / escalated results identically, so one path proves the wiring.
"""

from __future__ import annotations

from syncade.adapters.base import Invocation
from syncade.adapters.fake import FakeProducerAdapter
from syncade.adapters.producer import ProducerOutput
from syncade.config import ProducerConfig
from syncade.pricing_config import PricingConfig
from syncade.producer import run_producer
from syncade.worktree_env import worktree_scoped_env
from tests.producer._helpers import (
    _git_required,
    _make_findings_md,
    _make_pr_doc,
    _seed_repo,
)

_CODEX_JSONL = (
    '{"type":"turn.started"}\n'
    '{"type":"turn.completed","usage":{"input_tokens":5000,"output_tokens":1500}}'
)


class _CodexStdoutProducer(FakeProducerAdapter):
    """FakeProducerAdapter whose subprocess emits a codex JSONL envelope so the
    producer's usage extraction has a real payload to parse."""

    def build_invocation(self, producer_config, worktree_path, prompt) -> Invocation:
        return Invocation(
            argv=["/bin/sh", "-c", 'printf "%s" "$1"', "sh", _CODEX_JSONL],
            cwd=worktree_path,
            env=worktree_scoped_env(worktree_path),
            stdin_text=None,
            timeout_seconds=None,
        )


def test_run_producer_populates_priced_usage_on_stall(tmp_path):
    _git_required()
    starting_sha = _seed_repo(tmp_path)
    pr_doc = _make_pr_doc(tmp_path)
    findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")
    # commit_message=None → the producer makes no commit → stalled outcome
    fake = _CodexStdoutProducer(canned_output=ProducerOutput(narrative_text="looked, no commit"))
    result = run_producer(
        worktree_path=tmp_path,
        starting_sha=starting_sha,
        pr_doc_path=pr_doc,
        findings_md_path=findings,
        test_run_stdout_path=None,
        producer_config=ProducerConfig(provider="openai", model="gpt-5.5"),
        timeout_seconds=30.0,
        round_number=0,
        max_rounds=3,
        repo_root=tmp_path,
        adapter=fake,
        pricing=PricingConfig(),
    )
    assert result.outcome == "stalled"  # no commit
    assert result.usage is not None
    assert result.usage.model == "gpt-5.5"
    assert result.usage.input_tokens == 5000 and result.usage.output_tokens == 1500
    assert result.usage.cost_source == "estimated"
    assert result.usage.cost_usd and result.usage.cost_usd > 0
