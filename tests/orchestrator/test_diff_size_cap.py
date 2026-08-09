"""`[loop] max_diff_bytes` — PR-h-02e D2, owed since 2026-08-05, landed in PR-h-field-01.

D2 chose (a) REFUSE at the cap, exit 60, naming the size — consistent with `diff_malformed`:
if we cannot show reviewers the change, we decline to render a verdict on it. Not (b)
truncate — a verdict on a deliberately partial diff is a verdict on the wrong code, the
failure PR-h-02 exists to prevent. Not (c) warn and proceed — today's behaviour with extra
text.

Without the cap the ceiling is still enforced, just by the provider and only after the
operator has paid: `codex exec` refuses input over 1,048,576 characters with
`input_too_large`, and before PR-h-field-01 item 1 the same input produced a bare
`[Errno 7] Argument list too long` naming no cause at all.

The runs here go through `run_review` with fake adapters rather than calling the round
directly, so they exercise the real wiring — a cap the orchestrator never consults would
pass a unit test of the comparison.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import syncade.orchestrator.round as round_module
from syncade.adapters.fake import FakeAdapter
from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import _factory_returning, _ship, _two_reviewer_config
from tests.orchestrator.test_empty_diff import _init_repo


def _run(tmp_path, *, cap, extra_file=None, monkeypatch):
    """Run a real loop against a repo whose diff we control. Returns (result, dispatched)."""
    repo, pr_doc, head = _init_repo(tmp_path)

    provisioned: list[str] = []
    real_manager = round_module.WorktreeManager

    class CountingManager(real_manager):
        def __init__(self, *args, **kwargs):
            provisioned.append(kwargs.get("run_id", "?"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(round_module, "WorktreeManager", CountingManager)

    if extra_file is not None:
        name, content = extra_file
        (repo / name).write_text(content)
        import subprocess

        subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-qm", "bulk"], cwd=repo, capture_output=True, check=True)

    dispatched: list[str] = []

    class Tracking(FakeAdapter):
        def build_invocation(self, reviewer_config, worktree_path, prompt):
            dispatched.append(reviewer_config.name)
            return super().build_invocation(reviewer_config, worktree_path, prompt)

    config = _two_reviewer_config()
    config = config.model_copy(
        update={"loop": config.loop.model_copy(update={"max_diff_bytes": cap})}
    )

    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=config,
        logger=Logger("quiet"),
        adapter_factory=_factory_returning(*[Tracking(canned_output=_ship()) for _ in range(2)]),
        base_ref=head,
        worktree_base=tmp_path / "wt",
    )
    return result, dispatched, provisioned


# ── The default, calibrated in both directions ──────────────────────────────


def test_the_default_is_under_the_measured_provider_ceiling():
    """codex enforces 1,048,576 CHARACTERS. The cap counts UTF-8 BYTES, and bytes >=
    characters in every encoding, so a byte cap at or below that bounds the character
    count too — syncade's actionable message always fires before the provider's opaque one."""
    assert SyncadeConfig().loop.max_diff_bytes <= 1_048_576


def test_the_default_does_not_refuse_diffs_that_work_today():
    """Calibrated against this repo's own corpus: 30 measured rounds, max 147,171 B.

    A new blocking refusal on a published tool is a breaking change, so the default must
    clear observed reality widely. Pinned so a future tightening is a decision, not a drift.
    """
    assert SyncadeConfig().loop.max_diff_bytes >= 4 * 147_171


@pytest.mark.parametrize("bad", ["1000000", 0, -1, 1.5])
def test_a_bad_value_is_refused_at_config_load(bad):
    """Repo convention: numeric knobs validate STRICTLY, never silently coerce."""
    with pytest.raises(ValidationError):
        SyncadeConfig(
            reviewers=[{"name": "rv1", "provider": "fake1", "model": "x"}],
            loop={"max_diff_bytes": bad},
        )


# ── The refusal ─────────────────────────────────────────────────────────────


def test_an_oversize_diff_refuses_before_anything_is_provisioned(tmp_path, monkeypatch):
    """The property that makes the refusal worth having: it costs nothing.

    Asserted from both ends — no reviewer dispatched AND no worktree provisioned. A counter
    alone can be satisfied by a stub; a directory that never appears cannot.
    """
    result, dispatched, provisioned = _run(
        tmp_path, cap=2_000, extra_file=("bulk.py", "x = 1\n" * 5_000), monkeypatch=monkeypatch
    )

    assert result.exit_code == 60
    assert result.termination_reason == "diff_too_large"
    assert dispatched == [], "a reviewer ran for a diff syncade had already refused"
    assert provisioned == [], "a worktree was provisioned for a refused run"


def test_the_refusal_names_the_size_the_ceiling_and_what_to_do(tmp_path, monkeypatch):
    """`diff-refused.txt` is the durable artifact — the log line scrolls away."""
    result, _, _ = _run(
        tmp_path, cap=2_000, extra_file=("bulk.py", "x = 1\n" * 5_000), monkeypatch=monkeypatch
    )

    refused = (result.artifacts.round_dir / "diff-refused.txt").read_text()
    assert "diff_too_large" in refused
    assert "2,000" in refused, "the ceiling is not named"
    assert "--base" in refused and "max_diff_bytes" in refused, "no actionable next step"
    # The measured size must be real, not a placeholder.
    assert result.rounds[-1].oversize_diff_bytes > 2_000


def test_a_diff_under_the_cap_runs_normally(tmp_path, monkeypatch):
    """The control. Without it, the assertions above could pass because the harness
    refuses everything."""
    result, dispatched, provisioned = _run(
        tmp_path, cap=1_000_000, extra_file=("small.py", "x = 1\n"), monkeypatch=monkeypatch
    )

    assert result.termination_reason == "ship"
    assert len(dispatched) == 2
    assert provisioned, "harness cannot observe provisioning; the refusal tests are vacuous"


def test_the_cap_measures_the_REVIEWER_FACING_diff_not_the_raw_one(tmp_path, monkeypatch):
    """A diff huge only because of binary content must NOT be refused.

    Binary elision runs first, so the cap sees what a reviewer receives. Capping the raw
    diff would refuse the exact repo shape PR-h-field-01 exists to make reviewable: committed
    screenshot baselines whose real source change is a few hundred bytes.
    """
    blob = "\x00BINARY" * 20_000  # ~140 KB of binary that elides to one notice line
    result, dispatched, _ = _run(
        tmp_path, cap=20_000, extra_file=("shot.png", blob), monkeypatch=monkeypatch
    )

    assert result.termination_reason != "diff_too_large", (
        "the cap thresholded the RAW diff — a repo with committed binaries is refused for "
        "bytes no reviewer would ever have been sent"
    )
    assert len(dispatched) == 2


def test_oversize_diff_does_not_trigger_commit_safety_guards(tmp_path, monkeypatch):
    """Regression: _diff_will_dispatch() must return False for diff_too_large.

    Before the fix, _diff_will_dispatch() only returned False for diff_malformed and
    no_changes_to_review. An oversize diff was treated as a dispatching run, so the
    dirty-worktree and default-branch guards fired before the cap refusal — printing
    misleading "producer commits will land" or raising WorktreeError on a run that would
    never dispatch any reviewer.
    """
    from syncade.orchestrator.loop import _diff_will_dispatch
    from syncade.snapshot import Snapshot

    big_diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n" + (
        "+x = 1\n" * 50_000  # ~350 KB
    )
    snapshot = Snapshot(
        repo_root=tmp_path,
        commit_sha="a" * 40,
        branch="feature",
        base_ref="HEAD",
        base_oid="b" * 40,
        diff_text=big_diff,
        dirty_state="clean",
    )
    config = SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_diff_bytes": 1_000},  # far below the 350 KB diff
    )
    # An oversize diff must not be classified as a dispatching run.
    assert _diff_will_dispatch(snapshot, config) is False


def test_oversize_diff_artifacts_record_diff_bytes_in_manifest(tmp_path, monkeypatch):
    """Regression: diff_too_large refusal must populate diff_bytes_reviewed in the manifest.

    Before the fix, _persist_refusal() called persist_round_manifest() without passing
    filtered_diff_bytes/raw_diff_bytes, so the refusing round's manifest recorded both
    as null — machine consumers couldn't distinguish cap details without parsing text.
    """
    import json

    result, _, _ = _run(
        tmp_path, cap=2_000, extra_file=("bulk.py", "x = 1\n" * 5_000), monkeypatch=monkeypatch
    )
    assert result.termination_reason == "diff_too_large"

    manifest = json.loads((result.artifacts.round_dir / "manifest.json").read_text())
    snap = manifest["snapshot"]
    assert snap["diff_bytes_reviewed"] is not None, (
        "diff_too_large manifest has null diff_bytes_reviewed — "
        "machine consumers cannot verify the cap was applied"
    )
    assert snap["diff_bytes_reviewed"] > 2_000


def test_oversize_diff_summary_shows_diff_too_large_label(tmp_path, monkeypatch):
    """Regression: diff_too_large refusal must render DIFF_TOO_LARGE in summary.md.

    Before the fix, persist_run_summary() fell through to _EXIT_CODE_LABELS[60] =
    'WORKTREE_ERROR', so the per-round summary told operators to debug stale worktree
    provisioning instead of naming the actual cause.
    """
    result, _, _ = _run(
        tmp_path, cap=2_000, extra_file=("bulk.py", "x = 1\n" * 5_000), monkeypatch=monkeypatch
    )
    assert result.termination_reason == "diff_too_large"

    summary = (result.artifacts.round_dir / "summary.md").read_text()
    assert "DIFF_TOO_LARGE" in summary, (
        "summary.md labels a diff_too_large refusal as WORKTREE_ERROR — "
        "operators are told to debug stale worktrees instead of the actual cause"
    )


def test_assembled_prompt_too_large_refuses_before_dispatch(tmp_path, monkeypatch):
    """Regression: a diff under max_diff_bytes that assembles into a prompt over the
    provider ceiling must refuse with prompt_too_large before dispatching any reviewer.

    The diff-size cap gates on reviewer-facing diff bytes, but the assembled prompt
    also includes the reviewer template and prior-round context. A large template can
    push the prompt over the codex 1,048,576-character ceiling while the diff alone
    stays under max_diff_bytes — in that case the operator received the provider's
    opaque `input_too_large` error after paying for the round instead of syncade's
    actionable message.
    """
    from syncade.orchestrator.round_no_changes import _CODEX_CHAR_CEILING

    # Patch _build_reviewer_prompt to return a prompt that exceeds the ceiling.
    # This exercises the post-assembly check without needing a genuinely huge diff.
    over_ceiling = "x" * (_CODEX_CHAR_CEILING + 1)
    monkeypatch.setattr(
        round_module,
        "_build_reviewer_prompt",
        lambda *a, **kw: ({"rv1": over_ceiling, "rv2": over_ceiling}, None, False),
    )

    result, dispatched, provisioned = _run(
        tmp_path, cap=10_000_000, extra_file=("small.py", "x = 1\n"), monkeypatch=monkeypatch
    )

    assert result.termination_reason == "prompt_too_large", (
        f"expected prompt_too_large, got {result.termination_reason!r} — "
        "an oversized assembled prompt did not refuse before dispatch"
    )
    assert result.exit_code == 60
    assert dispatched == [], "a reviewer ran despite the assembled prompt exceeding the ceiling"
    assert provisioned == [], "a worktree was provisioned for a refused run"

    # The durable artifact must name the cause clearly.
    refused = (result.artifacts.round_dir / "diff-refused.txt").read_text()
    assert "prompt_too_large" in refused
    assert str(_CODEX_CHAR_CEILING) in refused or f"{_CODEX_CHAR_CEILING:,}" in refused


def _run_with_oversized_prompt(tmp_path, monkeypatch):
    """Helper: a small-diff run where the assembled prompt exceeds the ceiling."""
    import json

    from syncade.orchestrator.round_no_changes import _CODEX_CHAR_CEILING

    over_ceiling = "x" * (_CODEX_CHAR_CEILING + 1)
    monkeypatch.setattr(
        round_module,
        "_build_reviewer_prompt",
        lambda *a, **kw: ({"rv1": over_ceiling, "rv2": over_ceiling}, None, False),
    )
    result, _, _ = _run(
        tmp_path, cap=10_000_000, extra_file=("small.py", "x = 1\n"), monkeypatch=monkeypatch
    )
    assert result.termination_reason == "prompt_too_large"
    manifest = json.loads((result.artifacts.round_dir / "manifest.json").read_text())
    return result, manifest


def test_prompt_too_large_summary_shows_prompt_too_large_label(tmp_path, monkeypatch):
    """Regression: prompt_too_large refusal must render PROMPT_TOO_LARGE in summary.md.

    Before the fix, oversize_prompt_chars was never passed to persist_run_summary, so the
    exit label fell through to _EXIT_CODE_LABELS[60] = 'WORKTREE_ERROR' — operators were
    told to debug stale worktree provisioning instead of the actual cause.
    """
    result, _ = _run_with_oversized_prompt(tmp_path, monkeypatch)
    summary = (result.artifacts.round_dir / "summary.md").read_text()
    assert "PROMPT_TOO_LARGE" in summary, (
        "summary.md labels a prompt_too_large refusal as WORKTREE_ERROR — "
        "operators are told to debug stale worktrees instead of the actual cause"
    )


def test_prompt_too_large_summary_synthesizer_section_is_not_applicable(tmp_path, monkeypatch):
    """Regression: prompt_too_large refusal must say 'not applicable' in the Synthesizer
    section, not 'skipped (a reviewer failed)'.

    Before the fix, oversize_prompt_chars was not threaded through persist_run_summary, so
    the synthesizer and test sections fell through to the failure-inference path and told the
    operator that a reviewer subprocess had failed — misleading on a run that never dispatched.
    """
    result, _ = _run_with_oversized_prompt(tmp_path, monkeypatch)
    summary = (result.artifacts.round_dir / "summary.md").read_text()
    assert "skipped (a reviewer failed" not in summary, (
        "synthesizer section says 'a reviewer failed' on a run that never dispatched any reviewer"
    )
    assert "not applicable" in summary


def test_prompt_too_large_manifest_has_refusal_reason_and_prompt_chars(tmp_path, monkeypatch):
    """Regression: prompt_too_large refusal must record refusal_reason and oversize_prompt_chars
    in manifest.json so the resume planner and tooling can classify the round correctly.
    """
    _, manifest = _run_with_oversized_prompt(tmp_path, monkeypatch)
    assert manifest.get("refusal_reason") == "prompt_too_large", (
        f"manifest missing refusal_reason='prompt_too_large', "
        f"got {manifest.get('refusal_reason')!r}"
    )
    assert manifest.get("oversize_prompt_chars") is not None, (
        "manifest missing oversize_prompt_chars — resume planner cannot classify the round"
    )


def test_diff_too_large_manifest_has_refusal_reason(tmp_path, monkeypatch):
    """Regression: diff_too_large refusal must record refusal_reason='diff_too_large' in
    manifest.json so the resume planner can classify the round as drop-and-retry.
    """
    import json

    result, _, _ = _run(
        tmp_path, cap=2_000, extra_file=("bulk.py", "x = 1\n" * 5_000), monkeypatch=monkeypatch
    )
    assert result.termination_reason == "diff_too_large"
    manifest = json.loads((result.artifacts.round_dir / "manifest.json").read_text())
    assert manifest.get("refusal_reason") == "diff_too_large", (
        f"manifest missing refusal_reason='diff_too_large', got {manifest.get('refusal_reason')!r}"
    )


def test_diff_will_dispatch_uses_rendered_prompt_not_template_plus_diff(tmp_path, monkeypatch):
    """Regression: _diff_will_dispatch must render the full prompt to check the ceiling.

    Before the fix, _diff_will_dispatch used len(template) + len(filtered_diff) — a lower
    bound that omitted the JSON schema (~517 chars) and adversarial-lens block (~3,316 chars).
    A template sized to fit with a small diff but push the rendered prompt over the ceiling
    would cause _diff_will_dispatch to return True (dispatching), then commit-safety guards
    fired (dirty-tree or default-branch refusal) instead of the intended prompt_too_large.
    """
    from syncade.findings import get_findings_schema_string
    from syncade.orchestrator.loop import _diff_will_dispatch
    from syncade.orchestrator.round_no_changes import _CODEX_CHAR_CEILING
    from syncade.prompts import render_reviewer_prompt
    from syncade.prompts_loader import ADVERSARIAL_LENS_BLOCK
    from syncade.snapshot import Snapshot

    # Build a template that is under the ceiling when combined with a small diff, but
    # exceeds it once the adversarial-lens block and JSON schema are substituted in.
    # The template uses all placeholders so the render is faithful.
    small_diff = "diff --git a/x.py b/x.py\n+x = 1\n"
    fixed_overhead = len(ADVERSARIAL_LENS_BLOCK) + len(get_findings_schema_string())
    template = (
        "{pr_doc_path}\n{diff}\n{json_schema}\n{prior_round_output}\n{adversarial_lens_block}\n"
        + "x" * (_CODEX_CHAR_CEILING - len(small_diff) - fixed_overhead // 2 + 100)
    )
    rendered = render_reviewer_prompt(
        template,
        pr_doc_path="<pr-doc>",
        diff=small_diff,
        master_plan_path=None,
        json_schema=get_findings_schema_string(),
        adversarial_lens=True,
    )
    # Preconditions: the test only distinguishes the old and new behaviours when these hold.
    assert len(template) + len(small_diff) < _CODEX_CHAR_CEILING, (
        "precondition: template+diff must be under ceiling"
    )
    assert len(rendered) > _CODEX_CHAR_CEILING, "precondition: rendered prompt must exceed ceiling"

    monkeypatch.setattr("syncade.prompts.load_reviewer_template_for", lambda *a, **kw: template)

    snapshot = Snapshot(
        repo_root=tmp_path,
        commit_sha="a" * 40,
        branch="feature",
        base_ref="HEAD",
        base_oid="b" * 40,
        diff_text=small_diff,
        dirty_state="clean",
    )
    config = SyncadeConfig(
        reviewers=[{"name": "rv1", "provider": "fake1", "model": "x", "adversarial_lens": True}],
        review={"strip_repo_context_files": []},
    )
    assert _diff_will_dispatch(snapshot, config, repo_root=tmp_path) is False, (
        "prompt-size preflight returned True even though the rendered prompt exceeds the ceiling"
    )


def test_diff_will_dispatch_uses_real_pr_doc_path_not_placeholder(tmp_path, monkeypatch):
    """Regression: _diff_will_dispatch must use the real pr_doc_path, not "<pr-doc>".

    A template that repeats {pr_doc_path} can be under the ceiling with the placeholder
    "<pr-doc>" (8 chars) but over it with the real path (much longer). Before the fix,
    _diff_will_dispatch always rendered with pr_doc_path="<pr-doc>", so it false-greened
    for a run that would later refuse with prompt_too_large — and in loop mode the
    default-branch or dirty-tree guard would then fire instead of the intended refusal.
    """
    from syncade.findings import get_findings_schema_string
    from syncade.orchestrator.loop import _diff_will_dispatch
    from syncade.orchestrator.round_no_changes import _CODEX_CHAR_CEILING
    from syncade.prompts import render_reviewer_prompt
    from syncade.snapshot import Snapshot

    # Template repeats {pr_doc_path} enough times that:
    # - with placeholder "<pr-doc>" (8 chars): total stays under ceiling
    # - with real path (long): total exceeds ceiling
    repeat_count = 10_000
    template = (
        "{pr_doc_path}\n" * repeat_count
        + "{diff}\n{json_schema}\n{prior_round_output}\n{adversarial_lens_block}\n"
    )

    # A long-ish real path so the difference from placeholder is large
    real_path = tmp_path / ("your repo" + "a" * 100 + ".md")
    real_path.parent.mkdir(parents=True, exist_ok=True)
    real_path.write_text("PR doc content")
    real_ref = str(real_path.relative_to(tmp_path))  # the worktree-local reference

    small_diff = "diff --git a/x.py b/x.py\n+x = 1\n"
    schema = get_findings_schema_string()

    rendered_placeholder = render_reviewer_prompt(
        template,
        pr_doc_path="<pr-doc>",
        diff=small_diff,
        master_plan_path=None,
        json_schema=schema,
        adversarial_lens=False,
    )
    rendered_real = render_reviewer_prompt(
        template,
        pr_doc_path=real_ref,
        diff=small_diff,
        master_plan_path=None,
        json_schema=schema,
        adversarial_lens=False,
    )
    assert len(rendered_placeholder) < _CODEX_CHAR_CEILING, (
        "precondition: rendered prompt with placeholder must be under ceiling"
    )
    assert len(rendered_real) > _CODEX_CHAR_CEILING, (
        "precondition: rendered prompt with real path must exceed ceiling"
    )

    monkeypatch.setattr("syncade.prompts.load_reviewer_template_for", lambda *a, **kw: template)

    snapshot = Snapshot(
        repo_root=tmp_path,
        commit_sha="a" * 40,
        branch="feature",
        base_ref="HEAD",
        base_oid="b" * 40,
        diff_text=small_diff,
        dirty_state="clean",
    )
    config = SyncadeConfig(
        reviewers=[{"name": "rv1", "provider": "fake1", "model": "x", "adversarial_lens": False}],
        review={"strip_repo_context_files": []},
    )

    # Old behavior (no pr_doc_path): placeholder is used → false-green
    assert _diff_will_dispatch(snapshot, config, repo_root=tmp_path) is True, (
        "without pr_doc_path the check uses placeholder and must be under ceiling"
    )
    # New behavior (with pr_doc_path): real path is used → correctly refuses
    assert (
        _diff_will_dispatch(snapshot, config, repo_root=tmp_path, pr_doc_path=real_path) is False
    ), "with real pr_doc_path the preflight must detect the oversized prompt"
