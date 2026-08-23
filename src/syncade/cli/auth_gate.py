"""The one auth gate every entry point passes through.

Wiring the preflight into the review path only was not enough, and syncade's own panel
caught it unanimously: FIVE other modes load config and spawn provider subprocesses, and
none of them checked anything.

    --resume       resumes a FULL review loop -- reviewers, judge, producer
    --selfcheck    spawns real provider subprocesses
    --spec-audit   spawns the cold auditor
    --draft-spec   spawns the cold drafter
    --auth-check   probes each configured provider

``--resume`` is the one that stings: a user could be refused on a fresh run and then
simply resume past the refusal, billing the account they were protected from thirty
seconds earlier.

The mistake is worth naming, because it is the same one twice. In issue 2 I checked that
enforcement covered all five ACTOR TYPES and was pleased with myself. It never occurred to
me to check that it covered all six ENTRY POINTS. Right instinct, wrong axis. So the gate
now lives in ONE function that every mode calls, rather than in a policy each mode is
trusted to remember.
"""

from __future__ import annotations

import os
import sys

from syncade import auth_preflight
from syncade.config import SyncadeConfig
from syncade.exit_codes import CONFIG_ERROR


def auth_gate(config: SyncadeConfig, blocks: frozenset[str] | None = None) -> int | None:
    """Refuse impossible declarations; announce who is about to be billed.

    Returns ``CONFIG_ERROR`` when the run must not start, else ``None``.

    There is no ``announce=False``. It existed for ``--auth-check`` on the theory that a
    second auth block would be "noise" — and that was exactly the mutation I had warned
    about in issue 5: a quiet ``--auth-check`` could then spawn a probe under ``auto``,
    hit the API because ANTHROPIC_API_KEY was set, and never say so. The command whose
    entire job is auth is the LAST place to suppress the auth line.
    """
    env = dict(os.environ)
    # Scoped to the actors THIS command can spawn. `--spec-audit` runs only the auditor, so
    # refusing it over a REVIEWER's declaration blocks a command that would never have run
    # that reviewer -- and the report would announce billing for actors that will not bill.
    problems = auth_preflight.preflight(config, env, blocks)
    if problems:
        print("[syncade] auth error: declared mode contradicts this machine", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return CONFIG_ERROR

    print("[syncade] auth:", file=sys.stderr)
    for line in auth_preflight.report_lines(config, env, blocks):
        print(line, file=sys.stderr)
    _announce_unconfined_producer(config, blocks)
    return None


def _announce_unconfined_producer(config: SyncadeConfig, blocks: frozenset[str] | None) -> None:
    """Say out loud, on every run that CAN dispatch a producer, when its sandbox is off.

    "Every run" was the original wording and the gate below made it false: a single-pass review
    dispatches no producer, so warning there is the false alarm this docstring goes on to say
    teaches operators to ignore the real one. The claim and the condition are stated together
    here deliberately — they drifted apart once already, in this same function.

    Same rule as the auth block directly above, for the same reason: ``yolo`` is allowed to
    exist because the resolved truth is ANNOUNCED. What it grants is not recoverable from the
    run's own output — a producer that moves an operator ref directly leaves its OWN ``HEAD``
    unchanged, so syncade reports the round as STALLED and nothing else ever contradicts that.
    An operator who cannot see the grant cannot audit the consequence.

    Printed even under ``--quiet``: a verbosity preference is about progress, not about which
    authority a model was handed. It is a NOTICE, never a prompt — nothing here can stop or
    slow an unattended run, which is the property the whole tool depends on.

    Gated on ``blocks`` rather than on config alone, following the same axis mistake this
    module's docstring records: ``--spec-audit`` and ``--draft-spec`` never dispatch a producer,
    and warning about authority a command cannot grant is a false alarm that teaches operators
    to ignore the real one.
    """
    if blocks is not None and "producer" not in blocks:
        return
    # A single-pass review commits nothing, so warning about the producer's sandbox there is a
    # false alarm — and this function's own docstring says why that matters: a warning that fires
    # when nothing can happen teaches operators to ignore the one that means something.
    #
    # Derived, not mode-matched: a command that runs BOTH reviewers and a producer is the review
    # loop, whose producer is gated by `max_rounds`. A command that runs the producer WITHOUT
    # reviewers is `--selfcheck`, which dispatches one unconditionally and must keep the notice.
    if (
        blocks is not None
        and "reviewers" in blocks
        and "producer" in blocks
        and config.loop.max_rounds <= 1
    ):
        return
    if config.producer.permissions != "yolo":
        return
    print(
        '[syncade] producer sandbox: OFF (permissions = "yolo")\n'
        "  the producer runs an unsandboxed shell as you: it can read and write anything\n"
        "  you can, and can move operator git refs directly — a ref moved that way leaves\n"
        "  its own HEAD unchanged, so the round still reports as stalled\n"
        '  set producer.permissions = "confined" to enforce the sandbox '
        "(`syncade --config set producer.permissions confined`)",
        file=sys.stderr,
    )
