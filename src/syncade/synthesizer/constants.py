"""Synthesizer constants.

The four model knobs moved to :mod:`syncade.config_cold` in PR-v2-23, where they
are the DEFAULTS of the ``[synthesizer]`` config block. They are re-exported here
because ``persistence`` and ``metrics`` legitimately still want the *historical
default* rather than the operator's current config: when a legacy run's artifacts
never recorded which model the judge used, "the default as it was" is the only
honest fallback. Anything that wants the model actually in effect for THIS run must
read ``SyncadeConfig.synthesizer``, not these.

``SYNTHESIZER_NAME`` stays a real constant — it is an artifact basename, not a knob.
"""

from __future__ import annotations

from syncade.config_cold import (
    SYNTHESIZER_MODEL as SYNTHESIZER_MODEL,
)
from syncade.config_cold import (
    SYNTHESIZER_PERMISSIONS as SYNTHESIZER_PERMISSIONS,
)
from syncade.config_cold import (
    SYNTHESIZER_PROVIDER as SYNTHESIZER_PROVIDER,
)
from syncade.config_cold import (
    SYNTHESIZER_THINKING as SYNTHESIZER_THINKING,
)

SYNTHESIZER_NAME = "synthesizer"
"""Persistence basename for the synthesizer's artifacts
(``<round_dir>/synthesizer.{stdout,stderr,parsed.json,error.txt}``). Reused as the
``name`` of the synthetic :class:`ReviewerConfig` the driver builds, so the adapter's
provider-validation guards pass and the on-disk file naming stays consistent."""
