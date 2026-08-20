"""``syncade --update`` — PR-h-field-07 item 5.

**A process cannot upgrade itself.** Its modules are already imported; if the files change
underneath it, anything imported LATER comes back new while everything already loaded stays old.
A half-and-half process is worse than either version, so this upgrades and then EXITS, asking to
be re-run. `os.execv` was considered and rejected: replacing an executable mid-replacement, plus
argv preservation and loop-guarding, for a cosmetic gain. In a terminal the re-run is one
keystroke; in a harness the skill does it, which is what makes it seamless from the chat side.

**Install-method detection PROVES or REFUSES; it never guesses.** The method is read from where
``syncade`` actually lives — a marker file inside its own install tree — not from a table of
default paths, which vary with ``UV_TOOL_DIR``, ``PIPX_HOME``, XDG settings and platform. When no
marker is found, this prints the manual command and says plainly that it could not tell. That is
the PR-h-04.5 shape one layer up: refuse rather than act on an unproven belief.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import syncade
from syncade.exit_codes import SUCCESS, WORKTREE_ERROR
from syncade.process import SubprocessError, run_subprocess
from syncade.update_check import MANIFEST_URL, _fetch, _parse, _text

_UPGRADE_TIMEOUT = 300.0
#: Reading one version string out of a fresh interpreter. Generous, because a cold venv
#: import is slower than it looks; short, because we already hold the operator's terminal.
_VERSION_TIMEOUT = 60.0
#: How far up from ``syncade/__init__.py`` an install marker can sit. A venv layout is
#: ``<root>/lib/python3.x/site-packages/syncade/__init__.py`` — five parents to the root; the
#: extra headroom costs nothing and covers layouts that nest one deeper.
_MARKER_DEPTH = 7


@dataclass(frozen=True)
class InstallMethod:
    kind: str  # "uv" | "pipx" | "source" | "unknown"
    command: list[str] | None  # the upgrade to run, or None when we must not run anything
    where: Path | None


def detect_install() -> InstallMethod:
    """Prove how syncade got here by finding a marker inside its own install tree."""
    start = Path(syncade.__file__).resolve().parent
    for parent in [start, *start.parents][:_MARKER_DEPTH]:
        if (parent / "uv-receipt.toml").is_file():
            return InstallMethod("uv", ["uv", "tool", "upgrade", "syncade"], parent)
        if (parent / "pipx_metadata.json").is_file():
            return InstallMethod("pipx", ["pipx", "upgrade", "syncade"], parent)
        # An ENVIRONMENT ends the walk before any enclosing checkout can claim it. A plain
        # venv install commonly lives at `<checkout>/.venv/lib/pythonX/site-packages/syncade`,
        # and without this the ancestor `pyproject.toml` made it read as a source checkout — so
        # `--update` refused with "use git", which would not update the installed package at
        # all. Checked AFTER the uv/pipx markers, which sit at this same root and are more
        # specific; reaching here means the environment exists but its manager is unproven,
        # which is exactly what "unknown" is for.
        if (parent / "pyvenv.cfg").is_file():
            return InstallMethod("unknown", None, parent)
        # A checkout is the one case where upgrading would be actively wrong: the operator's
        # own working tree is the install, and `uv tool upgrade` would neither touch it nor
        # tell them why nothing changed. Detected by the project file naming THIS project, so a
        # coincidental pyproject.toml higher up cannot claim it.
        pyproject = parent / "pyproject.toml"
        if pyproject.is_file() and 'name = "syncade"' in _read(pyproject):
            return InstallMethod("source", None, parent)
    return InstallMethod("unknown", None, None)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def live_run(cwd: Path) -> Path | None:
    """A run in ``cwd`` that is still going, or ``None``.

    Upgrading underneath a running review is the same half-and-half hazard from outside the
    process: the reviewers and producer it has yet to spawn would load the new code while the
    orchestrator that planned them is the old one. Scoped to this repo's ``.syncade/runs/``,
    which is where the breadcrumb lives; a run in some other checkout is not visible here and
    the message says so rather than implying a global guarantee.
    """
    import json

    from syncade.run_status import STATUS_FILENAME, is_stale_running

    runs = cwd / ".syncade" / "runs"
    if not runs.is_dir():
        return None
    for status_path in sorted(runs.glob(f"*/{STATUS_FILENAME}")):
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # `is_stale_running` is true for a HARD-KILLED run, i.e. running-but-dead. A live run is
        # running and NOT stale — reusing the existing predicate rather than re-deriving
        # liveness, so the two cannot drift apart on what "still going" means.
        if isinstance(status, dict) and status.get("state") == "running":
            if not is_stale_running(status):
                return status_path.parent
    return None


def _syncade_argv() -> list[str]:
    # Use sys.executable rather than shutil.which("syncade"): the latter resolves against
    # ambient PATH and would invoke a shadow when the operator ran syncade by absolute path
    # or from a venv while a different syncade is earlier on PATH. sys.executable is the
    # interpreter of the upgraded install, so the subprocess always runs the right package.
    return [sys.executable, "-m", "syncade"]


def _resync_skills(out) -> bool:
    """Re-install every skill the operator ALREADY has, using the UPGRADED code.

    A subprocess, not an in-process call: this process is still running the OLD package, so
    installing from here would faithfully write the OLD skill files — the exact drift the
    upgrade is meant to close. Harnesses with no skill installed are skipped rather than gaining
    one they never asked for.

    Returns True when every installed skill was successfully re-synced, False when at least one
    was refused (so the caller can propagate the partial failure rather than claiming success).
    """
    from syncade.cli.skill_status import HARNESSES, skill_status

    all_ok = True
    for harness in HARNESSES:
        if skill_status(harness).status == "absent":
            continue
        result = subprocess.run(  # noqa: S603
            [*_syncade_argv(), "--install-skill", harness],
            capture_output=True,
            text=True,
        )
        if result.returncode == SUCCESS:
            print(f"[syncade] re-installed the {harness} skill", file=out)
        else:
            all_ok = False
            # `--install-skill` refuses rather than destroying — a non-zero here usually means
            # the operator edited their copy or the skill has no install record (unknown state).
            # Tell them the exact command to force a resync with their explicit consent.
            print(
                f"[syncade] the {harness} skill was NOT re-installed — syncade refused rather "
                f"than overwrite it. Run `syncade --install-skill {harness} --force-install` "
                "to sync it regardless.",
                file=out,
            )
    return all_ok


def _installed_version() -> str | None:
    """The version on disk NOW, or ``None`` when it cannot be established.

    It CANNOT be ``syncade.__version__``: this process imported that before the upgrade ran, so
    it is by definition the old number. A fresh interpreter re-reads the metadata the upgrade
    just rewrote, and ``sys.executable`` is the tool venv's own python under both uv and pipx —
    precisely the environment that changed.

    ``None`` means "could not verify", which is deliberately NOT the same as "did not change":
    the caller must not turn an unreadable version into a failure report.
    """
    try:
        result = run_subprocess(
            # `-I` (isolated) is load-bearing, not tidiness. Without it the probe inherits
            # PYTHONPATH and the user site dir, so a `sitecustomize` on that path runs inside
            # the answer and can emit stdout before OR after the version print.
            # `-I` keeps the venv's own site-packages, so `importlib.metadata` still finds
            # the install (verified) — but it does NOT suppress `sitecustomize.py` that lives
            # in those site-packages. An atexit hook registered there (syncade ships such a
            # shim for worktree imports) can append a version-shaped line AFTER the real one.
            # `os._exit(0)` bypasses all atexit handlers: the process terminates immediately
            # after `os.write`, so nothing can append to our stdout (reproduced: a hostile
            # sitecustomize turned "0.7.0" into "0.7.0\n99.0.0" before this fix).
            # `os.write` is used instead of `print` because `os._exit` skips stdio flushing;
            # an unbuffered syscall guarantees the version byte reaches the pipe.
            [
                sys.executable,
                "-I",
                "-c",
                "import importlib.metadata as m, os; "
                "os.write(1, (m.version('syncade') + '\\n').encode()); "
                "os._exit(0)",
            ],
            timeout=_VERSION_TIMEOUT,
        )
    except SubprocessError:
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    # sitecustomize from PYTHONPATH (suppressed by -I) could emit noise before -c runs;
    # the venv's own sitecustomize can still do so. Take the LAST line — atexit is now
    # impossible (os._exit), so the last line is what os.write put there — and validate it
    # parses as a version. Noise that accidentally ends in a dotted string still fails _parse.
    candidate = raw.splitlines()[-1].strip()
    return candidate if _parse(candidate) is not None else None


def _is_newest(installed: str) -> bool:
    """Whether ``installed`` is at or beyond the newest version the manifest advertises.

    Only ever consulted on the unhappy path — the version failed to move — so the happy path
    stays offline. Anything that leaves the question unanswered (unreachable manifest, no usable
    ``latest``, an installed version we cannot parse) returns ``False``, i.e. "cannot prove you
    are current", which routes to the louder message. That is the safe direction: telling someone
    to check for a pin when they were merely current is a wasted minute; telling someone they are
    up to date when the upgrade silently failed strands them on an old version believing it is
    the new one.

    **This compares versions itself and must never delegate to ``evaluate()``.** That function
    answers a DIFFERENT question — "should the operator see a notice?" — and the two answers
    only usually coincide. They part company in the documented critical-no-fix state: when
    ``critical_below`` is above the installed version and no newer release exists, ``evaluate()``
    returns a notice while the operator is genuinely current, so reading its ``None``-ness told a
    fully up-to-date install that its upgrade had failed and it was probably pinned. Two dogfood
    rounds landed on this function for the same underlying reason — a near-miss reuse of a
    neighbouring answer — and the fix both times was to ask the actual question.

    There is deliberately no special case for an unreachable manifest: ``{}`` has no usable
    ``latest``, so the one parse rule already answers it. An explicit branch was there and a
    mutation run proved it dead — every input reached the same verdict without it.
    """
    manifest = _fetch(MANIFEST_URL) or {}
    latest = _parse(_text(manifest.get("latest")))
    current = _parse(installed)
    return latest is not None and current is not None and current >= latest


def run_update(*, cwd: Path | None = None, out=None) -> int:
    """Upgrade syncade in place, then exit asking to be re-run."""
    out = out or sys.stderr
    cwd = cwd or Path.cwd()

    # Resolve to the actual git repo root so live_run() does not miss a run when invoked
    # from a subdirectory — `.syncade/runs/` is at the root, not at the invocation directory.
    # Fails open: if discovery raises (not in a git repo, git not on PATH, etc.) we fall back
    # to cwd and check there, which is correct behaviour for a non-repo invocation.
    try:
        from syncade.snapshot import discover_repo_root

        cwd = discover_repo_root(cwd)
    except Exception:  # noqa: BLE001
        pass

    running = live_run(cwd)
    if running is not None:
        print(
            f"[syncade] refusing to update: a run is still going ({running.name}).\n"
            "          Upgrading underneath it would leave the run half-old and half-new.\n"
            "          Wait for it to finish, then run `syncade --update` again.",
            file=out,
        )
        return WORKTREE_ERROR

    method = detect_install()
    if method.kind == "source":
        print(
            f"[syncade] this syncade runs from a source checkout at {method.where}.\n"
            "          Update it with git, not a package manager.",
            file=out,
        )
        return WORKTREE_ERROR
    if method.command is None:
        print(
            "[syncade] could not determine how syncade was installed, so nothing was run.\n"
            "          If you used pip:  pip install -U syncade",
            file=out,
        )
        return WORKTREE_ERROR

    print(f"[syncade] {' '.join(method.command)}", file=out)
    # `SubprocessResult.returncode` — NOT `exit_code`, which does not exist. The first version
    # read `exit_code` and every test passed, because the test double was hand-written with that
    # attribute: the fake encoded the belief instead of the type, so the suite and the mutation
    # run both validated the same wrong contract. A real upgrade would have raised AttributeError
    # AFTER upgrading, losing the skill re-sync and the re-run instruction. The tests now build
    # the real SubprocessResult, so a rename breaks them instead of hiding in them.
    try:
        result = run_subprocess(method.command, timeout=_UPGRADE_TIMEOUT)
    except SubprocessError as exc:
        # `uv`/`pipx` absent from PATH, or unlaunchable. Reported, not escaped: an operator who
        # ran --update deserves to be told the package manager is missing.
        print(f"[syncade] update failed: {exc}", file=out)
        return WORKTREE_ERROR
    if result.returncode != 0:
        print(
            f"[syncade] update failed (exit {result.returncode}). Nothing was changed.\n"
            + (result.stderr.strip() or result.stdout.strip())[:800],
            file=out,
        )
        return WORKTREE_ERROR

    # THE POST-CONDITION. Exit 0 from the package manager means "the command ran", NOT "the
    # version moved", and the two come apart routinely: `uv tool install syncade==0.6.3` records
    # `specifier = "==0.6.3"` in its receipt, so `uv tool upgrade` honours the pin, prints
    # "Nothing to upgrade" and exits 0. Held packages, resolver conflicts and a manifest that is
    # ahead of PyPI all land in the same place. Without this check syncade says "updated. Re-run
    # your command", the operator re-runs, gets the same version, and is told again next session
    # — forever. Measured live on the 0.7.0 release, which is why item 5 of that release existed.
    now = _installed_version()
    if now is not None and now == syncade.__version__:
        # The version did not move. There are TWO reasons for that and they need opposite
        # answers, so ask the manifest which one this is rather than guessing. Guessing was the
        # first version of this guard and it told an operator who was simply already current
        # that their install was pinned and broken — a fresh false claim inside the fix for a
        # false claim, caught by testing the path instead of reasoning about it.
        if _is_newest(now):
            skills_ok = _resync_skills(out)
            print(f"[syncade] already up to date ({now}).", file=out)
            return SUCCESS if skills_ok else WORKTREE_ERROR
        print(
            f"[syncade] the upgrade command succeeded but syncade is still "
            f"{syncade.__version__} — nothing was installed.\n"
            f"          Most often the install pins a version: check `uv tool list` / "
            f"`pipx list`, then\n"
            f"          reinstall unpinned (e.g. `uv tool install --force syncade`).",
            file=out,
        )
        return WORKTREE_ERROR

    skills_ok = _resync_skills(out)
    moved = f" to {now}" if now else ""
    print(
        f"[syncade] updated{moved}. Re-run your command to use the new version — this process is\n"
        "          still running the old one and cannot switch to it.",
        file=out,
    )
    if not skills_ok:
        # The package upgrade succeeded but at least one skill resync was refused. The operator
        # now has a fresh CLI and a stale or recordless skill — not a clean update end to end.
        return WORKTREE_ERROR
    return SUCCESS
