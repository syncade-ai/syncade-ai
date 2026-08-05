"""Tests for :mod:`syncade.worktree`.

Uses real ``git`` subprocess calls against an ephemeral repo built under
``tmp_path`` — no mocking. The point is to know if the integration
actually works, not whether our wrapper happens to call subprocess with
the right arguments.
"""

from __future__ import annotations

import datetime as _datetime
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from syncade.process import SubprocessResult
from syncade.worktree import (
    Worktree,
    WorktreeError,
    WorktreeManager,
    generate_run_id,
)
from tests.worktree._helpers import _git

# Skip everything in this module if git isn't on PATH — equivalent to the
# brief's "pytest.importorskip" hint for a non-Python dependency.
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not found on PATH",
)

RUN_ID_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# generate_run_id
# ---------------------------------------------------------------------------


class TestGenerateRunId:
    def test_no_prefix_matches_documented_regex(self):
        rid = generate_run_id()
        assert RUN_ID_REGEX.match(rid), rid

    def test_collisions_within_a_second_allowed_per_documented_policy(self, monkeypatch):
        """Documented policy in the module docstring: resolution is one
        second; two calls inside the same wall-clock second produce
        identical ids. Stub datetime.now to return a fixed value and
        confirm the policy holds."""
        fixed = _datetime.datetime(2026, 5, 11, 12, 30, 0)

        class FixedDatetime:
            @staticmethod
            def now():
                return fixed

        monkeypatch.setattr("syncade.worktree.datetime", FixedDatetime)
        assert generate_run_id() == "2026-05-11T12-30-00"
        assert generate_run_id() == "2026-05-11T12-30-00"

    def test_junk_chars_in_prefix_are_stripped(self):
        """Strip policy: only ``[A-Za-z0-9_-]`` survive. Space, slash,
        and punctuation are removed entirely."""
        rid = generate_run_id("pr-2 / nope")
        # Allowed chars survive — the order is preserved
        assert rid.endswith("-pr-2nope"), rid
        # And no stripped char makes it through anywhere
        for c in " /!":
            assert c not in rid

    def test_all_junk_prefix_yields_bare_timestamp(self):
        rid = generate_run_id("!!!@@@###")
        assert RUN_ID_REGEX.match(rid), rid

    def test_empty_string_prefix_is_treated_as_none(self):
        assert RUN_ID_REGEX.match(generate_run_id(""))
        assert RUN_ID_REGEX.match(generate_run_id(None))

    def test_leading_and_trailing_separators_in_prefix_are_trimmed(self):
        """After sanitization, leading and trailing dashes / underscores
        are trimmed so the joined id doesn't read as ``...-T--leading`` or
        ``...-trailing-``. Internal separators are preserved."""
        # Leading dash → trimmed
        rid = generate_run_id("-leading")
        assert rid.endswith("-leading"), rid
        assert "--" not in rid, rid

        # Trailing dash → trimmed
        rid = generate_run_id("trailing-")
        assert rid.endswith("-trailing"), rid

        # Leading + trailing underscore → trimmed
        rid = generate_run_id("_under_")
        assert rid.endswith("-under"), rid

        # Bare separator → empty after trim → bare timestamp
        assert RUN_ID_REGEX.match(generate_run_id("-"))
        assert RUN_ID_REGEX.match(generate_run_id("___"))

        # Internal separators are preserved (regression: don't collapse
        # legitimate internal patterns like "pr-2")
        assert generate_run_id("pr-2").endswith("-pr-2")
        assert generate_run_id("a_b").endswith("-a_b")


# ---------------------------------------------------------------------------
# WorktreeManager.create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_routes_git_calls_through_run_subprocess_with_timeout(
        self, repo, base_dir, monkeypatch
    ):
        repo_path, sha = repo
        calls = []

        def fake_run_subprocess(argv, *, cwd=None, env=None, timeout=None, input_text=None):
            del env, input_text
            calls.append((argv, cwd, timeout))
            if argv[:4] == ["git", "--no-replace-objects", "worktree", "add"]:
                return SubprocessResult(0, "", "", 0.0)
            if argv == ["git", "rev-parse", "HEAD"]:
                return SubprocessResult(0, f"{sha}\n", "", 0.0)
            if argv[:3] == ["git", "worktree", "remove"]:
                return SubprocessResult(0, "", "", 0.0)
            if argv == ["git", "worktree", "prune"]:
                return SubprocessResult(0, "", "", 0.0)
            raise AssertionError(f"unexpected command: {argv!r}")

        monkeypatch.setattr("syncade.worktree.run_subprocess", fake_run_subprocess, raising=False)
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)

        try:
            wt = mgr.create("claude-reviewer", sha)

            assert wt.commit_sha == sha
            assert calls[:2] == [
                (
                    ["git", "--no-replace-objects", "worktree", "add", str(wt.path), sha],
                    repo_path,
                    30.0,
                ),
                (["git", "rev-parse", "HEAD"], wt.path, 30.0),
            ]
        finally:
            mgr.cleanup_all()

    def test_create_produces_worktree_at_expected_path_with_source_files(self, repo, base_dir):
        repo_path, sha = repo
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        wt = mgr.create("claude-reviewer", sha)
        assert isinstance(wt, Worktree)
        assert wt.path == (base_dir / "run-1" / "claude-reviewer").absolute()
        assert wt.path.is_dir()
        # File from the source commit is present
        assert (wt.path / "README.md").read_text() == "hello\n"
        assert wt.reviewer_name == "claude-reviewer"
        assert wt.commit_sha == sha

    def test_create_strips_listed_files(self, repo, base_dir):
        repo_path, sha = repo
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        wt = mgr.create("claude-reviewer", sha, strip_files=["CLAUDE.md"])
        # CLAUDE.md was in the source commit but is gone from the worktree
        assert not (wt.path / "CLAUDE.md").exists()
        # Other files survive
        assert (wt.path / "README.md").exists()

    def test_create_strips_nested_context_files(self, repo, base_dir):
        repo_path, _ = repo
        nested = repo_path / "subdir"
        nested.mkdir()
        (nested / "CLAUDE.md").write_text("nested claude secret\n")
        (nested / "AGENTS.md").write_text("nested agents secret\n")
        (nested / "keep.md").write_text("public content\n")
        _git(repo_path, "add", "-A")
        _git(repo_path, "commit", "-m", "add nested context files")
        sha = _git(repo_path, "rev-parse", "HEAD").stdout.strip()

        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        wt = mgr.create(
            "claude-reviewer",
            sha,
            strip_files=["CLAUDE.md", "AGENTS.md"],
        )

        assert not (wt.path / "CLAUDE.md").exists()
        assert not (wt.path / "subdir" / "CLAUDE.md").exists()
        assert not (wt.path / "subdir" / "AGENTS.md").exists()
        assert (wt.path / "subdir" / "keep.md").exists()

    def test_create_strip_missing_file_is_not_an_error(self, repo, base_dir):
        repo_path, sha = repo
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        # AGENTS.md is not in the source commit — should be a silent no-op
        wt = mgr.create(
            "claude-reviewer",
            sha,
            strip_files=["AGENTS.md", "CLAUDE.md"],
        )
        assert wt.path.is_dir()
        assert not (wt.path / "AGENTS.md").exists()
        assert not (wt.path / "CLAUDE.md").exists()

    @pytest.mark.parametrize(
        "bad_name",
        [
            "",
            ".",
            "..",
            "../escape",
            "subdir/inner",
            "a\\b",
        ],
    )
    def test_create_rejects_invalid_reviewer_name(self, repo, base_dir, tmp_path, bad_name):
        """reviewer_name must be a plain basename. Each invalid form
        raises WorktreeError BEFORE any filesystem or git side effects
        — run_dir stays empty, no new worktree registration appears,
        manager bookkeeping is untouched."""
        repo_path, sha = repo
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        before = _git(repo_path, "worktree", "list").stdout
        with pytest.raises(WorktreeError) as exc_info:
            mgr.create(bad_name, sha)
        assert "invalid reviewer_name" in str(exc_info.value)
        # run_dir is either absent or still empty (no directory created)
        assert not mgr.run_dir.exists() or not any(mgr.run_dir.iterdir())
        # No new worktree registered with git
        after = _git(repo_path, "worktree", "list").stdout
        assert before == after, f"git registration changed: {before!r} -> {after!r}"
        # Manager bookkeeping wasn't touched
        assert mgr._worktrees == []

    def test_create_rejects_absolute_reviewer_name(self, repo, base_dir, tmp_path):
        """Absolute paths as reviewer_name must also be rejected up
        front. Parametrized fixture above can't carry tmp_path-derived
        values, so this lives as its own test."""
        repo_path, sha = repo
        escape_target = tmp_path / "escape-target"
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        before = _git(repo_path, "worktree", "list").stdout
        with pytest.raises(WorktreeError) as exc_info:
            mgr.create(str(escape_target), sha)
        assert "invalid reviewer_name" in str(exc_info.value)
        # The absolute-target directory must not have been created
        assert not escape_target.exists()
        # And no new worktree registration appears
        after = _git(repo_path, "worktree", "list").stdout
        assert before == after

    def test_create_strip_files_refuses_to_escape_worktree(self, repo, base_dir, tmp_path):
        """Path-traversal entries in strip_files must not delete files
        outside the worktree (security invariant: brief's "matched at
        the worktree root only"). Malformed entries are silently
        skipped, mirroring the missing-file behavior."""
        repo_path, sha = repo
        # Sentinel sitting under tmp_path, outside the worktree.
        sentinel_rel = tmp_path / "sentinel-rel.txt"
        sentinel_abs = tmp_path / "sentinel-abs.txt"
        sentinel_rel.write_text("must-survive\n")
        sentinel_abs.write_text("must-survive\n")
        # CLAUDE.md is a real file at the worktree root — verify the
        # well-formed entry still works while the malformed ones get
        # silently skipped.
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        wt = mgr.create(
            "claude-reviewer",
            sha,
            strip_files=[
                "CLAUDE.md",  # well-formed: should be stripped
                "../../sentinel-rel.txt",  # parent-ref: skipped
                str(sentinel_abs),  # absolute path: skipped
                "subdir/CLAUDE.md",  # contains separator: skipped
                "..",  # bare parent-ref: skipped
                ".",  # current dir: skipped
                "",  # empty: skipped
            ],
        )
        # Well-formed entry was honored
        assert not (wt.path / "CLAUDE.md").exists()
        # Sentinels outside the worktree must be intact
        assert sentinel_rel.exists(), "relative-traversal entry escaped!"
        assert sentinel_abs.exists(), "absolute-path entry escaped!"

    def test_create_with_bogus_sha_raises_worktree_error(self, repo, base_dir):
        repo_path, _ = repo
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        with pytest.raises(WorktreeError) as exc_info:
            mgr.create(
                "claude-reviewer",
                "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            )
        assert str(exc_info.value).startswith("git worktree add failed:")

    def test_create_canonicalizes_short_sha_to_full_sha(self, repo, base_dir):
        """The Worktree.commit_sha field is contractually the full
        object ID. A caller passing a short SHA must get the
        canonical full form back in the record."""
        repo_path, full_sha = repo
        short = full_sha[:8]
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        wt = mgr.create("claude-reviewer", short)
        assert wt.commit_sha == full_sha
        assert len(wt.commit_sha) == 40

    def test_create_rolls_back_when_post_add_step_fails(self, repo, base_dir, monkeypatch):
        """If a step AFTER `git worktree add` succeeds raises (here,
        a simulated rev-parse failure), the worktree must be rolled
        back: no orphan directory, no orphan git registration, and the
        manager's _worktrees list must not gain a record."""
        repo_path, sha = repo
        target = (base_dir / "run-1" / "claude-reviewer").absolute()

        import syncade.worktree as worktree_module

        real_run_subprocess = worktree_module.run_subprocess

        def selective_run_subprocess(argv, *, cwd=None, env=None, timeout=None, input_text=None):
            # Narrow monkeypatch: only `git rev-parse HEAD` is faked.
            # `git worktree add` and `git worktree remove` (used by
            # the rollback) run as real subprocesses so we're testing
            # the actual rollback path, not a hermetic mock.
            if (
                isinstance(argv, list)
                and len(argv) >= 3
                and argv[:3] == ["git", "rev-parse", "HEAD"]
            ):
                return SubprocessResult(1, "", "simulated rev-parse failure", 0.0)
            return real_run_subprocess(
                argv,
                cwd=cwd,
                env=env,
                timeout=timeout,
                input_text=input_text,
            )

        monkeypatch.setattr("syncade.worktree.run_subprocess", selective_run_subprocess)

        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        with pytest.raises(WorktreeError) as exc_info:
            mgr.create("claude-reviewer", sha)

        assert "git rev-parse HEAD failed" in str(exc_info.value)
        # Worktree directory is gone — no orphan on disk
        assert not target.exists()
        # git no longer registers it
        listing = subprocess.run(
            ["git", "worktree", "list"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert str(target) not in listing
        # Manager bookkeeping is clean
        assert mgr._worktrees == []

    def test_create_when_target_dir_already_exists_raises(self, repo, base_dir):
        repo_path, sha = repo
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        # Pre-create the target so create() should refuse
        target = base_dir / "run-1" / "claude-reviewer"
        target.mkdir(parents=True)
        with pytest.raises(WorktreeError) as exc_info:
            mgr.create("claude-reviewer", sha)
        msg = str(exc_info.value)
        assert msg.startswith("git worktree add failed:")
        assert "already exists" in msg

    def test_create_raises_when_run_dir_exists_as_a_file(self, repo, base_dir):
        """If `base_dir / run_id` exists as a file (not a directory),
        `mkdir(parents=True, exist_ok=True)` raises FileExistsError.
        That class of provisioning failure must surface as
        WorktreeError so the CLI can map it to exit 60 instead of
        leaking a raw OSError to the user."""
        repo_path, sha = repo
        # Make base_dir's parent first, then place a file where run_dir
        # would otherwise live.
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "run-1").write_text("not a directory\n")

        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        with pytest.raises(WorktreeError) as exc_info:
            mgr.create("claude-reviewer", sha)
        msg = str(exc_info.value)
        assert msg.startswith("git worktree add failed:")
        assert "run_dir" in msg
        # No git registration created
        listing = _git(repo_path, "worktree", "list").stdout
        assert "claude-reviewer" not in listing

    def test_create_replacement_ref_cannot_substitute_checkout_content(
        self, tmp_path: Path, base_dir: Path
    ) -> None:
        """A `refs/replace/*` ref swaps one object for another at every lookup.
        Pre-fix: `git worktree add <sha>` without --no-replace-objects checked
        out the benign replacement while `commit_sha` named the original, so a
        backdoored commit was reviewed as its benign replacement.

        The worktree must contain the ORIGINAL content, not the replacement."""
        from tests.worktree._helpers import _make_repo

        repo_path = tmp_path / "repo"
        _make_repo(repo_path, {"s.py": "BACKDOOR\n"})
        bad_sha = _git(repo_path, "rev-parse", "HEAD").stdout.strip()

        # Create a benign replacement commit
        (repo_path / "s.py").write_text("tidy\n")
        _git(repo_path, "add", "-A")
        _git(repo_path, "commit", "-m", "benign replacement")
        benign_sha = _git(repo_path, "rev-parse", "HEAD").stdout.strip()

        # Reset back to the backdoored commit and install the replace ref
        _git(repo_path, "reset", "-q", "--hard", bad_sha)
        _git(repo_path, "replace", "-f", bad_sha, benign_sha)

        mgr = WorktreeManager(repo_path, "run-replace", base_dir=base_dir)
        wt = mgr.create("reviewer", bad_sha)
        try:
            content = (wt.path / "s.py").read_text()
            assert content == "BACKDOOR\n", (
                "replacement ref substituted checkout content — "
                f"got {content!r}, expected 'BACKDOOR\\n'"
            )
            assert wt.commit_sha == bad_sha
        finally:
            mgr.cleanup_all()
