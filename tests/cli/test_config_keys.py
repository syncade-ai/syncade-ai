"""Schema-driven key resolution + coercion for ``--config set`` (pr-v2-31 Increment 2)."""

from pathlib import Path

import pytest

from syncade.checks_config import CheckSeverity
from syncade.cli import config_keys
from syncade.config_types import ReviewerPermissions, Thinking


class TestCoerce:
    """``coerce(annotation, raw)`` turns a raw CLI string into the field's typed value,
    raising ``InvalidValue`` when the string does not fit the type."""

    def test_int(self):
        assert config_keys.coerce(int, "5") == 5

    def test_int_rejects_non_integer(self):
        with pytest.raises(config_keys.InvalidValue):
            config_keys.coerce(int, "5.5")
        with pytest.raises(config_keys.InvalidValue):
            config_keys.coerce(int, "x")

    def test_float(self):
        assert config_keys.coerce(float, "1.5") == 1.5

    def test_float_rejects_non_number(self):
        with pytest.raises(config_keys.InvalidValue):
            config_keys.coerce(float, "x")

    def test_bool_true_forms(self):
        assert config_keys.coerce(bool, "true") is True
        assert config_keys.coerce(bool, "True") is True
        assert config_keys.coerce(bool, "1") is True

    def test_bool_false_forms(self):
        assert config_keys.coerce(bool, "false") is False
        assert config_keys.coerce(bool, "0") is False

    def test_bool_rejects_other(self):
        with pytest.raises(config_keys.InvalidValue):
            config_keys.coerce(bool, "maybe")

    def test_literal_member(self):
        assert config_keys.coerce(Thinking, "high") == "high"

    def test_literal_rejects_non_member(self):
        with pytest.raises(config_keys.InvalidValue):
            config_keys.coerce(Thinking, "bogus")

    def test_list_str_comma_split(self):
        assert config_keys.coerce(list[str], "a,b, c") == ["a", "b", "c"]

    def test_str_passthrough(self):
        assert config_keys.coerce(str, "pytest -q") == "pytest -q"

    def test_path_kept_as_string(self):
        # Stored as a string in TOML; pydantic coerces str -> Path on load.
        assert config_keys.coerce(Path, "/tmp/x") == "/tmp/x"

    def test_optional_empty_clears_to_none(self):
        assert config_keys.coerce(float | None, "") is None

    def test_optional_non_empty_coerces_inner(self):
        assert config_keys.coerce(float | None, "1.5") == 1.5


class TestResolveAnnotation:
    """``resolve_annotation(key)`` walks ``SyncadeConfig``'s schema to the leaf field's declared
    type, raising ``UnknownKey`` for an unknown path or a non-scalar (section/roster) leaf."""

    def test_producer_scalar_literal(self):
        assert config_keys.resolve_annotation("producer.thinking") == Thinking

    def test_reviewer_indexed_field(self):
        assert config_keys.resolve_annotation("reviewers.0.permissions") == ReviewerPermissions

    def test_loop_int(self):
        assert config_keys.resolve_annotation("loop.max_rounds") is int

    def test_loop_optional_float(self):
        assert config_keys.resolve_annotation("loop.budget_usd") == (float | None)

    def test_review_list_str(self):
        assert config_keys.resolve_annotation("review.strip_repo_context_files") == list[str]

    def test_top_level_scalar_path(self):
        assert config_keys.resolve_annotation("worktree_base") is Path

    def test_check_indexed_literal(self):
        assert config_keys.resolve_annotation("checks.0.severity") == CheckSeverity

    def test_unknown_field_raises(self):
        with pytest.raises(config_keys.UnknownKey):
            config_keys.resolve_annotation("producer.nope")

    def test_unknown_section_raises(self):
        with pytest.raises(config_keys.UnknownKey):
            config_keys.resolve_annotation("nope")

    def test_whole_section_not_settable(self):
        with pytest.raises(config_keys.UnknownKey):
            config_keys.resolve_annotation("producer")

    def test_whole_roster_not_settable(self):
        with pytest.raises(config_keys.UnknownKey):
            config_keys.resolve_annotation("reviewers")

    def test_reviewer_element_model_not_settable(self):
        with pytest.raises(config_keys.UnknownKey):
            config_keys.resolve_annotation("reviewers.0")

    def test_indexing_a_scalar_list_is_unknown_key(self):
        # a list[str] field is set as a whole (CSV), not walked by element index
        with pytest.raises(config_keys.UnknownKey):
            config_keys.resolve_annotation("review.strip_repo_context_files.0")

    def test_dict_roster_is_not_settable(self):
        # a dict-shaped roster (pricing.models) is not a settable scalar
        with pytest.raises(config_keys.UnknownKey):
            config_keys.resolve_annotation("pricing.models")
