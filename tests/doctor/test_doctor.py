"""Tests for ``syncade --doctor`` — readiness checks + orchestration (PR-v2-12).

Config summary, provider-CLI PATH, worktree-root/disk, the branch preview, the live legs
(auth probe + producer headless-commit), the ``$0`` spend gating (F1), ``--quick``, the exit
contract, and the CLI surface. The run-plan + cost PREVIEW checks live in
``test_doctor_preview.py``; shared helpers in ``_helpers.py`` and the ``healthy_env`` fixture
in ``conftest.py``.

Adversarial focus mirrors the brief's claims:
- **C1 (same plan):** the branch doctor names / refuses is what the real review path would
  touch / refuse for the same flags.
- **C2 (inertness):** ``--doctor`` writes no artifact, never moves HEAD, and never creates
  the worktree base dir.
- **C3 (no false-green):** a red check MUST flip the exit code off 0.
- **C5 (doomed run caught for $0):** a cheap red skips the live legs — no provider call.
- **C6 (opt-out live legs):** ``--quick`` reports auth + producer-commit as *skipped*,
  never passed, and makes no provider call.
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from syncade import doctor, doctor_env
from syncade.adapters.registry import known_providers
from syncade.cli import main
from syncade.config import ReviewerConfig, SyncadeConfig
from syncade.exit_codes import CONFIG_ERROR, FINDINGS_PRESENT, SUCCESS, WORKTREE_ERROR
from tests.doctor._helpers import (
    _fail_auth,
    _git,
    _head,
    _ok_auth,
    _repo_on,
    _repo_unborn,
    _repo_with_second_commit,
    _which_missing,
    _which_none,
)


class TestCollectChecks:
    def test_healthy_config_is_all_green(self, healthy_env):
        checks = doctor.collect_checks(SyncadeConfig(), healthy_env)
        assert checks[0].name == "config"
        assert [c.status for c in checks] == ["ok"] * len(checks)
        assert any(c.name.startswith("cli:") for c in checks)
        assert any(c.name == "worktree" for c in checks)
        assert any(c.name == "branch" for c in checks)
        assert any(c.name.startswith("auth") for c in checks)
        assert any(c.name == "producer-commit" for c in checks)

    def test_missing_provider_cli_is_red_with_a_fix(self, healthy_env, monkeypatch):
        monkeypatch.setattr(doctor_env, "which", _which_missing("codex"))
        reds = [c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.status == "red"]
        assert reds, "a missing codex binary must produce a red check"
        assert any("codex" in c.detail for c in reds)
        assert all(c.fix for c in reds), "a red check must carry a fix-it line"

    def test_non_launchable_provider_cli_is_red(self, healthy_env, monkeypatch):
        # shutil.which finds the binary but the OS cannot exec it (broken shebang / missing
        # interpreter). Doctor must red this rather than green it, since the real run will fail.
        def _raise_on_codex(binary: str) -> None:
            if binary == "codex":
                raise OSError("No such file or directory: 'codex'")

        monkeypatch.setattr(doctor_env, "_probe_cli_launch", _raise_on_codex)
        reds = [c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.status == "red"]
        assert reds, "an unexecutable codex binary must produce a red check"
        assert any("codex" in c.detail for c in reds)
        assert all(c.fix for c in reds)

    def test_config_row_matches_the_resolved_actors(self, healthy_env):
        # The report's data must match persistent state: the config row names the actual
        # actors a run would dispatch (the 'UI matches the DB' analog for a CLI report).
        cfg = SyncadeConfig()
        summary = doctor.collect_checks(cfg, healthy_env)[0].detail
        assert cfg.producer.model in summary
        assert cfg.synthesizer.model in summary
        for reviewer in cfg.reviewers:
            assert reviewer.model in summary

    def test_one_cli_check_per_unique_provider(self, healthy_env):
        cfg = SyncadeConfig()
        providers = {a.provider for a in (*cfg.reviewers, cfg.producer, cfg.synthesizer)}
        cli = [c for c in doctor.collect_checks(cfg, healthy_env) if c.name.startswith("cli:")]
        assert len(cli) == len(providers)

    def test_nonzero_exit_cli_is_red(self, healthy_env, monkeypatch):
        # A binary that's on PATH but exits non-zero must be red — not green.
        # Regression: _probe_cli_launch previously ignored the return code, so a fake codex
        # that exits 42 was reported as cli:openai green while real reviews failed with exit 40.
        def _exit_nonzero(binary: str) -> None:
            if binary == "codex":
                raise subprocess.CalledProcessError(42, binary)

        monkeypatch.setattr(doctor_env, "_probe_cli_launch", _exit_nonzero)
        reds = [c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.status == "red"]
        assert reds, "a non-zero exit from --version must produce a red check"
        assert any("codex" in c.detail for c in reds)
        assert any("42" in c.detail for c in reds), "exit code must appear in the detail"
        assert all(c.fix for c in reds)

    def test_unknown_provider_is_red_not_skipped(self, healthy_env):
        # Round-2 finding: reviewer providers are not validated at config load, so an
        # unregistered provider reaches doctor. The real run raises UnknownProviderError at
        # dispatch, so doctor must RED it (was: skipped -> false-green).
        cfg = SyncadeConfig(
            reviewers=[ReviewerConfig(name="x", provider="bogus-provider", model="m")]
        )
        row = next(
            c for c in doctor.collect_checks(cfg, healthy_env) if c.name == "cli:bogus-provider"
        )
        assert row.status == "red"
        assert row.fix and "known:" in row.fix


class TestWorktreeCheck:
    def test_unwritable_base_is_red_with_a_fix(self, healthy_env, monkeypatch):
        # os.access is doctor's only os.access call in the check path, so denying it here reds
        # the worktree check.
        monkeypatch.setattr(doctor_env.os, "access", lambda *a, **k: False)
        wt = next(
            c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.name == "worktree"
        )
        assert wt.status == "red"
        assert wt.fix

    def test_low_disk_is_red(self, healthy_env, monkeypatch):
        monkeypatch.setattr(doctor_env, "disk_usage", lambda p: SimpleNamespace(free=100 * 1024**2))
        wt = next(
            c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.name == "worktree"
        )
        assert wt.status == "red"
        assert "free" in wt.detail

    def test_probe_uses_configured_worktree_base(self, healthy_env, tmp_path):
        """PR-v2-9: the writability preview probes ``config.worktree_base`` (which
        ``--worktree-base`` overrides), NOT the hardcoded default — so a run that relocated its
        worktree base is previewed against the base it will actually use."""
        base = tmp_path / "custom_wt"
        base.write_text("a file, not a dir\n")  # the real run's mkdir would fail here
        cfg = SyncadeConfig(worktree_base=base)
        wt = next(c for c in doctor.collect_checks(cfg, healthy_env) if c.name == "worktree")
        assert wt.status == "red"
        assert "not a directory" in wt.detail
        assert str(base) in wt.detail  # names the CONFIGURED base, not /tmp/syncade

    def test_worktree_base_as_file_is_red(self, monkeypatch, tmp_path):
        # A regular file at DEFAULT_WORKTREE_BASE passes os.access but mkdir fails with
        # NotADirectoryError. Doctor must red it before any live probe runs.
        base = tmp_path / "syncade"
        base.write_text("not a dir\n")
        wt = doctor_env._check_worktree_root(base)
        assert wt.status == "red"
        assert "not a directory" in wt.detail
        assert wt.fix

    def test_worktree_base_broken_symlink_is_red(self, monkeypatch, tmp_path):
        # Round-2 finding: a BROKEN symlink at the base. Path.exists() follows the link and
        # returns False, so the old walk-up skipped PAST it to the parent and false-greened —
        # while the real run's mkdir fails on that symlink. lexists must stop the walk here.
        base = tmp_path / "syncade"
        base.symlink_to(tmp_path / "nonexistent-target")  # broken symlink
        wt = doctor_env._check_worktree_root(base)
        assert wt.status == "red"
        assert "broken symlink" in wt.detail
        assert wt.fix

    def test_probe_is_strictly_inert_no_file_no_mtime_bump(self, monkeypatch, tmp_path):
        # F4' (dogfood round 2): the writability probe must write NOTHING — no file, and not
        # even the directory-mtime bump a create-then-delete tempfile caused. (The old tempfile
        # probe would fail the mtime assertion.) With a non-existent base, the probe walks up to
        # an existing ancestor and reads it with os.access only.
        base = tmp_path / "deep" / "syncade"
        monkeypatch.setattr(doctor_env, "disk_usage", lambda p: SimpleNamespace(free=10 * 1024**3))
        mtime_before = tmp_path.stat().st_mtime_ns
        wt = doctor_env._check_worktree_root(base)
        assert wt.status == "ok"  # probed the existing tmp_path ancestor
        assert not (tmp_path / "deep").exists()  # base (and intermediates) not created
        assert tmp_path.stat().st_mtime_ns == mtime_before  # not even an mtime bump


class TestBranchCheck:
    """C5 — the branch preview catches a doomed run for $0. C1 — its verdict matches the
    real review path's guard for the same flags."""

    def test_feature_branch_loop_is_green(self, healthy_env):
        # healthy_env is on "work" with origin/HEAD -> main.
        branch = next(
            c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.name == "branch"
        )
        assert branch.status == "ok"
        assert "work" in branch.detail

    def test_on_default_branch_loop_is_red(self, healthy_env, tmp_path):
        repo = _repo_on(tmp_path / "m", "main")
        branch = next(c for c in doctor.collect_checks(SyncadeConfig(), repo) if c.name == "branch")
        assert branch.status == "red"
        assert branch.fix and "--allow-default-branch" in branch.fix

    def test_allow_default_branch_flag_makes_it_green(self, healthy_env, tmp_path):
        repo = _repo_on(tmp_path / "m", "main")
        branch = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), repo, allow_default_branch=True)
            if c.name == "branch"
        )
        assert branch.status == "ok"

    def test_single_pass_on_default_branch_is_green(self, healthy_env, tmp_path):
        repo = _repo_on(tmp_path / "m", "main")
        branch = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), repo, max_rounds=1)
            if c.name == "branch"
        )
        assert branch.status == "ok"
        assert "commits nothing" in branch.detail

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"base_ref": "HEAD", "max_rounds": 3},
            {"scope": "everything", "max_rounds": 3},
        ],
    )
    def test_known_empty_based_diff_on_default_branch_is_green(self, tmp_path, kwargs):
        """Known-empty based/scoped diff on default branch is green: guard is N/A."""
        repo = _repo_on(tmp_path / "m", "main")
        branch = next(
            c for c in doctor.collect_checks(SyncadeConfig(), repo, **kwargs) if c.name == "branch"
        )
        assert branch.status == "ok", "doctor refused a known-empty based/scoped run on main"

    def test_reviewable_based_diff_on_default_branch_is_red(self, tmp_path):
        repo, base = _repo_with_second_commit(tmp_path / "m")
        chk = doctor.collect_checks(SyncadeConfig(), repo, base_ref=base, max_rounds=3)
        assert next(c for c in chk if c.name == "branch").status == "red"

    def test_dirty_tree_loop_is_red(self, healthy_env):
        (healthy_env / "README.md").write_text("modified\n")  # tracked-modified
        branch = next(
            c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.name == "branch"
        )
        assert branch.status == "red"
        assert branch.fix and "--force-dirty" in branch.fix

    def test_force_dirty_flag_makes_it_green(self, healthy_env):
        (healthy_env / "README.md").write_text("modified\n")
        branch = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env, force_dirty=True)
            if c.name == "branch"
        )
        assert branch.status == "ok"

    def test_detached_head_loop_is_red(self, healthy_env):
        _git(healthy_env, "checkout", "-q", _head(healthy_env))  # detach
        branch = next(
            c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.name == "branch"
        )
        assert branch.status == "red"
        assert "detached" in branch.detail

    def test_branch_red_matches_the_real_run_refusal(self, healthy_env, tmp_path):
        # C1: on the SAME default-branch repo, doctor reds AND the real review path refuses
        # at the guard (before any spend) — both exit 60. (env probes patched green by
        # healthy_env, so --quick doctor's only possible red is the branch.)
        repo = _repo_on(tmp_path / "m", "main")
        # A REAL brief. PR-h-04 item A validates the brief path before anything mutates, so a
        # nonexistent one exits 2 and this contract — doctor's red matching the RUN's refusal —
        # would be asserted against the wrong refusal.
        (repo / "brief.md").write_text("# PR\n")
        assert main(["--doctor", "--quick", "--repo-root", str(repo)]) == WORKTREE_ERROR
        assert main([str(repo / "brief.md"), "--repo-root", str(repo)]) == WORKTREE_ERROR

    def test_unborn_head_single_pass_is_red_not_false_green(self, healthy_env, tmp_path):
        # F2': an unborn HEAD (git init, no commits) false-greened single-pass before; the
        # real run's snapshot fails there, so doctor must red it.
        repo = _repo_unborn(tmp_path / "unborn")
        branch = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), repo, max_rounds=1)
            if c.name == "branch"
        )
        assert branch.status == "red"
        assert "unborn" in branch.detail and branch.fix

    def test_unborn_head_committing_allow_default_is_red_not_a_traceback(
        self, healthy_env, tmp_path
    ):
        # F2': with --allow-default-branch the guard is skipped, so take_snapshot used to
        # raise an uncaught SnapshotError; it must be a red row instead.
        repo = _repo_unborn(tmp_path / "unborn")
        branch = next(
            c
            for c in doctor.collect_checks(
                SyncadeConfig(), repo, max_rounds=3, allow_default_branch=True
            )
            if c.name == "branch"
        )
        assert branch.status == "red"

    def test_unborn_head_via_cli_exits_60_no_traceback(self, healthy_env, tmp_path):
        # F2': the CLI must return a clean exit 60, never an uncaught-exception traceback.
        repo = _repo_unborn(tmp_path / "unborn")
        assert main(["--doctor", "--quick", "--repo-root", str(repo)]) == WORKTREE_ERROR


class TestExitContract:
    """C3 — the false-green PR-12 exists to kill. A red check flips the code; nothing else."""

    def test_all_green_exits_success(self, healthy_env, capsys):
        assert doctor.run_doctor(SyncadeConfig(), healthy_env) == SUCCESS

    def test_any_red_exits_60(self, healthy_env, monkeypatch, capsys):
        monkeypatch.setattr(doctor_env, "which", _which_none)
        assert doctor.run_doctor(SyncadeConfig(), healthy_env) == WORKTREE_ERROR

    def test_a_red_never_silently_passes(self, healthy_env, monkeypatch, capsys):
        monkeypatch.setattr(doctor_env, "which", _which_missing("codex"))
        assert doctor.run_doctor(SyncadeConfig(), healthy_env) != SUCCESS


def test_provider_cli_map_covers_every_known_provider():
    # Drift guard: if the adapter registry grows a provider doctor's _PROVIDER_CLI does not
    # map, doctor would silently skip its PATH check. Fail loudly here instead.
    assert set(known_providers()) <= set(doctor_env._PROVIDER_CLI)


class TestLiveLegs:
    def test_all_live_green(self, healthy_env):
        checks = doctor.collect_checks(SyncadeConfig(), healthy_env)
        assert all(c.status == "ok" for c in checks)

    def test_auth_probe_failure_is_red_with_a_fix(self, healthy_env, monkeypatch):
        monkeypatch.setattr(doctor, "probe_credentials", lambda *a, **k: [_fail_auth("openai")])
        auth = [
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env)
            if c.name.startswith("auth")
        ]
        assert auth and auth[0].status == "red"
        assert auth[0].fix

    def test_preflight_problem_is_red_and_the_probe_is_skipped(self, healthy_env, monkeypatch):
        # A contradicted declaration is the blocker; probing under a lie can green a
        # mis-declared codex — exactly the footgun preflight catches. So don't probe.
        monkeypatch.setattr(
            doctor, "preflight", lambda *a, **k: ["codex declared api on a ChatGPT login"]
        )
        probed = {"ran": False}

        def _probe(*a, **k):
            probed["ran"] = True
            return []

        monkeypatch.setattr(doctor, "probe_credentials", _probe)
        auth = [c for c in doctor.collect_checks(SyncadeConfig(), healthy_env) if c.name == "auth"]
        assert auth and auth[0].status == "red"
        assert not probed["ran"], "must not probe under a contradicted declaration"

    def test_selfcheck_failure_is_red_with_a_fix(self, healthy_env, monkeypatch):
        monkeypatch.setattr(doctor, "run_selfcheck", lambda *a, **k: FINDINGS_PRESENT)
        pc = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env)
            if c.name == "producer-commit"
        )
        assert pc.status == "red"
        assert pc.fix and "selfcheck" in pc.fix

    def test_auth_success_includes_billing_mode_disclosure(self, healthy_env):
        # Finding 2: successful auth rows must include a billing-mode disclosure row.
        auth_rows = [
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env)
            if c.name.startswith("auth")
        ]
        billing = next((c for c in auth_rows if c.name == "auth:billing"), None)
        assert billing is not None, "successful auth must include an auth:billing disclosure row"
        assert billing.status == "ok"

    def test_auth_billing_row_absent_on_probe_failure(self, healthy_env, monkeypatch):
        # Finding 2 complement: when auth fails, no billing row (nothing useful to disclose).
        monkeypatch.setattr(doctor, "probe_credentials", lambda *a, **k: [_fail_auth("openai")])
        auth_rows = [
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env)
            if c.name.startswith("auth")
        ]
        assert not any(c.name == "auth:billing" for c in auth_rows)

    def test_selfcheck_exception_becomes_red_producer_row(self, healthy_env, monkeypatch):
        # Finding 3: if run_selfcheck raises instead of returning an exit code, doctor must
        # render a red producer-commit row, not propagate the exception.
        def _raises(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(doctor, "run_selfcheck", _raises)
        pc = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env)
            if c.name == "producer-commit"
        )
        assert pc.status == "red"
        assert "disk full" in pc.detail
        assert pc.fix and "selfcheck" in pc.fix

    def test_producer_smoke_forces_workspace_cleanup(self, healthy_env, monkeypatch):
        # F3': doctor drops the selfcheck output, so it must run it with always_cleanup=True —
        # otherwise a failed smoke would leave an invisible preserved workspace behind.
        seen: dict = {}

        def _spy(config, **kwargs):
            seen.update(kwargs)
            return SUCCESS

        monkeypatch.setattr(doctor, "run_selfcheck", _spy)
        doctor.collect_checks(SyncadeConfig(), healthy_env)
        assert seen.get("always_cleanup") is True

    def test_selfcheck_verbose_output_is_suppressed(self, healthy_env, monkeypatch, capsys):
        # doctor renders ONE row; selfcheck's chatty output must not leak into the table.
        def _noisy(*a, **k):
            print("SELFCHECK NOISE STDOUT")
            print("SELFCHECK NOISE STDERR", file=sys.stderr)
            return SUCCESS

        monkeypatch.setattr(doctor, "run_selfcheck", _noisy)
        doctor.collect_checks(SyncadeConfig(), healthy_env)
        captured = capsys.readouterr()
        assert "SELFCHECK NOISE" not in captured.out
        assert "SELFCHECK NOISE" not in captured.err


class TestSpendGating:
    """F1 (dogfood round 1) — the ``$0`` doomed-run guarantee: the live legs never spend
    when a cheap check already reds (a real run would be refused), and the producer commit
    smoke never spends under a credential that failed auth."""

    def test_cheap_blocker_skips_the_live_legs(self, healthy_env, monkeypatch):
        monkeypatch.setattr(doctor_env, "which", _which_missing("codex"))  # a cheap red
        live = {
            c.name: c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env)
            if c.name in ("auth", "producer-commit")
        }
        assert live["auth"].status == "skip"
        assert live["producer-commit"].status == "skip"

    def test_cheap_blocker_makes_no_live_call(self, healthy_env, monkeypatch):
        # The load-bearing $0 claim: not just reported skipped — actually NOT invoked.
        monkeypatch.setattr(doctor_env, "which", _which_missing("codex"))

        def _boom(*a, **k):
            raise AssertionError("a live leg ran despite a cheap blocker")

        monkeypatch.setattr(doctor, "preflight", _boom)
        monkeypatch.setattr(doctor, "probe_credentials", _boom)
        monkeypatch.setattr(doctor, "run_selfcheck", _boom)
        doctor.collect_checks(SyncadeConfig(), healthy_env)  # must not raise

    def test_dirty_tree_makes_no_live_call(self, healthy_env, monkeypatch):
        # The exact C5 case the panel flagged: a dirty tree (branch red) is a doomed run.
        (healthy_env / "README.md").write_text("modified\n")

        def _boom(*a, **k):
            raise AssertionError("a live leg ran on a doomed (dirty-tree) run")

        monkeypatch.setattr(doctor, "probe_credentials", _boom)
        monkeypatch.setattr(doctor, "run_selfcheck", _boom)
        checks = doctor.collect_checks(SyncadeConfig(), healthy_env)
        assert next(c for c in checks if c.name == "auth").status == "skip"
        assert next(c for c in checks if c.name == "producer-commit").status == "skip"

    def test_auth_failure_skips_the_producer_commit(self, healthy_env, monkeypatch):
        monkeypatch.setattr(doctor, "probe_credentials", lambda *a, **k: [_fail_auth("openai")])

        def _boom(*a, **k):
            raise AssertionError("the producer commit smoke ran despite failed auth")

        monkeypatch.setattr(doctor, "run_selfcheck", _boom)
        pc = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env)
            if c.name == "producer-commit"
        )
        assert pc.status == "skip"
        assert "auth" in pc.detail

    def test_auth_preflight_red_skips_the_producer_commit(self, healthy_env, monkeypatch):
        monkeypatch.setattr(doctor, "preflight", lambda *a, **k: ["codex declared api on ChatGPT"])

        def _boom(*a, **k):
            raise AssertionError("the producer commit smoke ran despite a contradicted declaration")

        monkeypatch.setattr(doctor, "run_selfcheck", _boom)
        pc = next(
            c
            for c in doctor.collect_checks(SyncadeConfig(), healthy_env)
            if c.name == "producer-commit"
        )
        assert pc.status == "skip"

    def test_announce_fires_only_when_the_live_legs_run(self, healthy_env, monkeypatch, capsys):
        # Warning present on a live run...
        doctor.collect_checks(SyncadeConfig(), healthy_env)
        assert "running live checks" in capsys.readouterr().err
        # ...and absent when a cheap blocker means nothing will spend.
        monkeypatch.setattr(doctor_env, "which", _which_none)
        doctor.collect_checks(SyncadeConfig(), healthy_env)
        assert "running live checks" not in capsys.readouterr().err

    def test_live_warning_survives_quiet(self, healthy_env, capsys):
        # F4: --quiet silences the report table, but NOT the pre-spend warning when the live
        # legs run (matches the PR-v2-24 auth-line convention: never hide a spend disclosure).
        doctor.run_doctor(SyncadeConfig(), healthy_env, quiet=True)
        captured = capsys.readouterr()
        assert "running live checks" in captured.err  # warning still fires
        # Table header ("[syncade] doctor: N check(s)") is suppressed; the OK summary
        # ("[syncade] doctor OK: ...") is printed instead (see test_quiet_quick_emits_skip_summary).
        assert "[syncade] doctor: " not in captured.out  # per-row table suppressed

    def test_quiet_quick_run_makes_no_call_and_no_warning(self, healthy_env, monkeypatch, capsys):
        # --quiet --quick: no live legs -> no spend AND (correctly) no pre-spend warning.
        def _boom(*a, **k):
            raise AssertionError("a live leg ran under --quiet --quick")

        monkeypatch.setattr(doctor, "probe_credentials", _boom)
        monkeypatch.setattr(doctor, "run_selfcheck", _boom)
        doctor.run_doctor(SyncadeConfig(), healthy_env, quick=True, quiet=True)  # must not raise
        assert "running live checks" not in capsys.readouterr().err

    def test_quiet_quick_emits_skip_summary(self, healthy_env, capsys):
        # C3/C6 regression: --quick --quiet was completely silent on a green run, hiding that
        # auth and producer-commit were skipped. The OK summary must appear even in quiet mode
        # so skipped legs are visible and distinguishable from passed ones.
        checks = doctor.collect_checks(SyncadeConfig(), healthy_env, quick=True)
        n_passed = sum(1 for c in checks if c.status == "ok")
        n_skipped = sum(1 for c in checks if c.status == "skip")
        assert n_skipped == 2  # auth + producer-commit
        doctor.run_doctor(SyncadeConfig(), healthy_env, quick=True, quiet=True)
        out = capsys.readouterr().out
        assert f"doctor OK: {n_passed} check(s) passed, {n_skipped} skipped" in out


class TestQuick:
    def test_quick_marks_live_legs_skipped_not_passed(self, healthy_env):
        checks = doctor.collect_checks(SyncadeConfig(), healthy_env, quick=True)
        live = [c for c in checks if c.name in ("auth", "producer-commit")]
        assert len(live) == 2
        assert all(c.status == "skip" for c in live)  # C6/C3: skipped is NOT passed

    def test_quick_does_not_run_the_live_probes(self, healthy_env, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("a live leg ran under --quick")

        monkeypatch.setattr(doctor, "preflight", _boom)
        monkeypatch.setattr(doctor, "probe_credentials", _boom)
        monkeypatch.setattr(doctor, "run_selfcheck", _boom)
        doctor.collect_checks(SyncadeConfig(), healthy_env, quick=True)  # must not raise

    def test_quick_exits_0_when_cheap_checks_green(self, healthy_env, capsys):
        assert doctor.run_doctor(SyncadeConfig(), healthy_env, quick=True) == SUCCESS

    def test_quick_still_reds_on_a_cheap_failure(self, healthy_env, monkeypatch, capsys):
        monkeypatch.setattr(doctor_env, "which", _which_none)
        assert doctor.run_doctor(SyncadeConfig(), healthy_env, quick=True) == WORKTREE_ERROR

    def test_quick_summary_counts_skips_separately_not_as_passed(self, healthy_env, capsys):
        # F5' (dogfood round 2): the skipped live legs must read as "skipped", never be folded
        # into the "passed" total. Compute the expected split from the checks so the assertion
        # doesn't hard-code the check count.
        checks = doctor.collect_checks(SyncadeConfig(), healthy_env, quick=True)
        n_passed = sum(1 for c in checks if c.status == "ok")
        n_skipped = sum(1 for c in checks if c.status == "skip")
        assert n_skipped == 2  # the two live legs
        doctor.run_doctor(SyncadeConfig(), healthy_env, quick=True)
        out = capsys.readouterr().out
        assert f"{n_passed} check(s) passed, {n_skipped} skipped" in out
        assert f"{len(checks)} check(s) passed" not in out  # the F5' bug: skips-as-passed


class TestCliDoctor:
    def test_healthy_repo_exits_0(self, healthy_env):
        assert main(["--doctor", "--repo-root", str(healthy_env)]) == SUCCESS

    def test_doctor_is_inert(self, healthy_env):
        # C2 — no artifact written, HEAD unchanged.
        head_before = _head(healthy_env)
        main(["--doctor", "--repo-root", str(healthy_env)])
        assert not (healthy_env / ".syncade" / "runs").exists()
        assert _head(healthy_env) == head_before

    def test_missing_cli_exits_60(self, healthy_env, monkeypatch):
        monkeypatch.setattr(doctor_env, "which", _which_none)
        assert main(["--doctor", "--repo-root", str(healthy_env)]) == WORKTREE_ERROR

    def test_accepts_base_flag(self, healthy_env):
        # doctor is the one preflight that must NOT reject --base/--scope.
        assert main(["--doctor", "--base", "HEAD", "--repo-root", str(healthy_env)]) != 2

    def test_accepts_scope_flag(self, healthy_env):
        assert main(["--doctor", "--scope", "everything", "--repo-root", str(healthy_env)]) != 2

    def test_quick_flag_exits_0(self, healthy_env):
        assert main(["--doctor", "--quick", "--repo-root", str(healthy_env)]) == SUCCESS

    def test_quick_without_doctor_is_a_usage_error(self, capsys):
        assert main(["--quick"]) == 2
        assert "--quick is meaningful only with --doctor" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "other",
        [
            ["x.md"],
            ["--auth-check"],
            ["--selfcheck"],
            ["--gc"],
            ["--metrics"],
            ["--spec-audit", "x.md"],
        ],
    )
    def test_mutex_with_other_modes(self, other, capsys):
        assert main(["--doctor", *other]) == 2
        assert "--doctor cannot be combined" in capsys.readouterr().err

    def test_config_error_exits_50(self, healthy_env):
        (healthy_env / ".syncade").mkdir()
        (healthy_env / ".syncade" / "config.toml").write_text("[unclosed\n")
        assert main(["--doctor", "--repo-root", str(healthy_env)]) == CONFIG_ERROR

    def test_doctor_in_help(self, capsys):
        with pytest.raises(SystemExit):
            main(["--help"])
        assert "--doctor" in capsys.readouterr().out


class TestTimeout:
    """Finding 5 (minor) — --timeout threads into the live legs."""

    def test_timeout_threaded_to_auth_probe(self, healthy_env, monkeypatch):
        captured: dict = {}

        def _spy(*a, **k):
            captured["timeout_seconds"] = k.get("timeout_seconds")
            return [_ok_auth()]

        monkeypatch.setattr(doctor, "probe_credentials", _spy)
        doctor.collect_checks(SyncadeConfig(), healthy_env, timeout_seconds=42.0)
        assert captured.get("timeout_seconds") == 42.0

    def test_timeout_threaded_to_selfcheck(self, healthy_env, monkeypatch):
        captured: dict = {}

        def _spy(*a, **k):
            captured.update(k)
            return SUCCESS

        monkeypatch.setattr(doctor, "run_selfcheck", _spy)
        doctor.collect_checks(SyncadeConfig(), healthy_env, timeout_seconds=42.0)
        assert captured.get("timeout_seconds") == 42.0
