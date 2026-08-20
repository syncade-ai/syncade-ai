"""PR-h-field-08 — `--update` must not report success having installed nothing.

Split out of ``test_update_mode.py`` when these tests took it to 523 of a 500 code-LOC cap.
The same thing happened to ``test_config_schema.py`` and `CLAUDE.md` records it; the fix is the
same, and it is a real separation rather than a dumping ground: everything here is about ONE
question — *did the version actually move, and can we prove it?* — while the original file is
about install-method detection and the upgrade command.

**Four revisions of this code, four dogfood blockers, all the same shape:** something was trusted
to mean what it nearly meant. The package manager's exit code (ran != upgraded), then
``evaluate()`` returning ``None`` (no notice != no newer release), then ``evaluate()`` again for
the comparison (critical-no-fix), then the probe's raw stdout (any text != a version). The rule
that finally held is to validate a value as the thing it claims to be, at the point it enters.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import syncade
from syncade.cli import update_mode
from syncade.exit_codes import SUCCESS, WORKTREE_ERROR
from syncade.process import SubprocessError, SubprocessResult
from tests.cli.test_update_mode import _install_tree, _point_syncade_at, _Result

# --------------------------------------------------------------------- the post-condition
#
# Exit 0 from the package manager means "the command ran", NOT "the version moved". Found live
# on the 0.7.0 release: `uv tool install syncade==0.6.3` records `specifier = "==0.6.3"` in its
# receipt, so `uv tool upgrade` honours the pin, prints "Nothing to upgrade" and exits 0 — and
# syncade said "updated. Re-run your command" having installed nothing. The operator re-runs,
# sees the same version, and is told again every session, forever.


def _upgrade_reporting_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A proven uv install whose upgrade command exits 0."""
    pkg = _install_tree(tmp_path, "uv-receipt.toml")
    _point_syncade_at(monkeypatch, pkg)
    monkeypatch.setattr(update_mode, "run_subprocess", lambda *a, **k: _Result(0))
    monkeypatch.setattr(update_mode, "_resync_skills", lambda out: True)


def test_an_upgrade_that_changed_nothing_is_reported_as_failure(
    tmp_path, monkeypatch, capsys
) -> None:
    """The defect itself: same version after a 'successful' upgrade must not read as success."""
    _upgrade_reporting_success(monkeypatch, tmp_path)
    monkeypatch.setattr(update_mode, "_installed_version", lambda: syncade.__version__)
    monkeypatch.setattr(update_mode, "_fetch", lambda url: {"latest": "99.0.0"})

    assert update_mode.run_update() == WORKTREE_ERROR
    out = capsys.readouterr().err
    assert "nothing was installed" in out
    assert "updated." not in out, "it must not also claim success"
    assert "pins a version" in out, "name the most likely cause, not just the symptom"


def test_a_real_upgrade_still_succeeds_and_names_the_new_version(
    tmp_path, monkeypatch, capsys
) -> None:
    """The guard must not fire on the path it exists to protect."""
    _upgrade_reporting_success(monkeypatch, tmp_path)
    monkeypatch.setattr(update_mode, "_installed_version", lambda: "99.0.0")

    assert update_mode.run_update() == SUCCESS
    assert "updated to 99.0.0" in capsys.readouterr().err


def test_an_unverifiable_version_is_not_reported_as_failure(tmp_path, monkeypatch, capsys) -> None:
    """`None` means "could not verify", which is NOT "did not change".

    Turning an unreadable version into a failure would break every install whose metadata we
    cannot probe, having actually upgraded them — a worse error than the one being fixed.
    """
    _upgrade_reporting_success(monkeypatch, tmp_path)
    monkeypatch.setattr(update_mode, "_installed_version", lambda: None)

    assert update_mode.run_update() == SUCCESS
    out = capsys.readouterr().err
    assert "updated." in out
    assert "nothing was installed" not in out
    assert "updated to" not in out, "no version may be named when none was established"


def test_the_version_probe_reads_a_fresh_interpreter_not_this_one(monkeypatch) -> None:
    """It must not answer from `syncade.__version__`, which this process imported BEFORE the
    upgrade and is therefore the old number by construction."""
    seen: list[list[str]] = []

    def spy(cmd, **kw):
        seen.append(cmd)
        return SubprocessResult(returncode=0, stdout="1.2.3\n", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(update_mode, "run_subprocess", spy)
    assert update_mode._installed_version() == "1.2.3"
    assert seen and seen[0][0] == sys.executable, "must re-invoke the interpreter, not import"
    assert "importlib.metadata" in seen[0][-1]


def test_an_unreadable_probe_degrades_to_none(monkeypatch) -> None:
    for outcome in (
        lambda *a, **k: (_ for _ in ()).throw(SubprocessError("no python")),
        lambda *a, **k: SubprocessResult(returncode=1, stdout="", stderr="x", duration_seconds=0.0),
        lambda *a, **k: SubprocessResult(
            returncode=0, stdout="  \n", stderr="", duration_seconds=0.0
        ),
    ):
        monkeypatch.setattr(update_mode, "run_subprocess", outcome)
        assert update_mode._installed_version() is None


def test_startup_noise_before_version_line_is_stripped(monkeypatch) -> None:
    """sitecustomize imported from PYTHONPATH can emit stdout before the version print.

    The old code returned the full stdout verbatim, so "noise\\n0.7.0" != "0.7.0" and
    run_update treated the install as having moved — a false success on an unchanged version.
    """
    monkeypatch.setattr(
        update_mode,
        "run_subprocess",
        lambda *a, **k: SubprocessResult(
            returncode=0, stdout="startup noise\n1.2.3\n", stderr="", duration_seconds=0.0
        ),
    )
    assert update_mode._installed_version() == "1.2.3"


def test_non_version_stdout_degrades_to_none(monkeypatch) -> None:
    """stdout that doesn't end with a parseable version string means 'could not verify'."""
    for stdout in ("not a version\n", "noise only\n", "1.2\n", "1.2.3.4\n"):
        monkeypatch.setattr(
            update_mode,
            "run_subprocess",
            lambda *a, stdout=stdout, **k: SubprocessResult(
                returncode=0, stdout=stdout, stderr="", duration_seconds=0.0
            ),
        )
        assert update_mode._installed_version() is None, f"should be None for {stdout!r}"


def test_an_already_current_install_is_not_reported_as_a_failed_upgrade(
    tmp_path, monkeypatch, capsys
) -> None:
    """The post-condition's own false positive, found by exercising it rather than reasoning.

    The version not moving has TWO causes needing opposite answers: a pinned/held install (a
    real failure) and simply being current already (not a failure at all). The first version of
    the guard could not tell them apart and told an operator who ran `--update` while current
    that their install was pinned and broken — a fresh false claim inside the fix for one.
    """
    _upgrade_reporting_success(monkeypatch, tmp_path)
    monkeypatch.setattr(update_mode, "_installed_version", lambda: syncade.__version__)
    monkeypatch.setattr(update_mode, "_fetch", lambda url: {"latest": syncade.__version__})

    assert update_mode.run_update() == SUCCESS
    out = capsys.readouterr().err
    assert "already up to date" in out
    assert "pins a version" not in out


def test_an_unreachable_manifest_cannot_certify_you_as_current(
    tmp_path, monkeypatch, capsys
) -> None:
    """`_fetch` returning None must NOT read as "you are current".

    `evaluate(None, v)` also returns None, so collapsing the two would tell an operator whose
    upgrade silently failed while offline that they were up to date — stranding them on an old
    version believing it is the new one. The louder message is the safe direction here.
    """
    _upgrade_reporting_success(monkeypatch, tmp_path)
    monkeypatch.setattr(update_mode, "_installed_version", lambda: syncade.__version__)
    monkeypatch.setattr(update_mode, "_fetch", lambda url: None)

    assert update_mode.run_update() == WORKTREE_ERROR
    assert "already up to date" not in capsys.readouterr().err


def test_the_happy_path_never_touches_the_network(tmp_path, monkeypatch) -> None:
    """The manifest is consulted ONLY when the version failed to move, so a normal successful
    upgrade adds no request and no latency."""
    _upgrade_reporting_success(monkeypatch, tmp_path)
    monkeypatch.setattr(update_mode, "_installed_version", lambda: "99.0.0")
    monkeypatch.setattr(
        update_mode, "_fetch", lambda url: pytest.fail("the happy path must not fetch")
    )

    assert update_mode.run_update() == SUCCESS


# ---------------------------------------------------------------- malformed-manifest regression
#
# A reachable manifest that has no parseable latest field must NOT certify the operator as
# current. evaluate() returns None for BOTH "already current" and "no usable latest version",
# so trusting None alone on a dict manifest restores the false-success class for any manifest
# that is syntactically valid JSON but semantically unusable.


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"latest": None},
        {"latest": "not-a-version"},
        {"latest": "0.7"},  # two-part, not three-part
        {"latest": ""},
    ],
    ids=["empty", "null-latest", "unparseable-latest", "two-part-latest", "empty-latest"],
)
def test_is_newest_treats_malformed_manifest_as_unverifiable(manifest, monkeypatch) -> None:
    """A reachable manifest with no parseable latest must not certify any version as current.

    Verified: this test fails against the pre-fix _is_newest that only checked
    `evaluate(manifest, installed) is None` — all five manifests above make evaluate() return
    None while providing no actual version information, so the pre-fix code returned True and
    certified the operator as already-current on an unusable manifest.
    """
    monkeypatch.setattr(update_mode, "_fetch", lambda url: manifest)
    assert update_mode._is_newest("0.7.0") is False, (
        f"manifest {manifest!r} cannot prove current status; _is_newest must return False"
    )


def test_a_malformed_manifest_does_not_produce_already_up_to_date(
    tmp_path, monkeypatch, capsys
) -> None:
    """End-to-end: an empty manifest must not report success to an operator whose version
    did not move. Verified: fails against the pre-fix code."""
    _upgrade_reporting_success(monkeypatch, tmp_path)
    monkeypatch.setattr(update_mode, "_installed_version", lambda: syncade.__version__)
    monkeypatch.setattr(update_mode, "_fetch", lambda url: {})  # reachable but malformed

    assert update_mode.run_update() == WORKTREE_ERROR
    out = capsys.readouterr().err
    assert "already up to date" not in out
    assert "nothing was installed" in out


# ------------------------------------------------------------ already-current resync regression


def test_skills_are_resynced_on_the_already_current_path(tmp_path, monkeypatch) -> None:
    """--update must re-install skills even when the package is already at the newest version.

    The already-current early return was added by this PR. Without explicit resync on that
    path, a stale or missing skill is silently left behind on every invocation where the
    package is current. Verified: fails against the pre-fix code where the already-current
    branch returned before _resync_skills() was called.
    """
    _point_syncade_at(monkeypatch, _install_tree(tmp_path, "uv-receipt.toml"))
    monkeypatch.setattr(update_mode, "run_subprocess", lambda *a, **k: _Result(0))
    monkeypatch.setattr(update_mode, "_installed_version", lambda: syncade.__version__)
    monkeypatch.setattr(update_mode, "_fetch", lambda url: {"latest": syncade.__version__})

    calls: list = []
    monkeypatch.setattr(update_mode, "_resync_skills", lambda out: calls.append(1) or True)

    assert update_mode.run_update(cwd=tmp_path) == SUCCESS
    assert calls == [1], "skills must be resynced even when the package is already current"


def test_a_failed_skill_resync_on_already_current_path_returns_non_zero(
    tmp_path, monkeypatch, capsys
) -> None:
    """A failed resync on the already-current path must propagate the failure code."""
    _point_syncade_at(monkeypatch, _install_tree(tmp_path, "uv-receipt.toml"))
    monkeypatch.setattr(update_mode, "run_subprocess", lambda *a, **k: _Result(0))
    monkeypatch.setattr(update_mode, "_installed_version", lambda: syncade.__version__)
    monkeypatch.setattr(update_mode, "_fetch", lambda url: {"latest": syncade.__version__})
    monkeypatch.setattr(update_mode, "_resync_skills", lambda out: False)

    assert update_mode.run_update(cwd=tmp_path) == WORKTREE_ERROR
    assert "already up to date" in capsys.readouterr().err


def test_a_critical_advisory_with_no_newer_release_is_still_current(
    tmp_path, monkeypatch, capsys
) -> None:
    """`_is_newest` must compare VERSIONS, never delegate to `evaluate()`.

    `evaluate()` answers "should the operator see a notice?", which is a different question and
    only usually gives the same answer. In the documented critical-no-fix state — `critical_below`
    above the installed version, no newer release published — it returns a notice while the
    operator is genuinely current. Reading its None-ness therefore told a fully up-to-date install
    that its upgrade had failed and it was probably pinned. Unanimous dogfood blocker; it survived
    a producer round because the previous fix repaired the malformed-manifest instance while
    keeping the same near-miss delegation.
    """
    _upgrade_reporting_success(monkeypatch, tmp_path)
    monkeypatch.setattr(update_mode, "_installed_version", lambda: syncade.__version__)
    monkeypatch.setattr(
        update_mode,
        "_fetch",
        lambda url: {
            "latest": syncade.__version__,
            "critical_below": "99.0.0",
            "critical_reason": "a critical fix with no newer release",
        },
    )

    assert update_mode.run_update() == SUCCESS
    out = capsys.readouterr().err
    assert "already up to date" in out
    assert "pins a version" not in out


def test_is_newest_answers_the_version_question_across_every_manifest_shape(monkeypatch) -> None:
    """One table, so a future edit cannot fix one shape and reopen another — which is exactly how
    this function reached its third revision."""
    installed = "0.7.0"
    cases = [
        ({"latest": "0.7.0"}, True, "manifest agrees"),
        ({"latest": "0.6.3"}, True, "manifest is BEHIND the install"),
        ({"latest": "0.7.0", "critical_below": "99.0.0"}, True, "critical, no newer release"),
        ({"latest": "99.0.0"}, False, "a newer release exists"),
        (None, False, "unreachable"),
        ({}, False, "no latest key"),
        ({"latest": None}, False, "latest is null"),
        ({"latest": ""}, False, "latest is empty"),
        ({"latest": "garbage"}, False, "latest unparseable"),
        ({"latest": 5}, False, "latest not a string"),
        ({"latest": "1.2"}, False, "latest not three components"),
    ]
    for manifest, expected, why in cases:
        monkeypatch.setattr(update_mode, "_fetch", lambda url, m=manifest: m)
        assert update_mode._is_newest(installed) is expected, why


def test_is_newest_cannot_certify_an_unparseable_installed_version(monkeypatch) -> None:
    monkeypatch.setattr(update_mode, "_fetch", lambda url: {"latest": "0.7.0"})
    assert update_mode._is_newest("not-a-version") is False


def test_the_probe_is_isolated_from_ambient_interpreter_state(monkeypatch) -> None:
    """`-I` is load-bearing: without it the probe inherits PYTHONPATH and the user site dir.

    A `sitecustomize` on that path runs INSIDE the answer — an atexit hook printing a version
    number becomes the last stdout line, so a no-op upgrade reads as a move. Reproduced live: a
    hostile PYTHONPATH turned the probe's `"0.7.0"` into `"0.7.0\\n99.0.0"`. Syncade ships such a
    shim itself for worktree imports, so this is not purely adversarial.

    `-I` suppresses PYTHONPATH but NOT the venv's own site-packages, where a `sitecustomize.py`
    can still register atexit hooks. The probe must call `os._exit(0)` to bypass atexit entirely —
    the only guarantee against a venv-local hook appending to stdout after the version line.
    `os.write` is asserted alongside it: `os._exit` skips stdio flushing, so `print()` would lose
    the version; the unbuffered syscall guarantees delivery before the process exits.

    Pinned as flag assertions because the behavioural twin below cannot run the failure case
    without installing a real adversarial sitecustomize.
    """
    seen: list[list[str]] = []
    monkeypatch.setattr(
        update_mode,
        "run_subprocess",
        lambda cmd, **kw: (
            seen.append(cmd)
            or SubprocessResult(returncode=0, stdout="1.2.3\n", stderr="", duration_seconds=0.0)
        ),
    )
    update_mode._installed_version()
    assert seen and "-I" in seen[0], (
        "the version probe must run isolated; without -I a sitecustomize on PYTHONPATH can "
        "append a version number to its stdout and fake a successful upgrade"
    )
    assert seen[0].index("-I") < seen[0].index("-c"), "-I must precede -c to take effect"
    c_arg = seen[0][seen[0].index("-c") + 1]
    assert "os._exit(0)" in c_arg, (
        "the probe must call os._exit(0) to bypass atexit handlers; a venv-local "
        "sitecustomize can register atexit hooks that append a fake version line after the real one"
    )
    assert "os.write" in c_arg, (
        "the probe must use os.write (unbuffered) not print; os._exit skips stdio flushing "
        "so print output would be lost before reaching the pipe"
    )


def test_the_isolated_probe_still_resolves_the_real_install() -> None:
    """The behavioural twin: `-I` must not break what the probe is FOR.

    It drops PYTHONPATH and the user site dir but keeps the environment's own site-packages, so
    `importlib.metadata` still finds the package. Runs the real subprocess — a flag assertion
    alone would happily pin an isolation level that returns None for everyone.

    We assert the result is a parseable, non-None version, NOT that it equals
    `syncade.__version__`. In a dev worktree the source version and the installed distribution
    version legitimately differ (e.g. 0.7.0 in source, 0.1.0 in site-packages); asserting
    equality would make this test prove the ambient installation, not the probe's behaviour.
    """
    from syncade.update_check import _parse

    result = update_mode._installed_version()
    assert result is not None, (
        "the probe must find the installed package under -I "
        "(drops PYTHONPATH but keeps the environment's own site-packages)"
    )
    assert _parse(result) is not None, f"result {result!r} must be a parseable version string"
