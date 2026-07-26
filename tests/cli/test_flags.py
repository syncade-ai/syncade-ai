"""CLI-surface tests: ``--max-rounds``, ``--force-dirty``, and the
``--resume`` / ``--force-drift`` surface (PR-8 + PR-16).

Split out of the monolithic ``tests/test_cli.py`` (PR-R3).
"""

import shutil

import pytest

from syncade.cli import main
from tests.cli._helpers import _init_git_repo

# ---------------------------------------------------------------------------
# PR-8: --max-rounds and --force-dirty
# ---------------------------------------------------------------------------


class TestMaxRoundsFlag:
    """``--max-rounds INT`` overrides ``[loop] max_rounds`` from config.
    Argparse rejects out-of-range values before any orchestrator
    work; resolution order is documented in the help text."""

    def test_max_rounds_in_help(self, capsys):
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        assert "--max-rounds" in out
        normalized = " ".join(out.split())
        assert "[1, 10]" in normalized or "1, 10" in normalized or "1-10" in normalized

    def test_max_rounds_zero_rejected_by_argparse(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["--max-rounds", "0", "x.md"])
        # Argparse exits 2 on argument-type errors.
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "--max-rounds" in err
        assert "[1, 10]" in err or "in [1, 10]" in err

    def test_max_rounds_eleven_rejected_by_argparse(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["--max-rounds", "11", "x.md"])
        assert exc_info.value.code == 2

    def test_max_rounds_non_integer_rejected(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["--max-rounds", "two", "x.md"])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "integer" in err or "'two'" in err

    def test_max_rounds_type_accepts_ten_rejects_eleven(self):
        """PR-v2-31: the --max-rounds argparse type mirrors the schema's
        raised [1, 10] bound (was [1, 3])."""
        import argparse

        from syncade.cli.parser import _max_rounds

        assert _max_rounds("10") == 10
        with pytest.raises(argparse.ArgumentTypeError) as exc:
            _max_rounds("11")
        assert "[1, 10]" in str(exc.value)

    def test_max_rounds_one_overrides_config(self, tmp_path, monkeypatch):
        """``--max-rounds 1`` reaches ``run_review``'s ``config.loop.max_rounds``
        as ``1``, even when ``[loop] max_rounds = 3`` is set in
        ``.syncade/config.toml``."""
        if shutil.which("git") is None:
            pytest.skip("git not on PATH")
        _init_git_repo(tmp_path)
        (tmp_path / ".syncade").mkdir()
        (tmp_path / ".syncade" / "config.toml").write_text(
            '[[reviewers]]\nname = "rv1"\nprovider = "not-a-real-provider"\nmodel = "x"\n'
            "[loop]\nmax_rounds = 3\n"
        )
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")

        # Capture the config that reached run_review.
        captured: dict = {}
        import syncade.cli as cli_module

        real = cli_module.run_review

        def recording(**kwargs):
            captured["config"] = kwargs["config"]
            return real(**kwargs)

        monkeypatch.setattr(cli_module, "run_review", recording)

        # Run — bogus provider → exit 50, but the captured config
        # is what we're after.
        rc = main(["--max-rounds", "1", "--repo-root", str(tmp_path), str(pr_doc)])
        assert rc == 50
        # The flag overrode the config-file value.
        assert captured["config"].loop.max_rounds == 1

    def test_max_rounds_two_overrides_config(self, tmp_path, monkeypatch):
        if shutil.which("git") is None:
            pytest.skip("git not on PATH")
        _init_git_repo(tmp_path)
        (tmp_path / ".syncade").mkdir()
        (tmp_path / ".syncade" / "config.toml").write_text(
            '[[reviewers]]\nname = "rv1"\nprovider = "not-a-real-provider"\nmodel = "x"\n'
        )
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")

        captured: dict = {}
        import syncade.cli as cli_module

        real = cli_module.run_review

        def recording(**kwargs):
            captured["config"] = kwargs["config"]
            return real(**kwargs)

        monkeypatch.setattr(cli_module, "run_review", recording)

        rc = main(["--max-rounds", "2", "--repo-root", str(tmp_path), str(pr_doc)])
        assert rc == 50
        assert captured["config"].loop.max_rounds == 2

    def test_no_max_rounds_flag_uses_config(self, tmp_path, monkeypatch):
        """Without ``--max-rounds``, the config-file value (or
        default 3) reaches the orchestrator."""
        if shutil.which("git") is None:
            pytest.skip("git not on PATH")
        _init_git_repo(tmp_path)
        (tmp_path / ".syncade").mkdir()
        (tmp_path / ".syncade" / "config.toml").write_text(
            '[[reviewers]]\nname = "rv1"\nprovider = "not-a-real-provider"\nmodel = "x"\n'
            "[loop]\nmax_rounds = 2\n"
        )
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")

        captured: dict = {}
        import syncade.cli as cli_module

        real = cli_module.run_review

        def recording(**kwargs):
            captured["config"] = kwargs["config"]
            return real(**kwargs)

        monkeypatch.setattr(cli_module, "run_review", recording)

        rc = main(["--repo-root", str(tmp_path), str(pr_doc)])
        assert rc == 50
        # Config file's value wins; CLI default of None doesn't override.
        assert captured["config"].loop.max_rounds == 2


class TestBudgetFlags:
    """``--budget-tokens`` / ``--budget-usd`` (PR-v2-11): CLI-boundary validation + override
    of the ``[loop]`` config twins. The budget-abort BEHAVIOUR is a later issue; this issue
    is the knobs only, so a byte-identical unset path is the load-bearing property."""

    def test_budget_flags_parse(self):
        from syncade.cli import build_parser

        ns = build_parser().parse_args(["x.md", "--budget-tokens", "5000", "--budget-usd", "2.50"])
        assert ns.budget_tokens == 5000
        assert ns.budget_usd == 2.50

    @pytest.mark.parametrize(
        "argv,needle",
        [
            (["--budget-tokens", "0"], "budget-tokens"),
            (["--budget-tokens", "-5"], "budget-tokens"),
            (["--budget-tokens", "1.5"], "integer"),
            (["--budget-usd", "0"], "dollar amount"),
            (["--budget-usd", "nan"], "dollar amount"),
            (["--budget-usd", "inf"], "dollar amount"),
        ],
    )
    def test_invalid_budget_rejected_exit_2(self, argv, needle, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["x.md", *argv])
        assert exc.value.code == 2
        assert needle in capsys.readouterr().err

    def _capture_config(self, monkeypatch, tmp_path, config_toml, argv):
        """Run `main` with a bogus-provider config (reaches run_review) and return the config
        that got there — the same technique the --max-rounds override tests use."""
        if shutil.which("git") is None:
            pytest.skip("git not on PATH")
        _init_git_repo(tmp_path)
        (tmp_path / ".syncade").mkdir()
        (tmp_path / ".syncade" / "config.toml").write_text(config_toml)
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")
        captured: dict = {}
        import syncade.cli as cli_module

        real = cli_module.run_review

        def recording(**kwargs):
            captured["config"] = kwargs["config"]
            return real(**kwargs)

        monkeypatch.setattr(cli_module, "run_review", recording)
        main([*argv, "--repo-root", str(tmp_path), str(pr_doc)])
        return captured["config"]

    def test_budget_flags_override_config(self, tmp_path, monkeypatch):
        cfg = self._capture_config(
            monkeypatch,
            tmp_path,
            '[[reviewers]]\nname = "rv1"\nprovider = "not-a-real-provider"\nmodel = "x"\n'
            "[loop]\nbudget_usd = 10.0\n",
            ["--budget-tokens", "1000", "--budget-usd", "2.5"],
        )
        assert cfg.loop.budget_tokens == 1000  # flag set it (config had none)
        assert cfg.loop.budget_usd == 2.5  # flag overrode the config's 10.0

    def test_no_budget_flags_leaves_config_untouched(self, tmp_path, monkeypatch):
        cfg = self._capture_config(
            monkeypatch,
            tmp_path,
            '[[reviewers]]\nname = "rv1"\nprovider = "not-a-real-provider"\nmodel = "x"\n'
            "[loop]\nbudget_usd = 4.0\n",
            [],
        )
        assert cfg.loop.budget_usd == 4.0  # config value untouched
        assert cfg.loop.budget_tokens is None  # no config value, no flag -> None

    @pytest.mark.parametrize("budget_argv", [["--budget-tokens", "100"], ["--budget-usd", "1.0"]])
    def test_bare_openspec_with_budget_is_not_rejected(self, budget_argv, capsys):
        """Bare --openspec is a loop-mode invocation; budget flags must not be
        rejected with the one-shot guard even though argparse stores '' for openspec."""
        try:
            main(["--openspec", *budget_argv])
        except SystemExit:
            pass
        err = capsys.readouterr().err
        assert "meaningful only with a review loop" not in err, (
            "budget guard wrongly rejected bare --openspec as a non-loop invocation"
        )


class TestForceDirtyFlag:
    """``--force-dirty`` bypasses the loop-mode dirty-tree refusal
    (PR-8). Without it, ``max_rounds > 1`` + tracked-modified WIP
    raises WorktreeError → exit 60. With it, the orchestrator
    proceeds (the warning still fires)."""

    def test_force_dirty_in_help(self, capsys):
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        assert "--force-dirty" in out

    def test_no_force_dirty_loop_mode_refuses_tracked_dirty(self, tmp_path, capsys):
        if shutil.which("git") is None:
            pytest.skip("git not on PATH")
        _init_git_repo(tmp_path)
        # Dirty the tree
        (tmp_path / "README.md").write_text("modified\n")
        (tmp_path / ".syncade").mkdir()
        # Default max_rounds = 3 (loop mode); bogus provider would
        # be a CONFIG_ERROR if dispatch ran, but the dirty-tree
        # refusal fires first.
        (tmp_path / ".syncade" / "config.toml").write_text(
            '[[reviewers]]\nname = "rv1"\nprovider = "not-a-real-provider"\nmodel = "x"\n'
        )
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")

        rc = main(["--repo-root", str(tmp_path), str(pr_doc)])
        assert rc == 60  # WORKTREE_ERROR from loop-mode dirty refusal
        err = capsys.readouterr().err
        assert "loop mode" in err
        assert "force-dirty" in err or "--force-dirty" in err

    def test_force_dirty_bypasses_refusal(self, tmp_path, capsys):
        """``--force-dirty`` + tracked-dirty + loop mode → proceeds.
        The bogus-provider config triggers exit 50 at dispatch
        instead — that's the success signal here (we got past the
        dirty-tree refusal)."""
        if shutil.which("git") is None:
            pytest.skip("git not on PATH")
        _init_git_repo(tmp_path)
        (tmp_path / "README.md").write_text("modified\n")
        (tmp_path / ".syncade").mkdir()
        (tmp_path / ".syncade" / "config.toml").write_text(
            '[[reviewers]]\nname = "rv1"\nprovider = "not-a-real-provider"\nmodel = "x"\n'
        )
        pr_doc = tmp_path / "pr.md"
        pr_doc.write_text("# PR\n")

        rc = main(["--force-dirty", "--repo-root", str(tmp_path), str(pr_doc)])
        # Got past the dirty-tree refusal; bogus provider → exit 50.
        assert rc == 50
        err = capsys.readouterr().err
        # The dirty-tree warning still fires (force_dirty acknowledges
        # the race condition but doesn't hide it).
        assert "uncommitted" in err.lower() or "modified" in err.lower()


# ---------------------------------------------------------------------------
# PR-16: --resume / --force-drift CLI surface
# ---------------------------------------------------------------------------


class TestResumeCliSurface:
    """PR-16 T5: --resume + --force-drift flags, mutual exclusion
    rules, and the _run_resume handler's exit-code mapping."""

    def _init_repo(self, repo_root):
        """Initialize a real git repo so the CLI can resolve a
        repo_root. Returns the initial commit SHA."""
        import subprocess

        repo_root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo_root, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo_root, check=True)
        (repo_root / "x").write_text("x")
        subprocess.run(["git", "add", "x"], cwd=repo_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_root, check=True)
        # Feature branch (main stays the baseline), so the default-branch guard doesn't
        # preempt the resume/auth behavior under test.
        subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=repo_root, check=True)
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def test_force_drift_without_resume_is_refused(self, tmp_path, capsys):
        """--force-drift requires --resume; standalone use is a CLI
        usage error (exit 2, argparse-style invalid command shape)."""
        rc = main(["--repo-root", str(tmp_path), "--force-drift", "some.md"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "force-drift" in err.lower()
        assert "requires --resume" in err.lower()

    def test_resume_with_base_is_refused(self, tmp_path, capsys):
        """--resume + --base is refused; the original run's base_ref
        is in run-init.json so passing --base creates ambiguity."""
        rc = main(
            [
                "--repo-root",
                str(tmp_path),
                "--resume",
                "some-run",
                "--base",
                "HEAD~1",
            ]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "--base" in err
        assert "run-init.json" in err

    def test_resume_completed_normally_run_is_refused(self, tmp_path, capsys):
        """A run that completed cleanly (exit 0/20/30) is NOT eligible
        to resume. The handler surfaces a specific message."""
        import json

        repo = tmp_path / "repo"
        self._init_repo(repo)
        run_dir = repo / ".syncade" / "runs" / "2026-05-28T10-00-00"
        run_dir.mkdir(parents=True)
        (run_dir / "run-init.json").write_text(
            json.dumps(
                {
                    "syncade_version": "0.1.0",
                    "started_at_utc": "2026-05-28T10:00:00Z",
                    "pr_doc_path": "stub.md",
                    "base_ref": None,
                    "starting_sha": "a" * 40,
                    "operator_branch": "main",
                    "max_rounds": 1,
                    "config_snapshot": {"loop": {"max_rounds": 1}},
                }
            )
            + "\n"
        )
        (run_dir / "loop-manifest.json").write_text(
            json.dumps(
                {
                    "syncade_version": "0.1.0",
                    "run_id": "2026-05-28T10-00-00",
                    "started_at_utc": "2026-05-28T10:00:00Z",
                    "max_rounds": 1,
                    "final_exit_code": 0,
                    "final_round": 0,
                    "termination_reason": "ship",
                    "rounds": [],
                }
            )
            + "\n"
        )
        rc = main(["--repo-root", str(repo), "--resume", "2026-05-28T10-00-00"])
        assert rc == 60
        err = capsys.readouterr().err
        assert "completed normally" in err.lower()

    def test_resume_latest_with_no_eligible_runs_is_refused(self, tmp_path, capsys):
        """--resume (alone or 'latest') with no eligible runs → exit
        60 with a helpful message."""
        repo = tmp_path / "repo"
        self._init_repo(repo)
        # No runs at all.
        rc = main(["--repo-root", str(repo), "--resume"])
        assert rc == 60
        err = capsys.readouterr().err
        assert "no eligible runs" in err.lower()

    def test_resume_alone_resolves_to_latest(self, tmp_path):
        """Pass --resume with no argument → resolves to 'latest'.
        Argparse with nargs='?' + const='latest' encodes this."""
        from syncade.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["--resume"])
        assert args.resume == "latest"

    def test_resume_explicit_latest(self, tmp_path):
        """'--resume latest' is the explicit form; same resolution."""
        from syncade.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["--resume", "latest"])
        assert args.resume == "latest"

    def test_resume_specific_run_id(self, tmp_path):
        """'--resume <id>' captures the specific run-id verbatim."""
        from syncade.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["--resume", "2026-05-28T10-00-00"])
        assert args.resume == "2026-05-28T10-00-00"

    def test_help_shows_resume_and_force_drift(self, capsys):
        """--help mentions the new --resume / --force-drift flags."""
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        assert "--resume" in out
        assert "--force-drift" in out
