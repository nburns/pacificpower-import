"""Persistent bookkeeping for the importer."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class State:
    last_backfill: datetime | None = None
    last_incremental: datetime | None = None
    cumulative_wh: float = 0.0
    # Latest interval start we've imported. Used to compute the running sum
    # without re-reading old data.
    latest_interval_start: datetime | None = None
    # Bump when the backfill strategy changes in an incompatible way — an
    # existing state with an older version triggers a fresh backfill on
    # startup (which clears prior statistics first).
    backfill_version: int = 0
    # Extra metadata (e.g. discovered account/meter identifiers) for future
    # use; unstructured to avoid churn.
    extras: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        return cls(
            last_backfill=_parse_dt(raw.get("last_backfill")),
            last_incremental=_parse_dt(raw.get("last_incremental")),
            cumulative_wh=float(raw.get("cumulative_wh", 0.0)),
            latest_interval_start=_parse_dt(raw.get("latest_interval_start")),
            backfill_version=int(raw.get("backfill_version", 0)),
            extras=raw.get("extras", {}),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = asdict(self)
        for k in ("last_backfill", "last_incremental", "latest_interval_start"):
            v = raw.get(k)
            raw[k] = v.isoformat() if isinstance(v, datetime) else v
        path.write_text(json.dumps(raw, indent=2))


def _parse_dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None
