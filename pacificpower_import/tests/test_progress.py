"""Tests for progress.py."""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import date, timedelta

import pytest

from pacificpower_import import progress
from pacificpower_import.progress import Progress
from pacificpower_import.state import State


# ---------------------------------------------------------------------------
# EWMA update
# ---------------------------------------------------------------------------

def test_ewma_update(tmp_path):
    p = tmp_path / "progress.json"
    progress.start("test", total=5, path=p)

    progress.tick(item_seconds=10.0, path=p)
    state = progress.load(p)
    assert state.ewma_seconds_per_item == pytest.approx(10.0)
    assert state.last_item_seconds == pytest.approx(10.0)

    progress.tick(item_seconds=20.0, path=p)
    state = progress.load(p)
    expected = 0.2 * 20.0 + 0.8 * 10.0
    assert state.ewma_seconds_per_item == pytest.approx(expected)
    assert state.last_item_seconds == pytest.approx(20.0)

    progress.tick(item_seconds=5.0, path=p)
    state = progress.load(p)
    expected2 = 0.2 * 5.0 + 0.8 * expected
    assert state.ewma_seconds_per_item == pytest.approx(expected2)


# ---------------------------------------------------------------------------
# Save / load roundtrip
# ---------------------------------------------------------------------------

def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "progress.json"
    orig = Progress(
        task="backfill-download",
        started_at="2025-01-01T00:00:00+00:00",
        updated_at="2025-01-01T01:00:00+00:00",
        total=25,
        done=12,
        current_label="period=One Month ending=2024-12-01",
        ewma_seconds_per_item=42.5,
        last_item_seconds=38.0,
        last_error="timeout",
        last_error_at="2025-01-01T00:30:00+00:00",
    )
    progress.save(orig, p)
    loaded = progress.load(p)
    assert asdict(loaded) == asdict(orig)


# ---------------------------------------------------------------------------
# Atomic write uses os.replace
# ---------------------------------------------------------------------------

def test_atomic_write_uses_replace(tmp_path):
    p = tmp_path / "progress.json"
    tmp_file = p.with_suffix(".json.tmp")

    orig = Progress(task="test", total=10, done=3)
    progress.save(orig, p)

    assert p.exists()
    assert not tmp_file.exists()


# ---------------------------------------------------------------------------
# load missing file returns default
# ---------------------------------------------------------------------------

def test_load_missing_file_returns_default(tmp_path):
    result = progress.load(tmp_path / "nope.json")
    assert result == Progress()


# ---------------------------------------------------------------------------
# load corrupt file returns default
# ---------------------------------------------------------------------------

def test_load_corrupt_file_returns_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json")
    result = progress.load(p)
    assert result == Progress()


# ---------------------------------------------------------------------------
# eta_seconds
# ---------------------------------------------------------------------------

def test_eta_seconds_none_when_ewma_unset():
    p = Progress(total=10, done=3, ewma_seconds_per_item=None)
    assert progress.eta_seconds(p) is None


def test_eta_seconds_product():
    p = Progress(total=10, done=3, ewma_seconds_per_item=5.0)
    result = progress.eta_seconds(p)
    assert result == pytest.approx(35.0)


def test_eta_seconds_none_when_done_equals_total():
    p = Progress(total=5, done=5, ewma_seconds_per_item=10.0)
    assert progress.eta_seconds(p) is None


# ---------------------------------------------------------------------------
# snapshot_hourly_trickle
# ---------------------------------------------------------------------------

def _make_state(cursor_offset_days: int | None, complete: bool = False) -> State:
    """Build a State with hourly_backfill_cursor set to yesterday - offset."""
    yesterday = date.today() - timedelta(days=1)
    cursor = (yesterday - timedelta(days=cursor_offset_days)) if cursor_offset_days is not None else None
    return State(
        hourly_backfill_cursor=cursor,
        hourly_backfill_complete=complete,
    )


def test_snapshot_hourly_trickle_computes_done_midway(tmp_path):
    p = tmp_path / "progress.json"
    # cursor is 10 days behind yesterday => done=10
    state = _make_state(cursor_offset_days=10)
    progress.snapshot_hourly_trickle(state, backfill_window_days=30, cron_interval_minutes=15, path=p)
    loaded = progress.load(p)
    assert loaded.task == "hourly-trickle"
    assert loaded.total == 30
    assert loaded.done == 10


def test_snapshot_hourly_trickle_done_clamped_to_total(tmp_path):
    p = tmp_path / "progress.json"
    # cursor far in the past — done would exceed total
    state = _make_state(cursor_offset_days=200)
    progress.snapshot_hourly_trickle(state, backfill_window_days=30, cron_interval_minutes=15, path=p)
    loaded = progress.load(p)
    assert loaded.done == 30


def test_snapshot_hourly_trickle_complete_sets_done_equals_total(tmp_path):
    p = tmp_path / "progress.json"
    state = _make_state(cursor_offset_days=5, complete=True)
    progress.snapshot_hourly_trickle(state, backfill_window_days=30, cron_interval_minutes=15, path=p)
    loaded = progress.load(p)
    assert loaded.done == loaded.total


def test_snapshot_hourly_trickle_ewma_from_interval(tmp_path):
    p = tmp_path / "progress.json"
    state = _make_state(cursor_offset_days=5)
    progress.snapshot_hourly_trickle(state, backfill_window_days=30, cron_interval_minutes=20, path=p)
    loaded = progress.load(p)
    assert loaded.ewma_seconds_per_item == pytest.approx(20 * 60.0)


def test_snapshot_hourly_trickle_none_cursor(tmp_path):
    p = tmp_path / "progress.json"
    state = _make_state(cursor_offset_days=None)
    progress.snapshot_hourly_trickle(state, backfill_window_days=30, cron_interval_minutes=15, path=p)
    loaded = progress.load(p)
    assert loaded.done == 0
    assert loaded.total == 30
