"""``syncade --auth-check`` — verify configured CREDENTIAL authentication.

Probes one representative per DISTINCT CREDENTIAL (provider + resolved mode + key), not
merely per provider (PR-v2-24): two actors on the same provider but different credentials
are probed separately, so the check cannot pass on auth it never exercised.

Fast pre-flight diagnostic for "did my OAuth token rotate?" /
"is my provider auth healthy?" — the common operator
question that :func:`syncade.selfcheck.run_selfcheck` answers via a
~30s producer-commit smoke. The auth-check is the cheaper sibling:
~5-10 seconds end-to-end, probes the auth path only (no producer
round, no worktree, no findings.md).

For each unique credential in the config (``[[reviewers]]`` blocks
plus ``[producer]``, deduped by the credential each actor presents),
the auth-check runs the provider's documented cheap auth probe:

- **anthropic:** ``claude -p "respond with exactly: AUTH OK"
  --output-format json --model <model>``. Success iff
  ``is_error == false`` in the JSON envelope AND ``"AUTH OK"`` is
  ``in`` the ``.result`` text. The 30-second timeout is generous;
  healthy auth responds in well under 5s.
- **openai:** ``codex login status`` via the existing
  :meth:`syncade.adapters.openai.CodexAdapter.check_auth` — a
  filesystem check (no network call) the adapter already supports
  for the dispatcher's pre-flight phase.

The probe message ``"AUTH OK"`` is matched with ``in`` (not ``==``)
so a model that wraps the sentinel in additional text — common when
the model is overly chatty — still passes.

This module is NOT part of the review loop. ``syncade <pr-doc>``
does not call it; selfcheck does not call it; it's a standalone
operator diagnostic surfaced via ``--auth-check`` and intended to
be cheap enough to run as a habit before invoking syncade.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from syncade.adapters.base import ReviewerInvocationError
from syncade.config import SyncadeConfig
from syncade.config_auth import apply_auth_to_env, credential_fingerprint
from syncade.exit_codes import SUCCESS, WORKTREE_ERROR
from syncade.process import (
    SubprocessError,
    SubprocessNotFoundError,
    SubprocessTimeoutError,
    run_subprocess,
)

DEFAULT_AUTH_CHECK_TIMEOUT_SECONDS: float = 30.0
"""Per-provider probe timeout. Generous — healthy auth typically
responds in well under 5s; the 30s ceiling tolerates a slow network
or a sluggish provider without false-negative failures."""

AUTH_CHECK_PROBE_PROMPT: str = "respond with exactly: AUTH OK"
"""The probe message sent to the anthropic adapter's ``claude -p``
call. The model is expected to echo ``AUTH OK`` somewhere in its
response — the ``in`` check (not ``==``) tolerates provider-side
wrapping (e.g. an over-eager model that prefixes "Sure: AUTH OK")."""

AUTH_CHECK_SENTINEL: str = "AUTH OK"
"""The sentinel string the probe expects in the model's response.
Module constant so the probe instruction text and the verifier stay
in lockstep — a refactor that changes the prompt's sentinel but not
the verifier (or vice versa) would silently break auth-check."""


@dataclass(frozen=True)
class AuthCheckResult:
    """Per-provider auth-check outcome.

    Attributes:
        provider: The provider name (``"anthropic"`` / ``"openai"``).
        model: The representative model used for the probe — ``None``
            for providers whose probe doesn't pin a model (codex's
            ``codex login status`` is filesystem-level and doesn't
            need one).
        ok: ``True`` iff the probe succeeded and the sentinel was
            present.
        duration_seconds: Wall-clock probe duration.
        detail: Operator-facing summary — either the ``"OK (4.2s)"``
            success form or a remediation-bearing failure message
            (e.g. "401 — token expired. Run 'claude' interactively
            to re-authenticate."). Surfaced verbatim in stderr on
            failure paths.
    """

    provider: str
    model: str | None
    ok: bool
    duration_seconds: float
    detail: str


# Type alias for a provider-probe callable. Tests inject fakes by
# passing a dict of these to :func:`run_auth_check`'s ``probes``
# kwarg. Production routing goes through :data:`_DEFAULT_PROBES`.
ProviderProbe = Callable[..., AuthCheckResult]
"""``(model, timeout_seconds, env=...) -> AuthCheckResult``.

``env`` is the AUTH-APPLIED environment, not the parent's. Without it the probe
inherited ``ANTHROPIC_API_KEY`` and hit the API even when every anthropic actor declared
``auth = "subscription"`` -- so the check could BILL the API account it was supposed to
be keeping the user away from, and green-light a credential the actual run would never
use. Caught by syncade's own panel."""


def _probe_anthropic(
    model: str, timeout_seconds: float, env: dict[str, str] | None = None
) -> AuthCheckResult:
    """Probe anthropic auth via ``claude -p`` with the sentinel prompt.

    Build the argv directly rather than going through
    :class:`~syncade.adapters.anthropic.AnthropicAdapter` because the
    auth-check probe doesn't need the adapter's full reviewer-
    invocation argv (``--effort``, ``--permission-mode``,
    ``--add-dir``). Keep it minimal: ``claude -p <prompt>
    --output-format json --model <model>``. Same JSON envelope shape
    as the adapter, so the parse logic reuses the same
    ``is_error`` / ``.result`` fields.

    Failure modes:

    - ``SubprocessNotFoundError`` → claude CLI missing.
    - ``SubprocessTimeoutError`` → probe didn't return in time.
      Typically a hung auth helper or a wedged network.
    - Non-JSON stdout → CLI emitted something other than the
      documented envelope. May indicate a CLI version mismatch.
    - ``is_error: true`` envelope → token issue (401, model
      unavailable, etc.). The envelope's ``.result`` carries the
      provider-readable error; surface it verbatim.
    - Envelope OK but sentinel absent → the model responded but
      didn't follow the instruction. Unlikely auth issue; surfaced
      as a separate failure so the operator can rerun.
    """
    start = time.monotonic()
    argv = [
        "claude",
        "-p",
        AUTH_CHECK_PROBE_PROMPT,
        "--output-format",
        "json",
        "--model",
        model,
    ]
    try:
        result = run_subprocess(argv, timeout=timeout_seconds, env=env)
    except SubprocessNotFoundError:
        return AuthCheckResult(
            provider="anthropic",
            model=model,
            ok=False,
            duration_seconds=time.monotonic() - start,
            detail=(
                "claude binary not found on PATH — install the Anthropic "
                "claude CLI to run auth-check."
            ),
        )
    except SubprocessTimeoutError as exc:
        return AuthCheckResult(
            provider="anthropic",
            model=model,
            ok=False,
            duration_seconds=time.monotonic() - start,
            detail=(
                f"claude auth probe timed out after {exc.timeout:g}s — auth "
                "helper or network may be hung. Run 'claude' interactively "
                "to re-authenticate."
            ),
        )
    except SubprocessError as exc:
        return AuthCheckResult(
            provider="anthropic",
            model=model,
            ok=False,
            duration_seconds=time.monotonic() - start,
            detail=(
                f"claude subprocess launch failed: {exc}. Check that the "
                "claude binary is executable and accessible on PATH."
            ),
        )

    duration = time.monotonic() - start

    envelope: dict | None = None
    try:
        parsed = json.loads(result.stdout)
        if isinstance(parsed, dict):
            envelope = parsed
    except json.JSONDecodeError:
        envelope = None

    if envelope is None:
        stderr_snippet = result.stderr.strip()[:200]
        stdout_snippet = result.stdout.strip()[:200]
        tail = stderr_snippet or stdout_snippet or "(no output)"
        return AuthCheckResult(
            provider="anthropic",
            model=model,
            ok=False,
            duration_seconds=duration,
            detail=(
                f"claude returned non-JSON stdout "
                f"(rc={result.returncode}); output tail: {tail!r}. "
                "May indicate a claude CLI version mismatch."
            ),
        )

    is_error = bool(envelope.get("is_error", False))
    api_error_status = envelope.get("api_error_status")
    envelope_result = envelope.get("result")

    if is_error or result.returncode != 0:
        msg = (
            envelope_result
            if isinstance(envelope_result, str) and envelope_result
            else f"is_error={is_error}, api_error_status={api_error_status}"
        )
        return AuthCheckResult(
            provider="anthropic",
            model=model,
            ok=False,
            duration_seconds=duration,
            detail=(
                f"FAILED (rc={result.returncode}, "
                f"api_error_status={api_error_status}): {str(msg)[:200]}. "
                "Run 'claude' interactively to re-authenticate."
            ),
        )

    if not isinstance(envelope_result, str) or AUTH_CHECK_SENTINEL not in envelope_result:
        excerpt = (
            envelope_result[:120]
            if isinstance(envelope_result, str)
            else str(envelope_result)[:120]
        )
        return AuthCheckResult(
            provider="anthropic",
            model=model,
            ok=False,
            duration_seconds=duration,
            detail=(
                f"claude responded without {AUTH_CHECK_SENTINEL!r} sentinel "
                f"({excerpt!r}). Auth appears OK but the model didn't follow "
                "the instruction; retry once before treating this as a real "
                "failure."
            ),
        )

    return AuthCheckResult(
        provider="anthropic",
        model=model,
        ok=True,
        duration_seconds=duration,
        detail=f"OK ({duration:.1f}s)",
    )


def _probe_openai(
    model: str, timeout_seconds: float, env: dict[str, str] | None = None
) -> AuthCheckResult:  # noqa: ARG001
    """Probe codex auth via ``codex login status``.

    Delegates to
    :meth:`syncade.adapters.openai.CodexAdapter.check_auth`, which
    already implements the documented `codex login status` probe
    with the right error mapping. The model is recorded for
    operator-facing reporting but not used by the probe — codex
    login state is per-token, not per-model.

    ``timeout_seconds`` is part of the :data:`ProviderProbe` protocol
    signature but is intentionally unused: ``CodexAdapter.check_auth``
    has its own internal timeout via ``_CODEX_AUTH_CHECK_TIMEOUT_SECONDS``
    (10s by default). The parameter is kept on the public surface to
    conform to the probe-protocol shape so callers using
    ``timeout_seconds=`` as a keyword (including tests) keep working.
    The ``# noqa: ARG001`` annotation marks the intentional non-use for ruff's
    unused-arg linter while keeping the public keyword name stable.

    The local import sidesteps a circular-import concern (the adapters
    module's tests import from auth_check; importing CodexAdapter at
    module-load would tie this module to the adapters layer).
    """
    from syncade.adapters.openai import CodexAdapter

    start = time.monotonic()
    adapter = CodexAdapter()
    try:
        # CodexAdapter.check_auth already raises ReviewerInvocationError
        # on every failure path (binary missing, timeout, non-zero rc
        # from `codex login status`) with operator-facing messages.
        # Take its message verbatim — the codex CLI's own output is
        # exactly what the user needs to see.
        adapter.check_auth()
    except ReviewerInvocationError as exc:
        return AuthCheckResult(
            provider="openai",
            model=model,
            ok=False,
            duration_seconds=time.monotonic() - start,
            detail=str(exc)[:300],
        )
    duration = time.monotonic() - start
    return AuthCheckResult(
        provider="openai",
        model=model,
        ok=True,
        duration_seconds=duration,
        detail=f"OK ({duration:.1f}s)",
    )


_DEFAULT_PROBES: dict[str, ProviderProbe] = {
    "anthropic": _probe_anthropic,
    "openai": _probe_openai,
}
"""Mapping from provider name to its real auth probe. Tests override
by passing a different dict to :func:`run_auth_check`'s ``probes``
kwarg; production routes through this default."""


def _collect_unique_providers(config: SyncadeConfig) -> list[tuple[str, str]]:
    """Return ``[(provider, representative_model), ...]`` in config
    order with duplicates removed (first occurrence wins).

    Iterates reviewers in config order, then the producer, then the three COLD
    actors (judge / drafter / auditor).

    **The cold actors are why this list exists at all now.** Before PR-v2-23 they
    were hardwired to codex and invisible to config, so this probed reviewers +
    producer and returned exit 0 on a machine that *could not finish a run* — a
    false green, and the most expensive kind, because the user only discovers it
    after both reviewers have run and billed. Now that the judge's provider is
    configurable, an `[synthesizer] provider = "anthropic"` with OpenAI reviewers
    is a perfectly reasonable config that this check MUST cover.

    Cost is nothing in practice: dedup is by CREDENTIAL (see :func:`_by_credential`),
    so an actor presenting a credential already covered adds no probe. The default
    roster is single-credential, so the cold actors add ZERO probes there. They add
    one only when the operator actually configured a different credential — precisely
    when you want to know it works. (Same provider, DIFFERENT credential still probes:
    that is the point — a second credential is a second thing that can fail to auth.)

    A "representative" model is sufficient because auth is
    per-token, not per-model; ``claude -p --model claude-opus-4-6``
    and ``claude -p --model claude-sonnet-4-6`` succeed or fail on
    the same OAuth token. Probing one model per credential keeps
    the auth-check fast (the brief's ~5-10s target) without losing
    the diagnostic value.
    """
    return [(provider, model) for (provider, _cred), (model, _actor) in _by_credential(config)]


def _credential_key(actor: object) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """(provider, resolved mode, credential fingerprint). The fingerprint READS the actual
    credential from the enforced env -- see credential_fingerprint. Two credentials that
    resolve to the same mode but present different keys are distinct probes."""
    from syncade.auth_preflight import resolve_auth_mode

    provider = str(getattr(actor, "provider", ""))
    mode = resolve_auth_mode(actor, dict(os.environ))  # type: ignore[arg-type]
    return (provider, mode, credential_fingerprint(actor, dict(os.environ)))  # type: ignore[arg-type]


def _by_credential(config: SyncadeConfig) -> list[tuple[tuple[str, str], tuple[str, object]]]:
    """One representative actor per DISTINCT CREDENTIAL, in config order."""
    seen: dict[tuple[str, str, str], tuple[str, object]] = {}
    actors: list[object] = [
        *config.reviewers,
        config.producer,
        config.synthesizer,
        config.drafter,
        config.auditor,
    ]
    for actor in actors:
        key = _credential_key(actor)
        if key not in seen:
            seen[key] = (str(getattr(actor, "model", "")), actor)
    return [((k[0], f"{k[1]}:{[v for v, _h in k[2]]}"), v) for k, v in seen.items()]


def _provider_actors(config: SyncadeConfig) -> dict[str, object]:
    """One representative actor per provider (first wins). Kept for the probe-env lookup;
    the PROBE LIST itself is keyed by credential — see :func:`_by_credential`."""
    seen: dict[str, object] = {}
    for reviewer in config.reviewers:
        seen.setdefault(reviewer.provider, reviewer)
    for actor in (config.producer, config.synthesizer, config.drafter, config.auditor):
        seen.setdefault(actor.provider, actor)
    return seen


def probe_credentials(
    config: SyncadeConfig,
    *,
    timeout_seconds: float | None = None,
    probes: dict[str, ProviderProbe] | None = None,
):
    """Yield one :class:`AuthCheckResult` per DISTINCT CREDENTIAL, in config order.

    The pure probe core shared by :func:`run_auth_check` (which prints each result and maps
    them to an exit code) and ``syncade --doctor`` (which renders them as table rows). No
    printing, no exit code — just results. A generator so a caller can stream them as each
    sequential probe lands. Never raises: an unknown provider, an ``api`` declaration with
    no key, and a probe that itself throws all become a failed result, because a diagnostic
    must report, not traceback.

    Probing carries each credential's OWN actor through to the env (via
    :func:`apply_auth_to_env`), not the provider's first actor — the wiring bug the panel
    caught: a second ``api`` credential on a provider was probed under the first actor's env,
    verifying a credential the real run would never use.
    """
    if probes is None:
        probes = _DEFAULT_PROBES
    if timeout_seconds is None:
        timeout_seconds = DEFAULT_AUTH_CHECK_TIMEOUT_SECONDS

    for (provider, _cred), (model, actor) in _by_credential(config):
        probe = probes.get(provider)
        if probe is None:
            yield AuthCheckResult(
                provider=provider,
                model=model,
                ok=False,
                duration_seconds=0.0,
                detail=f"no probe registered for provider {provider!r}. Known: {sorted(probes)!r}.",
            )
            continue
        try:
            probe_env = apply_auth_to_env(dict(os.environ), actor)  # type: ignore[arg-type]
        except ValueError as exc:
            # `api` declared with no key: degrade gracefully, never traceback.
            yield AuthCheckResult(
                provider=provider, model=model, ok=False, duration_seconds=0.0, detail=str(exc)
            )
            continue
        try:
            yield probe(model, timeout_seconds, env=probe_env)
        except Exception as exc:
            yield AuthCheckResult(
                provider=provider,
                model=model,
                ok=False,
                duration_seconds=0.0,
                detail=f"probe raised an unexpected exception: {exc!r}",
            )


def run_auth_check(
    config: SyncadeConfig,
    *,
    timeout_seconds: float | None = None,
    probes: dict[str, ProviderProbe] | None = None,
    quiet: bool = False,
) -> int:
    """Run the auth-check across all configured credentials and return
    a CLI exit code.

    Steps:

    1. Collect the unique credentials from ``[[reviewers]]`` +
       ``[producer]`` via :func:`_collect_unique_providers`.
    2. Run each credential's probe (real probe from
       :data:`_DEFAULT_PROBES` unless ``probes`` overrides). Probes
       run sequentially — total wall-clock is the sum of per-probe
       durations. With two providers (anthropic + openai) and
       healthy auth, the brief targets ~5-10s.
    3. Report each result as it lands (so a slow probe doesn't
       silence early results).
    4. Return :data:`syncade.exit_codes.SUCCESS` (0) if every probe
       succeeded; :data:`~syncade.exit_codes.WORKTREE_ERROR` (60) if
       any failed. Same exit-60 semantics as ``--selfcheck`` — "your
       environment isn't ready for a real run".

    Args:
        config: The operator's loaded :class:`SyncadeConfig`.
        timeout_seconds: Optional per-probe timeout override
            (typically from ``--timeout``). ``None`` (default) uses
            :data:`DEFAULT_AUTH_CHECK_TIMEOUT_SECONDS` (30s).
        probes: Optional override of the probe registry, used by
            tests to inject fakes. Keys are provider names matching
            ``ReviewerConfig.provider`` / ``ProducerConfig.provider``;
            values are :data:`ProviderProbe` callables. ``None``
            (default) routes through :data:`_DEFAULT_PROBES`.
        quiet: Suppress informational stdout. Stderr (failure paths)
            unaffected. Wired from ``--quiet``.

    Returns:
        ``SUCCESS`` (0) when every probe succeeded.

        ``WORKTREE_ERROR`` (60) when any probe failed, when an
        unknown provider is configured (no probe registered), or
        when the config has zero providers (degenerate case).

    Raises:
        Never. All failure modes map to a ``WORKTREE_ERROR`` return.
    """
    if probes is None:
        probes = _DEFAULT_PROBES
    if timeout_seconds is None:
        timeout_seconds = DEFAULT_AUTH_CHECK_TIMEOUT_SECONDS

    def _info(msg: str) -> None:
        if not quiet:
            print(msg)

    def _err(msg: str) -> None:
        print(msg, file=sys.stderr)

    providers = _collect_unique_providers(config)
    if not providers:
        _err(
            "[syncade] auth-check FAILED: config has zero providers — "
            "ensure at least one [[reviewers]] block or [producer] is configured."
        )
        return WORKTREE_ERROR

    _info(f"[syncade] auth-check: probing {len(providers)} credential(s)")

    results: list[AuthCheckResult] = []
    any_failed = False
    # The per-credential probe core lives in probe_credentials() so syncade --doctor can
    # reuse the exact same wiring (carrying each credential's own actor through to the env)
    # rather than reimplementing it and re-introducing the "wrong actor's env" bug. This
    # loop just prints each result as it lands and tracks failure for the exit code.
    for result in probe_credentials(config, timeout_seconds=timeout_seconds, probes=probes):
        results.append(result)
        if result.ok:
            _info(f"[auth-check] {result.provider}: {result.detail}")
        else:
            any_failed = True
            _err(f"[auth-check] {result.provider}: {result.detail}")

    if any_failed:
        _err(
            "[syncade] auth-check FAILED: one or more credentials could not "
            "authenticate. See the per-credential detail above; re-authenticate "
            "before invoking syncade for a real review."
        )
        return WORKTREE_ERROR

    total_duration = sum(r.duration_seconds for r in results)
    _info(
        f"[syncade] auth-check OK: {len(results)} credential(s) verified in {total_duration:.1f}s"
    )
    return SUCCESS
