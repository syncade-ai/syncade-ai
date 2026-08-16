"""Prompt template loading and rendering.

Templates ship with the package under ``syncade/templates/`` and may
be overridden per-repo by dropping a same-named file under
``<repo_root>/.syncade/templates/``. The override mechanism exists so
a project with specialized verification needs (custom test harness,
unusual schema requirements, an explicit list of "do not skim these
subsystems") can tighten any prompt without forking syncade.

Rendering is intentionally minimal — :func:`str.format_map` with a
strict mapping. No Jinja, no conditionals, no loops. Adding a template
engine here is out of scope; if the prompt needs more structure,
write the override as a flat string with the placeholders the renderer
knows about.

The per-template loaders are thin wrappers
around :func:`load_template`, which takes the template basename. The
parameterized loader is the canonical entry point; the per-template
wrappers remain for clarity and back-compat with existing callers.
Each renderer keeps its own signature because the placeholder set
differs per template (reviewer needs ``diff``; synthesizer needs
``reviewer_outputs_json``); a unified renderer would either be untyped
or take an unwieldy union, which buys nothing.
"""

from __future__ import annotations

from pathlib import Path

# the template loader + the adversarial-lens block moved to
# prompts_loader; re-exported here so syncade.prompts.<name> is unchanged.
from syncade.prompts_loader import ADVERSARIAL_LENS_BLOCK, BUG_CLASS_BLOCK, load_template

DEFAULT_TEMPLATE_PATH = "templates/reviewer.md"
"""Package-relative path of the bundled reviewer template. Kept as a
module-level constant for back-compat with any external caller that
referenced it before the parameterized loader; new code should
prefer :func:`load_template` with the template basename."""


def load_reviewer_template(repo_root: Path) -> str:
    """Load the generic reviewer prompt template.

    Thin wrapper around :func:`load_template` with the reviewer
    basename. Kept for clarity at call sites and for back-compat with
    callers that imported it before the parameterized loader. This is
    the fallback template for any provider without a dedicated one (see
    :func:`load_reviewer_template_for_provider`).
    """
    return load_template(repo_root, "reviewer.md")


# Provider-specific reviewer templates. The two default reviewers get
# differentiated, hand-tuned adversarial prompts; any other provider
# (custom configs, the test fakes) falls back to the generic reviewer.md.
_REVIEWER_TEMPLATE_BY_PROVIDER = {
    "anthropic": "reviewer_adversarial.md",
    "openai": "reviewer_codex.md",
}


def reviewer_template_name_for_provider(provider: str) -> str:
    """Resolve the reviewer template basename for ``provider``.

    Returns the provider's dedicated template when one exists
    (``anthropic`` → ``reviewer_adversarial.md``, ``openai`` →
    ``reviewer_codex.md``), else the generic ``reviewer.md``.
    """
    return _REVIEWER_TEMPLATE_BY_PROVIDER.get(provider, "reviewer.md")


def load_reviewer_template_for_provider(repo_root: Path, provider: str) -> str:
    """Load the reviewer template for a specific provider.

    Resolution order:

    1. ``<repo_root>/.syncade/templates/<provider-template>`` — provider-specific
       per-repo override (e.g. ``reviewer_adversarial.md`` for ``anthropic``).
    2. ``<repo_root>/.syncade/templates/reviewer.md`` — generic per-repo override.
       A project that wants the same custom rules for all providers without
       creating provider-specific override files gets picked up here.
    3. Packaged provider-specific default (e.g. ``syncade/templates/reviewer_adversarial.md``).

    An unknown provider (any value other than ``"anthropic"`` and ``"openai"``)
    maps to ``reviewer.md`` via :func:`reviewer_template_name_for_provider`, so
    steps 1 and 3 both reference the generic template and step 2 is skipped.
    """
    provider_template = reviewer_template_name_for_provider(provider)
    provider_override = repo_root / ".syncade" / "templates" / provider_template
    if provider_override.is_file():
        return load_template(repo_root, provider_template)
    if provider_template != "reviewer.md":
        generic_override = repo_root / ".syncade" / "templates" / "reviewer.md"
        if generic_override.is_file():
            return load_template(repo_root, "reviewer.md")
    return load_template(repo_root, provider_template)


def load_reviewer_template_for(
    repo_root: Path, *, provider: str, template: str | None = None
) -> str:
    """Load a reviewer's prompt template, honoring a per-reviewer override.

    When ``template`` is set (a plain basename validated on
    :class:`~syncade.config.ReviewerConfig`), it overrides provider-based
    selection and is resolved via :func:`load_template` (per-repo
    ``.syncade/templates/<name>`` override → packaged default). When unset,
    falls back to :func:`load_reviewer_template_for_provider`.
    """
    if template:
        return load_template(repo_root, template)
    return load_reviewer_template_for_provider(repo_root, provider)


def load_synthesizer_template(repo_root: Path) -> str:
    """Load the synthesizer prompt template.

    Thin wrapper around :func:`load_template` with the synthesizer
    basename. Parallel to :func:`load_reviewer_template`; both
    resolve a per-repo ``.syncade/templates/<name>`` override before
    falling back to the packaged default.
    """
    return load_template(repo_root, "synthesizer.md")


def load_producer_template(repo_root: Path) -> str:
    """Load the producer prompt template.

    Thin wrapper around :func:`load_template` with the producer
    basename. Parallel to :func:`load_reviewer_template` and
    :func:`load_synthesizer_template`; the per-repo override path is
    ``<repo_root>/.syncade/templates/producer.md`` and the packaged default
    lives at ``syncade/templates/producer.md``.

    Path-traversal protection on the basename is shared via :func:`load_template`.
    """
    return load_template(repo_root, "producer.md")


_NO_PRIOR_ROUND_SENTINEL = "(no prior round — this is round 0)"
"""the literal substituted into the reviewer + producer
templates' ``{prior_round_output}`` placeholder on the first round
(``round_idx == 0``), when no prior-round artifact exists to replay.
A bare ``None`` would either KeyError under strict ``format_map`` or
silently render as the string ``"None"``; the sentinel is the
documented "no prior context" signal so the prompt still reads cleanly
on round 0 AND the reviewer / producer prose explicitly tells the
model what the sentinel means.

Renderers default this kwarg to the sentinel so callers without cross-round
context, plus explicit round-0 dispatch in the orchestrator, work unchanged."""


def render_reviewer_prompt(
    template: str,
    *,
    pr_doc_path: str,
    diff: str,
    master_plan_path: str | None = None,
    json_schema: str,
    prior_round_output: str = _NO_PRIOR_ROUND_SENTINEL,
    adversarial_lens: bool = False,
    bug_class_sweep: bool = False,
) -> str:
    """Substitute placeholders into the reviewer template.

    Known placeholders:

    - ``{pr_doc_path}``: path to the PR doc the reviewer should read
    - ``{diff}``: the actual diff text the reviewer is judging
    - ``{master_plan_path}``: optional path to the master plan; when
      ``None``, the placeholder is rendered as ``"(none)"`` so the
      prompt still reads cleanly
    - ``{json_schema}``: the JSON schema text the reviewer must emit
      its output against
    - ``{prior_round_output}``: the reviewer's OWN prior-round
      response text. ``round_idx == 0`` callers omit this kwarg and the
      renderer substitutes :data:`_NO_PRIOR_ROUND_SENTINEL`
      (``"(no prior round — this is round 0)"``); ``round_idx > 0``
      callers pass the extracted prior-round text. Cross-PR isolation:
      the orchestrator passes only the prior round's stdout within the
      SAME ``syncade <pr-doc>`` invocation; a new run starts with the
      sentinel again. Per-reviewer isolation: round-1 claude-reviewer
      sees claude-reviewer's round-0 output, NOT codex-reviewer's.
    - ``{adversarial_lens_block}``: the adversarial edge-enumeration
      block (:data:`ADVERSARIAL_LENS_BLOCK`) when ``adversarial_lens``
      is True, else the empty string. A reviewer not flagged renders this as
      ``""``.
    - ``{bug_class_block}``: the directed bug-class sweep
      (:data:`BUG_CLASS_BLOCK`) when ``bug_class_sweep`` is True, else the
      empty string. OPT-IN, like the adversarial lens — a reviewer that does
      not set it renders this as ``""``.

    The template is rendered with :meth:`str.format_map` against a
    strict mapping — any placeholder in the template that isn't one of
    the seven above raises :class:`KeyError`, so a typo in a custom
    override surfaces loudly instead of silently producing an
    unsubstituted prompt.
    """
    mapping = {
        "pr_doc_path": pr_doc_path,
        "diff": diff,
        "master_plan_path": master_plan_path if master_plan_path else "(none)",
        "json_schema": json_schema,
        "prior_round_output": prior_round_output,
        "adversarial_lens_block": ADVERSARIAL_LENS_BLOCK if adversarial_lens else "",
        "bug_class_block": BUG_CLASS_BLOCK if bug_class_sweep else "",
    }
    return template.format_map(mapping)


def render_synthesizer_prompt(
    template: str,
    *,
    pr_doc_path: str,
    reviewer_outputs_json: str,
    master_plan_path: str | None = None,
    json_schema: str,
) -> str:
    """Substitute placeholders into the synthesizer template.

    Known placeholders:

    - ``{pr_doc_path}``: path to the PR doc — the synthesizer reads
      it for context on what the producer was meant to ship (its only
      window into the spec, since it does NOT see the diff)
    - ``{reviewer_outputs_json}``: a single string containing the two
      reviewers' structured outputs serialized as JSON (typically
      formatted as labeled blocks so the model can attribute findings
      back to each reviewer by name)
    - ``{master_plan_path}``: optional path to the master plan; same
      ``None`` → ``"(none)"`` convention as :func:`render_reviewer_prompt`
    - ``{json_schema}``: the :class:`~syncade.synthesis.SynthesizerOutput` schema string
      pulled from :func:`~syncade.synthesis.get_synthesizer_schema_string`

    Strict :meth:`str.format_map` — any placeholder the template uses
    that isn't one of the four above raises :class:`KeyError`. Same
    surface-typos-loudly contract as the reviewer renderer.

    The synthesizer is NOT given a ``{diff}`` placeholder. That's a
    deliberate architecture choice: the synthesizer is the cold consolidator. It
    sees what the
    reviewers surfaced about the diff, not the diff itself. If a
    future override template tries to reference ``{diff}``,
    :meth:`str.format_map` will raise :class:`KeyError`, which is the right
    outcome.
    """
    mapping = {
        "pr_doc_path": pr_doc_path,
        "reviewer_outputs_json": reviewer_outputs_json,
        "master_plan_path": master_plan_path if master_plan_path else "(none)",
        "json_schema": json_schema,
    }
    return template.format_map(mapping)


_NO_TEST_FAILURE_SENTINEL = "(no test failure this round)"
"""Literal substituted into the producer template's
``{test_run_stdout_path}`` placeholder when the test leg either
didn't run this round or passed (i.e. there's no failure trace
to show). The renderer handles ``None`` directly so callers don't
have to pre-convert."""


_NO_PRIOR_COMMITS_SENTINEL = "(no prior commits)"
"""the literal substituted into the producer template's
``{prior_round_commits}`` placeholder on the first round
(``round_idx == 0``), when no prior-round artifact exists to replay.
Parallel to :data:`_NO_PRIOR_ROUND_SENTINEL` but specific to the
commit-subjects section of the producer's prior-round context. The
two sentinels exist as distinct strings so the producer prose can
explicitly name each — the round-0 producer sees both "(no prior
round)" and "(no prior commits)" rather than a single ambiguous
"(none)"."""


_NO_OPERATOR_DECISION_SENTINEL = "(no operator decision — this is not a resumed escalation round)"
"""the literal substituted into the producer template's
``{operator_decision}`` placeholder on every NON-resumed round. Only a
round resumed after a producer escalation (``syncade --resume`` reading
``decision.txt``) substitutes the operator's recorded decision; every
other producer run sees this sentinel. Parallel to the prior-round
sentinels."""


def load_spec_audit_template(repo_root: Path) -> str:
    """Load the spec audit prompt template.

    Thin wrapper around :func:`load_template` with the spec_audit
    basename. Per-repo override path is
    ``<repo_root>/.syncade/templates/spec_audit.md``; packaged default
    lives at ``syncade/templates/spec_audit.md``.
    """
    return load_template(repo_root, "spec_audit.md")


def render_spec_audit_prompt(
    template: str,
    *,
    pr_doc_path: str,
    json_schema: str,
) -> str:
    """Substitute placeholders into the spec audit template.

    Known placeholders:

    - ``{pr_doc_path}``: path to the PR brief to audit — the sole input
      to the cold auditor subprocess
    - ``{json_schema}``: the :class:`~syncade.spec_audit.SpecAuditOutput`
      schema string pulled from :func:`~syncade.spec_audit.get_spec_audit_schema_string`

    Strict :meth:`str.format_map` — any placeholder the template uses
    that isn't one of the two above raises :class:`KeyError`. The
    auditor intentionally receives no diff, no reviewer outputs, and no
    test results — only the brief. If a custom override template tries
    to reference ``{diff}``, :meth:`str.format_map` raises immediately.
    """
    mapping = {
        "pr_doc_path": pr_doc_path,
        "json_schema": json_schema,
    }
    return template.format_map(mapping)


def load_spec_draft_template(repo_root: Path) -> str:
    """Load the spec draft prompt template.

    Thin wrapper around :func:`load_template` with the spec_draft basename.
    Per-repo override path is ``<repo_root>/.syncade/templates/spec_draft.md``;
    packaged default lives at ``syncade/templates/spec_draft.md``.
    """
    return load_template(repo_root, "spec_draft.md")


def render_spec_draft_prompt(
    template: str,
    *,
    dialogue_path: str,
    diff_path: str,
    json_schema: str,
) -> str:
    """Substitute placeholders into the spec draft template.

    Known placeholders:

    - ``{dialogue_path}``: path to the parsed session dialogue the cold drafter
      reads for *intent*
    - ``{diff_path}``: path to the diff of what was built (may be the empty/sentinel
      file for a dialogue-only draft)
    - ``{json_schema}``: the :class:`~syncade.spec_draft.SpecDraftOutput` schema
      string from :func:`~syncade.spec_draft.get_spec_draft_schema_string`

    Strict :meth:`str.format_map` — any placeholder the template uses that isn't one
    of the three above raises :class:`KeyError`.
    """
    mapping = {
        "dialogue_path": dialogue_path,
        "diff_path": diff_path,
        "json_schema": json_schema,
    }
    return template.format_map(mapping)


def render_producer_prompt(
    template: str,
    *,
    pr_doc_path: str,
    findings_md_path: str,
    test_run_stdout_path: str | None,
    worktree_path: str,
    round_number: int,
    max_rounds: int,
    prior_round_output: str = _NO_PRIOR_ROUND_SENTINEL,
    prior_round_commits: str = _NO_PRIOR_COMMITS_SENTINEL,
    operator_decision: str = _NO_OPERATOR_DECISION_SENTINEL,
) -> str:
    """Substitute placeholders into the producer template.

    Known placeholders:

    - ``{pr_doc_path}``: path to the PR spec — the contract the
      producer is implementing
    - ``{findings_md_path}``: path to the just-completed round's
      ``findings.md`` — the consolidated review output (with
      provenance + per-reviewer summaries) the producer reads to
      know what to fix
    - ``{test_run_stdout_path}``: path to the just-completed
      round's ``test-run.stdout`` when the test leg ran and
      failed; the literal string
      ``"(no test failure this round)"`` otherwise. The renderer
      accepts ``None`` directly and substitutes the sentinel itself.
    - ``{worktree_path}``: the producer worktree on disk — the
      starting point for ``git log`` / ``git diff``, and where
      file edits should land
    - ``{round_number}``: 0-indexed round this producer is for
      (so the first producer-after-round-0 receives 0)
    - ``{max_rounds}``: configured ``[loop] max_rounds`` so the
      producer can budget effort ("you're in round 1 of 3")
    - ``{prior_round_output}``: the producer's OWN prior-round
      response text. ``round_idx == 0`` callers omit this kwarg and the
      renderer substitutes :data:`_NO_PRIOR_ROUND_SENTINEL`; ``round_idx > 0``
      callers (the orchestrator's producer-phase wiring) pass the
      extracted prior-round text. Cross-PR isolation: the orchestrator
      passes only the prior round's stdout within the SAME
      ``syncade <pr-doc>`` invocation; a new run starts with the
      sentinel again.
    - ``{prior_round_commits}``: the commit subjects of
      round-(N-1)'s producer commits, derived via ``git log -1
      --format='%s' <sha>`` in the operator's repo (NOT the producer
      worktree — branch advance has promoted the prior commits onto the
      operator's branch by the time round-N producer runs). ``round_idx
      == 0`` callers omit this kwarg and the renderer substitutes
      :data:`_NO_PRIOR_COMMITS_SENTINEL`.

    The template is rendered with :meth:`str.format_map` against a
    strict mapping — any placeholder in the template that isn't one
    of the eight above raises :class:`KeyError`. Surface-typos-loudly
    contract, same as the reviewer and synthesizer renderers.

    The producer is intentionally NOT given a ``{diff}`` placeholder
    even though it sees the diff via the worktree. The diff
    materializes via the producer running ``git log`` / ``git diff``
    in the worktree — explicit tool calls, not a prompt-embedded
    blob. This keeps the prompt size bounded (large diffs would
    blow the prompt budget) and lets the producer focus its
    attention on the consolidated findings rather than re-reading
    the diff blob.
    """
    # tolerate None for test_run_stdout_path. The brief's
    # acceptance: "``{test_run_stdout_path}`` substitution works
    # with None → renders the literal '(no test failure this round)'
    # string". Moved from caller-side (orchestrator) to renderer-
    # side so the public API contract handles None directly.
    resolved_test_path = (
        test_run_stdout_path if test_run_stdout_path is not None else _NO_TEST_FAILURE_SENTINEL
    )
    mapping = {
        "pr_doc_path": pr_doc_path,
        "findings_md_path": findings_md_path,
        "test_run_stdout_path": resolved_test_path,
        "worktree_path": worktree_path,
        "round_number": round_number,
        "max_rounds": max_rounds,
        "prior_round_output": prior_round_output,
        "prior_round_commits": prior_round_commits,
        "operator_decision": operator_decision,
    }
    return template.format_map(mapping)
