# config.py is the public Pydantic config import surface: it owns the reviewer/review/
# top-level models and RE-EXPORTS the rest, so `from syncade.config import X` keeps
# working for every X it ever exposed. Producer + harness detection live in
# ``config_producer``; the shared Literals in ``config_types`` (PR-v2-23 — this module
# was 462 LOC against a blocking 500 cap and had no room for the judge/drafter blocks).
"""Pydantic v2 models for ``.syncade/config.toml``.

Schema mirrors the PRD's "Configuration spec" section exactly. All defaults
match the PRD; any TOML field not listed here is rejected (``extra="forbid"``)
so typos in user config files surface as validation errors rather than being
silently ignored.
"""

import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from syncade.checks_config import CheckConfig, validate_check_names
from syncade.config_auth import AuthedActor
from syncade.config_cold import AuditorConfig, DrafterConfig, SynthesizerConfig
from syncade.config_gc import GcConfig
from syncade.config_loop import LoopConfig
from syncade.config_producer import (
    _PRODUCER_MODELS,
    Harness,
    ProducerConfig,
    ProducerPermissions,
    ProducerProvider,
    _default_producer_model,
    _default_producer_provider,
    _invoking_harness,
)
from syncade.config_retry import RetryConfig
from syncade.config_types import Permissions, Thinking
from syncade.diff_filter import REVIEWER_STRIP_FILES
from syncade.pricing_config import PricingConfig
from syncade.worktree import DEFAULT_WORKTREE_BASE, TEST_WORKTREE_NAME

__all__ = [
    # re-exported so every existing `from syncade.config import X` keeps working
    # after the PR-v2-23 split (config_producer / config_cold / config_types).
    "AuditorConfig",
    "DrafterConfig",
    "Harness",
    "Permissions",
    "ProducerConfig",
    "ProducerPermissions",
    "ProducerProvider",
    "ReviewConfig",
    "ReviewerConfig",
    "SyncadeConfig",
    "SynthesizerConfig",
    "Thinking",
    # private, but imported by tests and by config's own defaults
    "_PRODUCER_MODELS",
    "_default_producer_model",
    "_default_producer_provider",
    "_invoking_harness",
]

# ---------------------------------------------------------------------------
# Literal type aliases for enum-like fields. Kept narrow on purpose: any value
# not listed here will fail validation, which is the point.
# ---------------------------------------------------------------------------

# Thinking / Permissions moved to ``config_types`` so config.py and
# config_producer.py can share them without an import cycle. Re-exported below.

# Producer config + harness detection moved to ``config_producer`` (PR-v2-23) to
# keep this module under the blocking 500-LOC cap. Re-exported below so every
# existing ``from syncade.config import ProducerConfig`` keeps working.


class ReviewerConfig(AuthedActor):
    """A single reviewer entry. Multiple reviewers are configured via
    repeated ``[[reviewers]]`` TOML blocks. Adding another reviewer that
    uses an already-registered provider is configuration-only; adding a
    new provider requires an adapter and registry entry."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description=(
            "Stable identifier used in run artifacts and findings. "
            "Must not contain ``=`` — that character is the delimiter in the "
            "``NAME=VALUE`` per-reviewer CLI override grammar "
            "(``--reviewer-model``, ``--reviewer-thinking``, ``--reviewer-timeout``), "
            "so a name containing it cannot be targeted."
        ),
    )

    @field_validator("name")
    @classmethod
    def _name_no_equals(cls, value: str) -> str:
        if "=" in value:
            raise ValueError(  # GENERIC_ERR_OK: Pydantic validator expects ValueError.
                f"reviewer name {value!r} must not contain '=' — that character is the "
                "NAME=VALUE delimiter for --reviewer-model / --reviewer-thinking / "
                "--reviewer-timeout overrides; a name containing it cannot be targeted"
            )
        return value

    provider: str = Field(
        description="Model provider (e.g. ``anthropic``, ``openai``). "
        "Resolved against the adapter registry at dispatch time; new provider "
        "names require code support before they can be used in config.",
    )
    model: str = Field(
        description="Model identifier within the provider.",
    )
    thinking: Thinking = Field(
        default="high",
        description=(
            "Reasoning effort budget for this reviewer. "
            "CANONICAL SOURCE: this field's type annotation "
            "(:data:`syncade.config.Thinking`) is the single "
            "authoritative enumeration of accepted thinking values "
            "across syncade. All docs that need to list these "
            "values reference this field rather than duplicating "
            "the list. Future enum extensions update only the "
            "Literal; doc references stay current automatically. "
            "If you find yourself listing thinking values in a doc, "
            "link to :data:`syncade.config.Thinking` instead. The "
            "drift-defense regression test is "
            "``tests/config/test_config_schema.py::"
            "test_thinking_canonical_source_no_duplicate_enumerations``."
        ),
    )
    permissions: Permissions = Field(
        default="trusted-execute",
        description=(
            "Tool-permission tier for this reviewer. ``trusted-execute`` is the "
            "default: it still runs fully unattended (Codex "
            "``-s workspace-write -c approval_policy=never``, Anthropic "
            "``bypassPermissions`` — neither ever prompts) but keeps the OS "
            "sandbox ACTIVE and scoped to the reviewer's worktree, so worktree "
            "confinement is enforced structurally instead of resting on the "
            "prompt asking the model to stay put. ``yolo`` maps to Codex's "
            "``--dangerously-bypass-approvals-and-sandbox`` and turns that "
            "sandbox off; it buys a reviewer nothing, since reviewers only read "
            "the repo and run its test/lint commands inside the worktree. "
            "(``yolo`` and ``trusted-execute`` are identical on Anthropic — both "
            "are ``bypassPermissions``; the distinction only bites on Codex, "
            "which is the default reviewer provider.) ``safe`` is rejected by "
            "the real adapters: it prompts, so it hangs a headless subprocess."
        ),
    )
    adversarial_lens: bool = Field(
        default=False,
        description=(
            "When true, this reviewer's prompt carries the adversarial "
            "edge-enumeration block (enumerate-then-attack the flag/input/state "
            "combinations the spec does NOT cover, before any SHIP). Opt-in and "
            "default-off so only explicitly configured reviewers receive the "
            "adversarial lens."
        ),
    )
    template: str | None = Field(
        default=None,
        description=(
            "Optional reviewer prompt template basename that overrides "
            "provider-based selection (e.g. ``reviewer_adversarial.md``). When "
            "unset, the reviewer uses its provider's default template. Must be a "
            "plain basename — path separators, absolute paths, and a bare "
            "`.`/`..` are rejected at config load (mirrors load_template; a "
            "filename that merely contains `..`, e.g. `foo..md`, is accepted)."
        ),
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Per-reviewer wall-clock timeout in seconds. ``None`` (default) reuses the loop "
            "timeout (:attr:`LoopConfig.timeout_seconds`, itself overridable by the global "
            "``--timeout``). Set it to give one reviewer a longer or shorter budget than the "
            "others — e.g. a heavier model more time. Must be > 0 and finite when set; NaN and "
            "infinity are rejected (same ``math.isfinite`` guard as the producer / loop timeouts)."
        ),
    )

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _timeout_seconds_strict_number(cls, value: object) -> object:
        # None (unset) reuses the loop global. bool subclasses float (True→1.0) and a quoted number
        # ("1"→1.0) coerces silently — both are config mistakes in a TOML float knob, so reject them
        # (exit 50). A plain int is accepted and widened to float. (The CLI parses the
        # --reviewer-timeout value to float BEFORE validation, so this doesn't break the flag path.)
        if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)):
            return value
        raise ValueError(  # GENERIC_ERR_OK: Pydantic validator expects ValueError.
            f"reviewer timeout_seconds must be a number (got {value!r}); quoted numbers and "
            "booleans are rejected"
        )

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_seconds_isfinite(cls, value: float | None) -> float | None:
        # Reject NaN / inf — pydantic's gt=0 admits both. Mirrors ProducerConfig / LoopConfig: an
        # unkillable reviewer subprocess would hang the whole parallel dispatch.
        if value is not None and not math.isfinite(value):
            raise ValueError(  # GENERIC_ERR_OK: Pydantic validator expects ValueError.
                f"reviewer timeout_seconds must be a finite positive number; got {value!r}"
            )
        return value

    @field_validator("template")
    @classmethod
    def _validate_template_basename(cls, value: str | None) -> str | None:
        # Exact-match `.`/`..` (not substring) deliberately mirrors
        # load_template's basename guard: only `.` or `..` as the WHOLE value
        # traverses out of the templates dir. A filename that merely contains
        # `..` (e.g. `foo..md`) is a safe basename and is accepted — rejecting
        # substring `..` would over-reject valid names and drift from
        # load_template.
        if value is None:
            return value
        if (
            not value
            or "/" in value
            or "\\" in value
            or value in (".", "..")
            or Path(value).is_absolute()
        ):
            raise ValueError(
                f"reviewer template {value!r} must be a plain basename "
                "(no separators, parent refs, or absolute paths)"
            )
        return value


class ReviewConfig(BaseModel):
    """What reviewers are allowed to see and how their worktrees are
    prepared."""

    model_config = ConfigDict(extra="forbid")

    include_producer_summary: bool = Field(
        default=False,
        description="If False (v1 default), reviewers see only the PR doc, "
        "the diff, and the test output — never the producer's narrative.",
    )
    strip_repo_context_files: list[str] = Field(
        default_factory=lambda: list(REVIEWER_STRIP_FILES),
        description="Files to delete (or stub) from each reviewer worktree "
        "before dispatch, to prevent context leak from the producer's setup. "
        "this same list also drives the reviewer-facing diff filter "
        "(syncade.diff_filter.filter_diff_for_reviewer), so the worktree "
        "strip and the diff strip can never diverge. The default is sourced "
        "from REVIEWER_STRIP_FILES (single source of truth).",
    )


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


def _default_reviewers() -> list[ReviewerConfig]:
    """The two default reviewers used when no ``[[reviewers]]`` blocks
    are present in the user's config: ``codex-reviewer`` on the standard
    prompt and ``codex-reviewer-adv`` on ``reviewer_adversarial.md`` with
    the adversarial lens. Both use OpenAI / ``gpt-5.5``; the panel is
    same-model / cross-prompt while the Anthropic reviewer is offlined.
    The Anthropic reviewer is offlined but revivable — see the commented
    revival example below.

    Deliberately NOT harness-aware, unlike the producer. The reviewers and the
    judge are the blind panel; pinning them to one cold tier is what keeps a
    verdict comparable across runs and keeps the panel a different model from
    the Anthropic producer under Claude Code. Only the producer follows the
    harness the operator is coding in (see :func:`_invoking_harness`).

    **Why ``gpt-5.5``** (PR-29): PR-28 moved the panel to ``gpt-5.6-sol`` @
    ``medium``; two dogfood rounds later measurement showed it
    making ~18 tool calls/round against ``gpt-5.5`` @ ``xhigh``'s 90–101, with the
    plain reviewer shipping 2/2 rounds at ZERO findings while only the adversarial
    one caught anything. That is the same leniency shape that got the Anthropic
    reviewer offlined on 2026-06-30, so the panel went back to the configuration
    with 94 rounds behind it (29%/26% ship-rate, 81/77 findings) rather than being
    kept on a 2-round sample. The judge (:data:`SYNTHESIZER_MODEL`) went back too
    and still matches this pin. See the dogfood history (2026-07-11 → 07-12).

    ``gpt-5.5`` rather than ``gpt-5-codex``: real
    ``codex exec --model gpt-5-codex`` returns
    ``"The 'gpt-5-codex' model is not supported when using Codex with a
    ChatGPT account"`` for the auth mode this orchestrator targets in
    v1 (ChatGPT-account-backed ``codex login``). Users who pay for API-key
    auth and want ``gpt-5-codex`` can override per-reviewer in their
    ``.syncade/config.toml``.
    """
    return [
        ReviewerConfig(
            name="codex-reviewer",
            provider="openai",
            model="gpt-5.5",
        ),
        ReviewerConfig(
            name="codex-reviewer-adv",
            provider="openai",
            model="gpt-5.5",
            template="reviewer_adversarial.md",
            adversarial_lens=True,
        ),
    ]
    # Claude reviewer offlined 2026-06-30 (0 unique synth-upheld blocking
    # verdicts across many dogfood datapoints;. Revive
    # by adding, e.g.:
    #   ReviewerConfig(name="claude-reviewer", provider="anthropic",
    #                  model="claude-opus-4-6", adversarial_lens=True)


class SyncadeConfig(BaseModel):
    """Top-level ``.syncade/config.toml`` schema.

    Loading is handled by :mod:`syncade.config_loader`. Constructing this
    class with no arguments yields the PRD's documented zero-config defaults.
    """

    model_config = ConfigDict(extra="forbid")

    producer: ProducerConfig = Field(default_factory=ProducerConfig)
    reviewers: list[ReviewerConfig] = Field(
        default_factory=_default_reviewers,
        min_length=1,
        description="At least one reviewer is required. Zero-config defaults "
        "are two OpenAI reviewers (codex-reviewer + codex-reviewer-adv) — "
        "same-model, cross-prompt while the Anthropic reviewer is offlined. "
        "Custom configs are validated against the reviewer adapter registry, "
        "but provider diversity is advisory rather than schema- or "
        "dispatch-enforced today.",
    )
    loop: LoopConfig = Field(default_factory=LoopConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    # The shared transient-retry bound (PR-v2-9). Default reproduces retry.MAX_RETRIES exactly, so a
    # zero-config run is byte-identical; threaded to the reviewer / synth / producer legs.
    retry: RetryConfig = Field(default_factory=RetryConfig)
    # Run-artifact retention (PR-v2-9). Defaults reproduce gc.DEFAULT_KEEP / DEFAULT_MAX_AGE_DAYS;
    # governs BOTH the per-loop auto-prune and an explicit ``syncade --gc``.
    gc: GcConfig = Field(default_factory=GcConfig)
    # Where per-run git worktrees are provisioned (PR-v2-9). A single top-level value (not a
    # ``[worktree]`` block — Q5); default reproduces worktree.DEFAULT_WORKTREE_BASE.
    # ``--worktree-base`` overrides per-invocation; threaded into the review run and doctor preview.
    worktree_base: Path = Field(
        default=DEFAULT_WORKTREE_BASE,
        description=(
            "Base directory under which each run's per-reviewer/producer/test git worktrees are "
            f"created (default ``{DEFAULT_WORKTREE_BASE}``). Overridable per-run with "
            "``--worktree-base``. Point it at a fast local disk if ``/tmp`` is small or slow."
        ),
    )
    # User-defined mechanical checks. Empty (default) = today's loop,
    # byte-identical. Model + collision logic live in `syncade.checks_config`.
    checks: list[CheckConfig] = Field(default_factory=list)
    # Per-model token pricing for cost estimation (PR-v2-04). The model lives in
    # `syncade.pricing_config` (config.py is at its LOC cap); a default table ships
    # so zero-config runs still estimate cost. MANDATORY field — extra="forbid"
    # would otherwise reject a user's [pricing] table.
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    # The three COLD actors (PR-v2-23) — all were hardcoded to CodexAdapter, so
    # `codex` was mandatory even for an all-Anthropic user. Models live in
    # `syncade.config_cold`, which is a leaf: the actor modules re-export its
    # defaults and import `config`, so the values cannot live here. Every default
    # reproduces the previous hardcoded constants exactly — a zero-config run is
    # byte-identical.
    synthesizer: SynthesizerConfig = Field(default_factory=SynthesizerConfig)
    drafter: DrafterConfig = Field(default_factory=DrafterConfig)
    auditor: AuditorConfig = Field(default_factory=AuditorConfig)

    @field_validator("worktree_base", mode="before")
    @classmethod
    def _reject_nul_in_worktree_base(cls, value: object) -> object:
        # Path() accepts embedded NUL bytes but the OS rejects them at the first
        # syscall, producing an uncaught ValueError instead of a config error.
        # Catch it here so load_config raises ConfigError (exit 50) consistently.
        if isinstance(value, str) and "\x00" in value:
            raise ValueError(  # GENERIC_ERR_OK: Pydantic validator expects ValueError.
                "worktree_base must not contain an embedded NUL byte"
            )
        return value

    @model_validator(mode="after")
    def _reject_duplicate_reviewer_names(self) -> "SyncadeConfig":
        """Reviewer ``name`` is the dedup key in synthesis cross-input
        validation (:mod:`syncade.synthesizer.validation` indexes finding
        counts and unanimous-blocker provenance by ``reviewer_name``) and
        the per-reviewer prompt/worktree key in dispatch. Two reviewers
        sharing a name would collapse to one key and silently undercount a
        unanimous blocker, so reject duplicates at config-load with a clear
        error naming the offender(s) rather than letting the ambiguity reach
        synthesis. Exact-string match mirrors the dedup key, which is an
        exact dict/tuple key (not casefolded)."""
        seen: set[str] = set()
        duplicates: list[str] = []
        for reviewer in self.reviewers:
            if reviewer.name in seen and reviewer.name not in duplicates:
                duplicates.append(reviewer.name)
            seen.add(reviewer.name)
        if duplicates:
            raise ValueError(  # GENERIC_ERR_OK: Pydantic model validator expects ValueError.
                f"duplicate reviewer name(s) {duplicates!r}; each "
                f"[[reviewers]] name must be unique (the name is the dedup "
                f"key in synthesis and the per-reviewer worktree/prompt key "
                f"in dispatch)."
            )
        return self

    @model_validator(mode="after")
    def _reject_reviewer_named_tests_when_test_command_set(self) -> "SyncadeConfig":
        """``TEST_WORKTREE_NAME`` is the
        reserved worktree-basename the test re-run leg uses
        (single source of truth in :mod:`syncade.worktree`). When the
        operator configures ``[loop] test_command``, both the test
        leg AND a reviewer with a colliding name would try to
        create a worktree at the same path and the second one
        would fail with a generic ``WorktreeError``.

        Case-insensitive comparison is required: exact-match checks miss
        ``"Tests"`` / ``"TESTS"`` / ``"TeStS"`` etc. On
        case-insensitive filesystems (macOS default HFS+/APFS,
        Windows) those all resolve to the SAME on-disk path and
        produce the same collision. ``casefold()`` is the right
        comparator for case-insensitive equality (covers
        full-Unicode case folding like German ß → "ss").

        Catching this at config-load gives the operator a clear
        error message naming the conflict, rather than paying the
        reviewer+synth cost first only to fail on worktree
        collision in the test phase.

        The check is conditional on ``test_command`` being set — a
        reviewer named ``"tests"`` is harmless when the test leg
        is disabled (the only worktree at that path would be the
        reviewer's own).
        """
        if self.loop.test_command is None:
            return self
        reserved = TEST_WORKTREE_NAME.casefold()
        offenders = [r.name for r in self.reviewers if r.name.casefold() == reserved]
        if offenders:
            raise ValueError(  # GENERIC_ERR_OK: Pydantic model validator expects ValueError.
                f"reviewer name(s) {offenders!r} collide with the "
                f"reserved test-re-run worktree basename "
                f"{TEST_WORKTREE_NAME!r} (case-insensitive) used when "
                f"`[loop] test_command` is configured. Rename the "
                f"reviewer(s) or unset `test_command`. (Both target "
                f"{self.worktree_base}/<run-id>/{TEST_WORKTREE_NAME}/ as their "
                f"worktree directory on case-insensitive filesystems; "
                f"the second one to provision would fail with a "
                f"generic WorktreeError after reviewer+synth cost "
                f"was already paid.)"
            )
        return self

    @model_validator(mode="after")
    def _validate_check_name_collisions(self) -> "SyncadeConfig":
        """configured checks provision per-round worktrees, so check
        names must be unique and must not collide with reviewer names or the
        reserved `tests` basename. Logic lives in `syncade.checks_config` to
        keep this over-cap module from growing."""
        validate_check_names([r.name for r in self.reviewers], [c.name for c in self.checks])
        return self
