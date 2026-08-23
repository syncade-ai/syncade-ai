"""`--config set` must be able to repair a config that no longer LOADS (PR-h-05).

Found while a schema change (producer ``yolo`` -> ``confined``) briefly invalidated every config
ever written against a shipped release. That narrowing was reverted — ``yolo`` is a supported
opt-out again — but the deadlock it exposed is REAL and independent of it: ``run_config`` returned
50 on a ConfigError BEFORE dispatching the verb, so the one command documented to repair an
invalid config was refused BY the invalid config, and hand-editing TOML was the only way out of
ANY config-invalidating change. With the offending value in BOTH layers the merged-config check
then made each layer un-repairable because of the other.

The fixtures below use an out-of-range ``loop.max_rounds`` rather than the value that prompted the
work: the bug is about repairing an invalid config, not about which key made it invalid, and
pinning it to a live schema value would retire the test the next time that value moves.

Split from the at-cap ``test_config_mode.py``; reuses its fixtures.
"""

from __future__ import annotations

import tomllib

from syncade.cli import main
from tests.cli.test_config_mode import _git_init, _make, _use_global

# --- `set` must be able to repair a config that no longer loads (PR-h-05) -------------------
#
# A schema change that retires a released value (producer `yolo` -> `confined`) makes every
# existing config fail to load. `run_config` used to return 50 on that ConfigError BEFORE
# dispatching the verb, so the documented repair command was itself refused and a text editor
# was the only way out. Worse, with the same offending value in BOTH layers the merged-config
# check made each layer un-repairable because of the other.

# max_rounds ceiling is 10; 99 fails the schema in any layer.
_BROKEN = "[loop]\nmax_rounds = 99\n"


def test_set_repairs_a_config_that_fails_to_load(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path, global_toml=_BROKEN)
    _use_global(monkeypatch, g)
    monkeypatch.chdir(repo)

    assert main(["--config", "list"]) == 50  # every other verb still fails closed
    assert main(["--config", "set", "loop.max_rounds", "5"]) == 0
    assert tomllib.loads(g.read_text())["loop"]["max_rounds"] == 5
    assert main(["--config", "list"]) == 0


def test_set_repairs_each_layer_when_both_are_broken(tmp_path, monkeypatch, capsys):
    """Neither layer may be held hostage by the other: repair must work in EITHER order.

    The merged global+repo check is a NON-REGRESSION check — it rejects only errors the edit
    introduces — so the still-broken other layer cannot block this one.
    """
    g, repo = _make(tmp_path, global_toml=_BROKEN, repo_toml=_BROKEN)
    _use_global(monkeypatch, g)
    _git_init(repo)
    monkeypatch.chdir(repo)

    # Global first, while the repo layer is still invalid.
    assert main(["--config", "set", "loop.max_rounds", "5"]) == 0
    assert tomllib.loads(g.read_text())["loop"]["max_rounds"] == 5
    # ...and the operator is told the repair is not finished.
    assert "still invalid elsewhere" in capsys.readouterr().err

    assert main(["--config", "set", "loop.max_rounds", "5", "--repo"]) == 0
    repo_toml = tomllib.loads((repo / ".syncade" / "config.toml").read_text())
    assert repo_toml["loop"]["max_rounds"] == 5
    assert "still invalid elsewhere" not in capsys.readouterr().err
    assert main(["--config", "list"]) == 0


def test_set_still_refuses_an_edit_that_would_break_the_merge(tmp_path, monkeypatch, capsys):
    """Relaxing the merged check to non-regression must not let a NEW cross-layer break through."""
    g, repo = _make(tmp_path, global_toml="", repo_toml="")
    _use_global(monkeypatch, g)
    _git_init(repo)
    monkeypatch.chdir(repo)

    assert main(["--config", "set", "loop.max_rounds", "99"]) == 50
    assert g.read_text() == ""
