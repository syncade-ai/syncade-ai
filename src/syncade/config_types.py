"""Enum-like Literals shared by every config module.

Lives alone so ``config.py`` and ``config_producer.py`` can both import them without a
cycle (``config`` re-exports ``config_producer``'s names, so the dependency must run one
way only). Any value not listed here fails validation — which is the point.
"""

from __future__ import annotations

from typing import Literal

Thinking = Literal["low", "medium", "high", "xhigh", "max"]
"""Reasoning effort budget passed to the underlying model adapter.

``"xhigh"`` is the higher reasoning tier above
``"high"``. BOTH providers accept it at the CLI level:

- Codex via ``-c model_reasoning_effort=xhigh`` (verified live
  against codex 0.134.0; the operator's own
  ``~/.codex/config.toml`` uses xhigh as the default).
- Claude via ``--effort xhigh`` (verified live against claude
  2.1.152, where ``claude --help`` lists ``--effort`` accepting
  ``low | medium | high | xhigh | max``).

All four adapters (:class:`~syncade.adapters.openai.CodexAdapter`,
:class:`~syncade.adapters.producer_openai.OpenAIProducerAdapter`,
:class:`~syncade.adapters.anthropic.AnthropicAdapter`,
:class:`~syncade.adapters.producer_anthropic.AnthropicProducerAdapter`)
pass the value through verbatim — no per-provider rejection. The
brief's initial claim that ``claude --effort`` only supported
``low/medium/high`` was unverified speculation that the first
validation codex-reviewer caught.

``"max"`` was added when Claude Opus 4.8 dropped (2026-05-28).
Claude CLI lists it as the highest tier (above ``xhigh``); Codex's
``-c model_reasoning_effort=max`` is accepted as a pass-through
(verified live). Adapters pass the value through verbatim; no
per-provider mapping update needed.
"""

Permissions = Literal["safe", "trusted-execute", "yolo"]
"""Tool-permission tier for an agent process. ``yolo`` grants full
auto-approval; ``safe`` requires explicit user approval for every action.

``trusted-execute`` is the provider-symmetric "no prompts, full
worktree access" reviewer mode: it maps to ``bypassPermissions`` on
Anthropic and the unchanged ``-s workspace-write -c approval_policy=never``
on Codex. The old ``trusted`` value was provider-asymmetric and is no longer
accepted."""

ReviewerPermissions = Literal["trusted-execute", "yolo"]
"""Reviewer permission tiers — the runnable subset of :data:`Permissions`. ``safe`` is excluded:
the real reviewer adapters refuse it (it prompts, so it hangs a headless subprocess), so it is
rejected at config-load rather than only at dispatch — mirroring
:data:`~syncade.config_producer.ProducerPermissions`. The adapters keep their own ``safe`` guard as
belt-and-braces for callers that bypass the schema (e.g. ``model_construct``)."""

# LoopConfig moved to config_loop; it is imported above for
# SyncadeConfig's ``loop`` field.
