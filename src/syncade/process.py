"""Structured subprocess helper for reviewer-adapter invocations.

:func:`run_subprocess` wraps :func:`subprocess.Popen` with timeout
handling that kills the entire process group (not just the immediate
child), captures stdout and stderr separately, and surfaces the three
failure modes a reviewer dispatcher needs to distinguish:

- The binary at ``argv[0]`` isn't on ``PATH`` → :class:`SubprocessNotFoundError`
- The subprocess ran but exceeded its timeout → :class:`SubprocessTimeoutError`
- Anything else went wrong launching the process → :class:`SubprocessError`

The return code itself is never treated as a failure — callers decide
based on the exit code. A non-zero ``returncode`` in :class:`SubprocessResult`
is normal output, not an exception.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_TIMEOUT_DRAIN_SECONDS: Final = 0.25


@dataclass(frozen=True)
class SubprocessResult:
    """The outcome of a successfully-launched subprocess.

    Attributes:
        returncode: The process's exit code. Callers interpret this —
            ``run_subprocess`` itself never raises on non-zero.
        stdout: Captured standard output, decoded as text.
        stderr: Captured standard error, decoded as text.
        duration_seconds: Wall-clock time from launch to completion,
            measured via :func:`time.monotonic`.
    """

    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


class SubprocessError(Exception):
    """Base class for failures from :func:`run_subprocess`.

    A non-zero ``returncode`` does NOT raise this — that's normal output.
    This is raised only when the subprocess couldn't be launched, was
    killed for timing out, or hit some other OS-level failure.
    """


class SubprocessTimeoutError(SubprocessError):
    """Raised when the subprocess exceeded its timeout.

    The process group is killed (SIGKILL) before this exception is
    raised. Any output captured before the kill is attached as
    ``.stdout`` / ``.stderr`` so callers can surface partial reviewer
    output even on timeout.

    Attributes:
        stdout: Partial captured stdout, if any.
        stderr: Partial captured stderr, if any.
        timeout: The timeout value (seconds) that fired.
    """

    def __init__(
        self,
        message: str,
        *,
        stdout: str,
        stderr: str,
        timeout: float,
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.timeout = timeout


class SubprocessNotFoundError(SubprocessError):
    """Raised when ``argv[0]`` is not on ``PATH`` (or otherwise can't
    be exec'd as an executable).

    Attribute ``.binary`` names the missing executable so the CLI
    surface can produce a useful error like "claude is not installed"
    rather than a generic "no such file or directory".
    """

    def __init__(self, binary: str) -> None:
        super().__init__(f"executable not found on PATH: {binary!r}")
        self.binary = binary


def _decode_timeout_output(output: str | bytes | None) -> str:
    match output:
        case str():
            return output
        case bytes():
            return output.decode("utf-8", errors="replace")
        case None:
            return ""


def _kill_direct_process_if_running(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass


def _kill_process_group_if_running(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    _kill_direct_process_if_running(proc)


# Registry of in-flight child process groups. Reviewers dispatch in a
# ThreadPoolExecutor, so a signal landing in the MAIN thread while worker threads
# are blocked in communicate() cannot trigger their own except-BaseException
# cleanup. terminate_active_child_groups() lets the dispatcher's interrupt path
# kill those groups so each communicate() returns at once instead of the executor
# hanging in shutdown(wait=True) until the reviewer timeout.
_active_procs: set[subprocess.Popen[bytes]] = set()
_active_procs_lock = threading.Lock()


def _register_proc(proc: subprocess.Popen[bytes]) -> None:
    with _active_procs_lock:
        _active_procs.add(proc)


def _unregister_proc(proc: subprocess.Popen[bytes]) -> None:
    with _active_procs_lock:
        _active_procs.discard(proc)


def terminate_active_child_groups() -> int:
    """SIGKILL every in-flight child process group; return the count killed.

    Safe to call from any thread. The reviewer dispatcher calls this on interrupt
    so a signal during parallel dispatch tears the reviewers down promptly rather
    than orphaning them (their own killpg cleanup only fires when the interrupted
    thread IS the one in communicate() — true for synth/producer, false for the
    threaded reviewer phase)."""
    with _active_procs_lock:
        procs = list(_active_procs)
    for proc in procs:
        _kill_process_group_if_running(proc)
    return len(procs)


def _drain_after_timeout(
    proc: subprocess.Popen[bytes],
    timeout_exc: subprocess.TimeoutExpired,
) -> tuple[str, str]:
    initial_stdout = _decode_timeout_output(timeout_exc.stdout)
    initial_stderr = _decode_timeout_output(timeout_exc.stderr)
    try:
        stdout, stderr = proc.communicate(timeout=_TIMEOUT_DRAIN_SECONDS)
    except subprocess.TimeoutExpired as drain_exc:
        _kill_direct_process_if_running(proc)
        return (
            _decode_timeout_output(drain_exc.stdout) or initial_stdout,
            _decode_timeout_output(drain_exc.stderr) or initial_stderr,
        )
    return (
        _decode_timeout_output(stdout) or initial_stdout,
        _decode_timeout_output(stderr) or initial_stderr,
    )


def _git_hardened_env(argv: list[str], env: dict[str, str] | None) -> dict[str, str] | None:
    """Force ``GIT_NO_REPLACE_OBJECTS=1`` on every ``git`` child.

    ``refs/replace/*`` lives in the shared ``.git`` common dir and is
    PRODUCER-WRITABLE, so a replacement object silently substitutes itself in
    any git command that READS a commit. Reproduced, each against plain git
    first so the fixture was known-sensitive:

    - ``git merge-base --is-ancestor`` returned 0 for a non-descendant, which
      bypassed the fast-forward-only branch-advance invariant and landed the
      operator's branch on an unrelated commit.
    - ``git log -1 --pretty=%s <sha>`` returned an attacker-chosen subject, so
      an operator-facing commit summary can be a lie.
    - ``git reset --hard <sha>`` restored an attacker's tree, so a producer
      retry resumes from code nobody wrote.

    (``git rev-parse HEAD`` is NOT affected — it resolves a ref name without
    reading the object. Measured, not assumed.)

    Pinning ``--no-replace-objects`` per call site failed three dogfood rounds
    running: each round the blind panel found the next unflagged invocation,
    and two were still open when this landed. Setting it HERE makes every
    current and future git invocation safe by default — the per-call flags
    that remain are belt-and-braces, not the load-bearing defense.

    Non-git children are returned unchanged: the test/check legs share this
    helper and must keep the environment the operator configured.
    """
    if os.path.basename(argv[0]) != "git":
        return env
    # env=None means "inherit"; materialize it so the var can be added.
    return {**(os.environ if env is None else env), "GIT_NO_REPLACE_OBJECTS": "1"}


def run_subprocess(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    input_text: str | None = None,
) -> SubprocessResult:
    """Run a subprocess and return a structured :class:`SubprocessResult`.

    Args:
        argv: The argument vector. Must be a non-empty list. Passed
            directly to :func:`subprocess.Popen` with ``shell=False``.
        cwd: Working directory for the child. ``None`` inherits the
            caller's cwd.
        env: Environment for the child. ``None`` inherits the caller's
            environment. Pass an explicit dict to scope it.
        timeout: Maximum wall-clock seconds to wait. ``None`` waits
            indefinitely. On timeout the process group is SIGKILL'd
            and :class:`SubprocessTimeoutError` is raised with any
            partial output.
        input_text: If supplied, written to the child's stdin and stdin
            is then closed. ``None`` closes stdin without writing.

    Returns:
        :class:`SubprocessResult` on normal completion (regardless of
        ``returncode``).

    Raises:
        SubprocessNotFoundError: If ``argv[0]`` is not on ``PATH``.
        SubprocessTimeoutError: If the subprocess exceeded ``timeout``.
        SubprocessError: On any other launch / OS failure, or if
            ``argv`` is empty.
    """
    if not argv:
        raise SubprocessError("argv must be a non-empty list")

    # Pre-check cwd so a missing/invalid cwd surfaces as
    # SubprocessError, not SubprocessNotFoundError. Without this guard,
    # subprocess.Popen raises FileNotFoundError for BOTH "argv[0] not
    # on PATH" and "cwd doesn't exist", and we can't reliably tell
    # which one happened from the exception alone — falsely classifying
    # a missing cwd as "executable not found" sends callers down the
    # wrong remediation path (install `claude` vs. fix the worktree
    # path).
    if cwd is not None:
        if not cwd.exists():
            raise SubprocessError(f"cwd does not exist: {cwd}")
        if not cwd.is_dir():
            raise SubprocessError(f"cwd exists but is not a directory: {cwd}")

    start = time.monotonic()

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            env=_git_hardened_env(argv, env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Capture as bytes rather than text. text=True enables Python's
            # universal newline translation (\r and \r\n → \n), which converts
            # carriage-return bytes inside binary payload hunks before
            # diff_filter sees the text. A binary containing \r followed by
            # "diff --git ..." produces a fake section boundary after that
            # normalization, letting non-NUL bytes escape elision or triggering
            # a false diff_malformed refusal. Manual decode below preserves \r.
            # The two concerns that originally drove text=True are handled at
            # decode time: (1) non-UTF-8 locale → errors="replace"; (2)
            # SIGKILL-on-timeout truncates a multi-byte sequence at a buffer
            # boundary → errors="replace" still never raises.
            # New session → new process group on POSIX, so we can kill
            # the whole tree on timeout instead of orphaning grandchildren.
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        # cwd was pre-validated above, so a FileNotFoundError here is
        # genuinely "argv[0] not on PATH" — safe to classify as
        # SubprocessNotFoundError.
        raise SubprocessNotFoundError(argv[0]) from exc
    except OSError as exc:
        raise SubprocessError(f"failed to launch {argv[0]!r}: {exc}") from exc

    _register_proc(proc)
    input_bytes = input_text.encode("utf-8") if input_text is not None else None
    try:
        try:
            stdout_b, stderr_b = proc.communicate(input=input_bytes, timeout=timeout)
            stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
            stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        except subprocess.TimeoutExpired as timeout_exc:
            # Kill the whole process group so descendants die too. SIGKILL
            # is non-negotiable here — we already decided the child has
            # gone too long.
            _kill_process_group_if_running(proc)
            # Drain partial output. We pass no input here — that pipe is
            # already closed from the prior communicate call. The drain is
            # bounded: a descendant can escape the killed process group with
            # stdout/stderr still open, which would otherwise block forever.
            stdout, stderr = _drain_after_timeout(proc, timeout_exc)
            duration = time.monotonic() - start
            raise SubprocessTimeoutError(
                f"{argv[0]!r} exceeded timeout of {timeout}s (killed after {duration:.2f}s)",
                stdout=stdout or "",
                stderr=stderr or "",
                timeout=timeout if timeout is not None else 0.0,
            ) from timeout_exc
        except BaseException:
            # KeyboardInterrupt/SystemExit can arrive while communicate() is
            # waiting. The child is in its own process group, so it will not
            # receive the parent's signal unless we explicitly clean it up.
            _kill_process_group_if_running(proc)
            try:
                proc.communicate(timeout=_TIMEOUT_DRAIN_SECONDS)
            except subprocess.TimeoutExpired:
                _kill_direct_process_if_running(proc)
            raise

        duration = time.monotonic() - start
        return SubprocessResult(
            returncode=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            duration_seconds=duration,
        )
    finally:
        _unregister_proc(proc)
        # communicate() leaves the stdout/stderr pipes OPEN when it raises
        # TimeoutExpired. When the bounded drain ALSO times out (a descendant
        # escaped the killed group still holding the pipe), those fds would
        # otherwise leak until GC and surface as ResourceWarning — which
        # `pytest -W error` promotes to an error. Close them here: a single
        # chokepoint covering every exit (success, timeout, KeyboardInterrupt
        # re-raise). close() is idempotent, so the happy path (already closed
        # by communicate) is a no-op.
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
