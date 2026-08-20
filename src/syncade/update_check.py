"""Is a newer syncade published? — PR-h-field-07 item 2.

A LEAF module: stdlib only, no ``syncade`` imports, so it can be called from anywhere without
dragging the config graph behind it. The installed version is passed IN rather than read here.

Three properties define this module, and every one of them is load-bearing:

**It FAILS OPEN, without exception.** Unreachable host, DNS failure, timeout, non-200, oversized
body, malformed JSON, a version string that is not three integers — every one of them returns
``None`` and the caller proceeds as if no check happened. Syncade spent 2026-08-16 unable to
publish because GitHub was in a partial outage; a check that could convert *our* outage into
every operator's outage is not acceptable at any severity. That is why the fetch catches
``Exception`` broadly rather than enumerating failure types: an enumeration would eventually miss
one, and the miss would be a crash on the review path.

**It NEVER blocks or gates a run.** It reports; it cannot withhold. A ``critical_below`` that
could stop a run would be a remote kill switch, and a cheap one — one JSON line, versus repo
write plus a release plus a publish, and needing no action from the operator to take effect.
Print-only means the worst a compromised manifest can do is lie, which is visible and survivable.

**The background check pings ONCE PER SESSION via ``check_for_update``.** Not once per
invocation and not once per N hours: the operator's rule is that a later invocation in the same
window must not re-ping. The session is marked checked BEFORE the fetch, deliberately — marking
after a *successful* fetch would make a flaky network cost a 1.5s stall on every invocation for
the whole session, where marking first costs at most one missed notice. Explicit operator
invocations (``--update``, ``--doctor``) bypass the session gate and make their own fetch — they
are diagnostic tools, not background checks, and the docs say so.

Severity cannot come from PyPI's JSON, which has no such field. It comes from a manifest we host,
so a version can be marked critical the day a hole is found rather than when the fix ships.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Advisory file locking for the once-per-session check-and-mark. Unix only; absent on Windows.
# The lock is per-process (OS advisory), which is the granularity we need: concurrent CLI
# invocations are separate processes, and we want exactly one to fetch per session.
try:
    import fcntl as _fcntl

    _HAS_FLOCK = True
except ImportError:
    _HAS_FLOCK = False

#: Served from the public repo's default branch. Overridden in tests, not by config — an
#: operator-settable advisory source would let a repo-local file silence a security notice.
MANIFEST_URL = "https://raw.githubusercontent.com/syncade-ai/syncade-ai/main/update-manifest.json"

_TIMEOUT_SECONDS = 1.5
#: Cap the read so a hostile or corrupted response cannot exhaust memory. A truncated body is
#: invalid JSON, which fails open like every other defect.
_MAX_BYTES = 65_536
_STATE_RELATIVE = Path(".syncade") / "update-state.json"
#: Bounds the state file. Entries are trimmed oldest-first by insertion order, which dicts and
#: ``json.dumps`` both preserve.
_KEEP_SESSIONS = 50


@dataclass(frozen=True)
class UpdateNotice:
    """A newer or dangerous version exists. Rendering is the caller's job (item 4)."""

    #: Version to upgrade to, or ``None`` when nothing newer is published — which is a real
    #: state, not an error: a manifest may mark a version critical BEFORE the release that fixes
    #: it. The caller branches on this to avoid telling someone on 0.6.2 to upgrade to 0.6.2.
    latest: str | None
    critical: bool
    reason: str | None
    url: str | None


def session_key() -> str:
    """Identify the window this invocation belongs to.

    A harness exports a session id to its children (both verified readable). A bare shell has
    none, so the parent shell's pid stands in: it is stable for that terminal's life and distinct
    per window, which is exactly the grain required. Pid reuse after the shell exits can skip one
    check — harmless, and cheaper than walking the process tree to do better.
    """
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        value = os.environ.get(var)
        if value:
            return f"{var}:{value}"
    return f"ppid:{os.getppid()}"


def session_checked() -> bool:
    """Has this window already been told? Public so the SKILL-drift half of the notice can share
    one gate with the network half.

    Without it the two halves disagree: the ping is once-per-session but a local file comparison
    is free, so the skill notice re-printed on every invocation — measured, three commands in one
    window, three identical warnings. "Once per window" has to mean the whole notice.
    """
    try:
        return session_key() in _load_state(_state_path())
    except Exception:  # noqa: BLE001
        return False


def _state_path() -> Path:
    """``~/.syncade/update-state.json``. A function, not a constant, so tests can redirect it
    without touching ``$HOME`` — the same reason ``config_loader`` makes its global path one."""
    return Path.home() / _STATE_RELATIVE


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _mark_checked(path: Path, key: str) -> bool:
    """Write the session key into the state file. Returns True on success, False on failure.

    A failed write is propagated rather than swallowed so the caller can skip the fetch:
    if the session cannot be recorded, every subsequent invocation would also fetch,
    violating the once-per-session budget.
    """
    state = _load_state(path)
    state[key] = datetime.now(UTC).isoformat(timespec="seconds")
    while len(state) > _KEEP_SESSIONS:
        state.pop(next(iter(state)))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def _parse(value: object) -> tuple[int, ...] | None:
    """``"1.2.3"`` -> ``(1, 2, 3)``; anything else -> ``None`` (meaning: no information).

    Exactly three ASCII-decimal parts. ``str.isdigit`` alone would accept Devanagari digits and
    ``int`` alone would accept ``"1_0"`` as ten and ``" 1 "`` as one, so both guards are needed
    to keep a manifest from expressing a version we would misread.
    """
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) != 3 or not all(p.isascii() and p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _fetch(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310
            if response.status != 200:
                return None
            # _MAX_BYTES + 1: reading exactly the cap cannot tell "fits" from "truncated
            # but happens to parse". One extra byte makes oversize DETECTABLE rather than
            # silently accepted, which is what the cap claimed to do.
            body = response.read(_MAX_BYTES + 1)
            if len(body) > _MAX_BYTES:
                return None
            payload = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001 — fail open; see the module docstring.
        return None
    return payload if isinstance(payload, dict) else None


def evaluate(manifest: dict | None, installed: str) -> UpdateNotice | None:
    """Decide what the manifest means for ``installed``. Pure — no network, no state.

    Public, so it takes ``object`` in practice and re-checks the type ``_fetch`` already
    guarantees. Belt and braces on purpose: a module whose contract is "never raises" must not
    depend on every caller having gone through the one function that validates the shape.
    """
    if not isinstance(manifest, dict):
        return None
    have = _parse(installed)
    if have is None:
        return None

    latest_text = _text(manifest.get("latest"))
    latest = _parse(latest_text)
    newer = latest is not None and latest > have

    # Via _text like `latest`, so surrounding whitespace is a formatting artifact in BOTH fields
    # rather than a rejection in one and an acceptance in the other.
    critical_below = _parse(_text(manifest.get("critical_below")))
    critical = critical_below is not None and have < critical_below

    if not (newer or critical):
        return None
    return UpdateNotice(
        latest=latest_text if newer else None,
        critical=critical,
        reason=_text(manifest.get("critical_reason")) if critical else None,
        url=_text(manifest.get("critical_url")) if critical else None,
    )


def _exclusive_check_and_mark(path: Path, key: str) -> bool:
    """Atomically check whether this session is already recorded and mark it if not.

    Uses an advisory ``flock`` so that concurrent first invocations in the same session
    produce exactly one fetch rather than N. Measured without this: 25 simultaneous
    same-session processes produced 14 fetches, exceeding the one-GET budget.

    Falls back to the non-atomic path when locking is unavailable (Windows, unwritable dir).
    When the state write fails in either path, returns False (skip the fetch): if the session
    cannot be recorded, every subsequent invocation would fetch again, violating the
    once-per-session budget. Silence is the right trade-off — a skipped notice is cheaper
    than repeated network stalls.

    Returns True when this call is first-in-session (go ahead and fetch), False otherwise.
    """
    lock_path = path.with_suffix(".lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a") as lf:
            if _HAS_FLOCK:
                try:
                    _fcntl.flock(lf, _fcntl.LOCK_EX)
                except OSError:
                    pass  # advisory lock unavailable; continue without
            if key in _load_state(path):
                return False
            return _mark_checked(path, key)
    except Exception:  # noqa: BLE001
        # Cannot open or lock the lock file (permissions, unwritable dir, etc.).
        # Fall back to the non-atomic path.
        if key in _load_state(path):
            return False
        return _mark_checked(path, key)


#: Sentinel distinguishing "not fetched yet" from a fetch that legitimately returned ``None``.
_UNFETCHED = object()
_manifest_cache: object = _UNFETCHED


def manifest_once(*, enabled: bool) -> dict | None:
    """The update manifest, fetched AT MOST ONCE per process. ``None`` when disabled or unreadable.

    THE single egress point for the manifest. Three callers want it — the startup notice,
    ``--update`` and ``--doctor`` — and before this each fetched independently, so a CLI
    ``--doctor`` made two requests and ``--update`` fetched even with ``[update] check = false``.
    That made `README`'s "one network call of its own, suppressible" FALSE, which is a published
    privacy claim rather than a tidiness point. A blind panel raised it in all four rounds of one
    dogfood and three producer attempts each fixed a different facet, because the fix is to remove
    the duplication, not to patch each site.

    ``enabled`` is a PARAMETER, not read here: this module is a stdlib-only leaf and importing the
    config surface to answer it would end that. Callers hold the config; this holds the socket.

    A failed fetch is cached too. A syncade process is short-lived, and retrying inside one would
    reintroduce exactly the multiplication this exists to prevent.
    """
    global _manifest_cache
    if not enabled:
        return None
    if _manifest_cache is _UNFETCHED:
        _manifest_cache = _fetch(MANIFEST_URL)
    return _manifest_cache


def check_for_update(installed: str, *, enabled: bool = True) -> UpdateNotice | None:
    """One ping per session, or ``None``. Never raises, never blocks, never touches exit codes.

    ``CI`` is honoured as the near-universal marker for "no human is reading this", so automation
    makes no network call without needing a config file it has nowhere to put.
    """
    if not enabled or os.environ.get("CI"):
        return None
    try:
        path = _state_path()
        key = session_key()
        # Marked before the fetch, so a process killed DURING the request has still spent its
        # session and cannot re-ping. The exclusive check-and-mark also prevents concurrent
        # first invocations from all fetching simultaneously.
        if not _exclusive_check_and_mark(path, key):
            return None
        return evaluate(manifest_once(enabled=True), installed)
    except Exception:  # noqa: BLE001
        # The outermost guarantee, not a duplicate of the inner ones: `Path.home()` raises
        # RuntimeError with no $HOME and no passwd entry, and this runs on the review path where
        # any escape would fail a run over a version notice.
        return None
