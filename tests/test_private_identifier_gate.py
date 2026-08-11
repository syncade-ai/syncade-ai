"""`check-no-private-identifiers.sh` detects what it claims to — PR-h-10 item 1.

The gate this replaces was itself the leak. It assembled its two tokens from split string
literals so it would not match its own grep, shipped on the public allowlist, and therefore
published both identifiers in plaintext. Its self-exemption was deliberate and documented, so
nothing would ever have flagged it.

The rewrite moves the tokens out of the tracked tree and scans the STAGED SNAPSHOT rather than
raw tracked content. Both halves need a test or the gate is just a claim:

  * a gate that is always green detects nothing         -> plant into a SHIPPED file, require red
  * scanning the snapshot must be scoping, not a hole   -> plant into an UNSHIPPED file, require
                                                           green, so the narrower scope is a
                                                           recorded decision rather than a bug
  * fail-closed is the whole point of an external list  -> point it at nothing, require red
  * the regression that started this                    -> no token in the gate's own source

Tokens are read from the untracked list at runtime, never written here: a test file carrying the
literals would recreate the exact bug being fixed, one directory over.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_GATE = _ROOT / "scripts" / "check-no-private-identifiers.sh"
_TOKENS = Path(os.environ.get("SYNCADE_PRIVATE_TOKENS", _ROOT / ".private-identifiers"))
_SHIPPED = _ROOT / "src" / "syncade" / "config.py"
# A tracked file that is NOT on the ship allowlist. NOT CLAUDE.md: reviewer worktrees strip it
# (it is a repo-context file), so a blind reviewer saw this suite crash rather than run. Use the
# scrub script instead — the module skipif above already requires it, so if it is absent the
# whole module skips and this constant is never read.
_UNSHIPPED = _ROOT / "scripts" / "oss-scrub.py"

# A synthetic token, so the BEHAVIOURAL tests never depend on the operator's real list. They used
# to call _tokens() and skip without it — which is precisely CI and the staged-release verifier,
# i.e. they were inert everywhere that mattered and green everywhere they ran.
#
# GENERATED AT RUNTIME, never written as a literal here. The first attempt used a fixed string,
# and the gate immediately flagged this file — correctly, because tests/ SHIPS, so a constant
# token makes the test file itself the leak. Splitting the literal to dodge the grep is the
# exact bug item 1 removed, so the token simply must not exist in any file.


@pytest.fixture
def synthetic(tmp_path) -> tuple[str, dict[str, str]]:
    """A one-off token plus the env pointing the gate at it. No real secrets, no literals."""
    token = "syncade-probe-" + uuid.uuid4().hex
    f = tmp_path / "synthetic-identifiers"
    f.write_text(f"# synthetic, generated per-run\n{token}\n", encoding="utf-8")
    return token, {"SYNCADE_PRIVATE_TOKENS": str(f)}


pytestmark = [
    pytest.mark.skipif(
        not (_ROOT / "scripts" / "oss-scrub.py").exists(),
        reason="public snapshot: the dev-only stage/scrub scripts do not ship",
    ),
    pytest.mark.skipif(shutil.which("git") is None, reason="git binary not found on PATH"),
]


def _run(env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_GATE)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, **(env or {})},
    )


def _tokens() -> list[str]:
    """Every token in the list, or skip. The list is untracked, so a runner may not have one."""
    if not _TOKENS.is_file():
        pytest.skip(f"no private-identifier list at {_TOKENS}")
    found = [
        tok
        for line in _TOKENS.read_text(encoding="utf-8").splitlines()
        if (tok := line.strip()) and not tok.startswith("#")
    ]
    if not found:
        pytest.skip(f"{_TOKENS} lists no tokens")
    return found


@contextlib.contextmanager
def _plant(target: Path, token: str):
    """Append a token to a tracked file, restoring the exact original bytes afterwards."""
    original = target.read_bytes()
    try:
        target.write_bytes(original + f"\n# planted-by-test {token}\n".encode())
        yield
    finally:
        target.write_bytes(original)


def test_the_script_is_present_and_executable():
    """The gate must be present and executable in the dev tree.

    CI does NOT run it (a runner cannot hold the untracked token list, and feeding one through
    a secret was declined — one of the two identifiers is a client's). oss-release.sh calls it
    directly before staging, so that is the single enforcement point and the only path to
    public. Pin the script's presence here, where it is cheap to notice.
    """
    assert _GATE.is_file(), "the leak gate script is missing from the dev tree"
    assert os.access(_GATE, os.X_OK), "the leak gate script is not executable"


def test_the_gate_is_green_on_a_clean_tree(synthetic):
    _token, env = synthetic
    result = _run(env)
    assert result.returncode == 0, f"gate red on a clean tree:\n{result.stdout}\n{result.stderr}"


def test_the_gate_source_carries_no_token():
    """The regression that item 1 exists to close: the answer key must not be in the gate."""
    source = _GATE.read_text(encoding="utf-8").lower()
    for tok in _tokens():
        assert tok.lower() not in source, "the gate's own source contains a private identifier"


def test_a_planted_identifier_in_a_shipped_file_turns_it_red(synthetic):
    token, env = synthetic
    with _plant(_SHIPPED, token):
        result = _run(env)
    assert result.returncode != 0, "gate stayed green with an identifier planted in a SHIPPED file"
    assert "PRIVATE IDENTIFIER" in result.stderr


def test_a_planted_identifier_in_an_unshipped_file_stays_green(synthetic):
    """Snapshot scoping is a decision, not an oversight — pin it so a revert is visible.

    An identifier in a file that never ships is not a public leak. The previous gate failed on
    it, which is why a private brief could turn the whole repo red.
    """
    token, env = synthetic
    with _plant(_UNSHIPPED, token):
        result = _run(env)
    assert result.returncode == 0, (
        "gate went red over an UNSHIPPED file; it should scan the snapshot only:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_a_missing_token_list_fails_closed(tmp_path):
    result = _run({"SYNCADE_PRIVATE_TOKENS": str(tmp_path / "nope")})
    assert result.returncode == 1
    assert "fails closed" in result.stderr


def test_an_empty_token_list_fails_closed(tmp_path):
    empty = tmp_path / "empty"
    empty.write_text("# only a comment\n", encoding="utf-8")
    result = _run({"SYNCADE_PRIVATE_TOKENS": str(empty)})
    assert result.returncode == 1
    assert "no tokens" in result.stderr


def test_token_with_trailing_whitespace_still_catches_identifier(tmp_path):
    """A token with trailing whitespace must still detect the bare identifier.

    A secret file with a trailing space on the token line causes the gate to search
    for "identifier " (with space) rather than "identifier", producing a false green
    when the bare token is planted in a shipped file.
    """
    tok = "syncade-probe-" + uuid.uuid4().hex
    token_file = tmp_path / "tokens-with-trailing-space"
    # Write token with trailing whitespace — common paste/editor artifact
    token_file.write_text(f"{tok}   \n", encoding="utf-8")
    with _plant(_SHIPPED, tok):
        result = _run({"SYNCADE_PRIVATE_TOKENS": str(token_file)})
    assert result.returncode != 0, (
        "gate stayed green when token file had trailing whitespace and the identifier "
        "was planted in a shipped file — the gate searched for 'token ' not 'token'"
    )
    assert "PRIVATE IDENTIFIER" in result.stderr
