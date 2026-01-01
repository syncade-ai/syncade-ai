from __future__ import annotations

from pathlib import Path

from syncade.adapters.fake import FakeProducerAdapter, FakeSynthesizerAdapter
from syncade.config import ProducerConfig
from syncade.dispatcher import ReviewerRunResult
from syncade.process import SubprocessError
from syncade.producer import run_producer
from syncade.synthesizer import run_synthesizer
from tests.orchestrator._helpers import _init_git_repo, _ship


def test_synthesizer_malformed_template_override_returns_phase_error(
    repo_with_pr_doc,
) -> None:
    repo, pr_doc = repo_with_pr_doc
    override_dir = repo / ".syncade" / "templates"
    override_dir.mkdir(parents=True)
    (override_dir / "synthesizer.md").write_text("bad override {diff}\n")

    result = run_synthesizer(
        reviewer_results=[
            ReviewerRunResult(
                reviewer_name="rv1",
                provider="anthropic",
                output=_ship(),
                error=None,
                duration_seconds=1.0,
            )
        ],
        repo_root=repo,
        pr_doc_path=pr_doc,
        timeout_seconds=30.0,
        adapter=FakeSynthesizerAdapter(),
    )

    assert result.output is None
    assert isinstance(result.error, SubprocessError)
    assert "synthesizer template" in str(result.error)
    assert "diff" in str(result.error)


def test_synthesizer_driver_renders_schema_string_into_prompt(repo_with_pr_doc) -> None:
    repo, pr_doc = repo_with_pr_doc
    fake = FakeSynthesizerAdapter()

    result = run_synthesizer(
        reviewer_results=[
            ReviewerRunResult(
                reviewer_name="rv1",
                provider="anthropic",
                output=_ship(),
                error=None,
                duration_seconds=1.0,
            )
        ],
        repo_root=repo,
        pr_doc_path=pr_doc,
        timeout_seconds=30.0,
        adapter=fake,
    )

    assert result.error is None
    assert fake.invocations
    prompt = fake.invocations[0][2]
    assert "<function get_synthesizer_schema_string" not in prompt
    assert "consolidated_findings" in prompt
    assert "synthesis_summary" in prompt
    assert "provenance" in prompt


def test_producer_malformed_template_override_returns_phase_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    starting_sha = _init_git_repo(repo)
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")
    findings = repo / ".syncade" / "runs" / "r" / "round-0" / "findings.md"
    findings.parent.mkdir(parents=True)
    findings.write_text("# Findings\n")
    override_dir = repo / ".syncade" / "templates"
    override_dir.mkdir(parents=True)
    (override_dir / "producer.md").write_text("bad override {missing_key}\n")
    fake = FakeProducerAdapter(commit_message="fix: should not run")

    result = run_producer(
        worktree_path=repo,
        starting_sha=starting_sha,
        pr_doc_path=pr_doc,
        findings_md_path=findings,
        test_run_stdout_path=None,
        producer_config=ProducerConfig(),
        timeout_seconds=30.0,
        round_number=0,
        max_rounds=3,
        repo_root=repo,
        adapter=fake,
    )

    assert result.outcome == "subprocess_error"
    assert isinstance(result.error, SubprocessError)
    # The render + input-staging step share one error surface; the message
    # generalized from "template render failed" to "prompt setup failed".
    assert "producer prompt setup failed" in str(result.error)
    assert "missing_key" in str(result.error)
    assert fake.invocations == []
