"""Child output survives the parent's death — PR-h-field-03 item 1.

`run_subprocess` used to collect every child's stdout/stderr through pipes and write them to the
round directory only after the child exited, which meant the bytes lived in SYNCADE's memory for
the whole run. So anything that killed syncade destroyed what its reviewers had already said —
measured during the PR-h-field-02 investigation as three field runs whose `round-2/` was
completely empty while rounds 0 and 1 held their full fifteen-file sets.

The acceptance the brief demanded is asserted the only way it can honestly be asserted: by
SIGKILLing a REAL parent process mid-subprocess and looking at the disk afterwards. A mock
cannot fail this test, because the thing under test is what the operating system does with a
file descriptor when a process disappears.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from syncade.process import (
    SubprocessError,
    SubprocessTimeoutError,
    _stream_paths,
    run_subprocess,
)

# The parent under test: it starts a child that emits a line, flushes, then sleeps forever.
# We SIGKILL this parent while the child is mid-run, then inspect the capture files.
_PARENT = """
import sys
from pathlib import Path
from syncade.process import run_subprocess

prefix = Path(sys.argv[1])
run_subprocess(
    [sys.executable, "-c",
     "import sys,time; sys.stdout.write('PROVIDER SAID: usage limit reached\\\\n');"
     " sys.stdout.flush(); time.sleep(300)"],
    capture_prefix=prefix,
)
"""


def _kill_parent_mid_subprocess(tmp_path: Path, prefix: Path) -> None:
    script = tmp_path / "parent.py"
    script.write_text(textwrap.dedent(_PARENT))
    parent = subprocess.Popen(
        [sys.executable, str(script), str(prefix)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Captured BEFORE the kill. `start_new_session=True` makes the parent its own group leader,
    # so its pgid is its pid — but reading it back afterwards with os.getpgid() raises
    # ProcessLookupError once the parent has been reaped, which silently skipped the cleanup
    # below and leaked a 300s sleeper per run. Found by the blind panel as a minor finding.
    pgid = os.getpgid(parent.pid)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            out, _ = _stream_paths(prefix)
            if out.exists() and out.read_bytes():
                break
            time.sleep(0.05)
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=10)
    finally:
        # The grandchild survives its parent by design — that is what this test is about — so
        # the whole group has to go, addressed by the pgid captured while the parent was alive.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


@pytest.mark.skipif(os.name != "posix", reason="uses SIGKILL / process groups")
def test_a_sigkilled_parent_leaves_the_childs_output_on_disk(tmp_path):
    """THE acceptance criterion. Before this change the answer was zero files."""
    prefix = tmp_path / "codex-reviewer"
    _kill_parent_mid_subprocess(tmp_path, prefix)

    survived, _ = _stream_paths(prefix)
    assert survived.is_file(), (
        "the parent died and took the child's output with it — this is the exact loss "
        "PR-h-field-02 could not diagnose"
    )
    assert "PROVIDER SAID: usage limit reached" in survived.read_text(encoding="utf-8")


def test_the_result_is_identical_to_the_pipe_path(tmp_path):
    """197 call sites read `.stdout`. Streaming changes where bytes travel, not the contract."""
    argv = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(3)",
    ]
    piped = run_subprocess(argv)
    streamed = run_subprocess(argv, capture_prefix=tmp_path / "r")

    assert (piped.stdout, piped.stderr, piped.returncode) == ("out", "err", 3)
    assert (streamed.stdout, streamed.stderr, streamed.returncode) == ("out", "err", 3)


def test_stdin_still_reaches_a_streamed_child(tmp_path):
    """The prompt travels on stdin (PR-h-field-01) and a reviewer diff can be ~1 MB.

    Teeing means we feed stdin ourselves instead of letting `communicate()` do it, and a
    synchronous write would deadlock against a child blocked writing output. Asserted with a
    payload larger than a pipe buffer, which is the only size that can expose that.
    """
    payload = "x" * 200_000
    result = run_subprocess(
        [sys.executable, "-c", "import sys; sys.stdout.write(str(len(sys.stdin.read())))"],
        input_text=payload,
        capture_prefix=tmp_path / "r",
    )
    assert result.stdout == str(len(payload))


def test_bytes_are_not_translated_on_the_way_through(tmp_path):
    """The PR-h-field-01 guarantee, re-asserted for the new path.

    `text=True` rewrites a lone \\r to \\n, which let a committed binary payload forge a
    `diff --git` boundary and smuggle content past diff_filter's binary detection. Files must
    be opened in BINARY mode for the same reason pipes are read as bytes. A NUL is included
    because it is git's own binary heuristic and the one signal that cannot be forged.
    """
    argv = [
        sys.executable,
        "-c",
        r"import sys; sys.stdout.buffer.write(b'a\rb\r\nc\x00d')",
    ]
    streamed = run_subprocess(argv, capture_prefix=tmp_path / "r")
    assert streamed.stdout == "a\rb\r\nc\x00d"
    assert streamed.stdout == run_subprocess(argv).stdout, "streamed and piped must agree"


def test_a_timeout_keeps_what_the_child_had_already_emitted(tmp_path):
    """The drain path the brief flagged: with files there is no exception payload to read.

    Partial output is already on disk, put there by the pumps as it arrived. The timeout
    contract — callers can surface whatever the reviewer produced before the SIGKILL — must
    survive that rewrite.
    """
    with pytest.raises(SubprocessTimeoutError) as excinfo:
        run_subprocess(
            [
                sys.executable,
                "-c",
                "import sys,time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(300)",
            ],
            timeout=1.0,
            capture_prefix=tmp_path / "r",
        )
    assert excinfo.value.stdout == "partial", "partial output was lost on the streamed path"


def test_an_unopenable_capture_path_fails_loudly(tmp_path):
    """Silently falling back to pipes would leave the caller believing output is protected."""
    with pytest.raises(SubprocessError, match="cannot open capture file"):
        run_subprocess([sys.executable, "-c", "pass"], capture_prefix=tmp_path / "nope" / "r")


def test_a_missing_binary_still_raises_not_found_and_leaves_no_handles(tmp_path):
    """The launch-failure paths close the capture files they opened.

    `pytest -W error` promotes an unclosed-file ResourceWarning to an error, so a leak here
    fails the suite rather than lurking — which is how the last fd leak in this module was
    caught.
    """
    from syncade.process import SubprocessNotFoundError

    with pytest.raises(SubprocessNotFoundError):
        run_subprocess(["definitely-not-a-real-binary-xyz"], capture_prefix=tmp_path / "r")


def test_a_retry_replaces_the_previous_attempt_rather_than_appending(tmp_path):
    """Reviewers retry on transient errors, and every attempt streams to the SAME two paths.

    Opening "wb" truncates, so attempt 2 replaces attempt 1. Appending would be worse than a
    lost file: it would hand the parser two concatenated JSON documents and turn a recovered
    transient into a parse failure.
    """
    prefix = tmp_path / "rv1"
    run_subprocess(
        [sys.executable, "-c", "import sys; sys.stdout.write('A' * 50)"], capture_prefix=prefix
    )
    second = run_subprocess(
        [sys.executable, "-c", "import sys; sys.stdout.write('B')"], capture_prefix=prefix
    )
    assert second.stdout == "B"
    assert _stream_paths(prefix)[0].read_text(encoding="utf-8") == "B", (
        "the retry appended to the previous attempt instead of replacing it"
    )


def test_a_raising_on_spawn_callback_cannot_fail_the_run(tmp_path):
    """The pid breadcrumb is diagnostic, so its failure must be survivable.

    Separate from record_child_pid's own internal guard: this pins the choke point's contract
    for ANY caller's callback, including one added later that forgets to be careful.
    """

    def explode(pid: int) -> None:
        raise RuntimeError("callback blew up")

    result = run_subprocess(
        [sys.executable, "-c", "import sys; sys.stdout.write('ok')"], on_spawn=explode
    )
    assert result.stdout == "ok"


def test_on_spawn_receives_the_real_child_pid(tmp_path):
    seen: list[int] = []
    result = run_subprocess(
        [sys.executable, "-c", "import os,sys; sys.stdout.write(str(os.getpid()))"],
        on_spawn=seen.append,
    )
    assert seen == [int(result.stdout)]


def test_dotted_reviewer_names_produce_distinct_artifact_paths(tmp_path):
    """team.a and team.b must not both collapse to team.stdout via with_suffix."""
    r_a = run_subprocess(
        [sys.executable, "-c", "import sys; sys.stdout.write('from-a')"],
        capture_prefix=tmp_path / "team.a",
    )
    r_b = run_subprocess(
        [sys.executable, "-c", "import sys; sys.stdout.write('from-b')"],
        capture_prefix=tmp_path / "team.b",
    )

    # Each produces a distinct file; with_suffix would have made both write team.stdout.
    assert (tmp_path / "team.a.stdout").read_text(encoding="utf-8") == "from-a"
    assert (tmp_path / "team.b.stdout").read_text(encoding="utf-8") == "from-b"
    # SubprocessResult still returns the right content.
    assert r_a.stdout == "from-a"
    assert r_b.stdout == "from-b"


def test_capture_prefix_with_traversal_is_rejected(tmp_path):
    """A '..' component in capture_prefix must be refused before any file is opened."""
    with pytest.raises(SubprocessError, match="must not contain"):
        run_subprocess(
            [sys.executable, "-c", "pass"],
            capture_prefix=tmp_path / ".." / "escape",
        )


@pytest.mark.skipif(os.name != "posix", reason="uses process groups / os.killpg")
def test_descendant_that_outlives_drain_window_is_waited_not_killed(tmp_path):
    """A descendant writing after _TIMEOUT_DRAIN_SECONDS but within the timeout is not killed.

    The old _wait_for_pgroup waited only 0.25s then SIGKILLed remaining group members.
    A reviewer shell wrapper that spawns a helper which emits after 0.5s (a normal latency,
    well within a 30s timeout budget) would return empty stdout. The fix waits for the
    remaining timeout budget rather than the short drain window.
    """
    # Direct child forks a grandchild that sleeps 0.5s (> _TIMEOUT_DRAIN_SECONDS = 0.25s),
    # then writes. Before the fix, the grandchild was killed at 0.25s and stdout was empty.
    child_script = textwrap.dedent("""
        import subprocess, sys
        subprocess.Popen(
            [sys.executable, "-c",
             "import sys,time; time.sleep(0.5); sys.stdout.write('REVIEWER-LATE');"
             " sys.stdout.flush()"],
        )
        sys.exit(0)
    """)
    result = run_subprocess(
        [sys.executable, "-c", child_script.strip()],
        capture_prefix=tmp_path / "rv",
        timeout=10.0,
    )
    assert result.stdout == "REVIEWER-LATE", (
        "grandchild killed within the drain window instead of being waited for — "
        "a reviewer helper that emits after 0.5s would lose its output"
    )


@pytest.mark.skipif(os.name != "posix", reason="uses process groups / os.killpg")
def test_descendant_output_captured_before_read(tmp_path):
    """A grandchild that outlives the direct child must not truncate stdout.

    With pipes, communicate() waits for EOF — which requires EVERY holder of the write
    end (including descendants) to close it. With file-backed streams, communicate()
    returns on the direct child's exit. _wait_for_pgroup restores the same guarantee.
    Without it, _read_streamed fires while the grandchild is still writing and returns
    empty; persist_reviewer_result then atomically replaces the file with that empty
    content, discarding exactly the output this PR exists to protect.
    """
    # Direct child immediately spawns a grandchild (inheriting stdout) then exits.
    # Grandchild sleeps briefly and writes 'LATE', well within _TIMEOUT_DRAIN_SECONDS.
    child_script = textwrap.dedent("""
        import subprocess, sys, time
        subprocess.Popen(
            [sys.executable, "-c",
             "import sys,time; time.sleep(0.05); sys.stdout.write('LATE'); sys.stdout.flush()"],
        )
        sys.exit(0)
    """)
    result = run_subprocess(
        [sys.executable, "-c", child_script.strip()],
        capture_prefix=tmp_path / "rv",
    )
    assert result.stdout == "LATE", (
        "grandchild output was lost: _read_streamed fired before the descendant finished writing"
    )


def test_a_descendant_that_leaves_the_process_group_is_still_waited_for(tmp_path):
    """The blocker two dogfood runs kept re-raising, and the reason the design changed.

    The pipe path never returned until EOF, and EOF means every holder of the write end has
    closed it — including a grandchild that inherited fd 1/2. Pointing Popen at files destroyed
    that signal: a file has no EOF, so the parent returned the moment the DIRECT child exited
    and later output was lost. Three substitutes were tried and rejected in review (a 0.25s
    drain, the full timeout as a drain, polling the process group for liveness).

    This is the case that kills all three, and the last one specifically: the direct child exits
    immediately while a grandchild calls setsid — leaving the process group being polled — and
    writes 1.5s later. Only real EOF covers it.

    (A separate inheritable sentinel fd cannot substitute either: measured, Python closes fds
    above 2 in the grandchild, so it never receives one. The signal has to live on fd 1/2.)
    """
    child = (
        "import os,sys,subprocess;"
        "subprocess.Popen([sys.executable,'-c',"
        "\"import sys,time; time.sleep(1.5); sys.stdout.write('LATE-DESCENDANT')\"],"
        " start_new_session=True);"
        "sys.stdout.write('EARLY-'); sys.stdout.flush()"
    )
    result = run_subprocess(
        [sys.executable, "-c", child], capture_prefix=tmp_path / "r", timeout=30
    )
    assert result.stdout == "EARLY-LATE-DESCENDANT", (
        f"returned before the descendant closed the stream; got {result.stdout!r}"
    )


def test_a_child_that_closes_its_streams_early_is_not_a_timeout(tmp_path):
    """EOF is not exit — found by the blind panel, with this reproduction.

    A child may legitimately close fd 1/2 and keep working. On the teed path the pumps then
    reach EOF immediately, and an earlier cut gave the process only a fixed 0.25s drain to
    exit after that. Measured, it killed a child 0.27s into a 5s budget and raised
    SubprocessTimeoutError over a run that went on to exit 7 cleanly — a false timeout that
    discards the real return code and any work still in flight.

    The pipe path never did this: communicate() covers EOF *and* exit under one timeout. So
    the streamed path must give the child the REST of its budget, and this asserts the two
    agree rather than asserting a hardcoded duration.
    """
    argv = [
        sys.executable,
        "-c",
        "import os,sys,time; os.close(1); os.close(2); time.sleep(1); sys.exit(7)",
    ]
    piped = run_subprocess(argv, timeout=5)
    streamed = run_subprocess(argv, timeout=5, capture_prefix=tmp_path / "r")

    assert piped.returncode == 7
    assert streamed.returncode == 7, "a child that closed its streams early was killed as a timeout"
    assert (streamed.stdout, streamed.stderr) == (piped.stdout, piped.stderr)
