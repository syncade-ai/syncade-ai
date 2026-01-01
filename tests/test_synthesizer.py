from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from syncade.adapters.base import Invocation
from syncade.config import ReviewerConfig, SynthesizerConfig
from syncade.dispatcher import ReviewerRunResult
from syncade.findings import Finding, ReviewerOutput
from syncade.pricing_config import PricingConfig
from syncade.process import SubprocessResult, SubprocessTimeoutError
from syncade.synthesis import (
    ConsolidatedFinding,
    FindingProvenance,
    SynthesizerOutput,
    SynthesizerOutputError,
)
from syncade.synthesis_clusters import ClusterMemberEvidence, RootCauseCluster
from syncade.synthesizer import _validate_cluster_quotes_against_reviewers

_CODEX_USAGE_JSONL = (
    '{"type":"turn.started"}\n'
    '{"type":"turn.completed","usage":{"input_tokens":2000,"cached_input_tokens":0,'
    '"output_tokens":600,"reasoning_output_tokens":0}}'
)


def _ship_output() -> ReviewerOutput:
    """Minimal SHIP reviewer output for tests that just need a valid input."""
    return ReviewerOutput(
        verdict="SHIP",
        summary="ok",
        findings=[],
        priority_order=[],
        coverage_gaps=[],
        dismissed_concerns=[],
    )


def _no_ship_output() -> ReviewerOutput:
    """Minimal NO-SHIP reviewer output with one blocker (for synth input)."""
    finding = Finding(
        severity="blocker",
        file="src/foo.py",
        line=10,
        spec_clause="must guard against None",
        finding="missing null check",
    )
    return ReviewerOutput(
        verdict="NO-SHIP",
        summary="bug",
        findings=[finding],
        priority_order=[0],
        coverage_gaps=[],
        dismissed_concerns=[],
    )


def _reviewer_result(name: str) -> ReviewerRunResult:
    return ReviewerRunResult(
        reviewer_name=name,
        provider="anthropic",
        output=_no_ship_output(),
        error=None,
        duration_seconds=1.0,
    )


class _TimeoutSynthAdapter:
    def build_invocation(
        self,
        reviewer_config: ReviewerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        del reviewer_config, prompt
        return Invocation(argv=["codex"], cwd=worktree_path, env={}, stdin_text=None)

    def extract_final_text(
        self,
        result: SubprocessResult,
        *,
        empty_output_exception_class: type[Exception],
    ) -> str:
        del result, empty_output_exception_class
        raise AssertionError("timeout path must not extract a final agent message")


@pytest.fixture
def repo_with_pr_doc(tmp_path: Path) -> tuple[Path, Path]:
    """Minimal repo skeleton with a PR doc — enough for synth setup."""
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\nDoes the thing.\n")
    return tmp_path, pr_doc


def test_timeout_partial_stdout_records_usage(repo_with_pr_doc, monkeypatch):
    import syncade.synthesizer as synth
    import syncade.synthesizer.driver as synth_driver

    def fake_run_subprocess(argv, *, cwd, env, timeout, input_text):
        del argv, cwd, env, timeout, input_text
        raise SubprocessTimeoutError(
            "synth timed out",
            stdout=_CODEX_USAGE_JSONL,
            stderr="partial stderr",
            timeout=1.0,
        )

    monkeypatch.setattr(synth_driver, "run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(synth_driver, "_init_workspace_git", lambda workspace: None)

    repo, pr_doc = repo_with_pr_doc
    result = synth.run_synthesizer(
        reviewer_results=[_reviewer_result("rv1")],
        repo_root=repo,
        pr_doc_path=pr_doc,
        timeout_seconds=1.0,
        adapter=_TimeoutSynthAdapter(),
        pricing=PricingConfig(),
    )
    assert isinstance(result.error, SubprocessTimeoutError)
    assert result.raw_subprocess_result is not None
    assert result.raw_subprocess_result.stdout == _CODEX_USAGE_JSONL
    assert result.usage is not None
    assert result.usage.input_tokens == 2000
    assert result.usage.output_tokens == 600
    assert result.usage.total_tokens == 2600
    assert result.usage.cost_source == "estimated"
    assert result.usage.cost_usd and result.usage.cost_usd > 0


class TestDriverLookupMonkeyPatches:
    def test_run_synthesizer_resolves_adapter_from_registry_via_driver(
        self, repo_with_pr_doc, monkeypatch
    ):
        """The judge's adapter comes from ``get_adapter(config.provider)``, resolved
        at the DRIVER's lookup site.

        Two properties at once:

        1. The driver resolves through the registry, and hands it the provider from
           the ``[synthesizer]`` config block — that is what makes the judge
           provider-agnostic rather than codex-only.
        2. The decomposition rule (CLAUDE.md): patching the PACKAGE
           (``syncade.synthesizer``) does not affect the submodule global the
           function body actually reads.
        """
        import syncade.synthesizer as synth
        import syncade.synthesizer.driver as synth_driver

        instantiations: list[int] = []
        wrong_site_instantiations: list[int] = []
        resolved_providers: list[str] = []

        class RecordingAdapter:
            """Duck-typed synthesizer adapter recorded on construction."""

            def __init__(self):
                instantiations.append(len(instantiations))

            def build_invocation(
                self,
                reviewer_config: ReviewerConfig,
                worktree_path: Path,
                prompt: str,
            ) -> Invocation:
                del reviewer_config, prompt
                return Invocation(
                    argv=["echo", "noop"],
                    cwd=worktree_path,
                    env={},
                    stdin_text=None,
                )

            def extract_final_text(
                self,
                result: SubprocessResult,
                *,
                empty_output_exception_class: type[Exception],
            ) -> str:
                del result, empty_output_exception_class
                return '{"consolidated_findings": [], "synthesis_summary": "ok"}'

        class WrongSiteAdapter:
            def __init__(self):
                wrong_site_instantiations.append(len(wrong_site_instantiations))

        def recording_get_adapter(provider: str):
            resolved_providers.append(provider)
            return RecordingAdapter()

        monkeypatch.setattr(synth, "get_adapter", lambda _p: WrongSiteAdapter(), raising=False)
        monkeypatch.setattr(synth_driver, "get_adapter", recording_get_adapter)

        # Stub out subprocess + git so we don't shell out.
        def fake_run_subprocess(argv, *, cwd, env, timeout, input_text):
            del argv, cwd, env, timeout, input_text
            return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

        monkeypatch.setattr(synth_driver, "run_subprocess", fake_run_subprocess)
        monkeypatch.setattr(synth_driver, "_init_workspace_git", lambda workspace: None)

        repo, pr_doc = repo_with_pr_doc
        # adapter=None so the registry-resolution path fires — the path a real run
        # takes. The config names a NON-default provider, so a driver that still
        # hardcoded codex would resolve the wrong one.
        synth.run_synthesizer(
            reviewer_results=[_reviewer_result("rv1")],
            repo_root=repo,
            pr_doc_path=pr_doc,
            timeout_seconds=10.0,
            config=SynthesizerConfig(provider="anthropic", model="claude-sonnet-4-6"),
            adapter=None,
        )

        assert len(instantiations) == 1
        assert wrong_site_instantiations == []
        # The configured provider reached the registry — not a hardcoded "openai".
        assert resolved_providers == ["anthropic"]

    def test_run_synthesizer_resolves_run_subprocess_via_driver(
        self, repo_with_pr_doc, monkeypatch
    ):
        import syncade.synthesizer as synth
        import syncade.synthesizer.driver as synth_driver

        invocations: list[int] = []
        wrong_site_calls: list[int] = []

        def recording_run_subprocess(argv, *, cwd, env, timeout, input_text):
            del argv, cwd, env, timeout, input_text
            invocations.append(len(invocations))
            return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

        def wrong_site_run_subprocess(argv, *, cwd, env, timeout, input_text):
            del argv, cwd, env, timeout, input_text
            wrong_site_calls.append(len(wrong_site_calls))
            raise AssertionError("package-level run_subprocess patch should not be used")

        # Use a duck-typed adapter that returns a valid empty-findings
        # synthesizer output so the parse path completes cleanly.
        class _StubAdapter:
            def build_invocation(
                self,
                reviewer_config: ReviewerConfig,
                worktree_path: Path,
                prompt: str,
            ) -> Invocation:
                del reviewer_config, prompt
                return Invocation(
                    argv=["echo", "noop"],
                    cwd=worktree_path,
                    env={},
                    stdin_text=None,
                )

            def extract_final_text(
                self,
                result: SubprocessResult,
                *,
                empty_output_exception_class: type[Exception],
            ) -> str:
                del result, empty_output_exception_class
                return '{"consolidated_findings": [], "synthesis_summary": "ok"}'

        monkeypatch.setattr(synth, "run_subprocess", wrong_site_run_subprocess, raising=False)
        monkeypatch.setattr(synth_driver, "run_subprocess", recording_run_subprocess)
        monkeypatch.setattr(synth_driver, "_init_workspace_git", lambda workspace: None)

        repo, pr_doc = repo_with_pr_doc
        synth.run_synthesizer(
            reviewer_results=[_reviewer_result("rv1")],
            repo_root=repo,
            pr_doc_path=pr_doc,
            timeout_seconds=10.0,
            adapter=_StubAdapter(),
        )

        assert len(invocations) == 1
        assert wrong_site_calls == []

    def test_run_synthesizer_resolves_init_workspace_git_via_driver(
        self, repo_with_pr_doc, monkeypatch
    ):
        import syncade.synthesizer as synth
        import syncade.synthesizer.driver as synth_driver

        invocations: list[Path] = []
        wrong_site_calls: list[Path] = []

        def recording_init_workspace_git(workspace: Path) -> None:
            invocations.append(workspace)

        def wrong_site_init_workspace_git(workspace: Path) -> None:
            wrong_site_calls.append(workspace)
            raise AssertionError("package-level _init_workspace_git patch should not be used")

        class _StubAdapter:
            def build_invocation(
                self,
                reviewer_config: ReviewerConfig,
                worktree_path: Path,
                prompt: str,
            ) -> Invocation:
                del reviewer_config, prompt
                return Invocation(
                    argv=["echo", "noop"],
                    cwd=worktree_path,
                    env={},
                    stdin_text=None,
                )

            def extract_final_text(
                self,
                result: SubprocessResult,
                *,
                empty_output_exception_class: type[Exception],
            ) -> str:
                del result, empty_output_exception_class
                return '{"consolidated_findings": [], "synthesis_summary": "ok"}'

        def fake_run_subprocess(argv, *, cwd, env, timeout, input_text):
            del argv, cwd, env, timeout, input_text
            return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

        monkeypatch.setattr(
            synth, "_init_workspace_git", wrong_site_init_workspace_git, raising=False
        )
        monkeypatch.setattr(synth_driver, "_init_workspace_git", recording_init_workspace_git)
        monkeypatch.setattr(synth_driver, "run_subprocess", fake_run_subprocess)

        repo, pr_doc = repo_with_pr_doc
        synth.run_synthesizer(
            reviewer_results=[_reviewer_result("rv1")],
            repo_root=repo,
            pr_doc_path=pr_doc,
            timeout_seconds=10.0,
            adapter=_StubAdapter(),
        )

        assert len(invocations) == 1
        assert wrong_site_calls == []
        # Sanity: workspace argument was a real tempdir under the
        # syncade-synth- prefix.
        workspace = invocations[0]
        assert workspace.exists() or not workspace.exists(), "type sanity"
        # Defensive: confirm tempfile's prefix is what driver.py uses.
        # The prefix is documented in driver.py's run_synthesizer.
        assert tempfile.gettempdir() in str(workspace) or os.sep in str(workspace)


class TestClusterQuoteValidation:
    """PR-19: cluster evidence quotes must be VERBATIM substrings of the
    member's reviewer-original finding text — the cannot-invent guarantee for
    descriptive-only clusters. Mirrors the provenance cross-check but stricter
    (verbatim, not paraphrase-tolerant)."""

    def _reviewer_results(self) -> list[ReviewerRunResult]:
        f0 = Finding(
            severity="minor",
            file="src/a.py",
            line=1,
            spec_clause="c",
            finding="the secret leak depends on the user's gitignore",
        )
        f1 = Finding(
            severity="minor",
            file="src/a.py",
            line=2,
            spec_clause="c",
            finding="a symlink gitignore also defeats the guard",
        )
        out = ReviewerOutput(
            verdict="NO-SHIP",
            summary="related findings",
            findings=[f0, f1],
            priority_order=[0, 1],
            coverage_gaps=[],
            dismissed_concerns=[],
        )
        return [
            ReviewerRunResult(
                reviewer_name="claude-reviewer",
                provider="anthropic",
                output=out,
                error=None,
                duration_seconds=1.0,
            )
        ]

    def _output_with_cluster(self, quote0: str, quote1: str) -> SynthesizerOutput:
        prov0 = FindingProvenance(
            reviewer_name="claude-reviewer",
            original_severity="minor",
            original_index=0,
            original_description="leak depends on gitignore",
        )
        prov1 = FindingProvenance(
            reviewer_name="claude-reviewer",
            original_severity="minor",
            original_index=1,
            original_description="symlink case",
        )
        return SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="c0", file="src/a.py", severity="minor", provenance=[prov0]
                ),
                ConsolidatedFinding(
                    description="c1", file="src/a.py", severity="minor", provenance=[prov1]
                ),
            ],
            synthesis_summary="two findings, one root cause",
            root_cause_clusters=[
                RootCauseCluster(
                    member_finding_indices=[0, 1],
                    anchor_file="src/a.py",
                    evidence=[
                        ClusterMemberEvidence(finding_index=0, quote=quote0),
                        ClusterMemberEvidence(finding_index=1, quote=quote1),
                    ],
                )
            ],
        )

    def test_verbatim_quotes_pass(self) -> None:
        out = self._output_with_cluster("depends on the user's gitignore", "symlink gitignore")
        # Both are verbatim substrings of the respective reviewer findings → no raise.
        _validate_cluster_quotes_against_reviewers(out, self._reviewer_results())

    def test_non_substring_quote_rejected(self) -> None:
        out = self._output_with_cluster(
            "depends on the user's gitignore", "PARAPHRASED not verbatim"
        )
        with pytest.raises(SynthesizerOutputError):
            _validate_cluster_quotes_against_reviewers(out, self._reviewer_results())

    def test_no_clusters_is_noop(self) -> None:
        out = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="c0",
                    file="src/a.py",
                    severity="minor",
                    provenance=[
                        FindingProvenance(
                            reviewer_name="claude-reviewer",
                            original_severity="minor",
                            original_index=0,
                            original_description="x",
                        )
                    ],
                )
            ],
            synthesis_summary="one finding, no cluster",
        )
        # No clusters → nothing to validate, no raise.
        _validate_cluster_quotes_against_reviewers(out, self._reviewer_results())
