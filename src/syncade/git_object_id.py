from __future__ import annotations

from typing import Final

_SHA1_HEX_LENGTH: Final = 40
_SHA256_HEX_LENGTH: Final = 64


def is_full_git_object_id(value: str) -> bool:
    return len(value) in {_SHA1_HEX_LENGTH, _SHA256_HEX_LENGTH} and all(
        char in "0123456789abcdefABCDEF" for char in value
    )
