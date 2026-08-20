"""Tests for status.py."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from pacificpower_import import status
from pacificpower_import.progress import Progress
from pacificpower_import.state import State
from pacificpower_import.status import LastRun, StatusSnapshot, compute, load_last_run, save_last_run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)


def _state(*, latest_interval_start: datetime | None = None,
           hourly_backfill_cursor: date | None = None,
           hourly_backfill_complete: bool = False) -> State:
    return State(
        latest_interval_start=latest_interval_start,
        hourly_backfill_cursor=hourly_backfill_cursor,
        hourly_backfill_complete=hourly_backfill_complete,
    )


def _progress(*, task: str = "idle", done: int = 0, total: int = 0,
              last_error: str | None = None,
              last_error_at: str | None = None,
              updated_at: str | None = None) -> Progress:
    return Progress(
        task=task,
        done=done,
        total=total,
        last_error=last_error,
        last_error_at=last_error_at,
        updated_at=updated_at,
    )


def _last_run(*, ok: bool | None = None, finished_at: datetime | None = None,
              error: str | None = None) -> LastRun:
    return LastRun(ok=ok, finished_at=finished_at, error=error)


# ---------------------------------------------------------------------------
# test_compute_up_to_date
# ---------------------------------------------------------------------------

def test_compute_up_to_date():
    now = _now()
    today = now.date()
    s = _state(latest_interval_start=datetime(today.year, today.month, today.day, tzinfo=timezone.utc))
    p = _progress()
    lr = _last_run(ok=True, finished_at=now - timedelta(hours=1))
    snap = compute(s, p, lr, cron_expr=None, now=now)
    assert snap.state == "up-to-date"
    assert snap.newest_data_date == today


# ---------------------------------------------------------------------------
# test_compute_backfilling_stale
# ---------------------------------------------------------------------------

def test_compute_backfilling_stale():
    now = _now()
    stale_date = now.date() - timedelta(days=10)
    s = _state(latest_interval_start=datetime(stale_date.year, stale_date.month, stale_date.day, tzinfo=timezone.utc))
    p = _progress()
    lr = _last_run(ok=True, finished_at=now - timedelta(hours=1))
    snap = compute(s, p, lr, cron_expr=None, now=now)
    assert snap.state == "backfilling"
    assert snap.newest_data_date == stale_date


# ---------------------------------------------------------------------------
# test_compute_error_from_last_run
# ---------------------------------------------------------------------------

def test_compute_error_from_last_run():
    now = _now()
    today = now.date()
    s = _state(latest_interval_start=datetime(today.year, today.month, today.day, tzinfo=timezone.utc))
    p = _progress(updated_at=(now - timedelta(hours=2)).isoformat())
    lr = _last_run(ok=False, finished_at=now, error="timeout")
    snap = compute(s, p, lr, cron_expr=None, now=now)
    assert snap.state == "error"
    assert snap.last_error == "timeout"
    assert snap.last_error_at == now


# ---------------------------------------------------------------------------
# test_compute_error_from_progress
# ---------------------------------------------------------------------------

def test_compute_error_from_progress():
    now = _now()
    today = now.date()
    s = _state(latest_interval_start=datetime(today.year, today.month, today.day, tzinfo=timezone.utc))
    error_at = now.isoformat()
    p = _progress(
        last_error="parse failed",
        last_error_at=error_at,
        updated_at=(now - timedelta(hours=1)).isoformat(),
    )
    lr = _last_run()
    snap = compute(s, p, lr, cron_expr=None, now=now)
    assert snap.state == "error"
    assert snap.last_error == "parse failed"


# ---------------------------------------------------------------------------
# test_compute_hourly_incomplete_is_backfilling
# ---------------------------------------------------------------------------

def test_compute_hourly_incomplete_is_backfilling():
    now = _now()
    today = now.date()
    s = _state(
        latest_interval_start=datetime(today.year, today.month, today.day, tzinfo=timezone.utc),
        hourly_backfill_cursor=today - timedelta(days=5),
        hourly_backfill_complete=False,
    )
    p = _progress()
    lr = _last_run(ok=True, finished_at=now - timedelta(hours=1))
    snap = compute(s, p, lr, cron_expr=None, now=now)
    assert snap.state == "backfilling"


# ---------------------------------------------------------------------------
# test_last_run_roundtrip
# ---------------------------------------------------------------------------

def test_last_run_roundtrip(tmp_path):
    path = tmp_path / "last_run.json"
    lr = LastRun(
        started_at=datetime(2026, 8, 20, 6, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 8, 20, 6, 5, 0, tzinfo=timezone.utc),
        mode="daily",
        ok=True,
        error=None,
    )
    save_last_run(lr, path)
    loaded = load_last_run(path)
    assert loaded.started_at == lr.started_at
    assert loaded.finished_at == lr.finished_at
    assert loaded.mode == lr.mode
    assert loaded.ok == lr.ok
    assert loaded.error is None


# ---------------------------------------------------------------------------
# test_next_run_from_cron
# ---------------------------------------------------------------------------

def test_next_run_from_cron():
    pytest.importorskip("croniter")
    # now is 2026-08-20 12:00 UTC; next 06:00 is 2026-08-21 06:00 UTC
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    s = _state()
    p = _progress()
    lr = _last_run()
    snap = compute(s, p, lr, cron_expr="0 6 * * *", now=now)
    assert snap.next_run_at is not None
    assert snap.next_run_at.hour == 6
    assert snap.next_run_at.minute == 0
    assert snap.next_run_at.date() == date(2026, 8, 21)
