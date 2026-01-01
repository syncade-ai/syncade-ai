"""Run-layout orchestrator tests.

Moved verbatim from the former ``tests/test_orchestrator.py``:
``TestRunsGitignore``, ``TestRunIdTmpCollision``, ``TestRepoRootDiscovery``,
``TestTimeoutResolution``, ``TestLifecycleLogging``.

Fixtures (``repo_with_pr_doc`` + the two autouse fixtures
``_isolated_worktree_base`` / ``_default_to_fake_synthesizer``) come from
``tests/orchestrator/conftest.py``.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from syncade.adapters.fake import FakeAdapter
from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import _factory_returning, _ship, _two_reviewer_config

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestRunsGitignore:
    """The orchestrator auto-writes <repo>/.syncade/runs/.gitignore on
    first run so consumer repos don't accidentally commit run history
    via `git add -A`. Pre-existing user gitignores are preserved."""

    def test_gitignore_is_created_on_first_run_with_star_content(self, repo_with_pr_doc):
        repo, pr_doc = repo_with_pr_doc
        # The runs dir does not exist before the first run
        runs_root = repo / ".syncade" / "runs"
        assert not runs_root.exists()

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )

        gitignore = runs_root / ".gitignore"
        assert gitignore.is_file(), ".gitignore missing under .syncade/runs/"
        assert gitignore.read_text() == "*\n", (
            f"unexpected .gitignore content: {gitignore.read_text()!r}"
        )

    def test_existing_user_gitignore_is_preserved(self, repo_with_pr_doc):
        repo, pr_doc = repo_with_pr_doc
        runs_root = repo / ".syncade" / "runs"
        runs_root.mkdir(parents=True)
        custom = "# project-specific rules\n*\n!keep-this-run/\n"
        (runs_root / ".gitignore").write_text(custom)

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )

        assert (runs_root / ".gitignore").read_text() == custom, (
            "user's pre-existing .gitignore was overwritten — must preserve"
        )

    def test_gitignore_unchanged_across_multiple_runs(self, repo_with_pr_doc):
        repo, pr_doc = repo_with_pr_doc

        for _ in range(3):
            adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=_two_reviewer_config(),
                adapter_factory=_factory_returning(*adapters),
            )

        gitignore = repo / ".syncade" / "runs" / ".gitignore"
        assert gitignore.is_file()
        assert gitignore.read_text() == "*\n"


class TestPrDocArtifact:
    def test_generated_pr_doc_is_persisted_under_run_dir_before_run_init(
        self, repo_with_pr_doc, tmp_path
    ):
        repo, _pr_doc = repo_with_pr_doc
        generated = tmp_path / "syncade-openspec-add-auth.md"
        generated.write_text("# OpenSpec\n\nUsers need login.\n", encoding="utf-8")

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=generated,
            pr_doc_artifact_name=generated.name,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )

        persisted = result.artifacts.run_dir / generated.name
        assert persisted.is_file()
        assert persisted.read_text(encoding="utf-8") == generated.read_text(encoding="utf-8")
        run_init = json.loads((result.artifacts.run_dir / "run-init.json").read_text())
        assert run_init["pr_doc_path"] == str(persisted.resolve())


class TestRunIdTmpCollision:
    """When a prior failed run preserved /tmp/syncade/<run-id>/ for
    debugging (intentional per WorktreeManager's exception-exit
    contract), a new run in the same wall-clock second must NOT reuse
    that same /tmp directory. The collision check looks at both the
    repo's .syncade/runs/<run-id>/ AND /tmp/syncade/<run-id>/."""

    def test_preexisting_tmp_run_dir_forces_suffix(self, repo_with_pr_doc, monkeypatch, tmp_path):
        """Stub DEFAULT_WORKTREE_BASE so we can plant a fake
        /tmp/syncade/<base_run_id>/ before run_review starts. Pin
        generate_run_id to a fixed value so the test is deterministic.

        The orchestrator should see the planted dir and pick
        <base_run_id>-2 instead, even though the repo-side dir
        was free."""
        import syncade.orchestrator.loop as loop_module
        from syncade.worktree import generate_run_id

        # Pin run_id so we can plant the colliding tmp dir
        fixed_id = generate_run_id("collision-test")
        monkeypatch.setattr(loop_module, "generate_run_id", lambda *a, **kw: fixed_id)

        # Redirect the worktree base to a tmp_path subdir so we
        # don't touch /tmp/syncade/ on the developer's machine.
        fake_base = tmp_path / "fake-tmp-syncade"
        fake_base.mkdir()
        # Patch the WorktreeManager default (worktree.DEFAULT_WORKTREE_BASE) for any
        # code path that reads it directly; the explicit worktree_base= kwarg below is
        # the authoritative source for run_review.
        import syncade.worktree as wt_module

        monkeypatch.setattr(wt_module, "DEFAULT_WORKTREE_BASE", fake_base)

        # Plant a stale /tmp dir at the fixed run_id
        stale = fake_base / fixed_id
        stale.mkdir()
        (stale / "marker.txt").write_text("from a prior failed run\n")

        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        # Pass worktree_base= explicitly so run_review uses fake_base
        # (config.worktree_base defaults to the autouse fixture's test_base, not fake_base).
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            worktree_base=fake_base,
        )

        # The new run got the -2 suffix, not the colliding id
        new_run_id = result.artifacts.run_dir.name
        assert new_run_id == f"{fixed_id}-2", (
            f"expected suffix, got {new_run_id!r} — collision check didn't fire"
        )
        # The stale dir was left undisturbed (marker still readable)
        assert (stale / "marker.txt").read_text() == "from a prior failed run\n"
        # The successful run completed
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# config.worktree_base as authoritative fallback (PR-v2-9 regression)
# ---------------------------------------------------------------------------


class TestWorktreeBaseConfig:
    """PR-v2-9: config.worktree_base must be authoritative when run_review
    is called without an explicit worktree_base= kwarg. Previously the loop
    fell back to the module-level DEFAULT_WORKTREE_BASE, ignoring the config
    field for direct API callers."""

    def test_config_worktree_base_is_used_without_explicit_kwarg(
        self, repo_with_pr_doc, tmp_path, monkeypatch
    ):
        """Calling run_review(config=...) without worktree_base= must provision
        worktrees under config.worktree_base, not the module-level constant.

        Regression for the direct API path: the CLI wrapper always passed
        config.worktree_base as the kwarg, so the config field appeared to work.
        But a direct call without the kwarg fell back to DEFAULT_WORKTREE_BASE,
        ignoring the config field entirely.
        """
        from syncade.config import SyncadeConfig
        from syncade.worktree import WorktreeManager

        custom_base = tmp_path / "custom-worktree-base"
        custom_base.mkdir()

        # Intercept WorktreeManager to capture the base_dir actually used.
        captured_bases: list = []
        OriginalWM = WorktreeManager

        class _CapturingWM(OriginalWM):
            def __init__(self, repo_root, run_id, *, base_dir, **kwargs):
                captured_bases.append(base_dir)
                super().__init__(repo_root, run_id, base_dir=base_dir, **kwargs)

        import syncade.orchestrator.round as round_module

        monkeypatch.setattr(round_module, "WorktreeManager", _CapturingWM)

        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        # No worktree_base= kwarg — only config.worktree_base is set.
        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 1},
            worktree_base=custom_base,
        )
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=_factory_returning(*adapters),
        )

        assert result.exit_code == 0
        # Worktrees must have been provisioned under custom_base.
        assert captured_bases, "WorktreeManager was never instantiated"
        for base in captured_bases:
            assert base == custom_base, (
                f"WorktreeManager base_dir was {base!r}, expected {custom_base!r} — "
                "config.worktree_base was ignored in favour of DEFAULT_WORKTREE_BASE"
            )


# ---------------------------------------------------------------------------
# Bad worktree_base values → WorktreeError (PR-v2-9 regression)
# ---------------------------------------------------------------------------


class TestBadWorktreeBaseErrors:
    """A configurable worktree_base that can't be used for mkdir must raise
    WorktreeError (exit 60), not propagate an OS exception to exit 1/2."""

    def test_file_as_worktree_base_raises_worktree_error(self, repo_with_pr_doc, tmp_path):
        """When worktree_base is a FILE (not a dir), mkdir(parents=True) raises
        NotADirectoryError. Without the fix this escapes as exit 2; with it, it
        is caught and re-raised as WorktreeError → exit 60."""
        from syncade.worktree import WorktreeError

        file_base = tmp_path / "not_a_dir"
        file_base.write_text("I am a file\n")

        repo, pr_doc = repo_with_pr_doc
        config = _two_reviewer_config()
        config = config.model_copy(update={"worktree_base": file_base})

        with pytest.raises(WorktreeError, match="worktree_base"):
            run_review(
                repo_root=repo,
                pr_doc_path=pr_doc,
                config=config,
            )

    def test_nonwritable_worktree_base_raises_worktree_error(self, repo_with_pr_doc, tmp_path):
        """When worktree_base exists but is not writable, mkdir raises PermissionError
        (an uncaught OSError before the fix). After the fix it becomes WorktreeError."""
        import stat

        from syncade.worktree import WorktreeError

        ro_base = tmp_path / "readonly_base"
        ro_base.mkdir()
        ro_base.chmod(stat.S_IRUSR | stat.S_IXUSR)  # read+execute, no write

        repo, pr_doc = repo_with_pr_doc
        config = _two_reviewer_config()
        config = config.model_copy(update={"worktree_base": ro_base})

        try:
            with pytest.raises(WorktreeError, match="worktree_base"):
                run_review(
                    repo_root=repo,
                    pr_doc_path=pr_doc,
                    config=config,
                )
        finally:
            # Restore write permission so tmp_path cleanup succeeds.
            ro_base.chmod(stat.S_IRWXU)


# ---------------------------------------------------------------------------
# Repo-root discovery
# ---------------------------------------------------------------------------


class TestRepoRootDiscovery:
    """PR-5.5: the user-supplied repo_root is a starting hint. run_review
    resolves it to the actual git repo root via discover_repo_root, so
    invoking from a subdirectory still anchors .syncade/ at the root."""

    def test_invoked_from_subdir_writes_artifacts_to_repo_root(self, repo_with_pr_doc):
        """The Acme field bug: `syncade` invoked from
        `acme/docs/feature-work/` wrote artifacts under that
        subdir instead of `acme/.syncade/`. The run dir must land at
        the actual repo root regardless of the hint's depth."""
        repo, pr_doc = repo_with_pr_doc
        subdir = repo / "docs" / "feature-work"
        subdir.mkdir(parents=True)

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=subdir,  # the HINT is a nested subdirectory
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )

        # Artifacts land at the repo root, NOT under the subdir.
        assert result.artifacts.run_dir.parent == repo / ".syncade" / "runs"
        assert not (subdir / ".syncade").exists()
        assert result.exit_code == 0

    def test_invoked_from_repo_root_is_unchanged(self, repo_with_pr_doc):
        """Existing callers that pass the repo root directly keep working
        — discovery is a no-op when the hint already is the root."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )
        assert result.artifacts.run_dir.parent == repo / ".syncade" / "runs"
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Timeout resolution
# ---------------------------------------------------------------------------


class TestTimeoutResolution:
    """PR-5.5: the per-reviewer timeout resolves CLI flag > config >
    LoopConfig default, and the resolved value reaches dispatch_reviewers.
    No real timeouts here — FakeAdapter completes instantly; we only
    assert on the value handed to the dispatcher."""

    @staticmethod
    def _capture_dispatch_timeout(monkeypatch) -> dict:
        import syncade.orchestrator.round as round_module

        captured: dict = {}
        real = round_module.dispatch_reviewers

        def recording(*args, **kwargs):
            captured["timeout_seconds"] = kwargs.get("timeout_seconds")
            return real(*args, **kwargs)

        monkeypatch.setattr(round_module, "dispatch_reviewers", recording)
        return captured

    def _config_with_loop_timeout(self, timeout_seconds: float) -> SyncadeConfig:
        return SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            # PR-8: single-pass back-compat — these tests don't drive
            # multi-round behavior.
            loop={"timeout_seconds": timeout_seconds, "max_rounds": 1},
        )

    def test_default_falls_back_to_loop_config_default(self, repo_with_pr_doc, monkeypatch):
        """No timeout_seconds arg, no config override → the LoopConfig
        default of 1800s reaches the dispatcher."""
        captured = self._capture_dispatch_timeout(monkeypatch)
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )
        assert captured["timeout_seconds"] == 1800

    def test_config_value_reaches_dispatcher(self, repo_with_pr_doc, monkeypatch):
        """[loop] timeout_seconds in config reaches the dispatcher when
        no explicit arg overrides it."""
        captured = self._capture_dispatch_timeout(monkeypatch)
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config_with_loop_timeout(3600),
            adapter_factory=_factory_returning(*adapters),
        )
        assert captured["timeout_seconds"] == 3600

    def test_explicit_arg_overrides_config(self, repo_with_pr_doc, monkeypatch):
        """An explicit timeout_seconds arg (the CLI's --timeout flag)
        wins over config.loop.timeout_seconds."""
        captured = self._capture_dispatch_timeout(monkeypatch)
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._config_with_loop_timeout(3600),
            timeout_seconds=60,
            adapter_factory=_factory_returning(*adapters),
        )
        assert captured["timeout_seconds"] == 60

    def test_dispatch_log_notes_per_reviewer_timeouts_honestly(self, repo_with_pr_doc, capsys):
        """R2-M4: a per-reviewer ``timeout_seconds`` override makes the dispatch line
        ``(timeout 1800s each)`` a lie — the log says ``per-reviewer timeouts`` instead. (The
        no-override common case still says ``each``, covered by the other timeout tests + smoke.)"""
        repo, pr_doc = repo_with_pr_doc
        cfg = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x", "timeout_seconds": 600},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 1},
        )
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=cfg,
            adapter_factory=_factory_returning(*adapters),
        )
        out = capsys.readouterr().out
        assert "per-reviewer timeouts" in out
        assert "1800s each" not in out


# ---------------------------------------------------------------------------
# Lifecycle logging
# ---------------------------------------------------------------------------


class TestLifecycleLogging:
    """PR-5.5: run_review drives a Logger through each phase boundary.
    Default is normal verbosity; a quiet Logger suppresses phase lines
    and collapses non-parse-error summaries to one line."""

    def test_normal_logger_emits_phase_progress(self, repo_with_pr_doc, capsys):
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            logger=Logger("normal"),
            adapter_factory=_factory_returning(*adapters),
        )
        out = capsys.readouterr().out
        # The phase-level narrative is visible end to end.
        assert "snapshot" in out.lower()
        assert "dispatching" in out.lower()
        assert "persisting" in out.lower()
        assert "run complete" in out.lower()
        # Both reviewers show up in the per-reviewer lines.
        assert "rv1" in out
        assert "rv2" in out

    def test_quiet_logger_emits_only_the_summary_line(self, repo_with_pr_doc, capsys):
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            logger=Logger("quiet"),
            adapter_factory=_factory_returning(*adapters),
        )
        out = capsys.readouterr().out
        # Exactly one line — the final summary; phase lines suppressed.
        assert len(out.strip().splitlines()) == 1
        assert f"exit {result.exit_code}" in out

    def test_default_logger_is_normal_verbosity(self, repo_with_pr_doc, capsys):
        """run_review with no logger arg defaults to a normal Logger, so
        phase-level progress is on by default even for direct callers."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )
        out = capsys.readouterr().out
        assert "run complete" in out.lower()
