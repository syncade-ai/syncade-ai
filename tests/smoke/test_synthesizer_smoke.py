"""End-to-end smoke test for :func:`syncade.synthesizer.run_synthesizer`
against the real ``codex`` CLI.

Catches the "the synthesizer module compiles and unit-tests but
doesn't actually work when fed real codex output" failure mode —
exactly what the rest of the test surface can't validate.

The synthesizer is the only consolidation surface in syncade; if
codex returns malformed JSON, an unparseable agent_message, or
silently deactivates unanimous blockers (which would be schema-
rejected, but the smoke also verifies prompt compliance), the
operator-facing ``findings.md`` will be wrong in a way that's
expensive to debug from a unit-test diff.

Gated behind ``pytest -m smoke``. The default ``pytest`` run
(configured in ``[tool.pytest.ini_options]`` with
``addopts = "-m 'not smoke'"``) explicitly deselects.

Coverage:

1. ``test_synthesizer_dedups_overlapping_findings_and_respects_unanimous_blocker_rule`` —
   hand-crafts two reviewer outputs (overlap + disagreement +
   false-positive shape), runs the real codex synthesizer against
   the production template, asserts:

   - Output parses as ``SynthesizerOutput``.
   - The deliberate overlap is deduplicated — there exists a
     ``ConsolidatedFinding`` with provenance from both reviewers.
   - ``synthesis_summary`` is substantively non-empty
     (``len > 50``). The schema requires non-empty already; the
     length check catches the "model emitted one-character summary"
     compliance failure that schema validation can't.
   - The unanimous-blocker case (both reviewers, both blocker)
     remains active. The schema validator would reject dismissal or
     downgrade anyway,
     but the smoke verifies the model RESPECTS THE PROMPT
     INSTRUCTION independently — without that, every smoke run
     would fail with a SynthesizerOutputError and the test wouldn't
     surface the deeper "model didn't read the prompt" signal.

Per the brief: don't assert specific finding content (models vary).
Assert structure + schema + the dedup invariant.
"""

from __future__ import annotations

import shutil

import pytest

from syncade.adapters.openai import CodexAdapter
from syncade.config import SyncadeConfig
from syncade.dispatcher import ReviewerRunResult
from syncade.findings import Finding, ReviewerOutput
from syncade.synthesis import SynthesizerOutput
from syncade.synthesizer import run_synthesizer

# The cheapest model the user's codex install is known to authorize
# against; must match the Codex reviewer's default model in
# :func:`syncade.config._default_reviewers`. The synthesizer is
# hardcoded to use this model too (see
# :data:`syncade.synthesizer.SYNTHESIZER_MODEL`); this constant
# mirrors that pin for smoke-time sanity checking.
_CHEAP_MODEL = "gpt-5.5"


def _skip_if_no_codex() -> None:
    if shutil.which("codex") is None:
        pytest.skip("codex CLI not on PATH — install codex-cli to run smoke tests")


def test_smoke_synthesizer_model_matches_default_config():
    """The synthesizer is hardcoded to a specific codex model; that
    pin must match the Codex reviewer's default model in
    ``SyncadeConfig()``.

    Same regression guard as the codex smoke's
    :func:`test_smoke_model_matches_default_config` — if somebody
    bumps one but not the other, the divergence surfaces here at
    smoke-collection time rather than at production-run time when
    a user discovers their reviewers and synthesizer were pinned to
    different model identifiers.
    """
    from syncade.synthesizer import SYNTHESIZER_MODEL

    cfg = SyncadeConfig()
    codex_rev = next(r for r in cfg.reviewers if r.provider == "openai")
    assert SYNTHESIZER_MODEL == codex_rev.model == _CHEAP_MODEL, (
        f"synthesizer model pin {SYNTHESIZER_MODEL!r} does not match "
        f"SyncadeConfig() codex default {codex_rev.model!r} or smoke "
        f"constant {_CHEAP_MODEL!r}. Update them together — see "
        f"syncade.synthesizer.SYNTHESIZER_MODEL and "
        f"syncade.config._default_reviewers."
    )


def _hand_crafted_reviewer_results() -> list[ReviewerRunResult]:
    """Two synthetic ReviewerOutputs designed to exercise every
    interesting synthesizer path:

    - **Overlap**: both reviewers flag "user.email missing NOT NULL"
      at blocker — the synthesizer MUST dedup into one
      ConsolidatedFinding with provenance from both. Schema would
      reject a dismissal of this unanimous blocker independently of
      whether the model respected the instruction.
    - **Disagreement**: reviewer 1 calls a finding blocker; reviewer
      2 doesn't surface it at all. Pass-through with single-reviewer
      provenance.
    - **Plausible dismissal target**: reviewer 1 flags a README hygiene
      issue as minor. The PR doc (constructed below) explicitly
      exempts README updates — the synthesizer SHOULD dismiss with
      rationale (but isn't required to, since the schema only
      enforces that dismissed entries HAVE rationale, not that
      they're correctly dismissed).
    """
    claude_output = ReviewerOutput(
        verdict="NO-SHIP",
        findings=[
            Finding(
                severity="blocker",
                file="src/db/schema.sql",
                line=14,
                spec_clause="acceptance: user.email NOT NULL",
                finding=(
                    "user.email column lacks NOT NULL constraint; spec "
                    "acceptance criterion explicitly requires it."
                ),
                evidence_cmd="grep -n email src/db/schema.sql",
                evidence_output="14:  email TEXT,",
            ),
            Finding(
                severity="blocker",
                file="src/auth/login.py",
                line=42,
                spec_clause="acceptance: reject null emails",
                finding=(
                    "login.py does not validate that email is non-empty before "
                    "DB write — the spec's null-rejection rule has no application-"
                    "layer enforcement."
                ),
                evidence_cmd=None,
                evidence_output=None,
            ),
            Finding(
                severity="minor",
                file="README.md",
                line=None,
                spec_clause="spec section 4 (out-of-scope)",
                finding="README.md was not updated to mention the new field.",
                evidence_cmd=None,
                evidence_output=None,
            ),
        ],
        summary=(
            "I verified schema.sql against the spec acceptance criteria. The NOT "
            "NULL constraint on user.email is missing (blocker) and login.py "
            "doesn't enforce it at the application layer (blocker). Also noticed "
            "the README wasn't updated, but per the spec, docs are out of scope "
            "for this PR."
        ),
        priority_order=[0, 1, 2],
        coverage_gaps=[
            "did not run the migration against a real Postgres instance — only inspected the SQL"
        ],
        dismissed_concerns=[
            "considered: schema.sql doesn't have a backfill clause for existing "
            "NULL emails, but the spec says this column is new (no existing rows "
            "to backfill)"
        ],
    )

    codex_output = ReviewerOutput(
        verdict="NO-SHIP",
        findings=[
            Finding(
                severity="blocker",
                file="src/db/schema.sql",
                line=14,
                spec_clause="acceptance criterion 2",
                finding=(
                    "Email field missing NOT NULL — schema.sql defines "
                    "`email TEXT,` at line 14 but the spec requires NOT NULL "
                    "on this column."
                ),
                evidence_cmd="cat src/db/schema.sql",
                evidence_output="CREATE TABLE users (\n  id INTEGER,\n  email TEXT,\n);",
            ),
            Finding(
                severity="minor",
                file="src/db/schema.sql",
                line=14,
                spec_clause="schema hygiene",
                finding=(
                    "The email column has no UNIQUE constraint either, which "
                    "may be implied by the auth flow (not strictly required by "
                    "the spec)."
                ),
                evidence_cmd=None,
                evidence_output=None,
            ),
        ],
        summary=(
            "Examined the schema migration. Email is nullable (blocker, "
            "matches claude's call); also flagged the missing UNIQUE constraint "
            "as a minor concern, though the spec doesn't strictly require it. "
            "Did not exercise the login path."
        ),
        priority_order=[0, 1],
        coverage_gaps=[
            "did not run the test suite",
            "did not exercise the login flow end-to-end",
        ],
        dismissed_concerns=[],
    )

    return [
        ReviewerRunResult(
            reviewer_name="claude-reviewer",
            provider="anthropic",
            output=claude_output,
            error=None,
            duration_seconds=12.4,
            raw_subprocess_result=None,
        ),
        ReviewerRunResult(
            reviewer_name="codex-reviewer",
            provider="openai",
            output=codex_output,
            error=None,
            duration_seconds=18.2,
            raw_subprocess_result=None,
        ),
    ]


@pytest.mark.smoke
def test_synthesizer_dedups_overlapping_findings_and_respects_unanimous_blocker_rule(
    tmp_path,
):
    """The core synthesizer smoke: hand-craft two reviewer outputs
    with a deliberate overlap (both flag the same blocker), run real
    codex against the production template, assert the output
    consolidates correctly.

    Asserts on structure + invariants ONLY, not specific finding
    content (the model decides how to phrase the consolidated
    descriptions). What the test pins:

    1. Parse succeeds — output is a real :class:`SynthesizerOutput`.
       Catches the "production template + real codex emits something
       the parser can't validate" failure mode.
    2. The overlap is deduplicated — there's at least one
       :class:`~syncade.synthesis.ConsolidatedFinding` whose
       ``provenance`` lists BOTH reviewers. Without dedup, the synth
       would re-emit both originals separately and the operator
       would see double.
    3. ``synthesis_summary`` is substantively non-empty
       (``len > 50``). The schema requires non-empty already (PR-7
       task 2's `_validate_synthesis_summary_nonblank`); the length
       check catches the "model emitted '.' as summary" compliance
       failure schema validation can't.
    4. The unanimous-blocker case is NOT dismissed. The schema
       validator would reject a dismissal of a two-reviewer blocker
       independently — if the model attempted it, `parse_synthesizer_output`
       would raise SynthesizerOutputError and the test would fail at
       the parse step. This assertion makes the prompt-compliance
       signal explicit: we want the model to NOT TRY in the first
       place, not just be saved by the schema.
    """
    _skip_if_no_codex()

    # Synthetic PR doc — the synthesizer reads this for context. The
    # README-out-of-scope clause sets up the plausible
    # dismissal-with-rationale finding (claude flagged the README,
    # spec exempts it; the synth MAY dismiss with rationale).
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text(
        "# Add NOT NULL constraint to user.email\n\n"
        "**Goal:** Make user.email a required field at the database\n"
        "schema layer and the application-layer auth flow.\n\n"
        "**Acceptance:**\n"
        "1. `src/db/schema.sql` declares user.email as `NOT NULL`.\n"
        "2. `src/auth/login.py` rejects requests with empty / null\n"
        "   emails before any DB write.\n\n"
        "**Out of scope:** README updates, documentation, migration\n"
        "tooling — those land in phase 02 of this initiative.\n"
    )

    reviewer_results = _hand_crafted_reviewer_results()

    # Run the real synthesizer. timeout is intentionally generous —
    # the synthesizer has to read the PR doc and two ReviewerOutput
    # blobs and emit consolidated JSON. 180s is the same headroom
    # the full-template reviewer smoke uses.
    synth_result = run_synthesizer(
        reviewer_results,
        repo_root=tmp_path,
        pr_doc_path=pr_doc,
        timeout_seconds=180.0,
        adapter=CodexAdapter(),
    )

    # First-line failure modes: subprocess didn't run, codex failed,
    # parse failed, schema rejected. Surface the error class + a
    # snippet of stdout for fast triage if any of these fire.
    if synth_result.output is None:
        raw_stdout = (
            synth_result.raw_subprocess_result.stdout[:500]
            if synth_result.raw_subprocess_result is not None
            else "(no subprocess output)"
        )
        err_cls = type(synth_result.error).__name__ if synth_result.error else "None"
        raise AssertionError(
            f"synthesizer failed: error_class={err_cls}, "
            f"error_message={synth_result.error}, "
            f"stdout_head={raw_stdout!r}"
        )

    output = synth_result.output
    assert isinstance(output, SynthesizerOutput)

    # Substantive summary check — schema requires non-empty but a
    # one-char summary would pass that. Length > 50 catches the
    # "model didn't actually summarize" compliance failure.
    assert len(output.synthesis_summary) >= 50, (
        f"synth summary too short ({len(output.synthesis_summary)} chars) — "
        f"the model likely didn't produce a real consolidation narrative: "
        f"{output.synthesis_summary!r}"
    )

    # Dedup invariant: at least one finding shows provenance from
    # both reviewers. Without dedup, the unanimous-blocker about
    # user.email's NOT NULL would split into two single-provenance
    # entries.
    multi_provenance = [f for f in output.consolidated_findings if len(f.provenance) >= 2]
    if not multi_provenance:
        finding_summaries = [
            (f.description[:50], [p.reviewer_name for p in f.provenance])
            for f in output.consolidated_findings
        ]
        raise AssertionError(
            "synthesizer did not dedup overlapping findings — every "
            "consolidated entry has single-reviewer provenance. The "
            "unanimous-blocker on user.email NOT NULL was meant to be "
            f"merged. Consolidated findings: {finding_summaries!r}"
        )

    # Both reviewers must appear by name in at least one of the
    # multi-provenance findings (catches a model that fabricates a
    # provenance entry with a wrong reviewer_name — which would
    # actually pass schema validation because the model name field
    # isn't cross-checked against the input reviewer names).
    names_seen: set[str] = set()
    for f in multi_provenance:
        for p in f.provenance:
            names_seen.add(p.reviewer_name)
    assert "claude-reviewer" in names_seen and "codex-reviewer" in names_seen, (
        f"multi-provenance findings reference {names_seen!r}; expected both "
        f"'claude-reviewer' and 'codex-reviewer'."
    )

    # Unanimous-blocker rule, prompt-compliance flavor: any
    # consolidated finding whose provenance is two reviewers AND both
    # at blocker must NOT be dismissed. The schema would reject this
    # combination outright at parse time; the assertion here is
    # belt-and-braces, verifying the model RESPECTED THE INSTRUCTION
    # rather than tried and got caught by the schema.
    for f in output.consolidated_findings:
        if len(f.provenance) >= 2 and all(p.original_severity == "blocker" for p in f.provenance):
            assert not f.dismissed, (
                f"synthesizer attempted to dismiss a unanimous blocker "
                f"(would have been schema-rejected too): description="
                f"{f.description!r}, dismissal_rationale="
                f"{f.dismissal_rationale!r}"
            )
