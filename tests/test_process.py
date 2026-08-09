"""Tests for :mod:`syncade.process`.

Uses real subprocess calls against system binaries (``echo``, ``false``,
``cat``, ``sleep``, ``sh``, ``pwd``) — no mocking. Same discipline as
:mod:`tests.test_worktree`: we want to know the integration works, not
that our wrapper calls ``subprocess.run`` with the right arguments.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from syncade.process import (
    SubprocessError,
    SubprocessNotFoundError,
    SubprocessResult,
    SubprocessTimeoutError,
    run_subprocess,
)

# Skip the whole module on platforms missing the trivial system binaries
# we use — keeps Windows CI from melting if anyone tries to run pytest
# there before we explicitly support it.
pytestmark = pytest.mark.skipif(
    shutil.which("echo") is None or shutil.which("sleep") is None,
    reason="basic POSIX utilities not on PATH",
)


class TestStdoutStderrCapture:
    def test_echo_captures_stdout(self):
        result = run_subprocess(["echo", "hello world"])
        assert isinstance(result, SubprocessResult)
        assert result.returncode == 0
        assert result.stdout.strip() == "hello world"
        assert result.stderr == ""

    def test_false_returns_nonzero_without_raising(self):
        """`false` exits 1. run_subprocess does NOT raise on non-zero —
        the returncode is the caller's concern."""
        result = run_subprocess(["false"])
        assert result.returncode != 0  # macOS: 1, some shells: 1
        assert result.stdout == ""

    def test_stderr_captured_separately_from_stdout(self):
        # sh -c "echo OUT; echo ERR >&2" — stdout and stderr each get one line.
        result = run_subprocess(["sh", "-c", "echo OUT; echo ERR >&2"])
        assert result.returncode == 0
        assert "OUT" in result.stdout
        assert "ERR" in result.stderr
        # The two streams must NOT be interleaved
        assert "ERR" not in result.stdout
        assert "OUT" not in result.stderr


class TestStdinAndEnv:
    def test_input_text_piped_to_stdin(self):
        # `cat` echoes whatever's on stdin to stdout
        result = run_subprocess(["cat"], input_text="piped input\n")
        assert result.returncode == 0
        assert result.stdout == "piped input\n"

    def test_input_text_none_closes_stdin(self):
        # `cat` with closed stdin → reads EOF immediately, exits 0 with empty stdout
        result = run_subprocess(["cat"], input_text=None)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_env_vars_passed_through(self):
        custom_env = dict(os.environ)
        custom_env["SYNCADE_TEST_MARKER"] = "yes-it-flows-through"
        result = run_subprocess(
            ["sh", "-c", "echo $SYNCADE_TEST_MARKER"],
            env=custom_env,
        )
        assert "yes-it-flows-through" in result.stdout

    def test_env_none_inherits_parents_env(self):
        # PATH must exist in the parent's env for `sh` to be findable;
        # passing env=None inherits it.
        result = run_subprocess(["sh", "-c", "echo $PATH"])
        assert result.returncode == 0
        assert result.stdout.strip() != ""


class TestCwd:
    def test_cwd_is_respected(self, tmp_path: Path):
        result = run_subprocess(["pwd"], cwd=tmp_path)
        assert result.returncode == 0
        # /tmp paths can be symlinked (e.g. /tmp -> /private/tmp on macOS),
        # so resolve both before comparing.
        assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()

    def test_cwd_none_uses_caller_cwd(self):
        result = run_subprocess(["pwd"])
        assert result.returncode == 0
        # We don't assert the exact path — just that pwd produced
        # something. The point is no exception from a None cwd.
        assert result.stdout.strip() != ""


class TestTimeout:
    def test_timeout_kills_subprocess_and_raises(self):
        # `sleep 30` would naturally take 30s. With timeout=0.5 we
        # expect the kill to fire and SubprocessTimeoutError to raise
        # with duration close to the timeout, not the natural runtime.
        start = time.monotonic()
        with pytest.raises(SubprocessTimeoutError) as exc_info:
            run_subprocess(["sleep", "30"], timeout=0.5)
        elapsed = time.monotonic() - start
        # The subprocess was genuinely killed — total elapsed should be
        # within a few hundred ms of the timeout, not 30 seconds.
        assert elapsed < 5.0, f"timeout took {elapsed}s; child probably wasn't killed"
        assert exc_info.value.timeout == 0.5

    def test_timeout_captures_partial_stdout(self):
        # Print something, then sleep. The timeout fires during sleep,
        # but the print should have flushed first.
        script = "echo partial; sleep 30"
        with pytest.raises(SubprocessTimeoutError) as exc_info:
            run_subprocess(["sh", "-c", script], timeout=1.0)
        # "partial" should be in the captured stdout — exact whitespace
        # depends on buffering, but the substring must be present.
        assert "partial" in exc_info.value.stdout

    def test_timeout_message_names_binary_and_timeout(self):
        with pytest.raises(SubprocessTimeoutError) as exc_info:
            run_subprocess(["sleep", "5"], timeout=0.2)
        msg = str(exc_info.value)
        assert "sleep" in msg
        assert "0.2" in msg

    def test_timeout_bounded_drain_when_escaped_descendant_keeps_pipe_open(self):
        script = (
            "echo partial; "
            f"{sys.executable} -c 'import os, time; os.setsid(); time.sleep(2.0)' & "
            "sleep 30"
        )
        start = time.monotonic()
        with pytest.raises(SubprocessTimeoutError) as exc_info:
            run_subprocess(["sh", "-c", script], timeout=0.2)
        elapsed = time.monotonic() - start

        assert elapsed < 1.5, (
            f"timeout drain took {elapsed}s; escaped pipe holder blocked communicate()"
        )
        assert "partial" in exc_info.value.stdout


class TestInterruptCleanup:
    def test_keyboard_interrupt_kills_child_process_group(self, tmp_path: Path):
        ready_file = tmp_path / "child-ready.txt"
        sleep_script = (
            "import os, pathlib, time\n"
            f"pathlib.Path({str(ready_file)!r}).write_text("
            "f'{os.getpid()} {os.getpgrp()}\\n')\n"
            "time.sleep(30)\n"
        )
        wrapper_script = (
            "from syncade.process import run_subprocess\n"
            "import sys\n"
            f"run_subprocess([sys.executable, '-c', {sleep_script!r}], timeout=60)\n"
        )

        proc = subprocess.Popen(
            [sys.executable, "-c", wrapper_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        child_pid: int | None = None
        child_pgid: int | None = None
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if ready_file.exists():
                    parts = ready_file.read_text().split()
                    if len(parts) == 2:
                        child_pid = int(parts[0])
                        child_pgid = int(parts[1])
                        if _pid_is_running(child_pid):
                            break
                if proc.poll() is not None:
                    break
                time.sleep(0.05)

            setup_output = ""
            if child_pid is None and proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=0.1)
                setup_output = f"\nwrapper stdout:\n{stdout}\nwrapper stderr:\n{stderr}"
            assert (
                child_pid is not None and child_pgid is not None and _pid_is_running(child_pid)
            ), "test setup failed: child process never reported ready" + setup_output

            os.kill(proc.pid, signal.SIGINT)
            proc.wait(timeout=5.0)

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if not _pid_is_running(child_pid):
                    break
                time.sleep(0.05)

            assert not _pid_is_running(child_pid), (
                "run_subprocess left the child process alive after KeyboardInterrupt"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
            if child_pid is not None and child_pgid is not None and _pid_is_running(child_pid):
                try:
                    os.killpg(child_pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            # The happy path never calls communicate(), so the stdout/stderr
            # PIPE fds are otherwise left open until GC and leak as
            # ResourceWarning — which `pytest -W error -q` (the configured test
            # leg) promotes to an error. Close them explicitly; close() is
            # idempotent, so the setup-failure branch's communicate() is fine.
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestErrorClasses:
    def test_missing_binary_raises_subprocess_not_found(self):
        with pytest.raises(SubprocessNotFoundError) as exc_info:
            run_subprocess(["definitely-not-a-real-binary-syncade-test"])
        assert exc_info.value.binary == "definitely-not-a-real-binary-syncade-test"
        # Subclass relationship is part of the API contract — callers
        # may catch SubprocessError and still see this.
        assert isinstance(exc_info.value, SubprocessError)

    def test_missing_cwd_raises_subprocess_error_not_not_found(self, tmp_path: Path):
        """A missing cwd must NOT be misclassified as a missing binary.
        subprocess.Popen raises FileNotFoundError for both shapes;
        without the pre-check, callers would be told "executable not
        found" and try to install the binary when the real issue is a
        wrong cwd path."""
        missing = tmp_path / "definitely-missing-cwd"
        assert not missing.exists()
        with pytest.raises(SubprocessError) as exc_info:
            run_subprocess(["echo", "hi"], cwd=missing)
        # Must be the base SubprocessError, not the not-found subtype.
        assert not isinstance(exc_info.value, SubprocessNotFoundError)
        assert "cwd does not exist" in str(exc_info.value)
        assert str(missing) in str(exc_info.value)

    def test_cwd_pointing_at_a_file_raises_subprocess_error(self, tmp_path: Path):
        """cwd points at a regular file (not a directory) — same
        classification as missing cwd."""
        file_path = tmp_path / "not-a-dir.txt"
        file_path.write_text("hi\n")
        with pytest.raises(SubprocessError) as exc_info:
            run_subprocess(["echo", "hi"], cwd=file_path)
        assert not isinstance(exc_info.value, SubprocessNotFoundError)
        assert "not a directory" in str(exc_info.value)

    def test_empty_argv_raises_subprocess_error(self):
        with pytest.raises(SubprocessError):
            run_subprocess([])

    def test_timeout_error_is_subprocess_error_subclass(self):
        # Documented invariant — any catch site that handles
        # SubprocessError will also catch the timeout variant.
        assert issubclass(SubprocessTimeoutError, SubprocessError)
        assert issubclass(SubprocessNotFoundError, SubprocessError)


class TestCarriageReturnPreservation:
    """run_subprocess must NOT translate \\r bytes to \\n (universal newline
    normalization). text=True with the default newline=None converts standalone
    \\r and \\r\\n to \\n before diff_filter sees the text, which lets a binary
    payload containing \\r followed by 'diff --git ...' synthesize a fake section
    boundary — either leaking non-NUL binary bytes past elision or triggering a
    false diff_malformed refusal."""

    def test_cr_bytes_are_preserved_not_normalized(self):
        # Write a CR byte followed by text that looks like a diff header.
        # With text=True + universal newlines, the \r becomes \n, turning
        # "before\rdiff --git a/x b/x\n" into "before\ndiff --git a/x b/x\n",
        # which split('\n') would then split into a fake diff section.
        script = (
            "import sys; "
            "sys.stdout.buffer.write(b'before\\rdiff --git a/x b/x\\n'); "
            "sys.stdout.buffer.flush()"
        )
        result = run_subprocess([sys.executable, "-c", script])
        assert result.returncode == 0
        assert "\r" in result.stdout, (
            f"\\r was normalized away — universal newline translation is still active: "
            f"{result.stdout!r}"
        )
        # The CR must appear between 'before' and 'diff --git', not as a standalone \n.
        assert result.stdout == "before\rdiff --git a/x b/x\n", result.stdout


class TestInvalidUtf8Decoding:
    """Child output is decoded UTF-8/replace, never the parent locale +
    errors='strict'. Raw invalid bytes must NOT raise UnicodeDecodeError
    out of run_subprocess — they come back as replacement characters."""

    def test_invalid_utf8_stdout_does_not_raise(self):
        # Emit a lone 0xff (never valid UTF-8) so a strict decode would
        # raise. errors="replace" turns it into U+FFFD instead.
        script = r'import sys; sys.stdout.buffer.write(b"\xff\xfeok"); sys.stdout.buffer.flush()'
        result = run_subprocess([sys.executable, "-c", script])
        assert result.returncode == 0
        # Decoded without raising; the invalid bytes became the
        # replacement char and the ASCII tail survived.
        assert "�" in result.stdout
        assert "ok" in result.stdout

    def test_invalid_utf8_on_timeout_preserves_partial_output(self):
        # Write invalid bytes, flush, then sleep past the timeout. The
        # SIGKILL-drain communicate() must return the partial bytes
        # decoded with replacement, NOT raise UnicodeDecodeError — that
        # would break SubprocessTimeoutError's partial-output guarantee.
        script = (
            r"import sys, time; "
            r'sys.stdout.buffer.write(b"partial\xff"); sys.stdout.buffer.flush(); '
            r"time.sleep(30)"
        )
        with pytest.raises(SubprocessTimeoutError) as exc_info:
            run_subprocess([sys.executable, "-c", script], timeout=1.0)
        assert "partial" in exc_info.value.stdout
        assert "�" in exc_info.value.stdout


class TestDurationMeasurement:
    def test_duration_is_close_to_natural_runtime(self):
        # sleep 0.3 should take ~0.3s. Duration_seconds should reflect that.
        result = run_subprocess(["sleep", "0.3"])
        assert result.returncode == 0
        assert 0.25 < result.duration_seconds < 1.5


def _replace_poisoned_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo where ``refs/replace/<real>`` points at an unrelated commit.

    Returns ``(repo, real_sha, evil_sha)``. The real commit's subject is
    ``REAL subject`` and its tree holds ``keep.txt``; the replacement's are
    ``LIE subject`` / ``evil.txt``.
    """

    def g(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True
        ).stdout.strip()

    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (tmp_path / "keep.txt").write_text("real content\n")
    g("add", "-A")
    g("commit", "-qm", "REAL subject")
    real = g("rev-parse", "HEAD")
    g("checkout", "-q", "--orphan", "evil")
    g("rm", "-rqf", ".")
    (tmp_path / "evil.txt").write_text("attacker content\n")
    g("add", "-A")
    g("commit", "-qm", "LIE subject")
    evil = g("rev-parse", "HEAD")
    g("checkout", "-q", "main")
    g("replace", "-f", real, evil)
    return tmp_path, real, evil


class TestGitReplaceObjectsHardening:
    """``refs/replace/*`` is producer-writable and substitutes objects on read.

    Every case CALIBRATES first — it asserts plain ``git`` really is fooled by
    the fixture before asserting that :func:`run_subprocess` is not. A fixture
    that cannot fool raw git would make these pass for the wrong reason.
    """

    def test_commit_subject_is_not_substituted(self, tmp_path):
        repo, real, _ = _replace_poisoned_repo(tmp_path)
        argv = ["git", "log", "-1", "--pretty=format:%s", real]

        # Calibration: plain git IS poisoned here.  Run without the var this
        # module's own run_subprocess injects so the calibration reflects raw git
        # behavior, not the hardened behavior we are testing.
        _raw_env = {k: v for k, v in os.environ.items() if k != "GIT_NO_REPLACE_OBJECTS"}
        raw = subprocess.run(argv, cwd=repo, capture_output=True, text=True, env=_raw_env)
        assert raw.stdout.strip() == "LIE subject", "fixture does not fool raw git"

        assert run_subprocess(argv, cwd=repo).stdout.strip() == "REAL subject"

    def test_reset_hard_restores_the_real_tree(self, tmp_path):
        repo, real, _ = _replace_poisoned_repo(tmp_path)
        (repo / "keep.txt").write_text("uncommitted junk\n")

        run_subprocess(["git", "reset", "--hard", real], cwd=repo)

        # The producer-retry reset (PR-v2-22) must resume from OUR starting
        # state, never from a tree the replacement supplied.
        assert (repo / "keep.txt").read_text() == "real content\n"
        assert not (repo / "evil.txt").exists()

    def test_ancestry_check_sees_the_true_graph(self, tmp_path):
        """The fast-forward-only branch-advance invariant rests on this.

        Needs the INVERSE replacement direction from the other cases: the
        unrelated commit is replaced BY a real descendant, so the ancestry
        check passes on the substitute while ``update-ref`` would move the
        branch to the literal (unrelated) SHA.
        """

        def g(*args: str) -> str:
            return subprocess.run(
                ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True
            ).stdout.strip()

        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        (tmp_path / "a.txt").write_text("base\n")
        g("add", "-A")
        g("commit", "-qm", "start")
        start = g("rev-parse", "HEAD")
        (tmp_path / "a.txt").write_text("next\n")
        g("add", "-A")
        g("commit", "-qm", "descendant")
        descendant = g("rev-parse", "HEAD")
        g("checkout", "-q", "--orphan", "evil")
        g("rm", "-rqf", ".")
        (tmp_path / "evil.txt").write_text("unrelated\n")
        g("add", "-A")
        g("commit", "-qm", "unrelated")
        unrelated = g("rev-parse", "HEAD")
        g("checkout", "-q", "main")
        g("replace", "-f", unrelated, descendant)

        argv = ["git", "merge-base", "--is-ancestor", start, unrelated]
        _raw_env = {k: v for k, v in os.environ.items() if k != "GIT_NO_REPLACE_OBJECTS"}
        result = subprocess.run(argv, cwd=tmp_path, env=_raw_env)
        assert result.returncode == 0, "fixture does not fool raw git"

        assert run_subprocess(argv, cwd=tmp_path).returncode == 1

    def test_non_git_children_keep_their_environment(self):
        """The test/check legs share this helper — do not touch their env.

        The assertion checks that the parent value is PRESERVED, not that the
        variable is absent.  When syncade's own worktree env sets
        ``GIT_NO_REPLACE_OBJECTS=1``, a non-git child must still see that value.
        """
        script = r"import os; print(os.environ.get('GIT_NO_REPLACE_OBJECTS', 'unset'))"
        expected = os.environ.get("GIT_NO_REPLACE_OBJECTS", "unset")
        assert run_subprocess([sys.executable, "-c", script]).stdout.strip() == expected

    def test_explicit_env_is_augmented_not_replaced(self):
        env = {"PATH": os.environ.get("PATH", ""), "MARKER": "kept"}
        result = run_subprocess(["git", "--version"], env=env, timeout=30)
        assert result.returncode == 0
        # The caller's dict survives; we only add our key to a copy of it.
        assert env == {"PATH": os.environ.get("PATH", ""), "MARKER": "kept"}
