"""The pure screen/row model for the drill-in config menu (pr-v2-31 inc 4)."""

from __future__ import annotations

from syncade.cli.config_menu_rows import screen_rows
from syncade.config import SyncadeConfig


def _by_label(rows):
    return {r.label: r for r in rows}


def test_top_screen_has_actor_drills_loop_edits_and_advanced():
    rows = screen_rows(SyncadeConfig(), "")
    by = _by_label(rows)
    assert by["Producer"].kind == "drill" and by["Producer"].key == "producer"
    assert by["Judge"].kind == "drill" and by["Judge"].key == "synthesizer"
    assert by["Reviewer 1"].kind == "drill" and by["Reviewer 1"].key == "reviewers.0"
    assert by["Reviewer 2"].kind == "drill" and by["Reviewer 2"].key == "reviewers.1"
    assert by["Rounds (max)"].kind == "edit" and by["Rounds (max)"].key == "loop.max_rounds"
    assert by["Cost cap (USD)"].kind == "edit" and by["Cost cap (USD)"].key == "loop.budget_usd"
    assert by["Advanced…"].kind == "drill" and by["Advanced…"].key == "advanced"


def test_producer_screen_field_rows_are_edits_under_producer():
    rows = screen_rows(SyncadeConfig(), "producer")
    by = _by_label(rows)
    for want in ("Model", "Thinking", "Permissions", "Auth", "Timeout (s)"):
        assert want in by, f"producer screen missing {want!r}"
    for r in rows:
        assert r.kind == "edit" and r.key.startswith("producer.")
    assert by["Model"].key == "producer.model"  # the model row edits provider/model
    # provider is folded into the Model row; api_key_env is CLI-only (kept out of the menu)
    assert "Provider" not in by and "api_key_env" not in [r.key.split(".")[-1] for r in rows]


def test_reviewer_screen_has_reviewer_only_fields():
    rows = screen_rows(SyncadeConfig(), "reviewers.0")
    by = _by_label(rows)
    for want in (
        "Model",
        "Thinking",
        "Permissions",
        "Auth",
        "Name",
        "Adversarial lens",
        "Template",
    ):
        assert want in by, f"reviewer screen missing {want!r}"
    for r in rows:
        assert r.key.startswith("reviewers.0.")


def test_synthesizer_screen_has_no_timeout_field():
    # SynthesizerConfig has no timeout_seconds field — the screen must not invent one.
    labels = [r.label for r in screen_rows(SyncadeConfig(), "synthesizer")]
    assert "Model" in labels and "Thinking" in labels
    assert "Timeout (s)" not in labels


def test_advanced_screen_drills_to_sections():
    by = _by_label(screen_rows(SyncadeConfig(), "advanced"))
    for label, key in (
        ("Retry", "retry"),
        ("GC", "gc"),
        ("Review", "review"),
        ("Checks", "checks"),
    ):
        assert by[label].kind == "drill" and by[label].key == key


def test_retry_and_gc_section_field_rows():
    assert [r.key for r in screen_rows(SyncadeConfig(), "retry")] == ["retry.max_retries"]
    assert [r.key for r in screen_rows(SyncadeConfig(), "gc")] == ["gc.keep", "gc.max_age_days"]
    for r in screen_rows(SyncadeConfig(), "gc"):
        assert r.kind == "edit"


def test_checks_screen_lists_entries_as_drills():
    cfg = SyncadeConfig(checks=[{"name": "lint", "command": "true", "severity": "blocking"}])
    rows = screen_rows(cfg, "checks")
    assert rows[0].kind == "drill" and rows[0].key == "checks.0"
    # drilling into a check shows its fields
    fields = [r.key for r in screen_rows(cfg, "checks.0")]
    assert fields == ["checks.0.name", "checks.0.command", "checks.0.severity"]


def test_empty_checks_screen_is_an_info_row_not_a_crash():
    rows = screen_rows(SyncadeConfig(), "checks")  # default: no checks
    assert len(rows) == 1 and rows[0].kind == "info"


def test_actor_field_order_covers_all_settable_actor_fields():
    """Guard the maintainability trap: every settable scalar field of every actor model (minus the
    intentionally-skipped provider/api_key_env) must be in _ACTOR_FIELD_ORDER, else a future field
    would be silently dropped from the drill-in menu."""
    from syncade.cli import config_menu_rows as m
    from syncade.config import ProducerConfig, ReviewerConfig, SynthesizerConfig

    covered = set(m._ACTOR_FIELD_ORDER)
    for model in (ProducerConfig, ReviewerConfig, SynthesizerConfig):
        for f in m._settable(model):
            if f not in m._SKIP_FIELDS:
                assert f in covered, f"{model.__name__}.{f} settable but missing _ACTOR_FIELD_ORDER"
