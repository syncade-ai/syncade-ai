"""`syncade --config` read/write paths (pr-v2-30): list + get + set + layer provenance."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from syncade import config_loader
from syncade.cli import main
from syncade.cli.config_keys import InvalidValue, coerce
from syncade.config import SyncadeConfig


def _make(tmp_path: Path, *, global_toml: str = "", repo_toml: str | None = None):
    g = tmp_path / "global.toml"
    g.write_text(global_toml)
    repo = tmp_path / "repo"
    repo.mkdir()
    if repo_toml is not None:
        (repo / ".syncade").mkdir()
        (repo / ".syncade" / "config.toml").write_text(repo_toml)
    return g, repo


def _use_global(monkeypatch, path: Path):
    # Override the autouse isolation fixture with a real (temp) global file for this test.
    monkeypatch.setattr(config_loader, "_default_global_config_path", lambda: path)


def _git_init(repo: Path) -> None:
    # `--config set --repo` now requires a real git repo (else exit 60); tests that write the repo
    # layer must make the dir a git repo.
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)


def _line_for(out: str, key: str) -> str:
    return next(ln for ln in out.splitlines() if f"[{key}]" in ln)


def test_list_shows_settings_all_default(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)  # empty global, no repo config -> defaults
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "(default)" in _line_for(out, "loop.max_rounds")
    assert "producer.model" in out and "synthesizer.model" in out


def test_list_attributes_global_and_repo_layers(tmp_path, monkeypatch, capsys):
    g, repo = _make(
        tmp_path,
        global_toml="[loop]\nmax_rounds = 2\n",
        repo_toml="[loop]\ntimeout_seconds = 600\n",
    )
    _git_init(repo)  # a repo layer only participates inside a real git repo
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "(global)" in _line_for(out, "loop.max_rounds")  # set only in the global layer
    assert "(repo)" in _line_for(out, "loop.timeout_seconds")  # set only in the repo layer
    assert "(default)" in _line_for(out, "loop.budget_usd")  # set in neither


def test_get_prints_resolved_value(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path, global_toml="[loop]\nmax_rounds = 1\n")
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "get", "loop.max_rounds"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "1"


def test_get_reviewer_by_index(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "get", "reviewers.0.model"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "gpt-5.5"  # default roster


def test_get_unknown_key_is_usage_error(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    assert main(["--repo-root", str(repo), "--config", "get", "no.such.key"]) == 2
    assert "unknown key" in capsys.readouterr().err


def test_get_requires_exactly_one_key(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    assert main(["--repo-root", str(repo), "--config", "get"]) == 2  # no key given
    assert "exactly one" in capsys.readouterr().err


def test_get_rejects_non_schema_attributes(tmp_path, monkeypatch, capsys):
    """--config get must reject non-schema paths (methods, dunders, negative indices)."""
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    for bad_key in ("producer.model_dump", "__dict__", "loop.model_fields", "reviewers.-1.model"):
        rc = main(["--repo-root", str(repo), "--config", "get", bad_key])
        assert rc == 2, f"expected rc=2 for key {bad_key!r}, got {rc}"
        assert "unknown key" in capsys.readouterr().err


def test_bad_verb_is_usage_error(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    assert main(["--repo-root", str(repo), "--config", "bogus"]) == 2
    assert "unknown verb" in capsys.readouterr().err


def test_menu_requires_a_terminal(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    # no verb -> the TUI; under pytest stdin/stdout are not TTYs, so it degrades, never crashes.
    assert main(["--repo-root", str(repo), "--config"]) == 60
    assert "interactive terminal is required" in capsys.readouterr().err


# --- set (2b) ------------------------------------------------------------------------------------


def test_set_writes_global_and_round_trips(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)  # g does not exist yet
    _use_global(monkeypatch, g)
    assert main(["--repo-root", str(repo), "--config", "set", "loop.max_rounds", "1"]) == 0
    capsys.readouterr()
    assert main(["--repo-root", str(repo), "--config", "get", "loop.max_rounds"]) == 0
    assert capsys.readouterr().out.strip() == "1"  # written value reads back
    assert "max_rounds = 1" in g.read_text()  # in the GLOBAL file
    assert not (repo / ".syncade" / "config.toml").exists()  # repo untouched


def test_set_repo_targets_repo_file(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)
    _git_init(repo)
    _use_global(monkeypatch, g)
    assert (
        main(["--repo-root", str(repo), "--config", "set", "loop.max_rounds", "2", "--repo"]) == 0
    )
    assert "max_rounds = 2" in (repo / ".syncade" / "config.toml").read_text()
    assert g.read_text() == ""  # global untouched by --repo


def test_set_provider_materializes_and_rederives_model(tmp_path, monkeypatch, capsys):
    """set <actor>.provider re-derives the model in the same write — a consistent pair, never
    provider=openai + an inherited claude model."""
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    assert main(["--repo-root", str(repo), "--config", "set", "producer.provider", "openai"]) == 0
    text = g.read_text()
    assert 'provider = "openai"' in text
    expected = SyncadeConfig.model_validate({"producer": {"provider": "openai"}}).producer.model
    assert f'model = "{expected}"' in text and "claude" not in text


def test_set_rejects_invalid_value_and_leaves_file_untouched(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    main(["--repo-root", str(repo), "--config", "set", "loop.max_rounds", "2"])  # seed a valid file
    before = g.read_bytes()
    capsys.readouterr()
    rc = main(["--repo-root", str(repo), "--config", "set", "loop.max_rounds", "99"])  # > le=10
    assert rc == 50
    assert "file unchanged" in capsys.readouterr().err
    assert g.read_bytes() == before  # byte-identical: NEVER wrote a broken file


def test_set_global_invalid_is_not_masked_by_repo(tmp_path, monkeypatch, capsys):
    """A bad GLOBAL value is rejected even when a repo override would mask it in the merge — the
    file must be valid on its own, else it breaks the moment it's used without that repo."""
    g, repo = _make(tmp_path, repo_toml="[loop]\nmax_rounds = 2\n")  # repo would win the merge
    _git_init(repo)  # so the repo mask is real (loaded), not silently absent
    _use_global(monkeypatch, g)
    assert main(["--repo-root", str(repo), "--config", "set", "loop.max_rounds", "99"]) == 50
    assert g.read_text() == ""  # global not written despite the repo mask


def test_set_bad_key_and_bad_type(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    assert main(["--repo-root", str(repo), "--config", "set", "loop.bogus", "1"]) == 2  # bad KEY
    assert "unknown field" in capsys.readouterr().err
    # a bad VALUE is a config error (exit 50), matching the schema-invalid path + the docs/skills
    assert main(["--repo-root", str(repo), "--config", "set", "loop.max_rounds", "abc"]) == 50
    assert "not a valid value" in capsys.readouterr().err


def test_set_rejects_offprefix_cross_provider_models(tmp_path, monkeypatch, capsys):
    """The pair guard catches OFF-prefix models (o3, opus), not just gpt-*/claude-* (dogfood ①)."""
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    assert (
        main(["--repo-root", str(repo), "--config", "set", "producer.provider", "anthropic"]) == 0
    )
    capsys.readouterr()
    assert main(["--repo-root", str(repo), "--config", "set", "producer.model", "o3"]) == 50
    assert "openai" in capsys.readouterr().err  # o3 recognized as OpenAI's despite no gpt- prefix
    assert main(["--repo-root", str(repo), "--config", "set", "synthesizer.model", "opus"]) == 50
    assert "anthropic" in capsys.readouterr().err  # opus recognized as Anthropic's


def test_set_allows_genuine_custom_model(tmp_path, monkeypatch, capsys):
    """An off-map custom model string is allowed (the adapter validates it at dispatch)."""
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "set", "producer.model", "my-fine-tune-v3"])
    assert rc == 0 and "my-fine-tune-v3" in g.read_text()


def test_set_repo_outside_git_repo_refuses(tmp_path, monkeypatch, capsys):
    """--repo in a NON-git dir would fabricate a config no run reads → exit 60, nothing written."""
    g, repo = _make(tmp_path)  # not a git repo
    _use_global(monkeypatch, g)
    assert (
        main(["--repo-root", str(repo), "--config", "set", "loop.max_rounds", "2", "--repo"]) == 60
    )
    assert "not inside a git repository" in capsys.readouterr().err
    assert not (repo / ".syncade" / "config.toml").exists()


def test_global_set_does_not_materialize_repo_section(tmp_path, monkeypatch):
    """A GLOBAL edit materializes an absent section from defaults+global, NOT the repo layer — else
    the current repo's provider/model leaks into ~/.syncade (dogfood finding). Concretely: the repo
    pins the OTHER provider; a global model edit must keep the DEFAULT provider, not inherit it."""
    default = SyncadeConfig().producer
    other = "openai" if default.provider == "anthropic" else "anthropic"
    other_model = {"openai": "gpt-5.5", "anthropic": "claude-sonnet-4-6"}[other]
    edit_model = "claude-opus-4-8" if default.provider == "anthropic" else "o3"
    g, repo = _make(
        tmp_path, repo_toml=f'[producer]\nprovider = "{other}"\nmodel = "{other_model}"\n'
    )
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "set", "producer.model", edit_model])
    assert rc == 0, "a global edit must not inherit the repo's cross-provider producer"
    written = tomllib.loads(g.read_text())
    assert written["producer"]["provider"] == default.provider  # default, NOT the repo's `other`
    assert written["producer"]["model"] == edit_model


def test_list_get_outside_git_repo_ignore_cwd_config(tmp_path, monkeypatch, capsys):
    """Outside a git repo, `--config` inspects the CURRENT state: cwd/.syncade/config.toml is NOT a
    repo layer (no git repo → no repo layer), so it neither shows as `repo` nor bleeds into the
    effective config. A note warns of the divergence rather than pretending the file cannot exist
    (dogfood finding F3). A review run with --allow-auto-init would `git init` and consume the file;
    without it, a non-empty dir (like this one) is refused — either way there is a divergence."""
    g = tmp_path / "global.toml"
    g.write_text("")
    _use_global(monkeypatch, g)
    workdir = tmp_path / "notarepo"  # a non-git dir that happens to hold a .syncade/config.toml
    (workdir / ".syncade").mkdir(parents=True)
    (workdir / ".syncade" / "config.toml").write_text("[loop]\nmax_rounds = 1\n")
    assert main(["--repo-root", str(workdir), "--config", "list"]) == 0
    cap = capsys.readouterr()
    assert "(repo)" not in cap.out  # nothing attributed to a repo layer that --config didn't read
    assert "not a git repo" in cap.err  # the divergence note fires (honest, not silent)
    # `get` resolves the effective value: the default 5, NOT the phantom file's 1; note on stderr.
    assert main(["--repo-root", str(workdir), "--config", "get", "loop.max_rounds"]) == 0
    cap = capsys.readouterr()
    assert cap.out.strip() == "5"  # stdout stays the clean scriptable value
    assert "not a git repo" in cap.err  # get warns too (finding named get + list)


def test_no_ignored_config_note_when_absent(tmp_path, monkeypatch, capsys):
    """The F3 divergence note fires ONLY when a non-git cwd actually holds a .syncade/config.toml —
    a plain non-git dir (nothing to diverge) stays quiet."""
    g = tmp_path / "global.toml"
    g.write_text("")
    _use_global(monkeypatch, g)
    workdir = tmp_path / "empty-nonrepo"
    workdir.mkdir()
    assert main(["--repo-root", str(workdir), "--config", "list"]) == 0
    assert "not a git repo" not in capsys.readouterr().err  # no phantom file → no note


def test_set_atomic_write_error_is_controlled(tmp_path, monkeypatch, capsys):
    """A bad global path (parent is a file) → controlled exit 60, not an uncaught traceback."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    monkeypatch.setattr(
        config_loader, "_default_global_config_path", lambda: blocker / "config.toml"
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    assert main(["--repo-root", str(repo), "--config", "set", "loop.max_rounds", "2"]) == 60
    assert "cannot write" in capsys.readouterr().err


def test_config_rejects_loop_only_safety_flags(tmp_path, monkeypatch, capsys):
    """--config rejects --force-dirty / --allow-default-branch (dogfood ④)."""
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    assert main(["--repo-root", str(repo), "--config", "list", "--force-dirty"]) == 2
    assert "cannot be combined with --force-dirty" in capsys.readouterr().err
    assert main(["--repo-root", str(repo), "--config", "list", "--allow-default-branch"]) == 2
    assert "cannot be combined with --allow-default-branch" in capsys.readouterr().err


def test_set_model_rejects_cross_provider_pair(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    # anthropic provider + OpenAI model → exit 50, file untouched
    rc = main(["--repo-root", str(repo), "--config", "set", "producer.provider", "anthropic"])
    assert rc == 0
    capsys.readouterr()
    rc = main(["--repo-root", str(repo), "--config", "set", "producer.model", "gpt-5.5"])
    assert rc == 50
    err = capsys.readouterr().err
    assert "openai" in err and "anthropic" in err
    # The file must be unchanged (gpt-5.5 must not appear in global config)
    assert "gpt-5.5" not in g.read_text()


def test_set_reviewer_by_index_and_out_of_range(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    assert main(["--repo-root", str(repo), "--config", "set", "reviewers.0.model", "gpt-x"]) == 0
    assert "gpt-x" in g.read_text()  # roster materialized, index 0 model set
    capsys.readouterr()
    assert main(["--repo-root", str(repo), "--config", "set", "reviewers.9.model", "x"]) == 2
    assert "out of range" in capsys.readouterr().err


def test_set_reviewer_provider_rederives_model(tmp_path, monkeypatch):
    """set reviewers.0.provider re-derives the paired model — never leaves an OpenAI model
    when switching a reviewer to Anthropic (or vice-versa)."""
    import tomllib

    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    assert (
        main(["--repo-root", str(repo), "--config", "set", "reviewers.0.provider", "anthropic"])
        == 0
    )
    parsed = tomllib.loads(g.read_text())
    r0 = parsed["reviewers"][0]
    assert r0["provider"] == "anthropic"
    assert "claude-" in r0["model"]  # an Anthropic model was derived
    assert "gpt-" not in r0["model"]  # never an OpenAI model for an Anthropic reviewer

    # Reverse: switching back to openai re-derives an OpenAI model.
    assert (
        main(["--repo-root", str(repo), "--config", "set", "reviewers.0.provider", "openai"]) == 0
    )
    parsed2 = tomllib.loads(g.read_text())
    r0b = parsed2["reviewers"][0]
    assert r0b["provider"] == "openai"
    assert "gpt-" in r0b["model"]
    assert "claude-" not in r0b["model"]


# --- arity / argument guards (regression) -------------------------------------------------------


def test_list_rejects_trailing_operand(tmp_path, monkeypatch, capsys):
    """`--config list <extra>` must exit 2 — trailing args are not silently ignored.

    Before the fix, nargs='*' captured the extra arg into config_args and list dispatched
    without checking arity, so `syncade --config list docs/pr.md` silently succeeded.
    """
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list", "docs/pr.md"])
    assert rc == 2
    assert capsys.readouterr().err  # some error message emitted


def test_set_reviewer_unknown_provider_rejected(tmp_path, monkeypatch, capsys):
    """set reviewers.<i>.provider with an unregistered provider must exit 50 and leave the file
    unchanged — a config with an unknown reviewer provider cannot dispatch to any real adapter."""
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    before = g.read_bytes()
    rc = main(["--repo-root", str(repo), "--config", "set", "reviewers.0.provider", "fakeprovider"])
    assert rc == 50
    err = capsys.readouterr().err
    assert "unknown reviewer provider" in err
    assert "fakeprovider" in err
    assert g.read_bytes() == before  # file must be unchanged


def test_set_repo_negative_value_reaches_validation(tmp_path, monkeypatch, capsys):
    """``--config set key <negative> --repo`` must fail exit 50 (schema), not exit 2 (argparse).

    A negative integer matches argparse's negative-number matcher, so `--config` (nargs="*")
    consumes `-1` as a value rather than an unknown option; validation then rejects it (>= 1).
    """
    g, repo = _make(tmp_path)
    _git_init(repo)
    _use_global(monkeypatch, g)
    # Negative max_rounds fails pydantic (must be >= 1), so exit 50, not argparse exit 2.
    rc = main(["--repo-root", str(repo), "--config", "set", "loop.max_rounds", "-1", "--repo"])
    assert rc == 50, f"expected 50 (schema error), got {rc}"
    assert not (repo / ".syncade" / "config.toml").exists()  # file never written


def test_set_repo_negative_float_reaches_validation(tmp_path, monkeypatch, capsys):
    """Same as above for a negative float value (budget_usd -0.5)."""
    g, repo = _make(tmp_path)
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "set", "loop.budget_usd", "-0.5", "--repo"])
    assert rc == 50, f"expected 50 (schema error), got {rc}"


def test_set_malformed_target_section_returns_exit_50(tmp_path, monkeypatch, capsys):
    """A malformed target section must return exit 50 with file unchanged, not crash.

    When [loop] in the global file is a scalar (not a table) and the repo layer has a valid
    [loop] that masks it, the effective load succeeds — but _apply raises TypeError when it
    tries to mutate the malformed scalar. Without the fix this propagates as an uncaught
    exception; after the fix it's caught and returns exit 50 with the file unchanged.
    """
    g, repo = _make(tmp_path)
    _git_init(repo)
    # Malformed global: loop is a string, not a table
    g.write_text('loop = "broken"\n')
    # Repo masks it with a valid loop section so the effective load succeeds
    (repo / ".syncade").mkdir(exist_ok=True)
    (repo / ".syncade" / "config.toml").write_text("[loop]\nmax_rounds = 2\n")
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "set", "loop.max_rounds", "3"])
    assert rc == 50, "malformed target section must exit 50, not crash"
    assert g.read_text() == 'loop = "broken"\n'  # file must be unchanged


def test_set_accepts_dash_prefixed_model_value(tmp_path, monkeypatch):
    """--config set must accept model strings that begin with '-' (custom fine-tune names).
    argparse's nargs="*" stops at dash-prefixed tokens; operands are extracted from raw argv."""
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "set", "producer.model", "-my-fine-tune-v1"])
    assert rc == 0, "dash-prefixed model value must be accepted as a custom model string"
    content = tomllib.loads(g.read_text())
    assert content["producer"]["model"] == "-my-fine-tune-v1"


def test_set_rejects_repo_prefix_form(tmp_path, monkeypatch, capsys):
    """--repo must trail the key/value (suffix-only per D1); the prefix form is a usage error."""
    g, repo = _make(tmp_path)
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--repo", "--config", "set", "loop.max_rounds", "2"])
    assert rc == 2, "prefix --repo form must be rejected with exit 2"
    err = capsys.readouterr().err
    assert "trail" in err or "--repo" in err  # must explain the ordering constraint
    # File must not have been written.
    assert not (repo / ".syncade" / "config.toml").exists()


def test_set_missing_value_before_repo_flag_is_rejected(tmp_path, monkeypatch, capsys):
    """--config set producer.model --repo (missing value, --repo in the value slot) must be
    rejected as a missing-value error, not silently write model='--repo' to the global file."""
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    # --repo is the suffix target flag; without a value it occupies the value slot.
    rc = main(["--repo-root", str(repo), "--config", "set", "producer.model", "--repo"])
    assert rc != 0, "missing value before --repo must be rejected"
    # Global file must not have been created or mutated.
    assert not g.exists() or g.read_text() == "", "global config must not have been written"
    # Verify the file was not written with '--repo' as the model value.
    if g.exists() and g.read_text():
        import tomllib

        data = tomllib.loads(g.read_text())
        assert data.get("producer", {}).get("model") != "--repo", (
            "model='--repo' must never be persisted"
        )


@pytest.mark.parametrize("flag", ["--force-dirty", "--quiet", "--resume", "--allow-default-branch"])
def test_set_missing_value_any_double_dash_flag_rejected(tmp_path, monkeypatch, capsys, flag):
    """Any --flag in the value slot of --config set must be rejected, not persisted as a model name.

    The prior fix only guarded --repo; other known CLI flags were silently
    consumed and written to the config file unchanged.
    """
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "set", "producer.model", flag])
    assert rc != 0, f"missing value with {flag} in value slot must be rejected"
    assert not g.exists() or g.read_text() == "", "global config must not have been written"
    if g.exists() and g.read_text():
        import tomllib

        data = tomllib.loads(g.read_text())
        assert data.get("producer", {}).get("model") != flag, (
            f"model='{flag}' must never be persisted"
        )


# --- pr-v2-31 Increment 2: schema-driven set over the whole config surface ---


def test_set_new_scalar_field_materializes_paired_section(tmp_path, monkeypatch, capsys):
    """producer.thinking is now settable; a paired section absent from the file is materialized
    whole (provider/model pair preserved), then the field is set."""
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "set", "producer.thinking", "high"])
    assert rc == 0
    data = tomllib.loads(g.read_text())
    assert data["producer"]["thinking"] == "high"
    assert data["producer"]["provider"] and data["producer"]["model"]  # pair preserved


def test_coerce_bool_accepts_the_documented_spellings():
    """`review.include_producer_summary` was the schema's ONLY bool field; PR-h-03 item 7
    deleted it as a dead knob. `coerce`'s bool arm survives — it is what stops the next bool
    field from reading `"false"` as True — so it is now exercised directly instead of
    through a field that no longer exists."""
    for raw in ("true", "TRUE", "yes", "1", "on"):
        assert coerce(bool, raw) is True, raw
    for raw in ("false", "FALSE", "no", "0", "off"):
        assert coerce(bool, raw) is False, raw


def test_set_keymerge_int_field(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "set", "gc.keep", "20"])
    assert rc == 0
    assert tomllib.loads(g.read_text())["gc"]["keep"] == 20


def test_set_list_field_comma_split(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    rc = main(
        [
            "--repo-root",
            str(repo),
            "--config",
            "set",
            "review.strip_repo_context_files",
            "A.md,B.md",
        ]
    )
    assert rc == 0
    assert tomllib.loads(g.read_text())["review"]["strip_repo_context_files"] == ["A.md", "B.md"]


def test_set_top_level_scalar_worktree_base(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "set", "worktree_base", "/tmp/syncade-alt"])
    assert rc == 0
    assert tomllib.loads(g.read_text())["worktree_base"] == "/tmp/syncade-alt"


def test_set_bad_literal_exit_50_file_untouched(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path, global_toml="")
    _use_global(monkeypatch, g)
    before = g.read_bytes()
    rc = main(["--repo-root", str(repo), "--config", "set", "producer.thinking", "bogus"])
    assert rc == 50
    assert g.read_bytes() == before  # never wrote a broken file


def test_set_check_severity_by_index(tmp_path, monkeypatch, capsys):
    """checks is a second list-of-models section: an existing element's scalar edits by index,
    leaving the rest of the element intact."""
    g, repo = _make(
        tmp_path, repo_toml='[[checks]]\nname = "x"\ncommand = "true"\nseverity = "blocking"\n'
    )
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(
        ["--repo-root", str(repo), "--config", "set", "checks.0.severity", "advisory", "--repo"]
    )
    assert rc == 0
    data = tomllib.loads((repo / ".syncade" / "config.toml").read_text())
    assert data["checks"][0]["severity"] == "advisory"
    assert data["checks"][0]["name"] == "x"  # rest of the element preserved


def test_coerce_bool_rejects_a_non_boolean():
    """The exit-50 half of the deleted bool-field test. A bare `bool(raw)` would have
    accepted "maybe" as True — the trap this arm exists to prevent."""
    with pytest.raises(InvalidValue):
        coerce(bool, "maybe")


def test_set_optional_field_cleared_by_empty(tmp_path, monkeypatch, capsys):
    # budget_usd now rejects empty (opt-out is explicit 0); test_command still clears via empty.
    g, repo = _make(tmp_path, global_toml='[loop]\ntest_command = "pytest"\n')
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "set", "loop.test_command", ""])
    assert rc == 0
    assert "test_command" not in tomllib.loads(g.read_text()).get("loop", {})


def test_set_reviewer_permissions_safe_rejected(tmp_path, monkeypatch, capsys):
    """--config set reviewers.<i>.permissions safe is rejected (exit 50) — 'safe' is out of the
    reviewer's runnable bounds; the file is untouched."""
    g, repo = _make(tmp_path, global_toml="")
    _use_global(monkeypatch, g)
    before = g.read_bytes()
    rc = main(["--repo-root", str(repo), "--config", "set", "reviewers.0.permissions", "safe"])
    assert rc == 50
    assert g.read_bytes() == before


def test_set_repo_reviewer_name_collides_with_global_test_command(tmp_path, monkeypatch, capsys):
    """Cross-layer: repo reviewer 'tests' + global test_command collide on merged config."""
    g, repo = _make(tmp_path, global_toml="[loop]\ntest_command = 'pytest'\n")
    _git_init(repo)
    _use_global(monkeypatch, g)
    repo_cfg = repo / ".syncade" / "config.toml"
    before = repo_cfg.read_bytes() if repo_cfg.exists() else b""
    rc = main(["--repo-root", str(repo), "--config", "set", "reviewers.0.name", "tests", "--repo"])
    assert rc == 50
    assert (repo_cfg.read_bytes() if repo_cfg.exists() else b"") == before


def test_set_global_reviewer_name_collides_with_repo_check_name(tmp_path, monkeypatch, capsys):
    """Cross-layer: global reviewer 'lint' + repo check 'lint' collide on merged config."""
    g, repo = _make(
        tmp_path, global_toml="", repo_toml="[[checks]]\nname = 'lint'\ncommand = 'true'\n"
    )
    _git_init(repo)
    _use_global(monkeypatch, g)
    before = g.read_bytes()
    rc = main(["--repo-root", str(repo), "--config", "set", "reviewers.0.name", "lint"])
    assert rc == 50
    assert g.read_bytes() == before
