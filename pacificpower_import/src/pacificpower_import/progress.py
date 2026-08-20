"""Atomic progress sidecar written to /data/progress.json."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import State

PROGRESS_FILE = Path(os.environ.get("DATA_DIR", "/data")) / "progress.json"

_ALPHA = 0.2


@dataclass
class Progress:
    task: str = "idle"
    started_at: str | None = None
    updated_at: str | None = None
    total: int = 0
    done: int = 0
    current_label: str | None = None
    ewma_seconds_per_item: float | None = None
    last_item_seconds: float | None = None
    last_error: str | None = None
    last_error_at: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save(progress: Progress, path: Path = PROGRESS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(progress), indent=2))
    os.replace(tmp, path)


def load(path: Path = PROGRESS_FILE) -> Progress:
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return Progress()
    return Progress(
        task=raw.get("task", "idle"),
        started_at=raw.get("started_at"),
        updated_at=raw.get("updated_at"),
        total=int(raw.get("total", 0)),
        done=int(raw.get("done", 0)),
        current_label=raw.get("current_label"),
        ewma_seconds_per_item=raw.get("ewma_seconds_per_item"),
        last_item_seconds=raw.get("last_item_seconds"),
        last_error=raw.get("last_error"),
        last_error_at=raw.get("last_error_at"),
    )


def start(task: str, total: int, label: str | None = None, path: Path = PROGRESS_FILE) -> None:
    now = _now_iso()
    p = Progress(
        task=task,
        started_at=now,
        updated_at=now,
        total=total,
        done=0,
        current_label=label,
        ewma_seconds_per_item=None,
        last_item_seconds=None,
        last_error=None,
        last_error_at=None,
    )
    save(p, path)


def tick(label: str | None = None, item_seconds: float | None = None, path: Path = PROGRESS_FILE) -> None:
    p = load(path)
    p.done += 1
    p.current_label = label
    if item_seconds is not None:
        ewma = p.ewma_seconds_per_item
        p.ewma_seconds_per_item = item_seconds if ewma is None else _ALPHA * item_seconds + (1 - _ALPHA) * ewma
        p.last_item_seconds = item_seconds
    p.updated_at = _now_iso()
    save(p, path)


def finish(path: Path = PROGRESS_FILE) -> None:
    p = load(path)
    p.done = p.total
    p.updated_at = _now_iso()
    save(p, path)


def record_error(message: str, path: Path = PROGRESS_FILE) -> None:
    p = load(path)
    p.last_error = message
    p.last_error_at = _now_iso()
    p.updated_at = _now_iso()
    save(p, path)


def eta_seconds(progress: Progress) -> float | None:
    if progress.ewma_seconds_per_item is None:
        return None
    remaining = progress.total - progress.done
    if remaining <= 0:
        return None
    return remaining * progress.ewma_seconds_per_item


def snapshot_hourly_trickle(
    state: "State",
    backfill_window_days: int,
    cron_interval_minutes: int,
    path: Path = PROGRESS_FILE,
) -> None:
    yesterday = date.today() - timedelta(days=1)
    total = backfill_window_days

    if state.hourly_backfill_complete:
        done = total
    else:
        cursor = state.hourly_backfill_cursor
        if cursor is None:
            done = 0
        else:
            done = (yesterday - cursor).days
            done = max(0, min(done, total))

    p = load(path)
    p.task = "hourly-trickle"
    if p.started_at is None:
        p.started_at = _now_iso()
    p.updated_at = _now_iso()
    p.total = total
    p.done = done
    p.ewma_seconds_per_item = cron_interval_minutes * 60.0
    save(p, path)
