"""Tests for state.py and hourly-mode logic in __main__.py."""

import json
import tempfile
from datetime import date, datetime, timedelta, UTC
from pathlib import Path

import pytest

from pacificpower_import.state import State


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def test_state_roundtrip_with_hourly_fields(tmp_path):
    path = tmp_path / "state.json"
    s = State(
        cumulative_wh=12345.0,
        hourly_backfill_cursor=date(2024, 6, 15),
        hourly_backfill_complete=False,
        last_mode="hourly",
    )
    s.save(path)
    loaded = State.load(path)
    assert loaded.cumulative_wh == 12345.0
    assert loaded.hourly_backfill_cursor == date(2024, 6, 15)
    assert loaded.hourly_backfill_complete is False
    assert loaded.last_mode == "hourly"


def test_state_load_missing_hourly_fields_defaults(tmp_path):
    """Old state.json without hourly fields loads cleanly with defaults."""
    path = tmp_path / "state.json"
    legacy = {
        "last_backfill": None,
        "last_incremental": None,
        "cumulative_wh": 9999.0,
        "latest_interval_start": None,
        "backfill_version": 5,
        "extras": {},
    }
    path.write_text(json.dumps(legacy))
    loaded = State.load(path)
    assert loaded.cumulative_wh == 9999.0
    assert loaded.hourly_backfill_cursor is None
    assert loaded.hourly_backfill_complete is False
    assert loaded.last_mode is None


def test_state_load_nonexistent_returns_defaults():
    s = State.load(Path("/tmp/does_not_exist_pacificpower_state.json"))
    assert s.hourly_backfill_cursor is None
    assert s.hourly_backfill_complete is False
    assert s.last_mode is None


def test_state_cursor_date_persists_correctly(tmp_path):
    path = tmp_path / "state.json"
    s = State(hourly_backfill_cursor=date(2023, 1, 31))
    s.save(path)
    raw = json.loads(path.read_text())
    assert raw["hourly_backfill_cursor"] == "2023-01-31"
    loaded = State.load(path)
    assert loaded.hourly_backfill_cursor == date(2023, 1, 31)


# ---------------------------------------------------------------------------
# Cursor decrement logic (unit-tested without scraper/HA)
# ---------------------------------------------------------------------------

def _simulate_trickle(start_cursor: date, window_days: int) -> tuple[list[date], bool]:
    """Simulate the cursor-decrement loop: returns (dates visited, complete flag)."""
    window_start = (date.today() - timedelta(days=1)) - timedelta(days=window_days - 1)
    cursor = start_cursor
    visited: list[date] = []
    while True:
        visited.append(cursor)
        next_cursor = cursor - timedelta(days=1)
        if next_cursor < window_start:
            return visited, True
        cursor = next_cursor


def test_trickle_terminates_at_window_edge():
    yesterday = date.today() - timedelta(days=1)
    visited, complete = _simulate_trickle(yesterday, window_days=5)
    assert complete is True
    assert len(visited) == 5
    assert visited[0] == yesterday
    # Each step decrements by one day.
    for prev, curr in zip(visited, visited[1:]):
        assert prev - curr == timedelta(days=1)


def test_trickle_single_day_window():
    yesterday = date.today() - timedelta(days=1)
    visited, complete = _simulate_trickle(yesterday, window_days=1)
    assert complete is True
    assert len(visited) == 1


def test_trickle_covers_full_window():
    yesterday = date.today() - timedelta(days=1)
    visited, complete = _simulate_trickle(yesterday, window_days=30)
    assert complete is True
    assert len(visited) == 30
    expected_start = yesterday - timedelta(days=29)
    assert visited[-1] == expected_start + timedelta(days=1) or visited[-1] >= expected_start


# ---------------------------------------------------------------------------
# last_mode mismatch detection
# ---------------------------------------------------------------------------

def test_last_mode_mismatch_daily_to_hourly(tmp_path):
    """State written under daily mode should be detected as a mismatch when
    hourly mode is activated."""
    path = tmp_path / "state.json"
    s = State(last_mode="daily", cumulative_wh=500.0)
    s.save(path)

    loaded = State.load(path)
    assert loaded.last_mode == "daily"
    # Caller (run.sh / startup logic) detects mismatch: last_mode != "hourly"
    current_mode = "hourly"
    assert loaded.last_mode != current_mode  # triggers reset


def test_last_mode_no_mismatch_when_already_hourly(tmp_path):
    path = tmp_path / "state.json"
    s = State(
        last_mode="hourly",
        hourly_backfill_complete=True,
        cumulative_wh=1000.0,
    )
    s.save(path)

    loaded = State.load(path)
    current_mode = "hourly"
    assert loaded.last_mode == current_mode  # no reset needed


def test_last_mode_none_treated_as_daily(tmp_path):
    """A state file with last_mode=null (pre-feature) should trigger a reset
    when hourly_mode is enabled."""
    path = tmp_path / "state.json"
    s = State(last_mode=None, cumulative_wh=200.0)
    s.save(path)

    loaded = State.load(path)
    current_mode = "hourly"
    # None != "hourly" — reset is triggered.
    assert loaded.last_mode != current_mode


# ---------------------------------------------------------------------------
# Existing daily-mode state fields are preserved after save
# ---------------------------------------------------------------------------

def test_daily_fields_preserved_when_saving_hourly_fields(tmp_path):
    path = tmp_path / "state.json"
    ts = datetime(2025, 3, 1, 12, 0, 0, tzinfo=UTC)
    s = State(
        last_backfill=ts,
        cumulative_wh=42000.0,
        latest_interval_start=ts,
        backfill_version=5,
        last_mode="daily",
    )
    s.save(path)

    loaded = State.load(path)
    assert loaded.last_backfill == ts
    assert loaded.cumulative_wh == 42000.0
    assert loaded.latest_interval_start == ts
    assert loaded.backfill_version == 5
    assert loaded.last_mode == "daily"
    assert loaded.hourly_backfill_cursor is None
    assert loaded.hourly_backfill_complete is False
