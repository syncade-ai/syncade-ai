"""No operator-facing surface may describe the pre-import producer authority model.

**Why this is a derived sweep and not another list.** Four consecutive dogfood rounds raised this
as a blocker, each time in a new spelling on a surface the previous round had not enumerated:
"at the Item 3 checkpoint" (round 0), "confined is the only supported value" (round 1),
"candidates preserved outside the operator repository" (round 2), "yolo removes this enforcement
boundary" (round 3 — introduced by the fix for round 2). Each round a reviewer enumerated ~20
locations, a producer fixed exactly those, and more existed. That is the enumeration signature
this repo has now hit for the fourth time; `CLAUDE.md` records the same arc for the `.git`-shape
scan, the `--base` allowlist, and the installer-manifest bypass. Every one of them died the same
way — by re-deriving the requirement so that no list of instances was needed.

So the FILE SET here is derived from the repo (every tracked operator- or developer-facing text
surface), not hand-listed. A new doc, a new skill copy, or a new module that renders operator text
is covered the day it is added, which is the property a list can never have.

**What is excluded, and why it is principled rather than convenient.** Historical records are
supposed to contain these claims — they record what was true when written, and rewriting them
would be falsifying the record. The exclusions are therefore exactly the three kinds of document
whose job is to preserve a past state: PR briefs, the archived docs tree, and dated
audit/review/dogfood logs. Nothing is excluded because it was inconvenient to fix.

**What this does NOT check.** That the current claims are *correct* — only that the specific
retired ones are absent. A wrong new claim is a review problem; a resurrected old one is a drift
problem, and this is the drift guard.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.authority_scan import (
    _ITEM_NUMBERING,
    _REPO,
    _RETIRED_CLAIMS,
    _flatten,
    _hits,
    _operator_facing_docs,
    _read_tracked,
    _rendered_strings,
    _tracked_surfaces,
)
from tests.persistence_sweep_helpers import (
    sweep_persist_current_findings_md,
    sweep_persist_deactivated_blockers_decision_needed,
    sweep_persist_decision_needed,
    sweep_persist_findings_md,
    sweep_persist_handoff,
    sweep_persist_loop_summary,
    sweep_persist_producer_result,
    sweep_persist_reviewer_result,
    sweep_persist_run_summary,
    sweep_persist_synthesizer_result,
)

_RENDER_AT = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


# Claims that were TRUE before trusted import landed and are false now. Each is anchored on the
# distinctive phrase rather than a whole sentence, so a reflow or rewording cannot smuggle one back.
def test_the_sweep_actually_covers_the_surfaces_that_kept_regressing():
    """A derived file set is only a guard if it really reaches the files that failed.

    Without this, a `git ls-files` pattern that silently matched nothing would make every
    assertion below vacuously pass — the failure mode a drift test is least able to notice.

    Files marked "optional" are intentionally absent from stripped review workspaces (e.g.
    AGENTS.md, CLAUDE.md). The coverage assertion for those is skipped when the file does not
    exist on disk — the sweep still covers them in full product trees, and skipping rather than
    failing lets the focused drift suite run green in the expected exported workspace.
    """
    surfaces = set(_tracked_surfaces())
    # These files are intentionally stripped from exported review workspaces; skip the
    # per-file presence assertion when the file is absent from disk.
    # A named surface may legitimately be ABSENT, and this check has now been broken by three
    # separate environments that make it so — each time patched as a special case:
    #
    #   1. actor workspaces      `REVIEWER_STRIP_FILES` deletes CLAUDE.md / AGENTS.md
    #   2. the test worktree     same strip; the file is TRACKED and absent simultaneously
    #   3. the public snapshot   `scripts/oss-stage.sh`'s allowlist excludes dev-only docs, and
    #                            `oss-scrub.py` rewrites references to them — it rewrote the
    #                            STRING LITERAL "this README" in this list into
    #                            "this README", so the assertion looked for a file by a name the
    #                            scrubber had invented.
    #
    # So the rule is stated once instead of a fourth special case: **any named surface that is not
    # on disk is skipped.** Non-vacuity is carried by the count assertion below, which is what
    # actually protects against the sweep collapsing — a named-file list can only ever prove the
    # sweep reaches files that exist in THIS environment.
    for regressed in (
        "README.md",
        "SECURITY.md",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/config-reference.md",
        "docs/how-to-use.md",
        "this README",
        "the design docs",
        "src/syncade/persistence/loop_summary_text.py",
        "src/syncade/persistence/decision_needed.py",
        "src/syncade/cli/parser.py",
        "src/syncade/config_producer.py",
        ".claude/skills/syncade/SKILL.md",
        ".codex/skills/syncade/SKILL.md",
        "src/syncade/skills/claude/SKILL.md",
        "src/syncade/skills/codex/SKILL.md",
    ):
        if not (_REPO / regressed).exists():
            continue  # stripped from an actor workspace, or excluded from the public snapshot
        assert regressed in surfaces, f"{regressed} fell out of the derived sweep"
    assert len(surfaces) > 100, f"sweep collapsed to {len(surfaces)} files"


def test_operator_facing_docs_carry_no_pr_item_numbering():
    """An operator has no way to know what "Item 3" means; it is our internal scaffolding."""
    hits = [
        f"  {rel}: ...{_flatten(t)[max(0, m.start() - 50) : m.end() + 50]}..."
        for rel in _operator_facing_docs()
        for t in [_read_tracked(rel)]
        if t is not None
        for m in _ITEM_NUMBERING.finditer(_flatten(t))
    ]
    assert not hits, "PR item numbering in operator-facing docs:\n" + "\n".join(hits)


def test_no_rendered_string_carries_pr_item_numbering():
    """Comments and docstrings may cite an item as provenance; OUTPUT may not.

    This is the half that actually bit: `loop_summary_text.py`'s next-steps strings told
    operators about "the Item 3 checkpoint" in a file they read after every run.
    """
    hits = [
        f"  {rel}:{lineno}: {value.strip()[:100]}"
        for rel in _tracked_surfaces()
        if rel.endswith(".py")
        for lineno, value in _rendered_strings(rel)
        if _ITEM_NUMBERING.search(value)
    ]
    assert not hits, "PR item numbering inside rendered strings:\n" + "\n".join(hits)


@pytest.mark.parametrize("claim", sorted(_RETIRED_CLAIMS), ids=lambda c: c.replace(" ", "-"))
def test_no_operator_facing_surface_repeats_a_retired_claim(claim):
    hits = _hits(_RETIRED_CLAIMS[claim])
    assert not hits, f"retired claim ({claim}) still published in:\n" + "\n".join(hits)


# --- The load-bearing layer: read the OUTPUT, not the source. -------------------------------
#
# Static analysis cannot decide which strings reach an operator, and every attempt to approximate
# it was beaten by the next construction mechanism a reviewer named. These two tests sidestep the
# question entirely: one reads the actual runtime strings, the other reads actual rendered
# artifacts. Both are exact, and neither cares whether the text was built with an f-string, `%`,
# `.format()`, concatenation, or anything invented later.


def test_operator_text_tables_carry_no_item_numbering():
    """The operator-facing text tables, read as runtime objects rather than parsed from source.

    Every instance the blind panel found lived in one of these dicts. Importing them makes the
    check exhaustive over their VALUES with no parsing and no approximation.

    Covers both loop_summary_text (loop-summary.md tables) and run_summary_next_steps
    (per-round summary.md tables), since a str.join-built entry in either bypasses the
    source-constant scan.
    """
    from syncade.persistence import loop_summary_text as text_module
    from syncade.persistence import run_summary_next_steps as ns_module

    hits = [
        f"  loop_summary_text.{name}[{key!r}]: {value.strip()[:100]}"
        for name in ("_LOOP_NEXT_STEPS", "_EMPTY_SERIES_REASON_NOTES")
        for key, value in getattr(text_module, name).items()
        if _ITEM_NUMBERING.search(value)
    ]
    # Per-round next-steps dict
    hits += [
        f"  run_summary_next_steps._NEXT_STEPS[{key!r}]: {value.strip()[:100]}"
        for key, value in ns_module._NEXT_STEPS.items()
        if _ITEM_NUMBERING.search(value)
    ]
    # All _NEXT_STEPS_* string constants from run_summary_next_steps
    hits += [
        f"  run_summary_next_steps.{name}: {value.strip()[:100]}"
        for name in dir(ns_module)
        if name.startswith("_NEXT_STEPS_") and isinstance(getattr(ns_module, name), str)
        for value in [getattr(ns_module, name)]
        if _ITEM_NUMBERING.search(value)
    ]
    assert not hits, "PR item numbering in operator text tables:\n" + "\n".join(hits)


def test_candidate_location_note_carries_no_item_numbering():
    """candidate_location_note renders conditional text that the empty-rounds rendered sweep misses.

    The rendered loop-summary sweep passes rounds=[] so candidate_location_note returns None —
    any item numbering injected into that function bypasses both the source-constant scan (f-strings
    are not constants) and the rendered sweep (None is not rendered).  Test it directly with
    synthetic round-like objects that produce each conditional branch.
    """
    from types import SimpleNamespace

    from syncade.persistence.loop_summary_text import candidate_location_note

    def _round(ending_sha, recovery_ref):
        candidate_import = SimpleNamespace(recovery_ref=recovery_ref)
        producer = SimpleNamespace(
            starting_sha="a" * 40,
            ending_sha=ending_sha,
            candidate_import=candidate_import,
        )
        return SimpleNamespace(producer_result=producer)

    # Branch 1: candidate with recovery_ref — "IS in this repository" path
    text_with_ref = candidate_location_note(
        [_round("b" * 40, "refs/syncade/recovery/run-1/round-0/" + "b" * 40)],
        final_exit_code=20,
    )
    assert text_with_ref is not None, "expected a non-None note when ending_sha differs"
    assert not _ITEM_NUMBERING.search(_flatten(text_with_ref)), (
        f"Item numbering in candidate_location_note (with recovery_ref): {text_with_ref!r}"
    )

    # Branch 2: candidate without recovery_ref — "ONLY copy" path
    text_no_ref = candidate_location_note(
        [_round("b" * 40, None)],
        final_exit_code=20,
    )
    assert text_no_ref is not None, "expected a non-None note when ending_sha differs"
    assert not _ITEM_NUMBERING.search(_flatten(text_no_ref)), (
        f"Item numbering in candidate_location_note (no recovery_ref): {text_no_ref!r}"
    )


def test_per_round_next_steps_with_producer_carry_no_item_numbering():
    """Per-round summary.md next-step text from _resolve_next_steps_with_producer.

    The rendered loop-summary sweep passes rounds=[] and never invokes
    _resolve_next_steps_with_producer, so a dynamically constructed Item N string
    in any branch reaches operator-visible summary.md while bypassing the existing
    sweep layers. This test calls the resolver for every producer outcome branch.
    """
    from syncade.adapters.producer import ProducerOutput
    from syncade.persistence.run_summary_next_steps import _resolve_next_steps_with_producer
    from syncade.process import SubprocessError
    from syncade.producer import ProducerResult
    from syncade.producer_escalation import ProducerEscalation
    from syncade.producer_import import CandidateImportResult

    _sha_a = "a" * 40
    _sha_b = "b" * 40
    recovery = "refs/syncade/recovery/run-1/round-0/" + _sha_b
    _esc = ProducerEscalation(
        finding_indices=[0],
        finding="test finding",
        decision="A or B?",
        options=["A", "B"],
        rationale="spec conflict",
    )

    def _imp(status, ref=None, err=None):
        return CandidateImportResult(status=status, recovery_ref=ref, error=err)

    def _pr(outcome, candidate_import, *, moved=True, escalation=None):
        ending = _sha_b if moved else _sha_a
        return ProducerResult(
            outcome=outcome,
            starting_sha=_sha_a,
            ending_sha=ending,
            duration_seconds=1.0,
            output=None if outcome == "subprocess_error" else ProducerOutput(narrative_text="ok"),
            error=SubprocessError("crash") if outcome == "subprocess_error" else None,
            candidate_import=candidate_import,
            escalation=escalation,
        )

    cases = [
        ("committed/imported", _pr("committed", _imp("imported", recovery))),
        ("committed/error+ref", _pr("committed", _imp("error", recovery, "cleanup failed"))),
        ("committed/error-no-ref", _pr("committed", _imp("error", None, "import failed"))),
        ("committed/no-import", _pr("committed", None)),
        ("stalled", _pr("stalled", None, moved=False)),
        ("escalated/honored", _pr("escalated", None, moved=False, escalation=_esc)),
        ("escalated/rejected", _pr("escalated", None, moved=False, escalation=_esc)),
        ("subprocess_error/moved", _pr("subprocess_error", None, moved=True)),
        ("subprocess_error/no-move", _pr("subprocess_error", None, moved=False)),
    ]

    hits: list[str] = []
    # Every parameter the resolver branches on, not just the outcome. Pinning `exit_code=30`
    # left five of six exit branches unrendered, and `branch_already_advanced` unrendered
    # entirely — a claim reachable only at exit 60 would have passed.
    for label, producer in cases:
        honored = label == "escalated/honored"
        for exit_code in (0, 10, 20, 30, 40, 60):
            for advanced in (True, False):
                text = _resolve_next_steps_with_producer(
                    exit_code=exit_code,
                    producer_result=producer,
                    escalation_honored=honored,
                    branch_already_advanced=advanced,
                )
                flat = _flatten(text)
                for match in _ITEM_NUMBERING.finditer(flat):
                    start = max(0, match.start() - 50)
                    hits.append(
                        f"  {label!r} exit={exit_code} advanced={advanced}: "
                        f"...{flat[start : match.end() + 50]}..."
                    )
    assert not hits, (
        "PR item numbering in per-round summary next-steps with producer:\n" + "\n".join(hits)
    )


# Writers whose output an operator reads as prose. The SET is derived from
# `syncade.persistence` below and checked against this map, so a new markdown writer fails the
# coverage test until it is swept — the list cannot silently fall behind, which is the property
# that a hand-maintained list of renderers never has. Writers producing structured data rather
# than prose (`persist_*_manifest`, `persist_run_init`, `persist_dispatch_record`,
# `persist_last_reviewed`) carry no authored claims and are named here as deliberate exclusions
# rather than omitted.
_PROSE_WRITERS = frozenset(
    {
        "persist_loop_summary",
        "persist_run_summary",
        "persist_handoff",
        "persist_decision_needed",
        "persist_deactivated_blockers_decision_needed",
        "persist_findings_md",
        "persist_current_findings_md",
        # These three write operator-readable .error.txt prose (exception message +
        # authored context lines) alongside their structured artifacts.
        "persist_producer_result",
        "persist_reviewer_result",
        "persist_synthesizer_result",
    }
)
_STRUCTURED_WRITERS = frozenset(
    {
        "persist_check_result",
        "persist_dispatch_record",
        "persist_last_reviewed",
        "persist_loop_manifest",
        "persist_round_manifest",
        "persist_run_init",
        "persist_test_run_result",
    }
)


def test_every_persistence_writer_is_classified():
    """A new writer must be triaged, not silently uncovered.

    The rendered sweep below can only be as good as its writer set, and the blind panel found
    exactly that gap twice: the sweep covered `loop-summary.md` while per-round `summary.md`,
    `handoff.md` and `decision-needed.md` went unscanned. Deriving the set from the package and
    asserting the classification is total means the next renderer breaks this test on the day it
    is added rather than on the day someone remembers.
    """
    import syncade.persistence as persistence

    exported = {name for name in dir(persistence) if name.startswith("persist_")}
    unclassified = exported - _PROSE_WRITERS - _STRUCTURED_WRITERS
    assert not unclassified, (
        f"unclassified persistence writer(s): {sorted(unclassified)}. Add each to _PROSE_WRITERS "
        "(and to the rendered sweep) or to _STRUCTURED_WRITERS with a reason."
    )
    stale = (_PROSE_WRITERS | _STRUCTURED_WRITERS) - exported
    assert not stale, f"classification names a writer that no longer exists: {sorted(stale)}"


def test_rendered_loop_summaries_carry_no_item_numbering(tmp_path):
    """Render `loop-summary.md` for EVERY termination reason and scan what comes out.

    This is the layer that holds. It exercises the composition — next-steps text, the
    empty-series note, and `candidate_location_note` together — so a claim assembled at runtime
    from individually clean pieces is still caught. The reason set comes from `TerminationReason`
    itself, so a new terminal state is covered the day it is added.
    """
    import typing

    from syncade.orchestrator.results import TerminationReason
    from syncade.persistence.loop_summary import persist_loop_summary

    reasons = typing.get_args(TerminationReason)
    assert reasons, "TerminationReason resolved to nothing — the sweep would be vacuous"

    hits: list[str] = []
    for index, reason in enumerate(reasons):
        run_dir = tmp_path / f"run-{index}"
        run_dir.mkdir()
        rendered = persist_loop_summary(
            run_dir,
            final_exit_code=20,
            final_round=0,
            termination_reason=reason,
            rounds=[],
            max_rounds=1,
            started_at=_RENDER_AT,
            completed_at=_RENDER_AT,
        ).read_text(encoding="utf-8")
        flat = _flatten(rendered)
        for match in _ITEM_NUMBERING.finditer(flat):
            start = max(0, match.start() - 60)
            hits.append(f"  reason={reason!r}: ...{flat[start : match.end() + 60]}...")
    assert not hits, "PR item numbering in rendered loop summaries:\n" + "\n".join(hits)


# --- Rendered-output sweep for ALL prose writers ------------------
#
# `test_every_persistence_writer_is_classified` proves every writer is in
# _PROSE_WRITERS or _STRUCTURED_WRITERS, but does NOT prove the prose writers
# are actually swept. This dict derives the sweep from _PROSE_WRITERS: it must
# cover exactly the same set, so adding a writer to _PROSE_WRITERS without
# adding a render function here fails the totality assertion below, and vice
# versa. A prose writer that can be added to the classification map without any
# sweep assertion firing is the exact gap this closes.
#
# The render helpers live in persistence_sweep_helpers to keep this file within
# the 500 code-LOC gate while the sweep covers all prose writers.

_PROSE_WRITER_SWEEP = {
    "persist_loop_summary": sweep_persist_loop_summary,
    "persist_run_summary": sweep_persist_run_summary,
    "persist_handoff": sweep_persist_handoff,
    "persist_decision_needed": sweep_persist_decision_needed,
    "persist_deactivated_blockers_decision_needed": (
        sweep_persist_deactivated_blockers_decision_needed
    ),
    "persist_findings_md": sweep_persist_findings_md,
    "persist_current_findings_md": sweep_persist_current_findings_md,
    "persist_producer_result": sweep_persist_producer_result,
    "persist_reviewer_result": sweep_persist_reviewer_result,
    "persist_synthesizer_result": sweep_persist_synthesizer_result,
}


def test_every_prose_writer_rendered_output_carries_no_item_numbering(tmp_path):
    """Every prose writer must be rendered and its output swept for item numbering.

    `test_every_persistence_writer_is_classified` proves classification is total, but
    does NOT prove the classified writers are actually swept. This test closes that gap
    by deriving the sweep from _PROSE_WRITERS: _PROSE_WRITER_SWEEP must cover exactly
    that set, so adding a writer to _PROSE_WRITERS without a render function fails the
    totality assertion, and vice versa.
    """
    unswept = _PROSE_WRITERS - set(_PROSE_WRITER_SWEEP)
    assert not unswept, (
        f"prose writers not in rendered sweep registry: {sorted(unswept)}. "
        "Add each to _PROSE_WRITER_SWEEP with a minimal render function."
    )
    extra = set(_PROSE_WRITER_SWEEP) - _PROSE_WRITERS
    assert not extra, (
        f"sweep registry names writers not in _PROSE_WRITERS: {sorted(extra)}. "
        "Add each to _PROSE_WRITERS or remove from _PROSE_WRITER_SWEEP."
    )

    hits: list[str] = []
    for name in sorted(_PROSE_WRITERS):
        d = tmp_path / name
        d.mkdir()
        text = _PROSE_WRITER_SWEEP[name](d)
        if text is None:
            continue
        flat = _flatten(text)
        for match in _ITEM_NUMBERING.finditer(flat):
            start = max(0, match.start() - 60)
            hits.append(f"  {name}: ...{flat[start : match.end() + 60]}...")
    assert not hits, "PR item numbering in prose writer rendered output:\n" + "\n".join(hits)


def test_the_producer_sweep_covers_every_authored_channel(tmp_path):
    """`persist_producer_result` emits TWO authored artifacts; the sweep must render both.

    The behavioural twin to `sweep_persist_producer_result`'s docstring. A drift guard whose
    classification is sound can still be partial underneath it: this writer was correctly
    classified as prose while its helper rendered `producer.error.txt` and silently skipped
    `producer.import.error.txt` — the trusted-import diagnostic that per-round next-steps text
    tells operators to open by name. Classifying a WRITER says nothing about how many prose
    ARTIFACTS it emits.

    An audit of the other prose writers found no second authored channel (reviewer and
    synthesizer each write one `.error.txt` beside child stdout/stderr and a structured
    `.parsed.json`), so this pins the one real case rather than generalising a rule for a class
    of one. If another writer gains a second authored artifact, this is the shape to copy.
    """
    from tests.persistence_sweep_helpers import sweep_persist_producer_result

    rendered = sweep_persist_producer_result(tmp_path / "sweep")

    # producer.error.txt — the authored moved-HEAD sentence.
    assert "before the subprocess_error outcome" in rendered
    # producer.import.error.txt — every import-failure shape that reaches an operator.
    for import_error in ("fetch failed", "quarantine cleanup failed", "not a descendant"):
        assert import_error in rendered, f"import-error channel not swept: {import_error!r}"
