"""Producer confinement policy: ``confined`` is the DEFAULT, ``yolo`` is a disclosed opt-out.

PR-h-05 Item 2 first narrowed ``ProducerPermissions`` to ``Literal["confined"]``. That broke every
config ever written against a shipped release (``yolo`` was the only accepted value through 0.7.6)
and it went further than Item 2's own decision gate required — the gate says *record the exact
mode and proceed*, not *delete every other mode*.

Confinement costs something real: the sandbox can only auto-approve SANDBOXED Bash, so a confined
``claude`` producer runs Bash-only and edits through the shell. Whether that fixes as well as a
tool-equipped producer is unmeasured, and that trade belongs to the operator. So ``yolo`` stays
available — and, because it removes the host-confinement layer ``confined`` adds on top,
it is announced on every run that can dispatch a producer.

Split into its own file rather than added to the near-cap ``test_config_schema.py``.
"""

import pytest
from pydantic import ValidationError

from syncade.adapters.producer_anthropic import AnthropicProducerAdapter
from syncade.adapters.producer_openai import OpenAIProducerAdapter
from syncade.config import ProducerConfig, ProducerPermissions


def test_confined_is_the_default_and_yolo_is_available():
    assert set(ProducerPermissions.__args__) == {"confined", "yolo"}
    assert ProducerConfig().permissions == "confined", (
        "the SAFE mode must be what doing nothing gives"
    )
    for value in ("confined", "yolo"):
        assert ProducerConfig(permissions=value).permissions == value


def test_a_config_written_against_a_shipped_release_still_loads():
    """The 0.7.6 operator config must not be invalidated by this PR.

    `yolo` was the only value any released syncade accepted; refusing it would have made every
    existing config unloadable — and the documented repair (`--config set`) was itself refused by
    the invalid config until `tests/cli/test_config_repair.py`'s fixes landed.
    """
    assert ProducerConfig(provider="anthropic", permissions="yolo").permissions == "yolo"


def test_unsupported_values_still_fail_closed():
    for value in ("safe", "trusted-execute", "bypassPermissions", ""):
        with pytest.raises(ValidationError):
            ProducerConfig(permissions=value)


class TestAdapterArgvHonoursThePolicy:
    """Both modes must be REACHABLE and must differ in the flags that actually enforce."""

    def test_anthropic(self, tmp_path):
        confined = (
            AnthropicProducerAdapter()
            .build_invocation(
                ProducerConfig(provider="anthropic", permissions="confined"), tmp_path, "p"
            )
            .argv
        )
        yolo = (
            AnthropicProducerAdapter()
            .build_invocation(
                ProducerConfig(provider="anthropic", permissions="yolo"), tmp_path, "p"
            )
            .argv
        )

        assert "dontAsk" in confined and "--settings" in confined
        # Bash-only is FORCED by the sandbox (nothing else can be auto-approved, and `claude -p`
        # cannot answer a prompt) — it is the measured cost of confinement, so it is pinned.
        assert confined[confined.index("--tools") + 1] == "Bash"

        assert "bypassPermissions" in yolo
        assert "--settings" not in yolo, "yolo must not claim a sandbox it does not enable"
        assert "--tools" not in yolo, "the tool restriction exists only to serve the sandbox"

    def test_openai(self, tmp_path):
        confined = (
            OpenAIProducerAdapter()
            .build_invocation(
                ProducerConfig(provider="openai", permissions="confined"), tmp_path, "p"
            )
            .argv
        )
        yolo = (
            OpenAIProducerAdapter()
            .build_invocation(ProducerConfig(provider="openai", permissions="yolo"), tmp_path, "p")
            .argv
        )

        assert "workspace-write" in confined
        assert "--dangerously-bypass-approvals-and-sandbox" not in confined
        # The actor's own .git as an additional writable root is what Item 3's standalone
        # repository made possible and what PR-8 R2.T7 could not have.
        assert any(str(tmp_path / ".git") in arg for arg in confined)

        assert "--dangerously-bypass-approvals-and-sandbox" in yolo
        assert "workspace-write" not in yolo
