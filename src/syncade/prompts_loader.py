"""Prompt template loader + the reviewer prompt blocks.

``load_template`` (the per-repo-override-then-packaged-default resolver with the
symlink-containment guard), ``ADVERSARIAL_LENS_BLOCK`` (the opt-in reviewer
edge-enumeration text), and ``BUG_CLASS_BLOCK`` (the default-on directed
bug-class sweep). All are free of any dependency on ``prompts`` itself —
stdlib + importlib only — so they extract without a circular import. Re-exported
from ``prompts`` so the ``syncade.prompts.<name>`` import paths are unchanged.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

ADVERSARIAL_LENS_BLOCK = """
## Adversarial edge enumeration (before any SHIP)

Confirming the code does what the brief says is NOT enough — that is exactly how
real defects survive review, and it is the specific failure this instruction
exists to stop. The brief's acceptance criteria are the FLOOR of your review,
never the ceiling: ticking off "each AC is implemented" is the confirmatory trap
that ships bugs. Before any SHIP, treat the implementation as guilty until proven
innocent and do all three of the following — your `summary` must show you did.

1. **Enumerate the spec-omitted combinations, in writing.** List the cases the
   brief does NOT discuss — flag interactions (e.g. a verbosity/quiet flag
   crossed with the feature), empty / missing / malformed / unrelated inputs,
   absent state (no record, no upstream, no config), ordering / concurrency
   edges. This list is SEPARATE from the acceptance criteria; if your enumeration
   just restates the ACs, you have not done it.
2. **Falsify every invariant the brief asserts — do not confirm the mechanism.**
   For each absolute claim the brief makes ("byte-identical", "unchanged",
   "always …", "never silently …", "exactly one", "only X"), construct and RUN
   the comparison that would DISPROVE it, then cite the command and its output.
   Confirming the relevant code path exists or executes is NOT verifying the
   claim: "byte-identical" means you diff the actual bytes of both outputs, not
   that a branch is taken; "never silently" means you run the silent mode and
   inspect the channel, not that a log line exists in source. An asserted
   invariant you did not try to break is unverified.
3. **Run each enumerated edge in your worktree.** A probed edge that misbehaves
   is a finding (cite the reproduction). An edge you could NOT probe is a
   `coverage_gap`. SHIP is permitted ONLY when your `summary` shows the
   enumeration (1) AND a falsification attempt for each asserted invariant (2);
   without those you have not finished reviewing — emit NO-SHIP or coverage_gaps,
   never a silent clean SHIP.
4. **Verify the END-TO-END journey, and a real defect is a blocker — do not
   downgrade it to a nit to justify SHIP.** Two failure modes this stops, both
   seen in validation:
   - *Unit verified, journey not.* Trace each feature from invocation to its
     FINAL output / printed message / side-effect, and check every brief
     invariant holds at EVERY surface — not just the first. A `--base` correctly
     passed to one function but DROPPED from the printed next-step is a real
     blocker; the running test suite passing is not the journey.
   - *Happy path verified, consequence-of-bad-input not.* For each parse / IO /
     input operation, feed the corrupt / missing / malformed case and ask what
     the user GETS: a loud error, or a plausible-but-WRONG result? A
     silently-wrong result (a partial draft from a corrupt input that looks
     complete; a value silently defaulted) is a blocker, not an edge note.
   When you find a defect that loses data, changes behavior, or contradicts a
   brief invariant, your verdict is **NO-SHIP and the finding is a `blocker`** —
   however small the fix looks. Do not downgrade a genuine defect to a `nit` or
   `minor` so you can SHIP; "the fix is one line" is not a reason to ship the bug.
"""
"""The adversarial edge-enumeration block.

Substituted into ``reviewer.md``'s ``{adversarial_lens_block}`` placeholder
only for reviewers configured ``adversarial_lens=True`` (see
:class:`~syncade.config.ReviewerConfig`); other reviewers render the placeholder
as the empty string.
"""

BUG_CLASS_BLOCK = """## Directed bug-class sweep (before any SHIP)

The adversarial disposition above tells you to attack the change; this tells you
WHERE to aim, so recall does not depend on inspiration. Work each angle below
against the diff AND the enclosing functions, and surface every candidate with a
nameable failure — as a *candidate*. This is find-phase guidance: raising one here
does NOT relax the rule that a `blocker` needs reproduction, and a candidate you
drop silently is never verified — the single largest source of missed defects.

Severity follows verification state, not your confidence:
- A candidate you REPRODUCE (cite `evidence_cmd` / `evidence_output`) takes the
  severity its consequence warrants — `blocker` if it loses or corrupts data,
  breaks a user path, or violates an invariant the brief asserts. Do NOT downgrade
  a reproduced defect so you can SHIP (see the adversarial block above).
- A candidate whose mechanism is real but whose trigger you could not reproduce is
  a `minor`, or goes in `coverage_gaps` — surfaced, never dropped, and never an
  unreproduced `blocker`. This keeps recall high without spending a producer round
  on a maybe-bug.

Your `summary` must name which of these angles you ran.

- **Removed-behavior audit.** For every line the diff DELETES or replaces —
  including a rewritten docstring or comment that stated an invariant — name the
  guard, validation, error path, or invariant it enforced, then find where the new
  code re-establishes it. If you cannot, that is a candidate. Watch especially for
  an invariant only PARTIALLY re-established: a new code path that reaches the same
  sink (a delete, a write, a network call) without the guard the old path carried.
- **Caller/callee trace.** For every function the diff changes, grep its callers
  and check each call site against the NEW contract: a new precondition, a changed
  return shape or nullability, a new exception, a new empty/zero case the caller
  does not handle. Then check the callees the diff adds: does a new call feed a
  value into an existing sink (sweep / delete / overwrite / commit) whose guard
  assumed the old path's guarantees? A *success* that now carries an empty or
  absent result into a destructive sink is the highest-value find here. (Distinct
  from the consistency-class rule above: that rule is the same fact restated in
  many places; this is a changed contract breaking a call site.)
- **Language / framework pitfall.** Flag the classic footguns the diff introduces
  for its language: falsy-zero and `==` coercion (JS); mutable-default-arg and
  late-binding closures (Python); nil-map write and range-var capture (Go);
  unanchored regex; float equality; timezone/DST drift; SQL injection.
- **Wrapper / proxy correctness.** When the change adds or edits a type that wraps
  another (cache, proxy, decorator, adapter), check every method routes to the
  wrapped instance and not back through a registry/session/global (which re-enters
  or recurses), and that it forwards every method its callers actually use.
"""
"""The directed bug-class sweep block.

Substituted into every reviewer template's ``{bug_class_block}`` placeholder for
reviewers configured ``bug_class_sweep=True`` — the default, unlike the opt-in
:data:`ADVERSARIAL_LENS_BLOCK` (see :class:`~syncade.config.ReviewerConfig`);
reviewers with the sweep disabled render the placeholder as the empty string.
"""


def load_template(repo_root: Path, template_name: str) -> str:
    """Load a prompt template by basename.

    Resolution order:

    1. ``<repo_root>/.syncade/templates/<template_name>`` — per-repo
       override. If this file exists, its contents are returned. Any
       read failure (permissions, encoding) raises ``OSError`` and is
       NOT silently swallowed — a broken override is a config bug, not
       a fall-through-to-default condition.
    2. Packaged default at ``syncade/templates/<template_name>``,
       loaded via :func:`importlib.resources.files` so it works both
       for installed wheels and ``pip install -e .`` editable
       installs.

    Args:
        repo_root: The git repo root to check for an override under
            ``.syncade/templates/``.
        template_name: The template's basename (e.g. ``"reviewer.md"``,
            ``"synthesizer.md"``). Must be a plain basename — no path
            separators, no parent references, no leading ``.``. The
            check is intentionally strict: a template loader that
            accepts arbitrary paths could be tricked into reading
            anywhere on the filesystem by a malicious config.

    Returns:
        The raw template string with ``{placeholder}`` tokens
        unsubstituted. Substitution is the per-template renderer's
        concern.

    Raises:
        ValueError: If ``template_name`` is not a safe basename OR if
            the override file (or any parent directory) is a symlink
            that escapes ``<repo_root>/.syncade/templates/``. The
            containment guard prevents a malicious or
            accidental symlink (e.g. ``.syncade/templates/reviewer.md``
            pointing at ``/etc/passwd``) from making the template
            loader read arbitrary files.
        FileNotFoundError: If neither the override nor the packaged
            default exists. The packaged default should always be
            present for templates syncade ships; a missing default is
            a packaging bug, not a runtime condition.
    """
    if (
        not template_name
        or "/" in template_name
        or "\\" in template_name
        or template_name in (".", "..")
        or Path(template_name).is_absolute()
    ):
        raise ValueError(
            f"template_name {template_name!r} must be a plain basename "
            "(no separators, parent refs, or absolute paths). The "
            "loader resolves against `.syncade/templates/` or the "
            "packaged default; an arbitrary path is unsafe."
        )
    override = repo_root / ".syncade" / "templates" / template_name
    if override.is_file():
        # Template override containment guard. Three layered
        # checks against symlink-based escape:
        #
        # 1. Parent directories must be REAL dirs, not symlinks.
        #    Without this, ``<repo>/.syncade/templates`` could be a
        #    symlink to e.g. ``/etc/``, and the file-level
        #    ``relative_to`` check below would pass because both
        #    resolve to the same target.
        #
        # 2. The override path itself (if a symlink) must resolve
        #    to a target inside the REAL template dir. This is the
        #    file-level containment check.
        #
        # 3. The template-dir resolution uses ``strict=False`` so
        #    we don't follow symlinks on the parent components;
        #    they should NOT be symlinks per (1).
        syncade_dir = repo_root / ".syncade"
        template_dir = syncade_dir / "templates"
        for parent in (syncade_dir, template_dir):
            if parent.is_symlink():
                raise ValueError(
                    f"template override path contains a symlinked parent "
                    f"directory ({parent} is a symlink). Symlinks on the "
                    "parent components of the template directory are "
                    "refused: a symlink at <repo>/.syncade or "
                    "<repo>/.syncade/templates can redirect template "
                    "loading to arbitrary filesystem locations. Make "
                    "the parent components real directories."
                )
        # File-level containment check: the override (which
        # MAY itself be a file-level symlink) must resolve to a path
        # inside the now-confirmed-real template directory.
        resolved = override.resolve()
        try:
            # template_dir.resolve() is safe now — we verified above
            # that neither it nor its parent .syncade is a symlink.
            resolved.relative_to(template_dir.resolve())
        except ValueError as exc:
            raise ValueError(
                f"template override at {override} resolves to "
                f"{resolved}, which is outside the template directory "
                f"{template_dir}. Symlinks that escape the template "
                "directory are refused — the override path must be a "
                "real file (or a symlink to a real file inside the "
                "template directory)."
            ) from exc
        return override.read_text(encoding="utf-8")
    # `files("syncade")` resolves to the package root; `/ "templates" /
    # <name>` is the bundled template. Works for both editable and
    # installed-wheel layouts because importlib.resources abstracts
    # over them.
    return (files("syncade") / "templates" / template_name).read_text(encoding="utf-8")
