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
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from playwright.async_api import BrowserContext, Download, Page, async_playwright

_DEBUG_MAX_PAIRS = 20

LOGIN_URL = "https://csapps.pacificpower.net/idm/guest-pay-login"
DASHBOARD_URL = "https://csapps.pacificpower.net/secure/my-account/dashboard"
ENERGY_USAGE_URL = "https://csapps.pacificpower.net/secure/my-account/energy-usage"
BILLING_HISTORY_URL = "https://csapps.pacificpower.net/secure/my-account/billing-payment-history"

Period = Literal["Two Year", "One Year", "One Month", "One Week", "One Day"]

log = logging.getLogger(__name__)

_RETRYABLE_ERRORS = (
    "ERR_NETWORK_CHANGED",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_TIMED_OUT",
    "ERR_CONNECTION_RESET",
    "ERR_ABORTED",
    "ERR_NAME_NOT_RESOLVED",
)
_RETRY_DELAYS = (5, 15)


def _sanitize_url_for_log(url: str) -> str:
    """Drop query params. State/code/token params commonly land there."""
    q = url.find("?")
    return url if q < 0 else url[:q] + "?[stripped]"


# JS scrub that runs in the live page before we capture the screenshot AND
# before we serialize HTML. Blanks every input value (both the .value
# property and the value attribute Chromium serializes in outerHTML). Also
# strips <script> textContent because Angular apps can hold in-flight auth
# tokens there. Runs on the live page - we're already in an error-recovery
# path, so mutating the DOM is safe.
_SCRUB_JS = r"""
() => {
  const inputs = document.querySelectorAll('input, textarea');
  for (const el of inputs) {
    try {
      el.value = '';
      el.setAttribute('value', '');
      el.removeAttribute('data-value');
    } catch (e) {}
  }
  const scripts = document.querySelectorAll('script');
  for (const s of scripts) {
    try { s.textContent = '/* stripped */'; } catch (e) {}
  }
}
"""


async def _goto_with_retry(
    page: Page,
    url: str,
    *,
    tries: int = 3,
    wait_until: str = "networkidle",
    _delay_s: tuple[float, ...] = _RETRY_DELAYS,
) -> None:
    last_exc: Exception | None = None
    for attempt in range(tries):
        try:
            await page.goto(url, wait_until=wait_until)
            return
        except Exception as exc:
            msg = str(exc)
            if not any(tag in msg for tag in _RETRYABLE_ERRORS):
                raise
            last_exc = exc
            if attempt + 1 < tries:
                delay = _delay_s[min(attempt, len(_delay_s) - 1)]
                log.info("goto %s failed with transient error (%s); retrying in %ss", url, msg, delay)
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


@dataclass
class ScraperOptions:
    username: str
    password: str
    storage_dir: Path  # persistent context dir (cookies + local storage)
    headless: bool = True
    meter_id: str | None = None  # if set, match against the meter dropdown text


@dataclass(frozen=True)
class Bill:
    """One 'Regular Bill' row from the billing-payment-history table."""
    bill_date: date
    amount_usd: float
    description: str


def _parse_bill_row(cells: list[str]) -> Bill | None:
    """Table columns are Date | Description | Amount (+ trailing MORE cell).
    We keep only rows whose description contains 'Bill' — 'Regular Bill',
    'Adjustment Bill', etc. — and skip payments/refunds."""
    if len(cells) < 3:
        return None
    date_str, desc, amount_str = cells[0], cells[1], cells[2]
    if "bill" not in desc.lower():
        return None
    try:
        d = datetime.strptime(date_str, "%m/%d/%y").date()
    except ValueError:
        return None
    m = re.search(r"-?\d[\d,]*\.\d{2}", amount_str.replace(",", ""))
    if not m:
        return None
    return Bill(bill_date=d, amount_usd=float(m.group(0)), description=desc)


class PacificPowerScraper:
    def __init__(self, opts: ScraperOptions):
        self._opts = opts
        self._pw = None
        self._ctx: BrowserContext | None = None

    async def __aenter__(self) -> "PacificPowerScraper":
        sd = self._opts.storage_dir
        if sd.exists():
            # Recurse: Chromium stores cookies under Default/, so a non-recursive
            # listing showed "1 files, 0.0 KB" even when the context was fully
            # populated and misled us into thinking sessions weren't persisting.
            files = [p for p in sd.rglob("*") if p.is_file()]
            total = sum(p.stat().st_size for p in files)
            log.info("storage_dir %s: exists, %d files, %.1f KB total",
                     sd, len(files), total / 1024)
        else:
            log.info("storage_dir %s: does not exist (fresh context, login required)", sd)
        sd.mkdir(parents=True, exist_ok=True)
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
        try:
            initial_cookies = await self._ctx.cookies()
            pp_initial = [c for c in initial_cookies if "pacificpower" in c["domain"]]
            log.info("cookies restored from disk: total=%d pacificpower.net=%d",
                     len(initial_cookies), len(pp_initial))
        except Exception as exc:
            log.warning("could not read initial cookies: %s", exc)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ctx is not None:
            await self._ctx.close()
        if self._pw is not None:
            await self._pw.stop()

    async def _dump_debug(self, page: Page, tag: str) -> None:
        """Capture a screenshot + HTML dump for post-mortem debugging.
        Never raises - errors are logged as warnings. Gated on the live
        'diagnostics_enabled' option so failed runs don't accrue disk
        usage when the user hasn't opted in to diagnostics."""
        from .runtime_flags import diagnostics_enabled
        if not diagnostics_enabled():
            log.info("debug dump skipped (diagnostics disabled); tag=%s", tag)
            return
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            debug_dir = self._opts.storage_dir.parent / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{tag}-{ts}"

            try:
                title = await page.title()
                log.info("debug dump: url=%s title=%r",
                         _sanitize_url_for_log(page.url), title)
            except Exception as exc:
                log.warning("debug dump: could not read page url/title: %s", exc)

            # Scrub DOM input values + <script> bodies BEFORE either the
            # screenshot or the HTML dump. If the scrub itself throws, refuse
            # the entire capture rather than risk a credential landing on
            # disk. This is the load-bearing safeguard for the login page.
            try:
                await page.evaluate(_SCRUB_JS)
            except Exception as exc:
                log.warning("debug dump: DOM scrub failed (%s); refusing capture to protect credentials", exc)
                return

            png_path = debug_dir / f"{stem}.png"
            try:
                await page.screenshot(path=str(png_path))
                log.info("debug dump: screenshot -> %s (%d bytes)", png_path, png_path.stat().st_size)
            except Exception as exc:
                log.warning("debug dump: screenshot failed: %s", exc)

            html_path = debug_dir / f"{stem}.html"
            try:
                html_path.write_text(await page.content(), encoding="utf-8")
                log.info("debug dump: HTML -> %s (%d bytes)", html_path, html_path.stat().st_size)
            except Exception as exc:
                log.warning("debug dump: HTML dump failed: %s", exc)

            try:
                all_files = sorted(debug_dir.iterdir(), key=lambda p: p.stat().st_mtime)
                total_bytes = sum(f.stat().st_size for f in all_files if f.is_file())
                log.info("debug dir total: %d files, %.1f KB", len(all_files), total_bytes / 1024)
                pngs = sorted(debug_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
                if len(pngs) > _DEBUG_MAX_PAIRS:
                    for old_png in pngs[:-_DEBUG_MAX_PAIRS]:
                        old_html = old_png.with_suffix(".html")
                        old_png.unlink(missing_ok=True)
                        old_html.unlink(missing_ok=True)
            except Exception as exc:
                log.warning("debug dump: prune/size check failed: %s", exc)

        except Exception as exc:
            log.warning("_dump_debug: unexpected error: %s", exc)

    async def fetch_bill_history(self, years_back: int = 2) -> list["Bill"]:
        """Scrape the billing-payment-history table for bills (billed amount).
        Expands the From/To date range to years_back years so we get more
        than just the last few bills. Payment rows are skipped."""
        assert self._ctx is not None
        page = await self._ctx.new_page()
        try:
            await self._ensure_logged_in(page)
            await _goto_with_retry(page, BILLING_HISTORY_URL)
            try:
                await page.wait_for_selector("table tbody tr", timeout=30_000)
            except Exception:
                await self._dump_debug(page, "bill-history-timeout")
                raise

            # Always scrape the default view first as a guaranteed baseline.
            default_rows = await self._scrape_history_rows(page)

            # Best-effort date-range expansion — if it fails or returns fewer
            # rows, fall back to the default view.
            expanded_rows: list[list[str]] = []
            try:
                await self._expand_history_range(page, years_back)
                await self._show_all_history(page)
                expanded_rows = await self._scrape_history_rows(page)
            except Exception as e:
                log.warning("bill-history date-range expansion failed: %s", e)

            rows = expanded_rows if len(expanded_rows) > len(default_rows) else default_rows
            log.info("scraped %d billing-history rows (default=%d, expanded=%d)",
                     len(rows), len(default_rows), len(expanded_rows))
            return [b for b in (_parse_bill_row(r) for r in rows) if b is not None]
        finally:
            await page.close()

    async def _scrape_history_rows(self, page: Page) -> list[list[str]]:
        try:
            await page.wait_for_selector("table tbody tr", timeout=10_000)
        except Exception:
            return []
        return await page.eval_on_selector_all(
            "table tbody tr",
            "els => els.map(tr => [...tr.querySelectorAll('td')].map(td => td.innerText.trim()))",
        )

    async def _show_all_history(self, page: Page) -> None:
        """The billing table paginates 10 rows/page with a 'SHOW ALL' link
        at the bottom. Click it so we scrape every row, not just page 1."""
        try:
            show_all = page.get_by_text("SHOW ALL", exact=True).first
            await show_all.wait_for(state="visible", timeout=5_000)
            await show_all.click()
            await page.wait_for_load_state("networkidle")
            log.info("clicked SHOW ALL — full bill history loaded")
        except Exception as e:
            log.info("SHOW ALL not present or click failed (%s); "
                     "continuing with visible page", e)

    async def _expand_history_range(self, page: Page, years_back: int) -> None:
        """Set From = today - years_back, To = today; click UPDATE."""
        today = date.today()
        from_d = today.replace(year=today.year - years_back)
        # Both From and To use `matinput[aria-haspopup="true"]` (mat-datepicker).
        pickers = page.locator('input[matinput][aria-haspopup="true"]')
        from_input = pickers.nth(0)
        to_input = pickers.nth(1)
        await from_input.wait_for(state="visible", timeout=10_000)
        await from_input.fill(from_d.strftime("%m/%d/%Y"))
        await to_input.fill(today.strftime("%m/%d/%Y"))
        # UPDATE button is a mat-raised-button. Use text since it's unique
        # on this page (the other Update-labelled controls are elsewhere).
        await page.locator("button.mat-raised-button", has_text="UPDATE").first.click()
        await page.wait_for_load_state("networkidle")
        log.info("expanded bill-history range: %s → %s", from_d, today)

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
            await _goto_with_retry(page, ENERGY_USAGE_URL)
            # Angular renders the controls after the initial network-idle. Wait
            # for the meter dropdown to prove the page is ready.
            try:
                await page.locator("mat-select").first.wait_for(state="visible", timeout=30_000)
            except Exception:
                await self._dump_debug(page, "matselect-timeout")
                raise

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
            # Portal's suggested_filename is always today's date regardless of
            # what date range we requested — a plain sequential backfill would
            # therefore overwrite every earlier file. Prefix with the requested
            # ending date + period to keep each download distinct on disk.
            suggested = download.suggested_filename or "greenbutton.xml"
            period_slug = period.replace(" ", "").lower()
            dest = dest_dir / f"{ending_on.isoformat()}_{period_slug}_{suggested}"
            await download.save_as(dest)
            log.info("Downloaded Green Button XML → %s (%d bytes)", dest, dest.stat().st_size)
            return dest
        finally:
            await page.close()

    async def _ensure_logged_in(self, page: Page) -> None:
        await _goto_with_retry(page, DASHBOARD_URL)
        if page.url.startswith(DASHBOARD_URL):
            log.info("Session restored — already logged in")
            return

        # The login form lives inside an <iframe id="loginframe"> running an
        # Azure B2C flow on login.csapps.pacificpower.net.
        # Log where we actually landed so we can tell PP session-TTL expiry
        # (redirect to login.csapps.pacificpower.net) from a bug in the
        # DASHBOARD_URL match or an unexpected challenge page.
        log.info("Not authenticated — running login flow (landed on %s)",
                 _sanitize_url_for_log(page.url))
        await _goto_with_retry(page, LOGIN_URL)
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
        assert self._ctx is not None
        all_cookies = await self._ctx.cookies()
        pp_cookies = [c for c in all_cookies if "pacificpower" in c.get("domain", "")]
        log.info("cookies after login: total=%d pacificpower.net=%d",
                 len(all_cookies), len(pp_cookies))

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
