"""A refusal after auto-init removes the repo syncade created — PR-h-04.6 item 2.

Two dogfoods tried to make refusals inert by hoisting checks ahead of the mutation one at a
time, and found seven distinct inputs doing it anyway. Predicting which inputs fail cannot
terminate. This inverts it: let auto-init happen, and if the run then refuses, remove what
was created.

The undo lives in the function-level `finally`, so ANY return between auto-init and the
review reaches it — including refusals added later. There is deliberately no list of refusal
sites, because that list is exactly what could not be completed.
"""

from __future__ import annotations

import subprocess

import pytest

import syncade.cli as cli_module
import syncade.git_preconditions as git_preconditions
from syncade.cli import main
from syncade.process import SubprocessError
from tests.cli._undo_helpers import _assert_undone, _dispatch, _fake_review, _fresh

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def test_a_refusal_after_auto_init_leaves_the_directory_inert(tmp_path, monkeypatch, capsys):
    """Core claim: a post-mutation refusal in an empty-start dir removes what auto-init created."""
    work, brief = _fresh(tmp_path)
    monkeypatch.setattr(cli_module, "_resolve_scope_base", lambda *a, **k: None)

    rc = main(["--repo-root", str(work), str(brief), "--scope", "everything"])

    assert rc == 60
    _assert_undone(work, " — a refused run mutated the operator's directory")


def test_a_successful_run_keeps_its_repository(tmp_path, monkeypatch):
    """Undo must not fire when the run proceeds — the repo is the run's substrate."""
    work, brief = _fresh(tmp_path)
    monkeypatch.setattr(cli_module, "run_review", _fake_review(work, 0))

    rc = main(["--repo-root", str(work), str(brief)])

    assert rc == 0
    assert (work / ".git").is_dir(), "undo fired on a successful run"


def test_a_review_verdict_is_not_a_refusal(tmp_path, monkeypatch):
    """A non-zero exit from a COMPLETED review (findings, max rounds, budget) must keep the
    repository. Conflating "non-zero" with "refused" would delete repos after real reviews —
    along with the run artifacts inside `.syncade/`."""
    work, brief = _fresh(tmp_path)
    monkeypatch.setattr(cli_module, "run_review", _fake_review(work, 30))

    rc = main(["--repo-root", str(work), str(brief)])

    assert rc == 30
    assert (work / ".git").is_dir(), "a findings verdict deleted the repository"


def test_git_is_the_only_authority_on_what_a_base_ref_means(tmp_path):
    """No allowlist decides which `--base` spellings are valid — PR-h-04.6 item 3.

    `HEAD^{commit}` is the representative case: a peeling suffix the old allowlist had to
    learn. Deliberately ONE case — re-enumerating spellings would repeat the mistake.
    Runs the REAL run_review so the evidence is git's own answer.
    """
    work, brief = _fresh(tmp_path)

    rc = main(
        ["--repo-root", str(work), str(brief), "--base", "HEAD^{commit}", "--max-rounds", "1"]
    )

    assert rc == 0, "a spelling git resolves did not reach the snapshot (2 = CLI, 60 = ref)"
    # And, per item 1, reviewing nothing is a refusal: no reviewer was dispatched, so the
    # directory the operator started with comes back untouched.
    _assert_undone(work, " — a run that reviewed nothing left a repository behind")


def test_an_unresolvable_base_refuses_inertly_without_an_allowlist(tmp_path):
    """The other direction: git refuses it, and undo leaves nothing behind.

    Before item 3 this was caught by the allowlist at exit 2. It is now caught by
    `take_snapshot` at exit 60 — git's own message, naming the real reason — and the
    directory is inert because undo removed what auto-init created.
    """
    work, brief = _fresh(tmp_path)

    rc = main(["--repo-root", str(work), str(brief), "--base", "does-not-exist"])

    assert rc == 60
    _assert_undone(work, " — an unresolvable --base left the repository behind")


def test_the_allowlist_is_gone_not_bypassed(tmp_path):
    """Attack #7: deleted, not disabled. A dormant allowlist would drift back into use."""
    import inspect

    from syncade import cli

    source = inspect.getsource(cli)
    assert "_INIT_CREATES" not in source
    assert "_check_base" not in source


# ── Fix 1: partial auto-init failure ──────────────────────────────────────────


def test_partial_auto_init_failure_leaves_directory_inert(tmp_path, monkeypatch):
    """If git commit fails mid-init, the created .git is removed; .gitignore survives.

    undo_auto_init removes .git and nothing else; .gitignore is not touched, so it is left
    intact even on a partial-init failure. Before the fix,
    ensure_repo_initialized raised SubprocessError and _init stayed None, so the
    finally block skipped undo and .git was left behind.
    """
    work, brief = _fresh(tmp_path)

    _original_git = git_preconditions._git

    def _fail_on_commit(path, *args):
        if args[0] == "commit":
            raise SubprocessError("baseline commit failed (injected)")
        return _original_git(path, *args)

    monkeypatch.setattr(git_preconditions, "_git", _fail_on_commit)

    rc = main(["--repo-root", str(work), str(brief)])

    assert rc == 60
    _assert_undone(work, " — a partial init failure left the repository behind")


# ── Fix 2: stale pre-existing .syncade/runs ───────────────────────────────────


def test_stale_runs_directory_does_not_suppress_undo(tmp_path, monkeypatch):
    """Pre-existing run artifacts from a prior invocation must not disable cleanup."""
    work, brief = _fresh(tmp_path)
    # Plant a stale run directory that predates this invocation.
    stale = work / ".syncade" / "runs" / "2020-01-01T00-00-00"
    stale.mkdir(parents=True)
    monkeypatch.setattr(cli_module, "_resolve_scope_base", lambda *a, **k: None)

    rc = main(["--repo-root", str(work), str(brief), "--scope", "everything"])

    assert rc == 60
    assert not (work / ".git").exists(), "stale runs suppressed undo; .git was left behind"
    assert (work / ".syncade" / "runs" / "2020-01-01T00-00-00").is_dir(), (
        "undo deleted a pre-existing run artifact"
    )


# ── Fix 3: signal interrupt must not trigger undo ────────────────────────────


# ── Blocker 3: unreadable .syncade/runs must refuse before mutation ───────────


def test_unreadable_runs_directory_refuses_before_auto_init(tmp_path, monkeypatch):
    """An unreadable .syncade/runs must cause a pre-mutation refusal.

    Before the fix, the preflight checked W|X but not R. A mode-0300 runs directory
    (write+execute, no read) passed the check, auto-init created .git, and then the
    finally block could not list the runs dir in either the pre-snapshot or the post-run
    scan — setting _no_artifacts=False and suppressing undo. The repository was left
    behind on a clean refusal.
    """
    work, brief = _fresh(tmp_path)
    runs_dir = work / ".syncade" / "runs"
    runs_dir.mkdir(parents=True)
    runs_dir.chmod(0o300)  # write+execute but no read

    try:
        rc = main(["--repo-root", str(work), str(brief)])

        assert rc == 60
        assert not (work / ".git").exists(), (
            "auto-init ran despite unreadable .syncade/runs — undo should have been avoided "
            "by refusing before mutation"
        )
    finally:
        runs_dir.chmod(0o755)  # restore so tmp_path cleanup succeeds


# ── Blocker 2: artifact-presence heuristic replaced by explicit verdict flag ──


def test_config_error_from_run_review_triggers_undo(tmp_path, monkeypatch):
    """CONFIG_ERROR from run_review triggers undo even though a run dir was created."""
    from syncade.exit_codes import CONFIG_ERROR

    work, brief = _fresh(tmp_path)

    stub = _fake_review(work, CONFIG_ERROR, reviewer_subprocess_started=False)
    monkeypatch.setattr(cli_module, "run_review", stub)

    rc = main(["--repo-root", str(work), str(brief)])

    assert rc == CONFIG_ERROR
    _assert_undone(work, " — a CONFIG_ERROR return left the repository behind")


@pytest.mark.parametrize(
    ("exit_code", "state"),
    [(60, "diff_malformed"), (0, "no_changes_to_review")],
)
def test_a_run_that_dispatched_no_reviewer_is_a_refusal(tmp_path, monkeypatch, exit_code, state):
    """Pre-dispatch refusals (no reviewer subprocess started) leave empty-start dirs clean.

    `diff_malformed` (exit 60) and `no_changes_to_review` (exit 0) both terminate before
    any reviewer subprocess starts. Undo must fire for empty-start directories in these cases.
    The predicate is `reviewer_subprocess_started` on each round's dispatch result — the
    explicit fact set only when Phase 3 (subprocess dispatch) actually begins.
    """
    work, brief = _fresh(tmp_path)

    monkeypatch.setattr(
        cli_module,
        "run_review",
        _fake_review(work, exit_code, reviewer_subprocess_started=False),
    )

    rc = main(["--repo-root", str(work), str(brief)])

    assert rc == exit_code
    _assert_undone(work, f" — {state} left a repository behind in an empty-start directory")


def test_one_dispatched_reviewer_is_enough_to_keep_everything(tmp_path, monkeypatch):
    """The other side of the same predicate — calibration against an over-eager undo.

    A run that dispatched a reviewer produced work, whatever verdict it reached. Exit 30
    (findings present) is the common NO-SHIP case and must keep the repo AND the artifacts.
    """
    work, brief = _fresh(tmp_path)

    monkeypatch.setattr(
        cli_module, "run_review", _fake_review(work, 30, reviewer_subprocess_started=True)
    )

    rc = main(["--repo-root", str(work), str(brief)])

    assert rc == 30
    assert (work / ".git").is_dir(), "a NO-SHIP verdict deleted the repository"
    assert (work / ".syncade" / "runs" / "2026-01-01T00-00-00").is_dir(), (
        "a NO-SHIP verdict deleted the run artifacts"
    )


def test_adapter_lookup_error_with_non_empty_results_triggers_undo(tmp_path, monkeypatch):
    """Adapter-lookup failures set reviewer_subprocess_started=False; undo fires in empty dirs."""
    work, brief = _fresh(tmp_path)

    monkeypatch.setattr(cli_module, "run_review", _fake_review(work, 40, False))

    rc = main(["--repo-root", str(work), str(brief)])

    assert rc == 40
    _assert_undone(work, " — an adapter lookup failure left the repository behind")


def test_producer_emptied_diff_does_not_trigger_undo(tmp_path, monkeypatch):
    """any() across all rounds prevents undo when a prior round started reviewer subprocesses."""
    work, brief = _fresh(tmp_path)

    stub = type(
        "R",
        (),
        {
            "exit_code": 0,
            "rounds": [
                type("Rnd", (), {"dispatch_result": _dispatch(True)})(),
                type("Rnd", (), {"dispatch_result": _dispatch(False)})(),
            ],
        },
    )()

    def _multi_round_review(*a, **k):
        (work / ".syncade" / "runs" / "2026-01-01T00-00-00").mkdir(parents=True, exist_ok=True)
        return stub

    monkeypatch.setattr(cli_module, "run_review", _multi_round_review)

    rc = main(["--repo-root", str(work), str(brief)])

    assert rc == 0
    assert (work / ".git").is_dir(), "producer_emptied_diff deleted the repository"
    assert (work / ".syncade" / "runs" / "2026-01-01T00-00-00").is_dir(), (
        "producer_emptied_diff deleted the run artifacts"
    )


def test_signal_interrupt_does_not_trigger_undo(tmp_path, monkeypatch):
    """SIGTERM/SIGINT after auto-init must leave the repo in place.

    L4: an interrupted auto-init is recoverable; one that deleted the repo is not.
    Before the fix, the CLI caught KeyboardInterrupt and returned, so sys.exc_info()[0]
    was None in the finally and undo fired.
    """
    import signal as _signal

    from syncade import run_status

    work, brief = _fresh(tmp_path)

    def _signal_ki(*a, **k):
        # Simulate SIGTERM arriving inside run_review before any run artifacts exist.
        run_status._received_signal = _signal.SIGTERM
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "run_review", _signal_ki)

    rc = main(["--repo-root", str(work), str(brief)])

    assert rc == 128 + _signal.SIGTERM
    assert (work / ".git").is_dir(), "a signal-interrupted auto-init repo was deleted by undo"


def test_a_partial_auto_init_is_still_undone(tmp_path, monkeypatch):
    """Round 0's blocker against the receipt design, now closed BY CONSTRUCTION.

    The receipt could not cover this: it was returned only after every git step succeeded, so
    a failure part-way through left `.git` behind with nothing recording it. Recording
    before each write helped and still lost the race in later rounds.

    The emptiness bit is captured before the FIRST write, by the caller, so a failure at any
    point inside auto-init cannot invalidate it.
    """
    import syncade.git_preconditions as gp

    work, brief = _fresh(tmp_path)
    real_git = gp._git

    def fail_at_commit(path, *args, **kwargs):
        if args and args[0] == "commit":
            raise gp.SubprocessError("simulated mid-init failure")
        return real_git(path, *args, **kwargs)

    monkeypatch.setattr(gp, "_git", fail_at_commit)

    rc = main(["--repo-root", str(work), str(brief)])

    assert rc == 60
    _assert_undone(work, " — a half-initialized repository was left behind")


def test_an_interrupted_run_keeps_its_repository(tmp_path, monkeypatch):
    """L4, and the case that defeated the previous guard while appearing to satisfy it.

    The old condition included `sys.exc_info()[0] is None`. That reads EMPTY in the `finally`
    once the CLI has caught `KeyboardInterrupt` and returned an exit code — so an interrupted
    run was indistinguishable from a clean refusal and its repository was deleted. A dogfood
    round found it; reading the code did not.

    An interrupted run leaving a repository is recoverable — re-running is safe because a
    pre-existing `.git` refuses auto-init. One that deleted the repository is not.
    """
    work, brief = _fresh(tmp_path)

    def interrupt(*a, **k):
        (work / ".syncade" / "runs" / "x").mkdir(parents=True, exist_ok=True)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "run_review", interrupt)

    rc = main(["--repo-root", str(work), str(brief)])

    assert rc != 0
    assert (work / ".git").is_dir(), (
        "an interrupted run had its repository deleted — the L4 guard is inferring again"
    )


def test_an_unexpected_exception_keeps_its_repository(tmp_path, monkeypatch):
    """An unexplained failure is not a clean refusal (L6): keep the repo and let it propagate."""
    work, brief = _fresh(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli_module, "run_review", boom)

    with pytest.raises(RuntimeError):
        main(["--repo-root", str(work), str(brief)])

    assert (work / ".git").is_dir(), "an unexplained failure deleted the repository"


def test_undo_never_consults_exception_state(tmp_path):
    """Attack #5's second half: the guard must be a decision, not a sniff.

    Pinned as source, because both failure modes here are invisible at runtime until the
    exact interrupt/exception shape occurs — which is how the previous implementation shipped
    looking correct.
    """
    import inspect

    from syncade import cli

    body = inspect.getsource(cli._run)
    undo_region = body[body.index("finally:") :]
    assert "exc_info" not in undo_region, (
        "the undo guard is reading exception state again; it must use the explicit "
        "_keep_repo decision"
    )


# ── A raise out of run_review keeps the repository, however it was finalized ──


@pytest.mark.parametrize("exc_name", ["WorktreeError", "SnapshotError"])
def test_a_raise_out_of_run_review_keeps_the_repository(tmp_path, monkeypatch, exc_name):
    """The unanimous dogfood blocker, and why the previous guard could not have caught it.

    The old handlers decided post-begin by reading `run_status.active()`. Real `run_review`
    FINALIZES the breadcrumb before re-raising (`orchestrator/loop.py`), so `active()` is
    already None at the handler and a genuine post-begin failure was classified as a clean
    refusal — the repository and its artifacts were deleted.

    The old tests missed it because their stubs called `begin()` and raised WITHOUT
    finalizing, leaving `active()` non-None. This stub reproduces the REAL sequence
    (begin -> finalize -> raise), so under `active()`-based classification it FAILS.

    The marker makes the whole question moot: it is set before `run_review` is entered, so
    no property of the exception, the breadcrumb, or their ordering is consulted.
    """
    from datetime import UTC, datetime

    from syncade import run_status
    from syncade.snapshot import SnapshotError
    from syncade.worktree import WorktreeError

    exc_type = {"WorktreeError": WorktreeError, "SnapshotError": SnapshotError}[exc_name]
    work, brief = _fresh(tmp_path)

    def _begin_finalize_then_raise(*a, **k):
        run_dir = work / ".syncade" / "runs" / "2026-01-01T00-00-00"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_status.begin(run_dir, datetime.now(tz=UTC))
        # What real run_review does before re-raising — and what left active() reading None.
        run_status.finalize_active(f"exception:{exc_name}", 60)
        assert run_status.active() is None, "fixture must reproduce the cleared breadcrumb"
        raise exc_type("injected post-begin failure")

    monkeypatch.setattr(cli_module, "run_review", _begin_finalize_then_raise)

    rc = main(["--repo-root", str(work), str(brief)])

    assert rc == 60
    assert (work / ".git").is_dir(), f"a post-begin {exc_name} deleted the auto-init repo"
    assert (work / ".syncade" / "runs" / "2026-01-01T00-00-00").is_dir(), (
        f"a post-begin {exc_name} deleted .syncade run artifacts"
    )


def test_signal_in_pre_run_window_does_not_trigger_undo(tmp_path, monkeypatch):
    """A signal between auto-init and run_review must not delete the repository.

    The window between ensure_repo_initialized success and with run_status.install_signal_handlers()
    had no signal guard. A SIGINT there left _keep_repo=False, so undo fired. Before the fix,
    the outer try had no except KeyboardInterrupt handler.
    """
    import signal as _signal

    from syncade import run_status

    work, brief = _fresh(tmp_path)

    def _inject_signal(*a, **k):
        run_status._received_signal = _signal.SIGTERM
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "guard_default_branch", _inject_signal)

    rc = main(["--repo-root", str(work), str(brief)])

    assert rc == 128 + _signal.SIGTERM
    assert (work / ".git").is_dir(), "a pre-run-window signal deleted the auto-init repo"
