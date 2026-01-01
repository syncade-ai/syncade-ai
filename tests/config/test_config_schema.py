"""Schema-level tests for :mod:`syncade.config` (core schema, thinking
enum / canonical-source drift, permissions, producer, loop max_rounds)."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from syncade.config import ReviewerConfig, SyncadeConfig


def test_default_config_matches_prd():
    # ProducerConfig describes the fresh subprocess producer, not the operator's
    # session. With no harness marker (the conftest neutral-harness default) the
    # producer takes the Codex shape; the harness matrix below covers the rest.
    cfg = SyncadeConfig()
    assert cfg.producer.provider == "openai"
    assert cfg.producer.model == "gpt-5.6-terra"
    assert cfg.producer.thinking == "medium"
    assert cfg.producer.permissions == "yolo"
    assert cfg.producer.timeout_seconds is None
    assert cfg.loop.max_rounds == 3
    assert cfg.loop.timeout_seconds == 1800
    assert cfg.review.include_producer_summary is False
    assert cfg.review.strip_repo_context_files == ["CLAUDE.md", "AGENTS.md"]


def test_default_anthropic_producer_model_is_current_sonnet_api_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s")
    cfg = SyncadeConfig()
    assert cfg.producer.provider == "anthropic"
    assert cfg.producer.model == "claude-sonnet-4-6"


def test_default_reviewers_are_two_codex():
    # Roster swapped 2026-06-30: Claude offlined, panel is now same-model /
    # cross-prompt (codex-reviewer + codex-reviewer-adv). Anthropic adapter and
    # template are retained for one-line revival; only the defaults changed.
    cfg = SyncadeConfig()
    assert len(cfg.reviewers) == 2
    assert cfg.reviewers[0].name == "codex-reviewer"
    assert cfg.reviewers[0].provider == "openai"
    # gpt-5.5, not gpt-5-codex: real `codex exec --model gpt-5-codex`
    # rejects with "not supported when using Codex with a ChatGPT
    # account" — see _default_reviewers's docstring for the rationale.
    # And gpt-5.5 rather than the judge's gpt-5.6-sol: PR-29 moved the panel back
    # after gpt-5.6-sol @ medium audited too leniently (~18 tool calls/round vs
    # 90-101; plain reviewer shipped 2/2 rounds at zero findings).
    assert cfg.reviewers[0].model == "gpt-5.5"
    assert cfg.reviewers[0].thinking == "high"
    assert cfg.reviewers[0].template is None
    assert cfg.reviewers[0].adversarial_lens is False
    assert cfg.reviewers[1].name == "codex-reviewer-adv"
    assert cfg.reviewers[1].provider == "openai"
    assert cfg.reviewers[1].model == "gpt-5.5"
    assert cfg.reviewers[1].thinking == "high"
    assert cfg.reviewers[1].template == "reviewer_adversarial.md"
    assert cfg.reviewers[1].adversarial_lens is True


# ---------------------------------------------------------------------------
# Harness matrix. Only the PRODUCER follows the invoking harness; the reviewers
# and the judge stay pinned to the cold OpenAI tier so a verdict is comparable
# across runs no matter which harness the operator happened to be coding in.
# ---------------------------------------------------------------------------

_HARNESS_ENV = {
    "claude-code": {"CLAUDE_CODE_SESSION_ID": "s"},
    "codex": {"CODEX_THREAD_ID": "t"},
    "both": {"CLAUDE_CODE_SESSION_ID": "s", "CODEX_THREAD_ID": "t"},
    "neither": {},
}


@pytest.mark.parametrize(
    ("harness", "provider", "model"),
    [
        # Claude Code → producer follows the harness you are coding in.
        ("claude-code", "anthropic", "claude-sonnet-4-6"),
        ("codex", "openai", "gpt-5.6-terra"),
        # Claude Code wins the tie: it is the more specific signal, and a Codex
        # thread id can linger in an exported environment.
        ("both", "anthropic", "claude-sonnet-4-6"),
        # No marker (plain terminal, CI, cron) → Codex shape. Safe side: the
        # reviewers and judge are OpenAI regardless, so codex auth is required
        # to run syncade at all; Anthropic auth is only implied by Claude Code.
        ("neither", "openai", "gpt-5.6-terra"),
    ],
)
def test_producer_default_follows_invoking_harness(monkeypatch, harness, provider, model):
    for key, value in _HARNESS_ENV[harness].items():
        monkeypatch.setenv(key, value)
    cfg = SyncadeConfig()
    assert cfg.producer.provider == provider
    assert cfg.producer.model == model
    assert cfg.producer.thinking == "medium"


@pytest.mark.parametrize("harness", list(_HARNESS_ENV))
def test_reviewers_and_judge_are_not_harness_aware(monkeypatch, harness):
    """The blind panel must not shift with the operator's toolchain — that would
    make two runs of the same diff incomparable depending on where they ran."""
    from syncade.synthesizer.constants import SYNTHESIZER_MODEL, SYNTHESIZER_THINKING

    for key, value in _HARNESS_ENV[harness].items():
        monkeypatch.setenv(key, value)
    cfg = SyncadeConfig()
    assert [(r.provider, r.model, r.thinking) for r in cfg.reviewers] == [
        ("openai", "gpt-5.5", "high"),
        ("openai", "gpt-5.5", "high"),
    ]
    assert SYNTHESIZER_MODEL == "gpt-5.5"
    assert SYNTHESIZER_THINKING == "high"


def test_explicit_producer_provider_repins_model_to_that_provider(monkeypatch):
    """``[producer] provider = "openai"`` with no model must not inherit the
    Claude Code harness's claude-sonnet-4-6 and hand it to ``codex exec`` — an
    unknown-model failure at dispatch that only reproduces on that harness."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s")
    assert SyncadeConfig(producer={"provider": "openai"}).producer.model == "gpt-5.6-terra"
    assert SyncadeConfig(producer={"provider": "anthropic"}).producer.model == "claude-sonnet-4-6"
    # an explicit model still wins
    assert (
        SyncadeConfig(producer={"provider": "openai", "model": "gpt-5-codex"}).producer.model
        == "gpt-5-codex"
    )


def test_explicit_empty_or_null_model_not_silently_replaced(monkeypatch):
    """An explicitly supplied model="" or model=None must not be silently
    overwritten by the provider default.  The validator must distinguish
    an absent key (re-derive) from an explicitly supplied value (preserve /
    reject via type-check)."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s")
    # empty string is an explicit (if malformed) value — must not be replaced
    from syncade.config import ProducerConfig

    p = ProducerConfig(**{"provider": "anthropic", "model": ""})
    assert p.model == "", f"empty model was silently replaced: {p.model!r}"
    # None is an explicit value — Pydantic's str type check must reject it,
    # not silently substitute the provider default
    with pytest.raises(ValidationError):
        SyncadeConfig(producer={"provider": "anthropic", "model": None})


def test_removed_pushback_policy_raises():
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"pushback_policy": "wrong"})
    assert "pushback_policy" in str(exc_info.value)


def test_invalid_thinking_value_raises():
    with pytest.raises(ValidationError):
        SyncadeConfig(producer={"thinking": "extreme"})


def test_thinking_xhigh_accepted_on_producer():
    """PR-8.5 Task 4 + R1.T2: ``xhigh`` is a reasoning-effort tier
    above ``high`` (verified live against codex 0.134.0 AND
    claude 2.1.152 — ``claude --help`` lists ``--effort`` accepting
    ``low | medium | high | xhigh | max``). The Thinking Literal
    accepts it at schema level; both adapter families route it
    verbatim — codex via ``-c model_reasoning_effort=xhigh`` and
    claude via ``--effort xhigh``. No per-adapter rejection.
    """
    cfg = SyncadeConfig(producer={"thinking": "xhigh"})
    assert cfg.producer.thinking == "xhigh"


def test_thinking_xhigh_accepted_on_reviewer():
    """PR-8.5 Task 4: same schema-level acceptance on reviewer
    configs. A reviewer with ``provider="openai"`` + ``thinking="xhigh"``
    is the load-bearing use case: operators who want codex's top
    reasoning tier."""
    cfg = SyncadeConfig(
        reviewers=[
            {
                "name": "codex-reviewer",
                "provider": "openai",
                "model": "gpt-5.5",
                "thinking": "xhigh",
            }
        ]
    )
    assert cfg.reviewers[0].thinking == "xhigh"


# ---------------------------------------------------------------------------
# PR-9 Task 3: canonical-source enum for ``thinking``.
#
# ``ReviewerConfig.thinking`` is the single authoritative enumeration of
# accepted thinking values. The PR-8.5 R0/R1 dogfoods each surfaced one
# more drift surface that a brief's grep missed (different phrasings:
# parentheticals, prose, code comments). The structural fix is no
# duplication — docs reference the field instead of restating the list.
#
# This test scans .py and .md files for the value-duplication patterns
# the dogfoods caught and asserts each surviving match is in an explicit
# allowlist (the canonical source file, test parametrize fixtures,
# CLI-behavior notes, historical PR briefs, and CLAUDE.md's
# type-signature restatement).
# ---------------------------------------------------------------------------

# Paths permitted to host the value-duplication patterns. Adding paths
# casually is the failure mode this test is designed to prevent —
# explicit, deliberate exemptions only.
_THINKING_DRIFT_ALLOWLIST: tuple[str, ...] = (
    # The canonical ``Thinking`` Literal lives here; the Literal
    # signature and its surrounding historical commentary are
    # intentionally enumerated. It moved from ``config.py`` to
    # ``config_types.py`` in PR-v2-23 (config.py was 462 LOC against a
    # blocking 500 cap and had no room for the judge/drafter blocks);
    # ``config.py`` re-exports it, so the public name is unchanged.
    "src/syncade/config_types.py",
    # Test files: ``@pytest.mark.parametrize("thinking", ["low",
    # "medium", "high", "xhigh"])`` is the load-bearing parametrize
    # form — every value is enumerated by design. Other test
    # docstrings may quote CLI help output verbatim for historical
    # context. This includes this very test file
    # (``tests/config/test_config_schema.py``), which restates the patterns it
    # scans for.
    "tests/",
    # Historical PR briefs and completion records. Past briefs and
    # commit notes may have enumerated values literally; that's
    # the historical record and shouldn't be retroactively edited.
    "your repo",
    # The architecture description in ``CLAUDE.md`` reproduces the
    # ``Thinking = Literal[...]`` type signature once as part of
    # the ``syncade.config`` section. The PR-9 Task 3 note in
    # ``CLAUDE.md`` itself flags this as an intentional exemption.
    "CLAUDE.md",
)

# Patterns the dogfoods historically caught as drift. Each pattern is
# a distinct phrasing the brief's grep would have missed:
#
# - Slash-separated parenthetical (``(low/medium/high)``) — PR-8.5 R1
#   adapter docstring nit (anthropic.py:89 + openai.py:163).
# - Pipe-separated raw or with-spaces (``low | medium | high``) —
#   PRD config example before PR-9 Task 3.
# - Quoted pipe-separated (``"low" | "medium" | "high"``) — README
#   config example before PR-9 Task 3.
# - Quoted comma-separated (``"low", "medium", "high"``) — the
#   Literal type-signature form. Outside the canonical source +
#   allowlist this is a drift.
_THINKING_DRIFT_PATTERNS: tuple[str, ...] = (
    r"\blow/medium/high\b",
    r"\blow\s*\|\s*medium\s*\|\s*high\b",
    r"\"low\"\s*\|\s*\"medium\"\s*\|\s*\"high\"",
    r"\"low\",\s*\"medium\",\s*\"high\"",
)

# Directory names skipped during the walk. Build/cache/venv content
# routinely contains source-file copies; scanning them is wasted I/O
# and may surface stale patterns from past commits.
_THINKING_DRIFT_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".venv",
        "venv",
        "env",
        ".git",
        "__pycache__",
        ".ruff_cache",
        ".pytest_cache",
        ".mypy_cache",
        ".tox",
        "build",
        "dist",
        # Per-repo syncade runtime artifacts (per-run worktrees and
        # round outputs); not source.
        ".syncade",
        # ``.claude/`` hosts the in-tree skill bridge (PR-9 Task 1).
        # The skill markdown is documentation; reviewing it for
        # enum duplications is the same surface as ``docs/`` and
        # belongs in the same scan. Don't skip.
    }
)


def _walk_py_and_md(repo_root: Path):
    """Yield (path, text) pairs for ``.py`` / ``.md`` files under
    ``repo_root``, skipping build/cache/venv directories.

    Walk-not-glob so we can prune skip-dirs as we go; on a fresh
    checkout the ``.venv`` alone would otherwise contribute tens of
    thousands of files to scan.
    """
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _THINKING_DRIFT_SKIP_DIRS for part in path.parts):
            continue
        if path.suffix not in (".py", ".md"):
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Unreadable / binary-by-mistake — skip rather than fail
            # the test on an environment issue.
            continue


def _is_thinking_drift_allowlisted(path: Path, repo_root: Path) -> bool:
    """Return ``True`` when ``path`` falls under an allowlisted
    prefix. Directory entries (trailing ``/``) match by path prefix;
    file entries match by exact relative path."""
    rel_str = str(path.relative_to(repo_root)).replace("\\", "/")
    for entry in _THINKING_DRIFT_ALLOWLIST:
        if entry.endswith("/"):
            if rel_str.startswith(entry):
                return True
        elif rel_str == entry:
            return True
    return False


def test_thinking_canonical_source_no_duplicate_enumerations():
    """PR-9 Task 3 drift defense: scan all .py and .md files for the
    value-list patterns the PR-8.5 R0/R1 dogfoods caught and confirm
    each surviving match is in an explicit allowlist file.

    To fix a failure: replace the duplicated value list at the named
    line with a reference to the canonical source — e.g.
    ``# see syncade.config.ReviewerConfig.thinking for accepted
    values``. Adding paths to ``_THINKING_DRIFT_ALLOWLIST`` is
    discouraged — the test exists specifically because every brief
    that has tried to enumerate the exemptions by grep has missed
    one. Loosening the allowlist defeats the purpose.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    drift_patterns = [re.compile(p) for p in _THINKING_DRIFT_PATTERNS]
    violations: list[str] = []
    for path, text in _walk_py_and_md(repo_root):
        if _is_thinking_drift_allowlisted(path, repo_root):
            continue
        for ln_idx, line in enumerate(text.splitlines(), start=1):
            for pattern in drift_patterns:
                if pattern.search(line):
                    rel = path.relative_to(repo_root)
                    snippet = line.strip()
                    if len(snippet) > 120:
                        snippet = snippet[:117] + "..."
                    violations.append(
                        f"  {rel}:{ln_idx}: matched {pattern.pattern!r}\n      {snippet}"
                    )
                    break
    if violations:
        joined = "\n".join(violations)
        raise AssertionError(
            "Thinking-enum drift detected. The following lines "
            "duplicate the syncade.config.ReviewerConfig.thinking "
            "enum's value list outside the allowlist. Replace each "
            "with a reference to the canonical source — e.g. "
            "`see syncade.config.ReviewerConfig.thinking for "
            "accepted values`. Do NOT add the offending file to "
            "_THINKING_DRIFT_ALLOWLIST unless you have a clear "
            "reason that survives review (and the canonical-source "
            "rule in CLAUDE.md's syncade.config section explains "
            f"the discipline).\n\n{joined}"
        )


def test_invalid_permissions_value_raises():
    with pytest.raises(ValidationError):
        SyncadeConfig(producer={"permissions": "unsafe"})


def test_reviewer_trusted_execute_accepted():
    """PR-17: ``trusted-execute`` parses cleanly as a reviewer
    permission — the provider-symmetric "no prompts, full worktree
    access" mode (Anthropic ``bypassPermissions`` / Codex unchanged
    ``-s workspace-write -c approval_policy=never``)."""
    cfg = SyncadeConfig(
        reviewers=[
            {
                "name": "r",
                "provider": "anthropic",
                "model": "m",
                "permissions": "trusted-execute",
            }
        ]
    )
    assert cfg.reviewers[0].permissions == "trusted-execute"


def test_reviewer_adversarial_lens_defaults_false_and_accepts_true():
    """Per-reviewer adversarial-edge-enumeration lens (claude-reviewer
    confirmatory-review fix). Opt-in boolean, default ``False`` so codex (the
    control) and every existing config are unchanged; set ``True`` only on the
    reviewer being pushed adversarial."""
    rc = ReviewerConfig(name="r", provider="anthropic", model="m")
    assert rc.adversarial_lens is False
    rc_on = ReviewerConfig(name="r", provider="anthropic", model="m", adversarial_lens=True)
    assert rc_on.adversarial_lens is True


def test_reviewer_unknown_permission_value_rejected():
    """The reviewer ``Permissions`` Literal stays a closed set after
    PR-17: a typo'd / unknown permission value (here a near-miss of
    ``trusted-execute``) is rejected at config-load. ``safe`` itself
    remains schema-valid for reviewers — it's the adapter
    ``_validate_permissions`` that refuses it for headless dispatch,
    pinned by the adapter test suites."""
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(
            reviewers=[
                {
                    "name": "r",
                    "provider": "anthropic",
                    "model": "m",
                    "permissions": "trusted-exec",
                }
            ]
        )
    assert "permissions" in str(exc_info.value)


def test_producer_permissions_safe_rejected():
    """Producer permissions accept only ``yolo``.

    The corresponding
    ``--permission-mode default`` (claude) and
    ``approval_policy=untrusted`` (codex) modes prompt for every
    tool use, and the producer is headless, so the subprocess
    would hang until the timeout fires. Schema-level rejection
    surfaces the misconfiguration at config-load."""
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(producer={"permissions": "safe"})
    assert "permissions" in str(exc_info.value)


def test_producer_permissions_invalid_value_lists_valid_choices():
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(producer={"permissions": "trusted-execute"})
    msg = str(exc_info.value)
    assert "permissions" in msg
    assert "yolo" in msg


def test_producer_permissions_trusted_rejected():
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(producer={"permissions": "trusted"})
    assert "permissions" in str(exc_info.value)


def test_reviewer_permissions_trusted_rejected():
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(
            reviewers=[
                {
                    "name": "r",
                    "provider": "anthropic",
                    "model": "m",
                    "permissions": "trusted",
                }
            ]
        )
    assert "permissions" in str(exc_info.value)


def test_producer_provider_anthropic_accepted():
    cfg = SyncadeConfig(producer={"provider": "anthropic"})
    assert cfg.producer.provider == "anthropic"


def test_producer_provider_openai_accepted():
    """PR-8: codex-as-producer is the documented alternative for
    operators with codex-favorable auth or preference. Same
    provider vocabulary as ``[[reviewers]]`` so the adapter
    registry can map both."""
    cfg = SyncadeConfig(producer={"provider": "openai", "model": "gpt-5-codex"})
    assert cfg.producer.provider == "openai"
    assert cfg.producer.model == "gpt-5-codex"


def test_producer_provider_unknown_rejected():
    """PR-8: ``ProducerProvider = Literal["anthropic", "openai"]``
    — extending to a third provider lands by extending one
    registry, not by relaxing this Literal."""
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(producer={"provider": "google"})
    assert "provider" in str(exc_info.value)


def test_producer_timeout_seconds_unset_is_none():
    """The unset / None sentinel means 'reuse reviewer timeout' —
    the orchestrator does that resolution, keeping the field
    nullable so 'use the default' is distinguishable from
    'explicitly set to the same value'."""
    cfg = SyncadeConfig()
    assert cfg.producer.timeout_seconds is None


def test_producer_timeout_seconds_explicit_round_trips():
    cfg = SyncadeConfig(producer={"timeout_seconds": 600.0})
    assert cfg.producer.timeout_seconds == 600.0


def test_producer_timeout_seconds_zero_rejected():
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(producer={"timeout_seconds": 0})
    assert "timeout_seconds" in str(exc_info.value)


def test_producer_timeout_seconds_negative_rejected():
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(producer={"timeout_seconds": -1})
    assert "timeout_seconds" in str(exc_info.value)


def test_producer_timeout_seconds_nan_rejected():
    """Same ``math.isfinite`` guard PR-5.5 applies to
    :attr:`LoopConfig.timeout_seconds` and PR-7.5's T1.5 reapplied
    to :attr:`LoopConfig.test_timeout_seconds`. NaN's ``gt=0``
    behavior is pydantic-version-dependent — without this
    validator a runaway operator config could produce an
    unkillable producer subprocess that hangs the loop."""
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(producer={"timeout_seconds": float("nan")})
    assert "timeout_seconds" in str(exc_info.value)


def test_producer_timeout_seconds_inf_rejected():
    """``inf > 0`` is True so ``gt=0`` admits infinity unaided.
    The validator rejects it so an operator config can't produce
    a producer subprocess that reads timeout=inf as 'wait
    forever'."""
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(producer={"timeout_seconds": float("inf")})
    assert "timeout_seconds" in str(exc_info.value)


# --- PR-v2-11: [loop] budget_tokens / budget_usd ---------------------------------


def test_budget_unset_is_none():
    """No budget by default — max_rounds + timeout stay the only bounds (byte-identical to
    pre-PR-v2-11)."""
    cfg = SyncadeConfig()
    assert cfg.loop.budget_tokens is None
    assert cfg.loop.budget_usd is None


def test_budget_explicit_round_trips():
    cfg = SyncadeConfig(loop={"budget_tokens": 500_000, "budget_usd": 2.5})
    assert cfg.loop.budget_tokens == 500_000
    assert cfg.loop.budget_usd == 2.5


@pytest.mark.parametrize("value", [0, -1])
def test_budget_tokens_non_positive_rejected(value):
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"budget_tokens": value})
    assert "budget_tokens" in str(exc_info.value)


@pytest.mark.parametrize("value", [0, -1.0, float("nan"), float("inf")])
def test_budget_usd_invalid_rejected(value):
    # gt=0 rejects 0/negatives; the field_validator rejects NaN/inf (which gt=0 admits and
    # would silently mean "no ceiling").
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"budget_usd": value})
    assert "budget_usd" in str(exc_info.value)


def test_producer_section_absent_yields_defaults(tmp_path):
    """An empty / absent ``[producer]`` section in the TOML file
    instantiates :class:`ProducerConfig` with all defaults. PR-8
    requirement so existing config files don't need updating
    just because the producer schema changed shape."""
    from syncade.config_loader import load_config

    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        "[loop]\nmax_rounds = 2\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    # producer section absent → all defaults (neutral harness → Codex shape)
    assert cfg.producer.provider == "openai"
    assert cfg.producer.model == "gpt-5.6-terra"
    assert cfg.producer.thinking == "medium"
    assert cfg.producer.permissions == "yolo"
    assert cfg.producer.timeout_seconds is None
    # the loop override took effect
    assert cfg.loop.max_rounds == 2


def test_unknown_top_level_field_raises():
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(unknown_field=1)
    assert "unknown_field" in str(exc_info.value)


def test_unknown_nested_field_raises():
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(producer={"typo_field": "x"})
    assert "typo_field" in str(exc_info.value)


def test_reviewers_list_validates_and_applies_defaults():
    cfg = SyncadeConfig(
        reviewers=[
            {"name": "gemini", "provider": "google", "model": "gemini-pro"},
        ]
    )
    assert len(cfg.reviewers) == 1
    assert cfg.reviewers[0].name == "gemini"
    assert cfg.reviewers[0].provider == "google"
    assert cfg.reviewers[0].thinking == "high"  # default
    # trusted-execute, not yolo: still fully unattended (approval_policy=never /
    # bypassPermissions, neither prompts) but keeps the OS sandbox scoped to the
    # reviewer's worktree, so confinement is structural rather than prompt-based.
    assert cfg.reviewers[0].permissions == "trusted-execute"  # default


def test_reviewer_missing_required_field_raises():
    with pytest.raises(ValidationError):
        ReviewerConfig(name="incomplete", provider="anthropic")  # no model


def test_duplicate_reviewer_names_rejected():
    """L1: reviewer ``name`` is the dedup key in synthesis cross-input
    validation (``synthesizer.validation`` indexes finding counts and
    unanimous-blocker provenance by ``reviewer_name``) and the
    per-reviewer worktree/prompt key in dispatch. Two reviewers sharing a
    name would collapse to one key and silently undercount a unanimous
    blocker, so config-load rejects duplicates with a clear error that
    names the offender."""
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(
            reviewers=[
                {"name": "dup", "provider": "anthropic", "model": "m1"},
                {"name": "dup", "provider": "openai", "model": "m2"},
            ]
        )
    msg = str(exc_info.value)
    assert "dup" in msg
    assert "unique" in msg


def test_distinct_reviewer_names_accepted():
    """The uniqueness validator must not false-positive on the normal
    multi-reviewer config — distinct names round-trip unchanged."""
    cfg = SyncadeConfig(
        reviewers=[
            {"name": "r1", "provider": "anthropic", "model": "m1"},
            {"name": "r2", "provider": "openai", "model": "m2"},
        ]
    )
    assert [r.name for r in cfg.reviewers] == ["r1", "r2"]


def test_loop_max_rounds_two_accepted():
    """PR-8 boundary: 2 is the typical one-fix-attempt config."""
    cfg = SyncadeConfig(loop={"max_rounds": 2})
    assert cfg.loop.max_rounds == 2


def test_loop_max_rounds_three_accepted_boundary():
    """PR-8 boundary: 3 is the PRD cap (Appendix C). ``le=3`` accepts
    it; anything above is rejected."""
    cfg = SyncadeConfig(loop={"max_rounds": 3})
    assert cfg.loop.max_rounds == 3


def test_loop_max_rounds_one_is_valid_back_compat_floor():
    """PR-8 boundary: 1 is the single-pass back-compat escape hatch
    that recovers PR-7.5's exact code path (no producer subprocess
    instantiated). The pinning regression ``TestTestReRunActive``
    stays green under this setting."""
    cfg = SyncadeConfig(loop={"max_rounds": 1})
    assert cfg.loop.max_rounds == 1


def test_loop_max_rounds_zero_raises():
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"max_rounds": 0})
    assert "max_rounds" in str(exc_info.value)


def test_loop_max_rounds_negative_raises():
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"max_rounds": -1})
    assert "max_rounds" in str(exc_info.value)


def test_reviewer_template_defaults_none_and_accepts_basename():
    rc = ReviewerConfig(name="r", provider="openai", model="m")
    assert rc.template is None
    rc2 = ReviewerConfig(name="r", provider="openai", model="m", template="reviewer_adversarial.md")
    assert rc2.template == "reviewer_adversarial.md"


@pytest.mark.parametrize("bad", ["", "../x.md", "a/b.md", "sub\\x.md", "..", ".", "/abs.md"])
def test_reviewer_template_rejects_non_basename(bad):
    with pytest.raises(ValueError):
        ReviewerConfig(name="r", provider="openai", model="m", template=bad)


@pytest.mark.parametrize("ok", ["foo..md", "..hidden.md", "a..b..c.md"])
def test_reviewer_template_accepts_dotdot_within_filename(ok):
    # Only a BARE `.`/`..` traverses; `..` inside a filename is a safe basename
    # and must be accepted (exact-match guard mirrors load_template).
    rc = ReviewerConfig(name="r", provider="openai", model="m", template=ok)
    assert rc.template == ok
