"""Synthesizer persistence tests — moved verbatim from the former
``tests/test_persistence.py`` (PR-R2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.persistence import (
    SynthesizerArtifactPaths,
    persist_round_manifest,
    persist_run_summary,
    persist_synthesizer_result,
)
from syncade.synthesizer.constants import SYNTHESIZER_MODEL
from tests.persistence._helpers import (
    _FIXED_STARTED_AT,
    _make_round_dir,
    _ship,
    _snapshot,
    _subprocess_result,
    _synth_output_empty,
    _synth_output_with_findings,
    _synth_result,
)


class TestPersistSynthesizerResultSuccess:
    def test_writes_stdout_stderr_and_parsed_json(self, tmp_path: Path):
        from syncade.synthesis import SynthesizerOutput

        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        paths = persist_synthesizer_result(round_dir, synth)

        assert isinstance(paths, SynthesizerArtifactPaths)
        assert paths.stdout == round_dir / "synthesizer.stdout"
        assert paths.stderr == round_dir / "synthesizer.stderr"
        assert paths.parsed == round_dir / "synthesizer.parsed.json"
        assert paths.error is None

        assert (round_dir / "synthesizer.stdout").read_text() == "synth stdout\n"
        assert (round_dir / "synthesizer.stderr").read_text() == "synth stderr\n"
        # parsed.json round-trips through pydantic
        loaded = SynthesizerOutput.model_validate_json(
            (round_dir / "synthesizer.parsed.json").read_text()
        )
        assert loaded.synthesis_summary.startswith("both reviewers")
        # No error.txt on success.
        assert not (round_dir / "synthesizer.error.txt").exists()

    def test_parsed_json_is_indented(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())
        persist_synthesizer_result(round_dir, synth)
        text = (round_dir / "synthesizer.parsed.json").read_text()
        # Indented JSON has many newlines (consolidated_findings × 2 ×
        # several fields each).
        assert text.count("\n") >= 20


class TestPersistSynthesizerResultFailure:
    def test_writes_error_txt_with_class_and_message(self, tmp_path: Path):
        from syncade.synthesis import SynthesizerOutputError

        round_dir = _make_round_dir(tmp_path)
        try:
            raise SynthesizerOutputError(
                "synthesizer output had no parseable SynthesizerOutput JSON"
            )
        except SynthesizerOutputError as exc:
            err = exc
        synth = _synth_result(error=err, output=None)
        paths = persist_synthesizer_result(round_dir, synth)

        assert paths.parsed is None
        assert paths.error == round_dir / "synthesizer.error.txt"
        error_text = paths.error.read_text()
        assert "SynthesizerOutputError" in error_text
        assert "no parseable" in error_text
        assert "Traceback" in error_text  # raised+caught has a traceback
        # stdout / stderr still written
        assert paths.stdout.is_file()
        assert paths.stderr.is_file()
        # No parsed.json on failure
        assert not (round_dir / "synthesizer.parsed.json").exists()

    def test_no_subprocess_result_still_writes_empty_streams(self, tmp_path: Path):
        from syncade.synthesizer import SynthesizerResult

        round_dir = _make_round_dir(tmp_path)
        # SubprocessNotFoundError before any subprocess output: raw=None
        synth = SynthesizerResult(
            output=None,
            error=RuntimeError("codex binary missing"),
            duration_seconds=0.1,
            raw_subprocess_result=None,
        )
        paths = persist_synthesizer_result(round_dir, synth)
        assert paths.stdout.read_text() == ""
        assert paths.stderr.read_text() == ""
        assert paths.error is not None
        assert "codex binary missing" in paths.error.read_text()


class TestPersistRoundManifestSynthesizerSection:
    """PR-7: manifest.json gains a `synthesizer` section."""

    def _ship_dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv",
                    provider="anthropic",
                    output=_ship(),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )

    def test_synthesizer_skipped_renders_null(self, tmp_path: Path):
        """When the synth phase was skipped (synth_result=None),
        manifest['synthesizer'] is null."""
        round_dir = _make_round_dir(tmp_path)
        path = persist_round_manifest(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=40,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
        )
        manifest = json.loads(path.read_text())
        assert "synthesizer" in manifest
        assert manifest["synthesizer"] is None

    # Schema-symmetry key set the QA-fix tests pin against (PR-7 fix #11).
    # Every manifest synthesizer section — success OR failure — must
    # carry exactly these keys, with nullable values where not
    # applicable. Downstream tooling can index any of them without
    # KeyError-guarding by outcome.
    _SCHEMA_KEYS = {
        "outcome",
        "provider",
        "model",
        "stdout_path",
        "stderr_path",
        "parsed_path",
        "error_path",
        "duration_seconds",
        "tokens",
        "cost_usd",
        "cost_source",
        # PR-v2-24: the resolved auth mode rides WITH the cost, so an artifact is
        # self-describing. cost_usd alone cannot say whether those dollars were money or
        # an API-equivalent valuation of subscription traffic; without this on disk,
        # --metrics would have to guess retroactively.
        "auth_mode",
        "error_type",
        "dismissed_count",
        "active_blocker_count",
        "active_minor_count",
        "active_nit_count",
    }

    def test_synthesizer_success_renders_counts(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())
        path = persist_round_manifest(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
        )
        manifest = json.loads(path.read_text())
        section = manifest["synthesizer"]
        # PR-7 fix #11: schema symmetry — every documented key
        # must be present on success too.
        assert set(section.keys()) == self._SCHEMA_KEYS
        assert section["outcome"] == "success"
        assert section["provider"] == "openai"
        assert section["model"] == SYNTHESIZER_MODEL
        assert section["stdout_path"] == "synthesizer.stdout"
        assert section["parsed_path"] == "synthesizer.parsed.json"
        assert section["error_path"] is None
        # PR-7 fix #11: error_type explicitly null on success.
        assert section["error_type"] is None
        # Counts: one active blocker + one dismissed nit
        assert section["dismissed_count"] == 1
        assert section["active_blocker_count"] == 1
        assert section["active_minor_count"] == 0
        assert section["active_nit_count"] == 0
        assert section["duration_seconds"] == pytest.approx(18.7)

    def test_synthesizer_failure_renders_error_path(self, tmp_path: Path):
        from syncade.synthesis import SynthesizerOutputError

        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(error=SynthesizerOutputError("unparseable"), output=None)
        path = persist_round_manifest(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=70,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
        )
        manifest = json.loads(path.read_text())
        section = manifest["synthesizer"]
        # PR-7 fix #11: schema symmetry — same key set as success.
        assert set(section.keys()) == self._SCHEMA_KEYS
        assert section["outcome"] == "failure"
        assert section["parsed_path"] is None
        assert section["error_path"] == "synthesizer.error.txt"
        assert section["error_type"] == "SynthesizerOutputError"
        # Counts are null on failure
        assert section["dismissed_count"] is None
        assert section["active_blocker_count"] is None
        assert section["active_minor_count"] is None
        assert section["active_nit_count"] is None

    def test_synthesizer_contract_violation_rejected_before_manifest(self):
        """``SynthesizerResult`` now enforces exactly-one output/error.

        This prevents the old half-persistence path where a
        ``SynthesizerResult(output=None, error=None)`` could reach
        manifest rendering after writing partial synth artifacts.
        """
        from syncade.synthesizer import SynthesizerResult

        with pytest.raises(ValueError, match="exactly one of output or error"):
            SynthesizerResult(
                output=None,
                error=None,
                duration_seconds=0.5,
                raw_subprocess_result=None,
            )

    def test_synthesizer_contract_rejects_output_and_error_together(self):
        from syncade.synthesizer import SynthesizerResult

        with pytest.raises(ValueError, match="exactly one of output or error"):
            SynthesizerResult(
                output=_synth_output_empty(),
                error=RuntimeError("ambiguous result"),
                duration_seconds=0.5,
                raw_subprocess_result=None,
            )


class TestPersistRunSummarySynthesizerSection:
    """PR-7: summary.md gains a Synthesizer subsection between the
    per-reviewer entries and Next-steps."""

    def _ship_dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv",
                    provider="anthropic",
                    output=_ship(),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )

    def test_synthesizer_skipped_section_present(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=40,
            started_at=_FIXED_STARTED_AT,
            synth_result=None,
        ).read_text()
        assert "## Synthesizer" in text
        assert "skipped" in text.lower()

    def test_synthesizer_success_renders_counts_and_findings_md_link(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
        ).read_text()
        assert "## Synthesizer" in text
        assert "**Outcome:** success" in text
        # Counts visible
        assert "1 active blocker" in text
        # Findings.md link present
        assert "findings.md" in text
        # Synthesis summary rendered
        assert "Two findings consolidated" in text

    def test_synthesizer_failure_links_error_txt(self, tmp_path: Path):
        from syncade.synthesis import SynthesizerOutputError

        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(error=SynthesizerOutputError("unparseable"), output=None)
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._ship_dispatch(),
            exit_code=70,
            started_at=_FIXED_STARTED_AT,
            synth_result=synth,
        ).read_text()
        assert "## Synthesizer" in text
        assert "**Outcome:** failure" in text
        assert "SynthesizerOutputError" in text
        assert "synthesizer.error.txt" in text
        assert "synthesizer.stdout" in text

    def test_synth_contract_violation_rejected_before_summary(self):
        """The invalid both-None shape cannot reach summary rendering."""
        from syncade.synthesizer import SynthesizerResult

        with pytest.raises(ValueError, match="exactly one of output or error"):
            SynthesizerResult(
                output=None,
                error=None,
                duration_seconds=0.1,
                raw_subprocess_result=None,
            )
