"""Pacific Power portal scraper — logs in and downloads Green Button XML.

The download form on the energy-usage page posts to
`/api/energy-usage/getGreenButtonData` with an ENCRYPTED body (base64
ciphertext generated client-side). We can't easily replay that with
plain HTTP, so we drive the actual UI with Playwright: select period +
ending date, click the download link, capture the download event.

The Chromium context is persisted so subsequent runs skip login.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from playwright.async_api import BrowserContext, Download, Page, async_playwright

LOGIN_URL = "https://csapps.pacificpower.net/idm/guest-pay-login"
DASHBOARD_URL = "https://csapps.pacificpower.net/secure/my-account/dashboard"
ENERGY_USAGE_URL = "https://csapps.pacificpower.net/secure/my-account/energy-usage"

Period = Literal["Two Year", "One Year", "One Month", "One Week", "One Day"]

log = logging.getLogger(__name__)


@dataclass
class ScraperOptions:
    username: str
    password: str
    storage_dir: Path  # persistent context dir (cookies + local storage)
    headless: bool = True
    meter_id: str | None = None  # if set, match against the meter dropdown text


class PacificPowerScraper:
    def __init__(self, opts: ScraperOptions):
        self._opts = opts
        self._pw = None
        self._ctx: BrowserContext | None = None

    async def __aenter__(self) -> "PacificPowerScraper":
        self._opts.storage_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        # Chromium's own sandbox needs CAP_SYS_ADMIN / user-ns creation which
        # the add-on doesn't grant. Container + AppArmor + non-root user
        # provide the isolation instead.
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._opts.storage_dir),
            headless=self._opts.headless,
            accept_downloads=True,
            chromium_sandbox=False,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-dev-shm-usage",  # /dev/shm is often small in containers
                "--no-first-run",
                "--disable-features=Translate,MediaRouter",
            ],
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ctx is not None:
            await self._ctx.close()
        if self._pw is not None:
            await self._pw.stop()

    async def download_greenbutton(
        self,
        *,
        period: Period,
        ending_on: date,
        dest_dir: Path,
    ) -> Path:
        """Download one Green Button XML file for the given period + ending date.
        Returns the saved path."""
        assert self._ctx is not None
        page = await self._ctx.new_page()
        try:
            await self._ensure_logged_in(page)
            await page.goto(ENERGY_USAGE_URL, wait_until="networkidle")
            # Angular renders the controls after the initial network-idle. Wait
            # for the meter dropdown to prove the page is ready.
            await page.locator("mat-select").first.wait_for(state="visible", timeout=30_000)

            if self._opts.meter_id:
                await self._select_meter(page, self._opts.meter_id)

            await self._select_period(page, period)
            await self._set_ending_date(page, ending_on)

            dest_dir.mkdir(parents=True, exist_ok=True)
            async with page.expect_download(timeout=60_000) as dl_info:
                # The download control is an <a> inside a mat-list-item with
                # no href, so role=link doesn't always resolve. Match on text.
                await page.get_by_text("DOWNLOAD GREEN BUTTON DATA", exact=True).first.click()
            download: Download = await dl_info.value
            suggested = download.suggested_filename or f"greenbutton_{ending_on.isoformat()}.xml"
            dest = dest_dir / suggested
            await download.save_as(dest)
            log.info("Downloaded Green Button XML → %s (%d bytes)", dest, dest.stat().st_size)
            return dest
        finally:
            await page.close()

    async def _ensure_logged_in(self, page: Page) -> None:
        await page.goto(DASHBOARD_URL, wait_until="networkidle")
        if page.url.startswith(DASHBOARD_URL):
            log.info("Session restored — already logged in")
            return

        # The login form lives inside an <iframe id="loginframe"> running an
        # Azure B2C flow on login.csapps.pacificpower.net.
        log.info("Not authenticated — running login flow")
        await page.goto(LOGIN_URL, wait_until="networkidle")
        await page.wait_for_selector("#loginframe", timeout=30_000)

        login_frame = page.frame_locator("#loginframe")
        await login_frame.locator("#signInName").wait_for(state="visible", timeout=30_000)
        await login_frame.locator("#signInName").fill(self._opts.username)
        await login_frame.locator("#password").fill(self._opts.password)
        await login_frame.locator("#next").click()

        # Post-submit the outer page navigates through the OAuth callback and
        # lands on /secure/my-account/dashboard.
        await page.wait_for_url(
            lambda url: "/secure/my-account/" in url,
            timeout=45_000,
            wait_until="networkidle",
        )
        log.info("Logged in — landed on %s", page.url)

    async def _select_meter(self, page: Page, meter_id: str) -> None:
        # Meter selector is the first mat-select on the page.
        select = page.locator("mat-select").first
        current = (await select.inner_text()).strip()
        if meter_id in current:
            return
        await select.click()
        await page.get_by_role("option", name=lambda n: meter_id in n).first.click()
        # Angular re-renders the graph; brief settle.
        await page.wait_for_load_state("networkidle")

    async def _select_period(self, page: Page, period: Period) -> None:
        selects = page.locator("mat-select")
        period_select = selects.nth(1)
        current = (await period_select.inner_text()).strip()
        if current == period:
            return
        await period_select.click()
        await page.get_by_role("option", name=period, exact=True).click()
        await page.wait_for_load_state("networkidle")

    async def _set_ending_date(self, page: Page, d: date) -> None:
        # The "ending on" date input's attributes vary by selected period
        # (Angular re-renders it). Try several selectors and if none match,
        # continue with whatever the portal's default ending date is.
        candidates = [
            'input[placeholder="Show usage through :"]',
            'input[matinput][aria-haspopup="true"]',
            'input[matinput][min][max]',
            'input.mat-input-element[matinput]:not([type="hidden"])',
        ]
        date_input = None
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=8_000)
                date_input = loc
                log.info("date input matched selector: %s", sel)
                break
            except Exception:
                continue

        if date_input is None:
            debug = Path("/data/date-input-debug.html")
            try:
                debug.parent.mkdir(parents=True, exist_ok=True)
                debug.write_text(await page.content())
                log.warning("date input not found; dumped page to %s; "
                            "downloading with portal default date", debug)
            except Exception:
                log.warning("date input not found; downloading with portal default date")
            return

        formatted = f"{d.month}/{d.day}/{d.year}"
        try:
            await date_input.fill(formatted)
            await date_input.press("Enter")
            await page.wait_for_load_state("networkidle")
        except Exception as e:
            log.warning("failed to set ending date (%s); using portal default", e)


async def download_range(
    opts: ScraperOptions,
    *,
    period: Period,
    ending_on: date,
    dest_dir: Path,
) -> Path:
    """Convenience one-shot: open a scraper, download once, close."""
    async with PacificPowerScraper(opts) as s:
        return await s.download_greenbutton(period=period, ending_on=ending_on, dest_dir=dest_dir)


if __name__ == "__main__":
    # Manual debug: `uv run python -m pacificpower_import.scraper --username ... --password ... --headed`
    import argparse
    import os
    from datetime import timedelta

    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default=os.environ.get("PP_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("PP_PASSWORD"))
    parser.add_argument("--meter-id", default=os.environ.get("PP_METER_ID"))
    parser.add_argument("--period", default="One Month",
                        choices=["Two Year", "One Year", "One Month", "One Week", "One Day"])
    # Portal max date is typically yesterday — today's intervals aren't published yet.
    parser.add_argument("--ending-on", default=(date.today() - timedelta(days=1)).isoformat())
    parser.add_argument("--storage-dir", default="/tmp/pp_browser")
    parser.add_argument("--dest-dir", default="/tmp/pp_downloads")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not args.username or not args.password:
        raise SystemExit("Set PP_USERNAME and PP_PASSWORD (env or --flags)")

    opts = ScraperOptions(
        username=args.username,
        password=args.password,
        storage_dir=Path(args.storage_dir),
        meter_id=args.meter_id,
        headless=not args.headed,
    )
    out = asyncio.run(download_range(
        opts,
        period=args.period,
        ending_on=date.fromisoformat(args.ending_on),
        dest_dir=Path(args.dest_dir),
    ))
    print(out)
