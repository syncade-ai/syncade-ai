from __future__ import annotations

import json
import subprocess

import pytest

from syncade.adapters.fake import FakeAdapter
from syncade.orchestrator import run_review
from syncade.synthesis import SynthesizerOutputError
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _ship,
    _two_reviewer_config,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestSynthesizerProvenanceValidation:
    """QA fix #7 (P0.2): cross-input validation in run_synthesizer
    catches synth provenance that points at non-existent reviewer
    names or out-of-range original_index values.

    The schema's ``provenance: min_length=1`` prevents the empty-
    provenance "invented finding" case but doesn't know about the
    input reviewer set, so a fabricated ``reviewer_name`` or
    ``original_index`` would slip through schema validation and
    render into ``findings.md`` with fake attribution. The
    orchestrator-level check rejects these as
    ``SynthesizerOutputError`` → exit 70.

    Tests use ``_RawSynthAdapter`` so a CUSTOM SynthesizerOutput
    (one whose provenance is ill-formed against the actual reviewer
    set) reaches the parse + validation path — the standard
    ``FakeSynthesizerAdapter`` won't work for this because
    constructing the ill-formed output as a pydantic model would
    fail at construction time IF we tried to do cross-input
    validation in the schema; the whole point of the orchestrator-
    level check is to handle the case where the SCHEMA accepts but
    the cross-input check rejects.
    """

    def _raw_synth_adapter(self, synth_json: str):
        """A fake synth adapter whose ``extract_final_text``
        returns hand-crafted JSON text. This routes through the real
        ``parse_synthesizer_output`` AND the new
        ``_validate_provenance_against_reviewers`` — exactly the path
        we need to exercise for this test class.
        """
        import os

        from syncade.adapters.base import Invocation
        from syncade.adapters.fake import _noop_argv

        class _RawSynthAdapter:
            name = "openai"

            def __init__(self):
                self.invocations = []

            def build_invocation(self, reviewer_config, worktree_path, prompt):
                self.invocations.append((reviewer_config, worktree_path, prompt))
                return Invocation(
                    argv=_noop_argv(),
                    cwd=worktree_path,
                    env=dict(os.environ),
                    stdin_text=None,
                    timeout_seconds=None,
                )

            def extract_final_text(self, result, *, empty_output_exception_class):
                del result, empty_output_exception_class
                return synth_json

        return _RawSynthAdapter()

    def test_ghost_reviewer_name_in_provenance_maps_to_exit_70(self, repo_with_pr_doc):
        """Both reviewers return SHIP with zero findings; the synth
        invents a provenance entry naming ``ghost-reviewer``
        (nobody's name). Cross-input validation MUST reject this so
        it never reaches findings.md."""
        repo, pr_doc = repo_with_pr_doc

        # Synth output with one finding whose provenance names a
        # non-existent reviewer. Schema accepts (provenance is non-
        # empty, name is non-whitespace); orchestrator-level check
        # must reject.
        synth_json = json.dumps(
            {
                "consolidated_findings": [
                    {
                        "description": "fabricated finding with ghost provenance",
                        "file": "src/x.py",
                        "severity": "blocker",
                        "provenance": [
                            {
                                "reviewer_name": "ghost-reviewer",
                                "original_severity": "blocker",
                                "original_index": 0,
                                "original_description": "I am not a real source",
                            }
                        ],
                        "dismissed": False,
                    }
                ],
                "synthesis_summary": "should be rejected before reaching findings.md",
            }
        )

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=self._raw_synth_adapter(synth_json),
        )
        from syncade.synthesis import SynthesizerOutputError

        assert result.exit_code == 70, (
            f"ghost-reviewer provenance should produce exit 70; got "
            f"{result.exit_code} (output: "
            f"{result.synth_result.output if result.synth_result else None})"
        )
        assert isinstance(result.synth_result.error, SynthesizerOutputError)
        # findings.md must NOT exist on this failure path
        assert not (result.artifacts.round_dir / "findings.md").exists()
        # synthesizer.error.txt MUST exist + name the ghost reviewer
        error_txt = result.artifacts.round_dir / "synthesizer.error.txt"
        assert error_txt.is_file()
        assert "ghost-reviewer" in error_txt.read_text()

    def test_zero_findings_provenance_uses_clean_error_text(self, repo_with_pr_doc):
        """R2.4: reviewer ``rv1`` has 0 findings; synth references
        ``rv1`` at ``original_index=999``. The previous error text
        rendered the GARBAGE phrase ``"valid indices are 0..-1 or
        empty if True"`` (computing ``max_index - 1`` when
        max_index=0 and using a bool-as-conditional fstring); the
        operator inspecting synthesizer.error.txt saw nonsense.

        R2.4 split the message: for max_index==0 the text says
        "produced 0 findings, so no original_index value is valid".
        Pin the new wording AND assert the broken old wording is
        absent.
        """
        repo, pr_doc = repo_with_pr_doc

        synth_json = json.dumps(
            {
                "consolidated_findings": [
                    {
                        "description": "finding with bogus original_index",
                        "file": None,
                        "severity": "minor",
                        "provenance": [
                            {
                                "reviewer_name": "rv1",
                                "original_severity": "minor",
                                "original_index": 999,
                                "original_description": "this index does not exist",
                            }
                        ],
                        "dismissed": False,
                    }
                ],
                "synthesis_summary": "should be rejected",
            }
        )

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=self._raw_synth_adapter(synth_json),
        )
        from syncade.synthesis import SynthesizerOutputError

        assert result.exit_code == 70
        assert isinstance(result.synth_result.error, SynthesizerOutputError)
        error_txt = result.artifacts.round_dir / "synthesizer.error.txt"
        assert error_txt.is_file()
        text = error_txt.read_text()
        # New clean text for the zero-findings case
        assert "produced 0 findings" in text
        assert "no original_index value is valid" in text
        assert "999" in text  # the offending index
        # Old broken text MUST be absent
        assert "0..-1" not in text
        assert "empty if True" not in text
        assert "empty if False" not in text

    def test_out_of_range_provenance_with_nonzero_findings_uses_range_text(self, repo_with_pr_doc):
        """R2.4: when the reviewer has SOME findings but the synth
        references an out-of-range index, the error message uses
        the range-based wording. Pin the nonzero case so a future
        refactor that collapses the two branches doesn't drift
        either message."""
        repo, pr_doc = repo_with_pr_doc

        # rv1 has 1 finding (_no_ship has one); synth references
        # rv1 at index=5 (out of range; valid range is 0..0).
        synth_json = json.dumps(
            {
                "consolidated_findings": [
                    {
                        "description": "finding with out-of-range index",
                        "file": None,
                        "severity": "minor",
                        "provenance": [
                            {
                                "reviewer_name": "rv1",
                                "original_severity": "minor",
                                "original_index": 5,
                                "original_description": "should reference index 0 not 5",
                            }
                        ],
                        "dismissed": False,
                    }
                ],
                "synthesis_summary": "should be rejected",
            }
        )

        adapters = [
            FakeAdapter(canned_output=_no_ship()),  # rv1: 1 finding
            FakeAdapter(canned_output=_ship()),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=self._raw_synth_adapter(synth_json),
        )
        from syncade.synthesis import SynthesizerOutputError

        assert result.exit_code == 70
        assert isinstance(result.synth_result.error, SynthesizerOutputError)
        text = (result.artifacts.round_dir / "synthesizer.error.txt").read_text()
        # Range-based wording for the nonzero-findings case
        assert "out of range" in text
        assert "produced 1 finding(s)" in text
        assert "valid indices are 0..0" in text  # not 0..-1
        assert "5" in text  # the offending index

    def test_valid_provenance_passes_through(self, repo_with_pr_doc):
        """Sanity check: a synth output whose provenance correctly
        references existing reviewers + in-bounds indices passes
        validation."""
        repo, pr_doc = repo_with_pr_doc

        # rv1 has 1 finding (index 0); synth provenance references it.
        synth_json = json.dumps(
            {
                "consolidated_findings": [
                    {
                        "description": "real finding, real attribution",
                        "file": "src/x.py",
                        "severity": "blocker",
                        "provenance": [
                            {
                                "reviewer_name": "rv1",
                                "original_severity": "blocker",
                                "original_index": 0,
                                "original_description": "missing thing",
                            }
                        ],
                        "dismissed": False,
                    }
                ],
                "synthesis_summary": "single-reviewer pass-through",
            }
        )

        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=self._raw_synth_adapter(synth_json),
        )

        # Active blocker → exit 30, findings.md present.
        assert result.exit_code == 30
        assert result.synth_result.output is not None
        assert result.synth_result.error is None
        assert (result.artifacts.round_dir / "findings.md").is_file()

    def test_zero_findings_synth_with_ghost_provenance_still_rejected(self, repo_with_pr_doc):
        """The brief's explicit case: both reviewers return zero
        findings; synth invents a finding with ghost-reviewer +
        original_index=999. Must reject before findings.md is
        written. This is the most direct test of the cannot-invent
        invariant under the orchestrator-level validation layer."""
        repo, pr_doc = repo_with_pr_doc

        synth_json = json.dumps(
            {
                "consolidated_findings": [
                    {
                        "description": "completely invented finding",
                        "file": "src/nonexistent.py",
                        "severity": "blocker",
                        "provenance": [
                            {
                                "reviewer_name": "phantom-reviewer",
                                "original_severity": "blocker",
                                "original_index": 999,
                                "original_description": "no source for this",
                            }
                        ],
                        "dismissed": False,
                    }
                ],
                "synthesis_summary": "the synth invented this whole entry",
            }
        )

        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=self._raw_synth_adapter(synth_json),
        )

        assert result.exit_code == 70
        assert not (result.artifacts.round_dir / "findings.md").exists()

    def test_exact_duplicate_blockers_split_and_downgraded_rejected(self, repo_with_pr_doc):
        """Defense-in-depth split guard: pass-through alone is not enough if
        the synth splits exact duplicate source blockers into separate
        downgraded findings. The orchestrator-level validator must reject the
        raw synth output before it can false-SHIP."""
        repo, pr_doc = repo_with_pr_doc

        synth_json = json.dumps(
            {
                "consolidated_findings": [
                    {
                        "description": "rv1 blocker split and downgraded",
                        "file": "src/x.py",
                        "severity": "minor",
                        "provenance": [
                            {
                                "reviewer_name": "rv1",
                                "original_severity": "blocker",
                                "original_index": 0,
                                "original_description": "missing thing",
                            }
                        ],
                        "severity_change_rationale": "claimed lower blast radius",
                    },
                    {
                        "description": "rv2 blocker split and downgraded",
                        "file": "src/x.py",
                        "severity": "minor",
                        "provenance": [
                            {
                                "reviewer_name": "rv2",
                                "original_severity": "blocker",
                                "original_index": 0,
                                "original_description": "missing thing",
                            }
                        ],
                        "severity_change_rationale": "claimed lower blast radius",
                    },
                ],
                "synthesis_summary": "split duplicate blockers",
            }
        )

        adapters = [FakeAdapter(canned_output=_no_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=self._raw_synth_adapter(synth_json),
        )

        assert result.exit_code == 70
        assert isinstance(result.synth_result.error, SynthesizerOutputError)
        assert not (result.artifacts.round_dir / "findings.md").exists()
        text = (result.artifacts.round_dir / "synthesizer.error.txt").read_text()
        assert "split/deactivated duplicate reviewer blocker" in text
