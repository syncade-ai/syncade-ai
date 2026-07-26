"""Reviewer permission bounds (pr-v2-31): ``safe`` is not a runnable reviewer tier.

Split into its own file rather than added to the at-cap ``test_config_schema.py``.
"""

import pytest
from pydantic import ValidationError

from syncade.config import Permissions, ReviewerPermissions, SyncadeConfig


def test_permissions_importable_from_syncade_config():
    """``Permissions`` must be importable from ``syncade.config`` (the stable public surface).

    ``Permissions`` was split to ``config_types`` in PR-v2-23 but the re-export in
    ``config.py``'s ``__all__`` must keep ``from syncade.config import Permissions`` working.
    """
    assert "safe" in Permissions.__args__
    assert "trusted-execute" in Permissions.__args__
    assert "yolo" in Permissions.__args__
    # ReviewerPermissions is the narrowed subset — safe excluded
    assert "safe" not in ReviewerPermissions.__args__


_BASE = {"name": "r", "provider": "openai", "model": "gpt-5.5"}


def test_reviewer_permissions_safe_rejected():
    """Reviewers reject ``permissions='safe'`` at config-load — the real adapters refuse it (it
    prompts and hangs a headless subprocess), so ``ReviewerPermissions`` excludes it, mirroring
    ``ProducerPermissions``. ``trusted-execute`` / ``yolo`` remain valid."""
    with pytest.raises(ValidationError):
        SyncadeConfig(reviewers=[{**_BASE, "permissions": "safe"}])
    for ok in ("trusted-execute", "yolo"):
        cfg = SyncadeConfig(reviewers=[{**_BASE, "permissions": ok}])
        assert cfg.reviewers[0].permissions == ok
