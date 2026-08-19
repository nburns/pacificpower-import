"""Tests for PacificPowerScraper._dump_debug."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pacificpower_import.scraper import PacificPowerScraper, ScraperOptions


@pytest.fixture(autouse=True)
def _enable_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PP_DIAGNOSTICS_ENABLED", "true")


def _make_scraper(tmp_path: Path) -> PacificPowerScraper:
    opts = ScraperOptions(
        username="u",
        password="p",
        storage_dir=tmp_path / "browser",
    )
    return PacificPowerScraper(opts)


def _make_page(tmp_path: Path) -> MagicMock:
    page = MagicMock()
    page.title = AsyncMock(return_value="Test Page")
    page.url = "https://example.com/test"
    page.content = AsyncMock(return_value="<html><body>test</body></html>")
    page.evaluate = AsyncMock(return_value=None)

    async def fake_screenshot(path: str) -> None:
        Path(path).write_bytes(b"\x89PNG\r\n")

    page.screenshot = fake_screenshot
    return page


@pytest.mark.asyncio
async def test_dump_debug_creates_png_and_html(tmp_path: Path) -> None:
    scraper = _make_scraper(tmp_path)
    page = _make_page(tmp_path)

    await scraper._dump_debug(page, "test-tag")

    debug_dir = tmp_path / "debug"
    pngs = list(debug_dir.glob("test-tag-*.png"))
    htmls = list(debug_dir.glob("test-tag-*.html"))
    assert len(pngs) == 1
    assert len(htmls) == 1
    assert htmls[0].read_text() == "<html><body>test</body></html>"


@pytest.mark.asyncio
async def test_dump_debug_uses_storage_dir_parent(tmp_path: Path) -> None:
    """debug dir is storage_dir.parent / 'debug', not /data/debug."""
    storage = tmp_path / "sub" / "browser"
    opts = ScraperOptions(username="u", password="p", storage_dir=storage)
    scraper = PacificPowerScraper(opts)
    page = _make_page(tmp_path)

    await scraper._dump_debug(page, "tag")

    expected_debug = tmp_path / "sub" / "debug"
    assert expected_debug.exists()
    assert len(list(expected_debug.glob("*.png"))) == 1


@pytest.mark.asyncio
async def test_dump_debug_prunes_to_max_pairs(tmp_path: Path) -> None:
    """After 20 pairs exist, writing a 21st should delete the oldest."""
    from pacificpower_import.scraper import _DEBUG_MAX_PAIRS

    storage = tmp_path / "browser"
    debug_dir = tmp_path / "debug"
    debug_dir.mkdir(parents=True)

    opts = ScraperOptions(username="u", password="p", storage_dir=storage)
    scraper = PacificPowerScraper(opts)
    page = _make_page(tmp_path)

    # Pre-populate _DEBUG_MAX_PAIRS pairs with distinct mtimes.
    import time
    base_time = time.time() - 1000
    for i in range(_DEBUG_MAX_PAIRS):
        stem = f"old-tag-{i:02d}"
        png = debug_dir / f"{stem}.png"
        html = debug_dir / f"{stem}.html"
        png.write_bytes(b"\x89PNG")
        html.write_text(f"<html>{i}</html>")
        # Space mtimes so pruner sees a stable order.
        mtime = base_time + i
        import os
        os.utime(str(png), (mtime, mtime))
        os.utime(str(html), (mtime, mtime))

    assert len(list(debug_dir.glob("*.png"))) == _DEBUG_MAX_PAIRS

    # Writing one more should prune the oldest (old-tag-00).
    await scraper._dump_debug(page, "new-tag")

    pngs = list(debug_dir.glob("*.png"))
    # Still at max - the new one was added and the oldest was pruned.
    assert len(pngs) == _DEBUG_MAX_PAIRS
    # The oldest pre-existing file should be gone.
    assert not (debug_dir / "old-tag-00.png").exists()
    assert not (debug_dir / "old-tag-00.html").exists()
    # The newest pre-existing file should still be present.
    assert (debug_dir / f"old-tag-{_DEBUG_MAX_PAIRS - 1:02d}.png").exists()


@pytest.mark.asyncio
async def test_dump_debug_scrubs_dom_before_capture(tmp_path: Path) -> None:
    """DOM inputs must be scrubbed before both the screenshot and the HTML
    dump so residual username/password values never land on disk."""
    scraper = _make_scraper(tmp_path)
    page = _make_page(tmp_path)
    events: list[str] = []

    async def fake_evaluate(js: str) -> None:
        assert "removeAttribute" in js
        events.append("scrub")

    async def fake_screenshot(path: str) -> None:
        events.append("screenshot")
        Path(path).write_bytes(b"\x89PNG\r\n")

    async def fake_content() -> str:
        events.append("content")
        return "<html></html>"

    page.evaluate = fake_evaluate
    page.screenshot = fake_screenshot
    page.content = fake_content

    await scraper._dump_debug(page, "tag")

    assert events[0] == "scrub", "scrub must run before any capture"
    assert "screenshot" in events and "content" in events


@pytest.mark.asyncio
async def test_dump_debug_refuses_capture_if_scrub_fails(tmp_path: Path) -> None:
    """If the DOM scrub itself throws, refuse the whole capture rather than
    write a screenshot / HTML that might carry a credential."""
    scraper = _make_scraper(tmp_path)
    page = _make_page(tmp_path)

    async def failing_evaluate(js: str) -> None:
        raise RuntimeError("scrub broke")

    page.evaluate = failing_evaluate

    await scraper._dump_debug(page, "tag")

    debug_dir = tmp_path / "debug"
    if debug_dir.exists():
        assert list(debug_dir.glob("*.png")) == []
        assert list(debug_dir.glob("*.html")) == []


@pytest.mark.asyncio
async def test_dump_debug_is_noop_when_diagnostics_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When PP_DIAGNOSTICS_ENABLED is not 'true', no files are written."""
    monkeypatch.setenv("PP_DIAGNOSTICS_ENABLED", "false")
    scraper = _make_scraper(tmp_path)
    page = _make_page(tmp_path)

    await scraper._dump_debug(page, "test-tag")

    assert not (tmp_path / "debug").exists()


@pytest.mark.asyncio
async def test_dump_debug_does_not_raise_on_screenshot_failure(tmp_path: Path) -> None:
    """_dump_debug swallows all errors internally."""
    scraper = _make_scraper(tmp_path)

    page = MagicMock()
    page.title = AsyncMock(side_effect=RuntimeError("boom"))
    page.url = "https://example.com"
    page.content = AsyncMock(side_effect=RuntimeError("boom"))

    async def failing_screenshot(path: str) -> None:
        raise RuntimeError("screenshot failed")

    page.screenshot = failing_screenshot

    # Must not raise.
    await scraper._dump_debug(page, "error-tag")
