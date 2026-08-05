# SIZE_OK: 357 pure LOC; spec-audit keeps one cold diagnostic contract.
# Retained to avoid splitting tightly coupled prompt/workspace/result handling.
# Future split: extract prompt/workspace orchestration when that behavior changes.
"""Spec audit subprocess phase.

Pre-flight diagnostic that audits the PR brief itself BEFORE the review
loop dispatches reviewers. Surfaces: unverified claims about external
behavior, internal contradictions, ambiguous acceptance criteria, missing
references, scope drift, and missing structural sections. The auditor is
a single cold subprocess, provider resolved from the ``[auditor]`` config
block (PR-v2-23). Inputs: just the PR brief. No diff, no reviewer outputs,
no worktree context.

Advisory for v1 — the loop does not refuse to run when the spec audit
finds blockers; the operator decides whether to edit the brief or proceed.

Exit codes via ``syncade --spec-audit`` are split between the auditor's
own outcomes (this module) and the CLI mode handler's pre-flight
(``syncade.cli._run_spec_audit``):

- 0 (ready) — auditor outcome
- 10 (needs-clarification) — auditor outcome
- 40 (subprocess error) — auditor outcome
- 70 (parse failure) — auditor outcome
- 50 (config load failure) — CLI mode handler; emitted before
  ``run_spec_audit`` is called
- 60 (path validation failure) — CLI mode handler; PR_DOC doesn't
  exist or isn't readable

The 50/60 split follows the CLI mode handler convention shared with
``_run_selfcheck`` and ``_run_auth_check`` — see CLAUDE.md's
"Exit-code convention for CLI mode handlers" subsection.

Structural invariants this module enforces:

- *Process isolation.* The auditor is a fresh subprocess, same as
  reviewers and producer. No shared context with the operator's interactive
  session.
- *Cold inputs.* The auditor receives only the PR brief text. No diff, no
  reviewer outputs, no test results.
- *Cannot invent.* The ``SpecAuditOutput`` schema requires findings to
  cite a ``section`` and ``line`` (per the "cannot-invent" spirit of the
  synthesizer). The schema uses ``extra="forbid"`` to reject aliases.
- *Configurable provider.* Resolved from ``[auditor]`` in ``.syncade/config.toml``
  (PR-v2-23); defaults to ``openai``/``gpt-5.5``, ``thinking=xhigh``,
  ``permissions=trusted-execute``.

Workspace setup mirrors :mod:`syncade.synthesizer`: isolated tempdir,
git-init, copy of the PR doc, env scrub of repo_root-leaking variables.
Reuses :func:`~syncade.synthesizer._init_workspace_git` and
:func:`~syncade.synthesizer._scrub_env_for_cold_synth` directly — same
logic, no duplication.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from syncade.adapters.base import ReviewerAdapter, ReviewerInvocationError
from syncade.adapters.registry import get_adapter
from syncade.config import ReviewerConfig
from syncade.config_cold import (
    AUDITOR_MODEL as AUDITOR_MODEL,
)
from syncade.config_cold import (
    AUDITOR_PERMISSIONS as AUDITOR_PERMISSIONS,
)
from syncade.config_cold import (
    AUDITOR_PROVIDER as AUDITOR_PROVIDER,
)
from syncade.config_cold import (
    AUDITOR_THINKING as AUDITOR_THINKING,
)
from syncade.config_cold import (
    AuditorConfig,
)
from syncade.findings import Severity as Severity
from syncade.process import (
    SubprocessError,
    SubprocessNotFoundError,
    SubprocessResult,
    SubprocessTimeoutError,
    run_subprocess,
)
from syncade.prompts import load_spec_audit_template, render_spec_audit_prompt

# the pydantic schema + parser moved to spec_audit_schema; re-exported
# here so syncade.spec_audit.<name> import paths are unchanged.
from syncade.spec_audit_schema import (
    AuditVerdict,
    IssueClass,
    SpecAuditFinding,
    SpecAuditOutput,
    SpecAuditOutputError,
    get_spec_audit_schema_string,
    parse_spec_audit_output,
)

# Reuse the synthesizer's cold-workspace helpers rather than duplicating.
# Both modules provision an isolated tempdir, git-init it, and scrub env —
# identical logic, intentional reuse within the same package.
from syncade.synthesizer import _init_workspace_git, _scrub_env_for_cold_synth

__all__ = [
    "AUDITOR_MODEL",
    "AUDITOR_NAME",
    "AUDITOR_PERMISSIONS",
    "AUDITOR_PROVIDER",
    "AUDITOR_THINKING",
    "DEFAULT_SPEC_AUDIT_TIMEOUT_SECONDS",
    "AuditVerdict",
    "IssueClass",
    "Severity",
    "SpecAuditFinding",
    "SpecAuditOutput",
    "SpecAuditOutputError",
    "SpecAuditResult",
    "get_spec_audit_schema_string",
    "parse_spec_audit_output",
    "run_spec_audit",
]

# ---------------------------------------------------------------------------
# Auditor knobs
# ---------------------------------------------------------------------------
# The four model knobs are now the DEFAULTS of the [auditor] config block and live
# in `syncade.config_cold` (PR-v2-23); re-exported below (see the import block) so
# existing importers keep working. Anything wanting the values in effect for THIS
# run reads `SyncadeConfig.auditor`, not these.

AUDITOR_NAME = "spec-auditor"
"""Persistence basename for the auditor's artifacts. Not a knob — stays a real
constant."""

DEFAULT_SPEC_AUDIT_TIMEOUT_SECONDS: float = 300.0
"""Default timeout for the spec audit subprocess. Generous — the auditor
reads a brief (usually <2KB) and emits a structured JSON verdict. Healthy
runs complete in 30–90s; the 300s ceiling is a safety margin."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)  # SLOTS_OK: result shape is persisted and kept stable.
class SpecAuditResult:
    """Outcome of one spec audit subprocess run.

    Mirrors :class:`~syncade.synthesizer.SynthesizerResult` /
    :class:`~syncade.test_runner.TestRunResult` /
    :class:`~syncade.producer.ProducerResult` shape so persistence and the
    CLI can treat all subprocess-result types uniformly.

    Attributes:
        outcome: ``"ready"`` iff the audit ran cleanly AND found no
            blocker-severity findings. ``"needs_clarification"`` iff the
            audit ran cleanly AND found at least one blocker-severity
            finding. ``"subprocess_error"`` iff the audit subprocess itself
            failed (or parse-failed — check ``isinstance(error,
            SpecAuditOutputError)`` to distinguish exit-70 from exit-40).
        output: The parsed :class:`SpecAuditOutput` on success;
            ``None`` on ``subprocess_error``.
        error: The exception that fired on ``subprocess_error``
            (:class:`SpecAuditOutputError` for parse failures → exit 70;
            :class:`~syncade.adapters.base.ReviewerInvocationError` or
            :class:`~syncade.process.SubprocessError` subclass for
            subprocess failures → exit 40). ``None`` on success.
        duration_seconds: Wall-clock duration of the auditor subprocess.
        raw_subprocess_result: The :class:`SubprocessResult` from the
            auditor subprocess, preserved so persistence can write artifacts
            even on timeouts and parse failures. ``None`` only when the
            subprocess never started.
    """

    outcome: Literal["ready", "needs_clarification", "subprocess_error"]
    output: SpecAuditOutput | None
    error: Exception | None
    duration_seconds: float
    raw_subprocess_result: SubprocessResult | None = field(default=None)

    def __post_init__(self) -> None:
        """Enforce the consistency table before persistence sees it."""
        if self.outcome == "subprocess_error":
            if self.output is not None:
                raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                    "SpecAuditResult(outcome='subprocess_error') requires output=None; "
                    f"got output={self.output!r}"
                )
            if self.error is None:
                raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                    "SpecAuditResult(outcome='subprocess_error') requires error to be non-None"
                )
        elif self.outcome in ("ready", "needs_clarification"):
            if self.output is None:
                raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                    f"SpecAuditResult(outcome={self.outcome!r}) requires output to be non-None"
                )
            if self.error is not None:
                raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                    f"SpecAuditResult(outcome={self.outcome!r}) requires error=None; "
                    f"got error={self.error!r}"
                )
        else:
            raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                f"SpecAuditResult: unknown outcome {self.outcome!r}"
            )


# ---------------------------------------------------------------------------
# Schema string
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _classify_outcome(output: SpecAuditOutput) -> Literal["ready", "needs_clarification"]:
    """Derive the SpecAuditResult outcome from the parsed SpecAuditOutput.

    ``"needs_clarification"`` iff any finding has severity ``"blocker"``.
    The ``verdict`` field carries the auditor's own judgment, but the
    outcome classification is mechanical — same philosophy as the
    synthesizer's mechanical verdict.
    """
    if any(f.severity == "blocker" for f in output.findings):
        return "needs_clarification"
    return "ready"


def run_spec_audit(
    *,
    pr_doc_path: Path,
    repo_root: Path,
    timeout_seconds: float = DEFAULT_SPEC_AUDIT_TIMEOUT_SECONDS,
    config: AuditorConfig | None = None,
    adapter: ReviewerAdapter | None = None,
) -> SpecAuditResult:
    """Audit the PR brief at ``pr_doc_path`` for spec-level issues.

    Lifecycle:

    1. Provision an isolated cold workspace (tempdir, copy of the PR
       doc, git-init for trusted-execute).
    2. Render the spec audit prompt from the template at
       ``<repo_root>/.syncade/templates/spec_audit.md`` (or the packaged
       default).
    3. Build the invocation from the ``[auditor]`` config block through the
       ``adapter`` — resolved from the ADAPTER REGISTRY by ``config.provider``
       when not injected, so no ``codex`` on PATH is required (PR-v2-23).
    4. Run the subprocess via :func:`syncade.process.run_subprocess`.
    5. Extract the model's final text via
       :meth:`~syncade.adapters.base.ReviewerAdapter.extract_final_text`.
    6. Parse via :func:`parse_spec_audit_output`.
    7. Classify :attr:`SpecAuditResult.outcome` based on blocker findings.

    Never raises; all failure modes map to
    ``outcome="subprocess_error"``. The caller (CLI) inspects
    ``isinstance(result.error, SpecAuditOutputError)`` to distinguish
    exit 70 (parse failure) from exit 40 (subprocess failure).

    Args:
        pr_doc_path: Absolute path to the PR brief to audit.
        repo_root: The git repo root. Used for (a) per-repo template-
            override lookup and (b) env-scrub substring check. NOT
            passed to the auditor subprocess as cwd/-C/--add-dir.
        timeout_seconds: Wall-clock timeout for the auditor subprocess.
        config: The ``[auditor]`` block. Defaults to
            :class:`~syncade.config_cold.AuditorConfig` defaults, which reproduce
            the pre-PR-v2-23 hardcoded constants exactly.
        adapter: Optional :class:`~syncade.adapters.base.ReviewerAdapter`.
            Defaults to ``get_adapter(config.provider)``. Tests pass a
            :class:`~syncade.adapters.fake.FakeAuditorAdapter`.

    Returns:
        :class:`SpecAuditResult` carrying either the parsed output or the
        captured exception, plus the raw subprocess result for persistence.
    """
    run_start = time.monotonic()
    audit_cfg = config if config is not None else AuditorConfig()
    adapter = adapter if adapter is not None else get_adapter(audit_cfg.provider)

    # --- Cold workspace ----------------------------------------------------
    with tempfile.TemporaryDirectory(prefix="syncade-audit-") as workspace_str:
        workspace = Path(workspace_str)

        # git-init so trusted-execute codex accepts the workspace.
        try:
            _init_workspace_git(workspace)
        except SubprocessError as exc:
            return SpecAuditResult(
                outcome="subprocess_error",
                output=None,
                error=exc,
                duration_seconds=time.monotonic() - run_start,
            )

        # Copy the PR doc into the workspace.
        pr_doc_subdir = workspace / "pr-doc"
        try:
            pr_doc_subdir.mkdir()
            workspace_pr_doc = pr_doc_subdir / pr_doc_path.name
            shutil.copy2(pr_doc_path, workspace_pr_doc)
        except (OSError, shutil.SameFileError) as exc:
            return SpecAuditResult(
                outcome="subprocess_error",
                output=None,
                error=SubprocessError(
                    f"spec audit: failed to set up cold workspace at "
                    f"{workspace}: could not copy {pr_doc_path} — {exc}"
                ),
                duration_seconds=time.monotonic() - run_start,
            )

        # --- Build the prompt ------------------------------------------
        template = load_spec_audit_template(repo_root)
        prompt = render_spec_audit_prompt(
            template,
            pr_doc_path=str(workspace_pr_doc),
            json_schema=get_spec_audit_schema_string(),
        )

        # --- Build the invocation --------------------------------
        audit_config = ReviewerConfig(
            name=AUDITOR_NAME,
            provider=audit_cfg.provider,
            model=audit_cfg.model,
            thinking=audit_cfg.thinking,
            permissions=audit_cfg.permissions,
            # PR-v2-24: carry the auth declaration onto the synthetic config the
            # adapter sees. Without this the cold actors would silently run
            # unenforced -- the classic 'guarded four of five actors' leak.
            auth=audit_cfg.auth,
            api_key_env=audit_cfg.api_key_env,
        )
        try:
            invocation = adapter.build_invocation(audit_config, workspace, prompt)
        except ValueError as exc:
            return SpecAuditResult(
                outcome="subprocess_error",
                output=None,
                error=exc,
                duration_seconds=time.monotonic() - run_start,
            )

        # --- Run the subprocess ----------------------------------------
        subprocess_result: SubprocessResult | None = None
        try:
            subprocess_result = run_subprocess(
                invocation.argv,
                cwd=workspace,
                env=_scrub_env_for_cold_synth(invocation.env, repo_root),
                timeout=timeout_seconds,
                input_text=invocation.stdin_text,
            )
        except SubprocessTimeoutError as exc:
            elapsed = time.monotonic() - run_start
            return SpecAuditResult(
                outcome="subprocess_error",
                output=None,
                error=exc,
                duration_seconds=elapsed,
                raw_subprocess_result=SubprocessResult(
                    returncode=-1,
                    stdout=exc.stdout,
                    stderr=exc.stderr,
                    duration_seconds=elapsed,
                ),
            )
        except SubprocessNotFoundError as exc:
            return SpecAuditResult(
                outcome="subprocess_error",
                output=None,
                error=exc,
                duration_seconds=time.monotonic() - run_start,
            )
        except SubprocessError as exc:
            return SpecAuditResult(
                outcome="subprocess_error",
                output=None,
                error=exc,
                duration_seconds=time.monotonic() - run_start,
            )

        # --- Parse the output ------------------------------------------
        try:
            final_text = adapter.extract_final_text(
                subprocess_result,
                empty_output_exception_class=SpecAuditOutputError,
            )
        except (ReviewerInvocationError, SpecAuditOutputError) as exc:
            return SpecAuditResult(
                outcome="subprocess_error",
                output=None,
                error=exc,
                duration_seconds=time.monotonic() - run_start,
                raw_subprocess_result=subprocess_result,
            )
        except Exception as exc:  # noqa: BLE001  # BROAD_EXCEPT_OK: boundary converts parser surprises.
            # Same broad-catch rationale as synthesizer.run_synthesizer:
            # unexpected exceptions from the extractor (AttributeError,
            # IndexError on bizarre JSONL) are more plausibly model-output
            # variation than programming bugs; map to polite failure so
            # persistence writes every artifact the operator needs.
            return SpecAuditResult(
                outcome="subprocess_error",
                output=None,
                error=exc,
                duration_seconds=time.monotonic() - run_start,
                raw_subprocess_result=subprocess_result,
            )

        try:
            output = parse_spec_audit_output(final_text)
        except SpecAuditOutputError as exc:
            return SpecAuditResult(
                outcome="subprocess_error",
                output=None,
                error=exc,
                duration_seconds=time.monotonic() - run_start,
                raw_subprocess_result=subprocess_result,
            )

        return SpecAuditResult(
            outcome=_classify_outcome(output),
            output=output,
            error=None,
            duration_seconds=time.monotonic() - run_start,
            raw_subprocess_result=subprocess_result,
        )
