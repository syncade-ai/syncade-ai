"""PR-h-field-07 item 2 — the manifest check must never be able to harm a run.

The acceptance criteria in ``path/to/pr.md`` are: a newer version is
detected; EVERY failure mode proceeds silently; the second invocation in a session makes no
network call; a new session makes exactly one.

The failure-mode tests are the point of this file. A check that crashes on a malformed manifest
would take down the review path it was added to help.
"""

from __future__ import annotations

import json
import re
import urllib.error
from pathlib import Path

import pytest

from syncade import update_check
from syncade.update_check import UpdateNotice, check_for_update, evaluate


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect state off $HOME and clear the ambient session/CI markers, so the suite neither
    reads the developer's real state nor inherits a session id from the harness running it."""
    monkeypatch.setattr(update_check, "_state_path", lambda: tmp_path / "update-state.json")
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CI"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _fetches(monkeypatch: pytest.MonkeyPatch, payload, *, calls: list) -> None:
    def fake(url: str):
        calls.append(url)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(update_check, "_fetch", fake)


# --------------------------------------------------------------------------- detection


def test_a_newer_version_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    _fetches(monkeypatch, {"latest": "0.7.0"}, calls=[])
    notice = check_for_update("0.6.2")
    assert notice == UpdateNotice(latest="0.7.0", critical=False, reason=None, url=None)


def test_same_version_is_silent() -> None:
    assert evaluate({"latest": "0.6.2"}, "0.6.2") is None


def test_a_newer_installed_version_is_silent() -> None:
    """A developer on unreleased main must not be told to downgrade."""
    assert evaluate({"latest": "0.6.2"}, "0.7.0") is None


def test_critical_carries_reason_and_url() -> None:
    notice = evaluate(
        {
            "latest": "0.7.0",
            "critical_below": "0.7.0",
            "critical_reason": "producer force-pushes over uncommitted work",
            "critical_url": "https://example.invalid/advisory",
        },
        "0.6.2",
    )
    assert notice is not None
    assert notice.critical and notice.latest == "0.7.0"
    assert notice.reason == "producer force-pushes over uncommitted work"
    assert notice.url == "https://example.invalid/advisory"


def test_critical_with_no_fix_published_yet() -> None:
    """The state the self-hosted manifest exists for: a hole is known before the fix ships.

    ``latest`` must be None so the caller cannot render "upgrade to 0.6.2" at someone on 0.6.2.
    """
    notice = evaluate(
        {"latest": "0.6.2", "critical_below": "0.6.3", "critical_reason": "x"}, "0.6.2"
    )
    assert notice is not None
    assert notice.critical is True
    assert notice.latest is None


def test_reason_and_url_are_dropped_when_not_critical() -> None:
    notice = evaluate(
        {"latest": "0.7.0", "critical_reason": "scary", "critical_url": "https://x.invalid"},
        "0.6.2",
    )
    assert notice is not None and not notice.critical
    assert notice.reason is None and notice.url is None


# --------------------------------------------------------------------------- fails open


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(urllib.error.URLError("offline"), id="dns-or-network-down"),
        pytest.param(TimeoutError("timed out"), id="timeout"),
        pytest.param(urllib.error.HTTPError("u", 500, "boom", {}, None), id="http-500"),
        pytest.param(ValueError("not json"), id="malformed-json"),
        pytest.param(None, id="fetch-returned-none"),
        pytest.param([], id="json-is-a-list-not-an-object"),
        pytest.param({}, id="empty-object"),
        pytest.param({"latest": None}, id="latest-null"),
        pytest.param({"latest": 7}, id="latest-not-a-string"),
        pytest.param({"latest": "banana"}, id="latest-unparseable"),
        pytest.param({"latest": "1.2"}, id="latest-two-components"),
        pytest.param({"latest": "1.2.3.4"}, id="latest-four-components"),
        pytest.param({"critical_below": "not-a-version"}, id="critical_below-unparseable"),
    ],
)
def test_every_failure_mode_is_silent(payload, monkeypatch: pytest.MonkeyPatch) -> None:
    """No raise, no notice. `_fetch` swallows transport errors; `evaluate` swallows shape errors."""
    calls: list = []
    if isinstance(payload, Exception):
        monkeypatch.setattr(
            update_check.urllib.request,
            "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(payload),
        )
    else:
        _fetches(monkeypatch, payload, calls=calls)
    assert check_for_update("0.6.2") is None


def test_an_unparseable_installed_version_is_no_information(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dev build like 0.7.0.dev1 must not be compared, in either direction."""
    _fetches(monkeypatch, {"latest": "9.9.9", "critical_below": "9.9.9"}, calls=[])
    assert check_for_update("0.7.0.dev1") is None


@pytest.mark.parametrize("bogus", ["1_0.0.0", "١.٢.٣", "-1.0.0", "1.2.x", "1.2.3-rc1", "v1.2.3"])
def test_versions_that_python_would_misread_are_rejected(bogus: str) -> None:
    """`int()` alone accepts underscores (`1_0` is ten) and signs; `isdigit()` alone accepts
    non-ASCII digits (`١` is one). A manifest must not be able to express a version number that
    we would silently read as a different one."""
    assert evaluate({"latest": bogus}, "0.6.2") is None


@pytest.mark.parametrize("field", ["latest", "critical_below"])
def test_surrounding_whitespace_is_a_formatting_artifact_not_a_misreading(field: str) -> None:
    """`" 1.2.3"` means 1.2.3 — there is no other version it could be mistaken for, so it is
    accepted. Asserted on BOTH version fields because an earlier draft stripped only `latest`,
    so the same manifest string was honoured in one field and rejected in the other."""
    assert evaluate({field: " 9.9.9 "}, "0.6.2") is not None


def test_an_unwritable_state_dir_makes_no_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the state dir cannot be written, the session cannot be recorded, so no fetch is made.

    A fetch without a recorded session would repeat on every invocation, violating the
    once-per-session budget. Silence is the right trade-off — a skipped notice costs less than
    repeated network stalls.
    """
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("regular file, so mkdir under it raises")
    monkeypatch.setattr(update_check, "_state_path", lambda: blocked / "sub" / "state.json")
    calls: list = []
    _fetches(monkeypatch, {"latest": "0.7.0"}, calls=calls)
    assert check_for_update("0.6.2") is None
    assert calls == [], "no fetch should be made when the session cannot be recorded"
    # A subsequent call in the same session must also make no fetch.
    assert check_for_update("0.6.2") is None
    assert calls == [], "repeated calls must not fetch when state is unwritable"


def test_corrupt_state_file_is_ignored(monkeypatch: pytest.MonkeyPatch, _isolate: Path) -> None:
    (_isolate / "update-state.json").write_text("{ not json")
    _fetches(monkeypatch, {"latest": "0.7.0"}, calls=[])
    assert check_for_update("0.6.2") is not None


# --------------------------------------------------------------------------- once per session


def test_second_invocation_in_a_session_makes_no_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-a")
    calls: list = []
    _fetches(monkeypatch, {"latest": "0.7.0"}, calls=calls)

    assert check_for_update("0.6.2") is not None
    assert len(calls) == 1
    for _ in range(5):
        assert check_for_update("0.6.2") is None
    assert len(calls) == 1, "the session was already checked; no further fetch is allowed"


def test_a_new_session_makes_exactly_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _fetches(monkeypatch, {"latest": "0.7.0"}, calls=calls)
    for n, session in enumerate(("s1", "s2", "s3"), start=1):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
        assert check_for_update("0.6.2") is not None
        assert len(calls) == n


def test_a_failed_fetch_still_consumes_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Marked BEFORE the fetch on purpose: otherwise a flaky network re-pings — and re-stalls —
    on every invocation for the life of the window."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "flaky")
    calls: list = []
    _fetches(monkeypatch, None, calls=calls)
    assert check_for_update("0.6.2") is None
    assert check_for_update("0.6.2") is None
    assert len(calls) == 1


def test_a_raising_fetch_returns_none_and_still_consumes_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two guarantees in one: the contract is NEVER RAISES even if an inner guard is bypassed,
    and the session is spent because it is marked BEFORE the request — so a process interrupted
    mid-fetch cannot re-ping on the operator's next command."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "interrupted")
    calls: list = []

    def exploding(url: str):
        calls.append(url)
        raise RuntimeError("something no inner guard anticipated")

    monkeypatch.setattr(update_check, "_fetch", exploding)
    assert check_for_update("0.6.2") is None
    assert check_for_update("0.6.2") is None
    assert len(calls) == 1, "the interrupted session must still be spent"


def test_ctrl_c_is_never_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The never-raises guard catches `Exception`, NOT `BaseException`, on purpose: an operator
    pressing Ctrl-C during the 1.5s request must interrupt syncade, not be ignored by it.
    (Written after an earlier version of the test above raised KeyboardInterrupt and tore down
    the whole pytest session — the swallow-everything version would have looked green here.)"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ctrl-c")

    def interrupted(url: str):
        raise KeyboardInterrupt

    monkeypatch.setattr(update_check, "_fetch", interrupted)
    with pytest.raises(KeyboardInterrupt):
        check_for_update("0.6.2")


def test_an_undeterminable_home_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Path.home()` raises RuntimeError with no $HOME and no passwd entry. On the review path
    that would fail a run over a version notice."""
    monkeypatch.setattr(
        update_check, "_state_path", lambda: (_ for _ in ()).throw(RuntimeError("no home"))
    )
    assert check_for_update("0.6.2") is None


def test_harness_sessions_are_distinct_from_each_other_and_from_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    claude = update_check.session_key()
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.setenv("CODEX_THREAD_ID", "abc")
    codex = update_check.session_key()
    monkeypatch.delenv("CODEX_THREAD_ID")
    shell = update_check.session_key()
    assert len({claude, codex, shell}) == 3, "same id under different harnesses must not collide"
    assert shell == f"ppid:{__import__('os').getppid()}"


def test_claude_wins_when_both_harness_markers_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "c")
    monkeypatch.setenv("CODEX_THREAD_ID", "x")
    assert update_check.session_key().startswith("CLAUDE_CODE_SESSION_ID:")


def test_state_file_is_bounded_and_trims_oldest_first(
    monkeypatch: pytest.MonkeyPatch, _isolate: Path
) -> None:
    overflow = 15
    total = update_check._KEEP_SESSIONS + overflow
    _fetches(monkeypatch, {"latest": "0.7.0"}, calls=[])
    for i in range(total):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", f"s{i}")
        check_for_update("0.6.2")

    state = json.loads((_isolate / "update-state.json").read_text())
    assert len(state) == update_check._KEEP_SESSIONS
    # Oldest evicted, newest retained — a cap that dropped the NEW key would also hold the
    # length assertion above while re-pinging the current session forever.
    assert "CLAUDE_CODE_SESSION_ID:s0" not in state
    assert f"CLAUDE_CODE_SESSION_ID:s{total - 1}" in state


# --------------------------------------------------------------------------- opt out


def test_lock_failure_falls_back_and_still_gates_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If advisory locking raises, the fallback non-atomic path must still record the session.

    This covers the case where flock is unavailable (Windows, restricted environments) and
    ensures the once-per-session guarantee holds at least in the non-concurrent case even
    when the lock file cannot be written."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lock-fallback")
    calls: list = []
    _fetches(monkeypatch, {"latest": "9.9.9"}, calls=calls)

    if update_check._HAS_FLOCK:
        monkeypatch.setattr(
            update_check._fcntl,
            "flock",
            lambda *a, **k: (_ for _ in ()).throw(OSError("flock unavailable")),
        )

    result = check_for_update("0.6.2")
    assert result is not None
    assert len(calls) == 1

    assert check_for_update("0.6.2") is None
    assert len(calls) == 1, "second invocation in same session must not fetch"


def test_disabled_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _fetches(monkeypatch, {"latest": "0.7.0"}, calls=calls)
    assert check_for_update("0.6.2", enabled=False) is None
    assert calls == []


def test_ci_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    calls: list = []
    _fetches(monkeypatch, {"latest": "0.7.0"}, calls=calls)
    assert check_for_update("0.6.2") is None
    assert calls == []


def test_disabled_does_not_consume_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turning the check back on must not find the session already spent."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s")
    calls: list = []
    _fetches(monkeypatch, {"latest": "0.7.0"}, calls=calls)
    assert check_for_update("0.6.2", enabled=False) is None
    assert check_for_update("0.6.2") is not None
    assert len(calls) == 1


# --------------------------------------------------------------------------- the shipped manifest


def test_the_shipped_manifest_is_valid_and_tracks_the_package_version() -> None:
    """`latest` must move with `pyproject`, or the check silently announces nothing forever.

    This repo bumps `version` as part of the release commit, so the two are in step at rest. The
    failure this prevents is silent by nature: a forgotten manifest produces no error anywhere,
    just an update nobody is ever told about.
    """
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "update-manifest.json").read_text())
    pyproject = (root / "pyproject.toml").read_text()
    version = re.search(r'^version = "(.*)"', pyproject, re.M).group(1)

    assert manifest["latest"] == version
    # A manifest that cannot be parsed by our own parser would fail open forever — silently.
    assert update_check._parse(manifest["latest"]) is not None
    assert set(manifest) <= {
        "_comment",
        "latest",
        "critical_below",
        "critical_reason",
        "critical_url",
    }, "an unrecognised key is ignored at runtime; keep the shipped file to the known shape"
    # Nothing is critical by default; asserted so marking one is a deliberate, reviewed edit.
    assert manifest["critical_below"] is None


def test_the_shipped_manifest_is_silent_for_the_current_release() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "update-manifest.json").read_text())
    assert evaluate(manifest, manifest["latest"]) is None


def test_an_oversized_body_is_rejected_not_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading exactly the cap cannot tell "fits" from "truncated but still parses".

    A response whose first _MAX_BYTES happen to hold a complete object followed by padding was
    ACCEPTED, so the documented oversized-body guard did not exist. One extra byte makes it real.
    """
    payload = json.dumps({"latest": "9.9.9"}).encode()
    oversized = payload + b" " * (update_check._MAX_BYTES + 1 - len(payload))

    class _Resp:
        status = 200

        def read(self, n):
            return oversized[:n]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(update_check.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert update_check._fetch("https://x.invalid/m.json") is None

    # ...and a body that genuinely fits is still accepted, so the guard is not just "always None".
    small = payload

    class _Small(_Resp):
        def read(self, n):
            return small[:n]

    monkeypatch.setattr(update_check.urllib.request, "urlopen", lambda *a, **k: _Small())
    assert update_check._fetch("https://x.invalid/m.json") == {"latest": "9.9.9"}
