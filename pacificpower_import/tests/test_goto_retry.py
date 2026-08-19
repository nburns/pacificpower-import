"""Tests for _goto_with_retry in scraper.py."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from pacificpower_import.scraper import _goto_with_retry


class _FakePage:
    """Minimal page stand-in: goto raises or returns based on side_effect list."""

    def __init__(self, side_effects: list):
        self._effects = iter(side_effects)

    async def goto(self, url: str, wait_until: str = "networkidle") -> None:
        effect = next(self._effects)
        if isinstance(effect, Exception):
            raise effect


@pytest.mark.asyncio
async def test_retryable_error_retries_and_succeeds():
    transient = Exception("net::ERR_NETWORK_CHANGED at https://example.com")
    page = _FakePage([transient, transient, None])
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    with patch("pacificpower_import.scraper.asyncio.sleep", side_effect=fake_sleep):
        await _goto_with_retry(page, "https://example.com", tries=3, _delay_s=(5, 15))

    assert slept == [5, 15]


@pytest.mark.asyncio
async def test_retryable_error_eventually_raises_after_all_retries():
    transient = Exception("net::ERR_TIMED_OUT at https://example.com")
    page = _FakePage([transient, transient, transient])
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    with patch("pacificpower_import.scraper.asyncio.sleep", side_effect=fake_sleep):
        with pytest.raises(Exception, match="ERR_TIMED_OUT"):
            await _goto_with_retry(page, "https://example.com", tries=3, _delay_s=(5, 15))

    assert len(slept) == 2


@pytest.mark.asyncio
async def test_non_retryable_error_raises_immediately():
    boom = Exception("Page.goto: net::ERR_CERT_AUTHORITY_INVALID")
    page = _FakePage([boom])
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    with patch("pacificpower_import.scraper.asyncio.sleep", side_effect=fake_sleep):
        with pytest.raises(Exception, match="ERR_CERT_AUTHORITY_INVALID"):
            await _goto_with_retry(page, "https://example.com", tries=3, _delay_s=(5, 15))

    assert slept == []


@pytest.mark.asyncio
async def test_succeeds_on_first_try_no_sleep():
    page = _FakePage([None])
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    with patch("pacificpower_import.scraper.asyncio.sleep", side_effect=fake_sleep):
        await _goto_with_retry(page, "https://example.com", tries=3, _delay_s=(5, 15))

    assert slept == []


@pytest.mark.asyncio
async def test_all_retryable_substrings_are_caught():
    tags = [
        "ERR_NETWORK_CHANGED",
        "ERR_INTERNET_DISCONNECTED",
        "ERR_TIMED_OUT",
        "ERR_CONNECTION_RESET",
        "ERR_ABORTED",
        "ERR_NAME_NOT_RESOLVED",
    ]
    for tag in tags:
        page = _FakePage([Exception(f"net::{tag}"), None])

        async def fake_sleep(s: float) -> None:
            pass

        with patch("pacificpower_import.scraper.asyncio.sleep", side_effect=fake_sleep):
            await _goto_with_retry(page, "https://example.com", tries=2, _delay_s=(0,))
