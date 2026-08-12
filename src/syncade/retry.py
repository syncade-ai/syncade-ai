"""Bounded retry on transient reviewer / synthesizer / producer API errors (H5).

A single transient provider blip — an HTTP 429, a 5xx, or a dropped socket —
in a reviewer, the synthesizer, or the producer subprocess otherwise aborts
the whole review loop at exit 40 with no second attempt, which makes
real-provider runs non-deterministic (the H5 finding: a transient anthropic
reviewer error in round 1 took down an entire run). This module is the ONE
place that decides (a) whether a failure is transient enough to retry and
(b) how long to wait between attempts, so the per-reviewer dispatch
(:mod:`syncade.dispatcher`), the cold-synth driver
(:mod:`syncade.synthesizer.driver`), and the producer (:mod:`syncade.producer`
— its retry is SIDE-EFFECT-safe, PR-v2-22) cannot drift apart on that definition.

Classification — deliberately minimal (H5 scope guard). The taxonomy is a
*whitelist*: when in doubt, do NOT retry.

- ONLY a :class:`~syncade.adapters.base.ReviewerInvocationError` is ever
  retried, and only when it wraps a recognizable transient API/network
  condition. That is the single exception type both the reviewer adapter's
  ``parse_output`` and the synthesizer adapter's
  ``extract_final_text`` raise to wrap a *subprocess-side*
  provider failure (auth, model-unavailable, rate-limit, network).
- Transient ⇔ HTTP status 429 or 5xx, OR — when the adapter could not surface
  a status — a stderr/message substring in :data:`_TRANSIENT_MARKERS`.
- EVERYTHING else is permanent and never retried: parse/contract failures
  (:class:`~syncade.findings.ReviewerOutputError`,
  :class:`~syncade.synthesis.SynthesizerOutputError`), a missing CLI binary
  (:class:`~syncade.process.SubprocessNotFoundError`), a wall-clock timeout
  (:class:`~syncade.process.SubprocessTimeoutError` — that is the budget, not
  a blip; note its message contains "timed out", so the type gate below MUST
  fire before the substring scan), bad config, and any programming bug.
  Retrying a deterministic failure only burns budget on an outcome that
  cannot change.
- A status-bearing error that is NOT 429/5xx (400/401/403/404) is permanent:
  bad-request / auth / permission / missing-model do not get better on a
  second identical attempt.

ASSUMPTION / open question: the no-status path leans on substring markers,
which is heuristic. It only applies when an adapter raised a
``ReviewerInvocationError`` without an ``api_error_status`` — today the real
adapters set the status when they have one, so the markers are a fallback for
network-layer failures (dropped sockets) that never reached an HTTP status.
"""

from __future__ import annotations

import random
import time

from syncade.adapters.base import ReviewerInvocationError

# Extra attempts AFTER the first try, i.e. up to 3 subprocess attempts total.
# Bounded so a hard provider outage still fails the leg promptly rather than
# looping indefinitely.
MAX_RETRIES = 2

# Lowercased substrings checked ONLY when a ReviewerInvocationError carries no
# api_error_status. Kept small and high-signal: a marker that also matched a
# deterministic provider verdict would wrongly trigger retries.
_TRANSIENT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "temporarily unavailable",
    "temporary failure",
    "connection reset",
    "connection aborted",
    "connection refused",
    "socket",
    "network",
    "timed out",
    "timeout",
    "econnreset",
    "etimedout",
    # A provider dropping the response stream mid-flight. Empirically the most
    # common real transient we hit, and it matched NONE of the markers above:
    # codex reports it as `stream disconnected before completion: error sending
    # request for url (...)` with no HTTP status, so the status gate cannot
    # catch it either. Two dogfood runs died at exit 40 — losing every
    # remaining round — because the retry that exists for exactly this case
    # never fired.
    "stream disconnected",
    "error sending request",
)


# Provider QUOTA EXHAUSTION — deliberately NOT in _TRANSIENT_MARKERS (PR-h-field-02).
#
# The two need opposite handling and merging them would be worse than the bug. Retrying a
# usage limit is pointless — the window has not moved — so folding these into the transient
# list would fire MAX_RETRIES more doomed reviewer pairs against an exhausted quota before
# dying anyway. Classifying it permanent (today's behaviour, by omission) is also wrong: the
# run is resumable and the operator only has to wait.
#
# Read out of the shipped codex binary (codex-cli 0.145.0): its error enum carries
# `UsageLimitReached` and `QuotaExceeded`, and the user-facing text is
# "You've hit your usage limit for ".
#
# Markers are SPECIFIC on purpose. A bare "quota" or "usage limit" would match a reviewer
# discussing rate limiting in the code under review — which is exactly the string that turned
# up in the field runs' stdout and briefly looked like evidence. A false positive here tells an
# operator to wait out a limit they have not hit, so the cost of looseness is a wrong diagnosis.
_USAGE_LIMIT_MARKERS = (
    "usagelimitreached",
    "quotaexceeded",
    # snake_case, and the MOST common form in the binary (35 occurrences, more than any other).
    # Also the safest: prose says "usage limit" with a space, so the underscore cannot collide
    # with a reviewer discussing rate limiting. The first version of this tuple measured that
    # number, wrote it into the brief, and then left the marker out.
    "usage_limit",
    "usage limit reached",
    "hit your usage limit",
    "exceeded your quota",
    "quota exceeded",
)


def is_usage_limit_error(exc: BaseException) -> bool:
    """Return ``True`` iff ``exc`` is the provider refusing on exhausted quota.

    Neither transient nor permanent: the correct response is to stop cleanly and let the
    operator resume once the window resets, which is exit 25's existing contract.
    """
    if not isinstance(exc, ReviewerInvocationError):
        return False
    text = f"{exc} {exc.stderr}".lower()
    return any(marker in text for marker in _USAGE_LIMIT_MARKERS)


def is_transient_api_error(exc: BaseException) -> bool:
    """Return ``True`` iff ``exc`` is a transient provider/network failure
    worth one more subprocess attempt. See the module docstring for the
    (whitelist) taxonomy."""
    if not isinstance(exc, ReviewerInvocationError):
        return False
    if is_usage_limit_error(exc):
        # Quota is its own outcome; retrying it just burns the remaining attempts.
        return False
    status = exc.api_error_status
    if status == 429 or (status is not None and 500 <= status <= 599):
        return True
    if status is not None:
        # A concrete non-5xx/429 status is a terminal provider verdict.
        return False
    text = f"{exc} {exc.stderr}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def backoff_sleep(retry_index: int) -> None:
    """Sleep a short, fully-jittered interval before retry ``retry_index``
    (1-based).

    Exponential base (0.5s, 1s, …) with full jitter so concurrent reviewers
    that all hit a rate limit don't re-fire in lockstep. Small by design: the
    goal is to ride out a brief blip, not to implement a production backoff
    schedule. Isolated here so tests can monkeypatch it to a no-op.
    """
    base = 0.5 * (2 ** (retry_index - 1))
    time.sleep(random.uniform(0.0, base))
