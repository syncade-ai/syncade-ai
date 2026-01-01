from __future__ import annotations

import subprocess

from syncade.adapters.fake import FakeProducerAdapter
from syncade.config import ProducerConfig
from syncade.pricing_config import PricingConfig
from syncade.process import SubprocessResult, SubprocessTimeoutError
from syncade.producer import run_producer
from tests.producer._helpers import (
    _git_required,
    _make_findings_md,
    _make_pr_doc,
    _read_head,
    _seed_repo,
)

_CODEX_USAGE_JSONL = (
    '{"type":"turn.started"}\n'
    '{"type":"turn.completed","usage":{"input_tokens":5000,"cached_input_tokens":0,'
    '"output_tokens":1500,"reasoning_output_tokens":0}}'
)


def test_timeout_after_partial_commit_surfaces_moved_head(tmp_path, monkeypatch):
    import syncade.producer as producer_module

    _git_required()
    starting_sha = _seed_repo(tmp_path)
    pr_doc = _make_pr_doc(tmp_path)
    findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

    def fake_run_subprocess(argv, *, cwd=None, env=None, timeout=None, input_text=None):
        del env, timeout, input_text
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return SubprocessResult(
                returncode=0,
                stdout=_read_head(cwd) + "\n",
                stderr="",
                duration_seconds=0.01,
            )

        (tmp_path / "timeout-fix.txt").write_text(
            "committed before timeout\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "timeout-fix.txt"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "fix: committed before timeout"],
            cwd=tmp_path,
            check=True,
        )
        raise SubprocessTimeoutError(
            "producer timed out",
            stdout="partial stdout",
            stderr="partial stderr",
            timeout=1.0,
        )

    monkeypatch.setattr(producer_module, "run_subprocess", fake_run_subprocess)

    result = run_producer(
        worktree_path=tmp_path,
        starting_sha=starting_sha,
        pr_doc_path=pr_doc,
        findings_md_path=findings,
        test_run_stdout_path=None,
        producer_config=ProducerConfig(),
        timeout_seconds=1.0,
        round_number=0,
        max_rounds=3,
        repo_root=tmp_path,
        adapter=FakeProducerAdapter(),
    )

    moved_sha = _read_head(tmp_path)
    assert result.outcome == "subprocess_error"
    assert isinstance(result.error, SubprocessTimeoutError)
    assert result.starting_sha == starting_sha
    assert result.ending_sha == moved_sha
    assert result.ending_sha != starting_sha
    assert result.raw_subprocess_result is not None
    assert result.raw_subprocess_result.stdout == "partial stdout"
    assert result.raw_subprocess_result.stderr == "partial stderr"


def test_timeout_partial_stdout_records_usage(tmp_path, monkeypatch):
    import syncade.producer as producer_module

    _git_required()
    starting_sha = _seed_repo(tmp_path)
    pr_doc = _make_pr_doc(tmp_path)
    findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

    def fake_run_subprocess(argv, *, cwd=None, env=None, timeout=None, input_text=None):
        del env, timeout, input_text
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return SubprocessResult(
                returncode=0,
                stdout=_read_head(cwd) + "\n",
                stderr="",
                duration_seconds=0.01,
            )
        raise SubprocessTimeoutError(
            "producer timed out",
            stdout=_CODEX_USAGE_JSONL,
            stderr="partial stderr",
            timeout=1.0,
        )

    monkeypatch.setattr(producer_module, "run_subprocess", fake_run_subprocess)

    result = run_producer(
        worktree_path=tmp_path,
        starting_sha=starting_sha,
        pr_doc_path=pr_doc,
        findings_md_path=findings,
        test_run_stdout_path=None,
        producer_config=ProducerConfig(provider="openai", model="gpt-5.5"),
        timeout_seconds=1.0,
        round_number=0,
        max_rounds=3,
        repo_root=tmp_path,
        adapter=FakeProducerAdapter(),
        pricing=PricingConfig(),
    )

    assert result.outcome == "subprocess_error"
    assert isinstance(result.error, SubprocessTimeoutError)
    assert result.raw_subprocess_result is not None
    assert result.raw_subprocess_result.stdout == _CODEX_USAGE_JSONL
    assert result.usage is not None
    assert result.usage.input_tokens == 5000
    assert result.usage.output_tokens == 1500
    assert result.usage.total_tokens == 6500
    assert result.usage.cost_source == "estimated"
    assert result.usage.cost_usd and result.usage.cost_usd > 0
