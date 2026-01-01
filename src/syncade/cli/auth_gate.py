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
    return None
