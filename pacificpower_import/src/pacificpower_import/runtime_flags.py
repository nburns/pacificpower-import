"""Runtime-editable config flags.

The HA supervisor writes options to `/data/options.json` when the user
saves the Configuration tab. Reading the file on each access means a
toggle takes effect without a container restart.

Falls back to a `PP_*` env var when the options file is absent, so local
dev + tests keep working.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

_OPTIONS_PATH_ENV = "PP_OPTIONS_PATH"
_DEFAULT_OPTIONS_PATH = "/data/options.json"
_DIAGNOSTICS_ENV_FALLBACK = "PP_DIAGNOSTICS_ENABLED"

log = logging.getLogger(__name__)


def _options() -> dict:
    path = Path(os.environ.get(_OPTIONS_PATH_ENV, _DEFAULT_OPTIONS_PATH))
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        log.warning("runtime_flags: failed to read %s: %s", path, exc)
        return {}


def diagnostics_enabled() -> bool:
    """True when 'diagnostics_enabled' is set in the add-on options."""
    opts = _options()
    if "diagnostics_enabled" in opts:
        return bool(opts["diagnostics_enabled"])
    return os.environ.get(_DIAGNOSTICS_ENV_FALLBACK, "false").lower() == "true"
