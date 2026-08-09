"""Auth REPORTING + gate SCOPING (PR-v2-24, split from test_auth_preflight for LOC).

Who is about to be billed, per-actor reasons, and that each CLI mode is gated on ONLY the
actors it can spawn. The probe/refusal core stays in test_auth_preflight.py.

Original header follows:

Detect-and-refuse when a declaration cannot be honoured (PR-v2-24, issue 3).

``codex`` IGNORES ``OPENAI_API_KEY``. Not "prefers its stored login over it" — ignores
it. Verified live (codex-cli 0.144.1): with no stored login, ``codex exec`` fails
``401 Missing bearer`` whether the key is exported or not, IDENTICALLY, even with
``-c preferred_auth_method="apikey"``. Its own help says auth always comes from
``CODEX_HOME``.

So the env — syncade's only lever — cannot influence codex at all, and an ``auth = "api"``
declaration on a ChatGPT login is unenforceable. Running it anyway would silently bill the
subscription the user was trying to spare. The only honest move is to refuse.
"""

from __future__ import annotations

import subprocess

import pytest

from syncade.config import ProducerConfig, ReviewerConfig, SyncadeConfig

# The EXACT strings `codex login status` emits, captured from the real CLI by pointing
# CODEX_HOME at throwaway dirs (never touching the operator's real login).
_REAL_CHATGPT = "Logged in using ChatGPT"
_REAL_APIKEY = "Logged in using an API key - sk-test-***l-key"
_REAL_NONE = "Not logged in"


def _cfg(auth: str, provider: str = "openai") -> SyncadeConfig:
    return SyncadeConfig(
        reviewers=[ReviewerConfig(name="rv", provider=provider, model="m", auth=auth)],
        producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),
    )


class TestTheCliActuallyRefuses:
    """The gate is only real if the CLI calls it. Mutation-tested: deleting the check
    from `cli._run` left all 126 CLI tests green, so nothing pinned the guarantee — the
    logic existed in a module nobody was obliged to consult. This is that pin."""

    def test_review_exits_50_before_any_reviewer_spawns(self, tmp_path, monkeypatch, capsys):
        import syncade.cli as cli
        from syncade.exit_codes import CONFIG_ERROR

        repo = tmp_path
        (repo / ".syncade").mkdir()
        (repo / ".syncade" / "config.toml").write_text(
            '[[reviewers]]\nname = "rv"\nprovider = "openai"\nmodel = "m"\nauth = "api"\n'
        )
        (repo / "pr.md").write_text("# PR\n")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-decoy")

        # codex is really logged in with ChatGPT -> the `api` declaration is unenforceable
        monkeypatch.setattr(
            "syncade.auth_preflight.probe_codex_state", lambda *a, **k: ("subscription", "")
        )

        spawned: list[int] = []
        monkeypatch.setattr(cli, "run_review", lambda *a, **k: spawned.append(1))

        # `--allow-auto-init`: PR-h-04 item B refuses auto-init in a POPULATED directory
        # by default (a baseline commit there captures whatever it finds). This fixture
        # writes a brief into the dir, so the flag is setup, not the thing under test.
        rc = cli.main([str(repo / "pr.md"), "--repo-root", str(repo), "--allow-auto-init"])

        assert rc == CONFIG_ERROR, "a run that would bill the wrong account must not start"
        assert not spawned, "REVIEWERS RAN — the whole point is to refuse BEFORE spending"
        assert "auth error" in capsys.readouterr().err


class TestTheRunAlwaysSaysWhoIsPaying:
    """`auto` is the default because stripping keys by default would break users who run
    on API keys with no subscription. That is only SAFE because the resolved truth is
    announced every run. The bug this PR deletes is not "the wrong mode" -- it is "the
    wrong mode, SILENTLY"."""

    def test_the_footgun_is_named_out_loud(self, monkeypatch) -> None:
        """A developer with ANTHROPIC_API_KEY exported believes they are on their Max
        plan. The line has to say the key is what is overriding it -- a bare
        "anthropic -> api" is not actionable."""
        import syncade.auth_preflight as ap

        ap.set_codex_state("subscription")
        # an anthropic producer + openai reviewers: the common Claude-Code shape
        cfg = SyncadeConfig(
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6")
        )
        out = "\n".join(ap.report_lines(cfg, {"ANTHROPIC_API_KEY": "sk-real"}))
        assert "anthropic" in out and "api" in out
        assert "OVERRIDES your claude.ai login" in out
        assert "billed to your API account" in out

    def test_subscription_says_zero_marginal(self, monkeypatch) -> None:
        import syncade.auth_preflight as ap

        ap.set_codex_state("subscription")
        cfg = SyncadeConfig(
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6")
        )
        out = "\n".join(ap.report_lines(cfg, {}))
        assert "claude.ai login" in out
        assert "OPENAI_API_KEY is ignored" in out, "codex ignoring the key is load-bearing"
        assert "$0 marginal" in out

    def test_printed_even_under_quiet(self, tmp_path, monkeypatch, capsys) -> None:
        """--quiet suppresses progress, not billing. Same rule the codebase already
        applies to deprecation warnings: this is the user's money, and it is actionable
        regardless of a verbosity preference."""
        import syncade.auth_preflight as ap
        import syncade.cli as cli

        repo = tmp_path
        (repo / "pr.md").write_text("# PR\n")
        (repo / ".syncade").mkdir()
        (repo / ".syncade" / "config.toml").write_text(
            '[producer]\nprovider = "anthropic"\nmodel = "claude-sonnet-4-6"\n'
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
        monkeypatch.setattr(ap, "probe_codex_state", lambda *a, **k: ("subscription", ""))
        monkeypatch.setattr(
            cli,
            "run_review",
            lambda *a, **k: type(
                "R",
                (),
                {
                    "exit_code": 0,
                    "dispatch_result": type("D", (), {"results": [object()]})(),
                    "rounds": [
                        type(
                            "Rnd",
                            (),
                            {
                                "dispatch_result": type(
                                    "D2", (), {"reviewer_subprocess_started": True}
                                )(),
                            },
                        )()
                    ],
                },
            )(),
        )

        cli.main([str(repo / "pr.md"), "--repo-root", str(repo), "--quiet", "--allow-auto-init"])

        err = capsys.readouterr().err
        assert "[syncade] auth:" in err, "a --quiet run must still say who is being billed"
        assert "OVERRIDES your claude.ai login" in err


class TestEveryEntryPointIsGated:
    """Wiring the gate into the review path ONLY was not enough — syncade's own panel
    caught it unanimously. Five other modes load config and spawn provider subprocesses.

    `--resume` is the one that stings: a user refused on a fresh run could simply resume
    past the refusal and bill the account they were protected from seconds earlier.

    The mistake is worth naming because it is the same one twice. In issue 2 I verified
    enforcement covered all five ACTOR TYPES. It never occurred to me to check that it
    covered all six ENTRY POINTS. Right instinct, wrong axis — so the gate now lives in
    ONE function every mode calls, not a policy each mode is trusted to remember.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["--resume"],
            ["--selfcheck"],
            ["--spec-audit", "pr.md"],
            ["--auth-check"],
        ],
    )
    def test_mode_refuses_an_unhonourable_declaration(self, argv, tmp_path, monkeypatch, capsys):
        import syncade.auth_preflight as ap
        import syncade.cli as cli
        from syncade.exit_codes import CONFIG_ERROR

        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
        (tmp_path / "seed").write_text("x")
        subprocess.run(["git", "add", "seed"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)
        # Feature branch + authoritative origin/HEAD -> main (faked, no real remote) so
        # --resume's default-branch guard is satisfied and REACHES the auth refusal under
        # test (a remote-less repo would be refused before auth).
        subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=tmp_path, check=True)
        _main_sha = subprocess.run(
            ["git", "rev-parse", "main"], cwd=tmp_path, capture_output=True, text=True, check=True
        ).stdout.strip()
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", _main_sha], cwd=tmp_path, check=True
        )
        subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
            cwd=tmp_path,
            check=True,
        )
        (tmp_path / ".syncade").mkdir()
        # EVERY actor declares the unhonourable mode, because the gate is now SCOPED to the
        # actors each command can actually spawn: `--spec-audit` runs only the auditor, so a
        # bad REVIEWER declaration must not (and no longer does) block it. Each mode is
        # refused by its OWN actors.
        (tmp_path / ".syncade" / "config.toml").write_text(
            '[[reviewers]]\nname = "rv"\nprovider = "openai"\nmodel = "m"\nauth = "api"\n'
            '[synthesizer]\nauth = "api"\n'
            '[auditor]\nauth = "api"\n'
            '[drafter]\nauth = "api"\n'
            '[producer]\nprovider = "openai"\nauth = "api"\n'
        )
        (tmp_path / "pr.md").write_text("# PR\n")
        # A valid aborted run so `--resume` resolves a plan and REACHES auth_gate (the plan
        # is resolved before auth, PR-v2-26) rather than exiting early with a resume error.
        from tests.orchestrator._resume_fixtures import _prepare_aborted_run

        _prepare_aborted_run(
            tmp_path,
            tmp_path / "pr.md",
            completed_round_count=0,
            max_rounds=2,
            aborted_exit_code=40,
        )
        monkeypatch.setenv("OPENAI_API_KEY", "sk-decoy")
        # codex is really on ChatGPT -> the `api` declaration cannot be honoured
        monkeypatch.setattr(ap, "probe_codex_state", lambda *a, **k: ("subscription", ""))

        rc = cli.main([*argv, "--repo-root", str(tmp_path)])

        assert rc == CONFIG_ERROR, f"`syncade {' '.join(argv)}` ran in an unhonourable mode"
        assert "auth error" in capsys.readouterr().err


class TestReportIsPerActorNotPerProvider:
    """Auth is configured and enforced PER ACTOR, so collapsing the report on provider
    alone would keep whichever actor came first and hide the rest: a config with one
    anthropic actor on `subscription` and another on `api` would print
    "anthropic -> subscription" while an actor quietly billed the API.

    A false reassurance is worse than no line at all — and this is the one function whose
    entire job is to not do that. Caught by the panel, unanimously."""

    def test_two_modes_on_one_provider_are_both_shown(self) -> None:
        import syncade.auth_preflight as ap

        ap.set_codex_state("subscription")
        cfg = SyncadeConfig(
            reviewers=[
                ReviewerConfig(name="rv-sub", provider="anthropic", model="m", auth="subscription"),
                ReviewerConfig(name="rv-api", provider="anthropic", model="m", auth="api"),
            ],
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),
        )
        out = "\n".join(ap.report_lines(cfg, {"ANTHROPIC_API_KEY": "sk-real"}))

        assert "billed to your API account" in out, "the API-billed actor was HIDDEN"
        assert "billed to your subscription" in out
        assert "rv-api" in out and "rv-sub" in out, "the user must know WHICH actor"


class TestEachCredentialGetsItsOwnREASON:
    """Grouping on (provider, resolved mode) shared ONE reason across the group. Two
    anthropic actors can both resolve to `api` while presenting DIFFERENT keys — one via an
    exported ANTHROPIC_API_KEY, one via `api_key_env = "WORK_KEY"`. The second was then
    explained by the first's story: the report said "ANTHROPIC_API_KEY overrides your
    login" about an actor actually paying with WORK_KEY.

    A wrong reason is a wrong report. Grouped on the CREDENTIAL — (provider, mode, key var)
    — so each explanation is true of the actors it names."""

    def test_two_api_credentials_get_distinct_reasons(self) -> None:
        import syncade.auth_preflight as ap

        ap.set_codex_state("subscription")
        cfg = SyncadeConfig(
            reviewers=[ReviewerConfig(name="auto-key", provider="anthropic", model="m")],
            producer=ProducerConfig(
                provider="anthropic",
                model="claude-sonnet-4-6",
                auth="api",
                api_key_env="WORK_KEY",
            ),
        )
        out = ap.report_lines(cfg, {"ANTHROPIC_API_KEY": "sk-personal", "WORK_KEY": "sk-work"})
        text = "\n".join(out)

        assert "OVERRIDES your claude.ai login" in text, "the auto actor's reason is missing"
        assert "WORK_KEY" in text, (
            "the explicit api actor is paying with WORK_KEY, but the report explained it "
            "with the OTHER actor's reason"
        )
        # and each reason must be attached to the right actor
        assert text.count("billed to your API account") == 2, "the two credentials collapsed"

    def test_one_credential_reached_via_two_vars_names_both(self) -> None:
        """WORK1 and WORK2 holding the SAME key fingerprint identically -> one account, one
        group (billing is correctly not split). But naming only the first left a WORK2 actor
        explained by the WORK1 reason. The reason must name EVERY source var in the group."""
        import syncade.auth_preflight as ap

        ap.set_codex_state("subscription")
        cfg = SyncadeConfig(
            reviewers=[
                ReviewerConfig(
                    name="rev", provider="anthropic", model="m", auth="api", api_key_env="WORK1"
                )
            ],
            producer=ProducerConfig(
                provider="anthropic", model="claude-sonnet-4-6", auth="api", api_key_env="WORK2"
            ),
        )
        text = "\n".join(ap.report_lines(cfg, {"WORK1": "sk-same", "WORK2": "sk-same"}))
        # one shared credential -> one group (not split into two API lines)
        assert text.count("billed to your API account") == 1, "same key split into two accounts"
        assert "WORK1" in text and "WORK2" in text, (
            "both source vars must be named; naming only the first mislabels the WORK2 actor"
        )


class TestTheReasonNamesTheVarActuallyInPlay:
    """`auto` resolves to `api` if EITHER ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is set,
    but the reason hardcoded "ANTHROPIC_API_KEY is set and OVERRIDES your claude.ai login".

    A user who only ever set ANTHROPIC_AUTH_TOKEN is then told to unset a var they never
    set. A reason that names the wrong cause is not a reason."""

    def test_auth_token_is_named_when_it_is_the_cause(self) -> None:
        import syncade.auth_preflight as ap

        ap.set_codex_state("subscription")
        cfg = SyncadeConfig(
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6")
        )
        out = "\n".join(ap.report_lines(cfg, {"ANTHROPIC_AUTH_TOKEN": "tok"}))
        assert "ANTHROPIC_AUTH_TOKEN is set" in out, "named a var the user never set"
        assert "ANTHROPIC_API_KEY is set" not in out

    def test_api_key_is_named_when_it_is_the_cause(self) -> None:
        import syncade.auth_preflight as ap

        ap.set_codex_state("subscription")
        cfg = SyncadeConfig(
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6")
        )
        out = "\n".join(ap.report_lines(cfg, {"ANTHROPIC_API_KEY": "sk"}))
        assert "ANTHROPIC_API_KEY is set" in out


class TestTheGateIsScopedToWhatTheCommandCanSpawn:
    """`auth_gate` checked the WHOLE config, so `--spec-audit` (which spawns only the
    auditor) was refused because a REVIEWER's declaration contradicted reality — blocking a
    command that would never have run that reviewer. And the auth report announced billing
    for actors that were not going to bill anything."""

    def test_audit_ignores_a_reviewers_bad_declaration(self, monkeypatch) -> None:
        import syncade.auth_preflight as ap
        from syncade.config_auth import AUDIT_BLOCKS, REVIEW_BLOCKS

        monkeypatch.setattr(ap, "probe_codex_state", lambda *a, **k: ("subscription", ""))
        cfg = SyncadeConfig(
            reviewers=[ReviewerConfig(name="rv", provider="openai", model="m", auth="api")],
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),
        )
        # the auditor is auto and fine -> --spec-audit must NOT be refused
        assert ap.preflight(cfg, {}, AUDIT_BLOCKS) == []
        # but the review path WOULD run that reviewer -> it must be refused
        assert ap.preflight(cfg, {}, REVIEW_BLOCKS), "the review path must still refuse"

    def test_the_report_only_names_actors_that_will_run(self, monkeypatch) -> None:
        import syncade.auth_preflight as ap
        from syncade.config_auth import DRAFT_BLOCKS

        ap.set_codex_state("subscription")
        cfg = SyncadeConfig(
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6")
        )
        out = "\n".join(ap.report_lines(cfg, {"ANTHROPIC_API_KEY": "sk"}, DRAFT_BLOCKS))
        assert "[drafter]" in out
        assert "[producer]" not in out, (
            "--draft-spec announced billing for the producer, which it never spawns"
        )


class TestTheCliActuallyPassesTheScope:
    """`auth_gate` takes the mode's actor scope — but does the CLI HAND IT OVER?

    Asserting `preflight(cfg, env, AUDIT_BLOCKS)` is correct proves nothing about whether
    `auth_gate` passes AUDIT_BLOCKS at all. Un-scoping the gate left every scoping test
    green, because they all called the helper directly. Tenth time in this PR: the helper
    was right and nothing obliged the caller to use it.

    This drives the real CLI."""

    def test_spec_audit_runs_despite_a_bad_reviewer_declaration(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        import subprocess as sp

        import syncade.auth_preflight as ap
        import syncade.cli as cli
        from syncade.exit_codes import CONFIG_ERROR

        sp.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / ".syncade").mkdir()
        # ONLY the reviewer is unhonourable. The auditor is fine.
        (tmp_path / ".syncade" / "config.toml").write_text(
            '[[reviewers]]\nname = "rv"\nprovider = "openai"\nmodel = "m"\nauth = "api"\n'
        )
        (tmp_path / "pr.md").write_text("# PR\n\nGoal: x\n")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-decoy")
        monkeypatch.setattr(ap, "probe_codex_state", lambda *a, **k: ("subscription", ""))

        ran: list[int] = []
        # `modes` imports run_spec_audit function-locally, so patch it at the SOURCE —
        # the decomposition rule (CLAUDE.md): patch the concrete lookup the body reads.
        monkeypatch.setattr(
            "syncade.spec_audit.run_spec_audit",
            lambda **k: ran.append(1) or _audit_ok(),
        )

        rc = cli.main(["--spec-audit", str(tmp_path / "pr.md"), "--repo-root", str(tmp_path)])

        assert rc != CONFIG_ERROR, (
            "--spec-audit was refused over a REVIEWER's declaration — an actor it never "
            "spawns. The gate is not scoped to what the command can actually run."
        )
        assert ran, "the auditor never ran"


def _audit_ok():
    from syncade.spec_audit import SpecAuditOutput, SpecAuditResult

    return SpecAuditResult(
        outcome="ready",
        output=SpecAuditOutput(
            verdict="READY",
            summary="ok",
            findings=[],
            priority_order=[],
            coverage_gaps=[],
            dismissed_concerns=[],
        ),
        error=None,
        duration_seconds=0.1,
    )


class TestSelfcheckScopeMatchesWhatItSpawns:
    """`run_selfcheck` is a PRODUCER-ONLY commit smoke — it calls run_producer once and
    spawns no reviewer. SELFCHECK_BLOCKS listed `reviewers` in error, so a reviewer's auth
    declaration blocked a command that never runs a reviewer.

    Both panel reviewers caught this, unanimously. Pinned against the RESOLVED scope, and
    against the module's own contract, so the constant can't silently drift from what
    selfcheck actually does."""

    def test_selfcheck_scope_is_producer_only(self) -> None:
        from syncade.config_auth import SELFCHECK_BLOCKS

        assert SELFCHECK_BLOCKS == frozenset({"producer"}), (
            "run_selfcheck spawns only the producer; any other actor in scope refuses a "
            "command that would never run it"
        )

    def test_a_bad_reviewer_does_not_block_selfcheck(self, monkeypatch) -> None:
        import syncade.auth_preflight as ap
        from syncade.config_auth import SELFCHECK_BLOCKS

        monkeypatch.setattr(ap, "probe_codex_state", lambda *a, **k: ("subscription", ""))
        cfg = SyncadeConfig(
            reviewers=[ReviewerConfig(name="rv", provider="openai", model="m", auth="api")],
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),
        )
        assert ap.preflight(cfg, {}, SELFCHECK_BLOCKS) == [], (
            "a reviewer's declaration blocked --selfcheck, which never spawns a reviewer"
        )

    def test_selfcheck_still_refuses_a_bad_PRODUCER(self, monkeypatch) -> None:
        """The scope must not become permissive: an unhonourable PRODUCER (the one actor
        selfcheck DOES run) must still refuse."""
        import syncade.auth_preflight as ap
        from syncade.config_auth import SELFCHECK_BLOCKS

        monkeypatch.setattr(ap, "probe_codex_state", lambda *a, **k: ("subscription", ""))
        cfg = SyncadeConfig(
            producer=ProducerConfig(provider="openai", model="gpt-5.5", auth="api"),
        )
        assert ap.preflight(cfg, {}, SELFCHECK_BLOCKS), "an unhonourable producer must refuse"


class TestReportGroupsByTheREADCredential:
    """Two anthropic `api` actors can share a key_var yet present DIFFERENT credentials: an
    `auto` actor keeps ANTHROPIC_AUTH_TOKEN while an explicit `api` actor strips it. Grouping
    by the derived (provider, mode, key_var) put them under one line with one reason.

    Same read-not-derive lesson as `_credential_key` (round 6). Grouped by the credential
    vars actually present in the enforced env now."""

    def test_auto_and_explicit_api_get_separate_lines(self) -> None:
        import syncade.auth_preflight as ap

        ap.set_codex_state("subscription")
        cfg = SyncadeConfig(
            reviewers=[ReviewerConfig(name="auto", provider="anthropic", model="m")],
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6", auth="api"),
        )
        out = ap.report_lines(cfg, {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t"})
        api_lines = [line for line in out if "→ api" in line]
        assert len(api_lines) == 2, (
            "auto (keeps AUTH_TOKEN) and explicit api (strips it) present different "
            "credentials and were collapsed under one reason"
        )


class TestOpenAiCredentialIsNotEnvDerived:
    """codex reads NO env var (its login is in CODEX_HOME), so an openai actor's credential
    is not distinguishable by the environment: `auto`, `subscription`, `api` openai actors
    on one machine are the SAME stored codex login. Fingerprinting by OPENAI_API_KEY (which
    `auto` leaves in the env) split them into phantom "different credentials", duplicating
    probes and report lines for one real credential."""

    @pytest.mark.parametrize("auth", ["auto", "subscription"])
    def test_openai_fingerprint_is_empty_regardless_of_env_key(self, auth, monkeypatch) -> None:
        from syncade.config_auth import credential_fingerprint

        monkeypatch.setenv("OPENAI_API_KEY", "sk-codex-ignores-this")
        actor = ReviewerConfig(name="a", provider="openai", model="m", auth=auth)
        assert credential_fingerprint(actor, dict(__import__("os").environ)) == (), (
            "codex ignores the env, so no env var should distinguish an openai credential"
        )

    def test_anthropic_credential_IS_still_env_derived(self, monkeypatch) -> None:
        """The openai carve-out must not weaken anthropic, where the env DOES determine auth."""
        from syncade.config_auth import credential_fingerprint

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
        actor = ReviewerConfig(name="a", provider="anthropic", model="m", auth="api")
        assert credential_fingerprint(actor, {"ANTHROPIC_API_KEY": "sk-real"}) != ()
