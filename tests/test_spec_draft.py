"""Tests for :mod:`syncade.spec_draft` (PR-D — cold-drafter engine).

Mirrors the spec_audit test shape: schema/validator unit tests + a fake-adapter
driver exercise (no real codex except the @smoke test). The drafter is cold +
isolated (its own tempdir, env-scrubbed) and never raises — every failure maps to
``outcome="subprocess_error"``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.fake import FakeDrafterAdapter
from syncade.spec_draft import (
    Criterion,
    SpecDraftOutput,
    SpecDraftOutputError,
    _classify_outcome,
    parse_spec_draft_output,
    render_draft_spec_markdown,
    run_spec_draft,
)


def _ok_output(**overrides) -> SpecDraftOutput:
    base: dict = dict(
        proposal="Add a login button so returning users can sign in.",
        acceptance_criteria=[
            Criterion(text="A login button renders on the home page.", origin="transcribed"),
        ],
        deltas=[],
        assumptions=[],
    )
    base.update(overrides)
    return SpecDraftOutput(**base)


class TestSpecDraftSchema:
    def test_valid_construction(self):
        out = _ok_output()
        assert out.proposal.startswith("Add a login button")
        assert out.acceptance_criteria[0].origin == "transcribed"
        assert out.deltas == [] and out.assumptions == []

    def test_requires_at_least_one_criterion(self):
        with pytest.raises(ValidationError):
            SpecDraftOutput(proposal="x", acceptance_criteria=[])

    def test_criterion_origin_is_a_closed_literal(self):
        with pytest.raises(ValidationError):
            Criterion(text="something", origin="guessed")

    def test_proposal_rejects_whitespace_only(self):
        with pytest.raises(ValidationError):
            _ok_output(proposal="   ")

    def test_criterion_text_rejects_whitespace_only(self):
        with pytest.raises(ValidationError):
            SpecDraftOutput(
                proposal="ok",
                acceptance_criteria=[Criterion(text="   ", origin="inferred")],
            )

    def test_extra_forbidden(self):
        with pytest.raises(ValidationError):
            SpecDraftOutput(
                proposal="ok",
                acceptance_criteria=[Criterion(text="c", origin="inferred")],
                bogus_field=1,
            )

    def test_deltas_and_assumptions_optional(self):
        out = SpecDraftOutput(
            proposal="ok",
            acceptance_criteria=[Criterion(text="c", origin="inferred")],
        )
        assert out.deltas == [] and out.assumptions == []


class TestParseSpecDraftOutput:
    def test_round_trips_model_json(self):
        out = _ok_output()
        parsed = parse_spec_draft_output(out.model_dump_json())
        assert parsed.proposal == out.proposal

    def test_parses_fenced_json_with_prose_around_it(self):
        out = _ok_output()
        raw = f"Here is the draft:\n```json\n{out.model_dump_json()}\n```\nDone."
        parsed = parse_spec_draft_output(raw)
        assert parsed.acceptance_criteria[0].text == out.acceptance_criteria[0].text

    def test_garbage_raises(self):
        with pytest.raises(SpecDraftOutputError):
            parse_spec_draft_output("this is not json at all")


class TestRunSpecDraft:
    def test_well_formed_is_drafted_with_self_flag_preserved(self, tmp_path: Path):
        out = _ok_output(
            acceptance_criteria=[
                Criterion(text="explicit ask from the user", origin="transcribed"),
                Criterion(text="inferred detail", origin="inferred"),
            ],
            assumptions=["Assumed the button persists a session token."],
        )
        fake = FakeDrafterAdapter(canned_output=out)
        result = run_spec_draft(
            dialogue="User: build a login button",
            diff="diff --git a b",
            repo_root=tmp_path,
            adapter=fake,
        )
        assert result.outcome == "drafted"
        assert result.output is not None
        assert any(c.origin == "inferred" for c in result.output.acceptance_criteria)
        assert result.output.assumptions

    def test_malformed_output_is_subprocess_error_with_parse_exception(self, tmp_path: Path):
        fake = FakeDrafterAdapter(canned_text="not parseable json")
        result = run_spec_draft(dialogue="d", diff="", repo_root=tmp_path, adapter=fake)
        assert result.outcome == "subprocess_error"
        assert isinstance(result.error, SpecDraftOutputError)

    def test_subprocess_failure_maps_to_subprocess_error(self, tmp_path: Path):
        fake = FakeDrafterAdapter(
            canned_exception=ReviewerInvocationError(
                "codex boom", returncode=1, stdout="", stderr="boom"
            )
        )
        result = run_spec_draft(dialogue="d", diff="", repo_root=tmp_path, adapter=fake)
        assert result.outcome == "subprocess_error"
        assert result.output is None and result.error is not None

    def test_runs_in_isolated_tempdir(self, tmp_path: Path):
        fake = FakeDrafterAdapter(canned_output=_ok_output())
        run_spec_draft(dialogue="d", diff="", repo_root=tmp_path, adapter=fake)
        assert fake.invocations, "expected build_invocation to be called"
        workspace = fake.invocations[0][1]
        assert "syncade-draft-" in str(workspace), (
            "drafter must run in its own isolated tempdir, not repo_root"
        )

    def test_classify_outcome_always_drafted(self):
        # a draft is advisory — there is no blocker concept; well-formed → drafted
        assert _classify_outcome(_ok_output()) == "drafted"

    def test_malformed_per_repo_template_override_returns_subprocess_error(self, tmp_path: Path):
        """A .syncade/templates/spec_draft.md with unknown {placeholder} must not
        raise — it must return outcome='subprocess_error' per the no-raise contract."""
        templates_dir = tmp_path / ".syncade" / "templates"
        templates_dir.mkdir(parents=True)
        bad_template = templates_dir / "spec_draft.md"
        bad_template.write_text(
            "Read {dialogue_path} and {diff_path} and {unknown_key}.", encoding="utf-8"
        )
        # FakeDrafterAdapter is never reached because the KeyError fires first
        from syncade.adapters.fake import FakeDrafterAdapter

        fake = FakeDrafterAdapter(canned_output=_ok_output())
        result = run_spec_draft(dialogue="d", diff="", repo_root=tmp_path, adapter=fake)
        assert result.outcome == "subprocess_error"
        assert result.output is None
        # The error must name the unknown placeholder so the operator can fix the template
        assert result.error is not None
        assert "unknown_key" in str(result.error)


class TestRenderDraftSpecMarkdown:
    def test_no_base_ref_omits_base_flag(self):
        out = _ok_output()
        text = render_draft_spec_markdown(out, source_label="sess")
        assert "syncade <this-file>`" in text
        assert "--base" not in text

    def test_base_ref_included_in_ratification_instruction(self):
        out = _ok_output()
        text = render_draft_spec_markdown(out, source_label="sess", base_ref="HEAD~1")
        assert "syncade <this-file> --base HEAD~1`" in text


@pytest.mark.smoke
def test_real_codex_draft(tmp_path: Path):
    dialogue = (
        "User: I want a CLI flag --greet that prints a hello message and exits 0.\n\n"
        "Assistant: I'll add a --greet flag that prints 'hello' and exits 0."
    )
    diff = "diff --git a/cli.py b/cli.py\n+ add --greet flag\n"
    result = run_spec_draft(dialogue=dialogue, diff=diff, repo_root=tmp_path)
    assert result.outcome == "drafted", f"expected drafted, got {result.outcome}: {result.error}"
    assert result.output is not None and result.output.acceptance_criteria
