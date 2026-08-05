"""Drive the real ``syncade --config`` curses TUI in a pseudo-terminal (pr-v2-30 Issue 3).

The "as a user" proof: launch the actual CLI in a pty, arrow down to Rounds, edit it, save, quit —
then assert the value the user typed is what landed in the persisted global config. HOME points at a
temp dir so the global file is temp/.syncade/config.toml, never the developer's real one.
"""

from __future__ import annotations

import os
import select
import sys
import time
import tomllib

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")), reason="pty/curses drive is POSIX-only"
)


def _drain(master: int, timeout: float) -> bytes:
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.1)
        if not r:
            if buf:
                break
            continue
        try:
            chunk = os.read(master, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def _render_screen(stream: bytes) -> str:
    """Reconstruct the final visible terminal grid from a captured ANSI byte stream.

    curses emits DIFFERENTIAL updates (it rewrites only changed cells), so a value like ``3000.0``
    may never appear contiguously in the raw bytes. Replaying cursor moves + writes into a grid
    recovers what the user actually sees. Handles the subset curses uses here (CUP/VPA/CHA, cursor
    moves, ED/EL erase, CR/LF/BS); other CSI sequences (SGR, mode sets) are ignored."""
    import re

    grid: dict[tuple[int, int], str] = {}
    row = col = 1
    text = stream.decode("utf-8", "replace")
    # CSI: ESC [ , optional private '?', numeric params, final letter. `priv` sequences (mode sets
    # like ?25l show/hide cursor) carry no grid effect — matched so they're consumed, not leaked.
    csi = re.compile(r"\x1b\[(\??)(\d*)(?:;(\d*))?([A-Za-z@])")
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\x1b":
            m = csi.match(text, i)
            if m:
                priv, g1, g2, f = m.group(1), m.group(2), m.group(3), m.group(4)
                i = m.end()
                if priv:  # private mode set/reset — no effect on the character grid
                    continue
                a = int(g1) if g1 else 0
                b = int(g2) if g2 else 0
                if f in "Hf":
                    row, col = (a or 1), (b or 1)
                elif f == "d":
                    row = a or 1
                elif f == "G":
                    col = a or 1
                elif f == "A":
                    row = max(1, row - (a or 1))
                elif f == "B":
                    row += a or 1
                elif f in "CD":
                    col = max(1, col + (a or 1) * (1 if f == "C" else -1))
                elif f == "K":  # erase in line: 0/none = cursor->end
                    for c in [k for k in grid if k[0] == row and k[1] >= col]:
                        del grid[c]
                elif f == "J":  # erase in display: 2 = all; else cursor->end
                    if a == 2:
                        grid.clear()
                    else:
                        for c in [k for k in grid if k[0] > row or (k[0] == row and k[1] >= col)]:
                            del grid[c]
                continue
            # a non-CSI escape (ESC=, ESC>, ESC(B, ...): skip ESC + its 1 or 2 designator bytes
            i += 3 if i + 1 < len(text) and text[i + 1] in "()" else 2
            continue
        if ch == "\r":
            col = 1
        elif ch == "\n":
            row += 1
        elif ch == "\b":
            col = max(1, col - 1)
        elif ch >= " ":
            grid[(row, col)] = ch
            col += 1
        i += 1
    if not grid:
        return ""
    maxr = max(r for r, _ in grid)
    lines = []
    for r in range(1, maxr + 1):
        cols = [c for (rr, c) in grid if rr == r]
        line = "".join(grid.get((r, c), " ") for c in range(1, (max(cols) if cols else 0) + 1))
        lines.append(line)
    return "\n".join(lines)


def test_tui_edit_and_save_surfaces_to_the_config_file(tmp_path):
    import pty

    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")
    }
    env.update({"HOME": str(home), "TERM": "xterm", "LINES": "40", "COLUMNS": "100"})

    pid, master = pty.fork()
    if pid == 0:  # child: become the real CLI
        try:
            os.execvpe(
                sys.executable,
                [sys.executable, "-m", "syncade", "--repo-root", str(repo), "--config"],
                env,
            )
        except BaseException:
            os._exit(127)

    try:
        acc = b""
        deadline = time.time() + 8
        while b"Configure syncade" not in acc and time.time() < deadline:
            acc += _drain(master, 0.3)
        assert b"Configure syncade" in acc, f"menu never rendered; got: {acc[-600:]!r}"
        assert b"Rounds (max)" in acc  # the setting surfaces as advertised

        for _ in range(4):  # 'j' = down (producer, reviewer 1, reviewer 2, judge -> Rounds (max))
            os.write(master, b"j")
            acc += _drain(master, 0.1)
        os.write(master, b"\r")  # Enter -> edit prompt
        acc += _drain(master, 0.2)
        os.write(master, b"2\r")  # type the value + confirm
        acc += _drain(master, 0.4)  # capture the redraw so we can prove the edit surfaced
        # The edited value + its new "global" attribution must SURFACE in the UI, not just on disk.
        assert b"updated" in acc, f"edit did not register in the UI: {acc[-400:]!r}"
        assert b"global" in acc, (
            "the edited row was not re-attributed to the global layer on screen"
        )
        os.write(master, b"s")  # save
        acc += _drain(master, 0.2)
        assert b"saved" in acc, "save was not confirmed on screen"
        os.write(master, b"q")  # quit (not dirty after save)

        exited = False
        end = time.time() + 6
        while time.time() < end:
            acc += _drain(master, 0.2)
            wpid, _status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                exited = True
                break
        if not exited:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            pytest.fail("TUI did not exit after q")
    finally:
        os.close(master)

    cfg = home / ".syncade" / "config.toml"
    assert cfg.exists(), "global config file was not written"
    assert (
        tomllib.loads(cfg.read_text())["loop"]["max_rounds"] == 2
    )  # typed value == persisted state


def test_tui_tiny_terminal_degrades_without_crashing(tmp_path):
    """A tiny/narrow real TTY must degrade (resize message), never traceback out of curses.wrapper.
    Drive both pre-fix crash sites in a 6x20 terminal — the provider picker (wide title) and the
    numeric prompt (off-screen row) — then quit cleanly. Pre-fix this raised _curses.error."""
    import pty

    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")
    }
    env.update({"HOME": str(home), "TERM": "xterm", "LINES": "6", "COLUMNS": "20"})

    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvpe(
                sys.executable,
                [sys.executable, "-m", "syncade", "--repo-root", str(repo), "--config"],
                env,
            )
        except BaseException:
            os._exit(127)

    exit_ok = False
    try:
        acc = _drain(master, 2.0)  # initialize (may render the resize message)
        os.write(master, b"\r")  # Enter on Producer (drill) -> producer field screen
        acc += _drain(master, 0.3)
        os.write(master, b"\r")  # Enter on Model -> provider picker (over-wide title crash site)
        acc += _drain(master, 0.4)
        os.write(master, b"\x1b")  # Esc -> cancel the picker
        acc += _drain(master, 0.3)
        os.write(master, b"\x1b")  # Esc -> back to the top screen
        acc += _drain(master, 0.3)
        for _ in range(4):  # down to a numeric row (Rounds) -> the _prompt crash site
            os.write(master, b"j")
            acc += _drain(master, 0.1)
        os.write(master, b"\r")  # Enter -> _prompt in a too-small terminal (degrades to no-op)
        acc += _drain(master, 0.3)
        os.write(master, b"q")  # quit (nothing edited -> not dirty -> exits)

        end = time.time() + 6
        while time.time() < end:
            acc += _drain(master, 0.2)
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                exit_ok = os.WIFEXITED(status)
                break
        else:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            pytest.fail(f"TUI hung/crashed in a tiny terminal; output: {acc[-400:]!r}")
    finally:
        os.close(master)

    assert b"Traceback" not in acc, f"curses crashed in a tiny terminal: {acc[-600:]!r}"
    assert exit_ok, "TUI did not exit normally after driving a tiny terminal"


def test_tui_model_picker_selects_provider_then_model(tmp_path):
    """Drive the model-picker flow: Enter on Producer model → select provider → select model."""
    import pty

    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")
    }
    env.update({"HOME": str(home), "TERM": "xterm", "LINES": "40", "COLUMNS": "120"})

    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvpe(
                sys.executable,
                [sys.executable, "-m", "syncade", "--repo-root", str(repo), "--config"],
                env,
            )
        except BaseException:
            os._exit(127)

    try:
        acc = b""
        deadline = time.time() + 8
        while b"Configure syncade" not in acc and time.time() < deadline:
            acc += _drain(master, 0.3)
        assert b"Configure syncade" in acc, f"menu never rendered; got: {acc[-600:]!r}"

        # cursor starts on the Producer DRILL row → Enter drills into the producer field screen
        os.write(master, b"\r")
        acc += _drain(master, 0.4)
        assert b"Thinking" in acc, f"did not drill into producer screen: {acc[-400:]!r}"
        # cursor is on the Model row (first field) → Enter → provider picker
        os.write(master, b"\r")
        acc += _drain(master, 0.4)
        assert b"Select provider" in acc, f"provider picker did not appear: {acc[-400:]!r}"

        # Select the first provider (anthropic) by pressing Enter
        os.write(master, b"\r")
        acc += _drain(master, 0.4)
        # curses only redraws the changed part of the title; check for the suffix.
        assert b"anthropic model" in acc, f"model picker did not appear: {acc[-400:]!r}"

        # Select the first model (claude-sonnet-4-6) by pressing Enter
        os.write(master, b"\r")
        acc += _drain(master, 0.4)
        assert b"updated" in acc, f"edit did not register: {acc[-400:]!r}"

        # Save and quit
        os.write(master, b"s")
        acc += _drain(master, 0.2)
        assert b"saved" in acc, "save was not confirmed"
        os.write(master, b"q")

        exited = False
        end = time.time() + 6
        while time.time() < end:
            acc += _drain(master, 0.2)
            wpid, _status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                exited = True
                break
        if not exited:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            pytest.fail("TUI did not exit after q")
    finally:
        os.close(master)

    cfg = home / ".syncade" / "config.toml"
    assert cfg.exists(), "global config file was not written"
    parsed = tomllib.loads(cfg.read_text())
    assert parsed["producer"]["provider"] == "anthropic"
    assert "claude-" in parsed["producer"]["model"]


def test_tui_target_toggle_and_shadow_flag_surface(tmp_path):
    """As-a-user proof for the operator's original complaint: in a repo that overrides [producer],
    the Producer row is flagged 'shadowed by repo' at the global target, and pressing 't' switches
    the edit target to repo — so an edit there will actually take effect."""
    import pty
    import subprocess

    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # the repo target needs a git repo
    (repo / ".syncade").mkdir()
    (repo / ".syncade" / "config.toml").write_text(
        '[producer]\nprovider = "openai"\nmodel = "gpt-5.5"\n'
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")
    }
    env.update({"HOME": str(home), "TERM": "xterm", "LINES": "40", "COLUMNS": "100"})

    pid, master = pty.fork()
    if pid == 0:  # child: become the real CLI
        try:
            os.execvpe(
                sys.executable,
                [sys.executable, "-m", "syncade", "--repo-root", str(repo), "--config"],
                env,
            )
        except BaseException:
            os._exit(127)

    try:
        acc = b""
        deadline = time.time() + 8
        while b"Configure syncade" not in acc and time.time() < deadline:
            acc += _drain(master, 0.3)
        assert b"Configure syncade" in acc, f"menu never rendered; got: {acc[-600:]!r}"
        assert b"target: global" in acc  # starts on the global target
        assert b"shadowed by repo" in acc, f"producer row not flagged shadowed: {acc[-600:]!r}"
        os.write(master, b"t")  # toggle the edit target -> repo
        acc += _drain(master, 0.4)
        assert b"target: repo" in acc, f"target did not switch to repo: {acc[-400:]!r}"
        os.write(master, b"q")  # quit (toggling is not a dirtying edit)

        exited = False
        end = time.time() + 6
        while time.time() < end:
            acc += _drain(master, 0.2)
            wpid, _status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                exited = True
                break
        if not exited:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            pytest.fail("TUI did not exit after q")
    finally:
        os.close(master)


def test_tui_drill_into_producer_edit_thinking_and_persist(tmp_path):
    """As-a-user drill-in proof (inc 4): drill into Producer, edit Thinking (a field the menu could
    not reach before), save — the typed value lands in the persisted config."""
    import pty

    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")
    }
    env.update({"HOME": str(home), "TERM": "xterm", "LINES": "40", "COLUMNS": "120"})

    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvpe(
                sys.executable,
                [sys.executable, "-m", "syncade", "--repo-root", str(repo), "--config"],
                env,
            )
        except BaseException:
            os._exit(127)

    try:
        acc = b""
        deadline = time.time() + 8
        while b"Configure syncade" not in acc and time.time() < deadline:
            acc += _drain(master, 0.3)
        assert b"Configure syncade" in acc, f"menu never rendered; got: {acc[-600:]!r}"
        # cursor starts on Producer (drill) → Enter drills into the producer field screen
        os.write(master, b"\r")
        acc += _drain(master, 0.4)
        assert b"Thinking" in acc and b"producer" in acc  # breadcrumb + the field rows
        os.write(master, b"j")  # down to the Thinking row (Model is row 0)
        acc += _drain(master, 0.1)
        os.write(master, b"\r")  # Enter -> edit prompt
        acc += _drain(master, 0.2)
        os.write(master, b"high\r")  # type the value + confirm
        acc += _drain(master, 0.4)
        assert b"updated" in acc, f"thinking edit did not register: {acc[-400:]!r}"
        os.write(master, b"s")  # save
        acc += _drain(master, 0.2)
        assert b"saved" in acc, "save was not confirmed"
        os.write(master, b"q")

        exited = False
        end = time.time() + 6
        while time.time() < end:
            acc += _drain(master, 0.2)
            wpid, _status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                exited = True
                break
        if not exited:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            pytest.fail("TUI did not exit after q")
    finally:
        os.close(master)

    cfg = home / ".syncade" / "config.toml"
    assert cfg.exists(), "global config file was not written"
    assert tomllib.loads(cfg.read_text())["producer"]["thinking"] == "high"  # typed == persisted


def test_tui_global_edit_of_shadowed_field_updates_screen_immediately(tmp_path):
    """As-a-user regression for the dogfood bug: editing a repo-shadowed field at target=global
    must update the ON-SCREEN value at once (no save/quit/relaunch). Global timeout=2400, repo
    timeout=1800 (repo shadows). Before the fix the screen kept showing the repo's 1800."""
    import pty
    import subprocess

    home = tmp_path / "home"
    home.mkdir()
    (home / ".syncade").mkdir()
    (home / ".syncade" / "config.toml").write_text("[loop]\ntimeout_seconds = 2400\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # the repo layer needs a git repo
    (repo / ".syncade").mkdir()
    (repo / ".syncade" / "config.toml").write_text("[loop]\ntimeout_seconds = 1800\n")
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")
    }
    env.update({"HOME": str(home), "TERM": "xterm", "LINES": "40", "COLUMNS": "100"})

    pid, master = pty.fork()
    if pid == 0:  # child: become the real CLI
        try:
            os.execvpe(
                sys.executable,
                [sys.executable, "-m", "syncade", "--repo-root", str(repo), "--config"],
                env,
            )
        except BaseException:
            os._exit(127)

    try:
        acc = b""
        deadline = time.time() + 8
        while b"Configure syncade" not in acc and time.time() < deadline:
            acc += _drain(master, 0.3)
        assert b"Configure syncade" in acc, f"menu never rendered; got: {acc[-600:]!r}"
        # At target=global the row shows GLOBAL's 2400 (not the repo's 1800), flagged shadowed.
        assert b"2400.0" in acc, f"global value not shown at global target: {acc[-600:]!r}"
        assert b"shadowed by repo" in acc

        # Default roster is 2 reviewers, so the rows are Producer, Reviewer 1, Reviewer 2, Judge,
        # Rounds, then Time per subprocess — 5 downs from the top.
        for _ in range(5):
            os.write(master, b"j")
            acc += _drain(master, 0.1)
        os.write(master, b"\r")  # Enter -> edit prompt
        acc += _drain(master, 0.2)
        os.write(master, b"3000\r")  # type the new value + confirm
        acc += _drain(master, 0.5)  # capture the redraw
        # The NEW value must be on the RECONSTRUCTED screen immediately, BEFORE any save — the core
        # bug. (curses diffs the update, so 3000.0 isn't contiguous in the raw bytes.)
        screen = _render_screen(acc)
        time_line = next((ln for ln in screen.splitlines() if "Time per subprocess" in ln), "")
        assert "3000.0" in time_line, f"edit did not surface on the Time row: {time_line!r}"
        assert "1800.0" not in time_line  # the stale repo value is gone
        assert b"updated" in acc
        assert "shadowed by repo" in time_line  # the runtime-override warning is preserved

        os.write(master, b"q")  # quit; dirty -> confirm
        acc += _drain(master, 0.3)
        os.write(master, b"y")

        exited = False
        end = time.time() + 6
        while time.time() < end:
            acc += _drain(master, 0.2)
            wpid, _status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                exited = True
                break
        if not exited:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            pytest.fail("TUI did not exit after q")
    finally:
        os.close(master)
