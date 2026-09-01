"""``worktree_base`` config field (PR-v2-9): where per-run git worktrees are provisioned.

A single top-level ``SyncadeConfig`` value (Q5 — not a ``[worktree]`` block). Covers the schema
default (reproduces the runtime default), TOML load, and round-trip. The ``--worktree-base`` CLI
override lives in ``tests/cli/test_config_overrides.py``; that a run and ``--doctor`` honor it lives
in ``tests/cli/test_config_overrides.py`` and ``tests/doctor/test_doctor.py``."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from syncade.config import SyncadeConfig
from syncade.config_loader import load_config
from syncade.worktree import DEFAULT_WORKTREE_BASE


@pytest.mark.real_worktree_base
def test_default_reproduces_the_runtime_default():
    """Zero-config runs still provision worktrees under ``/tmp/syncade`` — byte-identical."""
    assert SyncadeConfig().worktree_base == DEFAULT_WORKTREE_BASE


def test_round_trips_losslessly():
    want = Path("/fast/disk/syncade")
    cfg = SyncadeConfig.model_validate({"worktree_base": "/fast/disk/syncade"})
    assert cfg.worktree_base == want
    assert SyncadeConfig.model_validate(cfg.model_dump()).worktree_base == want


def test_worktree_base_loads_from_toml(tmp_path):
    """The user path: ``worktree_base = "..."`` at the top level of the TOML reaches the config."""
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        'worktree_base = "/mnt/fast/syncade"\n', encoding="utf-8"
    )
    assert load_config(tmp_path).worktree_base == Path("/mnt/fast/syncade")


def test_embedded_nul_is_rejected_at_config_load():
    """An embedded NUL in worktree_base must raise ValidationError at config load, not crash at
    the OS call site with an uncaught ValueError."""
    with pytest.raises(ValidationError, match="NUL"):
        SyncadeConfig.model_validate({"worktree_base": "/tmp/bad\x00path"})


# --- B03: one setting, two spellings, one destination -----------------------


def _via_cli(raw: str) -> Path:
    """What ``--worktree-base <raw>`` resolves to."""
    from argparse import Namespace

    from syncade.cli.config_overrides import apply_worktree_base_override

    return apply_worktree_base_override(SyncadeConfig(), Namespace(worktree_base=raw)).worktree_base


@pytest.mark.parametrize("raw", ["~/scratch-syncade", "~"])
def test_a_tilde_means_the_same_thing_on_both_paths(raw: str) -> None:
    """``~`` expanded on the CLI and left literal in the file is two destinations.

    A config file saying ``worktree_base = "~/scratch"`` created a directory literally
    NAMED ``~`` under the process cwd, while the identical value passed as
    ``--worktree-base`` reached the home directory. Same setting, same spelling,
    different disk.
    """
    from_file = SyncadeConfig.model_validate({"worktree_base": raw}).worktree_base

    assert from_file == Path(raw).expanduser()
    assert from_file.is_absolute()
    assert from_file == _via_cli(raw), "the two spellings must name one destination"


@pytest.mark.parametrize("raw", ["wt", "./wt", "../wt", ""])
def test_a_relative_base_is_refused_rather_than_silently_cwd_relative(raw: str) -> None:
    """A relative base resolves against wherever syncade happened to be invoked.

    The same config then provisions into different places from different directories,
    and GC's ownership machinery keys on the base — a hazard already recorded one
    layer down, where a relative base produced ``base/base/<run-id>``.
    """
    with pytest.raises(ValidationError, match="absolute"):
        SyncadeConfig.model_validate({"worktree_base": raw})


def test_the_cli_refuses_a_relative_base_too() -> None:
    """The CLI override must not be the way around the rule the file obeys.

    Pinned as ``OverrideError`` — which the modes map to exit 50 — NOT as a bare
    ``ValueError``. The first version of this test asserted the exception TYPE and
    passed while ``syncade --gc --worktree-base wt`` printed a traceback and exited 1,
    because nothing converted the normalizer's pydantic-flavoured error at three of the
    four call sites. A test that pins an exception the operator never sees is a test
    that passes against the bug.
    """
    from syncade.cli.config_overrides import OverrideError

    with pytest.raises(OverrideError, match="absolute"):
        _via_cli("wt")


# --- B03 edge: unresolvable ~user path is a config error, not a traceback ----

_UNRESOLVABLE_USER = "~_syncade_no_such_user_xyzzy"


def test_unresolvable_tilde_user_path_raises_validation_error() -> None:
    """``~user`` for a non-existent user must produce ValidationError, not RuntimeError.

    ``Path.expanduser()`` raises ``RuntimeError`` for an unresolvable ``~user``
    spelling; if that is not caught in the normalizer, it escapes through Pydantic
    as a traceback instead of a config error.  Covers config-file load and the
    ``--config set`` validation path, both of which call ``SyncadeConfig.model_validate``.
    """
    with pytest.raises(ValidationError):
        SyncadeConfig.model_validate({"worktree_base": f"{_UNRESOLVABLE_USER}/x"})


def test_unresolvable_tilde_user_path_on_cli_is_an_override_error() -> None:
    """The CLI override must surface the same unresolvable-user failure as exit 50.

    ``apply_worktree_base_override`` converts the normalizer's ``ValueError`` into
    ``OverrideError`` (a ``ConfigError`` subclass); the modes map that to exit 50.
    """
    from syncade.cli.config_overrides import OverrideError

    with pytest.raises(OverrideError):
        _via_cli(f"{_UNRESOLVABLE_USER}/x")


def test_a_bad_worktree_base_override_is_a_config_error_everywhere() -> None:
    """Every mode that handles a bad config file also handles a bad override.

    Four call sites apply ``--worktree-base`` and only one used to catch
    ``OverrideError``; the inheritance is what makes the other three correct without
    each remembering a second handler.
    """
    from syncade.cli.config_overrides import OverrideError
    from syncade.config_loader import ConfigError

    assert issubclass(OverrideError, ConfigError)


def test_a_nul_is_refused_in_a_path_object_too() -> None:
    """The NUL guard is about the VALUE, not about how it was spelled.

    It tested `isinstance(value, str)` while the normalizer accepts `Path` as well,
    so `SyncadeConfig.model_validate({"worktree_base": Path("/tmp/bad\\0path")})`
    validated cleanly and `create_run_dir` later raised an uncaught ValueError from
    `mkdir`. The operator's TOML and CLI paths are string-based and were never
    affected; this is the direct-API surface, which the library exposes.
    """
    with pytest.raises(ValidationError, match="NUL"):
        SyncadeConfig.model_validate({"worktree_base": Path("/tmp/bad\x00path")})
