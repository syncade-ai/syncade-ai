"""``[update]`` config block (PR-h-field-07 item 2): the update check's single off switch.

A LEAF module — pydantic only — matching :mod:`syncade.config_gc` and :mod:`syncade.config_retry`.
It deliberately does NOT import :mod:`syncade.update_check`, which would be harmless today but
would put a network-capable module on the config import path.

There is ONE key on purpose. The advisory SOURCE is not configurable: a repo-local file that
could repoint or silence a security notice is a hole, not a feature.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class UpdateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: bool = Field(
        default=True,
        description=(
            "Check once per session whether a newer syncade is published (default true). One "
            "HTTPS GET to the public repo's update manifest, 1.5s timeout, at the first syncade "
            "invocation in a window and never again in that window. It only ever PRINTS: it "
            "cannot block a run, change an exit code, or fail one — every error is silent. "
            "Nothing about your repo, diff, run, or identity is sent. Also skipped whenever CI "
            "is set."
        ),
    )
