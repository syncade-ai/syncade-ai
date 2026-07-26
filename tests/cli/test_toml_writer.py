"""cli/toml_writer.dumps is round-trip-correct through stdlib tomllib (pr-v2-30 Issue 2b)."""

from __future__ import annotations

import tomllib

import pytest

from syncade import config_loader
from syncade.cli import main
from syncade.cli.toml_writer import dumps


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"loop": {"max_rounds": 2, "timeout_seconds": 1800.0}},
        {"producer": {"provider": "openai", "model": "gpt-5.5"}, "loop": {"max_rounds": 1}},
        {
            "reviewers": [
                {"name": "a", "provider": "openai", "model": "gpt-5.5"},
                {"name": "b", "provider": "anthropic", "model": "claude-opus-4-6"},
            ]
        },
        # nested table + a dotted key (the [pricing.models."gpt-5.5"] case):
        {"pricing": {"models": {"gpt-5.5": {"input": 1.25, "output": 10.0}}}},
        {"worktree_base": "/tmp/syncade", "loop": {"budget_usd": 5.0}},  # root scalar before tables
        {"loop": {"test_command": 'echo "hi"\tthere'}},  # string needing escapes
        {"gc": {"enabled": True, "keep": 20}},  # bool + int
        {"review": {"strip_repo_context_files": ["AGENTS.md", "CLAUDE.md"]}},  # scalar list
        {"review": {"strip_repo_context_files": []}},  # empty scalar list
    ],
)
def test_round_trip(data):
    assert tomllib.loads(dumps(data)) == data


def test_none_is_omitted():
    parsed = tomllib.loads(dumps({"loop": {"max_rounds": 2, "budget_usd": None}}))
    assert parsed == {"loop": {"max_rounds": 2}}  # None dropped (TOML has no null)


def test_mixed_list_rejected():
    with pytest.raises(ValueError, match="mixed"):
        dumps({"reviewers": [{"provider": "openai"}, "not-a-dict"]})


# --- pr-v2-34: render() preserves comments/formatting on write ---

from syncade.cli.toml_writer import render  # noqa: E402

COMMENTED = """# Syncade self-review config. First dogfood: PR-8.
#
# Producer permissions = "yolo" because headless commits need it.

[loop]
max_rounds = 3          # the round ceiling
timeout_seconds = 1800

# Reverted to gpt-5.5 @ xhigh (PR-29) — 94 rounds behind it.
[producer]
provider = "anthropic"
model = "claude-sonnet-4-6"

[[reviewers]]
name = "codex-reviewer"   # the plain prompt
thinking = "xhigh"

[[reviewers]]
name = "codex-reviewer-adv"
thinking = "xhigh"
"""


def _edited(mutate):
    data = tomllib.loads(COMMENTED)
    mutate(data)
    out = render(data, COMMENTED)
    assert tomllib.loads(out) == data, "render must produce EXACTLY the intended data"
    return out


def test_value_change_keeps_every_comment():
    out = _edited(lambda d: d["loop"].__setitem__("max_rounds", 5))
    assert out.count("#") == COMMENTED.count("#")
    assert "# Syncade self-review config. First dogfood: PR-8." in out
    assert "# Reverted to gpt-5.5 @ xhigh (PR-29)" in out
    assert "max_rounds = 5          # the round ceiling" in out  # trailing comment survives
    assert "timeout_seconds = 1800" in out  # untouched sibling unchanged


def test_array_of_tables_value_change_targets_the_right_element():
    out = _edited(lambda d: d["reviewers"][1].__setitem__("thinking", "high"))
    assert out.count("#") == COMMENTED.count("#")
    assert "# the plain prompt" in out
    # only the SECOND reviewer changed
    assert out.index('thinking = "xhigh"') < out.index('thinking = "high"')


def test_no_op_write_is_byte_identical():
    assert render(tomllib.loads(COMMENTED), COMMENTED) == COMMENTED


def test_added_key_lands_in_its_table_and_keeps_comments():
    out = _edited(lambda d: d["loop"].__setitem__("budget_usd", 5.0))
    assert out.count("#") == COMMENTED.count("#")
    loop_block = out[out.index("[loop]") : out.index("[producer]")]
    assert "budget_usd = 5.0" in loop_block  # inserted into [loop], not appended elsewhere


def test_added_table_is_appended_without_touching_existing_text():
    out = _edited(lambda d: d.__setitem__("retry", {"max_retries": 4}))
    assert out.count("#") == COMMENTED.count("#")
    assert out.startswith(COMMENTED.rstrip("\n").split("\n")[0])  # original head intact
    assert "[retry]" in out and "max_retries = 4" in out


def test_removed_key_deletes_only_its_line():
    out = _edited(lambda d: d["producer"].pop("provider"))
    assert out.count("#") == COMMENTED.count("#")
    assert "provider =" not in out
    assert 'model = "claude-sonnet-4-6"' in out


def test_new_file_falls_back_to_dumps():
    data = {"loop": {"max_rounds": 2}}
    assert render(data, "") == dumps(data)


@pytest.mark.parametrize(
    "text",
    [
        '[review]\nfiles = [\n  "CLAUDE.md",  # a\n  "AGENTS.md",\n]\n',  # multi-line array
        "[loop]\nmax_rounds = 3\n[pricing]\nmodels = { a = 1 }\n",  # inline table
        'producer.provider = "openai"\nproducer.model = "gpt-5.5"\n',  # dotted keys
        '[loop]\ntest_command = "pytest # not a comment"\nmax_rounds = 3\n',  # '#' in a string
        '[loop]\ntest_command = "a=b"\nmax_rounds = 3\n',  # '=' in a string
        '[loop]\ntest_command = """\nx\ny\n"""\nmax_rounds = 3\n',  # multi-line string
        "[loop]\ntest_command = 'C:\\path'\nmax_rounds = 3\n",  # literal string
        "[loop]\nmax_rounds = 3",  # no trailing newline
    ],
)
def test_never_writes_data_other_than_intended(text):
    """The safety net: whatever the patcher does, the written text must parse to EXACTLY the
    intended data — falling back to a full rewrite when a surgical patch can't be verified."""
    data = tomllib.loads(text)
    for mutated in ({**data, "added_key": "x"}, data):
        assert tomllib.loads(render(mutated, text)) == mutated
    # and with a leaf changed
    changed = tomllib.loads(text)
    for section in changed.values():
        if isinstance(section, dict):
            for k, v in section.items():
                if isinstance(v, str):
                    section[k] = "MUTATED"
                    break
            break
    assert tomllib.loads(render(changed, text)) == changed


# --- pr-v2-34 judge findings: each of these silently destroyed comments or content ---


def test_none_leaf_does_not_force_the_comment_destroying_rewrite():
    """F1 (severe): `_apply` materializes an absent section from `model_dump()`, which carries
    None optionals. TOML has no null and `dumps` omits None, so verifying against the RAW data was
    unsatisfiable and every such write fell back to the full rewrite. Data built with None here on
    purpose — every other test builds it via `tomllib.loads`, which can never produce one."""
    src = "# KEEP ME\n[loop]\nmax_rounds = 3      # three is plenty\n"
    data = tomllib.loads(src)
    data["producer"] = {"provider": "anthropic", "thinking": "high", "api_key_env": None}
    out = render(data, src)
    assert tomllib.loads(out) == {
        "loop": {"max_rounds": 3},
        "producer": {"provider": "anthropic", "thinking": "high"},
    }
    assert "# KEEP ME" in out and "# three is plenty" in out


def test_clearing_an_optional_removes_its_line_and_keeps_comments():
    """F1b: `--config set <key> ""` sets None — that must delete the key, not rewrite the file."""
    src = '# hdr\n[producer]\nprovider = "openai"\napi_key_env = "K"   # my key var\n'
    data = tomllib.loads(src)
    data["producer"]["api_key_env"] = None
    out = render(data, src)
    assert tomllib.loads(out) == {"producer": {"provider": "openai"}}
    assert "# hdr" in out and "api_key_env" not in out


def test_escaped_quote_in_old_value_does_not_graft_a_fragment_onto_the_line():
    """F3: a naive quote scan treated `\\"` as closing the string, so the `#` hunt ran in the wrong
    state and part of the OLD value was left behind as a permanent comment."""
    src = '[loop]\ntest_command = "grep -q \\"#\\" f && pytest"   # KEEPME\n'
    data = tomllib.loads(src)
    data["loop"]["test_command"] = "pytest -q"
    out = render(data, src)
    line = next(ln for ln in out.splitlines() if "test_command" in ln)
    assert tomllib.loads(out) == data
    assert "# KEEPME" in line
    assert "&& pytest" not in line  # no fragment of the old value survives


def test_new_table_block_ends_with_a_newline_and_one_blank_line():
    """F4: the appended block left the file with no trailing newline and a doubled blank line."""
    out = render({"loop": {"max_rounds": 3}, "gc": {"keep": 5}}, "# keep\n[loop]\nmax_rounds = 3\n")
    assert out.endswith("\n") and "\n\n\n" not in out
    assert tomllib.loads(out) == {"loop": {"max_rounds": 3}, "gc": {"keep": 5}}


def test_crlf_file_keeps_crlf_on_the_edited_line():
    """F5: the rebuilt line dropped its trailing \\r, leaving one LF line in a CRLF file."""
    out = render({"a": {"x": 5, "y": 2}}, "# H\r\n[a]\r\nx = 1\r\ny = 2\r\n")
    assert out.count("\r") == 4  # every line still CRLF
    assert tomllib.loads(out) == {"a": {"x": 5, "y": 2}}


def test_dumps_does_not_reparent_a_scalar_array_into_a_sub_table():
    """F6: a scalar array ordered after a dict was emitted AFTER the sub-table header, silently
    moving it inside that sub-table — `dumps` was not round-trip-correct as claimed."""
    data = {"t": {"sub": {"x": 1}, "arr": ["a"]}}
    assert tomllib.loads(dumps(data)) == data


def test_value_change_keeps_trailing_comment_column_aligned():
    """Replacing a value with a different-width rendering must keep the # at the same column, not
    preserve the old whitespace literally (which shifts the # left or right)."""
    src = "[loop]\nmax_rounds = 3          # the round ceiling\n"
    out = _edited(lambda d: d["loop"].__setitem__("max_rounds", 300))
    # # must still be at the original column (column 24, 0-indexed)
    line = next(ln for ln in out.splitlines() if "max_rounds" in ln)
    hash_col = line.index("#")
    assert hash_col == src.splitlines()[1].index("#"), (
        f"# shifted from column {src.splitlines()[1].index('#')} to {hash_col}: {line!r}"
    )
    assert "# the round ceiling" in line


def test_add_into_a_header_only_table_does_not_duplicate_the_header():
    """F7: a table whose keys are all commented out had no insertion anchor, so a second `[loop]`
    header was appended — invalid TOML, so the whole file was rewritten and comments lost."""
    src = "# HEADER\n[loop]\n# max_rounds = 3   (commented out by the operator)\n"
    out = render({"loop": {"max_rounds": 5}}, src)
    assert tomllib.loads(out) == {"loop": {"max_rounds": 5}}
    assert "# HEADER" in out and "# max_rounds = 3" in out


def test_crlf_inserted_key_uses_crlf():
    """Keys added to an existing table in a CRLF file must use CRLF, not bare LF."""
    src = "# H\r\n[a]\r\nx = 1\r\n"
    out = render({"a": {"x": 1, "y": 2}}, src)
    assert tomllib.loads(out) == {"a": {"x": 1, "y": 2}}
    assert "\n" not in out.replace("\r\n", ""), f"bare LF in output: {out!r}"


def test_crlf_new_table_uses_crlf():
    """New tables appended to a CRLF file must use CRLF on every inserted line."""
    src = "# H\r\n[a]\r\nx = 1\r\n"
    out = render({"a": {"x": 1}, "b": {"y": 2}}, src)
    assert tomllib.loads(out) == {"a": {"x": 1}, "b": {"y": 2}}
    assert "\n" not in out.replace("\r\n", ""), f"bare LF in output: {out!r}"


def test_clearing_already_absent_optional_is_byte_identical():
    """Clearing a never-set optional (e.g. --config set loop.budget_usd '') materializes
    {'loop': {'budget_usd': None}}. After stripping None AND the resulting empty sub-table,
    the wanted data matches the file exactly and render must return the file byte-for-byte."""
    src = '# hdr\n[producer]\nprovider = "openai"\n'
    data = {"loop": {"budget_usd": None}, "producer": {"provider": "openai"}}
    out = render(data, src)
    assert out == src


def test_header_only_table_render_is_byte_identical():
    """A section with only comments and no keys parses as {}. render() with the same parsed data
    must return the original bytes unchanged — not fall back to dumps() and lose the comments."""
    src = '# top\n[gc]\n# gc is fully commented out\n[producer]\nprovider = "openai"\n'
    import tomllib as _tl

    data = _tl.loads(src)  # {'gc': {}, 'producer': {'provider': 'openai'}}
    out = render(data, src)
    assert out == src


def test_last_key_removal_preserves_section_header_and_comments():
    """Removing the only key from a section makes the table empty. The section header and
    any comments must survive in the file — render() must not rewrite via dumps()."""
    src = "[loop]\n# optional ceiling\nbudget_usd = 5.0\n"
    out = render({"loop": {}}, src)
    # scalar is gone, but header and comment survive
    assert "budget_usd" not in out
    assert "[loop]" in out
    assert "# optional ceiling" in out


def test_header_only_table_plus_absent_optional_clear_is_byte_identical():
    """Clearing an absent optional when the file has a header-only [loop] table must not
    destroy the file: _strip_none drops the empty loop dict, but the file has [loop] in it.
    The verification must not misread that as a mismatch and fall back to dumps()."""
    src = '# cfg\n[loop]\n# no keys here\n[producer]\nprovider = "openai"\n'
    import tomllib as _tl

    base = _tl.loads(src)  # {'loop': {}, 'producer': {'provider': 'openai'}}
    # simulate clearing an absent optional inside loop
    data = {**base, "loop": {**base["loop"], "budget_usd": None}}
    out = render(data, src)
    assert out == src


def test_root_key_add_to_comments_only_file_preserves_comments():
    """Adding a root-level key to a config that contains only comments (no table headers, no keys)
    dropped the inserted key: tail[1:] ate the key itself (no leading blank to remove when root
    keys are inserted without a table separator). The file then verified-failed and fell back to
    dumps(), silently destroying the comments."""
    src = "# top comment\n# second comment\n"
    data = {"worktree_base": "/tmp/syncade"}
    out = render(data, src)
    assert tomllib.loads(out) == data, "root key must be present after insert"
    assert "# top comment" in out, "existing comments must survive"
    assert "# second comment" in out, "existing comments must survive"
    assert "worktree_base" in out


def test_control_characters_in_strings_produce_valid_toml():
    """_fmt_scalar only escaped \\, ", \\n, \\t, \\r — leaving \\b, \\f, DEL, and other C0
    controls raw, which TOML forbids in basic strings. The written file then failed to parse."""
    data = {"producer": {"api_key_env": "val\x01\x07\x0b\x1f\x7f"}}
    result = dumps(data)
    parsed = tomllib.loads(result)  # would raise TOMLDecodeError before the fix
    assert parsed == data

    # Named escapes \\b and \\f must also be handled
    data2 = {"producer": {"api_key_env": "a\x08b\x0cc"}}
    assert tomllib.loads(dumps(data2)) == data2

    # render() path calls the same _fmt_scalar via _render_value
    src = '[producer]\napi_key_env = "old"\n'
    out = render({"producer": {"api_key_env": "new\x01value"}}, src)
    assert tomllib.loads(out) == {"producer": {"api_key_env": "new\x01value"}}


def test_bool_and_int_one_are_distinct_toml_types():
    """Python True == 1; TOML `true` and `1` are different types. render() must not treat them
    as equal — in the no-op check, the changed-leaf detection, or the final verification."""
    # int 1 in file, bool True in data → must patch to `true`
    src = "[loop]\nmax_rounds = 1\n"
    out = render({"loop": {"max_rounds": True}}, src)
    assert tomllib.loads(out) == {"loop": {"max_rounds": True}}
    line = next(ln for ln in out.splitlines() if "max_rounds" in ln)
    assert "max_rounds = true" in line

    # bool true in file, int 1 in data → must patch to `1`
    src2 = "[loop]\nmax_rounds = true\n"
    out2 = render({"loop": {"max_rounds": 1}}, src2)
    assert tomllib.loads(out2) == {"loop": {"max_rounds": 1}}
    line2 = next(ln for ln in out2.splitlines() if "max_rounds" in ln)
    assert "max_rounds = 1" in line2 and "true" not in line2


# --- I/O-layer tests: CRLF preservation and integer-spelling no-op (via full CLI path) ---


def _global_config(tmp_path, monkeypatch, text: str | bytes):
    """Write ``text`` as the global config and return its path + a repo dir."""
    g = tmp_path / "global.toml"
    if isinstance(text, bytes):
        g.write_bytes(text)
    else:
        g.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config_loader, "_default_global_config_path", lambda: g)
    repo = tmp_path / "repo"
    repo.mkdir()
    return g, repo


def test_set_preserves_crlf_line_endings(tmp_path, monkeypatch):
    """_existing_text must not normalize CRLF; a CRLF config must stay CRLF after --config set."""
    g, repo = _global_config(tmp_path, monkeypatch, b"[gc]\r\nkeep = 5\r\n# note\r\n")
    assert main(["--repo-root", str(repo), "--config", "set", "gc.keep", "10"]) == 0
    b = g.read_bytes()
    assert b"\r\n" in b and b"\r\r\n" not in b


def test_mixed_ending_file_bare_lf_lines_unchanged(tmp_path, monkeypatch):
    """A config with mixed LF/CRLF line endings must not have its bare-LF lines converted.

    Previously _patch detected any \\r\\n and ran a global re.sub that also converted
    untouched bare-LF lines to CRLF, violating the preservation contract.
    """
    # Mixed file: first line CRLF, second line bare LF, third line CRLF
    src = b"[gc]\r\nkeep = 5\r\n# bare-lf comment\nmax_age_days = 30\r\n"
    g, repo = _global_config(tmp_path, monkeypatch, src)
    assert main(["--repo-root", str(repo), "--config", "set", "gc.keep", "10"]) == 0
    result = g.read_bytes()
    # The bare-LF comment line must NOT have been converted to CRLF
    assert b"# bare-lf comment\n" in result, f"bare-LF line was converted: {result!r}"
    # The CRLF lines must still be CRLF
    assert b"[gc]\r\n" in result, f"CRLF header was lost: {result!r}"
    # Only the targeted key changed
    assert b"keep = 10" in result


def test_materialized_aot_section_appended_without_rewrite(tmp_path, monkeypatch):
    """Editing reviewers.0.thinking when the file has no [[reviewers]] block must append
    the full [[reviewers]] roster without falling back to the comment-destroying dumps() rewrite.

    Previously _patch returned None for any added table path containing a digit, so config_mode's
    roster materialization always triggered the full rewrite and silently dropped comments.
    """
    src = "# KEEP ME\n[loop]\nmax_rounds = 3\n"
    data = {
        "loop": {"max_rounds": 3},
        "reviewers": [
            {"name": "r1", "provider": "openai", "model": "gpt-5.5", "thinking": "high"},
        ],
    }
    out = render(data, src)
    assert tomllib.loads(out) == data, "rendered file must parse to the intended data"
    assert "# KEEP ME" in out, "existing comment must survive"
    assert "[[reviewers]]" in out
    assert 'thinking = "high"' in out


def test_materialized_multi_element_aot_section_appended_in_order(tmp_path, monkeypatch):
    """Multiple [[reviewers]] elements materialized into a bare config appear in index order."""
    src = "# hdr\n[loop]\nmax_rounds = 3\n"
    data = {
        "loop": {"max_rounds": 3},
        "reviewers": [
            {"name": "a", "provider": "openai", "model": "gpt-5.5"},
            {"name": "b", "provider": "anthropic", "model": "claude-sonnet-4-6"},
        ],
    }
    out = render(data, src)
    assert tomllib.loads(out) == data
    assert "# hdr" in out
    # element 0 must appear before element 1
    assert out.index('name = "a"') < out.index('name = "b"')


def test_set_float_field_noop_preserves_integer_spelling(tmp_path, monkeypatch):
    """Integer-spelled float field (1800) must not be rewritten as 1800.0 on a no-op set."""
    g, repo = _global_config(tmp_path, monkeypatch, "[loop]\ntimeout_seconds = 1800\n")
    assert main(["--repo-root", str(repo), "--config", "set", "loop.timeout_seconds", "1800"]) == 0
    assert "1800.0" not in g.read_text()


# --- pr-v2-34 second dogfood: byte-perfect spacing / line-ending edges ---


def test_noncanonical_spacing_around_equals_is_preserved():
    """R4: an edit must not collapse `=   3` to `= 3` or pad `=3` to `= 3` — the operator's exact
    whitespace around `=` is byte-preserved (value width unchanged → line byte-identical bar it)."""
    src = "[loop]\ntimeout_seconds   =   1800   # note\n"
    data = tomllib.loads(src)
    data["loop"]["timeout_seconds"] = 2400
    assert render(data, src) == "[loop]\ntimeout_seconds   =   2400   # note\n"
    src2 = "[loop]\nmax_rounds=3\n"
    d2 = tomllib.loads(src2)
    d2["loop"]["max_rounds"] = 5
    assert render(d2, src2) == "[loop]\nmax_rounds=5\n"


def test_comment_column_held_when_value_width_changes():
    """R0: when the replacement is a different width, keep `#` at its original COLUMN."""
    src = "[loop]\nmax_rounds = 3          # the ceiling\n"
    data = tomllib.loads(src)
    data["loop"]["max_rounds"] = 100
    line = next(ln for ln in render(data, src).splitlines() if "max_rounds" in ln)
    assert line.index("#") == src.splitlines()[1].index("#")  # # held at its column


def test_crlf_file_without_trailing_newline_add_keeps_comments_and_crlf():
    """R4: CRLF file with no final newline — an EOF add keeps comments and stays all-CRLF, never
    leaving a bare CR/LF (the failure of the old \\r-in-line model)."""
    src = "# hdr\r\n[loop]\r\nmax_rounds = 3"  # no trailing newline
    data = tomllib.loads(src)
    data["gc"] = {"keep": 5}
    out = render(data, src)
    assert tomllib.loads(out) == data
    assert "# hdr" in out
    assert "\n" not in out.replace("\r\n", "")  # every newline is CRLF; no bare LF or CR
    assert not out.endswith("\n") or out.endswith("\r\n")


def test_mixed_line_endings_are_preserved_per_line():
    """A pathological mixed LF/CRLF file: an edit changes only the target value; every OTHER line
    keeps its own original ending (bare-LF stays LF, CRLF stays CRLF)."""
    src = "# a\r\n[loop]\nmax_rounds = 3\r\ntimeout_seconds = 60\n"
    data = tomllib.loads(src)
    data["loop"]["max_rounds"] = 9
    out = render(data, src)
    assert out == "# a\r\n[loop]\nmax_rounds = 9\r\ntimeout_seconds = 60\n"
