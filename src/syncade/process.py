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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

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


def _kill_pgid(pgid: int, proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL ``pgid`` using a pgid captured at spawn time, then kill the direct child.

    Using a pre-captured pgid rather than calling ``os.getpgid(proc.pid)`` here is
    load-bearing: ``proc.poll()`` calls ``waitpid(WNOHANG)`` which REAPS the zombie,
    removing its pid from the process table.  After that, ``os.getpgid(proc.pid)``
    raises ``ProcessLookupError`` and the descendants survive — exactly the failure
    mode that left pump threads blocked past the timeout.
    """
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    _kill_direct_process_if_running(proc)


# Registry of in-flight child process groups. Reviewers dispatch in a
# ThreadPoolExecutor, so a signal landing in the MAIN thread while worker threads
# are blocked in communicate() cannot trigger their own except-BaseException
# cleanup. terminate_active_child_groups() lets the dispatcher's interrupt path
# kill those groups so each communicate() returns at once instead of the executor
# hanging in shutdown(wait=True) until the reviewer timeout.
# Maps proc → pgid captured immediately at spawn, before any poll() can reap the child.
_active_procs: dict[subprocess.Popen[bytes], int] = {}
_active_procs_lock = threading.Lock()


def _register_proc(proc: subprocess.Popen[bytes], pgid: int) -> None:
    with _active_procs_lock:
        _active_procs[proc] = pgid


def _unregister_proc(proc: subprocess.Popen[bytes]) -> None:
    with _active_procs_lock:
        _active_procs.pop(proc, None)


def terminate_active_child_groups() -> int:
    """SIGKILL every in-flight child process group; return the count killed.

    Safe to call from any thread. The reviewer dispatcher calls this on interrupt
    so a signal during parallel dispatch tears the reviewers down promptly rather
    than orphaning them (their own killpg cleanup only fires when the interrupted
    thread IS the one in communicate() — true for synth/producer, false for the
    threaded reviewer phase)."""
    with _active_procs_lock:
        items = list(_active_procs.items())
    for proc, pgid in items:
        _kill_pgid(pgid, proc)
    return len(items)


def _pump(fd: int, sink: IO[bytes]) -> None:
    """Copy one stream to disk as the child produces it, until EOF.

    ``os.read`` returns whatever has arrived rather than waiting to fill a buffer, and each
    chunk is flushed, so the bytes are on disk before the parent has finished reading them.
    That is the durability half. The EOF half is what ENDS this loop: the read returns empty
    only when every holder of the write end has closed it — the direct child and any
    descendant that inherited fd 1/2 — which is the exact guarantee ``communicate()`` gave.
    """
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return
        if not chunk:
            return
        try:
            sink.write(chunk)
            sink.flush()
        except OSError:
            return


def _write_stdin(pipe: IO[bytes], payload: bytes | None) -> None:
    """Feed the child its prompt on a thread, then close.

    On a thread because the prompt can be ~1 MB (PR-h-field-01) — larger than a pipe buffer —
    so a synchronous write would block until the child drained it, while the child may be
    blocked writing output we have not started reading. That is the classic deadlock
    ``communicate()`` uses threads to avoid, and teeing means we have to avoid it ourselves.
    """
    try:
        if payload:
            pipe.write(payload)
    except OSError:
        pass  # child exited early; its returncode is the real signal
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _join_until(threads: list[threading.Thread], deadline: float | None) -> bool:
    """Wait for the pump threads to finish (i.e. for EOF). False if the deadline passed."""
    for thread in threads:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)
        if thread.is_alive():
            return False
    return True


def _reap(proc: subprocess.Popen[bytes]) -> None:
    """Collect an already-killed child so it does not linger as a zombie.

    Bounded, and best-effort twice: a descendant that escaped the killed process group can
    keep the leader unreapable, and blocking a review on that is worse than a stray process.
    """
    for _ in range(2):
        try:
            proc.wait(timeout=_TIMEOUT_DRAIN_SECONDS)
            return
        except subprocess.TimeoutExpired:
            _kill_direct_process_if_running(proc)


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


def _stream_paths(prefix: Path) -> tuple[Path, Path]:
    """The two files a streamed child writes.

    Appends ``.stdout``/``.stderr`` to the name rather than using ``with_suffix``, which
    replaces the last suffix. A reviewer named ``team.a`` must produce ``team.a.stdout`` and
    ``team.a.stderr``; ``with_suffix`` collapses it to ``team.stdout`` — the same path
    ``team.b`` would produce, losing one reviewer's output entirely.
    """
    name = prefix.name
    return prefix.parent / (name + ".stdout"), prefix.parent / (name + ".stderr")


def _open_stream_files(prefix: Path | None) -> tuple[IO[bytes], IO[bytes]] | None:
    """Open the child's stdout/stderr sinks, or ``None`` to use pipes.

    Binary mode, so no newline translation can rewrite a lone ``\r`` (PR-h-field-01). The
    pump threads write each chunk here as it arrives and flush, so the bytes are on disk while
    the child is still running — the entire point, since a pipe alone leaves the child's output
    in THIS process's memory where a SIGKILL destroys it.

    Opened EAGERLY, before the child is spawned, for two reasons the pumps cannot provide: a
    bad path fails loudly here instead of dying silently on a worker thread, and the traversal
    guard below runs before anything is created.

    A failure to open is raised, not swallowed. Falling back to pipes would leave the caller
    believing its output is protected when it is not — and the only caller passing a prefix has
    a round directory that was written to moments earlier, so a failure here means the artifact
    directory is already broken and the run would fail at persistence anyway, after the spend.
    """
    if prefix is None:
        return None
    # Reject path traversal. The persistence layer validates reviewer names, but streaming
    # opens the files BEFORE that check runs, so a ".." component would write outside the
    # capture directory before any guard fires.
    if ".." in prefix.parts:
        raise SubprocessError(f"capture_prefix must not contain '..': {prefix}")
    out_path, err_path = _stream_paths(prefix)
    try:
        out = open(out_path, "wb")
        try:
            err = open(err_path, "wb")
        except OSError:
            out.close()
            raise
    except OSError as exc:
        raise SubprocessError(f"cannot open capture file for {prefix}: {exc}") from exc
    return out, err


def _close_stream_files(files: tuple[IO[bytes], IO[bytes]] | None) -> None:
    for handle in files or ():
        try:
            handle.close()
        except OSError:
            pass


def _read_streamed(prefix: Path) -> tuple[str, str]:
    """Read back what the child wrote, decoded exactly as the pipe path decodes it.

    Same ``errors="replace"`` contract as the pipe path (PR-h-field-01): a non-UTF-8 locale
    cannot raise, and a SIGKILL that truncates a multi-byte sequence still yields the partial
    output the timeout contract promises. Bytes on the way in, bytes on the way out — no
    newline translation anywhere, so a lone ``\\r`` inside a binary payload survives to
    ``diff_filter`` instead of being rewritten into a forged ``diff --git`` boundary.

    A missing file decodes to ``""``: the child can die before either sink is touched.
    """
    texts = []
    for path in _stream_paths(prefix):
        try:
            texts.append(path.read_bytes().decode("utf-8", errors="replace"))
        except OSError:
            texts.append("")
    return texts[0], texts[1]


def _git_hardened_env(argv: list[str], env: dict[str, str] | None) -> dict[str, str] | None:
    """Force ``GIT_NO_REPLACE_OBJECTS=1`` on every ``git`` child.

    A ``refs/replace/*`` entry silently substitutes an object in any git command
    that READS a commit. Reproduced, each against plain git first so the fixture
    was known-sensitive:

    - ``git merge-base --is-ancestor`` returned 0 for a non-descendant, which
      bypassed the fast-forward-only branch-advance invariant and landed the
      operator's branch on an unrelated commit.
    - ``git log -1 --pretty=%s <sha>`` returned an attacker-chosen subject, so
      an operator-facing commit summary can be a lie.
    - ``git reset --hard <sha>`` restored an attacker's tree, so a producer
      retry resumes from code nobody wrote.

    (``git rev-parse HEAD`` is NOT affected — it resolves a ref name without
    reading the object. Measured, not assumed.)

    The original vector was a producer writing the OPERATOR's ``refs/replace/*``
    through a shared ``.git`` common dir; PR-h-05 closed that structurally by
    giving the producer a standalone repository. This stays, and is still
    load-bearing: an actor can write replacement refs in its OWN store, and
    ``producer_import`` reads that store to validate a candidate. It ignores actor
    refs entirely and sets this variable itself, so a replacement cannot decide
    what crosses — two independent defenses over the same object read.

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
    capture_prefix: Path | None = None,
    on_spawn: Callable[[int], None] | None = None,
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
        capture_prefix: When given, the child's stdout/stderr are TEE'd
            to ``<prefix>.stdout`` / ``<prefix>.stderr`` — still read from
            pipes (that is where the EOF that means "every writer has
            closed" comes from), but written to disk chunk by chunk as
            they arrive rather than held until the child exits. So the
            output survives what happens to this process: SIGKILL, OOM, a
            closed laptop. The guarantee is "everything the pump has
            written", not "every byte the child produced" — a chunk sitting
            in the pipe when the parent dies is still lost. That window is
            one read, not a whole run. ``None`` (the
            default) keeps the pipe path: output lives in this process's
            memory until the child exits, which is right for the ~50
            sub-second ``git`` calls whose output is a SHA nobody needs
            after a crash. ``SubprocessResult`` is identical either way.
        on_spawn: Called with the child's pid the moment it EXISTS, which
            is the only moment that pid is knowable and the only place
            that knows it. Purely diagnostic — this module stays a leaf,
            so a caller that wants the pid durable supplies a callback
            rather than this module reaching for persistence. Exceptions
            from it are swallowed: a diagnostic must never fail a review.

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

    stream_files = _open_stream_files(capture_prefix)
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
        _close_stream_files(stream_files)
        raise SubprocessNotFoundError(argv[0]) from exc
    except OSError as exc:
        _close_stream_files(stream_files)
        raise SubprocessError(f"failed to launch {argv[0]!r}: {exc}") from exc

    # Capture pgid NOW, while the child is alive and in the process table.  Any later
    # proc.poll() call may reap the zombie (waitpid WNOHANG removes the pid entry), after
    # which os.getpgid(proc.pid) raises ProcessLookupError and descendants survive the kill.
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pgid = proc.pid  # best-effort fallback; spawn just succeeded so this is very unlikely
    _register_proc(proc, pgid)
    if on_spawn is not None:
        # Swallowing is deliberate and narrow: this is a post-mortem breadcrumb, and losing it
        # is strictly better than failing a review that is otherwise about to succeed.
        try:
            on_spawn(proc.pid)
        except Exception:  # noqa: BLE001 - a diagnostic must never fail a run
            pass
    input_bytes = input_text.encode("utf-8") if input_text is not None else None
    deadline = (start + timeout) if timeout is not None else None
    pumps: list[threading.Thread] = []
    try:
        try:
            if capture_prefix is not None:
                # TEE rather than redirect. An earlier cut pointed Popen straight at the files,
                # which put the bytes on disk but DESTROYED the completion signal: a file has
                # no EOF, so the parent returned when the direct child exited while a
                # descendant holding fd 1/2 was still writing. Three proxies for that signal
                # were tried and rejected in review. Keeping the pipes keeps the signal itself
                # — EOF still means "every writer has closed", descendants included, because
                # they inherit fd 1/2 like any other child. (A separate sentinel fd cannot
                # substitute: Python closes fds above 2 in the grandchild, measured.)
                assert stream_files is not None
                for pipe, sink in ((proc.stdout, stream_files[0]), (proc.stderr, stream_files[1])):
                    if pipe is None:
                        continue
                    pump = threading.Thread(target=_pump, args=(pipe.fileno(), sink), daemon=True)
                    pump.start()
                    pumps.append(pump)
                if proc.stdin is not None:
                    feeder = threading.Thread(
                        target=_write_stdin, args=(proc.stdin, input_bytes), daemon=True
                    )
                    feeder.start()
                    pumps.append(feeder)
                if not _join_until(pumps, deadline):
                    raise subprocess.TimeoutExpired(argv, timeout or 0.0)
                # EOF is not exit. A child may legitimately close fd 1/2 and keep working —
                # and it is entitled to the REST of its budget to do so, exactly as the pipe
                # path allowed (communicate() waits for EOF *and* exit under one timeout).
                # Waiting a fixed drain here instead killed such a child at 0.27s of a 5s
                # budget and reported a SubprocessTimeoutError over a run that exited 7
                # cleanly — a false timeout that discards the real return code.
                left = None if deadline is None else max(0.0, deadline - time.monotonic())
                proc.wait(timeout=left)
                stdout, stderr = _read_streamed(capture_prefix)
            else:
                stdout_b, stderr_b = proc.communicate(input=input_bytes, timeout=timeout)
                stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
                stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        except subprocess.TimeoutExpired as timeout_exc:
            # Kill the whole process group so descendants die too. SIGKILL
            # is non-negotiable here — we already decided the child has
            # gone too long.
            _kill_pgid(pgid, proc)
            if capture_prefix is not None:
                # Everything the child emitted is already on disk; the pumps put it there as
                # it arrived. The SIGKILL above closes the write ends, so they reach EOF and
                # exit on their own — joined briefly so the files are closed before we read.
                #
                # The killed child must still be REAPED: the pipe path gets that for free
                # because its drain calls communicate() again, and skipping it leaves a
                # zombie whose Popen.__del__ raises a ResourceWarning that `pytest -W error`
                # promotes to a failure (which is how this was found).
                _join_until(pumps, time.monotonic() + _TIMEOUT_DRAIN_SECONDS)
                _reap(proc)
                stdout, stderr = _read_streamed(capture_prefix)
            else:
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
            _kill_pgid(pgid, proc)
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
        _close_stream_files(stream_files)
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
