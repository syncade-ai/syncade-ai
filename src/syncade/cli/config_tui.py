"""Interactive ``syncade --config`` arrow-menu (pr-v2-30 Issue 3).

Arrow keys move between settings, Enter edits the highlighted one, ``t`` toggles the edit target
(global⇄repo), ``s`` saves the active target, ``q`` quits (prompting on unsaved edits in either
target). stdlib ``curses`` — no dependency.

The state machine :class:`ConfigMenu` is pure Python (unit-tested directly); ``run`` is the thin
curses layer (draw + key input), exercised end-to-end by a pty test. Non-TTY invocation degrades to
a message + exit 60 rather than a curses crash.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

from pydantic import ValidationError

from syncade import config_loader
from syncade.cli import config_keys, config_menu_rows
from syncade.cli.config_mode import (
    _LIST_SECTIONS,
    _PROVIDER_DEFAULT_MODELS,
    _apply,
    _atomic_write,
    _cross_provider_error,
    _existing_text,
    _layer_of,
    _shown,
    _unknown_reviewer_provider_error,
)
from syncade.cli.toml_writer import render
from syncade.config import SyncadeConfig
from syncade.config_loader import CONFIG_RELATIVE_PATH, _read_toml

# Providers shown in the picker, in display order.
_PICK_PROVIDERS = list(_PROVIDER_DEFAULT_MODELS) + ["custom..."]
# Curated model lists per provider (shown first; "custom..." always last).
_PICK_MODELS: dict[str, list[str]] = {
    "anthropic": ["claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5", "custom..."],
    "openai": ["gpt-5.5", "gpt-5.6-sol", "gpt-5.6-terra", "o3", "custom..."],
}

# Precedence rank of the editable layers (see config_loader). A row is "shadowed" when its effective
# value comes from a layer strictly ABOVE the current edit target — an edit there won't take effect.
_LAYER_RANK = {"default": 0, "global": 1, "repo": 2}


class ConfigMenu:
    """The editable state for the menu. Edits the ACTIVE TARGET layer — global (default) or, when
    toggled inside a git repo, the repo's ``.syncade/config.toml``. The displayed value is the value
    the ACTIVE TARGET produces (defaults+global at target=global; the full effective config at
    target=repo), so an edit is visible immediately even when a higher layer shadows it; a row is
    flagged ``shadowed by <layer>`` when an edit at the current target would be masked by a higher
    layer at runtime (PR-v2-31 inc 3; the target-perspective display closes a dogfood UX bug where a
    global edit of a repo-shadowed field appeared to do nothing)."""

    def __init__(self, global_raw: dict, repo_raw: dict, *, in_git: bool = True) -> None:
        self.global_raw = global_raw
        self.repo_raw = repo_raw
        self.in_git = in_git
        self.target = "global"  # edit target; `t` toggles global<->repo (repo needs a git repo)
        self.cursor = 0
        self.dirty_global = False  # per-target: saving one target must not mask the other's edits
        self.dirty_repo = False
        self.message = ""
        self.nav = [""]  # screen stack (drill-in, inc 4); nav[-1] is the current screen, "" = top
        self._recompute()

    @property
    def screen(self) -> str:
        return self.nav[-1]

    def current_row(self) -> config_menu_rows.Row:
        return self.rows[self.cursor]

    def drill(self) -> None:
        """Enter the highlighted row's child screen (only for a ``drill`` row)."""
        row = self.current_row()
        if row.kind == "drill":
            self.nav.append(row.key)
            self.cursor = 0
            self.message = ""
            self._recompute()

    def back(self) -> bool:
        """Pop to the parent screen; returns True if it navigated (False at the top)."""
        if len(self.nav) > 1:
            self.nav.pop()
            self.cursor = 0
            self.message = ""
            self._recompute()
            return True
        return False

    @property
    def dirty(self) -> bool:
        """True if EITHER target has unsaved edits — the quit guard prompts on either."""
        return self.dirty_global or self.dirty_repo

    @property
    def _target_dirty(self) -> bool:
        return self.dirty_repo if self.target == "repo" else self.dirty_global

    @_target_dirty.setter
    def _target_dirty(self, value: bool) -> None:
        if self.target == "repo":
            self.dirty_repo = value
        else:
            self.dirty_global = value

    def _recompute(self) -> None:
        merged = config_loader._deep_merge(copy.deepcopy(self.global_raw), self.repo_raw)
        self.config = SyncadeConfig.model_validate(merged)
        self.rows = config_menu_rows.screen_rows(self.config, self.screen)
        self.cursor = max(0, min(self.cursor, len(self.rows) - 1))

    @property
    def target_raw(self) -> dict:
        """Raw dict of the active edit target — repo when toggled in a git repo, else global."""
        return self.repo_raw if self.target == "repo" else self.global_raw

    @target_raw.setter
    def target_raw(self, value: dict) -> None:
        if self.target == "repo":
            self.repo_raw = value
        else:
            self.global_raw = value

    def toggle_target(self) -> None:
        """Switch the edit target global<->repo. A no-op outside a git repo (no repo layer)."""
        if self.in_git:
            self.target = "repo" if self.target == "global" else "global"

    def _mat_config(self):
        """Config to materialize an absent section from — the layer the TARGET inherits. A repo edit
        inherits defaults+global (== the effective config); a global edit inherits defaults+global
        only, so rebuild global-only lest the current repo's choices bake into ``~/.syncade``."""
        if self.target == "repo":
            return self.config
        try:
            return SyncadeConfig.model_validate(self.global_raw)
        except ValidationError:
            return self.config

    def display_rows(self) -> list[tuple[str, str, str]]:
        """(label, value, layer) per row. ``value`` is the row's value FROM THE CURRENT TARGET'S
        perspective (:meth:`_mat_config` — defaults+global at target=global, full effective at
        target=repo), so an edit at the active target is visible at once even when a higher layer
        shadows it; ``layer`` is the effective source, ``shadowed by <layer>`` when an edit at the
        target would be masked by a higher, ``→`` for a sectionless drill, or blank."""
        mat = self._mat_config()
        out = []
        for r in self.rows:
            value = _shown(mat, r.value_key) if r.value_key else ""
            if r.kind == "info":
                layer = ""
            elif r.section is not None:
                layer = self._layer_display(r.section, r.subkey)
            else:
                layer = "→"
            out.append((r.label, value, layer))
        return out

    def _layer_display(self, section: str, subkey: str | None) -> str:
        source = _layer_of(self.global_raw, self.repo_raw, section, subkey)
        if _LAYER_RANK[source] > _LAYER_RANK[self.target]:
            return f"shadowed by {source}"  # an edit at the current target won't change this value
        return source

    def move(self, delta: int) -> None:
        self.cursor = max(0, min(len(self.rows) - 1, self.cursor + delta))

    def apply_edit(self, text: str) -> str | None:
        """Apply the typed value to the highlighted row. A model row accepts ``model`` (keep the
        provider) or ``provider/model`` (switch both). Validates the result in isolation; on any
        error returns a message and changes nothing.

        Returns ``None`` on a state change (caller should show "updated"), ``""`` on a no-op
        (e.g. empty input on a non-clearable row), or a non-empty error string on failure."""
        text = text.strip()
        row = self.current_row()
        if row.kind != "edit":  # Enter on a drill/info row is navigation, handled by the caller
            return ""
        key = row.key
        # Empty input: loop.budget_usd supports clearing (removes the TOML key); optional and
        # list fields clear via the normal coerce/apply path; all other rows are a no-op.
        if not text:
            if key == "loop.budget_usd":
                candidate = copy.deepcopy(self.target_raw)
                loop = candidate.get("loop")
                # Guard isinstance: this clear runs BEFORE the caught edit path, so a malformed
                # target loop (a list/scalar masked by a higher layer) would crash on `.pop`.
                if isinstance(loop, dict) and "budget_usd" in loop:
                    loop.pop("budget_usd")
                    if loop:
                        candidate["loop"] = loop
                    else:
                        candidate.pop("loop", None)
                    self.target_raw = candidate
                    self._target_dirty = True
                    self._recompute()
                    return None  # state changed → caller shows "updated"
                return ""
            # Optional and list fields: empty input clears (parity with --config set).
            try:
                ann = config_keys.resolve_annotation(key)
            except config_keys.UnknownKey:
                return ""
            if not config_keys._empty_clears(ann):
                return ""
            # fall through with text="" — coerce handles Optional→None and list→[]
        # Build (dotted-key, raw) ops. A model row given "provider/model" splits into a provider
        # edit (which re-derives the model) followed by the explicit model edit.
        if key.endswith(".model") and "/" in text:
            provider, model = (p.strip() for p in text.split("/", 1))
            ops = [(key[: -len(".model")] + ".provider", provider), (key, model)]
        else:
            ops = [(key, text)]
        candidate = copy.deepcopy(self.target_raw)
        # Materialize an absent section from the layer the TARGET inherits (see _mat_config): a
        # global edit uses global-only so the repo's choices don't bake into ~/.syncade; a repo edit
        # uses the effective config (== defaults+global when the section is absent from repo).
        mat_config = self._mat_config()
        # A list-section element (reviewers/checks) may exist only in a layer ABOVE the target (e.g.
        # a repo-only check edited at target=global). Range-check against the TARGET's roster so the
        # menu emits the CLI's clean "N out of range" message, not an IndexError leak from _apply.
        parts = key.split(".")
        if parts[0] in _LIST_SECTIONS and len(parts) >= 2 and parts[1].isdigit():
            index = int(parts[1])
            existing = candidate.get(parts[0])
            roster = len(existing) if existing is not None else len(getattr(mat_config, parts[0]))
            if index >= roster:
                return f"invalid: {parts[0]} {index} out of range (has {roster})"
        try:
            for op_key, raw in ops:
                if op_key.endswith(".provider") and op_key.startswith("reviewers."):
                    err = _unknown_reviewer_provider_error(raw)
                    if err:
                        return f"invalid: {err}"
                value = config_keys.coerce(config_keys.resolve_annotation(op_key), raw)
                _apply(candidate, op_key, value, mat_config)
            # Check for an obvious cross-provider model mismatch before pydantic validation.
            if key.endswith(".model"):
                parts = key.split(".")
                actor_raw = (
                    candidate["reviewers"][int(parts[1])]
                    if parts[0] == "reviewers"
                    else candidate.get(parts[0], {})
                )
                pair_err = _cross_provider_error(actor_raw.get("provider", ""), ops[-1][1])
                if pair_err:
                    return f"invalid: {pair_err}"
            SyncadeConfig.model_validate(candidate)
            # Also validate the merged effective config to catch cross-layer conflicts
            # (e.g. duplicate reviewer/check names split across layers).
            if self.target == "repo":
                merged_cand = config_loader._deep_merge(copy.deepcopy(self.global_raw), candidate)
            else:
                merged_cand = config_loader._deep_merge(copy.deepcopy(candidate), self.repo_raw)
            SyncadeConfig.model_validate(merged_cand)
        except (
            config_keys.InvalidValue,
            config_keys.UnknownKey,
            ValueError,
            ValidationError,
            KeyError,
            TypeError,
            IndexError,
        ) as exc:
            return _error(exc)
        self.target_raw = candidate
        self._target_dirty = True
        self._recompute()
        return None

    def save(self, path: Path) -> str | None:
        if not self._target_dirty:  # don't write (or create) a file for a target with no edits
            return f"no unsaved edits for the {self.target} config"
        try:
            _atomic_write(path, render(self.target_raw, _existing_text(path)))
        except OSError as exc:
            return f"save failed: {exc}"
        self._target_dirty = False
        return None


def _error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        err = exc.errors()[0]
        loc = ".".join(str(p) for p in err["loc"])
        return f"invalid: {loc}: {err['msg']}"
    return f"invalid: {exc}"


def run(*, global_path: Path, repo_root: Path, in_git: bool = True) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "[syncade] --config: an interactive terminal is required for the menu. Use "
            "`--config list` / `--config set <key> <value>` instead.",
            file=sys.stderr,
        )
        return 60
    import curses
    import locale

    locale.setlocale(locale.LC_ALL, "")
    # Outside a git repo there is no repo layer (a stray cwd/.syncade/config.toml no run would read
    # must not masquerade as one), matching --config list/get/set.
    repo_path = repo_root / CONFIG_RELATIVE_PATH
    repo_raw = _read_toml(repo_path) if in_git else {}
    menu = ConfigMenu(_read_toml(global_path), repo_raw, in_git=in_git)
    return curses.wrapper(_loop, menu, global_path, repo_path)


def _target_path(menu: ConfigMenu, global_path: Path, repo_path: Path) -> Path:
    return repo_path if menu.target == "repo" else global_path


def _loop(stdscr, menu: ConfigMenu, global_path: Path, repo_path: Path) -> int:
    import curses

    curses.curs_set(0)
    stdscr.keypad(True)
    while True:
        _draw(stdscr, menu, _target_path(menu, global_path, repo_path))
        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            menu.move(-1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            menu.move(1)
        elif ch == ord("t"):
            menu.toggle_target()
            menu.message = (
                f"editing target: {menu.target}"
                if menu.in_git
                else "not a git repo — target stays global"
            )
        elif ch == 27:  # Esc -> back up one screen; at the top, same quit path as q
            if not menu.back():
                n = len(menu.rows)
                if not menu.dirty or _confirm(stdscr, "Unsaved edits — quit anyway? (y/N) ", n):
                    return 0
            else:
                menu.message = ""
        elif ch in (curses.KEY_ENTER, 10, 13):
            row = menu.current_row()
            if row.kind == "drill":
                menu.drill()
            elif row.kind == "edit":
                if row.key.endswith(".model"):
                    value = _pick_model(stdscr, menu)
                    if value is None:
                        menu.message = ""
                        continue
                else:
                    value = _prompt(stdscr, f"New {row.label}: ", len(menu.rows))
                result = menu.apply_edit(value)
                # None → state changed; "" → no-op (clear message); non-empty → error
                menu.message = "updated (unsaved — press s)" if result is None else result
            # kind == "info": inert
        elif ch == ord("s"):
            path = _target_path(menu, global_path, repo_path)
            menu.message = menu.save(path) or f"saved to {path}"
        elif ch == ord("q"):
            n = len(menu.rows)
            if not menu.dirty or _confirm(stdscr, "Unsaved edits — quit anyway? (y/N) ", n):
                return 0


def _safe_addstr(win, y: int, x: int, text: str) -> None:
    """``addstr`` that never raises on an off-screen or overflowing write (tiny/narrow terminals):
    skips fully off-screen rows/cols, clips text to the line, and swallows the bottom-right-corner
    ``curses.error``. Rendering degrades instead of crashing out of ``curses.wrapper``."""
    import curses

    rows, cols = win.getmaxyx()
    if y < 0 or y >= rows or x < 0 or x >= cols:
        return
    try:
        win.addstr(y, x, text[: max(0, cols - x - 1)])
    except curses.error:
        pass


def _pick_from_list(stdscr, title: str, choices: list[str]) -> str | None:
    """Pick from a list with arrows. Returns the selected item, or None on Esc. Writes are bounded
    (:func:`_safe_addstr`) so a too-small terminal degrades to a truncated list, not a traceback."""
    import curses

    cursor = 0
    while True:
        stdscr.erase()
        _safe_addstr(stdscr, 0, 0, title)
        for i, choice in enumerate(choices):
            _safe_addstr(stdscr, 2 + i, 0, (">" if i == cursor else " ") + " " + choice)
        _safe_addstr(stdscr, 3 + len(choices), 0, "up/down move  Enter select  Esc cancel")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            cursor = max(0, cursor - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            cursor = min(len(choices) - 1, cursor + 1)
        elif ch in (curses.KEY_ENTER, 10, 13):
            return choices[cursor]
        elif ch == 27:  # Esc
            return None


def _pick_model(stdscr, menu: ConfigMenu) -> str | None:
    """Two-step provider → model picker. Returns ``'provider/model'`` or None on cancel."""
    provider = _pick_from_list(stdscr, "Select provider (Esc to cancel):", _PICK_PROVIDERS)
    if provider is None:
        return None
    if provider == "custom...":
        raw = _prompt(stdscr, "Custom (e.g. anthropic/claude-opus-4-8): ", len(menu.rows))
        return raw.strip() or None
    model = _pick_from_list(
        stdscr,
        f"Select {provider} model (Esc to cancel):",
        _PICK_MODELS.get(provider, ["custom..."]),
    )
    if model is None:
        return None
    if model == "custom...":
        raw = _prompt(stdscr, f"Custom model for {provider}: ", len(menu.rows))
        return f"{provider}/{raw.strip()}" if raw.strip() else None
    return f"{provider}/{model}"


def _too_small(stdscr) -> None:
    """Render the shared resize message (bounded) and refresh. Used when a screen can't fit."""
    stdscr.erase()
    _safe_addstr(stdscr, 0, 0, "Terminal too small — resize window")
    stdscr.refresh()


def _draw(stdscr, menu: ConfigMenu, target_path: Path) -> None:
    rows, _cols = stdscr.getmaxyx()
    min_rows = 3 + len(menu.rows) + 1  # header gap + rows + footer line
    stdscr.erase()
    if rows < min_rows:
        _safe_addstr(stdscr, 0, 0, "Terminal too small — resize window")
        stdscr.refresh()
        return
    crumb = "" if menu.screen == "" else "  ›  " + " › ".join(menu.nav[1:])
    _safe_addstr(stdscr, 0, 0, f"Configure syncade{crumb}   [target: {menu.target} · t toggles]")
    _safe_addstr(stdscr, 1, 0, f"(writing {target_path})")
    for i, (label, value, layer) in enumerate(menu.display_rows()):
        marker = ">" if i == menu.cursor else " "
        _safe_addstr(stdscr, 2 + i, 0, f"{marker} {label:<20} {value:<28} {layer}")
    foot = 3 + len(menu.rows)
    _safe_addstr(stdscr, foot, 0, "up/down move  Enter select  Esc back  t target  s save  q quit")
    if menu.message and rows > foot + 1:
        _safe_addstr(stdscr, foot + 1, 0, menu.message)
    stdscr.refresh()


def _prompt(stdscr, label: str, n_rows: int) -> str:
    import curses

    row = 5 + n_rows
    rows, cols = stdscr.getmaxyx()
    if row >= rows:  # too short to place the input line — degrade to a no-op edit, never crash
        _too_small(stdscr)
        return ""
    _safe_addstr(stdscr, row, 0, label)
    stdscr.clrtoeol()
    curses.echo()
    curses.curs_set(1)
    try:
        raw = stdscr.getstr(row, min(len(label), cols - 1)).decode("utf-8", "replace")
    finally:
        curses.noecho()
        curses.curs_set(0)
    return raw


def _confirm(stdscr, label: str, n_rows: int) -> bool:
    rows, _cols = stdscr.getmaxyx()
    row = 5 + n_rows
    if row >= rows:  # can't show the prompt — do NOT quit (preserve unsaved edits), never crash
        _too_small(stdscr)
        return False
    _safe_addstr(stdscr, row, 0, label)
    stdscr.clrtoeol()
    stdscr.refresh()
    return stdscr.getch() in (ord("y"), ord("Y"))
