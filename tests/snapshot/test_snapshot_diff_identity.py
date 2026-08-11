"""PR-h-02 increment A — the diff describes ONE known commit range, and the
repository cannot choose what a reviewer sees.

Both defects were reproduced against `6bb2890` before the fix:

- a repo-configured ``diff.external`` EXECUTED, and its stdout became
  ``Snapshot.diff_text`` — i.e. the reviewers' entire view of the change;
- the diff was taken against symbolic ``HEAD``, re-resolved at diff time, so a
  commit landing between the HEAD capture and the diff produced a diff
  describing a different commit than ``Snapshot.commit_sha``. The producer
  commits to this repo, so that race is ordinary operation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import syncade.snapshot as snapshot_mod
from syncade.snapshot import SnapshotError, take_snapshot
from tests.snapshot._helpers import _commit, _git, _init_repo


@pytest.fixture
def base_and_change(tmp_path: Path) -> tuple[Path, str]:
    """Repo with a base commit and one change on top. Returns (repo, base_sha)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(repo, {"a.txt": "one\n"}, "base")
    _commit(repo, {"a.txt": "two\n"}, "change")
    return repo, base


class TestRepositoryCannotChooseWhatReviewersSee:
    def test_diff_external_does_not_execute(self, base_and_change: tuple[Path, str]) -> None:
        """`diff.external` hands the diff to an arbitrary program and uses ITS
        stdout. Pre-fix the program ran and its output reached the reviewers."""
        repo, base = base_and_change
        marker = repo / "EXTERNAL_DIFF_RAN"
        script = repo / "fake-diff.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\necho 'ATTACKER CONTROLLED'\n")
        script.chmod(0o755)
        _git(repo, "config", "diff.external", str(script))

        snap = take_snapshot(repo, base_ref=base)

        assert not marker.exists(), "diff.external executed"
        assert "ATTACKER CONTROLLED" not in snap.diff_text
        assert "-one" in snap.diff_text and "+two" in snap.diff_text

    def test_textconv_filter_does_not_run(
        self, base_and_change: tuple[Path, str], tmp_path: Path
    ) -> None:
        """`textconv` is the same hole per-path: git replaces file content with
        a filter's stdout before diffing.

        The script lives OUTSIDE the repo — committing it would put its own
        source in the diff and make the assertion vacuously fail.
        """
        repo, base = base_and_change
        marker = tmp_path / "TEXTCONV_RAN"
        script = tmp_path / "fake-textconv.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\necho 'FILTERED CONTENT'\n")
        script.chmod(0o755)
        _git(repo, "config", "diff.evil.textconv", str(script))
        (repo / ".gitattributes").write_text("*.txt diff=evil\n")
        _commit(repo, {"a.txt": "three\n"}, "another change")

        snap = take_snapshot(repo, base_ref=base)

        assert not marker.exists(), "textconv filter executed"
        assert "FILTERED CONTENT" not in snap.diff_text
        assert "+three" in snap.diff_text, "real file content must still be diffed"


class TestDiffAndCommitShaNameOneCommit:
    def test_commit_landing_mid_snapshot_cannot_enter_the_diff(
        self, base_and_change: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The race, made deterministic: land a commit between the HEAD capture
        and the diff call. The diff must describe the CAPTURED commit."""
        repo, base = base_and_change
        real_git = snapshot_mod._git
        fired: list[bool] = []

        def racing_git(repo_root: Path, *args: str):
            if "diff" in args and not fired:
                fired.append(True)
                _commit(repo_root, {"a.txt": "RACED\n"}, "raced-in commit")
            return real_git(repo_root, *args)

        monkeypatch.setattr(snapshot_mod, "_git", racing_git)
        snap = take_snapshot(repo, base_ref=base)

        assert fired, "fixture did not actually race a commit in"
        assert "RACED" not in snap.diff_text
        # And the diff is exactly the range the snapshot claims.
        expected = _git(
            repo,
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            f"{base}..{snap.commit_sha}",
        ).stdout
        assert snap.diff_text == expected

    def test_annotated_tag_base_is_peeled_to_its_commit(self, tmp_path: Path) -> None:
        """`^{commit}` peels an annotated tag — which `git diff` did implicitly.
        Resolving it explicitly must not change the diff."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, {"a.txt": "one\n"}, "base")
        _git(repo, "tag", "-a", "v1", "-m", "release")
        _commit(repo, {"a.txt": "two\n"}, "change")

        snap = take_snapshot(repo, base_ref="v1")

        assert "-one" in snap.diff_text and "+two" in snap.diff_text

    def test_unresolvable_base_still_raises_named_error(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, {"a.txt": "one\n"}, "base")
        with pytest.raises(SnapshotError, match="does not resolve"):
            take_snapshot(repo, base_ref="no-such-ref")

    def test_base_equal_to_head_yields_empty_diff(self, tmp_path: Path) -> None:
        """Guards increment A against changing the empty-diff shape; making
        empty distinguishable from absent is increment D's job, not this one."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        sha = _commit(repo, {"a.txt": "one\n"}, "base")
        assert take_snapshot(repo, base_ref=sha).diff_text == ""


class TestAdversarialReviewFindings:
    """Found by adversarial review AFTER the first cut of increment A, which
    claimed "no repository-controlled config can alter the bytes" and was wrong
    on four counts. Every vector below was reproduced before being closed."""

    def test_refs_replace_cannot_substitute_the_reviewed_commit(self, tmp_path: Path) -> None:
        """`refs/replace/*` swaps one object for another at every lookup and
        lives in the shared common dir, so it is writable from a producer
        worktree. Pre-fix: a backdoored commit was reviewed as its benign
        replacement while the SHA landing upstream still carried the backdoor.
        """
        repo = tmp_path / "repo"
        _init_repo(repo)
        base = _commit(repo, {"s.py": "orig\n"}, "base")
        bad = _commit(repo, {"s.py": "BACKDOOR\n"}, "real")
        benign = _commit(repo, {"s.py": "tidy\n"}, "benign")
        _git(repo, "reset", "-q", "--hard", bad)
        _git(repo, "replace", "-f", bad, benign)

        snap = take_snapshot(repo, base_ref=base)

        assert snap.commit_sha == bad
        assert "BACKDOOR" in snap.diff_text, "diff described the replacement, not commit_sha"

    @pytest.mark.parametrize(
        "vector",
        ["bigfile", "info_attributes", "attributes_file", "committed_gitattributes"],
    )
    def test_content_suppression_vectors_cannot_hide_the_change(
        self, tmp_path: Path, vector: str
    ) -> None:
        """Four ways to collapse a whole change to `Binary files ... differ`.
        Two are config; two are attributes files that no config key reaches —
        `--text` is the only lever against those."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        base = _commit(repo, {"s.py": "def a():\n    return check()\n"}, "base")
        _commit(repo, {"s.py": "def a():\n    return True  # BACKDOOR\n"}, "change")

        if vector == "bigfile":
            _git(repo, "config", "core.bigFileThreshold", "1")
        elif vector == "info_attributes":
            info = repo / ".git" / "info"
            info.mkdir(parents=True, exist_ok=True)
            (info / "attributes").write_text("* -diff\n")
        elif vector == "attributes_file":
            (tmp_path / "attrs").write_text("* -diff\n")
            _git(repo, "config", "core.attributesFile", str(tmp_path / "attrs"))
        else:
            _commit(repo, {".gitattributes": "* -diff\n"}, "attrs")

        snap = take_snapshot(repo, base_ref=base)

        assert "BACKDOOR" in snap.diff_text, f"{vector} suppressed the change"

    def test_custom_diff_prefixes_cannot_defeat_the_strip_filter(self, tmp_path: Path) -> None:
        """A blindness leak, not just byte drift: the strip filter matches
        `a/`/`b/` headers, so repo-set `diff.srcPrefix`/`dstPrefix` let
        repo-context files through to blind reviewers."""
        from syncade.diff_filter import REVIEWER_STRIP_FILES, filter_diff_for_reviewer

        repo = tmp_path / "repo"
        _init_repo(repo)
        base = _commit(repo, {"CLAUDE.md": "old\n"}, "base")
        _commit(repo, {"CLAUDE.md": "SECRET PRODUCER NARRATIVE\n"}, "change")
        _git(repo, "config", "diff.srcPrefix", "EVIL/")
        _git(repo, "config", "diff.dstPrefix", "EVIL/")

        snap = take_snapshot(repo, base_ref=base)
        filtered = filter_diff_for_reviewer(snap.diff_text, REVIEWER_STRIP_FILES)

        assert "SECRET" not in filtered, "CLAUDE.md leaked to blind reviewers"

    @pytest.mark.parametrize(
        "key,value",
        [
            ("diff.context", "0"),
            ("diff.interHunkContext", "40"),
            ("core.abbrev", "40"),
            ("diff.algorithm", "patience"),
        ],
    )
    def test_repo_config_cannot_rewrite_the_diff_bytes(
        self, tmp_path: Path, key: str, value: str
    ) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        base = _commit(repo, {"s.py": "\n".join(f"line {i}" for i in range(40)) + "\n"}, "base")
        body = "\n".join(("CHANGED" if i in (5, 30) else f"line {i}") for i in range(40)) + "\n"
        _commit(repo, {"s.py": body}, "change")

        before = take_snapshot(repo, base_ref=base).diff_text
        _git(repo, "config", key, value)
        after = take_snapshot(repo, base_ref=base).diff_text

        assert before == after, f"{key}={value} changed the diff reviewers see"

    def test_commit_message_search_base_still_works(self, tmp_path: Path) -> None:
        """Regression caught by adversarial review: appending `^{commit}` to the
        raw ref broke git's `:/<text>` search, which consumes the rest of the
        string as a regex. Resolve the ref first, then peel the OID."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, {"s.py": "one\n"}, "alpha marker")
        _commit(repo, {"s.py": "two\n"}, "later work")

        snap = take_snapshot(repo, base_ref=":/alpha")

        assert "-one" in snap.diff_text and "+two" in snap.diff_text

    def test_core_quotepath_false_does_not_change_diff_bytes(self, tmp_path: Path) -> None:
        """`core.quotePath=false` changes non-ASCII path headers, making the diff
        bytes dependent on repo config.  Pre-fix: the key was not pinned, so the
        reviewer-visible path could differ from the canonical C-quoted form."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        # Non-ASCII filename: git C-quotes it by default; with quotePath=false
        # it emits the raw bytes instead.
        base = _commit(repo, {"café.py": "a = 1\n"}, "base")
        _commit(repo, {"café.py": "a = 2\n"}, "change")

        before = take_snapshot(repo, base_ref=base).diff_text
        _git(repo, "config", "core.quotePath", "false")
        after = take_snapshot(repo, base_ref=base).diff_text

        assert before == after, "core.quotePath=false changed the diff bytes reviewers see"

    def test_diff_renames_false_does_not_change_diff_bytes(self, tmp_path: Path) -> None:
        """`diff.renames=false` turns rename detection off, expanding a rename
        into a full delete+add pair and changing the diff bytes materially."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        base = _commit(repo, {"old_name.py": "x = 1\n"}, "base")
        # Rename: git should detect similarity and emit a rename header
        (repo / "old_name.py").rename(repo / "new_name.py")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "rename")

        before = take_snapshot(repo, base_ref=base).diff_text
        _git(repo, "config", "diff.renames", "false")
        after = take_snapshot(repo, base_ref=base).diff_text

        assert before == after, "diff.renames=false changed the diff bytes reviewers see"

    def test_diff_ignoresubmodules_all_does_not_suppress_submodule_change(
        self, tmp_path: Path
    ) -> None:
        """`diff.ignoreSubmodules=all` hides submodule pointer bumps entirely,
        turning a non-empty diff into the empty string."""
        # Create the submodule repo with only v1 so that `submodule add` below
        # pins the outer repo to v1 (HEAD at add time).
        sub_repo = tmp_path / "sub"
        _init_repo(sub_repo)
        _commit(sub_repo, {"x.py": "v1\n"}, "sub-v1")

        # Build the outer repo with the submodule at v1.
        # `-c protocol.file.allow=always` is required on recent git versions that
        # block file-protocol clones by default; only affects test setup, not the
        # production code path under test.
        outer = tmp_path / "outer"
        _init_repo(outer)
        _git(outer, "-c", "protocol.file.allow=always", "submodule", "add", str(sub_repo), "sub")
        _git(outer, "commit", "-m", "add sub")
        base = _git(outer, "rev-parse", "HEAD").stdout.strip()

        # NOW commit v2 in the sub repo, then bump the outer pointer to it.
        sub_v2_sha = _commit(sub_repo, {"x.py": "v2\n"}, "sub-v2")
        _git(outer / "sub", "fetch", "-q", "origin")
        _git(outer / "sub", "checkout", "-q", sub_v2_sha)
        _git(outer, "add", "sub")
        _git(outer, "commit", "-m", "bump sub")

        before = take_snapshot(outer, base_ref=base).diff_text
        assert before != "", "submodule diff was unexpectedly empty — fixture broken"
        _git(outer, "config", "diff.ignoreSubmodules", "all")
        after = take_snapshot(outer, base_ref=base).diff_text

        assert before == after, "diff.ignoreSubmodules=all suppressed the submodule change"

    def test_replacement_ref_on_base_does_not_block_snapshot(self, tmp_path: Path) -> None:
        """`refs/replace/<base-sha>` pointing to a non-commit object makes
        `git rev-parse <base-sha>^{commit}` fail (can't peel a blob to commit).
        With `--no-replace-objects`, base peeling uses the original object
        directly and snapshot succeeds."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        base = _commit(repo, {"a.py": "v1\n"}, "base")
        _commit(repo, {"a.py": "v2\n"}, "change")
        # Write a blob and install a replace ref pointing the base commit to it.
        (repo / "_src").write_text("ATTACK\n")
        blob_sha = _git(repo, "hash-object", "-w", "_src").stdout.strip()
        _git(repo, "update-ref", f"refs/replace/{base}", blob_sha)

        # Must succeed and produce the correct diff, not raise SnapshotError.
        snap = take_snapshot(repo, base_ref=base)
        assert "-v1" in snap.diff_text and "+v2" in snap.diff_text

    def test_replacement_ref_on_head_does_not_change_commit_sha(self, tmp_path: Path) -> None:
        """`refs/replace/<HEAD-sha>` poisons object-reading commands (`log`,
        `merge-base`, `diff`) but NOT `git rev-parse HEAD`, which resolves
        a ref name to a SHA without reading the object.  `--no-replace-objects`
        (belt-and-braces) plus the structural `GIT_NO_REPLACE_OBJECTS=1` env
        ensure `Snapshot.commit_sha` is the real HEAD in either case."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        base = _commit(repo, {"a.py": "v1\n"}, "base")
        real_head = _commit(repo, {"a.py": "v2\n"}, "change")
        benign = _commit(repo, {"a.py": "tidy\n"}, "benign replacement")
        _git(repo, "reset", "-q", "--hard", real_head)
        _git(repo, "replace", "-f", real_head, benign)

        snap = take_snapshot(repo, base_ref=base)
        assert snap.commit_sha == real_head, "commit_sha was the replacement, not the real HEAD"

    def test_diff_suppressblankempty_true_does_not_change_diff_bytes(self, tmp_path: Path) -> None:
        """`diff.suppressBlankEmpty=true` strips the trailing space from blank
        context lines, changing the literal bytes of the diff."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        # File with a blank line in the middle so context lines include it.
        base = _commit(repo, {"s.py": "a = 1\n\nb = 2\n"}, "base")
        _commit(repo, {"s.py": "a = 1\n\nb = 3\n"}, "change")

        before = take_snapshot(repo, base_ref=base).diff_text
        _git(repo, "config", "diff.suppressBlankEmpty", "true")
        after = take_snapshot(repo, base_ref=base).diff_text

        assert before == after, "diff.suppressBlankEmpty=true changed the diff bytes reviewers see"

    def test_diff_submodule_log_does_not_rewrite_submodule_diff(self, tmp_path: Path) -> None:
        """`diff.submodule=log` replaces the normal `--- /dev/null` pointer-bump
        patch with a multi-line `Submodule sub ...` log entry, changing diff
        bytes materially."""
        sub_repo = tmp_path / "sub"
        _init_repo(sub_repo)
        _commit(sub_repo, {"x.py": "v1\n"}, "sub-v1")

        outer = tmp_path / "outer"
        _init_repo(outer)
        _git(outer, "-c", "protocol.file.allow=always", "submodule", "add", str(sub_repo), "sub")
        _git(outer, "commit", "-m", "add sub")
        base = _git(outer, "rev-parse", "HEAD").stdout.strip()

        sub_v2_sha = _commit(sub_repo, {"x.py": "v2\n"}, "sub-v2")
        _git(outer / "sub", "fetch", "-q", "origin")
        _git(outer / "sub", "checkout", "-q", sub_v2_sha)
        _git(outer, "add", "sub")
        _git(outer, "commit", "-m", "bump sub")

        before = take_snapshot(outer, base_ref=base).diff_text
        _git(outer, "config", "diff.submodule", "log")
        after = take_snapshot(outer, base_ref=base).diff_text

        assert before == after, "diff.submodule=log rewrote the submodule diff reviewers see"

    def test_gitmodules_ignore_all_does_not_suppress_submodule_change(self, tmp_path: Path) -> None:
        """A committed `.gitmodules` with `ignore = all` suppresses a submodule
        pointer bump even when `diff.ignoreSubmodules=none` is set in git
        config.  The explicit `--ignore-submodules=none` CLI flag must override
        this per-submodule setting."""
        sub_repo = tmp_path / "sub"
        _init_repo(sub_repo)
        _commit(sub_repo, {"x.py": "v1\n"}, "sub-v1")

        outer = tmp_path / "outer"
        _init_repo(outer)
        _git(outer, "-c", "protocol.file.allow=always", "submodule", "add", str(sub_repo), "sub")
        # Commit .gitmodules with `ignore = all` so git suppresses this submodule
        # in diff output by default.
        gitmodules = outer / ".gitmodules"
        existing = gitmodules.read_text()
        gitmodules.write_text(existing + "\tignore = all\n")
        _git(outer, "add", ".gitmodules")
        _git(outer, "commit", "-m", "add sub with ignore=all")
        base = _git(outer, "rev-parse", "HEAD").stdout.strip()

        sub_v2_sha = _commit(sub_repo, {"x.py": "v2\n"}, "sub-v2")
        _git(outer / "sub", "fetch", "-q", "origin")
        _git(outer / "sub", "checkout", "-q", sub_v2_sha)
        # `ignore = all` hides the gitlink from porcelain, and HOW COMPLETELY IS VERSION-DEPENDENT:
        # git 2.50 still stages it via `git add` (merely hiding it from `git diff --cached`), while
        # git 2.54 stages nothing and the commit dies with "nothing to commit, working tree clean".
        # That is why this passed on a developer's machine and failed on every CI runner.
        # `-c submodule.sub.ignore=none` does NOT rescue it — measured, still nothing staged.
        #
        # Write the gitlink with plumbing, which no ignore setting can suppress. Staging the bump
        # is SETUP, not the claim: the claim is that `take_snapshot` refuses to be silenced by
        # `ignore = all`, and the committed `.gitmodules` carrying it is untouched.
        _git(outer, "update-index", "--add", "--cacheinfo", f"160000,{sub_v2_sha},sub")
        _git(outer, "commit", "-m", "bump sub")

        snap = take_snapshot(outer, base_ref=base)
        assert snap.diff_text != "", ".gitmodules ignore=all suppressed the submodule change"

    def test_xfuncname_does_not_change_hunk_headers(self, tmp_path: Path) -> None:
        """A committed `.gitattributes` selecting a custom diff driver whose
        `xfuncname` is configured in `.git/config` changes the function-context
        suffix of `@@ -N,M +N,M @@` hunk headers.  Post-processing must strip
        that suffix so diff bytes are independent of any xfuncname config."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        base = _commit(
            repo,
            {
                "s.py": "class Foo:\n    def bar(self):\n        return 1\n",
                ".gitattributes": "*.py diff=mypython\n",
            },
            "base",
        )
        _commit(repo, {"s.py": "class Foo:\n    def bar(self):\n        return 2\n"}, "change")

        before = take_snapshot(repo, base_ref=base).diff_text
        # Install an xfuncname that would annotate hunk headers with the class name.
        _git(repo, "config", "diff.mypython.xfuncname", "(class .*|def .*)")
        after = take_snapshot(repo, base_ref=base).diff_text

        assert before == after, "diff.mypython.xfuncname changed hunk headers reviewers see"
        # Confirm neither result has a function-context suffix on any @@ line.
        import re

        for line in after.splitlines():
            if line.startswith("@@"):
                assert re.match(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@$", line), (
                    f"@@ line has unexpected suffix: {line!r}"
                )

    def test_indent_heuristic_config_does_not_change_diff_bytes(self, tmp_path: Path) -> None:
        """`diff.indentHeuristic=false` shifts hunk-boundary placement for the
        same objects — reproduced by round-0 reviewers. The pin must prevent it."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        v1 = "def foo():\n    x = 1\n    return x\n\n\ndef bar():\n    y = 2\n    return y\n"
        v2 = "def foo():\n    x = 1\n    return x\n\n\ndef bar():\n    y = 99\n    return y\n"
        base = _commit(repo, {"m.py": v1}, "base")
        _commit(repo, {"m.py": v2}, "change")

        before = take_snapshot(repo, base_ref=base).diff_text
        _git(repo, "config", "diff.indentHeuristic", "false")
        after = take_snapshot(repo, base_ref=base).diff_text

        assert before == after, "diff.indentHeuristic=false changed hunk bytes reviewers see"

    def test_rename_limit_config_does_not_suppress_rename_detection(self, tmp_path: Path) -> None:
        """`diff.renameLimit=1` prevents detection of all but one rename when
        multiple files are renamed, converting them to delete+add pairs. The pin
        must ensure a fixed, adequate limit regardless of repo config."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        base = _commit(repo, {"a.py": "x=1\n", "b.py": "x=2\n", "c.py": "x=3\n"}, "base")
        for old, new in [("a.py", "aa.py"), ("b.py", "bb.py"), ("c.py", "cc.py")]:
            (repo / old).rename(repo / new)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "rename all three")

        before = take_snapshot(repo, base_ref=base).diff_text
        _git(repo, "config", "diff.renameLimit", "1")
        after = take_snapshot(repo, base_ref=base).diff_text

        assert before == after, "diff.renameLimit=1 changed rename detection"

    def test_nul_bytes_SURVIVE_into_diff_text_and_are_removed_at_prompt_assembly(
        self, tmp_path: Path
    ) -> None:
        """INVERTED by PR-h-field-01. The old assertion was "NUL must not survive", justified by
        "Python subprocess rejects argv with embedded NUL". Item 1 moved the prompt to
        stdin, so that justification is gone — and item 2 needs these bytes: a NUL is git's
        own binary heuristic and the ONLY binary signal an attacker cannot forge with a
        `.gitattributes` `-diff` entry.

        Stripping at snapshot time silently blinded that detection. Measured on the
        reported repo: 6,667 NULs removed, after which all 12 committed PNGs read as text
        and the 1.6 MB diff went to the provider whole.

        The guarantee did not weaken, it MOVED — the property that matters was never "the
        snapshot holds no NUL", it was "no binary content reaches a reviewer". Both halves
        are asserted here so the pair cannot drift apart.
        """
        repo = tmp_path / "repo"
        _init_repo(repo)
        base = _commit(repo, {"f.bin": "clean\n"}, "base")
        (repo / "f.bin").write_bytes(b"has\x00nul\x00bytes\n")
        _git(repo, "add", "f.bin")
        _git(repo, "commit", "-m", "add nul bytes")

        snap = take_snapshot(repo, base_ref=base)

        assert "\x00" in snap.diff_text, (
            "NUL was stripped from diff_text again — that is the binary signal, and "
            "removing it here makes elide_binary_hunks blind to every real binary"
        )

        from syncade.diff_filter import elide_binary_hunks

        reviewer_diff, elided = elide_binary_hunks(snap.diff_text)
        assert "\x00" not in reviewer_diff, "binary bytes reached the reviewer diff"
        assert elided == ["f.bin"], f"the binary path was not disclosed: {elided}"

    def test_base_oid_populated_when_base_ref_given(self, tmp_path: Path) -> None:
        """Snapshot must carry the resolved base OID, not just the symbolic ref,
        so artifacts can reconstruct the exact reviewed range even after a ref moves."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        base_sha = _commit(repo, {"f.py": "v1\n"}, "base")
        _commit(repo, {"f.py": "v2\n"}, "change")

        snap = take_snapshot(repo, base_ref=base_sha)

        assert snap.base_oid == base_sha
        assert snap.base_ref == base_sha

    def test_base_oid_none_when_no_base_ref(self, tmp_path: Path) -> None:
        """Without a base_ref, base_oid must be None."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, {"f.py": "v1\n"}, "initial")

        snap = take_snapshot(repo)

        assert snap.base_oid is None
