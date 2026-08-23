"""Environment readiness for ``syncade --doctor`` — "is this machine set up?".

Split out of ``doctor.py`` when the diff-size prediction (PR-h-field-01 item 4) pushed that file
over the 500-LOC cap. The seam is the one ``CLAUDE.md`` already describes: these checks ask
whether the machine can run syncade AT ALL — config resolves, each configured provider's CLI
is on PATH and launchable, the worktree root is usable — and none of them look at the repo,
the diff, or the run plan. ``doctor.py`` keeps everything that is about THIS run: the branch
preview, the two live legs, and the collect/render/exit orchestration.

Strictly inert, like every doctor check (Invariant I4): no commit, no ref move, no artifact,
no ``/tmp`` worktree, not even a directory-mtime bump — writability is an ``os.access`` read,
never a tempfile.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from shutil import disk_usage, which

from syncade.adapters.registry import known_providers
from syncade.config import SyncadeConfig
from syncade.doctor_types import _OK, _RED, _SKIP, DoctorCheck

_MIN_FREE_DISK_BYTES: int = 1024**3  # 1 GiB

# provider -> the CLI binary a run of that provider shells out to. The BEHAVIOURAL source
# of truth is the adapters (adapters/anthropic.py runs ``claude``; adapters/openai.py runs
# ``codex``); this mirrors them for a synchronous PATH pre-check that costs nothing and runs
# even when the live probes are skipped. It covers ``known_providers()`` by construction —
# config validation rejects any other provider before doctor runs — and
# ``tests/doctor/test_doctor.py`` fails if the registry grows a provider absent here.
_PROVIDER_CLI: dict[str, str] = {"anthropic": "claude", "openai": "codex"}


_CLI_LAUNCH_TIMEOUT: float = 5.0  # seconds for --version probe; fail fast, no network needed


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
    must have headroom — every actor leg materializes a repository tree there. Reading the
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
            f"{probe_dir} is a broken symlink — actor workspaces cannot be created under it",
            fix=f"remove or repoint {probe_dir}; its target must exist as a directory",
        )
    if not probe_dir.is_dir():
        return DoctorCheck(
            "worktree",
            _RED,
            f"{probe_dir} exists but is not a directory — actor workspaces "
            "cannot be created under it",
            fix=f"remove or rename {probe_dir}; it must be a directory for run workspaces",
        )
    # W_OK to create worktree entries, X_OK to traverse into the base.
    if not os.access(probe_dir, os.W_OK | os.X_OK):
        return DoctorCheck(
            "worktree",
            _RED,
            f"{probe_dir} is not writable",
            fix=f"make {worktree_base} (or {probe_dir}) writable for run workspaces",
        )
    free = disk_usage(probe_dir).free
    free_gib = free / 1024**3
    if free < _MIN_FREE_DISK_BYTES:
        return DoctorCheck(
            "worktree",
            _RED,
            f"only {free_gib:.1f} GiB free on {probe_dir}'s filesystem — actor workspaces may fail",
            fix="free up disk space; each reviewer/producer/test leg materializes "
            "a repository tree",
        )
    return DoctorCheck("worktree", _OK, f"{probe_dir} writable, {free_gib:.1f} GiB free")


def _check_update_manifest(*, enabled: bool = True) -> DoctorCheck:
    """Can this machine reach the update manifest at all?

    A red row here does not affect a review — the background check is silent by design and must
    never slow or fail a run. It is worth a row because its failure is INVISIBLE everywhere else:
    an unreachable manifest and an up-to-date syncade look identical, forever, so an operator can
    sit years behind believing they are current. Doctor exists to surface exactly that kind of
    quietly-broken setup for $0.

    Measured cause on the reporting machine: python.org's macOS framework build ships no CA
    bundle, its `openssl_cafile` points at a `cert.pem` that does not exist, and TLS verification
    fails in 0.05s. Not a timeout — no timeout value would have changed it.

    Costs one bounded GET (1.5s ceiling, fails open), the same request the first run of any
    session already makes, so this adds no capability doctor did not already exercise.

    Skipped (with a SKIP row, not red) when ``enabled`` is False or when the ``CI`` environment
    variable is set — the same gate ``check_for_update`` applies to the background notice.
    A disabled check cannot be meaningfully diagnosed, and CI environments rarely have a human
    to act on the result.
    """
    import os

    from syncade.update_check import _parse, _text, manifest_once

    if not enabled or os.environ.get("CI"):
        return DoctorCheck(
            "update check",
            _SKIP,
            "skipped: update check is disabled (check=false or CI is set)",
        )
    manifest = manifest_once(enabled=True)  # `enabled` already gated above
    if manifest is None:
        return DoctorCheck(
            "update check",
            _RED,
            "cannot reach the update manifest — new releases will never be announced",
            "on macOS python.org builds, run `Install Certificates.command` from your "
            "Python's Applications folder; otherwise check network/proxy access to "
            "raw.githubusercontent.com",
        )
    latest = _text(manifest.get("latest"))
    if _parse(latest) is None:
        return DoctorCheck("update check", _RED, f"manifest reachable but unusable: {latest!r}")
    return DoctorCheck("update check", _OK, f"reachable; latest release is {latest}")
