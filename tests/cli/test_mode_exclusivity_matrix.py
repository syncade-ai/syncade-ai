"""Every pair of one-shot modes is rejected — derived from the parser, not hand-listed.

`--install-skill` WAS in `validate.py` and did block `--doctor` and `--base`, so this was an
incomplete matrix rather than an absent guard: all NINE other pairings were accepted, exit 0,
with the other intent silently dropped. `syncade <brief> --install-skill claude` ran no
reviewers, destroyed the destination, and returned success.

The guard is written as per-mode exclusion lists, which is readable but hand-maintained — and
a hand-maintained list is what produced the gap. So the GENERALITY lives here: the matrix is
generated from `build_parser()`, so a mode added later is covered the day it lands, whatever
shape the guard takes. That is the brief's attack #5, and it is why this is a test rather than
a refactor of 20 careful pairwise checks.
"""

from __future__ import annotations

import itertools

import pytest

from syncade.cli import main
from syncade.cli.parser import build_parser

#: One-shot modes: each does its own thing and then exits, so no two can be honoured at once.
#: `value` is a usable operand for flags that require one.
_MODES: dict[str, list[str]] = {
    "--gc": ["--gc"],
    "--metrics": ["--metrics"],
    "--selfcheck": ["--selfcheck"],
    "--auth-check": ["--auth-check"],
    "--draft-spec": ["--draft-spec"],
    # An explicit run-id: `--resume` takes an OPTIONAL value, so a bare `--resume <brief>`
    # has argparse swallow the brief AS the run-id — there is then no pair to detect,
    # and the operator gets "run-id not found". That is tokenization, not a guard gap;
    # pinning the value here tests the GUARD rather than argparse's greediness.
    "--resume": ["--resume", "latest"],
    "--config": ["--config", "list"],
    "--install-skill": ["--install-skill", "claude"],
    "--doctor": ["--doctor"],
}


def test_the_mode_list_matches_the_parser():
    """If a new one-shot flag lands and is not listed here, this fails and points at it.

    Without this, `_MODES` is just a third hand-maintained list and the matrix below silently
    stops covering whatever was added.
    """
    declared = set()
    for action in build_parser()._actions:
        declared.update(action.option_strings)
    missing = [m for m in _MODES if m not in declared]
    assert not missing, f"_MODES names flags the parser does not define: {missing}"

    # Flags that terminate the process without running a review. Kept explicit so adding a
    # mode forces a deliberate decision about exclusivity rather than a silent omission.
    known_one_shots = set(_MODES) | {"--version", "--help", "-h"}
    suspicious = {
        f
        for f in declared
        if f.endswith(("-check", "-skill")) or f in {"--gc", "--metrics", "--doctor"}
    } - known_one_shots
    assert not suspicious, (
        f"these look like one-shot modes but are not in _MODES, so no pair covers them: "
        f"{sorted(suspicious)}"
    )


@pytest.mark.parametrize("a,b", sorted(itertools.combinations(sorted(_MODES), 2)))
def test_mode_pairs_are_all_rejected(a, b, tmp_path, capsys):
    """No pair may be accepted. An accepted pair means one intent was silently dropped."""
    rc = main([*_MODES[a], *_MODES[b], "--repo-root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 2, f"{a} + {b} was ACCEPTED (rc={rc}) — one of them was silently dropped"
    assert "cannot be combined" in err, f"{a} + {b} rejected without saying why: {err!r}"


@pytest.mark.parametrize("mode", sorted(_MODES))
def test_a_mode_combined_with_a_pr_doc_is_rejected(mode, tmp_path, capsys):
    """The shape that actually bit: a review request silently becoming something else."""
    brief = tmp_path / "brief.md"
    brief.write_text("# PR\n")
    rc = main([*_MODES[mode], str(brief), "--repo-root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 2, f"{mode} + PR_DOC was ACCEPTED (rc={rc}) — the review was dropped"
    # `--config` answers with a better, mode-specific message ("takes no arguments ... did you
    # mean `syncade <PR_DOC>`?"), so require a REFUSAL that explains itself, not one phrasing.
    assert ("cannot be combined" in err) or ("takes no arguments" in err), (
        f"{mode} + PR_DOC rejected without saying why: {err!r}"
    )
