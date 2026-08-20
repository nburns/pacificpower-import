"""Compute a uniform StatusSnapshot from state + progress + last_run heartbeat."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:
    from .progress import Progress
    from .state import State

LAST_RUN_FILE = Path(os.environ.get("DATA_DIR", "/data")) / "last_run.json"

GRACE_DAYS = 2

BadgeState = Literal["backfilling", "up-to-date", "error"]

# How many days back to show last_error in the summary block.
_ERROR_DISPLAY_DAYS = 7


@dataclass
class LastRun:
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    mode: Optional[str] = None
    ok: Optional[bool] = None
    error: Optional[str] = None


@dataclass
class StatusSnapshot:
    state: BadgeState
    newest_data_date: Optional[date]
    last_run_started_at: Optional[datetime]
    last_run_finished_at: Optional[datetime]
    last_run_ok: Optional[bool]
    last_error: Optional[str]
    last_error_at: Optional[datetime]
    next_run_at: Optional[datetime]
    schedule_cron: Optional[str]


def _parse_dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def load_last_run(path: Path = LAST_RUN_FILE) -> LastRun:
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return LastRun()
    ok_raw = raw.get("ok")
    return LastRun(
        started_at=_parse_dt(raw.get("started_at")),
        finished_at=_parse_dt(raw.get("finished_at")),
        mode=raw.get("mode"),
        ok=bool(ok_raw) if ok_raw is not None else None,
        error=raw.get("error"),
    )


def save_last_run(lr: LastRun, path: Path = LAST_RUN_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = {
        "started_at": lr.started_at.isoformat() if lr.started_at is not None else None,
        "finished_at": lr.finished_at.isoformat() if lr.finished_at is not None else None,
        "mode": lr.mode,
        "ok": lr.ok,
        "error": lr.error,
    }
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def compute(
    state: "State",
    p: "Progress",
    last_run: LastRun,
    cron_expr: str | None,
    now: datetime,
) -> StatusSnapshot:
    # Determine newest_data_date.
    newest_data_date: date | None = None
    if (
        state.hourly_backfill_cursor is not None
        and state.latest_interval_start is not None
        and state.hourly_backfill_cursor > state.latest_interval_start.date()
    ):
        newest_data_date = state.hourly_backfill_cursor
    elif state.latest_interval_start is not None:
        newest_data_date = state.latest_interval_start.date()

    # Parse progress timestamps for comparison.
    progress_error_at: datetime | None = _parse_dt(p.last_error_at)
    progress_updated_at: datetime | None = _parse_dt(p.updated_at)

    # Determine last_error and last_error_at from either source.
    last_error: str | None = None
    last_error_at: datetime | None = None

    if last_run.ok is False and last_run.error:
        last_error = last_run.error
        last_error_at = last_run.finished_at
    elif p.last_error and progress_error_at is not None:
        last_error = p.last_error
        last_error_at = progress_error_at

    # Determine badge state.
    badge: BadgeState

    # error: last completed run failed and that failure is more recent than progress update,
    # OR progress recorded an error more recent than any successful last_run.
    is_error = False
    if (
        last_run.ok is False
        and last_run.finished_at is not None
        and (
            progress_updated_at is None
            or last_run.finished_at > progress_updated_at
        )
    ):
        is_error = True
    elif (
        progress_error_at is not None
        and (
            last_run.finished_at is None
            or not last_run.ok
            or progress_error_at > last_run.finished_at
        )
    ):
        is_error = True

    if is_error:
        badge = "error"
    else:
        today = now.date()
        hourly_incomplete = (
            state.hourly_backfill_cursor is not None
            and not state.hourly_backfill_complete
        )
        task_active = p.task in {"backfill-download", "importing"} and p.done < p.total
        stale = newest_data_date is None or newest_data_date < today - timedelta(days=GRACE_DAYS)

        if task_active or hourly_incomplete or stale:
            badge = "backfilling"
        else:
            badge = "up-to-date"

    # next_run_at from cron.
    next_run_at: datetime | None = None
    if cron_expr:
        try:
            from croniter import croniter
            it = croniter(cron_expr, now)
            next_run_at = it.get_next(datetime).replace(tzinfo=timezone.utc)
        except Exception:
            pass

    return StatusSnapshot(
        state=badge,
        newest_data_date=newest_data_date,
        last_run_started_at=last_run.started_at,
        last_run_finished_at=last_run.finished_at,
        last_run_ok=last_run.ok,
        last_error=last_error,
        last_error_at=last_error_at,
        next_run_at=next_run_at,
        schedule_cron=cron_expr,
    )
