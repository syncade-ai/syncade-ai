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
from shutil import disk_usage, which

from syncade.adapters.registry import known_providers
from syncade.auth_check import probe_credentials
from syncade.auth_preflight import preflight, report_lines
from syncade.config import SyncadeConfig
from syncade.config_auth import ALL_BLOCKS
from syncade.doctor_preview import check_cost, check_plan
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
_MIN_FREE_DISK_BYTES: int = 1024**3  # 1 GiB

# provider -> the CLI binary a run of that provider shells out to. The BEHAVIOURAL source
# of truth is the adapters (adapters/anthropic.py runs ``claude``; adapters/openai.py runs
# ``codex``); this mirrors them for a synchronous PATH pre-check that costs nothing and runs
# even when the live probes are skipped. It covers ``known_providers()`` by construction —
# config validation rejects any other provider before doctor runs — and
# ``tests/doctor/test_doctor.py`` fails if the registry grows a provider absent here.
_PROVIDER_CLI: dict[str, str] = {"anthropic": "claude", "openai": "codex"}


def _configured_providers(config: SyncadeConfig) -> list[str]:
    """Unique provider names across every actor (reviewers, producer, and the three cold
    actors), in first-seen order."""
    seen: list[str] = []
    for actor in (
        *config.reviewers,
        config.producer,
        config.synthesizer,
        config.drafter,
        config.auditor,
    ):
        if actor.provider not in seen:
            seen.append(actor.provider)
    return seen


def _check_config(config: SyncadeConfig) -> DoctorCheck:
    """Summarise the resolved config doctor is operating on. Always green when reached (a
    broken config exits 50 upstream); surfaced so the operator SEES which actors a run will
    dispatch before anything spends."""
    detail = (
        f"{len(config.reviewers)} reviewer(s): "
        + ", ".join(f"{r.provider}/{r.model}" for r in config.reviewers)
        + f"; producer {config.producer.provider}/{config.producer.model}"
        + f"; judge {config.synthesizer.provider}/{config.synthesizer.model}"
    )
    return DoctorCheck("config", _OK, detail)


_CLI_LAUNCH_TIMEOUT: float = 5.0  # seconds for --version probe; fail fast, no network needed


def _probe_cli_launch(binary: str) -> None:
    """Run ``binary --version`` to verify the binary is actually executable and exits cleanly.

    Raises ``OSError`` if the interpreter cannot be found (e.g. broken shebang),
    ``subprocess.TimeoutExpired`` if the binary hangs on startup, or
    ``subprocess.CalledProcessError`` if the binary exits non-zero — a non-zero exit means
    the binary is not usable, and a real review dispatching it would fail.

    Extracted as a module-level function so tests can patch ``doctor._probe_cli_launch``
    without affecting unrelated subprocess calls (e.g. git in ``_head_has_commit``)."""
    result = subprocess.run(
        [binary, "--version"],
        capture_output=True,
        timeout=_CLI_LAUNCH_TIMEOUT,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, binary)


def _check_provider_clis(config: SyncadeConfig) -> list[DoctorCheck]:
    """One check per configured provider: its CLI binary is on PATH AND can be launched.
    PATH discovery via ``shutil.which``; executability via a ``--version`` probe with a short
    timeout. A script with a broken shebang interpreter is on PATH but raises OSError on
    exec; this catches it before a real run fails with SubprocessNotFoundError."""
    checks: list[DoctorCheck] = []
    for provider in _configured_providers(config):
        binary = _PROVIDER_CLI.get(provider)
        if binary is None:
            # Reviewer providers are NOT validated at config load (unlike the cold actors), so
            # a config can carry a provider with no registered adapter — the real run then
            # raises UnknownProviderError at dispatch. Red that (a real run fails). A provider
            # that IS in the registry but lacks a _PROVIDER_CLI binary mapping is doctor's own
            # gap (the drift test guards it) -> skip, not red.
            if provider in known_providers():
                checks.append(
                    DoctorCheck(
                        f"cli:{provider}",
                        _SKIP,
                        f"no PATH mapping for provider {provider!r} — doctor needs updating",
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        f"cli:{provider}",
                        _RED,
                        f"unknown provider {provider!r} — no adapter is registered, so a real "
                        f"run cannot dispatch it",
                        fix=f"fix the provider name in .syncade/config.toml (known: "
                        f"{', '.join(known_providers())})",
                    )
                )
            continue
        found = which(binary)
        if not found:
            checks.append(
                DoctorCheck(
                    f"cli:{provider}",
                    _RED,
                    f"{binary} not found on PATH",
                    fix=f"install the {provider} CLI ({binary}) and put it on your PATH",
                )
            )
            continue
        # Verify the resolved binary is actually launchable — shutil.which only checks
        # PATH/permission bits, not whether the interpreter (e.g. a broken shebang) exists.
        try:
            _probe_cli_launch(binary)
            checks.append(DoctorCheck(f"cli:{provider}", _OK, f"{binary} on PATH ({found})"))
        except OSError as exc:
            checks.append(
                DoctorCheck(
                    f"cli:{provider}",
                    _RED,
                    f"{binary} found at {found} but cannot be launched: {exc}",
                    fix=f"reinstall the {provider} CLI ({binary}); found but not executable",
                )
            )
        except subprocess.CalledProcessError as exc:
            checks.append(
                DoctorCheck(
                    f"cli:{provider}",
                    _RED,
                    f"{binary} at {found} exited {exc.returncode} on --version — not runnable",
                    fix=f"reinstall or reconfigure the {provider} CLI ({binary}); "
                    f"it exits {exc.returncode} on --version",
                )
            )
        except subprocess.TimeoutExpired:
            timeout = int(_CLI_LAUNCH_TIMEOUT)
            checks.append(
                DoctorCheck(
                    f"cli:{provider}",
                    _RED,
                    f"{binary} found at {found} but did not respond to --version within {timeout}s",
                    fix=f"check the {provider} CLI ({binary}) — may be hanging on startup",
                )
            )
    return checks


def _check_worktree_root(worktree_base: Path) -> DoctorCheck:
    """The worktree base (``worktree_base`` — ``config.worktree_base`` or ``--worktree-base``,
    default :data:`DEFAULT_WORKTREE_BASE` = ``/tmp/syncade``) must be writable and its filesystem
    must have headroom — every reviewer/producer/test leg checks out a worktree there. Reading the
    configured/overridden base (not the hardcoded default) keeps the preview honest for a run that
    relocated it. **Strictly inert (F4'):** probes the nearest EXISTING ancestor (the base if
    present, else its first existing parent) with ``os.access`` — a pure permission read that writes
    NOTHING, not even the directory-mtime bump a create-then-delete tempfile would cause.
    ``os.access`` can theoretically false-green under exotic ACLs / root-on-read-only, but the real
    run's worktree provisioning is the final arbiter (a clean exit-60 if it is ever wrong) — worth
    it to keep ``--quick`` truly side-effect-free."""
    probe_dir = worktree_base
    # lexists (not exists): a BROKEN symlink is "present" and must stop the walk-up — exists()
    # follows the link, returns False for a broken target, and would skip PAST it to the parent
    # and false-green, while the real run's mkdir(parents=True) fails on that same symlink.
    while not os.path.lexists(probe_dir):
        probe_dir = probe_dir.parent  # terminates at "/", which always exists
    if probe_dir.is_symlink() and not probe_dir.exists():
        return DoctorCheck(
            "worktree",
            _RED,
            f"{probe_dir} is a broken symlink — worktrees cannot be created under it",
            fix=f"remove or repoint {probe_dir}; its target must exist as a directory",
        )
    if not probe_dir.is_dir():
        return DoctorCheck(
            "worktree",
            _RED,
            f"{probe_dir} exists but is not a directory — worktrees cannot be created under it",
            fix=f"remove or rename {probe_dir}; it must be a directory for run worktrees",
        )
    # W_OK to create worktree entries, X_OK to traverse into the base.
    if not os.access(probe_dir, os.W_OK | os.X_OK):
        return DoctorCheck(
            "worktree",
            _RED,
            f"{probe_dir} is not writable",
            fix=f"make {worktree_base} (or {probe_dir}) writable for run worktrees",
        )
    free = disk_usage(probe_dir).free
    free_gib = free / 1024**3
    if free < _MIN_FREE_DISK_BYTES:
        return DoctorCheck(
            "worktree",
            _RED,
            f"only {free_gib:.1f} GiB free on {probe_dir}'s filesystem — worktrees may fail",
            fix="free up disk space; each reviewer/producer/test leg checks out a worktree",
        )
    return DoctorCheck("worktree", _OK, f"{probe_dir} writable, {free_gib:.1f} GiB free")


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
) -> DoctorCheck:
    """Preview the branch a real run would touch and the refusals it would hit — for $0,
    before any spend (C5). Mirrors the CLI's own resolution EXACTLY (C1): ``will_commit``
    is ``effective_rounds > 1``, and the guard / dirty-tree checks are the same calls
    ``syncade <pr-doc>`` makes. Read-only (git status / symbolic-ref), so inert."""
    effective = max_rounds if max_rounds is not None else config.loop.max_rounds
    will_commit = effective > 1
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
        return DoctorCheck(
            "branch", _OK, f"single-pass (max_rounds={effective}) commits nothing; guard N/A"
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
        ),
        check_plan(repo_root, config, base_ref=base_ref, scope=scope, max_rounds=max_rounds),
        check_cost(config, repo_root, max_rounds=max_rounds),
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
        timeout_seconds=timeout_seconds,
    )
    _render(checks, quiet=quiet)
    return SUCCESS if not any(c.status == _RED for c in checks) else WORKTREE_ERROR
