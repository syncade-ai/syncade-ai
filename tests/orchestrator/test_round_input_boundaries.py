"""Round-level input-boundary regressions: PR-doc collision (H2) and the
reviewer-template phase boundary (N2).

Uses :class:`FakeAdapter` exclusively via ``adapter_factory`` — no real CLI
calls. Each test stands up an ephemeral git repo in ``tmp_path`` so snapshot +
worktree provisioning exercise real git, then injects fakes for dispatch.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.config import SyncadeConfig
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import (
    _factory_returning,
    _init_git_repo,
    _no_ship,
    _ship,
    _synth_with_blocker,
    _two_reviewer_config,
)

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def _latest_run_dir(repo: Path) -> Path:
    """Return the most recent run directory under ``<repo>/.syncade/runs/``."""
    runs_dir = repo / ".syncade" / "runs"
    assert runs_dir.is_dir(), f"no runs dir created under {repo / '.syncade'}"
    run_dirs = [p for p in sorted(runs_dir.glob("*")) if p.is_dir()]
    assert run_dirs, f"no run directory created: {list(runs_dir.iterdir())}"
    return run_dirs[-1]


class TestOutOfRepoPrDocBasenameCollision:
    """H2: an out-of-repo PR doc whose basename collides with a tracked file
    (e.g. ``README.md``) must not be shadowed. The bare-``.name`` ref resolved
    to the repo's tracked file and the copy was skipped because the path
    already existed — reviewers silently read the WRONG spec. The fix renders a
    reserved collision-free worktree-local ref and ALWAYS copies the supplied
    doc there.
    """

    def test_reviewer_reads_supplied_doc_not_tracked_collision(self, tmp_path):
        # Repo tracks README.md with its OWN content.
        repo = (tmp_path / "repo").resolve()
        _init_git_repo(repo, files={"README.md": "REPO README\n"})

        # Out-of-repo PR doc that happens to be named README.md too.
        external = tmp_path / "external"
        external.mkdir()
        pr_doc = external / "README.md"
        pr_doc.write_text("OUTSIDE PR SPEC")

        # rv1 NO-SHIP + a synth blocker → exit 30 (FINDINGS_PRESENT), which
        # preserves the worktrees on disk for inspection.
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(
                canned_output=_synth_with_blocker(reviewer_name="rv1")
            ),
        )
        assert result.exit_code == 30  # worktrees preserved

        # The prompt points reviewers at a reserved collision-free ref under
        # .syncade-inputs/, NOT the bare README.md (which would resolve to the
        # repo's tracked file inside the worktree).
        _, worktree_path, prompt = adapters[0].invocations[0]
        inputs_dir = worktree_path / ".syncade-inputs"
        copied = list(inputs_dir.glob("pr-doc-*-README.md"))
        assert len(copied) == 1, (
            "expected exactly one reserved pr-doc copy under .syncade-inputs/; "
            f"got {list(inputs_dir.glob('*')) if inputs_dir.exists() else 'no dir'}"
        )
        reserved_ref = f".syncade-inputs/{copied[0].name}"
        assert reserved_ref in prompt

        # The reserved worktree file carries the SUPPLIED out-of-repo doc...
        assert copied[0].read_text() == "OUTSIDE PR SPEC"
        # ...and the repo's tracked README.md at the worktree root is untouched
        # (the supplied doc did not shadow it, and vice versa).
        assert (worktree_path / "README.md").read_text() == "REPO README\n"


class TestReviewerTemplatePhaseBoundary:
    """N2: ``load_reviewer_template`` + ``render_reviewer_prompt`` run BEFORE
    the worktree try/except, so a malformed operator override crashed
    ``run_review`` with no manifest/summary/error artifact. The fix mirrors the
    producer/synth contract: a render failure returns a phase-failure
    ``RoundResult`` (exit 40) with the round artifacts written.
    """

    def test_malformed_override_yields_phase_failure_with_artifacts(self, repo_with_pr_doc):
        repo, pr_doc = repo_with_pr_doc

        # Malformed reviewer.md override: an unknown placeholder makes
        # str.format_map raise KeyError at render time.
        templates_dir = repo / ".syncade" / "templates"
        templates_dir.mkdir(parents=True)
        (templates_dir / "reviewer.md").write_text(
            "Review {pr_doc_path} carefully but {bogus_placeholder}\n"
        )

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]

        # No uncaught exception: run_review returns a RunResult (pre-fix this
        # call raised KeyError straight out of run_review).
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )

        # Phase failure → exit 40 (REVIEWER_FAILURE).
        assert result.exit_code == 40

        # The reviewers never ran — the template failed before dispatch.
        assert adapters[0].invocations == []
        assert adapters[0].parse_output_calls == 0

        # Round artifacts were written despite the early failure.
        round_dir = _latest_run_dir(repo) / "round-0"
        assert (round_dir / "manifest.json").is_file(), "manifest.json not written"
        assert (round_dir / "summary.md").is_file(), "summary.md not written"
        manifest = json.loads((round_dir / "manifest.json").read_text())
        assert manifest["round_exit_code"] == 40
        assert manifest["synthesizer"] is None

    def test_unknown_reviewer_template_yields_phase_failure(self, repo_with_pr_doc):
        # A reviewer.template naming a non-existent file raises FileNotFoundError
        # in the render path; round.py must degrade to a phase-failure round
        # (exit 40) with artifacts, not crash out of run_review.
        repo, pr_doc = repo_with_pr_doc
        config = SyncadeConfig(
            reviewers=[
                {"name": "codex-reviewer", "provider": "openai", "model": "gpt-5.5"},
                {
                    "name": "codex-reviewer-adv",
                    "provider": "openai",
                    "model": "gpt-5.5",
                    "template": "does-not-exist.md",
                },
            ],
            loop={"max_rounds": 1},
        )
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(),
        )
        assert result.exit_code == 40
        assert adapters[0].invocations == []
        round_dir = _latest_run_dir(repo) / "round-0"
        assert (round_dir / "manifest.json").is_file()
