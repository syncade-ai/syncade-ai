"""C-quoted path handling in the reviewer diff filter — PR-h-02c item 1, audit rank 6 (R5).

Reviewers are blind, and repo-context stripping is how that is enforced. Git C-quotes any
path containing a non-ASCII byte, a quote, a backslash, or a control character, and the
filter matched the RAW header text — so a strip target such as ``naïve.md`` never matched
its own ``"a/na\\303\\257ve.md"`` header and its content shipped to a blind reviewer.

Measured by reverting the fix and re-running these tests: **6 of the 8 shapes below leaked**
(non-ASCII, quote, backslash, tab, newline, and a non-ASCII DIRECTORY component). The
brief's earlier hand-probe said 4 of 6 — it missed the last two, which is why the
calibration is done against these tests rather than a one-off script.

A plain SPACE does not trigger quoting, so it is included as a control that passes BOTH
before and after: the intuitive "handle paths with spaces" fix would have addressed a case
that was never broken and missed every case that was.

Real git output is used throughout rather than hand-written headers, because the bug was
in the gap between what git emits and what the matcher expected.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from syncade.diff_filter import (
    _unquote_path,
    filter_diff_for_reviewer,
    unidentifiable_sections,
)

_SECRET = "SECRET REPO CONTEXT"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


class TestNoShapeLeaks:
    """Every path shape: the strip target's content must not reach the reviewer."""

    @pytest.mark.parametrize(
        "label,name",
        [
            ("ascii", "CLAUDE.md"),
            ("non_ascii", "naïve.md"),
            ("quote", 'we"ird.md'),
            ("backslash", "back\\slash.md"),
            ("tab", "ta\tb.md"),
            ("space", "my notes.md"),  # control: git does NOT quote a space
            ("newline", "new\nline.md"),
            ("non_ascii_dir", "dïr/inner.md"),
        ],
    )
    def test_target_is_stripped_and_unrelated_file_survives(self, tmp_path, label, name):
        repo = _repo(tmp_path)
        (repo / "keep.py").write_text("x = 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        base = _git(repo, "rev-parse", "HEAD").strip()

        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{_SECRET}\n")
        (repo / "keep.py").write_text("x = 2\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add context")

        diff = _git(repo, "diff", f"{base}..HEAD")
        filtered = filter_diff_for_reviewer(diff, [name.rsplit("/", 1)[-1]])

        assert _SECRET not in filtered, f"{label}: stripped file leaked to the reviewer"
        # Over-stripping is the opposite failure and just as wrong.
        assert "keep.py" in filtered

    def test_mixed_header_only_one_side_quoted(self, tmp_path):
        """A rename quotes ONLY the side that needs it — measured, and the shape that
        a both-quoted-or-both-bare pair of patterns misses entirely."""
        repo = _repo(tmp_path)
        (repo / "plain.md").write_text("body\n" * 10)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        base = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "mv", "plain.md", "naïve.md")
        _git(repo, "commit", "-qm", "rename")

        diff = _git(repo, "diff", "-M", f"{base}..HEAD")
        header = next(ln for ln in diff.splitlines() if ln.startswith("diff --git"))
        assert header.count('"') == 2, f"fixture is not a mixed header: {header}"

        assert "naïve" not in filter_diff_for_reviewer(diff, ["naïve.md"])


class TestAsciiBehaviourUnchanged:
    """Without this, item 1 would silently change every review the way three-dot nearly did."""

    def test_ascii_target_still_stripped_and_others_kept(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "CLAUDE.md").write_text("ctx\n")
        (repo / "app.py").write_text("a = 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        base = _git(repo, "rev-parse", "HEAD").strip()
        (repo / "CLAUDE.md").write_text("ctx2\n")
        (repo / "app.py").write_text("a = 2\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "edit")

        filtered = filter_diff_for_reviewer(_git(repo, "diff", f"{base}..HEAD"), ["CLAUDE.md"])
        assert "app.py" in filtered
        assert "ctx2" not in filtered

    def test_no_match_returns_input_byte_for_byte(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "app.py").write_text("a = 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        base = _git(repo, "rev-parse", "HEAD").strip()
        (repo / "app.py").write_text("a = 2\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "edit")

        diff = _git(repo, "diff", f"{base}..HEAD")
        assert filter_diff_for_reviewer(diff, ["CLAUDE.md"]) == diff


class TestUnquoteExactness:
    """Git's octal escapes are BYTES, not code points: ``ï`` is UTF-8 ``\\xc3\\xaf`` and is
    written ``\\303\\257``. Decoding per-character would produce mojibake that silently
    fails to match."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("a/plain.md", "a/plain.md"),  # bare token passes through untouched
            (r'"a/na\303\257ve.md"', "a/naïve.md"),
            (r'"a/ta\tb.md"', "a/ta\tb.md"),
            (r'"a/new\nline.md"', "a/new\nline.md"),
            (r'"a/we\"ird.md"', 'a/we"ird.md'),
            (r'"a/back\\slash.md"', "a/back\\slash.md"),
        ],
    )
    def test_decodes_to_the_real_filesystem_name(self, raw, expected):
        assert _unquote_path(raw) == expected

    def test_invalid_utf8_raises_value_error(self):
        """A path whose bytes are not valid UTF-8 must raise ValueError (via
        UnicodeDecodeError), so the section is treated as unidentifiable and
        dropped — not silently kept with a surrogate-containing name."""
        import pytest

        with pytest.raises(ValueError):
            _unquote_path(r'"a/bad\377byte.md"')


class TestFailClosed:
    """D1(a), PR-h-02c item 2: a file section whose identity cannot be established is
    DROPPED, reversing the previous choice to keep unparseable headers.

    An invisible leak of the blindness property is worse than a visible gap in a
    reviewer's context. The distinction that makes this safe is that a section which is
    not a ``diff --git`` header at all is preamble, not a file, and is still kept.
    """

    def test_unparseable_file_header_is_dropped(self):
        diff = "diff --git THIS IS NOT A VALID HEADER\n+SECRET\n"
        assert "SECRET" not in filter_diff_for_reviewer(diff, ["CLAUDE.md"])

    def test_malformed_escape_drops_instead_of_crashing(self):
        """Git's quoting never emits a malformed escape, but a TRUNCATED diff can
        (increment E caps diff size) — and a half-written header must not abort the run
        or become a leak."""
        diff = 'diff --git "a/x\\zy.md" "b/x\\zy.md"\n+SECRET\n'
        assert "SECRET" not in filter_diff_for_reviewer(diff, ["CLAUDE.md"])

    def test_preamble_is_still_kept(self):
        """The trap: fail-closed must not eat leading text that is not a file section."""
        diff = "leading preamble text\ndiff --git a/app.py b/app.py\n+x = 1\n"
        filtered = filter_diff_for_reviewer(diff, ["CLAUDE.md"])
        assert "leading preamble text" in filtered
        assert "app.py" in filtered

    def test_nothing_is_dropped_when_no_strip_targets_configured(self):
        diff = "diff --git THIS IS NOT A VALID HEADER\n+SECRET\n"
        assert filter_diff_for_reviewer(diff, []) == diff

    def test_invalid_utf8_path_drops_section(self):
        """A C-quoted path whose octal bytes are not valid UTF-8 must fail
        closed: the section is dropped rather than kept with a surrogate name
        that silently mismatches all configured strip targets."""
        # \377 = byte 0xFF, not valid UTF-8. Previously round-tripped via
        # surrogateescape and silently mismatched every strip target (kept).
        diff = 'diff --git "a/bad\\377byte.md" "b/bad\\377byte.md"\n+SECRET\n'
        assert "SECRET" not in filter_diff_for_reviewer(diff, ["keep.py"])

    def test_short_octal_escape_drops_section(self):
        """A C-quoted header ending in a short octal (< 3 digits) is malformed.
        It must not be silently kept — it must fail closed and be dropped."""
        # Body ends with \30 (2 digits), which git never emits but a truncated
        # diff can produce. Without validation this decoded to byte 0x18, failed
        # to match any strip target, and the section was kept (content leaked).
        diff = 'diff --git "a/x\\30" "b/x\\30"\n+SECRET\n'
        assert "SECRET" not in filter_diff_for_reviewer(diff, ["keep.py"])

    def test_quoted_token_without_git_prefix_drops_section(self):
        """A quoted diff header whose decoded path lacks the git a/ / b/ prefix
        is unverifiable and must be dropped fail-closed, not silently kept."""
        # "x/app.py" "y/app.py" is syntactically a valid quoted pair but
        # carries untrusted prefixes — without this check the section was kept
        # and its content reached the reviewer regardless of strip config.
        diff = 'diff --git "x/app.py" "y/app.py"\n+SECRET\n'
        assert "SECRET" not in filter_diff_for_reviewer(diff, ["keep.py"])

    def test_valid_quoted_prefixes_are_kept(self):
        """Confirm the prefix check does not drop legitimately-quoted paths."""
        diff = 'diff --git "a/na\\303\\257ve.md" "b/na\\303\\257ve.md"\n+content\n'
        result = filter_diff_for_reviewer(diff, ["CLAUDE.md"])
        assert "content" in result


class TestObservability:
    """Fail-closed drops emit a WARNING so callers can distinguish a malformed-
    header drop from a legitimate all-stripped diff."""

    def test_unparseable_header_logs_warning(self, caplog):
        import logging

        diff = "diff --git THIS IS NOT A VALID HEADER\n+content\n"
        with caplog.at_level(logging.WARNING, logger="syncade.diff_filter"):
            filter_diff_for_reviewer(diff, ["CLAUDE.md"])
        assert any("unidentifiable" in r.message for r in caplog.records)

    def test_malformed_encoding_logs_warning(self, caplog):
        import logging

        diff = 'diff --git "a/bad\\377byte.md" "b/bad\\377byte.md"\n+content\n'
        with caplog.at_level(logging.WARNING, logger="syncade.diff_filter"):
            filter_diff_for_reviewer(diff, ["CLAUDE.md"])
        assert any("malformed" in r.message for r in caplog.records)

    def test_untrusted_prefix_logs_warning(self, caplog):
        """A section dropped for an untrusted prefix must emit a WARNING."""
        import logging

        diff = 'diff --git "x/app.py" "y/app.py"\n+content\n'
        with caplog.at_level(logging.WARNING, logger="syncade.diff_filter"):
            filter_diff_for_reviewer(diff, ["keep.py"])
        assert any("untrusted" in r.message for r in caplog.records)

    def test_normal_drop_does_not_log(self, caplog):
        """Stripping a known target must not emit a warning — it is expected."""
        import logging

        diff = "diff --git a/CLAUDE.md b/CLAUDE.md\n+content\n"
        with caplog.at_level(logging.WARNING, logger="syncade.diff_filter"):
            filter_diff_for_reviewer(diff, ["CLAUDE.md"])
        assert caplog.records == []


_ALL_HEADER_SHAPES: dict[str, str] = {
    "valid_bare": "diff --git a/app.py b/app.py",
    "valid_quoted_non_ascii": 'diff --git "a/na\\303\\257ve.md" "b/na\\303\\257ve.md"',
    "valid_mixed": 'diff --git a/plain.md "b/na\\303\\257ve.md"',
    "valid_space": "diff --git a/my notes.md b/my notes.md",
    "unparseable": "diff --git THIS IS NOT VALID",
    "malformed_escape": 'diff --git "a/x\\3" "b/x\\3"',
    "invalid_utf8": 'diff --git "a/bad\\377b.md" "b/bad\\377b.md"',
    "untrusted_a_prefix": 'diff --git "x/app.py" "b/app.py"',
    "untrusted_b_prefix": 'diff --git "a/app.py" "y/app.py"',
    # A destination path containing " b/" makes the bare header ambiguous:
    # `git mv CLAUDE.md 'foo b/bar.py'` emits this header and git does not quote spaces.
    # The greedy regex yields a='CLAUDE.md b/foo' / b='bar.py' — wrong basename on both
    # sides — so the strip check would have passed the section and leaked source content.
    "ambiguous_bare_sep": "diff --git a/CLAUDE.md b/foo b/bar.py",
}
_UNIDENTIFIABLE = {
    "unparseable",
    "malformed_escape",
    "invalid_utf8",
    "untrusted_a_prefix",
    "untrusted_b_prefix",
    "ambiguous_bare_sep",
}


class TestUnidentifiableSections:
    """PR-h-02d item 2: report WHICH sections were dropped fail-closed.

    A diff can filter to empty for two opposite reasons — everything was legitimately
    repo-context, or we could not read it. Without this signal the caller cannot tell an
    honest no-change result from shipping a change BECAUSE it was unreadable (D2).
    """

    @pytest.mark.parametrize("label", sorted(_ALL_HEADER_SHAPES))
    def test_classification(self, label):
        diff = _ALL_HEADER_SHAPES[label] + "\n+content\n"
        assert bool(unidentifiable_sections(diff)) is (label in _UNIDENTIFIABLE)

    @pytest.mark.parametrize("label", sorted(_ALL_HEADER_SHAPES))
    def test_agrees_with_what_the_filter_actually_drops(self, label):
        """The anti-drift property. Two separate notions of "identifiable" would
        eventually disagree — a leak on one side, a spurious refusal on the other. Both
        consumers route through ``_decode_header``, and this asserts they stay in step.

        The strip list deliberately matches nothing, so any section the filter removes
        can only have been removed fail-closed.
        """
        diff = _ALL_HEADER_SHAPES[label] + "\n+content\n"
        reported = bool(unidentifiable_sections(diff))
        dropped = "content" not in filter_diff_for_reviewer(diff, ["nothing-matches.md"])
        assert reported == dropped

    def test_returns_the_header_text_so_a_refusal_can_name_it(self):
        diff = 'diff --git "a/x\\3" "b/x\\3"\n+content\n'
        assert unidentifiable_sections(diff) == ['diff --git "a/x\\3" "b/x\\3"']

    def test_a_legitimate_strip_target_is_identifiable(self):
        """3a (all repo-context) must be distinguishable from 3b (unreadable)."""
        diff = "diff --git a/CLAUDE.md b/CLAUDE.md\n+ctx\n"
        assert unidentifiable_sections(diff) == []
        assert "ctx" not in filter_diff_for_reviewer(diff, ["CLAUDE.md"])

    def test_clean_diff_and_preamble_report_nothing(self):
        clean = "diff --git a/a.py b/a.py\n+1\ndiff --git a/b.py b/b.py\n+2\n"
        assert unidentifiable_sections(clean) == []
        assert filter_diff_for_reviewer(clean, ["CLAUDE.md"]) == clean
        assert unidentifiable_sections("leading preamble\nmore\n") == []


class TestAmbiguousBareSeparator:
    """Destination paths containing ' b/' produce ambiguous bare headers and must not leak.

    ``git mv CLAUDE.md 'foo b/bar.py'`` emits ``diff --git a/CLAUDE.md b/foo b/bar.py``.
    Git does NOT quote spaces, so the header is bare on both sides.  The greedy regex splits
    at the LAST ' b/', yielding a-side='CLAUDE.md b/foo' / b-side='bar.py'; the a-side
    basename is then 'foo', not 'CLAUDE.md', so the strip check passes the section through
    and the source name plus hunk content reach the reviewer.

    The fix fails closed: a bare a-side match that contains ' b/' implies multiple valid
    split points; the header is treated as unidentifiable and the section is dropped.
    Parametrised coverage is already carried by 'ambiguous_bare_sep' in _ALL_HEADER_SHAPES /
    _UNIDENTIFIABLE above; this class adds the source-doesn't-leak assertion.
    """

    _DIFF = (
        "diff --git a/CLAUDE.md b/foo b/bar.py\n"
        "similarity index 100%\n"
        "rename from CLAUDE.md\n"
        "rename to foo b/bar.py\n"
        "--- a/CLAUDE.md\n"
        "+++ b/foo b/bar.py\n"
        "@@ -1 +1 @@\n"
        " SECRET memory\n"
    )

    def test_source_name_and_content_withheld(self):
        """The section is dropped fail-closed; source identity must not reach the reviewer."""
        filtered = filter_diff_for_reviewer(self._DIFF, ["CLAUDE.md"])
        assert "SECRET" not in filtered, "source content leaked through ambiguous header"
        assert "CLAUDE.md" not in filtered, "source name leaked through ambiguous header"

    def test_unambiguous_space_path_is_still_parseable(self):
        """A space in a path is fine when it does not create a second ' b/' separator."""
        diff = "diff --git a/my notes.md b/my notes.md\n+content\n"
        assert unidentifiable_sections(diff) == []
        assert "content" in filter_diff_for_reviewer(diff, ["CLAUDE.md"])
