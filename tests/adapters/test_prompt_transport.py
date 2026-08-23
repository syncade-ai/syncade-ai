"""The reviewer prompt travels on STDIN, never argv — PR-h-field-01 bug 1.

Found by dogfooding syncade on an unrelated repo. The reviewed branch had 12 committed
PNG screenshot baselines (an ordinary Playwright visual-regression suite). syncade builds
its diff with ``--text``, which renders those binaries as raw text — 65,961 B of real diff
became 3,129,026 B — and the prompt was passed as one argv element. ``execve`` refused:

    SubprocessError: failed to launch 'codex': [Errno 7] Argument list too long
    exit code: 40, both reviewers FAILED in 0.0s, no review ever happened

``--auth-check`` and ``--selfcheck`` had both passed 60s earlier; neither builds a prompt.

**This suite proves its own fixture is over the limit.** A regression test for an argv
ceiling is worthless if the fixture happens to fit — it would pass under the old transport
too, and read as proof while proving nothing. So
:func:`test_the_fixture_actually_exceeds_the_execve_ceiling` attempts the OLD transport and
requires it to fail. If a future OS raises the ceiling past the fixture, that test fails
loudly rather than letting the rest quietly become vacuous.
"""

from __future__ import annotations

import subprocess

import pytest

from syncade.adapters.anthropic import AnthropicAdapter
from syncade.adapters.openai import CodexAdapter
from syncade.adapters.producer_anthropic import AnthropicProducerAdapter
from syncade.adapters.producer_openai import OpenAIProducerAdapter
from syncade.config_producer import ProducerConfig
from syncade.process import run_subprocess
from tests.adapters._anthropic_helpers import _make_config

#: Comfortably over the ceiling measured on macOS 15/arm64 (1,044,480 B) and over the
#: 3.1 MB the reported run actually produced is not needed — 2 MiB already cannot be an
#: argv element anywhere this project runs. Kept as one string so it maps to one argv slot.
_OVERSIZE_PROMPT = "x" * (2 << 20)


def _reviewer_cases():
    return [
        ("anthropic", AnthropicAdapter(), _make_config(provider="anthropic")),
        (
            "openai",
            CodexAdapter(),
            _make_config(provider="openai", model="gpt-5.5", name="codex-reviewer"),
        ),
    ]


def _producer_cases():
    return [
        (
            "anthropic",
            AnthropicProducerAdapter(),
            ProducerConfig(
                provider="anthropic",
                model="claude-opus-4-7",
                thinking="medium",
                permissions="confined",
            ),
        ),
        (
            "openai",
            OpenAIProducerAdapter(),
            ProducerConfig(
                provider="openai",
                model="gpt-5.6-terra",
                thinking="medium",
                permissions="confined",
            ),
        ),
    ]


def _all_cases():
    return [(f"reviewer/{n}", a, c) for n, a, c in _reviewer_cases()] + [
        (f"producer/{n}", a, c) for n, a, c in _producer_cases()
    ]


@pytest.mark.parametrize("label,adapter,config", _all_cases())
def test_no_real_adapter_puts_the_prompt_in_argv(label, adapter, config, tmp_path):
    """Every real provider path, both roles. The acceptance criterion is `no provider path`."""
    inv = adapter.build_invocation(config, tmp_path, _OVERSIZE_PROMPT)

    assert inv.stdin_text == _OVERSIZE_PROMPT, f"{label}: prompt is not on stdin"
    assert _OVERSIZE_PROMPT not in inv.argv, f"{label}: prompt is an argv element"
    # Not just absent as a whole element — absent entirely. A future adapter that wraps it
    # (`--prompt=<text>`) would pass the check above and still blow the same ceiling.
    assert not any(_OVERSIZE_PROMPT in a for a in inv.argv), f"{label}: prompt embedded in argv"


@pytest.mark.parametrize("label,adapter,config", _all_cases())
def test_argv_stays_far_below_the_execve_ceiling(label, adapter, config, tmp_path):
    """argv size must be a function of config, not of the diff."""
    inv = adapter.build_invocation(config, tmp_path, _OVERSIZE_PROMPT)
    argv_bytes = sum(len(a.encode()) for a in inv.argv)

    assert argv_bytes < 8192, (
        f"{label}: argv is {argv_bytes:,} B — it scales with the prompt again, which is the "
        f"defect this suite exists to catch"
    )


def test_the_fixture_actually_exceeds_the_execve_ceiling():
    """The calibration, permanent rather than one-off.

    Attempts what the adapters USED to do — the prompt as one argv element — and requires
    the OS to refuse it. If this ever passes, the fixture no longer reproduces the bug and
    every other test in this file has quietly stopped proving anything.
    """
    with pytest.raises(OSError) as exc:
        subprocess.run(["/bin/echo", _OVERSIZE_PROMPT], capture_output=True)

    assert "Argument list too long" in str(exc.value) or exc.value.errno == 7, (
        f"expected E2BIG for a {len(_OVERSIZE_PROMPT):,} B argv element, got {exc.value!r}"
    )


def test_an_oversize_prompt_survives_the_round_trip_on_stdin(tmp_path):
    """End-to-end through the real subprocess choke point, not a mock.

    `/bin/cat` echoes stdin, so a payload that `execve` would refuse as an argument comes
    back intact as data. This is the property the fix depends on: the transport has no
    ceiling of its own.
    """
    result = run_subprocess(
        ["/bin/cat"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        timeout=60,
        input_text=_OVERSIZE_PROMPT,
    )

    assert result.returncode == 0, f"cat failed: {result.stderr[:200]}"
    assert result.stdout == _OVERSIZE_PROMPT, (
        f"stdin round-trip lost data: sent {len(_OVERSIZE_PROMPT):,} B, "
        f"got {len(result.stdout):,} B"
    )


@pytest.mark.parametrize("label,adapter,config", _all_cases())
def test_the_prompt_is_delivered_on_exactly_one_channel(label, adapter, config, tmp_path):
    """Both CLIs APPEND a piped stdin when a positional prompt is also present.

    `codex exec --help`: "If stdin is piped and a prompt is also provided, stdin is appended
    as a `<stdin>` block". Sending on both channels would hand the reviewer its instructions
    twice — not a crash, so nothing else would catch it.
    """
    marker = "UNIQUE-PROMPT-MARKER-7f3a"
    inv = adapter.build_invocation(config, tmp_path, marker)

    occurrences = sum(a.count(marker) for a in inv.argv) + (inv.stdin_text or "").count(marker)
    assert occurrences == 1, f"{label}: prompt delivered {occurrences} times, expected exactly 1"
