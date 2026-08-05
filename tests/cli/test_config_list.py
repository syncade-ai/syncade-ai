"""`config_list.all_rows` — the full-surface enumeration behind `--config list --all` (pr-v2-32)."""

from __future__ import annotations

from pathlib import Path

from syncade import config_loader
from syncade.cli import config_keys, config_list, main
from syncade.config import SyncadeConfig


def _rows(config=None):
    return config_list.all_rows(config or SyncadeConfig())


def test_every_enumerated_key_is_settable():
    """--all ⊆ --config set: every key it emits resolves through the same schema walk `set` uses.
    (Attack claim 1 — the two surfaces cannot drift.)"""
    for key, _label, _section, _subkey in _rows():
        config_keys.resolve_annotation(key)  # raises UnknownKey if it isn't a settable scalar


def test_covers_advanced_cold_and_cli_only_fields():
    keys = {key for key, *_ in _rows()}
    for expected in (
        "producer.thinking",
        "producer.api_key_env",  # CLI-only, but settable → present in --all
        "reviewers.0.model",
        "reviewers.1.template",
        "synthesizer.model",
        "loop.max_rounds",
        "loop.budget_tokens",  # CLI-only knob
        "review.strip_repo_context_files",  # a list-of-scalar leaf
        "retry.max_retries",
        "gc.keep",
        "drafter.model",  # cold actor
        "auditor.provider",
        "worktree_base",  # top-level scalar
    ):
        assert expected in keys, f"{expected} not enumerated"


def test_dict_roster_is_not_enumerated():
    """`pricing.models` is a dict roster — not settable, so it must never appear as a row."""
    keys = {key for key, *_ in _rows()}
    assert not any(k.startswith("pricing.models") for k in keys)


def test_subkey_is_per_field_for_keymerge_and_none_for_wholesale():
    by_key = {key: (section, subkey) for key, _l, section, subkey in _rows()}
    # loop is key-merge → subkey is the field (per-key layer attribution)
    assert by_key["loop.max_rounds"] == ("loop", "max_rounds")
    # producer / reviewers are wholesale → subkey is None (section-granular)
    assert by_key["producer.thinking"] == ("producer", None)
    assert by_key["reviewers.0.model"] == ("reviewers", None)
    # top-level scalar → None
    assert by_key["worktree_base"] == ("worktree_base", None)


def test_roster_size_follows_the_effective_config():
    cfg = SyncadeConfig.model_validate(
        {"checks": [{"name": "lint", "command": "true"}, {"name": "fmt", "command": "true"}]}
    )
    keys = {key for key, *_ in _rows(cfg)}
    assert "checks.0.name" in keys and "checks.1.severity" in keys  # both check elements enumerated
    assert "checks.2.name" not in keys  # not beyond the roster


def test_labels_are_section_relative():
    by_key = {key: label for key, label, *_ in _rows()}
    assert by_key["loop.max_rounds"] == "max_rounds"
    assert by_key["reviewers.0.model"] == "0.model"
    assert by_key["worktree_base"] == "worktree_base"


def _independent_keys(model_cls, instance, prefix=""):
    """A SEPARATE structural walk of the settable surface (iterating model_fields directly), so a
    missed/extra branch in `all_rows` shows up as a set difference. Claim-1 regression guard."""
    from typing import get_args, get_origin

    out = set()
    for name, field in model_cls.model_fields.items():
        ann, key, val = field.annotation, f"{prefix}{name}", getattr(instance, name)
        if config_keys._settable_scalar(ann):
            out.add(key)
        elif config_keys._is_model(ann):
            out |= _independent_keys(ann, val, f"{key}.")
        elif get_origin(ann) is list:
            elems = [a for a in get_args(ann) if a is not type(None)]
            if elems and config_keys._is_model(elems[0]):
                for i, item in enumerate(val):
                    out |= _independent_keys(elems[0], item, f"{key}.{i}.")
    return out


def test_all_rows_equals_independent_enumeration():
    """--all ⊇ set (nothing dropped) AND ⊆ set (nothing extra), across a non-trivial roster."""
    cfg = SyncadeConfig.model_validate(
        {"checks": [{"name": "a", "command": "true"}, {"name": "b", "command": "true"}]}
    )
    assert {k for k, *_ in _rows(cfg)} == _independent_keys(SyncadeConfig, cfg)


# --- `syncade --config list --all` CLI integration ---


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
    monkeypatch.setattr(config_loader, "_default_global_config_path", lambda: path)


def _git_init(repo: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)


def _line_for(out: str, key: str) -> str:
    return next(ln for ln in out.splitlines() if f"[{key}]" in ln)


def test_list_all_shows_full_surface(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)  # defaults
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list", "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    for key in (
        "producer.thinking",
        "retry.max_retries",
        "gc.keep",
        "review.strip_repo_context_files",
        "drafter.model",  # cold actor (CLI-only in the TUI)
        "loop.budget_tokens",  # CLI-only loop knob
    ):
        assert f"[{key}]" in out, f"{key} missing from --all output"


def test_list_plain_omits_advanced(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[retry.max_retries]" not in out  # curated list stays curated
    assert "[loop.max_rounds]" in out


def test_list_all_overrides_note_honest(tmp_path, monkeypatch, capsys):
    """The `overrides` note fires iff repo wins AND global explicitly sets a DIFFERENT value."""
    g, repo = _make(
        tmp_path,
        global_toml="[loop]\ntimeout_seconds = 2400\nmax_rounds = 5\n",
        repo_toml="[loop]\ntimeout_seconds = 1800\nmax_rounds = 5\n",
    )
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list", "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "(repo — overrides global 2400.0)" in _line_for(out, "loop.timeout_seconds")
    assert "overrides" not in _line_for(out, "loop.max_rounds")  # agree (both 5) → no note
    assert "(default)" in _line_for(out, "loop.budget_usd")  # global never set it


def test_list_all_shows_configured_checks(tmp_path, monkeypatch, capsys):
    g, repo = _make(
        tmp_path,
        repo_toml="[[checks]]\nname = 'file-length'\ncommand = 'true'\nseverity = 'blocking'\n",
    )
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list", "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[advanced.checks]" in out
    assert "file-length" in _line_for(out, "checks.0.name")


def test_list_all_multiline_value_is_single_line(tmp_path, monkeypatch, capsys):
    """Multiline scalar values (e.g. test_command with embedded newlines) must not split a row."""
    g, repo = _make(
        tmp_path,
        repo_toml='[loop]\ntest_command = "step1\\nstep2\\nstep3"\n',
    )
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list", "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    line = _line_for(out, "loop.test_command")
    # The value must appear on a single physical line — no raw newlines in the row
    assert "\n" not in line.rstrip("\n")
    # The escaped form must be present so the value isn't silently dropped
    assert "\\n" in line


def test_list_all_multiline_model_value_is_single_line(tmp_path, monkeypatch, capsys):
    """A multiline model string must not split the `provider / model` row across physical lines."""
    g, repo = _make(
        tmp_path,
        repo_toml='[producer]\nmodel = "gpt-5\\nspecial"\n',
    )
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list", "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    line = _line_for(out, "producer.model")
    assert "\n" not in line.rstrip("\n")
    assert "\\n" in line


def test_list_all_escapes_cr_and_tab(tmp_path, monkeypatch, capsys):
    """--all must escape ALL control chars (not just \\n) — a CR/TAB would split/misalign a row."""
    g, repo = _make(tmp_path, repo_toml='[loop]\ntest_command = "a\\rb\\tc"\n')
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list", "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    line = _line_for(out, "loop.test_command")
    assert "\r" not in line and "\t" not in line  # no raw control chars in the row
    assert "\\r" in line and "\\t" in line  # both escaped readably


def test_plain_list_does_not_escape_control_chars(tmp_path, monkeypatch, capsys):
    """Byte-compat (Attack claim 3): the escaping lives in --all ONLY. Bare `--config list` renders
    a multiline model RAW, exactly as before the increment — never `\\n`-escaped."""
    g, repo = _make(tmp_path, repo_toml='[producer]\nmodel = "gpt-5\\nX"\n')
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list"])  # NO --all
    out = capsys.readouterr().out
    assert rc == 0
    assert "\\n" not in out  # curated list is NOT escaped
    assert "gpt-5\nX" in out  # the raw newline is present (pre-increment bytes)


def test_list_all_override_note_survives_masked_invalid_global_section(
    tmp_path, monkeypatch, capsys
):
    """Override notes must appear even when the global file's [[reviewers]] is invalid on its own
    but the repo layer replaces it wholesale — a common hand-edited-global scenario."""
    # Global: valid [loop] with timeout_seconds=2400, BUT invalid [[reviewers]] (duplicate name).
    # On its own, SyncadeConfig.model_validate(global_raw) raises ValidationError.
    g, repo = _make(
        tmp_path,
        global_toml=(
            "[loop]\ntimeout_seconds = 2400\n"
            "[[reviewers]]\nname = 'r1'\nprovider = 'openai'\nmodel = 'gpt-5.5'\n"
            "[[reviewers]]\nname = 'r1'\nprovider = 'openai'\nmodel = 'gpt-5.5'\n"
        ),
        repo_toml=(
            "[loop]\ntimeout_seconds = 1800\n"
            "[[reviewers]]\nname = 'r1'\nprovider = 'openai'\nmodel = 'gpt-5.5'\n"
        ),
    )
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list", "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    # The override note must appear despite the global file failing standalone validation.
    assert "(repo — overrides global 2400.0)" in _line_for(out, "loop.timeout_seconds")


def test_list_all_masked_invalid_producer_reports_raw_values(tmp_path, monkeypatch, capsys):
    """Masked invalid [producer] must show actual global raw values, not schema defaults.

    When the global file has an invalid [producer] (bad provider) but the repo layer replaces it
    wholesale, the effective config is valid. The override note for producer.model must reflect the
    actual raw global values, not pydantic-defaulted anthropic/claude-sonnet-4-6.
    """
    g, repo = _make(
        tmp_path,
        global_toml=(
            "[producer]\nprovider = 'not-a-provider'\nmodel = 'bad-global-model'\n"
            "[loop]\ntimeout_seconds = 2400\n"
        ),
        repo_toml=(
            "[producer]\nprovider = 'openai'\nmodel = 'gpt-5.5'\n[loop]\ntimeout_seconds = 1800\n"
        ),
    )
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list", "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    producer_line = _line_for(out, "producer.model")
    # Must show the actual raw global value, not the schema default (anthropic / claude-sonnet-4-6)
    assert "not-a-provider / bad-global-model" in producer_line
    assert "anthropic" not in producer_line
    # Scalar note for loop still works alongside the masked invalid section
    assert "(repo — overrides global 2400.0)" in _line_for(out, "loop.timeout_seconds")


def test_list_all_masked_reviewer_roster_longer_than_effective_no_crash(
    tmp_path, monkeypatch, capsys
):
    """No IndexError when the effective reviewer roster is shorter than the raw global roster.

    Global has 2 reviewers; repo replaces with 1 reviewer. all_rows() iterates over 1 effective
    reviewer. Override note for reviewers.0.model must be computed without crashing.
    """
    g, repo = _make(
        tmp_path,
        global_toml=(
            "[[reviewers]]\nname = 'r1'\nprovider = 'openai'\nmodel = 'gpt-5.5'\n"
            "[[reviewers]]\nname = 'r2'\nprovider = 'openai'\nmodel = 'gpt-5.5'\n"
            "[loop]\ntimeout_seconds = 2400\n"
        ),
        repo_toml=(
            "[[reviewers]]\nname = 'r1'\nprovider = 'openai'\nmodel = 'gpt-5.6-sol'\n"
            "[loop]\ntimeout_seconds = 1800\n"
        ),
    )
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list", "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    # reviewer 0 model differs: repo gpt-5.6-sol vs global gpt-5.5 → override note
    r0_line = _line_for(out, "reviewers.0.model")
    assert "openai / gpt-5.5" in r0_line  # the actual global value
    assert "(repo — overrides global" in r0_line
    # scalar note still fires
    assert "(repo — overrides global 2400.0)" in _line_for(out, "loop.timeout_seconds")


def test_list_all_override_note_for_model_when_global_omits_provider(tmp_path, monkeypatch, capsys):
    """Override note fires for .model rows when global sets model but not provider.

    A valid global layer may explicitly set [synthesizer] model without provider (relying on the
    schema default). When the repo replaces the section, the note must still appear using the
    default provider in the display string.
    """
    g, repo = _make(
        tmp_path,
        global_toml="[synthesizer]\nmodel = 'gpt-5.6-sol'\n",
        repo_toml="[synthesizer]\nprovider = 'anthropic'\nmodel = 'claude-sonnet-4-6'\n",
    )
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list", "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    line = _line_for(out, "synthesizer.model")
    assert "overrides global" in line, "override note must fire when global explicitly sets model"
    assert "openai / gpt-5.6-sol" in line, "default provider must appear when global omits it"


def test_list_all_override_note_for_list_valued_scalar(tmp_path, monkeypatch, capsys):
    """Override note for list[str] fields shows the actual global value faithfully.

    Pre-fix, _raw_shown fed str(['AGENTS.md', 'CLAUDE.md']) back through CSV coercion,
    producing a mangled display. With the fix the already-decoded list is used directly.
    """
    g, repo = _make(
        tmp_path,
        global_toml='[review]\nstrip_repo_context_files = ["AGENTS.md", "CLAUDE.md"]\n',
        repo_toml='[review]\nstrip_repo_context_files = ["README.md"]\n',
    )
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list", "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    line = _line_for(out, "review.strip_repo_context_files")
    assert "overrides global" in line, "override note must fire for list-valued scalar"
    # The correct repr is the standard list str: ['AGENTS.md', 'CLAUDE.md'].
    # The pre-fix mangled version was ["['AGENTS.md'", "'CLAUDE.md']"] — double-nested brackets.
    assert "['AGENTS.md', 'CLAUDE.md']" in line, "global list must appear faithfully in note"


def test_list_all_non_git_drops_repo_layer(tmp_path, monkeypatch, capsys):
    g, repo = _make(tmp_path, repo_toml="[loop]\ntimeout_seconds = 1800\n")  # not git-init'd
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list", "--all"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "(repo)" not in captured.out  # nothing attributed to a repo layer
    assert "not a git repo" in captured.err  # the ignored-config note still fires


def test_list_all_reviewer_model_override_does_not_fabricate_provider(
    tmp_path, monkeypatch, capsys
):
    """Full-PR dogfood R3: a masked global reviewer that sets `model` but omits the REQUIRED
    `provider` must NOT invent a provider in the override note (reviewers have no schema provider
    default) — show the bare model the global layer actually set."""
    g, repo = _make(
        tmp_path,
        global_toml="[[reviewers]]\nmodel = 'gpt-5.6-sol'\n",  # sets model, omits required provider
        repo_toml="[[reviewers]]\nname = 'r'\nprovider = 'openai'\nmodel = 'gpt-5.5'\n",
    )
    _git_init(repo)
    _use_global(monkeypatch, g)
    rc = main(["--repo-root", str(repo), "--config", "list", "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    line = _line_for(out, "reviewers.0.model")
    assert "overrides global gpt-5.6-sol" in line  # the bare model global actually set
    assert "openai / gpt-5.6-sol" not in line  # NOT a fabricated provider
