"""Confinement regression for H4: the producer prompt must never leak an
absolute main-repo / run-artifact path, and every input it references must
resolve INSIDE the producer worktree.

Mirrors the reviewer worktree-escape hardening (``orchestrator/round.py``): a
yolo producer handed an absolute path to the operator repo or a run artifact
can be lured OUT of its isolated worktree to read or write the live repo. The
fix stages every input inside the worktree and renders worktree-relative refs.
"""

from __future__ import annotations

import subprocess

from syncade.adapters.fake import FakeProducerAdapter
from syncade.config import ProducerConfig
from syncade.producer import run_producer
from tests.producer._helpers import _git_required, _seed_repo


def test_producer_prompt_confines_inputs_to_worktree(tmp_path):
    """The rendered producer prompt contains NO absolute main-repo path,
    and each referenced input resolves to a file INSIDE the producer
    worktree.

    Fails before the H4 fix (the renderer substituted ``str(pr_doc_path)`` /
    ``str(findings_md_path)``, i.e. absolute paths under the operator repo,
    and nothing was staged into the worktree) and passes after it.
    """
    _git_required()

    # Operator repo and producer worktree are DISTINCT siblings under
    # tmp_path — like production, where the worktree lives outside the repo.
    repo_root = tmp_path / "operator-repo"
    repo_root.mkdir()
    starting_sha = _seed_repo(repo_root)

    # PR doc: a repo-relative (but untracked-at-the-snapshot) file. Not in
    # the seed commit, so the worktree checkout below won't have it until
    # it is staged in.
    pr_doc = repo_root / "docs" / "pr.md"
    pr_doc.parent.mkdir(parents=True, exist_ok=True)
    pr_doc.write_text("# PR\n\nSpec body.\n", encoding="utf-8")

    # Run artifacts live under the operator repo's .syncade/runs/.
    round_dir = repo_root / ".syncade" / "runs" / "r" / "round-0"
    round_dir.mkdir(parents=True, exist_ok=True)
    findings = round_dir / "findings.md"
    findings.write_text("# Findings\n\nblocker: do the thing\n", encoding="utf-8")
    test_stdout = round_dir / "test-run.stdout"
    test_stdout.write_text("FAIL: test_x\n", encoding="utf-8")

    # Provision a real producer worktree at the seed SHA, OUTSIDE repo_root.
    worktree = tmp_path / "producer-worktree"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), starting_sha],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )

    # No commit_message → no commit; we only inspect the rendered prompt,
    # which the fake records at build_invocation time.
    fake = FakeProducerAdapter()
    run_producer(
        worktree_path=worktree,
        starting_sha=starting_sha,
        pr_doc_path=pr_doc,
        findings_md_path=findings,
        test_run_stdout_path=test_stdout,
        producer_config=ProducerConfig(),
        timeout_seconds=30.0,
        round_number=0,
        max_rounds=3,
        repo_root=repo_root,
        adapter=fake,
    )

    assert fake.invocations
    _, _, prompt = fake.invocations[0]

    # 1. No absolute main-repo / run-artifact path leaks into the prompt.
    assert str(repo_root) not in prompt
    assert str(pr_doc) not in prompt
    assert str(findings) not in prompt
    assert str(test_stdout) not in prompt

    # 2. The worktree's OWN absolute path is allowed — it is the producer's
    #    sandbox (not a main-repo path) and the template's boundary checks
    #    compare it against `git rev-parse --show-toplevel`.
    assert str(worktree) in prompt

    # 3. Every referenced input is a worktree-relative ref that resolves to a
    #    real file INSIDE the worktree (relative_to yields a forward-only
    #    subpath, so none can escape).
    expected_refs = (
        "docs/pr.md",
        ".syncade/runs/r/round-0/findings.md",
        ".syncade/runs/r/round-0/test-run.stdout",
    )
    worktree_resolved = worktree.resolve()
    for ref in expected_refs:
        assert ref in prompt
        resolved = (worktree / ref).resolve()
        assert resolved.is_file()
        assert resolved.is_relative_to(worktree_resolved)


def test_producer_stages_out_of_repo_pr_doc_into_worktree(tmp_path):
    """An out-of-repo PR doc is staged in by basename and referenced
    worktree-relatively — its absolute path never reaches the prompt."""
    _git_required()

    repo_root = tmp_path / "operator-repo"
    repo_root.mkdir()
    starting_sha = _seed_repo(repo_root)

    # PR doc OUTSIDE the operator repo entirely (relative_to fails → basename).
    pr_doc = tmp_path / "external" / "brief.md"
    pr_doc.parent.mkdir(parents=True, exist_ok=True)
    pr_doc.write_text("# External brief\n", encoding="utf-8")

    round_dir = repo_root / ".syncade" / "runs" / "r" / "round-0"
    round_dir.mkdir(parents=True, exist_ok=True)
    findings = round_dir / "findings.md"
    findings.write_text("# Findings\n", encoding="utf-8")

    worktree = tmp_path / "producer-worktree"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), starting_sha],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )

    fake = FakeProducerAdapter()
    run_producer(
        worktree_path=worktree,
        starting_sha=starting_sha,
        pr_doc_path=pr_doc,
        findings_md_path=findings,
        test_run_stdout_path=None,
        producer_config=ProducerConfig(),
        timeout_seconds=30.0,
        round_number=0,
        max_rounds=3,
        repo_root=repo_root,
        adapter=fake,
    )

    _, _, prompt = fake.invocations[0]
    # The out-of-repo absolute path is gone; the doc is staged under a
    # reserved, collision-free .syncade-inputs/ ref (never the bare
    # basename, which a same-named tracked file could shadow — H2) and
    # resolves inside the worktree.
    assert str(pr_doc) not in prompt
    assert str(pr_doc.parent) not in prompt
    assert (worktree / "brief.md").exists() is False
    staged = list((worktree / ".syncade-inputs").glob("*-brief.md"))
    assert len(staged) == 1
    assert staged[0].read_text(encoding="utf-8") == "# External brief\n"
    assert str(staged[0].relative_to(worktree)) in prompt


def test_stage_producer_input_out_of_repo_does_not_shadow_tracked_file(tmp_path):
    """H2 (producer leg): an out-of-repo input whose basename collides with a
    file already present in the worktree checkout must NOT be shadowed. It is
    staged to a reserved, collision-free path so the producer reads ITS
    content, not the tracked file's — the property round.py established for
    the reviewer leg. Before the fix ``_stage_producer_input`` used the bare
    basename and skipped the copy when the dest already existed, handing the
    producer the wrong spec.
    """
    from syncade.producer_git import _stage_producer_input

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    # A file already present in the worktree checkout (the producer worktree
    # keeps repo-context files), same basename as the external input.
    (worktree / "README.md").write_text("REPO README", encoding="utf-8")
    external = tmp_path / "external" / "README.md"
    external.parent.mkdir(parents=True)
    external.write_text("OUTSIDE PR SPEC", encoding="utf-8")

    ref = _stage_producer_input(external, worktree_path=worktree, repo_root=repo_root)

    # Not the bare basename (which resolves to the tracked file), and the
    # producer reads the EXTERNAL content.
    assert ref != "README.md"
    staged = worktree / ref
    assert staged.read_text(encoding="utf-8") == "OUTSIDE PR SPEC"
    # The tracked file is untouched, and the staged ref stays in-worktree.
    assert (worktree / "README.md").read_text(encoding="utf-8") == "REPO README"
    assert staged.resolve().is_relative_to(worktree.resolve())


def test_stage_producer_input_refreshes_a_mutated_staged_copy_on_retry(tmp_path):
    """PR-v2-22 F8: a run-artifact input staged into a SEPARATE worktree, then MUTATED by a crashed
    attempt (the mutation survives the gitignored reset), is re-copied from the pristine source on
    the next stage call — so a retry never reads corrupted input (C2)."""
    from syncade.producer_git import _stage_producer_input

    repo_root = tmp_path / "repo"
    (repo_root / ".syncade" / "runs" / "r").mkdir(parents=True)
    src = repo_root / ".syncade" / "runs" / "r" / "findings.md"
    src.write_text("PRISTINE findings\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    ref = _stage_producer_input(src, worktree_path=worktree, repo_root=repo_root)
    staged = worktree / ref
    assert staged.read_text(encoding="utf-8") == "PRISTINE findings\n"

    # A crashed attempt mutates the staged copy; it survives the (gitignored) reset.
    staged.write_text("CORRUPTED by attempt 1\n", encoding="utf-8")

    # Re-stage (the retry): the mutated copy is refreshed from the pristine source.
    ref2 = _stage_producer_input(src, worktree_path=worktree, repo_root=repo_root)
    assert ref2 == ref
    assert (worktree / ref2).read_text(encoding="utf-8") == "PRISTINE findings\n"


def test_stage_producer_input_same_root_is_a_noop_not_an_error(tmp_path):
    """PR-v2-22 F8: when worktree_path == repo_root the staged dest IS the source, so a re-copy
    would raise SameFileError. The content compare must skip it (return the ref) — not error."""
    from syncade.producer_git import _stage_producer_input

    repo_root = tmp_path / "repo"
    (repo_root / ".syncade" / "runs" / "r").mkdir(parents=True)
    src = repo_root / ".syncade" / "runs" / "r" / "findings.md"
    src.write_text("findings\n", encoding="utf-8")

    ref = _stage_producer_input(src, worktree_path=repo_root, repo_root=repo_root)
    assert (repo_root / ref).read_text(encoding="utf-8") == "findings\n"  # untouched, no error
