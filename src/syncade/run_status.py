"""Live run-status breadcrumb: never terminate without a discoverable reason.

One ``.syncade/runs/<id>/status.json`` per run, rewritten at each phase transition
(``state: running``) and finalized on every exit path (normal reason / OS signal /
exception). A ``running`` file whose pid is dead IS the evidence of a hard kill
(SIGKILL) at that phase — see :func:`is_stale_running`. syncade runs one review
per process, so the active status is a module global rather than threaded through
the orchestrator call chain.
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from syncade.persistence._atomic import atomic_write_json

STATUS_FILENAME = "status.json"


@dataclass
class RunStatus:
    """The live breadcrumb for one run. ``running()`` and ``finalize()`` each
    rewrite ``status.json`` atomically with the current phase."""

    path: Path
    started_at: datetime
    pid: int
    phase: str = "starting"
    round_index: int | None = None

    def _write(self, state: str, **extra: object) -> None:
        record = {
            "state": state,
            "phase": self.phase,
            "round": self.round_index,
            "pid": self.pid,
            "started_at_utc": self.started_at.isoformat(),
            "updated_at_utc": datetime.now(tz=UTC).isoformat(),
            **extra,
        }
        atomic_write_json(self.path, record, sort_keys=False)

    def running(self, phase: str, round_index: int | None = None) -> None:
        self.phase = phase
        self.round_index = round_index
        self._write("running")

    def finalize(self, reason: str, exit_code: int | None) -> None:
        self._write("terminated", reason=reason, exit_code=exit_code)


_active: RunStatus | None = None
_began: bool = False


def begin(run_dir: Path, started_at: datetime) -> RunStatus:
    """Create + register the active breadcrumb and write the first ``running`` record."""
    global _active, _began
    _active = RunStatus(Path(run_dir) / STATUS_FILENAME, started_at, os.getpid())
    _began = True
    _active.running("starting")
    return _active


def update_phase(phase: str, round_index: int | None = None) -> None:
    """Advance the active breadcrumb's phase. No-op when no run is active."""
    if _active is not None:
        _active.running(phase, round_index)


def finalize_active(reason: str, exit_code: int | None) -> None:
    """Finalize + clear the active breadcrumb. No-op when no run is active."""
    global _active
    if _active is not None:
        _active.finalize(reason, exit_code)
        _active = None


def clear_active() -> None:
    global _active, _began
    _active = None
    _began = False


def active() -> RunStatus | None:
    return _active


def began() -> bool:
    """Did :func:`begin` run in this process since the last :func:`clear_active`?

    ``active()`` cannot answer this: :func:`finalize_active` clears it, so by the time a
    caller handles an exception ``run_review`` re-raised, ``active()`` reads ``None`` for a
    run that had very much begun. A caller that used it to decide "was this a clean refusal?"
    deleted the operator's repository and its artifacts after a real mid-run failure.

    This flag is set at the one moment the run takes ownership — immediately after the run
    directory is created — and only ``clear_active`` (in-process reuse) resets it.
    """
    return _began


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


def is_stale_running(status: dict) -> bool:
    """A ``running`` status whose pid is no longer alive = hard-killed at its phase."""
    if status.get("state") != "running":
        return False
    pid = status.get("pid")
    return isinstance(pid, int) and pid > 0 and not _pid_alive(pid)


_received_signal: int | None = None

_SIGNALS = [signal.SIGTERM, signal.SIGINT]
if hasattr(signal, "SIGHUP"):
    _SIGNALS.append(signal.SIGHUP)


def _handler(signum: int, _frame: object) -> None:
    global _received_signal
    _received_signal = signum
    raise KeyboardInterrupt


@contextmanager
def install_signal_handlers() -> Iterator[None]:
    """Install parent-process SIGTERM/SIGINT/SIGHUP handlers that raise
    KeyboardInterrupt — so process.py's existing child-group cleanup fires and the
    interrupt bubbles to the run's finalizer. Restores prior handlers on exit.

    Must be called from the main thread (Python signal constraint); the CLI review
    dispatch is the main thread.
    """
    global _received_signal
    _received_signal = None  # clear any stale signum from a prior in-process run
    previous = {sig: signal.signal(sig, _handler) for sig in _SIGNALS}
    try:
        yield
    finally:
        for sig, prev in previous.items():
            signal.signal(sig, prev)


def received_signal() -> int | None:
    return _received_signal


def signal_exit_code(signum: int | None) -> int:
    """Shell convention: a process killed by signal N exits ``128 + N``."""
    return 128 + int(signum if signum is not None else signal.SIGINT)


def signal_name(signum: int | None) -> str:
    if signum is None:
        return "UNKNOWN"
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"SIG{signum}"


def finalize_signal() -> int:
    """Finalize the active breadcrumb with the received signal, log one stderr
    line, and return the ``128+signum`` exit code. Used by the CLI's
    KeyboardInterrupt handler."""
    signum = _received_signal
    name = signal_name(signum)
    rs = active()
    phase = rs.phase if rs is not None else "startup"
    if rs is not None:
        print(
            f"[syncade] terminated by signal {name} during '{phase}' — recorded in status.json",
            file=sys.stderr,
        )
    else:
        print(
            f"[syncade] terminated by signal {name} during '{phase}'"
            " — no active run, status.json not written",
            file=sys.stderr,
        )
    finalize_active(f"signal:{name}", signal_exit_code(signum))
    return signal_exit_code(signum)
