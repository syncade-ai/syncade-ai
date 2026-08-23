"""Pipeline-level orchestrator tests (cold-synth env scrub, happy path,
findings present, reviewer failures, unknown provider, worktree error).

Moved verbatim from the former ``tests/test_orchestrator.py`` monolith.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.config import SyncadeConfig
from syncade.findings import ReviewerOutputError
from syncade.orchestrator import RunArtifacts, RunResult, run_review
from syncade.worktree import WorktreeError
from tests.orchestrator._helpers import (
    _factory_returning,
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


class TestColdSynthEnvScrub:
    def test_path_scrub_preserves_sibling_prefix_paths(self, tmp_path):
        """Repo-local PATH segments are removed without dropping siblings."""
        from syncade.synthesizer import _scrub_env_for_cold_synth

        repo = tmp_path / "syncade"
        repo.mkdir()
        repo_local_bin = repo / ".venv" / "bin"
        sibling_bin = tmp_path / "syncade-dev" / "bin"
        env = {
            "PATH": os.pathsep.join([str(sibling_bin), str(repo_local_bin), ".", "/usr/local/bin"]),
            "PWD": str(repo),
            "OLDPWD": str(tmp_path),
            "VIRTUAL_ENV": str(repo / ".venv"),
            "SIBLING_HOME": str(tmp_path / "syncade-dev"),
            "SIBLING_NOTE": f"{repo}-dev is not the repo",
            "TOKEN": "kept",
        }

        scrubbed = _scrub_env_for_cold_synth(env, repo)

        assert "PWD" not in scrubbed
        assert "OLDPWD" not in scrubbed
        assert "VIRTUAL_ENV" not in scrubbed
        assert scrubbed["SIBLING_HOME"] == str(tmp_path / "syncade-dev")
        assert scrubbed["SIBLING_NOTE"] == f"{repo}-dev is not the repo"
        assert scrubbed["TOKEN"] == "kept"

        path_segments = scrubbed["PATH"].split(os.pathsep)
        assert str(sibling_bin) in path_segments
        assert "." in path_segments
        assert "/usr/local/bin" in path_segments
        assert str(repo_local_bin) not in path_segments

    def test_scalar_scrub_uses_path_boundaries(self, tmp_path):
        """A sibling path containing the repo name prefix is not a leak."""
        from syncade.synthesizer import _scrub_env_for_cold_synth

        repo = tmp_path / "syncade"
        repo.mkdir()
        scrubbed = _scrub_env_for_cold_synth(
            {
                "REPO_FILE": f"read {repo}/secrets.txt",
                "SIBLING_FILE": f"read {repo}-dev/secrets.txt",
            },
            repo,
        )

        assert "REPO_FILE" not in scrubbed
        assert scrubbed["SIBLING_FILE"] == f"read {repo}-dev/secrets.txt"

    def test_scalar_scrub_catches_symlink_alias_of_repo_root(self, tmp_path):
        """A scalar env value using a symlink alias of the repo root is scrubbed.

        On macOS /tmp is a symlink to /private/tmp, so VIRTUAL_ENV may be set to
        /tmp/<repo>/.venv while the resolved repo root is /private/tmp/<repo>.
        Previously _value_references_repo_path resolved the repo root internally,
        collapsing both forms to the canonical string, so the alias spelling was
        never found in the value and leaked into the cold subprocess.

        Regression: create an actual filesystem symlink to simulate two distinct
        string spellings of the same directory.
        """
        from syncade.synthesizer import _scrub_env_for_cold_synth

        real_base = tmp_path / "real"
        real_base.mkdir()
        alias_base = tmp_path / "alias"
        alias_base.symlink_to(real_base)

        repo_real = real_base / "myrepo"
        repo_real.mkdir()
        repo_via_alias = alias_base / "myrepo"

        # The env value spells the repo root through the symlink alias.
        env = {
            "VIRTUAL_ENV": str(repo_via_alias / ".venv"),
            "CACHE_DIR": f"prefix {repo_via_alias}/cache suffix",
            "TOKEN": "kept",
        }

        # Pass the repo via the symlink path so str(repo_root) ≠ str(repo_root.resolve()).
        scrubbed = _scrub_env_for_cold_synth(env, repo_via_alias)

        assert "VIRTUAL_ENV" not in scrubbed, "alias-spelled VIRTUAL_ENV must be scrubbed"
        assert "CACHE_DIR" not in scrubbed, "alias-spelled CACHE_DIR must be scrubbed"
        assert scrubbed["TOKEN"] == "kept"

    def test_scalar_scrub_catches_alias_when_caller_passes_canonical_root(self, tmp_path):
        """The same leak, but with the repo root passed in CANONICAL form — which is
        what production actually does.

        This is the case that matters and the one the first fix missed. Enumerating
        spellings of the ROOT (`{str(repo_root), str(repo_root.resolve())}`) only
        helps when the caller hands you an UNRESOLVED path. The orchestrator resolves
        the repo root in `snapshot`, so both spellings collapse to the canonical one,
        the alias is never in the set, and an alias-spelled env value sails through.

        The fix is to resolve the VALUE's tokens rather than the root: symlink
        aliasing is unbounded, so you cannot enumerate your way out of it, but
        resolving the candidate collapses both spellings to the same real path.
        """
        from syncade.synthesizer import _scrub_env_for_cold_synth

        real_base = tmp_path / "real"
        real_base.mkdir()
        alias_base = tmp_path / "alias"
        alias_base.symlink_to(real_base)

        repo_real = real_base / "myrepo"
        repo_real.mkdir()
        repo_via_alias = alias_base / "myrepo"

        # A sibling that merely shares a string prefix must NOT be scrubbed — the
        # boundary property the old substring scan was protecting.
        sibling = real_base / "myrepo-sibling"
        sibling.mkdir()

        env = {
            "VIRTUAL_ENV": str(repo_via_alias / ".venv"),
            "CACHE_DIR": f"prefix {repo_via_alias}/cache suffix",
            "FLAGS": f"--prefix={repo_via_alias}/opt",
            "SIBLING": str(sibling / "x"),
            "TOKEN": "kept",
        }

        # Caller passes the CANONICAL root, exactly as the orchestrator does.
        scrubbed = _scrub_env_for_cold_synth(env, repo_real)

        assert "VIRTUAL_ENV" not in scrubbed, "alias-spelled VIRTUAL_ENV leaked to the cold synth"
        assert "CACHE_DIR" not in scrubbed, "alias-spelled CACHE_DIR leaked to the cold synth"
        assert "FLAGS" not in scrubbed, "alias-spelled path inside a flag leaked"
        assert scrubbed["SIBLING"] == str(sibling / "x"), "sibling-prefix false positive"
        assert scrubbed["TOKEN"] == "kept"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_two_ship_reviewers_exit_zero_with_all_artifacts(self, repo_with_pr_doc):
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )

        assert isinstance(result, RunResult)
        assert isinstance(result.artifacts, RunArtifacts)
        assert result.exit_code == 0
        assert result.dispatch_result.all_succeeded

        round_dir = result.artifacts.round_dir
        # Every reviewer wrote stdout / stderr / parsed.json; no error.txt.
        for name in ("rv1", "rv2"):
            assert (round_dir / f"{name}.stdout").is_file()
            assert (round_dir / f"{name}.stderr").is_file()
            assert (round_dir / f"{name}.parsed.json").is_file()
            assert not (round_dir / f"{name}.error.txt").exists()
        # Manifest exists and parses
        manifest = json.loads(result.artifacts.manifest_path.read_text())
        # PR-8 polish R1.T1: per-round key renamed exit_code →
        # round_exit_code for consistency with loop-manifest.json.
        assert manifest["round_exit_code"] == 0


# ---------------------------------------------------------------------------
# Findings (NO-SHIP)
# ---------------------------------------------------------------------------


class TestFindingsPresent:
    def test_two_no_ship_reviewers_exit_30(self, repo_with_pr_doc):
        """PR-7: verdict is mechanical from the synthesizer's
        consolidated findings, not from reviewer verdict strings. Two
        NO-SHIP reviewers with findings → the synthesizer (here a
        fake driven by ``_synth_with_blocker``) surfaces an active
        blocker → exit 30."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_no_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_with_blocker()),
        )
        assert result.exit_code == 30
        # Reviewers' parsed.json still capture their per-reviewer NO-SHIP
        # verdict; the mechanical verdict layer lives in the synthesizer
        # phase, not the reviewer phase.
        parsed1 = json.loads((result.artifacts.round_dir / "rv1.parsed.json").read_text())
        assert parsed1["verdict"] == "NO-SHIP"
        assert len(parsed1["findings"]) == 1

    def test_mixed_verdicts_one_ship_one_no_ship_exit_30(self, repo_with_pr_doc):
        """Mixed reviewer verdicts still reach the synthesizer; the
        synth's consolidated findings drive the mechanical verdict.
        Inject a fake synth that surfaces an active blocker → exit 30.

        QA fix P0.2: synth provenance must reference a reviewer that
        actually produced findings. rv1=SHIP (0 findings), so the
        synth's provenance references rv2 instead (which produces 1
        finding via ``_no_ship()``).
        """
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_no_ship()),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(
                canned_output=_synth_with_blocker(reviewer_name="rv2"),
            ),
        )
        assert result.exit_code == 30


# ---------------------------------------------------------------------------
# Reviewer failures
# ---------------------------------------------------------------------------


class TestReviewerFailures:
    def test_one_invocation_error_exit_40(self, repo_with_pr_doc):
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(
                canned_exception=ReviewerInvocationError(
                    "auth failed", returncode=1, stdout="", stderr=""
                )
            ),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )
        assert result.exit_code == 40
        # The failed reviewer has an error.txt; the successful one has parsed.json
        assert (result.artifacts.round_dir / "rv1.parsed.json").is_file()
        assert (result.artifacts.round_dir / "rv2.error.txt").is_file()

    def test_one_output_error_exit_70(self, repo_with_pr_doc):
        """ReviewerOutputError maps to exit 70 — parsing failure is a
        distinct category from subprocess failure in PRD Appendix C."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_exception=ReviewerOutputError("unparseable")),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )
        assert result.exit_code == 70

    def test_mixed_output_and_invocation_errors_parse_wins(self, repo_with_pr_doc):
        """Mixed-failure precedence (documented in run_review's
        docstring): when a single dispatch produces both a
        ReviewerOutputError AND a ReviewerInvocationError, exit 70
        wins over exit 40. Rationale: parse failures point at a
        distinct fix path (tighten the prompt) and shouldn't be
        masked by a sibling reviewer's subprocess failure."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_exception=ReviewerOutputError("garbage")),
            FakeAdapter(
                canned_exception=ReviewerInvocationError(
                    "auth failed", returncode=1, stdout="", stderr=""
                )
            ),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )
        assert result.exit_code == 70, (
            "ReviewerOutputError must take precedence over "
            "ReviewerInvocationError per the decision table"
        )


# ---------------------------------------------------------------------------
# Unknown provider
# ---------------------------------------------------------------------------


class TestUnknownProvider:
    def test_unknown_provider_exit_50(self, repo_with_pr_doc):
        """The default adapter_factory (real registry) doesn't know
        'not-a-provider'; the dispatcher records a ValueError naming
        the provider, and the orchestrator's decision table maps that
        to CONFIG_ERROR (50)."""
        repo, pr_doc = repo_with_pr_doc
        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "anthropic", "model": "x"},
                {"name": "rv2", "provider": "not-a-provider", "model": "y"},
            ],
            loop={"max_rounds": 1},  # PR-8 single-pass back-compat
        )
        # No adapter_factory override — use the real registry
        result = run_review(repo_root=repo, pr_doc_path=pr_doc, config=config)
        assert result.exit_code == 50


# ---------------------------------------------------------------------------
# Worktree error
# ---------------------------------------------------------------------------


class TestWorktreeError:
    def test_reviewer_workspace_provisioning_error_propagates(self, repo_with_pr_doc, monkeypatch):
        """A reviewer-workspace provisioning failure (pre-dispatch) raises
        WorktreeError straight out of run_review rather than landing in the
        DispatchResult, so the CLI maps directly to exit 60 without writing a
        stale manifest.

        Historically this was reached via two reviewers sharing a name —
        their workspace directories overlapped and the second ``create``
        collided. Config-load now rejects duplicate reviewer names up front
        (see tests/config/test_config_schema.py::test_duplicate_reviewer_names_rejected),
        so that collision can no longer be constructed via config. The
        propagation invariant is still real, so simulate the collision by
        forcing the second reviewer's ``manager.create`` to raise: the first
        workspace provisions, the second fails, and the error must propagate
        out untouched."""
        repo, pr_doc = repo_with_pr_doc
        from syncade.reviewer_workspace import ReviewerWorkspaceManager

        real_create = ReviewerWorkspaceManager.create

        def patched_create(self, reviewer_name, commit_sha, strip_files=None):
            if reviewer_name == "rv2":
                raise WorktreeError("test injection: simulated overlapping worktree directory")
            return real_create(
                self,
                reviewer_name=reviewer_name,
                commit_sha=commit_sha,
                strip_files=strip_files,
            )

        monkeypatch.setattr(ReviewerWorkspaceManager, "create", patched_create)

        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 1},  # PR-8 single-pass back-compat
        )
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        with pytest.raises(WorktreeError, match="test injection"):
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=config,
                adapter_factory=_factory_returning(*adapters),
            )


class TestCodexHomeSurvivesTheColdScrub:
    """The auth gate probes `codex login status` with the PARENT env; the cold actor runs
    with the SCRUBBED env. If CODEX_HOME points inside the repo, the scrub stripped it, so
    the cold actor's codex credential differed from the one the gate verified -- "verified
    == runtime" broken. CODEX_HOME is codex's login dir (not the source under review), so
    it is preserved; a repo-referencing SECRET is still stripped."""

    def test_codex_home_is_kept_but_repo_refs_are_stripped(self):
        from pathlib import Path

        from syncade.synthesizer.workspace import _scrub_env_for_cold_synth

        repo = Path("/tmp/myrepo")
        env = {
            "PATH": "/usr/bin",
            "CODEX_HOME": "/tmp/myrepo/.codex",  # repo-local login dir
            "TOOL_CACHE": "/tmp/myrepo/.cache",  # repo ref, not auth -> stripped
        }
        out = _scrub_env_for_cold_synth(env, repo)
        assert out.get("CODEX_HOME") == "/tmp/myrepo/.codex", (
            "CODEX_HOME was stripped -- the cold actor would re-auth to a different "
            "credential than the gate verified"
        )
        assert "TOOL_CACHE" not in out, "a non-auth repo reference must still be scrubbed"
