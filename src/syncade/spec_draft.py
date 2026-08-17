# SIZE_OK: 334 pure LOC; cold drafter schema, parser, and renderer move together.
# Retained to avoid separating the ratified draft contract from its parser.
# Future split: move output models/rendering to spec_draft_models.py.
"""Cold-drafter engine.

Given a session *dialogue* (from :mod:`syncade.transcript`) + a *diff*, a cold
subprocess manufactures an OpenSpec-shaped :class:`SpecDraftOutput` with
**self-flagged assumptions**, so an independent reviewer can later measure the
work against a checkable intent. Structurally mirrors :mod:`syncade.spec_audit`
(registry-resolved cold adapter, isolated git-init'd tempdir, env scrub, never-raises,
mechanical ``_classify_outcome``).

The honesty property: the drafter is *disciplined*-cold, not hard-isolated — it
reads the dialogue (to find affirmed proposals) but the prompt
(``templates/spec_draft.md``) enforces the direction-based firewall (admit
forward-looking intent; exclude backward-looking self-justification). This is
coherent only because the output is **ratified by a human before use**.

Exit-code mapping (CLI mode handler, ``--draft-spec``): a well-formed draft → 0;
subprocess failure → 40; parse failure → 70 (``SpecDraftOutputError``). The
drafter has no "blocker" concept — a draft is advisory.
"""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from syncade.adapters.base import ReviewerAdapter, ReviewerInvocationError
from syncade.adapters.registry import get_adapter
from syncade.config import ReviewerConfig
from syncade.config_cold import (
    DRAFTER_MODEL as DRAFTER_MODEL,
)
from syncade.config_cold import (
    DRAFTER_PERMISSIONS as DRAFTER_PERMISSIONS,
)
from syncade.config_cold import (
    DRAFTER_PROVIDER as DRAFTER_PROVIDER,
)
from syncade.config_cold import (
    DRAFTER_THINKING as DRAFTER_THINKING,
)
from syncade.config_cold import (
    DrafterConfig,
)
from syncade.findings_json import decode_and_validate, validate_dropping_forbidden_extras
from syncade.process import (
    SubprocessError,
    SubprocessNotFoundError,
    SubprocessResult,
    SubprocessTimeoutError,
    run_subprocess,
)
from syncade.prompts import load_spec_draft_template, render_spec_draft_prompt

# The drafter provisions an isolated tempdir, git-inits it, and scrubs env —
# identical cold-isolation needs as the synthesizer + auditor. Reuse their helpers.
from syncade.synthesizer import _init_workspace_git, _scrub_env_for_cold_synth

DEFAULT_SPEC_DRAFT_TIMEOUT_SECONDS = 600.0


class SpecDraftOutputError(Exception):
    """Raised when the drafter's stdout can't be parsed as a
    :class:`SpecDraftOutput`. The CLI routes this to exit 70."""


def _reject_whitespace(value: str) -> str:
    if not value.strip():
        raise ValueError(  # GENERIC_ERR_OK: Pydantic validator expects ValueError.
            "must contain non-whitespace content; got an all-whitespace value"
        )
    return value


class Criterion(BaseModel):
    """One acceptance criterion + its honesty tag. ``origin="inferred"`` marks a
    criterion the drafter inferred rather than transcribed from the user's explicit
    words — a ratification confirm-point."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="A discrete, independently checkable, scenario-style criterion.")
    origin: Literal["transcribed", "inferred"] = Field(
        description="`transcribed` = from the user's explicit words; `inferred` = drafter-inferred."
    )

    _no_ws_text = field_validator("text")(_reject_whitespace)


class Delta(BaseModel):
    """An OpenSpec-style requirement delta inferred from the diff."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["ADDED", "MODIFIED", "REMOVED"]
    capability: str = Field(description="The capability/area this requirement touches.")
    requirement: str = Field(description="What the requirement is.")

    _no_ws_cap = field_validator("capability")(_reject_whitespace)
    _no_ws_req = field_validator("requirement")(_reject_whitespace)


class SpecDraftOutput(BaseModel):
    """The cold drafter's manufactured spec (OpenSpec-shaped, self-flagged)."""

    model_config = ConfigDict(extra="forbid")

    proposal: str = Field(description="Why this work exists and what changes, in the user's terms.")
    acceptance_criteria: list[Criterion] = Field(
        min_length=1, description="The yardstick units; at least one."
    )
    deltas: list[Delta] = Field(default_factory=list)
    assumptions: list[str] = Field(
        default_factory=list,
        description="Cross-cutting inferences the drafter made; ratification confirm-points.",
    )

    _no_ws_proposal = field_validator("proposal")(_reject_whitespace)

    @field_validator("assumptions")
    @classmethod
    def _assumptions_non_whitespace(cls, v: list[str]) -> list[str]:
        for item in v:
            if not item.strip():
                raise ValueError(  # GENERIC_ERR_OK: Pydantic validator expects ValueError.
                    "assumptions entries must be non-whitespace"
                )
        return v


def get_spec_draft_schema_string() -> str:
    """The schema description embedded in the drafter prompt's ``{json_schema}``
    placeholder (a value, not a format string — single braces)."""
    return (
        "{\n"
        '  "proposal": "<why this work exists + what changes, in the user\'s terms>",\n'
        '  "acceptance_criteria": [\n'
        '    {"text": "<discrete, checkable, scenario-style criterion>",\n'
        '     "origin": "transcribed" | "inferred"}\n'
        "  ],\n"
        '  "deltas": [\n'
        '    {"kind": "ADDED" | "MODIFIED" | "REMOVED",\n'
        '     "capability": "<area>", "requirement": "<what changes>"}\n'
        "  ],\n"
        '  "assumptions": ["<a cross-cutting inference the user should confirm>"]\n'
        "}\n\n"
        "Rules: acceptance_criteria must have at least one entry; every criterion\n"
        "needs an `origin`; `deltas` and `assumptions` may be empty lists; no extra\n"
        "fields (the parser uses extra=forbid and will reject unknown keys)."
    )


def render_draft_spec_markdown(
    output: SpecDraftOutput, *, source_label: str, base_ref: str | None = None
) -> str:
    """Render a :class:`SpecDraftOutput` into a ratifiable OpenSpec-shaped markdown
    spec. The **Assumptions to confirm** section surfaces every inferred criterion
    + cross-cutting assumption as the human's ratification confirm-points (the
    honesty backstop — review/edit before running ``syncade <this-file>``).

    ``base_ref`` is the ``--base`` ref used when drafting, if any. When supplied it
    is embedded in the ratification command so the later review runs against the
    same diff the drafter saw."""
    syncade_cmd = (
        f"`syncade <this-file> --base {base_ref}`" if base_ref else "`syncade <this-file>`"
    )
    lines: list[str] = [
        f"# Draft spec: {source_label}",
        "",
        (
            "> Manufactured by `syncade --draft-spec` from a session transcript. This is a "
            "DRAFT — review and edit it before use. The **Assumptions to confirm** section "
            "lists everything the drafter inferred rather than transcribed; confirm or correct "
            f"each. Once ratified, run {syncade_cmd} to review your work against it."
        ),
        "",
        "## Why / What",
        "",
        output.proposal.strip(),
        "",
        "## Acceptance criteria",
        "",
    ]
    for i, criterion in enumerate(output.acceptance_criteria, start=1):
        tag = "" if criterion.origin == "transcribed" else "  _(inferred — confirm)_"
        lines.append(f"{i}. {criterion.text.strip()}{tag}")
    if output.deltas:
        lines += ["", "## Deltas", ""]
        for delta in output.deltas:
            lines.append(f"- **{delta.kind}** ({delta.capability}): {delta.requirement}")
    lines += ["", "## Assumptions to confirm", ""]
    inferred = [c for c in output.acceptance_criteria if c.origin == "inferred"]
    if inferred or output.assumptions:
        lines.append(
            "These were INFERRED, not stated outright — confirm or correct each before "
            "relying on this spec:"
        )
        lines.append("")
        for criterion in inferred:
            lines.append(f"- (criterion) {criterion.text.strip()}")
        for assumption in output.assumptions:
            lines.append(f"- {assumption.strip()}")
    else:
        lines.append(
            "(none — every acceptance criterion was transcribed from your explicit words; "
            "no cross-cutting assumptions were flagged.)"
        )
    return "\n".join(lines) + "\n"


def parse_spec_draft_output(raw: str) -> SpecDraftOutput:
    """Parse the drafter's raw stdout into a :class:`SpecDraftOutput`.

    Selects exactly ONE verdict block via
    :func:`syncade.findings_json._decode_verdict_object` (last
    ``json``/unlabeled fence, else the whole response) and validates it. No
    fallback to an earlier block — see :mod:`syncade.findings_json`. Raises
    :class:`SpecDraftOutputError` on failure (→ exit 70)."""
    return decode_and_validate(
        raw,
        # Same repair the reviewer gets (PR-h-field-05 item 2): a verdict whose ONLY
        # defect is a forbidden extra key is repaired, not discarded. Cheaper leg than
        # a reviewer, but the failure mode and the eligibility rule are identical.
        validate=lambda payload: validate_dropping_forbidden_extras(
            payload, SpecDraftOutput.model_validate, label="spec draft"
        ),
        error=SpecDraftOutputError,
        label="spec draft",
        model_name="SpecDraftOutput",
        artifact="spec-drafter.stdout in the run directory",
    )


# --- Cold drafter knobs -------------------------------------------------------
# The four model knobs are now the DEFAULTS of the [drafter] config block and live
# in `syncade.config_cold` (PR-v2-23); re-exported here so existing importers keep
# working. DRAFTER_NAME stays a real constant — it is an artifact basename, not a
# knob. Anything wanting the values in effect for THIS run reads
# `SyncadeConfig.drafter`, not these.
DRAFTER_NAME = "spec-drafter"


@dataclass  # MUTABLE_OK, SLOTS_OK: result shape is persisted and kept stable.
class SpecDraftResult:
    """Outcome of a drafter run. ``drafted`` ⟺ output set + error None;
    ``subprocess_error`` ⟺ output None + error set (parse failures carry a
    :class:`SpecDraftOutputError`, which the CLI maps to exit 70)."""

    outcome: Literal["drafted", "subprocess_error"]
    output: SpecDraftOutput | None
    error: Exception | None
    duration_seconds: float
    raw_subprocess_result: SubprocessResult | None = field(default=None)

    def __post_init__(self) -> None:
        if self.outcome == "subprocess_error":  # IF_VARIANT_OK: existing invariant guard.
            if self.output is not None:
                raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                    "subprocess_error requires output=None"
                )
            if self.error is None:
                raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                    "subprocess_error requires a non-None error"
                )
        elif self.outcome == "drafted":
            if self.output is None:
                raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                    "drafted requires a non-None output"
                )
            if self.error is not None:
                raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                    "drafted requires error=None"
                )
        else:
            raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                f"unknown outcome {self.outcome!r}"
            )


def _classify_outcome(output: SpecDraftOutput) -> Literal["drafted"]:
    """A well-formed draft is always ``drafted`` — the drafter is advisory and has
    no blocker concept (the human ratifies; the later review judges)."""
    del output
    return "drafted"


def run_spec_draft(
    *,
    dialogue: str,
    diff: str,
    repo_root: Path,
    timeout_seconds: float = DEFAULT_SPEC_DRAFT_TIMEOUT_SECONDS,
    config: DrafterConfig | None = None,
    adapter: ReviewerAdapter | None = None,
) -> SpecDraftResult:
    """Manufacture a spec from ``dialogue`` + ``diff`` via a cold subprocess.

    Writes the two inputs into an isolated git-init'd tempdir, renders the
    firewall prompt, runs the drafter, parses the output. Never raises — every
    failure maps to ``outcome="subprocess_error"`` (parse failures carry a
    :class:`SpecDraftOutputError`). ``repo_root`` is used only for per-repo
    template override + the env-scrub substring check, never passed to the
    subprocess as cwd.

    ``config`` is the ``[drafter]`` block; ``adapter`` defaults to
    ``get_adapter(config.provider)`` — the same registry the dispatcher uses, so a
    box with no ``codex`` on PATH can still draft a spec (PR-v2-23)."""
    run_start = time.monotonic()
    draft_cfg = config if config is not None else DrafterConfig()
    adapter = adapter if adapter is not None else get_adapter(draft_cfg.provider)

    def _err(exc: Exception, raw: SubprocessResult | None = None) -> SpecDraftResult:
        return SpecDraftResult(
            outcome="subprocess_error",
            output=None,
            error=exc,
            duration_seconds=time.monotonic() - run_start,
            raw_subprocess_result=raw,
        )

    with tempfile.TemporaryDirectory(prefix="syncade-draft-") as workspace_str:
        workspace = Path(workspace_str)
        try:
            _init_workspace_git(workspace)
        except SubprocessError as exc:
            return _err(exc)

        try:
            dialogue_file = workspace / "dialogue.md"
            dialogue_file.write_text(dialogue, encoding="utf-8")
            diff_file = workspace / "diff.txt"
            diff_file.write_text(
                diff if diff.strip() else "(no diff provided — draft from the dialogue alone)\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return _err(
                SubprocessError(f"spec draft: failed to write inputs to {workspace}: {exc}")
            )

        template = load_spec_draft_template(repo_root)
        try:
            prompt = render_spec_draft_prompt(
                template,
                dialogue_path=str(dialogue_file),
                diff_path=str(diff_file),
                json_schema=get_spec_draft_schema_string(),
            )
        except KeyError as exc:
            return _err(
                SubprocessError(
                    f"spec draft: template has unknown placeholder {exc} — "
                    "check .syncade/templates/spec_draft.md"
                )
            )

        draft_config = ReviewerConfig(
            name=DRAFTER_NAME,
            provider=draft_cfg.provider,
            model=draft_cfg.model,
            thinking=draft_cfg.thinking,
            permissions=draft_cfg.permissions,
            # PR-v2-24: carry the auth declaration onto the synthetic config the
            # adapter sees. Without this the cold actors would silently run
            # unenforced -- the classic 'guarded four of five actors' leak.
            auth=draft_cfg.auth,
            api_key_env=draft_cfg.api_key_env,
        )
        try:
            invocation = adapter.build_invocation(draft_config, workspace, prompt)
        except ValueError as exc:
            return _err(exc)

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
            return SpecDraftResult(
                outcome="subprocess_error",
                output=None,
                error=exc,
                duration_seconds=elapsed,
                raw_subprocess_result=SubprocessResult(
                    returncode=-1, stdout=exc.stdout, stderr=exc.stderr, duration_seconds=elapsed
                ),
            )
        except (SubprocessNotFoundError, SubprocessError) as exc:
            return _err(exc)

        try:
            final_text = adapter.extract_final_text(
                subprocess_result, empty_output_exception_class=SpecDraftOutputError
            )
        except (ReviewerInvocationError, SpecDraftOutputError) as exc:
            return _err(exc, subprocess_result)
        except Exception as exc:  # noqa: BLE001  # BROAD_EXCEPT_OK: boundary converts parser surprises.
            return _err(exc, subprocess_result)

        try:
            output = parse_spec_draft_output(final_text)
        except SpecDraftOutputError as exc:
            return _err(exc, subprocess_result)

        return SpecDraftResult(
            outcome=_classify_outcome(output),
            output=output,
            error=None,
            duration_seconds=time.monotonic() - run_start,
            raw_subprocess_result=subprocess_result,
        )
