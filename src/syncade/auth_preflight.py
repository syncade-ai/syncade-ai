"""Resolve what auth EACH actor will really use, and refuse when reality contradicts it.

Declaring a mode is worthless if syncade cannot tell whether it took effect. The two
CLIs are enforceable to completely different degrees, and the difference is not a
detail — it is the whole design:

**anthropic / ``claude`` — the env IS the lever.** Verified live (2.1.208): an exported
``ANTHROPIC_API_KEY`` beats the claude.ai login (a bogus one 401s rather than falling
back), and stripping it drops claude onto OAuth. So
:func:`~syncade.config_auth.apply_auth_to_env` *enforces* the declaration outright, and
the resolved mode is a pure function of the env we hand over. No probe needed.

**openai / ``codex`` — the env is IGNORED.** Verified live (codex-cli 0.144.1): with no
stored login, ``codex exec`` fails ``401 Missing bearer`` whether ``OPENAI_API_KEY`` is
set or not — *identically*. It never reads the var. Not "the stored login wins": the env
key is not consulted at all, even with ``-c preferred_auth_method="apikey"``. codex's own
help confirms it — ``--skip-config`` documents that "auth still uses ``CODEX_HOME``".

So for codex, auth is machine-global state (``codex login``), and syncade's only lever
does nothing. Enforcement is impossible; the honest move is to PROBE and REFUSE rather
than silently run in the mode the user did not ask for.

(A per-run ``CODEX_HOME`` *could* force API mode — ``codex login --with-api-key`` into a
temp dir. Rejected: it writes the user's API key to a temp ``auth.json`` on disk. Buying
enforcement with a secret spill is a bad trade when a one-line refusal, and a one-time
``codex login --with-api-key`` the user runs themselves, gets the same guarantee.)
"""

from __future__ import annotations

from typing import Literal

from syncade.config import SyncadeConfig
from syncade.config_auth import (
    PROVIDER_KEY_VARS,
    AuthedActor,
    authed_actors,
    credential_fingerprint,
)
from syncade.process import SubprocessError, run_subprocess

CodexState = Literal["subscription", "api", "none", "unknown"]
"""What ``codex login status`` reports. Observed non-destructively via CODEX_HOME:

    "Logged in using ChatGPT"                     -> subscription
    "Logged in using an API key - sk-xxx***yyy"   -> api
    "Not logged in"                               -> none

``unknown`` is anything else — a new CLI version rewording its output. It is NOT treated
as "probably fine": an unrecognised state with a non-auto declaration is refused, because
proceeding would mean guessing about the user's money.
"""


def probe_codex_state(timeout: float = 15.0) -> tuple[CodexState, str]:
    """Ask ``codex login status`` what credential it would actually use.

    Returns ``(state, raw_output)``. The raw output is carried so a refusal can SHOW the
    user what syncade saw, instead of asserting an opaque verdict about their machine.
    """
    try:
        result = run_subprocess(
            ["codex", "login", "status"], cwd=None, env=None, timeout=timeout, input_text=None
        )
    except (SubprocessError, OSError) as exc:
        return "unknown", f"codex login status failed: {exc}"

    raw = (result.stdout + result.stderr).strip()
    lowered = raw.lower()

    # codex's documented logged-out state is rc=1 with stdout "Not logged in" (verified;
    # adapters/openai.py:60) — a real ANSWER, not a probe failure. Recognise it regardless
    # of exit code, so a logged-out user gets the actionable "NOT LOGGED IN — run codex
    # login" path instead of the opaque `unknown` diagnostic.
    if "not logged in" in lowered:
        return "none", raw

    # Any OTHER non-zero exit means the probe FAILED — its text is an error message, not a
    # status. Classifying it by substring is how a failed probe whose stderr happens to
    # contain "api key" ("could not read API key file: permission denied") got read as a
    # confident `api`, so the gate ANNOUNCED API billing on a state it never verified. The
    # guardrail is that an unverified state is `unknown` (and `unknown` + a non-auto
    # declaration is refused). Only a clean exit resolves the remaining real states.
    if result.returncode != 0:
        return "unknown", raw or f"codex login status exited {result.returncode}"

    if "api key" in lowered:
        return "api", raw
    if "chatgpt" in lowered:
        return "subscription", raw
    return "unknown", raw


def resolve_auth_mode(
    actor: AuthedActor, env: dict[str, str], codex_state: CodexState | None = None
) -> str:
    """The mode this actor will ACTUALLY run in — not the one it declared.

    ``auto`` exists so syncade changes nobody's behaviour by default; this is what makes
    it safe, because the preflight can then state the resolved truth out loud. Silence
    is the bug, not the default.
    """
    provider = getattr(actor, "provider", "")
    if provider == "openai":
        # The env cannot influence codex. Reality is whatever it is logged in as -- so
        # the declaration is irrelevant here and the probed state IS the answer.
        return codex_state or get_codex_state()
    if provider == "anthropic":
        if actor.auth != "auto":
            return actor.auth  # apply_auth_to_env enforces this outright
        # auto: claude uses a key if one is present, else its OAuth login.
        return "api" if any(env.get(v) for v in PROVIDER_KEY_VARS["anthropic"]) else "subscription"
    return "unknown"


_CODEX_STATE: CodexState = "unknown"
"""Resolved once per process by :func:`preflight`, then read by every cost record.

A module global rather than a value threaded through the orchestrator, for the same
reason ``run_status`` is one: there is exactly one review per process, and threading a
constant through six call sites buys nothing. It NEVER probes lazily — an unset value
stays ``"unknown"`` — so unit tests are hermetic and no test can accidentally spawn
``codex``. Honest ignorance beats a hidden subprocess.
"""


def set_codex_state(state: CodexState) -> None:
    """Record the probed state (or inject one in tests)."""
    global _CODEX_STATE
    _CODEX_STATE = state


def get_codex_state() -> CodexState:
    return _CODEX_STATE


def assert_codex_reality_honours_declaration(actor: AuthedActor) -> None:
    """Structural spend-safety backstop, called at the codex spawn site.

    :func:`~syncade.config_auth.apply_auth_to_env` makes an anthropic declaration real in
    the adapter env, but codex reads NO env var, so an openai declaration is only ever made
    real by :func:`preflight`'s probe-and-refuse. The CLI runs that gate; a DIRECT library
    call (``run_review`` / ``run_producer`` / the cold drivers) can skip it — which is the
    bypass the panel flagged. So every openai adapter calls this before it builds a codex
    invocation:

    - On the CLI path preflight already probed and, for a NON-auto declaration, refused
      unless reality matched — so ``_CODEX_STATE`` equals ``actor.auth`` and this passes
      silently.
    - On a gate-skipping path ``_CODEX_STATE`` is still its ``"unknown"`` default, so a
      non-auto declaration is refused HERE rather than silently billing the wrong codex
      account. ``auto`` never refuses (it accepts whatever codex is), matching preflight.

    Enforcement thus lives at the spawn site, not only in the CLI wrapper — the same
    read-not-remember lesson as the six-entry-point gate, pushed one layer down to where a
    subprocess is actually about to cost money.
    """
    if getattr(actor, "provider", "") != "openai" or actor.auth == "auto":
        return
    state = get_codex_state()
    if state != actor.auth:
        raise ValueError(
            f'openai auth = "{actor.auth}" cannot be honoured: codex login reality is '
            f"{state!r} and no auth gate ran before this spawn. Run through the syncade "
            f"CLI, which probes `codex login status` and refuses before any spend."
        )


def preflight(
    config: SyncadeConfig, env: dict[str, str], blocks: frozenset[str] | None = None
) -> list[str]:
    """Probe reality, remember it, and return every declaration we cannot honour.

    Non-empty ⇒ the caller must REFUSE TO RUN, before a single subprocess bills.

    The probe runs whenever an openai actor EXISTS, not merely when one declares a mode:
    the resolved mode is needed for honest cost reporting either way (issue 4), and
    codex's reality is the only way to know whether a dollar figure is money or fiction.
    One ~100ms local subprocess per run. A config with no openai actor pays nothing.
    """
    actors = authed_actors(config, blocks)
    has_openai = any(getattr(a, "provider", "") == "openai" for _, a in actors)
    if not has_openai:
        set_codex_state("unknown")
        return []
    state, _raw = probe_codex_state()
    set_codex_state(state)
    return reality_problems(config, env, state, blocks)


def preflight_problems(config: SyncadeConfig, env: dict[str, str]) -> list[str]:
    """Backwards-compatible alias of :func:`preflight`."""
    return preflight(config, env)


def _why(provider: str, declared: str, mode: str, env: dict[str, str], key_var: str = "") -> str:
    """The reason this provider resolved the way it did, in the user's terms.

    A mode with no reason is just as opaque as no mode at all — the user cannot act on
    "anthropic → api" unless they know it is THEIR exported key doing it.
    """
    if provider == "anthropic":
        if declared == "subscription":
            return 'auth = "subscription": API keys stripped from the child env'
        if declared == "api":
            named = f" from {key_var}" if key_var else ""
            return f'auth = "api": your key{named} is passed; the claude.ai login is not used'
        if mode == "api":
            # Name the var that ACTUALLY resolved it. auto resolves to `api` if EITHER
            # ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is set, and naming the wrong one
            # sends the user to unset a var they never set.
            present = [v for v in PROVIDER_KEY_VARS["anthropic"] if env.get(v)]
            named = " and ".join(present) if present else "an API credential"
            return f"{named} is set and OVERRIDES your claude.ai login"
        return "no API key in the environment; using your claude.ai login"
    if provider == "openai":
        if mode == "subscription":
            return "codex is logged in with ChatGPT; OPENAI_API_KEY is ignored"
        if mode == "api":
            return "codex is logged in with an API key"
        if mode == "none":
            return "codex is NOT logged in — run `codex login`"
        return "could not read `codex login status`"
    return "unknown provider"


def report_lines(
    config: SyncadeConfig, env: dict[str, str], blocks: frozenset[str] | None = None
) -> list[str]:
    """What each provider will actually bill, stated out loud, every run.

    **This is why ``auto`` is allowed to be the default.** Stripping keys by default would
    break users who deliberately run on API keys with no subscription; so ``auto`` leaves
    the CLIs alone — and that is only safe because the resolved truth is ANNOUNCED. The
    failure this PR exists to delete is not "the wrong mode", it is "the wrong mode,
    silently". A developer with ANTHROPIC_API_KEY exported believes they are on their Max
    plan while every reviewer, producer and judge subprocess bills the API.

    Printed even under ``--quiet``, for the same reason deprecation warnings are: how your
    money is being spent is actionable regardless of a verbosity preference.

    Grouped by (provider, RESOLVED MODE) — never by provider alone. Auth is configured and
    enforced PER ACTOR, so collapsing on provider would keep whichever actor came first and
    hide the others: a config with one anthropic actor on ``subscription`` and another on
    ``api`` would print "anthropic → subscription" while an actor quietly billed the API.
    A false reassurance is worse than no line at all, and this is the one function whose
    entire job is to not do that. (Caught by syncade's own panel, unanimously.)
    """
    # Keyed on the CREDENTIAL -- (provider, resolved mode, key var) -- not merely
    # (provider, mode). Two anthropic actors can both resolve to `api` while presenting
    # DIFFERENT keys (one via an exported ANTHROPIC_API_KEY, one via api_key_env =
    # "WORK_KEY"). Sharing one group meant sharing one REASON, so the second actor was
    # explained by the first actor's story -- the report said "ANTHROPIC_API_KEY overrides
    # your login" about an actor that is actually paying with WORK_KEY. A wrong reason is
    # a wrong report.
    groups: dict[tuple[str, str, tuple[str, ...]], list[str]] = {}
    declared: dict[tuple[str, str, tuple[str, ...]], str] = {}
    key_vars: dict[tuple[str, str, tuple[str, ...]], list[str]] = {}
    for label, actor in authed_actors(config, blocks):
        provider = getattr(actor, "provider", "")
        mode = resolve_auth_mode(actor, env)
        key_var = (actor.key_var() or "") if mode == "api" else ""
        # Group by the credential the CLI will actually get, READ from the enforced env --
        # not derived from (provider, mode, key_var). Two anthropic `api` actors can share a
        # key_var yet present different credentials: an `auto` actor keeps ANTHROPIC_AUTH_TOKEN
        # while an explicit `api` actor strips it. Deriving grouped them together under one
        # reason. Same read-not-derive lesson as _credential_key (round 6).
        cred = credential_fingerprint(actor, env)
        key = (provider, mode, cred)
        groups.setdefault(key, []).append(label)
        declared.setdefault(key, actor.auth)
        # One credential (one account) can be reached through several source vars -- WORK1
        # and WORK2 both holding the same key fingerprint identically, so they share a group.
        # Naming only the first left a WORK2 actor explained by the WORK1 reason. Collect
        # EVERY distinct source var so no actor is named by another's.
        if key_var and key_var not in key_vars.setdefault(key, []):
            key_vars[key].append(key_var)

    lines = []
    for key in sorted(groups):
        provider, mode, _cred = key
        reason = _why(provider, declared[key], mode, env, ", ".join(key_vars.get(key, [])))
        billing = {
            "api": "billed to your API account",
            "subscription": "billed to your subscription — $0 marginal",
        }.get(mode, "billing unknown")
        lines.append(f"  {provider:<10} → {mode:<12} {billing}")
        lines.append(f"  {'':<10}   ({reason})")
        lines.append(f"  {'':<10}   actors: {', '.join(groups[key])}")
    return lines


def reality_problems(
    config: SyncadeConfig,
    env: dict[str, str],
    codex_state: CodexState,
    blocks: frozenset[str] | None = None,
) -> list[str]:
    """Declarations syncade cannot honour. Non-empty ⇒ REFUSE TO RUN.

    Only openai actors can land here: for anthropic the declaration IS enforced, so a
    contradiction is impossible by construction.

    Both directions are refused, deliberately. Running an ``api`` declaration on a
    ChatGPT login silently bills a subscription the user meant to spare (and burns a
    quota they were escaping); running a ``subscription`` declaration on a stored API key
    silently bills money they never agreed to spend. Neither is "close enough".
    """
    problems: list[str] = []
    for label, actor in authed_actors(config, blocks):
        if getattr(actor, "provider", "") != "openai" or actor.auth == "auto":
            continue
        if codex_state == actor.auth:
            continue

        if codex_state == "none":
            problems.append(
                f'{label}: declares auth = "{actor.auth}" but codex is NOT LOGGED IN. '
                f"Run `codex login` (subscription) or "
                f"`printenv OPENAI_API_KEY | codex login --with-api-key` (api)."
            )
        elif actor.auth == "api" and codex_state == "subscription":
            problems.append(
                f'{label}: declares auth = "api", but codex is logged in with ChatGPT and '
                f"IGNORES OPENAI_API_KEY entirely — this run would bill your ChatGPT "
                f"subscription, not your API account. Run "
                f"`printenv OPENAI_API_KEY | codex login --with-api-key`, or declare "
                f'auth = "subscription".'
            )
        elif actor.auth == "subscription" and codex_state == "api":
            problems.append(
                f'{label}: declares auth = "subscription", but codex is logged in with an '
                f"API KEY — this run would bill your API account real money. Run "
                f'`codex login` to use your ChatGPT plan, or declare auth = "api".'
            )
        else:  # unknown
            problems.append(
                f'{label}: declares auth = "{actor.auth}", but syncade could not tell what '
                f"credential codex would use. It said: {codex_state!r}. Refusing rather "
                f"than guessing about your money."
            )
    return problems
