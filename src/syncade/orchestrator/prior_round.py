"""Prior-round context extraction helpers.

The reviewer and producer prompts support ``{prior_round_output}`` (and,
for producer, ``{prior_round_commits}``) placeholders. This module is the
data-loading bridge: read the prior round's persisted
``.stdout`` artifacts off disk, extract the response text via the
matching adapter's helper, and return the text (or a documented
framing fallback when the prior artifact is missing / unparseable).

Cross-PR isolation is structural: every ``syncade <pr-doc>`` invocation
runs under a fresh ``<run-id>``, so this module only ever sees stdouts
from prior rounds of the CURRENT run. No file path constructed here can
escape ``<run_dir>``.

Per-reviewer isolation is also structural: each reviewer's stdout lives
at ``<round-dir>/<reviewer_name>.stdout``, so a round-N claude reviewer
loading ``<round-(N-1)>/<claude_name>.stdout`` cannot see the codex
reviewer's output. The orchestrator (``round.py``) drives one extraction
per reviewer by their own name.

T4 formalized the per-adapter ``extract_response_text``
interface; this module dispatches by provider name to the matching
adapter's method. Both ``AnthropicAdapter`` and ``CodexAdapter`` now
expose a symmetric ``extract_response_text(self, raw_stdout: str) -> str``
method.
"""

from __future__ import annotations

import json
from pathlib import Path

from syncade.adapters.anthropic import AnthropicAdapter
from syncade.adapters.openai import CodexAdapter

# Sentinels rendered when the prior-round artifact is missing or
# unrecoverable. The reviewer / producer prompt prose tells the model
# how to interpret these strings, so the operator gets a coherent
# prompt instead of an empty placeholder.
_MISSING_ARTIFACT_FRAMING = (
    "(prior round artifact not found on disk; treat this round as a fresh attempt)"
)
"""Returned when ``<round-(N-1)>/<basename>.stdout`` doesn't exist or
can't be read. The framing tells the reviewer / producer that no prior
context is available and they should proceed as if starting fresh."""


def _extract_response_text_from_stdout(provider: str, raw_stdout: str) -> str:
    """Extract the assistant's response text from a reviewer / producer
    subprocess's raw stdout.

    Dispatches by provider name to the matching adapter's symmetric
    :meth:`extract_response_text` helper:

    - ``anthropic`` →
      :meth:`syncade.adapters.anthropic.AnthropicAdapter.extract_response_text`
    - ``openai`` →
      :meth:`syncade.adapters.openai.CodexAdapter.extract_response_text`

    Empty stdout short-circuits to returning ``""`` — calling the
    adapter's extractor on empty input would raise (codex would say
    "no agent_message event") which the broad except below would
    catch and fall back to raw_stdout anyway. The short-circuit makes
    the empty-stdout path explicit.

    Args:
        provider: The reviewer / producer config's ``provider`` string.
            Must be ``"anthropic"`` or ``"openai"`` to extract; any
            other value falls through to returning the raw stdout
            unchanged (the unknown-provider safe default — letting the
            model see something is better than letting it see nothing).
        raw_stdout: The contents of the prior round's
            ``<reviewer_name>.stdout`` or ``producer.stdout`` artifact.

    Returns:
        The extracted response text. Falls back to ``raw_stdout``
        verbatim on any per-adapter extraction failure (malformed
        envelope, missing ``result`` field, codex extraction raised).
        The fallback is deliberate — for cross-round context, having
        the model see the raw envelope is better than rendering an
        empty string.
    """
    # Empty stdout: pass through. The adapter helpers raise on empty
    # input; this short-circuit is cheaper than try/except.
    if not raw_stdout:
        return raw_stdout
    try:
        if provider == "anthropic":
            return AnthropicAdapter().extract_response_text(raw_stdout)
        if provider == "openai":
            return CodexAdapter().extract_response_text(raw_stdout)
    except Exception:  # noqa: BLE001 — best-effort: see docstring
        return raw_stdout
    # Unknown provider — pass the raw stdout through. The reviewer /
    # producer prompt will show it verbatim. Better than an empty
    # placeholder that loses cross-round signal entirely.
    return raw_stdout


def load_prior_reviewer_response_text(
    *,
    prior_round_dir: Path,
    reviewer_name: str,
    reviewer_provider: str,
) -> str:
    """Load and extract the prior round's response text for ONE reviewer.

    Reads ``<prior_round_dir>/<reviewer_name>.stdout`` and extracts the
    assistant's response text via the matching adapter's helper.
    Per-reviewer isolation: this function is called per reviewer name,
    so round-N claude-reviewer's load reads claude's prior stdout, NOT
    codex's. The orchestrator (round.py) drives the loop.

    Args:
        prior_round_dir: Path to ``<run_dir>/round-(N-1)``. Caller
            (round.py) constructs this from ``run_dir / f"round-{round_idx - 1}"``.
        reviewer_name: The :class:`~syncade.config.ReviewerConfig.name`.
            Becomes the basename of the on-disk stdout file.
        reviewer_provider: The :class:`~syncade.config.ReviewerConfig.provider`
            (``"anthropic"`` or ``"openai"``). Selects the extraction
            helper.

    Returns:
        The extracted prior-round response text, or
        :data:`_MISSING_ARTIFACT_FRAMING` when the stdout artifact is
        missing on disk. Defensive: a happy-path multi-round loop will
        always have the prior stdout present (a failed reviewer would
        have terminated the loop before reaching the next round), but
        the missing-artifact path keeps the orchestrator robust
        against on-disk corruption / manual artifact cleanup.
    """
    stdout_path = prior_round_dir / f"{reviewer_name}.stdout"
    if not stdout_path.is_file():
        return _MISSING_ARTIFACT_FRAMING
    try:
        raw_stdout = stdout_path.read_text(encoding="utf-8")
    except OSError:
        return _MISSING_ARTIFACT_FRAMING
    return _extract_response_text_from_stdout(reviewer_provider, raw_stdout)


def _read_prior_producer_outcome(prior_round_dir: Path) -> str | None:
    """Read the prior round's producer outcome from manifest.json.

    Returns one of ``"committed"`` / ``"stalled"`` /
    ``"subprocess_error"`` when the manifest has a producer block, or
    ``None`` when the manifest is missing/unreadable, the producer
    block isn't present, or the outcome field isn't a recognized
    string. The caller treats ``None`` as "outcome unknown — don't
    prefix" so a missing-manifest scenario doesn't pollute the
    rendered prompt with a misleading framing message.
    """
    manifest_path = prior_round_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    producer = manifest.get("producer")
    if not isinstance(producer, dict):
        return None
    outcome = producer.get("outcome")
    if outcome in ("committed", "stalled", "subprocess_error"):
        return outcome
    return None


def load_prior_producer_response_text(
    *,
    prior_round_dir: Path,
) -> str:
    """Load the prior round's producer response text.

    Reads ``<prior_round_dir>/producer.stdout`` and returns its content.
    UNLIKE :func:`load_prior_reviewer_response_text`, no per-adapter
    envelope-strip is needed — the producer persistence layer
    (:func:`syncade.persistence.persist_producer_result`) writes
    ``producer_result.output.narrative_text`` to ``producer.stdout``
    on the ``committed`` / ``stalled`` paths (where the producer
    adapter's :meth:`parse_output` already extracted the response text
    from the raw envelope) and writes the raw subprocess stdout on the
    ``subprocess_error`` path (the adapter never produced a
    :class:`ProducerOutput`).

    Asymmetry vs. reviewer artifacts (load-bearing):

    - **Reviewer** ``.stdout`` files hold the **raw subprocess stdout**
      (anthropic JSON envelope / codex JSONL stream). The orchestrator
      runs the per-adapter ``extract_response_text`` to get the
      assistant's text.
    - **Producer** ``.stdout`` files hold the **already-extracted
      narrative text** (on committed/stalled) or raw partial stdout
      (on subprocess_error). Re-extracting the narrative path would
      either (a) be a no-op because the narrative isn't envelope-
      shaped, or (b) silently truncate the narrative if it happens to
      look like a valid envelope (e.g. a producer narrating with a
      JSON dict at the top level). Skipping extraction is correct and
      defensive.

    **the design framing for non-committed prior outcomes:** when the
    prior round's manifest records ``producer.outcome`` of ``stalled``
    or ``subprocess_error``, the helper prefixes the returned text with
    a one-line framing message
    (``"(prior round producer ended with outcome=<X>; partial output
    below)\\n\\n"``) so the round-N producer prose ("if your prior
    attempt errored, treat as a fresh round") has a concrete signal to
    interpret. The framing applies whether the partial output is empty
    or has content. In the production loop the orchestrator terminates
    on ``stalled`` / ``subprocess_error`` so round-N producer wouldn't
    normally see these, but a future loop update that retries past a
    stall would surface them; the framing keeps the prompt coherent
    either way.

    Edge cases mirror the reviewer loader's:

    - producer.stdout missing → :data:`_MISSING_ARTIFACT_FRAMING`.
    - producer.stdout empty + committed/unknown outcome → empty string.
    - producer.stdout empty + stalled/subprocess_error → framing
      message alone (the framing is itself the signal).

    Args:
        prior_round_dir: Path to ``<run_dir>/round-(N-1)``.

    Returns:
        The prior round's producer narrative text (with framing
        prefix on non-committed outcomes), or
        :data:`_MISSING_ARTIFACT_FRAMING` when producer.stdout
        doesn't exist on disk.
    """
    stdout_path = prior_round_dir / "producer.stdout"
    if not stdout_path.is_file():
        return _MISSING_ARTIFACT_FRAMING
    try:
        raw = stdout_path.read_text(encoding="utf-8")
    except OSError:
        return _MISSING_ARTIFACT_FRAMING
    outcome = _read_prior_producer_outcome(prior_round_dir)
    if outcome in ("stalled", "subprocess_error"):
        return f"(prior round producer ended with outcome={outcome}; partial output below)\n\n{raw}"
    return raw


def load_prior_producer_commit_subjects(
    *,
    prior_round_dir: Path,
    repo_root: Path,
) -> str:
    """Load subjects for prior candidates present in the operator repository.

    Reads two artifacts the orchestrator persists for the prior round:

    - ``<prior_round_dir>/manifest.json``'s ``snapshot.commit_sha`` —
      the SHA the prior round's producer repository was provisioned at
      (the producer's STARTING sha).
    - ``<prior_round_dir>/producer.commit.txt`` — the SHA the prior
      producer left HEAD at (the producer's ENDING sha; written by
      :func:`syncade.persistence.persist_producer_result`).

    With both SHAs in hand, runs ``git log --pretty=format:%s
    <starting>..<ending>`` in the operator's repo to get every commit
    subject from the prior candidate range.

    The lookup runs in the OPERATOR'S REPO, not the producer repository.
    Reaching round N requires trusted import and branch advance of the prior
    candidate. Each producer
    repository is fresh and lacks history before its provisioning SHA.

    Reading the starting SHA from the prior round's manifest (rather
    than threading it through ``_run_producer_phase`` from the loop
    driver) keeps the producer-phase signature unchanged at the cost
    of one small JSON read per round > 0.

    Args:
        prior_round_dir: Path to ``<run_dir>/round-(N-1)``.
        repo_root: The operator's repo root — where ``git log`` runs.

    Returns:
        Newline-separated commit subjects, oldest first reading
        top-down (``git log`` is newest-first by default; the helper
        reverses to chronological order for legibility). When any
        artifact is missing or git returns nothing useful, returns a
        sentinel string framed as the absence of commits.
    """
    commit_txt_path = prior_round_dir / "producer.commit.txt"
    if not commit_txt_path.is_file():
        return "(prior round producer.commit.txt not found)"
    try:
        ending_sha = commit_txt_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "(prior round producer.commit.txt unreadable)"
    if not ending_sha:
        return "(prior round producer made no commits)"

    # Read the prior round's manifest to get the starting SHA. The
    # manifest is always present alongside producer.commit.txt on the
    # happy path (persist_round_manifest fires before _run_producer_phase
    # can attach a producer.commit.txt).
    manifest_path = prior_round_dir / "manifest.json"
    if not manifest_path.is_file():
        return "(prior round manifest.json not found)"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "(prior round manifest.json unreadable)"
    snapshot_dict = manifest.get("snapshot")
    if not isinstance(snapshot_dict, dict):
        return "(prior round manifest.json missing snapshot block)"
    starting_sha = snapshot_dict.get("commit_sha")
    if not isinstance(starting_sha, str) or not starting_sha:
        return "(prior round manifest.json missing snapshot.commit_sha)"
    if ending_sha == starting_sha:
        return "(prior round producer made no commits)"

    # Use run_subprocess for consistency with the rest of syncade —
    # same timeout / process-group cleanup discipline.
    from syncade.process import run_subprocess

    try:
        result = run_subprocess(
            ["git", "log", "--pretty=format:%s", f"{starting_sha}..{ending_sha}"],
            cwd=repo_root,
            timeout=5.0,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort lookup
        return f"(git log lookup failed: {type(exc).__name__})"
    if result.returncode != 0:
        return f"(git log returned rc={result.returncode})"
    subjects = result.stdout.strip()
    if not subjects:
        return "(prior round producer commits: git log returned no subjects)"
    # git log is newest-first; reverse to chronological so the
    # producer reads commits in the order they were applied.
    lines = subjects.splitlines()
    return "\n".join(reversed(lines))
