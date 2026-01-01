"""Missing-command stderr extraction.

The shell-variant "command not found" patterns support
``_extract_missing_binary``, which the test runner uses to name the missing
binary when bash reports rc 127. Pure ``re``-only logic.
"""

from __future__ import annotations

import re

# shell-variant "command not found" patterns. Each
# regex captures the missing-command name from one shell family's
# stderr format. Lines must start with a shell name prefix (or a
# common shell binary path) to avoid false-positive matches
# against ordinary test output that happens to contain the
# substring "command not found".
#
# Variants covered:
# - bash:     ``bash: line 1: foo: command not found``
# - sh:       ``sh: foo: command not found``
# - dash:     ``sh: 1: foo: not found`` / ``dash: 1: foo: not found``
# - zsh:      ``zsh: command not found: foo``
# - busybox:  ``/bin/sh: foo: not found`` / ``ash: foo: not found``
# - generic:  ``<any-shell-path>: <prefix>: <cmd>: (command not found|not found)``
#
# A few defenses against false matches:
# - Each pattern anchors at start-of-line (``re.MULTILINE``).
# - The leading shell-name field accepts only word-class /
#   path-like characters — random prose with "command not found"
#   later in the line won't match.
# - The "zsh-reverse" pattern requires the literal sequence
#   ``command not found:`` (with trailing colon) so prose
#   sentences like "the command not found" don't match.
_SHELL_NAME_PREFIX = r"(?:[\w./-]*sh|bash|zsh|dash|ash|ksh|busybox)"
"""Trailing-``sh`` (including bare ``sh``) or any of the named
shells. Matches common on-disk paths too (``/bin/sh``,
``/usr/local/bin/zsh``). Uses ``[\\w./-]*sh`` (zero-or-more) so
the bare-shell-name prefix is included; ``[\\w./-]+sh`` would
require at least one character before ``sh`` and miss the most
common case."""

_SH_NOT_FOUND_PATTERNS = (
    # bash / sh form with optional line-number prefix:
    # "bash: line 1: foo: command not found"
    # "sh: foo: command not found"
    re.compile(
        rf"^{_SHELL_NAME_PREFIX}:\s*(?:line\s+\d+:\s*)?(?P<cmd>[^:\s][^:\n]*?):"
        r"\s*command not found\s*$",
        re.MULTILINE,
    ),
    # dash / busybox "not found" form (no "command "):
    # "sh: 1: foo: not found"
    # "/bin/sh: foo: not found"
    re.compile(
        rf"^{_SHELL_NAME_PREFIX}:\s*(?:\d+:\s*)?(?P<cmd>[^:\s][^:\n]*?):"
        r"\s*not found\s*$",
        re.MULTILINE,
    ),
    # zsh reversed form:
    # "zsh: command not found: foo"
    re.compile(
        rf"^{_SHELL_NAME_PREFIX}:\s*command not found:\s*(?P<cmd>\S+)\s*$",
        re.MULTILINE,
    ),
)


def _extract_missing_binary(stderr: str) -> str | None:
    """Pull the offending binary name out of a POSIX shell
    "command not found" stderr.

    Handles the major shell variants: bash, sh (POSIX), zsh, dash,
    ash, busybox. Each shell's diagnostic format is matched by a distinct regex in
    :data:`_SH_NOT_FOUND_PATTERNS`. The line-anchored shell-name
    prefix prevents false positives from prose containing the
    substring "command not found".

    Returns the binary name from the LAST match by line
    position (chronologically last shell diagnostic). The "last"
    rule preserves the operator's most-recent failure when
    multiple diagnostics are present — earlier-in-stderr
    diagnostics could be from an earlier subcommand that
    succeeded post-diagnostic (rare but possible in conditional
    pipelines). Returns ``None`` when no pattern matches; caller
    falls back to the full ``test_command`` string.
    """
    # Collect (start_pos, captured_command) tuples from every
    # pattern. Sort by start_pos descending so we return the
    # last-occurring diagnostic.
    matches: list[tuple[int, str]] = []
    for pattern in _SH_NOT_FOUND_PATTERNS:
        for m in pattern.finditer(stderr):
            matches.append((m.start(), m.group("cmd").strip()))
    if not matches:
        return None
    matches.sort(key=lambda t: t[0], reverse=True)
    return matches[0][1]
