"""Auth-mode declaration for every actor: subscription vs API key.

**The bug this exists to close.** Syncade copies ``dict(os.environ)`` into every
subprocess, so the auth *mode* is whatever each CLI decides — and the two CLIs decide
OPPOSITELY. Verified live (claude 2.1.208 / codex-cli 0.144.1) by injecting a bogus
key into each and seeing who lost:

    claude  + bogus ANTHROPIC_API_KEY -> 401. The ENV KEY beat the claude.ai login.
                                        The CLI even says so: "ANTHROPIC_API_KEY ...
                                        takes precedence over your claude.ai login".
    codex   + bogus OPENAI_API_KEY    -> replied fine. The STORED CHATGPT LOGIN beat
                                        the env key; the run billed the subscription.

So on ONE machine, in ONE run, with ONE environment, one provider can bill your
subscription while the other silently bills your API account — fanned out N reviewers
x M rounds. A developer with ``ANTHROPIC_API_KEY`` exported (extremely common) believes
they are on their Max plan while every subprocess charges the API.

**Declared mode is enforced mode, in BOTH directions.** This is about quota as much as
money: subscriptions have usage caps and syncade is *designed* to fan out, so
``auth = "api"`` is a real escape hatch for heavy users — and their traffic must not
silently fall back to a throttled subscription either.

``auto`` is the default and resolves to whatever the CLI would actually do. It is
deliberately NOT "strip everything by default": that would break users who intentionally
run on API keys with no subscription at all. Auto is safe only because the preflight
ALWAYS prints the resolved mode. Silence is the bug.

Leaf module (imports nothing from syncade but :mod:`syncade.config_types`) because every
actor config imports it and ``config`` cannot depend on the actors — same constraint that
put the cold-actor defaults in :mod:`syncade.config_cold`.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:  # runtime import would cycle: config imports THIS module
    from syncade.config import SyncadeConfig

AuthMode = Literal["auto", "subscription", "api"]

PROVIDER_KEY_VARS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "openai": ("OPENAI_API_KEY",),
}
"""Env vars that route a provider's CLI to the API instead of its stored login.

Stripping these is what makes ``auth = "subscription"`` an ENFORCED guarantee rather
than a request: the CLI physically cannot reach the API without them.

Deliberately NOT included: ``*_BASE_URL``. Those point at proxies/gateways rather than
carrying credentials, and stripping them would break 3P-provider users for no security
gain. Only credential-bearing vars belong here.
"""


def default_api_key_env(provider: str) -> str | None:
    """The conventional key var for ``provider``, or ``None`` if unknown."""
    keys = PROVIDER_KEY_VARS.get(provider)
    return keys[0] if keys else None


class AuthedActor(BaseModel):
    """Mixin: every actor that spawns a provider CLI declares its auth mode.

    Inherited by ``ReviewerConfig``, ``ProducerConfig`` and ``_ColdActorConfig``, so the
    declaration exists on all five actor types (reviewers, producer, judge, drafter,
    auditor) rather than only where someone remembered to add it.
    """

    model_config = ConfigDict(extra="forbid")

    auth: AuthMode = Field(
        default="auto",
        description=(
            "Which credential this actor's CLI must use. ``subscription`` STRIPS the "
            "provider's API-key vars from the child env, so the CLI physically cannot "
            "reach the API. ``api`` on ANTHROPIC requires the key in the env (exit 50 at "
            "config load if missing); on OPENAI it means `codex login --with-api-key` and "
            "reads no env var -- verified by the codex probe, not at load. "
            "``auto`` (default) resolves to whatever the CLI would do on its own — and "
            "the preflight always prints which that is, because the failure mode here "
            "is silence, not the wrong choice."
        ),
    )
    api_key_env: str | None = Field(
        default=None,
        description=(
            'Env var holding the API key, when ``auth = "api"``. Defaults to the '
            "provider's conventional var (ANTHROPIC_API_KEY / OPENAI_API_KEY). Set it "
            "to use a differently-named var — e.g. a work key separate from a personal "
            "one."
        ),
    )

    @model_validator(mode="after")
    def _api_key_env_must_be_honourable(self) -> AuthedActor:
        """``api_key_env`` is refused whenever syncade could not actually honour it.

        Two ways it becomes a silent no-op, and a user who names a key var plainly intends
        it to be used, so both fail loudly at config load:

        1. **Without ``auth = "api"``** the key would simply be ignored.
        2. **On an openai actor, at all.** ``codex`` does not read the environment — its
           key comes from ``CODEX_HOME`` (``codex login --with-api-key``). So
           ``api_key_env = "WORK_KEY"`` on an openai actor was ACCEPTED while codex quietly
           used whatever key was in its stored login: the user believes their WORK_KEY is
           paying, and a different key actually is. That is the exact deceit this module
           exists to delete, so it is refused rather than accepted-and-ignored.
        """
        if self.api_key_env is None:
            return self
        if self.auth != "api":
            raise ValueError(
                f'api_key_env={self.api_key_env!r} requires auth = "api" '
                f'(got auth = "{self.auth}"), otherwise the key would be ignored'
            )
        if getattr(self, "provider", "") == "openai":
            raise ValueError(
                f"api_key_env={self.api_key_env!r} cannot be honoured for provider "
                f'"openai": codex ignores the environment and reads its key from its '
                f"stored login. Run `printenv {self.api_key_env} | codex login "
                f"--with-api-key` instead — syncade will verify it with `codex login "
                f"status`."
            )
        return self

    def key_var(self) -> str | None:
        """The env var this actor reads its API key from, when in ``api`` mode."""
        return self.api_key_env or default_api_key_env(getattr(self, "provider", ""))


def apply_auth_to_env(env: dict[str, str], actor: AuthedActor) -> dict[str, str]:
    """Enforce ``actor.auth`` on the environment its CLI subprocess will inherit.

    This is where a declaration becomes a guarantee. The env is the ONLY lever syncade
    has — it cannot reach inside a CLI's credential resolution — so:

    - ``subscription`` → **strip** the provider's key vars. The CLI then physically
      cannot reach the API; it has no key. This closes the ``claude`` footgun outright
      (an exported ``ANTHROPIC_API_KEY`` otherwise beats the claude.ai login, verified
      live). Not a request to the CLI — a removal of the capability.
    - ``api`` → strip the provider's key vars, then set the CLI's CANONICAL var from
      wherever the user actually keeps the key. That indirection is the point of
      ``api_key_env``: ``claude`` reads ``ANTHROPIC_API_KEY`` and nothing else, so a
      user with the key in ``WORK_KEY`` needs it mapped across or the setting would be
      a silent no-op. Stripping first also means a stale ``ANTHROPIC_AUTH_TOKEN`` can't
      outrank the key the user just declared.
    - ``auto`` → untouched. Whatever the CLI would have done, it still does — and the
      preflight prints which that is. Auto is only safe BECAUSE it is announced.

    Note this deliberately does NOT run for the test/check legs, which share
    :func:`~syncade.worktree_env.worktree_scoped_env`: those run the operator's own test
    command, which may legitimately need API keys. Enforcement belongs where a PROVIDER
    CLI is spawned, which is why it lives in the adapters and not in the env builder.

    Returns a new dict; ``env`` is never mutated.
    """
    provider = getattr(actor, "provider", "")
    key_vars = PROVIDER_KEY_VARS.get(provider, ())
    if actor.auth == "auto" or not key_vars:
        return env

    stripped = {k: v for k, v in env.items() if k not in key_vars}
    if actor.auth == "subscription":
        return stripped

    if provider == "openai":
        # codex NEVER reads the environment -- its key lives in CODEX_HOME. There is no key
        # to route and nothing to require: `auth = "api"` here is satisfied by
        # `codex login --with-api-key`, which auth_preflight verifies via `codex login
        # status` before anything spends.
        #
        # Config load already exempts openai from the key requirement. Raising HERE anyway
        # meant the config loaded and then the reviewer/judge DIED at subprocess-build
        # time -- the same bug one layer down, which is the seventh time in this PR I fixed
        # one call site and left its twin. Strip the (ignored) var and get out of the way.
        return stripped

    var = actor.key_var()
    key = env.get(var or "")
    if not key:
        # config load already guaranteed this; reaching here means the env changed
        # underneath us, and shipping the request without a key would silently fall
        # back to the subscription -- the exact failure this PR deletes.
        raise ValueError(
            f'auth = "api" for provider "{provider}" requires {var} to be set in the '
            f"environment, but it is empty or missing"
        )
    return {**stripped, key_vars[0]: key}


#: The actors each CLI mode can ACTUALLY spawn. The gate must refuse (and report) on
#: these and no others: `syncade --spec-audit` spawns only the auditor, so refusing it
#: because a REVIEWER's declaration contradicts reality blocks a command that would never
#: have run that reviewer -- and the auth report would announce billing for actors that
#: are not going to bill anything.
REVIEW_BLOCKS = frozenset({"reviewers", "producer", "synthesizer"})
#: `--selfcheck` is a PRODUCER-ONLY commit smoke (run_selfcheck calls run_producer once
#: and spawns no reviewer). It listed `reviewers` in error — a reviewer's auth declaration
#: then blocked a command that never runs a reviewer. Verified against selfcheck.py.
SELFCHECK_BLOCKS = frozenset({"producer"})
AUDIT_BLOCKS = frozenset({"auditor"})
DRAFT_BLOCKS = frozenset({"drafter"})
ALL_BLOCKS = frozenset({"reviewers", "producer", "synthesizer", "drafter", "auditor"})


def credential_fingerprint(actor: AuthedActor, env: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """A stable identity for the credential this actor will ACTUALLY present, READ from the
    enforced env rather than derived from its declaration.

    ONE definition, used by both surfaces that need it — ``--auth-check``'s probe-dedup
    (:func:`syncade.auth_check._credential_key`) and the auth report's grouping
    (:func:`syncade.auth_preflight.report_lines`). Two copies of "which credential is this"
    diverged twice: round 6 (auto vs explicit api, custom key var) and round 11 (stale
    AUTH_TOKEN). Reading VALUES, not var names, settles both — and keeping it in one place
    is the only thing that stops the third.

    Values are hashed so a secret never lands in a dict key or a log. Missing-key ``api``
    (``apply_auth_to_env`` raises) fingerprints as ``()`` — no credential, its own group.
    """
    import hashlib

    provider = getattr(actor, "provider", "")
    if provider == "openai":
        # codex reads NO env var (its login lives in CODEX_HOME), so the env does not
        # distinguish an openai actor's credential at all: `auto`, `subscription`, and `api`
        # openai actors on one machine are the SAME stored codex login. Fingerprinting by
        # OPENAI_API_KEY (which `auto` leaves in the env) split them into phantom "different
        # credentials", duplicating probes and report lines for one real credential.
        return ()
    try:
        applied = apply_auth_to_env(dict(env), actor)
    except ValueError:
        return ()
    return tuple(
        sorted(
            (var, hashlib.sha256(applied[var].encode()).hexdigest()[:12])
            for var in PROVIDER_KEY_VARS.get(provider, ())
            if applied.get(var)
        )
    )


def authed_actors(
    config: SyncadeConfig, blocks: frozenset[str] | None = None
) -> list[tuple[str, AuthedActor]]:
    """Every actor that spawns a provider CLI, labelled by its config block.

    One list, so nothing gets enforced for four actors and forgotten for the fifth.
    Reused by the load-time key check, the env enforcement, the codex reality probe,
    and the preflight print.

    ``blocks`` narrows it to the actors a given mode can actually spawn (see
    ``*_BLOCKS`` above). ``None`` means all of them — correct for the load-time key check,
    which validates the whole config regardless of which command is running.
    """
    tagged: list[tuple[str, str, AuthedActor]] = [
        ("reviewers", f'[[reviewers]] "{r.name}"', r) for r in config.reviewers
    ]
    tagged.append(("producer", "[producer]", config.producer))
    tagged.append(("synthesizer", "[synthesizer]", config.synthesizer))
    tagged.append(("drafter", "[drafter]", config.drafter))
    tagged.append(("auditor", "[auditor]", config.auditor))
    return [(label, a) for tag, label, a in tagged if blocks is None or tag in blocks]


def api_key_problems(config: SyncadeConfig, env: dict[str, str] | None = None) -> list[str]:
    """Actors that declare ``auth = "api"`` but have no key to use.

    Returned rather than raised so :mod:`syncade.config_loader` owns the error type
    (importing it here would cycle). The caller turns a non-empty list into a
    ``ConfigError`` — i.e. **exit 50, at config load**.

    Failing here is the entire point: the alternative is discovering it mid-run, after
    both reviewers have already run and billed. Every problem is reported at once, not
    one per re-run.
    """
    env = os.environ if env is None else env  # type: ignore[assignment]
    problems: list[str] = []
    for label, actor in authed_actors(config):
        if actor.auth != "api":
            continue
        var = actor.key_var()
        provider = getattr(actor, "provider", "?")
        if provider == "openai":
            # codex NEVER reads the environment — its key lives in CODEX_HOME. Demanding
            # OPENAI_API_KEY here was a false requirement, and a DEAD END: we tell the user
            # to run `codex login --with-api-key` (the only thing that works), and then
            # refused to load anyway unless they also exported a var codex ignores.
            #
            # The openai `api` declaration is verified where it actually lives — the
            # `codex login status` probe in auth_preflight, which refuses if codex is not
            # logged in with an API key. That is the real check; this one was theatre.
            continue
        if var is None:
            problems.append(
                f'{label}: auth = "api" but provider "{provider}" has no conventional '
                f"key var — set api_key_env explicitly"
            )
        elif not env.get(var):
            problems.append(
                f'{label}: auth = "api" requires {var} to be set, but it is not '
                f'(export it, or use auth = "subscription")'
            )
    return problems
