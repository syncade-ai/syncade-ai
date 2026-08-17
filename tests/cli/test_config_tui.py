"""ConfigMenu logic (pr-v2-30 Issue 3) — pure state, no curses.

The curses layer (draw + key input) is exercised end-to-end by the pty test in
tests/cli/test_config_tui_pty.py.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from syncade.cli.config_tui import ConfigMenu, _draw


def _menu(global_toml: str = "", repo_toml: str = "") -> ConfigMenu:
    g = tomllib.loads(global_toml) if global_toml else {}
    r = tomllib.loads(repo_toml) if repo_toml else {}
    return ConfigMenu(g, r)


def _row_index(menu: ConfigMenu, key: str) -> int:
    return next(i for i, row in enumerate(menu.rows) if row.key == key)


def _goto(menu: ConfigMenu, key: str) -> None:
    """Navigate to the edit row ``key``, drilling into its actor/section screen first if needed
    (inc 4). Loop scalars are on the top screen; other fields are one drill down."""
    parts = key.split(".")
    if parts[0] == "loop":
        menu.cursor = _row_index(menu, key)
        return
    menu.cursor = _row_index(menu, ".".join(parts[:-1]))  # the actor/section drill row
    menu.drill()
    menu.cursor = _row_index(menu, key)


def test_move_clamps_to_range():
    m = _menu()
    m.move(-1)
    assert m.cursor == 0
    m.move(100)
    assert m.cursor == len(m.rows) - 1


def test_display_rows_cover_the_settings():
    m = _menu()
    labels = [r[0] for r in m.display_rows()]  # top screen: actor drills + loop edits + Advanced
    assert "Producer" in labels and "Rounds (max)" in labels and "Judge" in labels
    assert "Advanced…" in labels
    assert len(m.display_rows()) == len(m.rows)


def test_apply_numeric_updates_marks_dirty_and_shows():
    m = _menu()
    i = _row_index(m, "loop.max_rounds")
    m.cursor = i
    assert m.apply_edit("1") is None
    assert m.dirty and m.global_raw["loop"]["max_rounds"] == 1
    _label, value, layer = m.display_rows()[i]
    assert value == "1" and layer == "global"  # the edit surfaces in the display + is attributed


def test_apply_invalid_changes_nothing():
    m = _menu()
    m.cursor = _row_index(m, "loop.max_rounds")
    err = m.apply_edit("99")  # > le=10 ceiling
    assert err is not None and "invalid" in err
    assert not m.dirty and "loop" not in m.global_raw  # untouched


def test_apply_model_with_provider_slash_switches_both():
    m = _menu()
    _goto(m, "producer.model")
    assert m.apply_edit("openai/gpt-5.5") is None
    prod = m.global_raw["producer"]
    assert prod["provider"] == "openai" and prod["model"] == "gpt-5.5"


def test_apply_model_only_keeps_provider():
    m = _menu('[producer]\nprovider = "anthropic"\nmodel = "claude-sonnet-4-6"\n')
    _goto(m, "producer.model")
    assert m.apply_edit("claude-opus-4-6") is None
    prod = m.global_raw["producer"]
    assert prod["provider"] == "anthropic" and prod["model"] == "claude-opus-4-6"


def test_apply_model_only_rejects_cross_provider():
    m = _menu('[producer]\nprovider = "anthropic"\nmodel = "claude-sonnet-4-6"\n')
    _goto(m, "producer.model")
    err = m.apply_edit("gpt-5.5")
    assert err is not None and "openai" in err and "anthropic" in err
    assert not m.dirty  # state unchanged


def test_empty_input_rejected_for_budget_usd():
    # loop.budget_usd must not be clearable via empty — that omits the TOML key, causing
    # --resume to re-inherit a prior ceiling the user expected to suppress. Set 0 explicitly.
    m = _menu("[loop]\nbudget_usd = 5.0\n")
    m.cursor = _row_index(m, "loop.budget_usd")
    result = m.apply_edit("")
    assert isinstance(result, str) and result  # non-empty error string, not state-change
    assert not m.dirty  # state unchanged


def test_empty_input_for_non_clearable_row_is_noop():
    m = _menu()
    m.cursor = _row_index(m, "loop.max_rounds")
    result = m.apply_edit("")
    assert result == ""  # no-op sentinel
    assert not m.dirty


def test_save_writes_and_clears_dirty(tmp_path):
    m = _menu()
    m.cursor = _row_index(m, "loop.max_rounds")
    m.apply_edit("2")
    path = tmp_path / ".syncade" / "config.toml"
    assert m.save(path) is None
    assert not m.dirty
    assert tomllib.loads(path.read_text())["loop"]["max_rounds"] == 2


def test_apply_reviewer_provider_via_slash_rederives_model():
    """Editing a reviewer model row with 'anthropic/claude-opus-4-8' updates both fields."""
    m = _menu()
    _goto(m, "reviewers.0.model")
    assert m.apply_edit("anthropic/claude-opus-4-8") is None
    reviewer = m.global_raw["reviewers"][0]
    assert reviewer["provider"] == "anthropic"
    assert reviewer["model"] == "claude-opus-4-8"
    assert m.dirty


def test_apply_reviewer_model_slash_for_judge_row():
    """Judge model row accepts 'provider/model' format and updates synthesizer config."""
    m = _menu()
    _goto(m, "synthesizer.model")
    assert m.apply_edit("openai/gpt-5.5") is None
    assert m.global_raw["synthesizer"]["provider"] == "openai"
    assert m.global_raw["synthesizer"]["model"] == "gpt-5.5"


def test_apply_offprefix_cross_provider_rejected():
    """The TUI's custom-entry path rejects off-prefix cross-provider models too (dogfood ①)."""
    m = _menu('[producer]\nprovider = "anthropic"\nmodel = "claude-sonnet-4-6"\n')
    _goto(m, "producer.model")
    err = m.apply_edit("o3")  # OpenAI model under an anthropic producer
    assert err is not None and "openai" in err
    assert not m.dirty


def test_draw_shows_resize_message_on_small_terminal():
    """_draw must render a resize message (not crash) when the terminal is too small."""

    class _MockStdscr:
        def __init__(self, height):
            self._h = height
            self.lines = []

        def getmaxyx(self):
            return self._h, 80

        def erase(self):
            self.lines.clear()

        def addstr(self, row, col, text):
            self.lines.append((row, text))

        def refresh(self):
            pass

    m = _menu()
    scr = _MockStdscr(height=4)  # far too small for the full menu
    _draw(scr, m, Path("/tmp/config.toml"))
    texts = [t for _, t in scr.lines]
    assert any("resize" in t.lower() or "small" in t.lower() for t in texts), (
        "_draw must show a resize/small-terminal message when rows < min_rows"
    )
    # Nothing drawn beyond the first row (no IndexError-inducing out-of-bounds writes)
    rows_written = [r for r, _ in scr.lines]
    assert all(r == 0 for r in rows_written), "only row 0 should be written on a small terminal"


def test_apply_edit_handles_malformed_global_section():
    """apply_edit must return an error string (not crash) when global_raw has a malformed section.

    If global_raw["loop"] is a scalar (not a dict), _apply raises TypeError when mutating it.
    The repo layer masks the malformed global so _recompute succeeds, but a subsequent edit
    attempt crashes without the fix.
    """
    # global_raw: loop is a string (malformed); repo masks it so _recompute succeeds.
    m = ConfigMenu({"loop": "broken"}, {"loop": {"max_rounds": 2}})
    m.cursor = _row_index(m, "loop.max_rounds")
    result = m.apply_edit("2")
    # Without the fix, TypeError propagates; after the fix, returns a non-empty error string.
    assert result is not None and result != "", "malformed section must return error, not crash"
    assert not m.dirty  # state must be unchanged


def test_apply_edit_does_not_leak_repo_fields_to_global():
    """Materializing a section for a global edit must not bake repo-only fields into global_raw.

    If the effective config includes a repo-level thinking=high on the producer and the global has
    no [producer], the TUI used to materialize from self.config (merged) and write thinking=high
    to global_raw. After the fix it materializes from the global-only config (defaults).
    """
    repo_toml = '[producer]\nprovider = "openai"\nmodel = "gpt-5.5"\nthinking = "high"\n'
    m = _menu("", repo_toml)  # global empty; repo has producer with thinking=high
    _goto(m, "producer.model")
    result = m.apply_edit("gpt-5.6-sol")  # edit model, keeping provider
    assert result is None  # edit succeeded
    prod = m.global_raw.get("producer", {})
    assert prod.get("thinking") != "high", (
        "repo thinking=high must not be baked into global_raw; "
        "materialization must use global-only config"
    )


def test_empty_budget_usd_with_malformed_global_loop_returns_error():
    """AC6: the menu degrades, never crashes. A malformed global loop masked by a valid repo loop
    must return an error string on empty budget_usd input — never raise."""
    m = _menu(global_toml='loop = ["budget_usd"]\n', repo_toml="[loop]\nmax_rounds = 2\n")
    m.cursor = _row_index(m, "loop.budget_usd")
    result = m.apply_edit("")  # budget key: empty is now rejected, not cleared
    assert isinstance(result, str) and result and not m.dirty


# --- pr-v2-31 Increment 3: edit-target toggle (global <-> repo) + shadowed-row flagging ---


def test_target_defaults_global_and_toggles():
    m = _menu(repo_toml="[loop]\nmax_rounds = 2\n")  # _menu builds with in_git=True
    assert m.target == "global"
    m.toggle_target()
    assert m.target == "repo"
    m.toggle_target()
    assert m.target == "global"


def test_toggle_target_is_noop_without_git():
    m = ConfigMenu({}, {}, in_git=False)  # no repo layer available
    m.toggle_target()
    assert m.target == "global"


def test_edit_at_repo_target_writes_repo_and_takes_effect():
    m = _menu(repo_toml="[loop]\nmax_rounds = 2\n")
    m.toggle_target()  # -> repo
    m.cursor = _row_index(m, "loop.max_rounds")
    assert m.apply_edit("3") is None
    assert m.repo_raw["loop"]["max_rounds"] == 3  # written to the REPO layer
    assert "loop" not in m.global_raw  # global untouched
    assert m.config.loop.max_rounds == 3  # effective reflects it (repo is the top layer)


def test_producer_row_shadowed_by_repo_at_global_target():
    m = _menu(repo_toml='[producer]\nprovider = "openai"\nmodel = "gpt-5.5"\n')
    i = _row_index(m, "producer")  # the top-screen Producer drill row (section=producer)
    assert m.display_rows()[i][2] == "shadowed by repo"  # a global edit here won't take effect
    m.toggle_target()  # -> repo
    assert m.display_rows()[i][2] == "repo"  # editing repo takes effect; not shadowed


def test_save_writes_the_active_target_raw(tmp_path):
    m = _menu(repo_toml="[loop]\nmax_rounds = 2\n")
    m.toggle_target()  # repo
    m.cursor = _row_index(m, "loop.max_rounds")
    m.apply_edit("3")
    rpath = tmp_path / "repo.toml"
    assert m.save(rpath) is None
    assert tomllib.loads(rpath.read_text())["loop"]["max_rounds"] == 3


def test_saving_one_target_keeps_the_others_edit_dirty(tmp_path):
    """dirty is per-target: saving the repo layer must NOT silently drop an unsaved global edit —
    the quit guard must still fire (no silent data loss on toggle+save+quit)."""
    m = _menu(repo_toml="[loop]\nmax_rounds = 2\n")
    m.cursor = _row_index(m, "loop.max_rounds")
    m.apply_edit("1")  # edit at the GLOBAL target (default)
    assert m.dirty
    m.toggle_target()  # -> repo (no repo edit made)
    m.save(tmp_path / "repo.toml")  # saving repo must not clear the global edit's dirtiness
    assert m.dirty, "the unsaved global edit must keep the menu dirty so quit still prompts"


def test_save_is_noop_when_active_target_not_dirty(tmp_path):
    """Pressing save at a target with no edits must not create a spurious (trackable) empty file."""
    m = _menu(repo_toml="[loop]\nmax_rounds = 2\n")
    m.toggle_target()  # -> repo, no edits
    rpath = tmp_path / "repo.toml"
    result = m.save(rpath)
    assert result is not None and "no unsaved" in result.lower()  # "nothing to save" message
    assert not rpath.exists()  # no empty file written


def test_edit_higher_layer_only_list_element_gives_clean_range_message():
    """D5 parity: editing a [[checks]]/[[reviewers]] element that exists only in a layer ABOVE the
    active target must reject with the CLI's clean 'N out of range (has M)' message, not leak a raw
    IndexError. Repro: repo defines a check, target=global has none."""
    m = _menu(repo_toml='[[checks]]\nname = "lint"\ncommand = "true"\nseverity = "blocking"\n')
    m.cursor = _row_index(m, "advanced")
    m.drill()
    m.cursor = _row_index(m, "checks")
    m.drill()
    m.cursor = _row_index(m, "checks.0")
    m.drill()  # the effective (repo) config shows checks.0; target is still global (0 checks)
    m.cursor = _row_index(m, "checks.0.severity")
    result = m.apply_edit("advisory")
    assert result is not None and "out of range (has" in result  # the CLI's clean form, not a leak


# --- pr-v2-31 Increment 4: empty-input clear parity + Esc-at-top quit ---


def test_empty_input_clears_optional_producer_timeout():
    """Empty input on producer.timeout_seconds clears it to None (parity with --config set)."""
    m = _menu("[producer]\ntimeout_seconds = 120\n")
    _goto(m, "producer.timeout_seconds")
    result = m.apply_edit("")
    assert result is None  # state changed
    assert m.dirty
    # None is omitted by the TOML writer, so the field is absent from the persisted dict
    assert m.global_raw["producer"].get("timeout_seconds") is None


def test_empty_input_clears_optional_reviewer_template():
    """Empty input on reviewers.0.template clears it to None (parity with --config set)."""
    m = _menu(
        "[[reviewers]]\n"
        'name = "r1"\n'
        'provider = "openai"\n'
        'model = "gpt-5.5"\n'
        'template = "reviewer_adversarial.md"\n'
    )
    _goto(m, "reviewers.0.template")
    result = m.apply_edit("")
    assert result is None
    assert m.dirty
    assert m.global_raw["reviewers"][0].get("template") is None


def test_empty_input_clears_list_strip_repo_context_files():
    """Empty input on review.strip_repo_context_files clears it to [] (parity with --config set)."""
    m = _menu('[review]\nstrip_repo_context_files = ["CLAUDE.md"]\n')
    m.cursor = _row_index(m, "advanced")
    m.drill()
    m.cursor = _row_index(m, "review")
    m.drill()
    m.cursor = _row_index(m, "review.strip_repo_context_files")
    result = m.apply_edit("")
    assert result is None
    assert m.dirty
    assert m.global_raw["review"]["strip_repo_context_files"] == []


def test_empty_input_on_non_clearable_drilled_row_is_noop():
    """Empty input on a non-optional, non-list field (producer.thinking) is still a no-op."""
    m = _menu()
    _goto(m, "producer.thinking")
    result = m.apply_edit("")
    assert result == ""
    assert not m.dirty


def test_back_returns_false_at_top_and_true_when_drilled():
    """back() returns False at the top screen and True when there is a parent to pop to."""
    m = _menu()
    assert m.back() is False  # at the top: no parent
    m.cursor = _row_index(m, "advanced")
    m.drill()
    assert m.screen == "advanced"
    assert m.back() is True  # drilled: pops back to top
    assert m.screen == ""


# --- cross-layer validation (blocker fix) ---


def test_apply_edit_rejects_reviewer_name_colliding_with_global_test_command():
    """Cross-layer: repo reviewer 'tests' + global test_command → apply_edit returns error."""
    m = _menu(
        global_toml="[loop]\ntest_command = 'pytest'\n",
        repo_toml="",
    )
    m.toggle_target()  # -> repo
    _goto(m, "reviewers.0.name")
    result = m.apply_edit("tests")
    assert result is not None and result != "", "cross-layer collision must return an error"
    assert not m.dirty  # state must be unchanged


def test_apply_edit_rejects_global_reviewer_name_colliding_with_repo_check():
    """apply_edit on a global-target reviewer name must be rejected when merged global+repo is
    invalid. Repo has a check named 'lint'; renaming the global reviewer to 'lint' creates a
    worktree-basename collision visible only in the merged config. State must be unchanged."""
    m = _menu(
        global_toml="",
        repo_toml="[[checks]]\nname = 'lint'\ncommand = 'true'\n",
    )
    # default target is global
    _goto(m, "reviewers.0.name")
    result = m.apply_edit("lint")
    assert result is not None and result != "", "cross-layer collision must return an error"
    assert not m.dirty  # global_raw untouched


# --- pr-v2-31 follow-up: an edit at the active target must be VISIBLE immediately (dogfood bug) ---


def test_global_edit_of_shadowed_field_reflects_in_display():
    """The 'my edit did nothing' bug: a global edit of a repo-shadowed field must show the NEW
    global value at once (still flagged shadowed), not the unchanged effective/repo value.
    Regression for the display showing the merged-effective value instead of the target's."""
    m = _menu(
        global_toml="[loop]\ntimeout_seconds = 2400\n",
        repo_toml="[loop]\ntimeout_seconds = 1800\n",
    )
    i = _row_index(m, "loop.timeout_seconds")
    m.cursor = i
    assert m.display_rows()[i][1] == "2400.0"  # target=global shows GLOBAL's value, not repo's 1800
    assert m.display_rows()[i][2] == "shadowed by repo"
    assert m.apply_edit("3000") is None  # edit global
    label, value, layer = m.display_rows()[i]
    assert value == "3000.0"  # the user SEES their global edit immediately
    assert layer == "shadowed by repo"  # still correctly warned the repo overrides it at runtime


def test_repo_target_shows_effective_value():
    """At target=repo the display shows the effective value (repo wins, nothing above it)."""
    m = _menu(
        global_toml="[loop]\ntimeout_seconds = 2400\n",
        repo_toml="[loop]\ntimeout_seconds = 1800\n",
    )
    m.toggle_target()  # -> repo
    i = _row_index(m, "loop.timeout_seconds")
    assert m.display_rows()[i][1] == "1800.0"  # repo's value
    assert m.display_rows()[i][2] == "repo"
    m.cursor = i
    assert m.apply_edit("900") is None
    assert m.display_rows()[i][1] == "900.0"  # repo edit reflects too


def test_save_preserves_comments_in_the_target_file(tmp_path):
    """pr-v2-34: the menu's save must not destroy the operator's comments — this is the path that
    stripped syncade's own repo config twice during PR-v2-31 dogfooding."""
    path = tmp_path / "config.toml"
    path.write_text("# keep this rationale\n[loop]\nmax_rounds = 3    # the ceiling\n")
    m = ConfigMenu(tomllib.loads(path.read_text()), {}, in_git=False)
    m.cursor = _row_index(m, "loop.max_rounds")
    assert m.apply_edit("7") is None
    assert m.save(path) is None
    text = path.read_text()
    assert "# keep this rationale" in text
    assert "# the ceiling" in text  # the trailing comment on the EDITED line survives
    assert tomllib.loads(text)["loop"]["max_rounds"] == 7
