"""Shared helper for the fake adapters.

``_noop_argv`` is the do-nothing-exit-0 argv every fake's ``build_invocation``
points at, so the dispatcher / synthesizer / producer can still flow through
``process.run_subprocess`` without errors during integration tests.
"""

from __future__ import annotations

import shutil
import sys


def _noop_argv() -> list[str]:
    """The argv for a "do nothing, exit 0" subprocess.

    POSIX gets a path discovered via ``shutil.which("true")``; Windows
    gets ``cmd /c exit 0``. Using ``shutil.which`` rather than a
    hard-coded path covers the GNU coreutils install (``/usr/bin/true``)
    AND the BSD/macOS layout (``/usr/bin/true``, with ``/bin/true``
    absent on recent macOS). Falls back to ``["true"]`` if not on PATH —
    that path is virtually never exercised because every POSIX system
    has ``true`` somewhere, but it keeps imports safe even in stripped
    environments.
    """
    if sys.platform == "win32":  # pragma: no cover - macOS/Linux dev path
        return ["cmd", "/c", "exit", "0"]
    resolved = shutil.which("true")
    return [resolved or "true"]
