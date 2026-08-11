"""Who owns a file in the skill destination — PR-h-04.5 items 3 and 4.

Split from test_install_skill_refuses.py when it passed the 500-LOC cap. That file asks
WHAT COUNTS AS LOSS; this one asks WHO WROTE IT — the `.syncade-install.json` record, how
it distinguishes an upgrade from an operator edit, and why it is not exempt from the very
survey it feeds.

Reproduced on `main` before this landed, in a real skill directory:

    BEFORE:  MY-NOTES.md  reference.md  SKILL.md
    AFTER:   README.md    SKILL.md

A hand-edited `SKILL.md` overwritten and two unrelated operator files deleted — at **exit 0**,
with no prompt, warning or backup. `--install-skill` ran `rmtree` over the destination and
rewrote it.

The rule is now: survey every destination first, and if installing would destroy anything,
refuse (exit 60) naming each casualty. Two things deliberately do NOT count as loss, because
treating them as loss would break documented paths to protect data that was never at risk:

- a **symlinked** destination (README documents symlink-to-checkout; unlinking the symlink
  leaves the checkout untouched), and
- a bundled file whose bytes are **identical** to what we would write (rewriting it changes
  nothing), which is what keeps an ordinary re-install from demanding a flag.
"""

from __future__ import annotations

from pathlib import Path

from syncade.cli.install_skill import install_skill

_CLAUDE = Path(".claude") / "skills" / "syncade"


def _home(tmp_path):
    home = tmp_path / "home"
    (home / _CLAUDE).mkdir(parents=True)
    return home


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


# ── Upgrade vs operator edit — PR-h-04.5 item 3 ─────────────────────────────
#
# Item 2 left a gap on purpose: a destination written by an OLDER syncade differs from the
# current bundle, so it was refused exactly like an operator edit. Measured, an ordinary
# version upgrade exited 60. That is rule 5's failure mode — it trains operators to pass
# --force-install reflexively, which loses the protection entirely.
#
# The brief's rule 2 says "if a bundled-name file differs from what this version would
# write, it is the operator's". Taken literally that IS the bug above; it cannot coexist
# with rule 5. A RECORD of what we wrote reconciles them, and is content-based (hashes),
# which is what rule 2 actually asks for — not a name and not a marker.


def _install(tmp_path, home_name="home"):
    home, cx = tmp_path / home_name, tmp_path / "cx"
    assert install_skill("claude", home=home, codex_home=cx) == 0
    return home, cx


def test_an_upgrade_to_a_changed_bundle_needs_no_flag(tmp_path, monkeypatch):
    """The gap item 2 left, closed. A file we wrote in an older version is OURS."""
    from syncade.cli import install_skill as mod

    home, cx = _install(tmp_path)
    real = mod._bundle("claude")

    # A newer syncade ships different bytes for the same file names.
    monkeypatch.setattr(
        mod, "_bundle", lambda h: {**real, "SKILL.md": real["SKILL.md"] + b"\n<!-- v2 -->\n"}
    )

    assert mod.install_skill("claude", home=home, codex_home=cx) == 0, (
        "an ordinary version upgrade demanded --force-install"
    )
    assert b"<!-- v2 -->" in (home / _CLAUDE / "SKILL.md").read_bytes(), "the upgrade did not land"


def test_an_operator_edit_is_still_refused_after_the_record_exists(tmp_path):
    """The other direction. A record that made everything installable would be worse than
    no record at all."""
    home, cx = _install(tmp_path)
    skill = home / _CLAUDE / "SKILL.md"
    skill.write_bytes(skill.read_bytes() + b"\n# my own note\n")

    assert install_skill("claude", home=home, codex_home=cx) == 60
    assert b"my own note" in skill.read_bytes(), "the operator's edit was destroyed"


def test_a_file_the_bundle_dropped_is_removed_not_refused(tmp_path, monkeypatch):
    """We wrote it; a later version stopped shipping it. Ours to clean up, not a casualty."""
    from syncade.cli import install_skill as mod

    real = mod._bundle("claude")
    monkeypatch.setattr(mod, "_bundle", lambda h: {**real, "LEGACY.md": b"shipped once\n"})
    home, cx = tmp_path / "home", tmp_path / "cx"
    assert mod.install_skill("claude", home=home, codex_home=cx) == 0
    assert (home / _CLAUDE / "LEGACY.md").is_file()

    monkeypatch.setattr(mod, "_bundle", lambda h: real)  # the next version drops it

    assert mod.install_skill("claude", home=home, codex_home=cx) == 0, (
        "a file syncade itself had installed was treated as an operator's"
    )
    assert not (home / _CLAUDE / "LEGACY.md").exists()


def test_a_missing_record_falls_back_to_refusing(tmp_path):
    """Fail closed. A destination from before the record existed cannot be proven ours, so
    it costs ONE forced install rather than silently overwriting whatever is there.

    That is the deliberate migration cost for operators upgrading from a syncade that wrote
    no record; stated here so it is a decision rather than a surprise.
    """
    home, cx = _install(tmp_path)
    (home / _CLAUDE / ".syncade-install.json").unlink()
    skill = home / _CLAUDE / "SKILL.md"
    skill.write_bytes(b"whatever an older version wrote\n")

    assert install_skill("claude", home=home, codex_home=cx) == 60


def test_a_corrupt_record_falls_back_to_refusing(tmp_path):
    """Unparseable is not "trust nothing about the destination" — it is "prove nothing",
    which means every differing file is the operator's."""
    home, cx = _install(tmp_path)
    (home / _CLAUDE / ".syncade-install.json").write_text("{ not json")
    skill = home / _CLAUDE / "SKILL.md"
    skill.write_bytes(b"changed\n")

    assert install_skill("claude", home=home, codex_home=cx) == 60


def test_the_record_is_not_exempted_by_name(tmp_path, capsys):
    """It is recognised by PARSING as a record — a property of its content. A file merely
    NAMED like one is the operator's, and item 4 attacks this seam directly."""
    home, cx = _install(tmp_path)
    (home / _CLAUDE / ".syncade-install.json").write_text("my own file that happens to sit here\n")

    assert install_skill("claude", home=home, codex_home=cx) == 60
    assert ".syncade-install.json" in capsys.readouterr().err


# ── The record is not exempt from its own rules — PR-h-04.5 item 4 ──────────
#
# The round-4 finding this closes: "the .syncade-installed sentinel is excluded from its own
# loss detection", i.e. the mechanism protecting files did not protect itself.
#
# The testable form of the brief's claim 2 is: deleting or editing the record must never make
# the install MORE destructive. "Never destructive at all" is not achievable and pretending
# otherwise would be the false claim — syncade holds no secret, so a record that parses is
# believed. What makes that acceptable is the BOUND: anyone who can edit the record already
# has write access to the destination and could delete those files directly, so it grants no
# capability its writer did not already have. The tests below pin that bound.


def _record(dest: Path) -> Path:
    return dest / ".syncade-install.json"


def test_deleting_the_record_makes_the_install_more_conservative_not_less(tmp_path):
    home, cx = _install(tmp_path)
    (home / _CLAUDE / "MY-NOTES.md").write_text("personal\n")
    _record(home / _CLAUDE).unlink()

    assert install_skill("claude", home=home, codex_home=cx) == 60
    assert (home / _CLAUDE / "MY-NOTES.md").exists(), "deleting the record enabled destruction"


def test_corrupting_the_record_makes_the_install_more_conservative_not_less(tmp_path):
    home, cx = _install(tmp_path)
    (home / _CLAUDE / "MY-NOTES.md").write_text("personal\n")
    _record(home / _CLAUDE).write_text("{{{ not json")

    assert install_skill("claude", home=home, codex_home=cx) == 60
    assert (home / _CLAUDE / "MY-NOTES.md").exists(), "corrupting the record enabled destruction"


def test_a_record_directory_is_refused_not_walked_around(tmp_path):
    """A directory at the record's path parses as nothing, so it is surveyed like anything
    else rather than silently tolerated."""
    home, cx = _install(tmp_path)
    rec = _record(home / _CLAUDE)
    rec.unlink()
    rec.mkdir()
    (rec / "inside").write_text("x\n")

    assert install_skill("claude", home=home, codex_home=cx) == 60


def test_a_record_key_cannot_reach_outside_the_destination(tmp_path, capsys):
    """The bound that matters. A crafted key must not let the installer touch anything
    outside the skill directory it lives in.

    Previously, traversal keys were filtered silently so the self-hash still verified and the
    install succeeded (exit 0), overwriting the edited manifest. Now any non-plain key causes
    _parse_manifest to fail closed, so the manifest is treated as operator-authored and the
    install refuses (exit 60) — which also guarantees nothing outside the skill dir is touched.
    """
    import hashlib
    import json as _json

    home, cx = _install(tmp_path)
    outside = tmp_path / "SECRET.txt"
    outside.write_bytes(b"do not touch\n")
    rec = _record(home / _CLAUDE)
    data = _json.loads(rec.read_text())
    data["files"]["../../../SECRET.txt"] = hashlib.sha256(b"do not touch\n").hexdigest()
    data["files"]["/etc/passwd"] = "0" * 64
    rec.write_text(_json.dumps(data))

    assert install_skill("claude", home=home, codex_home=cx) == 60, (
        "a manifest with traversal keys was not treated as operator-authored"
    )
    assert outside.read_bytes() == b"do not touch\n", "a record key reached outside the destination"
    assert ".syncade-install.json" in capsys.readouterr().err


def test_traversal_keys_are_dropped_when_the_record_is_read(tmp_path):
    """Non-plain keys in the manifest make it tampered — _read_manifest fails closed to {}.

    Previously, invalid keys were filtered silently so they never appeared in the returned
    dict (the lookup-only property held incidentally). Now _parse_manifest detects them before
    the self-hash check and returns {} for the whole manifest, which is the structural fix:
    a future change that joins a key onto a path finds the hole already closed AND the
    presence of any non-plain key is detected as an operator edit.
    """
    import json as _json

    from syncade.cli.install_skill import _read_manifest

    home, _ = _install(tmp_path)
    rec = _record(home / _CLAUDE)
    data = _json.loads(rec.read_text())
    for bad in ("../x", "a/b", "/abs", "..", ".", ""):
        data["files"][bad] = "0" * 64
    rec.write_text(_json.dumps(data))

    keys = set(_read_manifest(home / _CLAUDE))
    # Non-plain keys must not appear; the whole manifest fails closed to {}.
    assert keys == set(), f"a manifest with non-plain keys should fail closed, got: {keys}"


# ── Parseable edit inside the manifest's files dict — blocker fixed in round-0 review ──
#
# A parseable operator edit to hash values INSIDE the files dict kept the manifest's
# top-level structure valid ({"files": {...}}), so _read_manifest returned a non-empty
# prior, and _scan_casualties skipped the manifest on `name == _MANIFEST and prior`.
# The fix introduces a self-hash: the manifest stores sha256(canonical_inner) under its
# own filename key, and _scan_casualties re-verifies that hash from current bytes.


def test_a_parseable_edit_inside_the_manifest_files_dict_is_caught(tmp_path, capsys):
    """An operator changes a hash value inside files dict but keeps the JSON structure valid.
    Before the self-hash fix this was silently overwritten; the self-hash now catches it."""
    import json as _json

    home, cx = _install(tmp_path)
    rec = _record(home / _CLAUDE)
    data = _json.loads(rec.read_text())
    # Change a hash inside the files dict — structure stays valid, self-hash is invalidated.
    first_bundle_key = next(k for k in data["files"] if k != ".syncade-install.json")
    data["files"][first_bundle_key] = "a" * 64
    rec.write_text(_json.dumps(data))

    rc = install_skill("claude", home=home, codex_home=cx)
    assert rc == 60, "a parseable manifest edit was silently overwritten"
    assert ".syncade-install.json" in capsys.readouterr().err, (
        "the manifest was not named as a casualty"
    )


# ── Parseable manifest with extra keys — finding in PR-h-04.5 dogfood ───────


def test_a_parseable_manifest_with_extra_keys_is_treated_as_operators(tmp_path, capsys):
    """An operator-added top-level field keeps the manifest parseable, which used to let
    it pass _read_manifest and suppress itself from the casualty survey."""
    import json as _json

    home, cx = _install(tmp_path)
    rec = _record(home / _CLAUDE)
    data = _json.loads(rec.read_text())
    data["operator_note"] = "my annotation"
    rec.write_text(_json.dumps(data))

    rc = install_skill("claude", home=home, codex_home=cx)

    assert rc == 60, "a manifest with extra keys was not treated as operator-authored"
    assert "operator_note" in rec.read_text(), "the manifest was overwritten"
    assert ".syncade-install.json" in capsys.readouterr().err, "the casualty was not named"


def test_a_nonstring_value_in_manifest_files_dict_is_treated_as_tampered(tmp_path, capsys):
    """A new key with a non-string value (e.g. an integer) is silently filtered by the old
    code, leaving the canonical inner dict unchanged so the self-hash still verifies.
    The manifest is then overwritten at exit 0. The fix rejects the whole manifest."""
    import json as _json

    home, cx = _install(tmp_path)
    rec = _record(home / _CLAUDE)
    data = _json.loads(rec.read_text())
    data["files"]["new_key"] = 42  # non-string value for a NEW key
    rec.write_text(_json.dumps(data))

    rc = install_skill("claude", home=home, codex_home=cx)
    assert rc == 60, "a manifest with a non-string value was not treated as operator-authored"
    assert ".syncade-install.json" in capsys.readouterr().err, "the casualty was not named"


# ── Special files at the manifest path — blocker fixed in round-0 review ─────
#
# _read_manifest used to call read_bytes() unconditionally on the manifest path.
# A FIFO at that path blocks read_bytes() forever. A symlink could redirect to a
# blocking target. Both bypass the _scan_casualties special-file protections because
# the read happens before the casualty scanner ever runs.
#
# The fix: lstat the manifest path before reading; return {} for anything that is
# not a regular non-symlink file. _scan_casualties then classifies the entry normally.


def test_fifo_at_manifest_path_does_not_hang(tmp_path):
    """A FIFO at .syncade-install.json must not block read_bytes.

    _read_manifest now lstats before reading and returns {} for non-regular files,
    letting _scan_casualties report the FIFO as a special file and refuse (exit 60).
    """
    import os

    home, cx = _install(tmp_path)
    rec = _record(home / _CLAUDE)
    rec.unlink()
    os.mkfifo(rec)

    rc = install_skill("claude", home=home, codex_home=cx)
    assert rc == 60, "a FIFO at the manifest path was not refused"


def test_symlink_at_manifest_path_is_not_followed(tmp_path, capsys):
    """A symlink at .syncade-install.json must not be opened (it might point to a FIFO).

    _read_manifest now lstats and returns {} for symlinks. _scan_casualties reports
    the symlink as a casualty and the install refuses (exit 60).
    """
    home, cx = _install(tmp_path)
    rec = _record(home / _CLAUDE)
    rec.unlink()
    rec.symlink_to(tmp_path / "nonexistent_target")  # dangling symlink

    rc = install_skill("claude", home=home, codex_home=cx)
    assert rc == 60, "a symlink at the manifest path was not refused"
    assert ".syncade-install.json" in capsys.readouterr().err


# ── Duplicate JSON keys in manifest — blocker fixed in round-1 review ─────────
#
# json.loads silently collapses duplicate keys — last value wins. A manifest with
# a duplicated file entry (e.g. two "README.md" keys) would be normalised to a
# canonical dict identical to the original before the self-hash check, so the
# self-hash would still verify. _scan_casualties would treat the manifest as ours
# and skip it; the reinstall would overwrite the edited manifest at exit 0.
#
# The fix: _parse_manifest uses object_pairs_hook=_no_duplicate_keys, which raises
# ValueError on any duplicate key. The except ValueError block returns {}, so the
# manifest is treated as unknown — a casualty if it differs from the current bundle.


def test_duplicate_key_in_manifest_files_is_treated_as_tampered(tmp_path, capsys):
    """A manifest with a duplicate files key normalises silently in the old code,
    leaving the canonical dict unchanged so the self-hash still verifies and the
    manifest is overwritten at exit 0. The fix rejects the whole manifest."""
    home, cx = _install(tmp_path)
    rec = _record(home / _CLAUDE)
    original = rec.read_bytes()
    # Inject a duplicate key at the JSON text level — json.dumps would deduplicate it,
    # so we splice the raw bytes directly: prepend a spurious "README.md" entry before
    # the real one inside the "files" object.
    raw = original.decode()
    # Find the opening brace of "files" and insert a duplicate entry after it.
    files_open = raw.index('"files": {')
    insert_at = raw.index("\n", files_open) + 1
    fake_hash = "aa" * 32  # 64 hex chars, wrong value
    dup_line = f'    "README.md": "{fake_hash}",\n'
    tampered = raw[:insert_at] + dup_line + raw[insert_at:]
    rec.write_bytes(tampered.encode())

    rc = install_skill("claude", home=home, codex_home=cx)
    assert rc == 60, "a manifest with a duplicate key was not treated as tampered"
    assert ".syncade-install.json" in capsys.readouterr().err, "the casualty was not named"
    assert rec.read_bytes() == tampered.encode(), "the manifest was overwritten despite the refusal"


# ── Lexical (JSON-equivalent) edits to the manifest — blocker fixed in round-2 review ──
#
# _parse_manifest verifies the self-hash over the PARSED canonical dict, not the raw bytes.
# An operator who adds leading whitespace or uses alternative JSON escaping preserves the
# same parsed dict → same canonical re-serialisation → same hash → self-hash verifies.
# _scan_casualties then skips the manifest as ours, and reinstall overwrites it at exit 0
# without naming it — the same self-hash-bypass class as the prior blocker.
#
# The fix: after the self-hash check passes, compare raw bytes against the expected canonical
# serialisation. Any deviation (whitespace, escaping, BOM) causes _parse_manifest to return
# {}, so the manifest is treated as operator-authored and the install refuses.


def test_lexical_whitespace_edit_to_manifest_is_treated_as_operator_authored(tmp_path, capsys):
    """Leading whitespace in the manifest preserves JSON semantics but differs in raw bytes.
    Before the fix, the self-hash still verified and the manifest was silently overwritten.
    After the fix, the raw-bytes check catches it and the install refuses (exit 60)."""
    home, cx = _install(tmp_path)
    rec = _record(home / _CLAUDE)
    original = rec.read_bytes()
    # Prepend a space — the JSON is still valid and parses to the same dict.
    rec.write_bytes(b" " + original)

    rc = install_skill("claude", home=home, codex_home=cx)
    assert rc == 60, "a lexical whitespace edit to the manifest was silently overwritten"
    assert ".syncade-install.json" in capsys.readouterr().err, (
        "the manifest was not named as a casualty"
    )
    assert rec.read_bytes() == b" " + original, "the manifest was overwritten despite the refusal"
