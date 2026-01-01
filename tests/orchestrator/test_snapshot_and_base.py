"""Snapshot / base-ref / manifest-schema orchestrator tests.

Moved verbatim from the former ``tests/test_orchestrator.py``. Fixtures
(``repo_with_pr_doc`` + the two autouse fixtures) are auto-discovered from
``tests/orchestrator/conftest.py``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import pytest

from syncade.adapters.fake import FakeAdapter
from syncade.logging import Logger
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import _factory_returning, _ship, _two_reviewer_config

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestDirtyTreeWarning:
    """When ``git status --porcelain`` returns output at snapshot time
    the orchestrator surfaces a stderr warning. PR-7.5 distinguishes
    two operationally-distinct cases:

    - ``"tracked"`` — strong warning: the operator has uncommitted
      modifications to tracked files (genuinely dangerous, the
      original PR-5.6 case).
    - ``"untracked"`` — soft note: the operator has scratch files
      they're usually intentionally keeping out of git (the
      Phase-04 Acme alpha-briefings case).

    Both messages go to stderr; both are suppressed in quiet mode
    (same severity tier — informational, not errors). Clean trees
    stay silent regardless of verbosity.
    """

    def test_tracked_modification_fires_strong_warning(self, repo_with_pr_doc, capsys):
        """Modifying a tracked file fires the strong "your local
        changes are invisible" message — the actually-dangerous
        case the original warning was designed for."""
        repo, pr_doc = repo_with_pr_doc
        # Modify the tracked README.md created by the fixture.
        (repo / "README.md").write_text("locally modified\n")

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            logger=Logger("normal"),
            adapter_factory=_factory_returning(*adapters),
        )
        captured = capsys.readouterr()
        # Strong warning signals: "uncommitted modifications to tracked"
        # and "your local changes are invisible".
        assert "uncommitted modifications to tracked" in captured.err.lower()
        assert "invisible to them" in captured.err.lower()
        assert "HEAD" in captured.err
        # Soft note (untracked variant) must NOT fire here.
        assert "untracked files" not in captured.err.lower()

    def test_untracked_only_fires_soft_note_not_strong_warning(self, repo_with_pr_doc, capsys):
        """The Phase-04 Acme regression: untracked-only files
        should fire the SOFT note ("usually intentional"), not the
        strong warning. This is the case PR-5.6's blanket warning
        conflated and PR-7.5 splits."""
        repo, pr_doc = repo_with_pr_doc
        # Untracked scratch file — no tracked file modified.
        (repo / "scratch.txt").write_text("not yet added\n")

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            logger=Logger("normal"),
            adapter_factory=_factory_returning(*adapters),
        )
        captured = capsys.readouterr()
        # Soft note signals: "untracked files" + "usually intentional".
        assert "untracked files" in captured.err.lower()
        assert "usually intentional" in captured.err.lower()
        # Strong-warning lead-in must NOT fire on the untracked-only
        # case — that's the Phase-04 conflation bug we're fixing.
        assert "uncommitted modifications to tracked" not in captured.err.lower()

    def test_both_states_fires_both_messages(self, repo_with_pr_doc, capsys):
        """Tracked-modified AND untracked-only co-occurring: the
        operator needs to know about both classes. Strong message
        first, soft note second."""
        repo, pr_doc = repo_with_pr_doc
        # Modify a tracked file.
        (repo / "README.md").write_text("locally modified\n")
        # AND add an untracked one.
        (repo / "scratch.txt").write_text("not yet added\n")

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            logger=Logger("normal"),
            adapter_factory=_factory_returning(*adapters),
        )
        captured = capsys.readouterr()
        # Both messages present
        assert "uncommitted modifications to tracked" in captured.err.lower()
        assert "untracked files" in captured.err.lower()
        # Order check: strong message comes first.
        tracked_idx = captured.err.lower().find("uncommitted modifications to tracked")
        untracked_idx = captured.err.lower().find("untracked files")
        assert tracked_idx != -1 and untracked_idx != -1
        assert tracked_idx < untracked_idx

    def test_clean_tree_emits_no_warning(self, repo_with_pr_doc, capsys):
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            logger=Logger("normal"),
            adapter_factory=_factory_returning(*adapters),
        )
        captured = capsys.readouterr()
        assert "uncommitted" not in captured.err.lower()
        assert "untracked" not in captured.err.lower()
        assert "warning" not in captured.err.lower()

    def test_dirty_tree_warnings_suppressed_in_quiet_mode(self, repo_with_pr_doc, capsys):
        """Both the strong warning and the soft note are
        informational (same severity tier) and ``--quiet``
        suppresses both. Same as the PR-5.6 quiet-mode contract;
        PR-7.5's split into two messages doesn't change it."""
        repo, pr_doc = repo_with_pr_doc
        # Cover both message paths
        (repo / "README.md").write_text("locally modified\n")
        (repo / "scratch.txt").write_text("not committed\n")
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            logger=Logger("quiet"),
            adapter_factory=_factory_returning(*adapters),
        )
        captured = capsys.readouterr()
        assert "uncommitted" not in captured.err.lower()
        assert "untracked" not in captured.err.lower()


# ---------------------------------------------------------------------------
# Base-ref flow-through
# ---------------------------------------------------------------------------


class TestBaseRefFlowThrough:
    def test_base_ref_diff_is_substituted_into_prompt(self, repo_with_pr_doc, monkeypatch):
        """Pass base_ref=HEAD~1 against a repo with two commits; assert
        the rendered prompt contains the second commit's diff (via the
        FakeAdapter's recorded `prompt` argument to build_invocation)."""
        repo, pr_doc = repo_with_pr_doc
        # Add a second commit so HEAD~1 resolves.
        (repo / "new.txt").write_text("added in second commit\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=repo, check=True)

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            base_ref="HEAD~1",
            adapter_factory=_factory_returning(*adapters),
        )
        # The first adapter's recorded prompt should contain the diff
        # text for the second commit.
        recorded_prompt = adapters[0].invocations[0][2]
        assert "new.txt" in recorded_prompt
        assert "added in second commit" in recorded_prompt

    def test_no_base_ref_inserts_no_diff_sentinel(self, repo_with_pr_doc):
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )
        recorded_prompt = adapters[0].invocations[0][2]
        # The no-diff sentinel from orchestrator._NO_DIFF_SENTINEL
        # must appear in the rendered prompt's {diff} substitution.
        assert "diff not provided" in recorded_prompt
        assert "full repo state at HEAD" in recorded_prompt


class TestReviewerDiffFilter:
    """PR-17 Part 2: the reviewer-facing diff has CLAUDE.md / AGENTS.md
    hunks stripped before it reaches the reviewer prompt. The worktree
    already strips those files; the diff strip closes the recurring
    "tracked deletion" false positive (the diff showed a file changing
    that was absent from the reviewer's worktree)."""

    # A sentinel unique enough that it can only appear if the CLAUDE.md
    # diff hunk leaked into the prompt. The template addendum MENTIONS
    # "CLAUDE.md", so asserting the bare string's absence would be wrong;
    # we assert on the hunk header + this content sentinel instead.
    _CLAUDE_SENTINEL = "CLAUDE_MEMORY_MUST_NOT_LEAK_TO_REVIEWER"
    _CODE_SENTINEL = "code_change_reviewers_should_see"

    def test_claude_md_hunk_stripped_from_reviewer_prompt(self, repo_with_pr_doc):
        """A diff touching both CLAUDE.md and a code file: the reviewer
        prompt must carry the code hunk but NOT the CLAUDE.md hunk."""
        repo, pr_doc = repo_with_pr_doc
        # Base commit: plant CLAUDE.md so it exists at HEAD~1.
        (repo / "CLAUDE.md").write_text("original project memory\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add CLAUDE.md"], cwd=repo, check=True)
        # HEAD: modify CLAUDE.md AND add a code file.
        (repo / "CLAUDE.md").write_text(f"original project memory\n{self._CLAUDE_SENTINEL}\n")
        (repo / "feature.py").write_text(f"# {self._CODE_SENTINEL}\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "modify CLAUDE.md + add code"], cwd=repo, check=True
        )

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            base_ref="HEAD~1",
            adapter_factory=_factory_returning(*adapters),
        )
        recorded_prompt = adapters[0].invocations[0][2]
        # Code change reaches the reviewer.
        assert self._CODE_SENTINEL in recorded_prompt
        assert "feature.py" in recorded_prompt
        # The CLAUDE.md hunk does NOT — neither its header nor its content.
        assert "diff --git a/CLAUDE.md" not in recorded_prompt
        assert self._CLAUDE_SENTINEL not in recorded_prompt

    def test_code_only_diff_reaches_reviewer_unchanged(self, repo_with_pr_doc):
        """Negative case: a diff with no stripped files is NOT
        over-filtered — the code hunk reaches the reviewer intact."""
        repo, pr_doc = repo_with_pr_doc
        (repo / "feature.py").write_text(f"# {self._CODE_SENTINEL}\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add code only"], cwd=repo, check=True)

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            base_ref="HEAD~1",
            adapter_factory=_factory_returning(*adapters),
        )
        recorded_prompt = adapters[0].invocations[0][2]
        assert self._CODE_SENTINEL in recorded_prompt
        assert "diff --git a/feature.py" in recorded_prompt


# ---------------------------------------------------------------------------
# Manifest schema
# ---------------------------------------------------------------------------


class TestManifestSchema:
    def test_manifest_has_expected_top_level_keys(self, repo_with_pr_doc):
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )
        manifest = json.loads(result.artifacts.manifest_path.read_text())
        for key in (
            "syncade_version",
            "run_id",
            "round",
            "started_at_utc",
            "snapshot",
            "reviewers",
            # PR-8 polish R1.T1: per-round key renamed exit_code →
            # round_exit_code.
            "round_exit_code",
        ):
            assert key in manifest, f"manifest missing {key!r}"
        # Snapshot section
        assert manifest["snapshot"]["commit_sha"] == result.snapshot.commit_sha
        assert manifest["snapshot"]["base_ref"] is None
        assert manifest["snapshot"]["diff_present"] is False
        # One reviewer entry per ReviewerConfig
        assert len(manifest["reviewers"]) == 2


# ---------------------------------------------------------------------------
# Run-start timestamp
# ---------------------------------------------------------------------------


class TestRunStartTimestamp:
    """PR-5.5 review fix: run_review captures the run-start instant ONCE
    and threads it into both persist_round_manifest and
    persist_run_summary, so manifest.json and summary.md agree on when
    the run began — not when each file happened to be written."""

    def test_manifest_and_summary_share_the_run_start_instant(self, repo_with_pr_doc, monkeypatch):
        import syncade.orchestrator.loop as loop_module

        frozen = datetime(2029, 3, 4, 5, 6, 7, tzinfo=UTC)

        class _FrozenDatetime:
            @staticmethod
            def now(tz=None):
                return frozen

        # run_review's only datetime.now() call is the started_at capture.
        monkeypatch.setattr(loop_module, "datetime", _FrozenDatetime)

        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )

        manifest = json.loads(result.artifacts.manifest_path.read_text())
        summary = result.artifacts.summary_path.read_text()
        # Both files render the SAME captured instant — not write time.
        assert manifest["started_at_utc"] == "2029-03-04T05:06:07Z"
        assert "**Started:** 2029-03-04 05:06:07 UTC" in summary
