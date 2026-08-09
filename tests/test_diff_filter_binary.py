"""Binary content never reaches the reviewer prompt — PR-h-field-01 item 2.

`snapshot` renders the diff with `--text`, the only lever against attribute-driven
suppression. The cost is that a genuine binary is emitted as raw bytes: on the reported
repo, 12 committed PNG screenshot baselines turned 65,961 B of real diff into 3,129,026 B,
which no reviewer can read and which exceeds both providers' prompt ceilings.

**The measurement that shaped this file.** The brief said to drop hunks for paths "git
independently reports as binary". `git diff --numstat` looks like that oracle. It is not —
measured, it reports `-\t-` for a plain text file an attacker marked `*.py -diff` exactly
as it does for a real PNG, and `--text` does not change its answer. Filtering on git's
report would let a committed `.gitattributes` erase source changes from the reviewer's
diff, reopening the hole `--text` exists to close. So detection is by CONTENT (a NUL byte,
git's own heuristic), which no attributes file can forge.
"""

from __future__ import annotations

import subprocess

import pytest

from syncade.diff_filter import elide_binary_hunks, unidentifiable_sections

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, errors="replace", check=True
    ).stdout


def _repo(tmp_path, name="r"):
    repo = tmp_path / name
    repo.mkdir()
    for argv in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        _git(repo, *argv)
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _text_diff(repo):
    """The diff exactly as `snapshot` builds it — `--text` included."""
    return _git(
        repo, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--text", "HEAD~1..HEAD"
    )


def test_a_committed_binary_is_elided_and_the_path_is_disclosed(tmp_path):
    repo = _repo(tmp_path)
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 400)
    (repo / "app.py").write_text("def f():\n    return 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add binary + source")

    diff = _text_diff(repo)
    out, elided = elide_binary_hunks(diff)

    assert "\0" in diff, "fixture must actually contain binary bytes"
    assert elided == ["logo.png"]
    assert "\0" not in out, "binary bytes reached the reviewer diff"
    assert len(out) < len(diff) / 10, f"barely shrank: {len(diff)} -> {len(out)}"
    # Disclosed, not silently dropped.
    assert "logo.png" in out
    assert "binary file content omitted" in out
    # The real source change survives untouched.
    assert "def f():" in out and "return 2" in out


def test_an_attribute_suppressed_SOURCE_file_is_never_elided(tmp_path):
    """The attack `--text` exists to defeat, and the reason detection is content-based.

    An attacker commits `.gitattributes` with `*.py -diff`. `git diff --numstat` then
    reports that source file as binary — identical to a real PNG. Had the filter trusted
    that report, this change would vanish from the reviewer's diff.
    """
    repo = _repo(tmp_path)
    (repo / "payments.py").write_text("def transfer(x):\n    return x * 2  # backdoor\n")
    (repo / ".gitattributes").write_text("*.py -diff\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "suppressed source change")

    # Precondition: git DOES report it as binary. If this ever stops being true the test
    # is no longer exercising the attack.
    numstat = _git(repo, "diff", "--numstat", "HEAD~1..HEAD")
    assert "-\t-\tpayments.py" in numstat, f"attack precondition not met:\n{numstat}"

    out, elided = elide_binary_hunks(_text_diff(repo))

    assert elided == [], "an attributes file made syncade drop a SOURCE change"
    assert "def transfer(x):" in out, "the reviewer lost an attacker-suppressed source change"
    assert "backdoor" in out


def test_a_DELETED_binary_is_elided_too(tmp_path):
    """Deletions inflate identically — a PR that merely drops a vendored asset is as
    exposed as one that adds it, because `--text` emits the removed content in full."""
    repo = _repo(tmp_path)
    (repo / "font.woff2").write_bytes(b"wOF2" + bytes(range(256)) * 400)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add font")
    (repo / "font.woff2").unlink()
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "drop the vendored font")

    diff = _text_diff(repo)
    out, elided = elide_binary_hunks(diff)

    assert "\0" in diff, "fixture must contain the removed binary's bytes"
    assert elided == ["font.woff2"], f"a deleted binary was not elided: {elided}"
    assert "\0" not in out
    assert "x = 1" in out, "the real source change was lost"


def test_a_pure_text_diff_is_returned_byte_for_byte(tmp_path):
    """No NUL anywhere → the function must be a no-op, not a reformatter."""
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("def a():\n    return 1\n")
    (repo / "b.md").write_text("# heading\n\ntext\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "text only")

    diff = _text_diff(repo)
    out, elided = elide_binary_hunks(diff)

    assert elided == []
    assert out == diff, "a binary-free diff was modified"


def test_a_latin1_encoded_source_file_is_not_elided(tmp_path):
    """U+FFFD is deliberately NOT a binary signal.

    A Latin-1 encoded source file decodes to replacement characters under the UTF-8 read in
    `process.run_subprocess`. Treating that as binary would drop real source from review —
    a false positive that HIDES CODE, which is far worse than passing some noise through.
    """
    repo = _repo(tmp_path)
    (repo / "cafe.py").write_bytes("def caf\xe9():\n    return 'na\xefve'\n".encode("latin-1"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "latin-1 source")

    diff = _text_diff(repo)
    out, elided = elide_binary_hunks(diff)

    assert "�" in diff, "fixture must produce replacement chars"
    assert "\0" not in diff, "fixture must NOT contain NUL — that would make it truly binary"
    assert elided == [], "a Latin-1 source file was misread as binary and dropped"
    assert out == diff


def test_the_elided_diff_fits_under_the_provider_prompt_ceiling(tmp_path):
    """The end the whole item serves. codex exec refuses >1,048,576 characters
    (`input_too_large`); the reported repo produced 3.1 MB."""
    repo = _repo(tmp_path)
    baselines = repo / "tests" / "baselines"
    baselines.mkdir(parents=True)
    for i in range(12):
        (baselines / f"shot-{i:02d}.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 550
        )
    (repo / "app.py").write_text("def app():\n    return 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "visual regression baselines")

    diff = _text_diff(repo)
    out, elided = elide_binary_hunks(diff)

    assert len(diff) > 1_048_576, (
        f"fixture is only {len(diff):,} chars — it must exceed codex's ceiling or this "
        f"test proves nothing"
    )
    assert len(elided) == 12
    assert len(out) < 1_048_576, f"still over the provider ceiling at {len(out):,} chars"
    assert "return 2" in out, "the source change under review was lost"


# ── The orchestrator must actually USE the elider ───────────────────────────
#
# The calibration for this file initially showed a hole: removing the
# `elide_binary_hunks` call from round.py left every test above green, because they
# exercise the function directly and nothing asserted the wiring. A unit test for a
# filter proves the filter works, not that anything calls it.


def test_the_reviewer_prompt_itself_contains_no_binary_bytes(tmp_path):
    """End-to-end through the real prompt builder, not the filter in isolation."""
    from syncade.config import SyncadeConfig
    from syncade.orchestrator.round import _build_reviewer_prompt
    from syncade.snapshot import Snapshot

    binary_diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/logo.png b/logo.png\n"
        "--- a/logo.png\n+++ b/logo.png\n@@ -1 +1 @@\n"
        "+\x00\x01\x02BINARYPAYLOAD\x00\x00\n"
    )
    snapshot = Snapshot(
        repo_root=tmp_path,
        commit_sha="a" * 40,
        branch="feature",
        base_ref="HEAD",
        base_oid="b" * 40,
        diff_text=binary_diff,
        dirty_state="clean",
    )
    config = SyncadeConfig(
        reviewers=[{"name": "rv1", "provider": "openai", "model": "gpt-5.5"}],
        review={"strip_repo_context_files": []},
    )
    pr_doc = tmp_path / "spec.md"
    pr_doc.write_text("# Spec\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    prompts, _, _ = _build_reviewer_prompt(
        repo_root=tmp_path,
        snapshot=snapshot,
        config=config,
        round_idx=0,
        run_dir=run_dir,
        pr_doc_path=pr_doc,
    )
    rendered = "".join(prompts.values()) if isinstance(prompts, dict) else prompts

    assert "\0" not in rendered, "raw binary bytes reached the reviewer prompt"
    assert "BINARYPAYLOAD" not in rendered, "binary content reached the reviewer prompt"
    assert "binary file content omitted" in rendered, "the omission was not disclosed"
    assert "logo.png" in rendered, "the changed binary path was not disclosed"
    # The real source change must survive.
    assert "app.py" in rendered and "+new" in rendered


# ── CR-in-binary / fake boundary regression ─────────────────────────────────
#
# splitlines() treats \r as a line separator, so a binary payload containing
# \r immediately followed by "diff --git ..." would produce a fake section
# boundary. This caused two failure modes:
#   1. Bytes after a syntactically valid fake header reached the reviewer prompt.
#   2. A malformed fake header caused unidentifiable_sections() to return a
#      non-empty list, making _diff_will_dispatch() refuse a valid diff as
#      diff_malformed.


def test_cr_in_binary_payload_does_not_create_fake_diff_boundary(tmp_path):
    """A \r byte inside a binary section must not split into a fake diff --git header.

    This is the CR-bypass: a NUL-containing payload that also contains \r followed
    by 'diff --git ...' must still be elided in full, with no bytes leaking past
    the fake boundary to the reviewer prompt.
    """
    # Binary section with NUL (real binary marker) and a \r immediately before what
    # looks like a valid diff --git line. splitlines() would split there; split('\n')
    # must not.
    fake_boundary = "\rdiff --git a/legit.py b/legit.py\n"
    binary_section = (
        "diff --git a/image.png b/image.png\n"
        "--- a/image.png\n"
        "+++ b/image.png\n"
        "@@ -0,0 +1 @@\n"
        "+binary\x00payload" + fake_boundary + "+after_cr\x00more\n"
    )
    legit_section = (
        "diff --git a/legit.py b/legit.py\n"
        "--- a/legit.py\n"
        "+++ b/legit.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    diff = binary_section + legit_section

    assert "\0" in diff, "fixture must contain NUL bytes"

    out, elided = elide_binary_hunks(diff)

    assert "\0" not in out, "binary bytes (NUL) leaked past the CR fake boundary"
    assert "after_cr" not in out, "bytes after the CR fake boundary reached the output"
    assert "image.png" in elided or len(elided) > 0, "nothing was elided"
    # The legitimate section must survive intact.
    assert "+new" in out, "the real source change was lost"
    assert "legit.py" in out


def test_cr_in_binary_malformed_fake_header_does_not_cause_diff_malformed(tmp_path):
    """A \r followed by a syntactically MALFORMED 'diff --git' fragment must not be
    treated as an unidentifiable section header, which would cause _diff_will_dispatch
    to refuse a valid binary-heavy diff as diff_malformed.

    unidentifiable_sections() must not see the fake fragment as a real diff --git line.
    """
    # A binary payload whose \r is followed by a malformed (unidentifiable) diff header.
    # With splitlines(), this fragment appears as "diff --git NOTAVALIDPATH" to
    # unidentifiable_sections(). With split('\n') it stays inside the binary line.
    malformed_fake = "\rdiff --git NOTAVALIDPATH\n"
    diff = (
        "diff --git a/image.png b/image.png\n"
        "--- a/image.png\n"
        "+++ b/image.png\n"
        "@@ -0,0 +1 @@\n"
        "+binary\x00content" + malformed_fake + "diff --git a/real.py b/real.py\n"
        "--- a/real.py\n"
        "+++ b/real.py\n"
        "@@ -1 +1 @@\n"
        "-old\n+new\n"
    )

    bad = unidentifiable_sections(diff)
    assert bad == [], (
        f"A CR-prefixed fake 'diff --git' fragment was treated as an unidentifiable "
        f"section: {bad!r}. This would cause _diff_will_dispatch to refuse the diff."
    )
