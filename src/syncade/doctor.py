"""``syncade --doctor`` — read-only run preflight (PR-v2-12).

Composes the checks a first run needs into one green/red report so a failure is
self-serviceable instead of a GitHub issue. **Advisory (Invariant I4): doctor mutates
nothing** — no commit, no ref move, no artifact under ``.syncade/runs/``, no ``/tmp``
worktree — and it never changes a review's verdict. Its own exit code is a scriptable
green/red: :data:`~syncade.exit_codes.SUCCESS` (0) iff every non-skipped check is green,
else :data:`~syncade.exit_codes.WORKTREE_ERROR` (60) — the "environment isn't ready"
family ``--auth-check`` / ``--selfcheck`` already use. A config that will not load never
reaches here: the CLI handler maps that to ``CONFIG_ERROR`` (50) upstream, exactly like
every other one-shot mode.

This module is the check *engine*; the CLI dispatch (repo/config resolution) lives in
:mod:`syncade.cli.doctor_mode`. :func:`collect_checks` is pure (no I/O beyond the
read-only probes each check owns) so it is asserted directly, without scraping stdout.

The checks: resolved-config summary; each provider's CLI on PATH; worktree root + disk;
the branch preview (the exact default-branch / dirty-tree / detached-HEAD refusal a real
run would hit, for $0); the run-plan preview (resolved base + diff size, actor set, round
budget); the cost preview (API-equivalent $ range from the local corpus). The auth probe
and the producer headless-commit smoke are the two LIVE legs — they spawn a provider CLI
(~30s) and are what ``--quick`` skips.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from syncade.auth_check import probe_credentials
from syncade.auth_preflight import preflight, report_lines
from syncade.config import SyncadeConfig
from syncade.config_auth import ALL_BLOCKS

# Re-exported for import stability after the PR-h-field-01 split (Decomposition Rule): callers
# keep importing these from `syncade.doctor`. MONKEYPATCH TARGETS ARE `doctor_env`, NOT
# here — these names are bindings, and rebinding one does not change what the function
# bodies in doctor_env read.
from syncade.doctor_env import (  # noqa: F401
    _CLI_LAUNCH_TIMEOUT,
    _MIN_FREE_DISK_BYTES,
    _PROVIDER_CLI,
    _check_config,
    _check_provider_clis,
    _check_worktree_root,
    _configured_providers,
    _probe_cli_launch,
)
from syncade.doctor_preview import based_diff_classify, check_budget, check_cost, check_plan
from syncade.doctor_types import _OK, _RED, _SKIP, _STATUS_GLYPH, DoctorCheck
from syncade.exit_codes import SUCCESS, WORKTREE_ERROR
from syncade.orchestrator.branch_guard import current_branch_name, guard_default_branch
from syncade.selfcheck import run_selfcheck
from syncade.snapshot import SnapshotError, take_snapshot
from syncade.worktree import WorktreeError

# Danger floor for free disk on the worktree filesystem. Conservative on purpose: every
# reviewer/producer/test leg checks out a full worktree under DEFAULT_WORKTREE_BASE, and a
# machine under ~1 GiB free is at real risk of a mid-run `git worktree add` failure. Small
# enough that any healthy dev box clears it, so a red here means genuinely low, not tight.


def _check_auth(
    config: SyncadeConfig, *, timeout_seconds: float | None = None
) -> list[DoctorCheck]:
    """LIVE leg. Mirrors ``--auth-check``: first the declaration-honesty preflight (the
    codex ``auth = "api"``-on-a-subscription footgun), rendered as red rows instead of a
    refusal; if that is clean, one probe row per distinct credential (does it actually
    authenticate), followed by a billing-mode disclosure row matching what ``auth_gate``
    prints before every real run. Preflight and the probe both spawn a provider CLI, so
    this whole leg is what ``--quick`` skips."""
    env = dict(os.environ)
    problems = preflight(config, env, ALL_BLOCKS)
    if problems:
        # A contradicted declaration is the blocker; do not probe under a lie (the probe
        # can green a mis-declared codex, which is the exact footgun preflight catches).
        return [
            DoctorCheck(
                "auth",
                _RED,
                problem,
                fix="reconcile the actor's `auth =` with this machine's login",
            )
            for problem in problems
        ]
    rows: list[DoctorCheck] = []
    for result in probe_credentials(config, timeout_seconds=timeout_seconds):
        rows.append(
            DoctorCheck(
                f"auth:{result.provider}",
                _OK if result.ok else _RED,
                result.detail,
                fix=None if result.ok else "re-authenticate; `syncade --auth-check` shows detail",
            )
        )
    # Disclose resolved billing mode — the auth_gate analog the PR-v2-24 transparency
    # requirement adds to every real run. Only emit when auth is healthy (no point
    # disclosing billing mode for a credential the probe just rejected).
    if all(r.status == _OK for r in rows):
        billing = report_lines(config, env, ALL_BLOCKS)
        if billing:
            rows.append(
                DoctorCheck(
                    "auth:billing",
                    _OK,
                    "; ".join(ln.strip() for ln in billing if ln.strip()),
                )
            )
    return rows


def _check_producer_commit(
    config: SyncadeConfig, *, timeout_seconds: float | None = None
) -> DoctorCheck:
    """LIVE leg. Reuses ``--selfcheck`` wholesale: the producer must headless-commit in a
    throwaway workspace (that smoke never touches the operator's repo). Its verbose output
    is captured and dropped so doctor renders one clean row; the fix points at
    ``--selfcheck`` for the full transcript. ``--quick`` skips it (~30s + a real call).

    ``always_cleanup=True`` keeps doctor inert: the selfcheck preserves its workspace on
    failure by default (for debugging), but doctor drops that output, so a preserved workspace
    would be an invisible leftover. Doctor removes it and points at ``--selfcheck`` (which
    still preserves) for the raw output."""
    sink = io.StringIO()
    who = f"{config.producer.provider}/{config.producer.model}"
    try:
        with redirect_stdout(sink), redirect_stderr(sink):
            code = run_selfcheck(
                config, quiet=True, always_cleanup=True, timeout_seconds=timeout_seconds
            )
    except Exception as exc:
        return DoctorCheck(
            "producer-commit",
            _RED,
            f"{who} selfcheck raised an unexpected error: {exc}",
            fix="run `syncade --selfcheck` to see the full producer output",
        )
    if code == SUCCESS:
        return DoctorCheck("producer-commit", _OK, f"{who} committed headlessly")
    return DoctorCheck(
        "producer-commit",
        _RED,
        f"{who} could not headless-commit (selfcheck exit {code})",
        fix="run `syncade --selfcheck` to see the full producer output",
    )


def _head_has_commit(repo_root: Path) -> bool:
    """True iff HEAD resolves to a commit. False for an unborn HEAD (``git init`` with no
    commits): there, the real run's ``take_snapshot`` fails (``could not resolve HEAD`` ->
    exit 60), so doctor must red it rather than false-green single-pass or let the exception
    escape. Read-only."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet", "HEAD"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _check_branch(
    repo_root: Path,
    config: SyncadeConfig,
    *,
    max_rounds: int | None,
    allow_default_branch: bool,
    force_dirty: bool,
    base_ref: str | None = None,
    scope: str | None = None,
    two_dot: bool = False,
) -> DoctorCheck:
    """Branch preview (C5, C1): RED if malformed diff; ``will_commit`` iff dispatch. Inert."""
    effective = max_rounds if max_rounds is not None else config.loop.max_rounds
    # based_diff_classify returns "dispatch" immediately for baseless runs (base_ref=None,
    # scope=None) so the baseless case needs no special branch — behaviourally identical.
    _diff_class = based_diff_classify(
        repo_root, config, base_ref=base_ref, scope=scope, two_dot=two_dot
    )
    will_commit = effective > 1 and _diff_class == "dispatch"
    branch = current_branch_name(repo_root)

    # Unborn HEAD (git init, no commits): a real run snapshots HEAD and fails (exit 60), in
    # EVERY mode. Red it up front — before the single-pass early-return could false-green it,
    # and before take_snapshot below could raise an uncaught SnapshotError.
    if not _head_has_commit(repo_root):
        return DoctorCheck(
            "branch",
            _RED,
            "HEAD has no commits yet (unborn) — a review needs a committed HEAD to snapshot",
            fix="make at least one commit before running syncade",
        )

    # Default-branch guard — the exact call the CLI makes before dispatch.
    try:
        guard_default_branch(repo_root, branch, allow=allow_default_branch, will_commit=will_commit)
    except WorktreeError as exc:
        return DoctorCheck(
            "branch",
            _RED,
            str(exc),
            fix="re-run on a feature branch, or pass --allow-default-branch to commit here",
        )

    if not will_commit:
        if effective <= 1:
            return DoctorCheck(
                "branch", _OK, f"single-pass (max_rounds={effective}) commits nothing; guard N/A"
            )
        if _diff_class == "too_large":
            # Refused before dispatch, so no commit — the branch guard must not double-fire
            # (check_plan already reds this). Same reasoning as `no_changes`/`malformed`.
            return DoctorCheck(
                "branch",
                _OK,
                "no commits — the diff exceeds [loop] max_diff_bytes (see plan)",
            )
        if _diff_class == "prompt_too_large":
            # Refused before dispatch (prompt_too_large exits 60), so no commit.
            # check_plan already reds this; the branch guard must not double-fire.
            return DoctorCheck(
                "branch",
                _OK,
                "no commits — assembled reviewer prompt exceeds provider ceiling (see plan)",
            )
        if _diff_class == "malformed":
            # Malformed diff: real run exits 60 (diff_malformed) before dispatch — RED so the
            # cheap-red gate fires and live spend (auth/producer-commit) is skipped.
            return DoctorCheck(
                "branch",
                _RED,
                "based/scoped diff is malformed — real run exits 60 (diff_malformed)",
                fix="use --two-dot if paths contain ' b/'; check strip_repo_context_files",
            )
        return DoctorCheck(
            "branch", _OK, "based/scoped diff is known-empty; no producers fire — guard N/A"
        )

    # Loop mode on detached HEAD: the guard exempts it, but the producer's commits would be
    # unreachable and dropped (branch_advance -> skipped_detached_head). A doomed run, so red.
    if branch is None:
        return DoctorCheck(
            "branch",
            _RED,
            "HEAD is detached; a loop run would drop the producer's commits (no branch to advance)",
            fix="check out a branch before running a committing loop",
        )

    # Dirty-tree refusal — same condition loop.py enforces. Defensive SnapshotError -> red: a
    # diagnostic must never traceback (HEAD is committed here, so this is a belt-and-suspenders
    # guard against any other git failure).
    try:
        state = take_snapshot(repo_root).dirty_state
    except SnapshotError as exc:
        return DoctorCheck(
            "branch",
            _RED,
            f"cannot snapshot the working tree ({exc})",
            fix="ensure the repo has a resolvable HEAD and a clean git state",
        )
    if state in ("tracked", "both") and not force_dirty:
        return DoctorCheck(
            "branch",
            _RED,
            f"loop mode refuses a tracked-dirty tree (dirty_state={state!r})",
            fix="commit or stash your changes, or pass --force-dirty to run over your WIP",
        )
    return DoctorCheck(
        "branch", _OK, f"commits would fast-forward {branch!r}; tree dirty_state={state!r}"
    )


_LIVE_ANNOUNCE = (
    "[syncade] doctor: running live checks — auth probe + producer headless-commit "
    "(real provider calls, ~30s; pass --quick to skip)..."
)


def collect_checks(
    config: SyncadeConfig,
    repo_root: Path,
    *,
    quick: bool = False,
    max_rounds: int | None = None,
    allow_default_branch: bool = False,
    force_dirty: bool = False,
    base_ref: str | None = None,
    scope: str | None = None,
    two_dot: bool = False,
    timeout_seconds: float | None = None,
) -> list[DoctorCheck]:
    """Run every doctor check and return the results. The cheap checks (config, provider CLIs
    on PATH, worktree/disk, branch preview, run-plan + cost preview) are inert and always run.

    The two LIVE legs (auth probe + producer headless-commit) spawn provider CLIs and cost
    ~30s, so they are gated to preserve the ``$0`` doomed-run guarantee: they are reported as
    ``skip`` (skipped, NOT passed) when ``quick`` is set OR when any cheap check is already
    red — a red cheap check means a real run would be refused, so doctor must not spend on
    provider calls first. Within the live section the producer commit smoke runs only if the
    auth rows are all green (never spend on a commit smoke under a credential that failed).
    When the live legs will actually run, the ~30s warning is emitted here (right before the
    spend), not in the caller, so it fires exactly when — and only when — money is at stake.
    The warning bypasses ``--quiet`` deliberately (like the PR-v2-24 auth line): quiet may
    silence the report, but never a disclosure printed right before real spend."""
    cheap = [
        _check_config(config),
        *_check_provider_clis(config),
        _check_worktree_root(config.worktree_base),
        _check_branch(
            repo_root,
            config,
            max_rounds=max_rounds,
            allow_default_branch=allow_default_branch,
            force_dirty=force_dirty,
            base_ref=base_ref,
            scope=scope,
            two_dot=two_dot,
        ),
        check_plan(
            repo_root,
            config,
            base_ref=base_ref,
            scope=scope,
            two_dot=two_dot,
            max_rounds=max_rounds,
        ),
        check_cost(config, repo_root, max_rounds=max_rounds),
        check_budget(config),
    ]
    checks = list(cheap)
    if quick:
        checks.append(DoctorCheck("auth", _SKIP, "skipped (--quick): credential probe not run"))
        checks.append(
            DoctorCheck("producer-commit", _SKIP, "skipped (--quick): commit smoke not run")
        )
    elif any(c.status == _RED for c in cheap):
        reason = "skipped: a red check above would refuse this run before any spend"
        checks.append(DoctorCheck("auth", _SKIP, reason))
        checks.append(DoctorCheck("producer-commit", _SKIP, reason))
    else:
        # Spend disclosure: printed even under --quiet — real provider calls are imminent.
        print(_LIVE_ANNOUNCE, file=sys.stderr)
        auth_rows = _check_auth(config, timeout_seconds=timeout_seconds)
        checks.extend(auth_rows)
        if any(r.status == _RED for r in auth_rows):
            checks.append(
                DoctorCheck(
                    "producer-commit",
                    _SKIP,
                    "skipped: auth failed above — not spending on the producer commit smoke",
                )
            )
        else:
            checks.append(_check_producer_commit(config, timeout_seconds=timeout_seconds))
    return checks


def _render(checks: list[DoctorCheck], *, quiet: bool) -> None:
    """Print the green/red table (normal) or just the reds (quiet). stdout for the report,
    stderr for anything a red-detecting script should see."""
    reds = [c for c in checks if c.status == _RED]
    if not quiet:
        print(f"[syncade] doctor: {len(checks)} check(s)")
        width = max((len(c.name) for c in checks), default=0)
        for check in checks:
            print(f"  {_STATUS_GLYPH[check.status]} {check.name.ljust(width)}  {check.detail}")
            if check.fix:
                print(f"      → {check.fix}")
    else:
        for check in reds:
            print(f"[doctor] {check.name}: {check.detail}", file=sys.stderr)
            if check.fix:
                print(f"      → {check.fix}", file=sys.stderr)
    # Flush the stdout table before the stderr summary so a combined TTY reads top-to-bottom
    # (stderr is unbuffered and would otherwise race ahead of the buffered table).
    sys.stdout.flush()
    if reds:
        suffix = "" if quiet else " (see the → lines above)"
        print(f"[syncade] doctor: {len(reds)} check(s) need attention{suffix}", file=sys.stderr)
    else:
        # Always print the OK summary — even under --quiet — so skipped live legs are
        # visible. C3/C6: a skipped check must not read as silently passed. The full
        # per-row table is still suppressed in quiet mode; only this one-liner appears.
        n_passed = sum(1 for c in checks if c.status == _OK)
        n_skipped = sum(1 for c in checks if c.status == _SKIP)
        skip_note = f", {n_skipped} skipped" if n_skipped else ""
        print(f"[syncade] doctor OK: {n_passed} check(s) passed{skip_note}")


def run_doctor(
    config: SyncadeConfig,
    repo_root: Path,
    *,
    quick: bool = False,
    max_rounds: int | None = None,
    allow_default_branch: bool = False,
    force_dirty: bool = False,
    base_ref: str | None = None,
    scope: str | None = None,
    two_dot: bool = False,
    quiet: bool = False,
    timeout_seconds: float | None = None,
) -> int:
    """Collect the checks, render the report, and return the exit code: ``0`` when no check
    is red, else ``60``. This is doctor's whole contract — advisory to a review, scriptable
    on its own. The live-legs ~30s warning is emitted inside :func:`collect_checks`, which is
    the only place that knows whether the live legs will actually run (they are skipped when a
    cheap check is already red), so it fires exactly when a provider call is imminent."""
    checks = collect_checks(
        config,
        repo_root,
        quick=quick,
        max_rounds=max_rounds,
        allow_default_branch=allow_default_branch,
        force_dirty=force_dirty,
        base_ref=base_ref,
        scope=scope,
        two_dot=two_dot,
        timeout_seconds=timeout_seconds,
    )
    _render(checks, quiet=quiet)
    return SUCCESS if not any(c.status == _RED for c in checks) else WORKTREE_ERROR
