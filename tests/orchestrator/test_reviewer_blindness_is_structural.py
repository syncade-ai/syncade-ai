"""Reviewers never see the producer's narrative — structurally, not by config. PR-h-03 item 7.

`review.include_producer_summary` was declared, documented as a blindness control, editable
via `--config set`, and rendered in the menu — with NO runtime reader anywhere in `src/`.
An operator could set it, see it accepted, and believe a guarantee that no code enforced.
That is worse than an absent knob, so item 7 deleted it (brief D1(a)).

Deleting a safety knob is only honest if the guarantee it NAMED is real. It is, by
construction rather than by branch:

- `render_reviewer_prompt` has no parameter that could carry producer narrative;
- cross-round context comes from two DIFFERENT loaders —
  `load_prior_reviewer_response_text` for reviewers, `load_prior_producer_response_text`
  for the producer — so there is no flag to get wrong.

This test asserts the OUTCOME end-to-end, because both facts above are refactorable and the
property is the thing that must survive them.
"""

from __future__ import annotations

import subprocess

import pydantic
import pytest

from syncade.adapters.fake import FakeAdapter, FakeProducerAdapter
from syncade.adapters.producer import ProducerOutput
from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _RoundCyclingSynth,
    _ship,
    _synth_clean,
    _synth_with_blocker,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)

#: Distinctive enough that a substring hit cannot be coincidence.
_LEAK = "PRODUCER-NARRATIVE-CANARY-9f3a1c"


def _two_round_config() -> SyncadeConfig:
    """Two reviewers named to match the synth fixtures' provenance, and a second round."""
    return SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_rounds": 2},
    )


def test_round_1_reviewers_never_receive_the_round_0_producer_narrative(repo_with_pr_doc):
    repo, pr_doc = repo_with_pr_doc

    round_1_reviewers = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
    adapters = [FakeAdapter(canned_output=_no_ship()) for _ in range(2)] + round_1_reviewers

    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_two_round_config(),
        adapter_factory=_factory_returning(*adapters),
        synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_clean()),
        producer_adapter=FakeProducerAdapter(
            commit_message="fix: round 0",
            canned_output=ProducerOutput(narrative_text=_LEAK),
        ),
        logger=Logger(level="quiet"),
    )

    assert result.final_round == 1, "the test needs a SECOND round to have anything to leak into"

    for reviewer in round_1_reviewers:
        assert reviewer.invocations, "round-1 reviewer was never dispatched"
        _config, _worktree, prompt = reviewer.invocations[0]
        assert _LEAK not in prompt, (
            "a round-1 reviewer received the round-0 producer's narrative. Reviewer blindness "
            "is a core invariant and has no config switch — it must hold unconditionally."
        )


def test_the_deleted_knob_is_gone_from_the_whole_config_surface(tmp_path, monkeypatch):
    """Not just off the model: unsettable, and rejected in a config file.

    `ReviewConfig` is `extra="forbid"`, so a leftover line in someone's config.toml now
    fails loudly rather than being silently ignored — which is the correct outcome for a
    knob that never did anything.
    """
    from syncade.cli.config_keys import UnknownKey, resolve_annotation
    from syncade.config import ReviewConfig

    assert "include_producer_summary" not in ReviewConfig.model_fields

    with pytest.raises(UnknownKey):
        resolve_annotation("review.include_producer_summary")

    with pytest.raises(pydantic.ValidationError):
        ReviewConfig(include_producer_summary=False)
