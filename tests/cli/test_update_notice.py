"""PR-h-field-07 item 4 — the notice informs and can do nothing else.

The invariant under test is negative: no exit code, no gate, no refusal, at any severity. That is
the whole tier design — a `critical_below` that could stop a run would be a remote kill switch,
so "critical" must differ from "routine" in prominence and wording ONLY.

Patches target ``cli.update_notice``'s own globals, not the modules they come from — the
Decomposition Rule: the function body reads the names bound here.
"""

from __future__ import annotations

import subprocess

import pytest

from syncade import __version__
from syncade.cli import update_notice
from syncade.cli.skill_status import SkillStatus
from syncade.update_check import UpdateNotice

ROUTINE = UpdateNotice(latest="9.9.9", critical=False, reason=None, url=None)
CRITICAL = UpdateNotice(
    latest="9.9.9",
    critical=True,
    reason="the producer can force-push over uncommitted work",
    url="https://example.invalid/GHSA-1",
)
CRITICAL_NO_FIX = UpdateNotice(
    latest=None, critical=True, reason="secrets can reach a reviewer", url="https://x.invalid/G2"
)


@pytest.fixture(autouse=True)
def _no_ambient(monkeypatch: pytest.MonkeyPatch):
    """Neither signal fires unless a test asks for it, and the global config is never read.

    ``emit_update_notice`` reads ``session_checked()`` twice per call: once before
    ``check_for_update`` (first_in_session) and once after (session_confirmed). The default
    stub models "first invocation, write succeeded": the pre-check read returns False and the
    post-check read returns True. Tests that need a different session state override this stub.
    """
    monkeypatch.setattr(update_notice, "check_for_update", lambda *a, **k: None)
    monkeypatch.setattr(update_notice, "stale_skills", lambda *a, **k: [])
    monkeypatch.setattr(update_notice, "_check_enabled", lambda *a: True)
    # Stateful: first call returns False (not yet checked), second returns True (mark succeeded).
    # A fresh list per fixture instance, so each test starts from a clean state.
    _calls: list = []

    def _session_checked_once() -> bool:
        _calls.append(1)
        return len(_calls) > 1

    monkeypatch.setattr(update_notice, "session_checked", _session_checked_once)
    monkeypatch.delenv("CI", raising=False)


def _emit(capsys, *, quiet: bool = False) -> str:
    update_notice.emit_update_notice(quiet=quiet)
    return capsys.readouterr().err


def _with(monkeypatch: pytest.MonkeyPatch, notice=None, skills=()) -> None:
    monkeypatch.setattr(update_notice, "check_for_update", lambda *a, **k: notice)
    monkeypatch.setattr(update_notice, "stale_skills", lambda *a, **k: list(skills))


# --------------------------------------------------------------------------- the two tiers


def test_nothing_to_say_prints_nothing(capsys) -> None:
    assert _emit(capsys) == ""


def test_routine_is_one_line_naming_both_versions(monkeypatch, capsys) -> None:
    _with(monkeypatch, ROUTINE)
    out = _emit(capsys)
    assert len(out.strip().splitlines()) == 1
    assert "9.9.9" in out and __version__ in out
    assert "syncade --update" in out
    assert "CRITICAL" not in out


def test_the_notice_never_names_a_flag_the_parser_would_reject(monkeypatch, capsys) -> None:
    """The notice is operator-facing text, so a flag it names must exist — the same contract
    ``test_documented_flags_exist`` holds for docs. It caught a real forward reference to
    ``--update`` before item 5 defined it; this keeps the rendered output honest too."""
    import re

    from syncade.cli.parser import build_parser
    from syncade.cli.skill_status import SkillStatus

    real = {opt for a in build_parser()._actions for opt in a.option_strings}
    _with(
        monkeypatch,
        CRITICAL,
        [SkillStatus("claude", "stale", None), SkillStatus("codex", "unknown", None)],
    )
    rendered = _emit(capsys)
    _with(monkeypatch, CRITICAL_NO_FIX, [])
    rendered += _emit(capsys)
    named = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]{2,}[a-z0-9]", rendered))
    assert named <= real, f"notice names flags the parser does not define: {sorted(named - real)}"


def test_routine_is_suppressed_by_quiet(monkeypatch, capsys) -> None:
    _with(monkeypatch, ROUTINE)
    assert _emit(capsys, quiet=True) == ""


def test_critical_survives_quiet(monkeypatch, capsys) -> None:
    """The one behavioural difference between the tiers. A security notice a flag can silence is
    not a security notice."""
    _with(monkeypatch, CRITICAL)
    out = _emit(capsys, quiet=True)
    assert "CRITICAL" in out
    assert CRITICAL.reason in out and CRITICAL.url in out


def test_critical_names_its_reason_and_advisory_url(monkeypatch, capsys) -> None:
    _with(monkeypatch, CRITICAL)
    out = _emit(capsys)
    assert CRITICAL.reason in out and CRITICAL.url in out
    assert "9.9.9" in out


def test_critical_with_no_fix_does_not_invent_an_upgrade(monkeypatch, capsys) -> None:
    """A version can be marked critical before its fix ships. Telling someone on 0.6.2 to
    upgrade to 0.6.2 is visibly broken advice, so the wording changes instead."""
    _with(monkeypatch, CRITICAL_NO_FIX)
    out = _emit(capsys)
    assert "No fix is published yet" in out
    assert "--update" not in out, "there is nothing to update to"
    assert CRITICAL_NO_FIX.reason in out


def test_every_tier_says_the_run_continues(monkeypatch, capsys) -> None:
    """Load-bearing wording: an operator who reads 'CRITICAL' must not think syncade stopped."""
    for notice in (CRITICAL, CRITICAL_NO_FIX):
        _with(monkeypatch, notice)
        assert "continue" in _emit(capsys).lower()


# --------------------------------------------------------------------------- skill drift


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_stale_skill_names_its_own_harness(monkeypatch, capsys, harness: str) -> None:
    _with(monkeypatch, None, [SkillStatus(harness, "stale", None)])
    out = _emit(capsys)
    assert f"--install-skill {harness}" in out and "out of date" in out


def test_an_unknown_skill_claims_no_ownership(monkeypatch, capsys) -> None:
    """We cannot prove the files are ours, so the wording must not assert that they are."""
    _with(monkeypatch, None, [SkillStatus("claude", "unknown", None)])
    out = _emit(capsys)
    assert "no" in out.lower() and "install record" in out
    assert "out of date" not in out, "that would claim knowledge we do not have"
    assert "refuses rather than overwriting" in out


def test_a_stale_skill_is_suppressed_by_quiet(monkeypatch, capsys) -> None:
    _with(monkeypatch, None, [SkillStatus("claude", "stale", None)])
    assert _emit(capsys, quiet=True) == ""


def test_package_and_skill_notices_appear_together(monkeypatch, capsys) -> None:
    """They drift apart routinely — upgrading the package leaves the copied markdown alone."""
    _with(monkeypatch, ROUTINE, [SkillStatus("claude", "stale", None)])
    out = _emit(capsys)
    assert "update available" in out and "--install-skill claude" in out


def test_a_critical_package_notice_does_not_make_skill_lines_survive_quiet(
    monkeypatch, capsys
) -> None:
    """Criticality belongs to the package advisory. Skill drift riding along on its unsuppressible
    channel would make `--quiet` mean different things on different days."""
    _with(monkeypatch, CRITICAL, [SkillStatus("claude", "stale", None)])
    out = _emit(capsys, quiet=True)
    assert "CRITICAL" in out
    assert "--install-skill" not in out


# --------------------------------------------------------------------------- cannot harm a run


def test_a_raising_check_is_silent(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        update_notice,
        "check_for_update",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert _emit(capsys) == ""


def test_a_raising_skill_scan_is_silent(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        update_notice, "stale_skills", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    )
    assert _emit(capsys) == ""


def test_a_broken_config_never_silences_the_notice(monkeypatch) -> None:
    """Fail OPEN on the enable flag specifically: a malformed config file must not be a way to
    turn off a security notice."""
    import syncade.config_loader as loader

    monkeypatch.undo()  # the autouse fixture stubs _check_enabled; this test needs the real one
    monkeypatch.setattr(
        loader, "load_config", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad toml"))
    )
    assert update_notice._check_enabled() is True


def test_disabled_suppresses_both_signals(monkeypatch, capsys) -> None:
    """One switch, not 'the network half of it' — a skill scan is local but rides the same flag."""
    _with(monkeypatch, ROUTINE, [SkillStatus("claude", "stale", None)])
    monkeypatch.setattr(update_notice, "_check_enabled", lambda *a: False)
    assert _emit(capsys) == ""


def test_the_enable_flag_honors_effective_config(monkeypatch) -> None:
    """update.check is read from the merged effective config (global + repo layers).

    A repo-level ``[update] check = false`` must take effect, matching what the config surface
    accepts and what ``--config list`` reports. The repo layer is simply absent when not in a
    git tree, so _check_enabled() still works in a bare shell.
    """

    def fake_disabled(repo_root, **kwargs):
        class C:
            update = type("U", (), {"check": False})()

        return C()

    import syncade.config_loader as loader

    monkeypatch.undo()  # the autouse fixture stubs _check_enabled; this test needs the real one
    monkeypatch.setattr(loader, "load_config", fake_disabled)
    assert update_notice._check_enabled() is False


def test_repo_root_is_passed_to_check_enabled(monkeypatch, tmp_path) -> None:
    """emit_update_notice must pass its repo_root to _check_enabled, not always use cwd.

    A ``--repo-root`` invocation from outside the target repo ignores the target repo's
    ``[update] check = false`` without this — the notice config is read from the wrong dir.
    """
    import syncade.config_loader as loader

    target = tmp_path / "target-repo"
    target.mkdir()
    seen: list = []

    def capture(repo_root, **kwargs):
        seen.append(repo_root)

        class C:
            update = type("U", (), {"check": True})()

        return C()

    monkeypatch.undo()  # remove autouse stub so the real _check_enabled runs
    monkeypatch.setattr(loader, "load_config", capture)
    update_notice.emit_update_notice(repo_root=target, quiet=True)
    assert seen and seen[0] == target, "config must be loaded from repo_root, not cwd"


def test_check_enabled_resolves_subdir_hint_to_git_root(monkeypatch, tmp_path) -> None:
    """A subdirectory hint must be resolved to the actual git root before loading config.

    Without this fix, invoking syncade from within a repo subdirectory (or passing
    ``--repo-root <subdir>``) misses the root's ``.syncade/config.toml``, so a repo-level
    ``[update] check = false`` is silently ignored.
    """
    import syncade.config_loader as loader

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # A REAL repo: `discover_repo_root` validates, so a bare `.git` directory is not enough.
    # The previous version created one and asserted the marker was "all _resolve_repo_root
    # needs" — codifying the approximation that WAS the blocker.
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    subdir = repo_root / "src" / "syncade"
    subdir.mkdir(parents=True)

    seen: list = []

    def capture(root, **kwargs):
        seen.append(root)

        class C:
            update = type("U", (), {"check": True})()

        return C()

    monkeypatch.undo()  # the autouse fixture stubs _check_enabled; this test needs the real one
    monkeypatch.setattr(loader, "load_config", capture)
    update_notice._check_enabled(subdir)
    assert seen and seen[0] == repo_root, (
        f"expected git root {repo_root}, got {seen[0] if seen else 'nothing'}"
    )


def test_check_enabled_passes_include_repo_false_outside_git(monkeypatch, tmp_path) -> None:
    """Outside a git tree, _check_enabled must call load_config with include_repo=False.

    Without this, a stray ``.syncade/config.toml`` in a non-git directory can incorrectly
    suppress the update notice.
    """
    import syncade.config_loader as loader

    non_git_dir = tmp_path / "not-a-repo"
    non_git_dir.mkdir()

    kwargs_seen: list = []

    def capture(root, **kwargs):
        kwargs_seen.append(kwargs)

        class C:
            update = type("U", (), {"check": True})()

        return C()

    monkeypatch.undo()  # the autouse fixture stubs _check_enabled; this test needs the real one
    monkeypatch.setattr(loader, "load_config", capture)
    update_notice._check_enabled(non_git_dir)
    assert kwargs_seen, "load_config must be called"
    assert kwargs_seen[0].get("include_repo") is False, (
        "must drop the repo layer when the hint is not inside a git tree"
    )


def test_the_skill_notice_is_once_per_window_too(monkeypatch, capsys) -> None:
    """Found by running the real CLI, not by a unit test: the session gate lived inside
    `check_for_update`, so it governed the network ping but NOT the local skill scan — three
    commands in one window produced three identical skill warnings. "Once per window" has to
    mean the whole notice, so both halves read one gate.

    ``emit_update_notice`` reads ``session_checked()`` TWICE: once before ``check_for_update``
    (to learn first_in_session) and once after (to confirm the write succeeded). Both reads
    must be modelled to test the first-invocation path correctly.
    """
    _with(monkeypatch, None, [SkillStatus("claude", "stale", None)])

    # First invocation: before-mark returns False, after-mark returns True (write succeeded).
    pre_post = [False, True]

    def _first_in_session() -> bool:
        return pre_post.pop(0) if pre_post else True

    monkeypatch.setattr(update_notice, "session_checked", _first_in_session)
    assert "--install-skill claude" in _emit(capsys), "the first invocation must report it"

    # Second invocation: session already recorded, both reads return True.
    monkeypatch.setattr(update_notice, "session_checked", lambda: True)
    assert _emit(capsys) == "", "a later invocation in the same window must stay silent"


def test_skill_notice_suppressed_when_session_write_fails(monkeypatch, capsys) -> None:
    """When the session cannot be written (e.g. unwritable home dir), skill notices must not
    re-print on every invocation.

    ``check_for_update`` skips the network fetch in this state (PR-h-field-02), but the whole
    notice must also be suppressed: if ``session_checked()`` stays False after the mark attempt,
    every subsequent invocation would see ``first_in_session=True`` and re-print the skill notice,
    violating the once-per-window invariant. The fix gates the skill scan on BOTH first_in_session
    AND a confirmed write (the post-mark re-read of ``session_checked``).
    """
    _with(monkeypatch, None, [SkillStatus("claude", "stale", None)])
    # session_checked always returns False: the write failed, session was never recorded.
    monkeypatch.setattr(update_notice, "session_checked", lambda: False)
    assert _emit(capsys) == "", "skill notice must not appear when session cannot be written"


def test_ci_suppresses_both_the_package_and_skill_notices(monkeypatch, capsys) -> None:
    """CI=true must suppress the skill-drift notice as well as the network ping.

    Previously `CI` was honoured only inside `check_for_update`, so a CI job with a stale
    skill still printed the notice — and because the network half returned before marking
    the session, the same warning re-printed on every invocation."""
    _with(monkeypatch, ROUTINE, [SkillStatus("claude", "stale", None)])
    monkeypatch.setenv("CI", "true")
    assert _emit(capsys) == "", "CI must silence the entire notice, not just the network half"


def test_ci_suppression_applies_even_on_first_invocation(monkeypatch, capsys) -> None:
    """The session is not marked when CI exits early, so without this fix the skill notice
    would re-print on every CI invocation (session_checked() always returns False)."""
    _with(monkeypatch, None, [SkillStatus("codex", "unknown", None)])
    monkeypatch.setenv("CI", "true")
    assert _emit(capsys) == ""


# --------------------------------------------------------------------------- terminal TTY prompt


def test_tty_prompt_returns_true_on_y(monkeypatch, capsys) -> None:
    """In a terminal TTY, the operator answering 'y' must return True so the caller can
    dispatch to --update instead of continuing with the original command."""
    _with(monkeypatch, ROUTINE)
    monkeypatch.setattr(update_notice.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(update_notice.sys.stdin, "readline", lambda: "y\n")
    monkeypatch.setattr(update_notice.sys.stdout, "write", lambda s: None)
    monkeypatch.setattr(update_notice.sys.stdout, "flush", lambda: None)
    assert update_notice.emit_update_notice() is True


def test_tty_prompt_returns_false_on_n(monkeypatch, capsys) -> None:
    """Operator declines: the current command continues, session is still marked checked."""
    _with(monkeypatch, ROUTINE)
    monkeypatch.setattr(update_notice.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(update_notice.sys.stdin, "readline", lambda: "N\n")
    monkeypatch.setattr(update_notice.sys.stdout, "write", lambda s: None)
    monkeypatch.setattr(update_notice.sys.stdout, "flush", lambda: None)
    assert update_notice.emit_update_notice() is False


def test_non_tty_never_prompts(monkeypatch) -> None:
    """A non-TTY stdin (pipe, harness) must not try to read input — it would block."""
    _with(monkeypatch, ROUTINE)
    monkeypatch.setattr(update_notice.sys.stdin, "isatty", lambda: False)
    readline_called: list = []
    monkeypatch.setattr(
        update_notice.sys.stdin, "readline", lambda: readline_called.append(1) or ""
    )
    assert update_notice.emit_update_notice() is False
    assert readline_called == [], "non-TTY must not read stdin"


def test_tty_prompt_absent_when_no_update_available(monkeypatch) -> None:
    """No package notice means no y/N prompt — only skill drift, not updateable from here."""
    _with(monkeypatch, None, [SkillStatus("claude", "stale", None)])
    monkeypatch.setattr(update_notice.sys.stdin, "isatty", lambda: True)
    readline_called: list = []
    monkeypatch.setattr(
        update_notice.sys.stdin, "readline", lambda: readline_called.append(1) or ""
    )
    assert update_notice.emit_update_notice() is False
    assert readline_called == []


def test_quiet_suppresses_tty_prompt(monkeypatch) -> None:
    """--quiet implies non-interactive; no prompt even on a TTY."""
    _with(monkeypatch, ROUTINE)
    monkeypatch.setattr(update_notice.sys.stdin, "isatty", lambda: True)
    readline_called: list = []
    monkeypatch.setattr(
        update_notice.sys.stdin, "readline", lambda: readline_called.append(1) or ""
    )
    assert update_notice.emit_update_notice(quiet=True) is False
    assert readline_called == []


def test_no_prompt_when_the_manifest_says_no_fix_exists(monkeypatch, capsys) -> None:
    """Printing 'No fix is published yet' and then asking 'Update now?' would send the operator
    at an upgrade the manifest just said does not exist."""
    _with(monkeypatch, CRITICAL_NO_FIX)
    monkeypatch.setattr(update_notice.sys.stdin, "isatty", lambda: True)
    asked: list = []
    monkeypatch.setattr(update_notice.sys.stdin, "readline", lambda: asked.append(1) or "y\n")

    assert update_notice.emit_update_notice() is False
    assert asked == [], "there is nothing to upgrade to, so nothing may be offered"
    assert "No fix is published yet" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("shape", "make"),
    [
        ("malformed .git file", lambda d: (d / ".git").write_text("not a gitdir pointer\n")),
        ("empty .git directory", lambda d: (d / ".git").mkdir()),
    ],
)
def test_a_broken_git_marker_is_not_a_repo(tmp_path, monkeypatch, shape, make) -> None:
    """`discover_repo_root` rejects these; a lookalike filesystem walk accepted them.

    The consequence was concrete: a stray repo-local `[update] check = false` under a tree
    syncade does not consider a repository could suppress the manifest check entirely.
    """
    broken = tmp_path / "notarepo"
    broken.mkdir()
    make(broken)
    monkeypatch.undo()
    assert update_notice._resolve_repo_root(broken) is None, shape


def test_an_invalid_invocation_still_emits_and_marks_the_session(monkeypatch, capsys) -> None:
    """`syncade --update --gc` is rejected by shape validation. Before the fix it returned
    BEFORE the notice, so the first command in a window emitted nothing and marked nothing —
    and the next valid command printed the stale notice instead. The emit now sits ahead of
    every early return, so position enforces the rule rather than memory.
    """
    from syncade.cli import main

    calls: list = []
    monkeypatch.setattr(update_notice, "emit_update_notice", lambda **k: calls.append(k) or False)

    rc = main(["--update", "--gc"])
    assert rc == 2, "the invalid pairing must still be rejected"
    assert calls, "the notice must fire even for an invocation that fails validation"
    assert "cannot be combined" in capsys.readouterr().err
