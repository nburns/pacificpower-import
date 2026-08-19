"""CLI entrypoint: orchestrate scraper → parser → HA statistics import."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

from datetime import UTC

from .espi import IntervalReading, UsagePoint, parse_file
from .ha_client import HAClient, StatisticEntry
from .scraper import Bill, PacificPowerScraper, Period, ScraperOptions
from .state import State

log = logging.getLogger("pacificpower_import")


# Bumped when the backfill strategy changes in a way that requires
# clearing prior statistics and re-importing. v0.1.1 used a single
# "Two Year" download which returned monthly-granularity readings;
# v0.1.2 iterates "One Month" downloads for daily granularity.
BACKFILL_VERSION = 5

# Both backfill and daily use "One Month" (returns 30 days of daily
# intervals). Backfill iterates back 25 times to cover ~2 years.
# Daily runs a single "One Month" ending yesterday to top up + repair.
MONTH_PERIOD: Period = "One Month"
DAY_PERIOD: Period = "One Day"
BACKFILL_MONTHS = 25

# Small pause between downloads to be polite to the portal.
DOWNLOAD_PAUSE_S = 2.0


def _yesterday() -> date:
    # Portal's max ending date is yesterday — today's intervals aren't published yet.
    return date.today() - timedelta(days=1)


def _swap_to_steady_state_and_reload_cron() -> None:
    # Called from the trickle job right after switchover flips the mode.
    # Rewrites the crontab in place and signals supercronic to exit; run.sh
    # wraps supercronic in a loop and re-execs it against the new crontab.
    # No-op outside the add-on container (missing env vars / no supercronic).
    schedule = os.environ.get("PP_DAILY_SCHEDULE")
    crontab_path_str = os.environ.get("PP_CRONTAB_PATH", "/data/crontab")
    if not schedule:
        log.info("PP_DAILY_SCHEDULE not set; skipping crontab hot-swap")
        return
    crontab_path = Path(crontab_path_str)
    if not crontab_path.parent.exists():
        log.info("crontab dir %s missing; skipping crontab hot-swap", crontab_path.parent)
        return
    new_content = f"{schedule} python -m pacificpower_import --mode hourly-daily\n"
    crontab_path.write_text(new_content)
    log.info("rewrote %s to steady-state schedule: %s", crontab_path, schedule)
    try:
        subprocess.run(["pkill", "-TERM", "supercronic"], check=False)
        log.info("sent SIGTERM to supercronic; run.sh loop will restart it with the new crontab")
    except FileNotFoundError:
        log.info("pkill not available; skipping supercronic reload signal")


def _prune_downloads(dest_dir: Path, keep: int = 60) -> None:
    if not dest_dir.exists():
        return
    xmls = sorted(dest_dir.glob("*.xml"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in xmls[keep:]:
        stale.unlink(missing_ok=True)


async def _download_range(
    scraper: PacificPowerScraper,
    *,
    ending_dates: list[date],
    dest_dir: Path,
) -> list[Path]:
    paths: list[Path] = []
    for i, d in enumerate(ending_dates):
        log.info("download %d/%d — period=%s ending=%s", i + 1, len(ending_dates), MONTH_PERIOD, d)
        try:
            path = await scraper.download_greenbutton(
                period=MONTH_PERIOD, ending_on=d, dest_dir=dest_dir,
            )
            paths.append(path)
        except Exception as e:
            # Don't abort the whole backfill on one failure; log and move on.
            log.error("download for ending=%s failed: %s", d, e)
        if i + 1 < len(ending_dates):
            await asyncio.sleep(DOWNLOAD_PAUSE_S)
    return paths


def _bills_to_daily_cost_entries(bills: list[Bill]) -> list[StatisticEntry]:
    """Spread each bill's amount evenly across the days since the prior bill
    so the Energy dashboard shows smooth daily cost bars instead of monthly
    spikes. Older bills are dropped if we can't infer their period (i.e.
    the very oldest bill in the history)."""
    if not bills:
        return []
    bills = sorted(bills, key=lambda b: b.bill_date)
    entries: list[StatisticEntry] = []
    running = 0.0
    for prev, curr in zip(bills, bills[1:]):
        span_days = (curr.bill_date - prev.bill_date).days
        if span_days <= 0:
            continue
        per_day = curr.amount_usd / span_days
        d = prev.bill_date
        for _ in range(span_days):
            d += timedelta(days=1)
            running += per_day
            entries.append(StatisticEntry(
                start=datetime(d.year, d.month, d.day, tzinfo=UTC),
                state=per_day, sum=running,
            ))
    return entries


def _hourly_file_for_date(hourly_dir: Path, d: date) -> Path:
    return hourly_dir / f"{d.isoformat()}_oneday.xml"


async def run_hourly_trickle(
    *,
    data_dir: Path,
    opts: ScraperOptions,
    ha: HAClient,
    statistic_id: str,
    statistic_name: str,
    cost_statistic_id: str,
    cost_statistic_name: str,
    backfill_window_days: int,
) -> None:
    state = State.load(data_dir / "state.json")
    hourly_dir = data_dir / "hourly_downloads"
    hourly_dir.mkdir(parents=True, exist_ok=True)

    if state.hourly_backfill_complete:
        log.info("hourly trickle: backfill already complete, nothing to do")
        return

    cursor = state.hourly_backfill_cursor
    if cursor is None:
        cursor = _yesterday()

    window_start = _yesterday() - timedelta(days=backfill_window_days - 1)
    dest = _hourly_file_for_date(hourly_dir, cursor)

    if not dest.exists():
        log.info("hourly trickle: downloading One Day XML for %s", cursor)
        async with PacificPowerScraper(opts) as scraper:
            await scraper.download_greenbutton(
                period=DAY_PERIOD, ending_on=cursor, dest_dir=hourly_dir,
            )
        # Rename from the scraper's naming convention to our canonical name.
        period_slug = DAY_PERIOD.replace(" ", "").lower()
        for f in hourly_dir.glob(f"{cursor.isoformat()}_{period_slug}_*.xml"):
            f.rename(dest)
            break
    else:
        log.info("hourly trickle: %s already on disk, skipping download", dest.name)

    next_cursor = cursor - timedelta(days=1)
    if next_cursor < window_start:
        log.info("hourly trickle: reached window start (%s), marking backfill complete", window_start)
        state.hourly_backfill_complete = True
        state.hourly_backfill_cursor = next_cursor
        state.last_mode = "hourly"
        state.save(data_dir / "state.json")
        log.info("running hourly switchover now")
        await run_hourly_switchover(
            data_dir=data_dir,
            ha=ha,
            statistic_id=statistic_id,
            statistic_name=statistic_name,
            cost_statistic_id=cost_statistic_id,
            cost_statistic_name=cost_statistic_name,
        )
        _swap_to_steady_state_and_reload_cron()
        return

    state.hourly_backfill_cursor = next_cursor
    state.last_mode = "hourly"
    state.save(data_dir / "state.json")
    log.info("hourly trickle: cursor advanced to %s", next_cursor)


async def run_hourly_switchover(
    *,
    data_dir: Path,
    ha: HAClient,
    statistic_id: str,
    statistic_name: str,
    cost_statistic_id: str,
    cost_statistic_name: str,
) -> None:
    state = State.load(data_dir / "state.json")
    hourly_dir = data_dir / "hourly_downloads"

    xml_files = sorted(hourly_dir.glob("*.xml"))
    if not xml_files:
        raise RuntimeError(f"hourly switchover: no XML files found in {hourly_dir}")

    readings_by_start: dict[datetime, IntervalReading] = {}
    for path in xml_files:
        try:
            (up,) = parse_file(path)
        except Exception as e:
            log.warning("failed to parse %s: %s", path, e)
            continue
        for r in up.readings:
            readings_by_start[r.start] = r

    if not readings_by_start:
        raise RuntimeError("hourly switchover: no readings parsed from any XML file")

    readings = sorted(readings_by_start.values(), key=lambda r: r.start)
    log.info("hourly switchover: %d unique hourly intervals (%s → %s)",
             len(readings), readings[0].start, readings[-1].start)

    async with ha:
        log.info("hourly switchover: clearing statistics for %s + %s",
                 statistic_id, cost_statistic_id)
        await ha.clear_statistics([statistic_id, cost_statistic_id])

        entries: list[StatisticEntry] = []
        running_wh = 0.0
        for r in readings:
            running_wh += r.wh
            entries.append(StatisticEntry(
                start=r.start, state=r.wh / 1000, sum=running_wh / 1000,
            ))

        # Import in batches matching how the existing backfill works (one call
        # per ~month's worth; here we use 720 entries = ~30 days of hourly data).
        batch_size = 720
        for i in range(0, len(entries), batch_size):
            batch = entries[i:i + batch_size]
            await ha.import_statistics(
                statistic_id=statistic_id,
                name=statistic_name,
                unit="kWh",
                source=statistic_id.split(":", 1)[0],
                stats=batch,
            )
        log.info("hourly switchover: imported %d entries into HA (%s)",
                 len(entries), statistic_id)

        await ha.touch_heartbeat("input_datetime.pacificpower_last_import")

    now = datetime.now().astimezone()
    state.last_backfill = now
    state.backfill_version = BACKFILL_VERSION
    state.cumulative_wh = running_wh
    state.latest_interval_start = readings[-1].start
    state.hourly_backfill_complete = True
    state.last_mode = "hourly"
    state.save(data_dir / "state.json")
    log.info("hourly switchover complete")


async def run_hourly_daily(
    *,
    data_dir: Path,
    opts: ScraperOptions,
    ha: HAClient,
    statistic_id: str,
    statistic_name: str,
    cost_statistic_id: str,
    cost_statistic_name: str,
) -> None:
    state = State.load(data_dir / "state.json")
    dest_dir = data_dir / "downloads"
    yesterday = _yesterday()

    async with PacificPowerScraper(opts) as scraper:
        log.info("hourly-daily: downloading One Day XML for %s", yesterday)
        xml_path = await scraper.download_greenbutton(
            period=DAY_PERIOD, ending_on=yesterday, dest_dir=dest_dir,
        )
        try:
            bills = await scraper.fetch_bill_history()
        except Exception as e:
            log.warning("bill history fetch failed: %s", e)
            bills = []

    try:
        (up,) = parse_file(xml_path)
    except Exception as e:
        raise RuntimeError(f"failed to parse {xml_path}: {e}") from e

    readings = sorted(up.readings, key=lambda r: r.start)
    if not readings:
        raise RuntimeError(f"hourly-daily: no readings in downloaded XML {xml_path}")

    baseline_wh = state.cumulative_wh
    baseline_start = state.latest_interval_start

    entries: list[StatisticEntry] = []
    running_wh = 0.0
    max_start = baseline_start
    for r in readings:
        running_wh += r.wh
        if baseline_start is not None and r.start <= baseline_start:
            entries.append(StatisticEntry(
                start=r.start, state=r.wh / 1000, sum=running_wh / 1000,
            ))
            continue
        cumulative_wh = baseline_wh + running_wh
        entries.append(StatisticEntry(
            start=r.start, state=r.wh / 1000, sum=cumulative_wh / 1000,
        ))
        if max_start is None or r.start > max_start:
            max_start = r.start

    async with ha:
        await ha.import_statistics(
            statistic_id=statistic_id,
            name=statistic_name,
            unit="kWh",
            source=statistic_id.split(":", 1)[0],
            stats=entries,
        )
        log.info("hourly-daily: imported %d hourly entries (%s)", len(entries), statistic_id)

        cost_entries = _bills_to_daily_cost_entries(bills)
        if cost_entries:
            await ha.import_statistics(
                statistic_id=cost_statistic_id,
                name=cost_statistic_name,
                unit="USD",
                source=cost_statistic_id.split(":", 1)[0],
                stats=cost_entries,
            )
            log.info("hourly-daily: imported %d cost entries from %d bills",
                     len(cost_entries), len(bills))
        else:
            log.info("No cost entries built (need >=2 bills to compute per-day cost)")

        await ha.touch_heartbeat("input_datetime.pacificpower_last_import")

    now = datetime.now().astimezone()
    state.last_incremental = now
    if max_start is not None and (baseline_start is None or max_start > baseline_start):
        state.latest_interval_start = max_start
        state.cumulative_wh = baseline_wh + sum(
            r.wh for r in readings
            if baseline_start is None or r.start > baseline_start
        )
    state.last_mode = "hourly"
    state.save(data_dir / "state.json")
    _prune_downloads(dest_dir)


async def run(mode: str, *, data_dir: Path, opts: ScraperOptions, ha: HAClient,
              statistic_id: str, statistic_name: str,
              cost_statistic_id: str, cost_statistic_name: str,
              backfill_window_days: int = 730) -> None:
    state = State.load(data_dir / "state.json")
    dest_dir = data_dir / "downloads"

    if mode in ("hourly-trickle", "hourly-switchover", "hourly-daily"):
        if mode == "hourly-trickle":
            await run_hourly_trickle(
                data_dir=data_dir, opts=opts, ha=ha,
                statistic_id=statistic_id, statistic_name=statistic_name,
                cost_statistic_id=cost_statistic_id, cost_statistic_name=cost_statistic_name,
                backfill_window_days=backfill_window_days,
            )
        elif mode == "hourly-switchover":
            await run_hourly_switchover(
                data_dir=data_dir, ha=ha,
                statistic_id=statistic_id, statistic_name=statistic_name,
                cost_statistic_id=cost_statistic_id, cost_statistic_name=cost_statistic_name,
            )
        else:
            await run_hourly_daily(
                data_dir=data_dir, opts=opts, ha=ha,
                statistic_id=statistic_id, statistic_name=statistic_name,
                cost_statistic_id=cost_statistic_id, cost_statistic_name=cost_statistic_name,
            )
        return

    # Decide the ending-date list.
    if mode == "backfill":
        # Walk back one 30-day step at a time. Overlap between consecutive
        # months is fine — import is idempotent by (statistic_id, start).
        ending_dates = [_yesterday() - timedelta(days=30 * i) for i in range(BACKFILL_MONTHS)]
        ending_dates.reverse()  # oldest first, so sums grow monotonically
    elif mode == "daily":
        ending_dates = [_yesterday()]
    else:
        raise ValueError(f"Unknown mode: {mode}")

    async with PacificPowerScraper(opts) as scraper:
        xml_paths = await _download_range(scraper, ending_dates=ending_dates, dest_dir=dest_dir)
        try:
            bills = await scraper.fetch_bill_history()
        except Exception as e:
            log.warning("bill history fetch failed: %s", e)
            bills = []

    if not xml_paths:
        raise RuntimeError("No downloads succeeded — nothing to import")

    # Parse all + deduplicate readings by start timestamp (last wins).
    readings_by_start: dict[datetime, IntervalReading] = {}
    usage_point: UsagePoint | None = None
    for path in xml_paths:
        try:
            (up,) = parse_file(path)
        except Exception as e:
            log.warning("failed to parse %s: %s", path, e)
            continue
        usage_point = up
        for r in up.readings:
            readings_by_start[r.start] = r

    if not readings_by_start or usage_point is None:
        raise RuntimeError("No readings parsed from any downloaded XML — nothing to import")

    readings = sorted(readings_by_start.values(), key=lambda r: r.start)
    log.info("Parsed %d unique intervals (%s → %s)",
             len(readings), readings[0].start, readings[-1].start)

    # If this is a fresh backfill, wipe prior statistics so we don't leave
    # stale mixed-granularity leftovers behind.
    async with ha:
        if mode == "backfill":
            log.info("clearing existing statistics for %s + %s before backfill",
                     statistic_id, cost_statistic_id)
            await ha.clear_statistics([statistic_id, cost_statistic_id])
            baseline_wh = 0.0
            baseline_start: datetime | None = None
        else:
            baseline_wh = state.cumulative_wh
            baseline_start = state.latest_interval_start

        # Build cumulative-sum series.
        entries: list[StatisticEntry] = []
        running_wh = 0.0
        max_start = baseline_start
        for r in readings:
            running_wh += r.wh
            if baseline_start is not None and r.start <= baseline_start:
                # Re-emit prior rows verbatim to preserve idempotency.
                entries.append(StatisticEntry(
                    start=r.start, state=r.wh / 1000, sum=running_wh / 1000,
                ))
                continue
            cumulative_wh = baseline_wh + running_wh
            entries.append(StatisticEntry(
                start=r.start, state=r.wh / 1000, sum=cumulative_wh / 1000,
            ))
            if max_start is None or r.start > max_start:
                max_start = r.start

        await ha.import_statistics(
            statistic_id=statistic_id,
            name=statistic_name,
            unit="kWh",
            source=statistic_id.split(":", 1)[0],
            stats=entries,
        )
        log.info("Imported %d consumption statistics into HA (%s)",
                 len(entries), statistic_id)

        cost_entries = _bills_to_daily_cost_entries(bills)
        if cost_entries:
            await ha.import_statistics(
                statistic_id=cost_statistic_id,
                name=cost_statistic_name,
                unit="USD",
                source=cost_statistic_id.split(":", 1)[0],
                stats=cost_entries,
            )
            log.info("Imported %d cost statistics into HA (%s) from %d bills",
                     len(cost_entries), cost_statistic_id, len(bills))
        else:
            log.info("No cost entries built (need ≥2 bills to compute per-day cost)")

        # Heartbeat for stale-import alerting (no-op if helper missing).
        await ha.touch_heartbeat("input_datetime.pacificpower_last_import")

    # Persist state.
    now = datetime.now().astimezone()
    if mode == "backfill":
        state.last_backfill = now
        state.backfill_version = BACKFILL_VERSION
        # After backfill, cumulative sum = sum of everything we just imported.
        state.cumulative_wh = sum(r.wh for r in readings)
        state.latest_interval_start = max_start
        state.last_mode = "daily"
    else:
        state.last_incremental = now
        if max_start is not None and (baseline_start is None or max_start > baseline_start):
            state.latest_interval_start = max_start
            state.cumulative_wh = baseline_wh + sum(
                r.wh for r in readings
                if baseline_start is None or r.start > baseline_start
            )
        state.last_mode = "daily"
    state.save(data_dir / "state.json")
    _prune_downloads(dest_dir)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode",
                        choices=["backfill", "daily",
                                 "hourly-trickle", "hourly-switchover", "hourly-daily"],
                        required=True)
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "/data"))
    parser.add_argument("--statistic-id",
                        default=os.environ.get("STATISTIC_ID", "pacificpower:electric_consumption"))
    parser.add_argument("--statistic-name",
                        default=os.environ.get("STATISTIC_NAME", "Pacific Power electric consumption"))
    parser.add_argument("--cost-statistic-id",
                        default=os.environ.get("COST_STATISTIC_ID", "pacificpower:electric_cost"))
    parser.add_argument("--cost-statistic-name",
                        default=os.environ.get("COST_STATISTIC_NAME", "Pacific Power electric cost"))
    parser.add_argument("--backfill-window-days",
                        type=int,
                        default=int(os.environ.get("HOURLY_BACKFILL_WINDOW_DAYS", "730")))
    # Scraper opts
    parser.add_argument("--username", default=os.environ.get("PP_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("PP_PASSWORD"))
    parser.add_argument("--meter-id", default=os.environ.get("PP_METER_ID"))
    parser.add_argument("--headed", action="store_true")
    # HA opts
    parser.add_argument("--ha-url", default=os.environ.get("HA_URL"),
                        help="e.g. ws://homeassistant.local:8123/api/websocket "
                             "(add-on defaults to ws://supervisor/core/websocket)")
    parser.add_argument("--ha-token", default=os.environ.get("HA_TOKEN"))
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.username or not args.password:
        raise SystemExit("Set PP_USERNAME / PP_PASSWORD (env or --flags)")

    data_dir = Path(args.data_dir)
    opts = ScraperOptions(
        username=args.username,
        password=args.password,
        storage_dir=data_dir / "browser",
        meter_id=args.meter_id,
        headless=not args.headed,
    )

    if args.ha_url and args.ha_token:
        ha = HAClient(args.ha_url, args.ha_token)
    elif os.environ.get("SUPERVISOR_TOKEN"):
        ha = HAClient.for_supervisor()
    else:
        raise SystemExit("Provide --ha-url + --ha-token, or run inside an HA add-on with SUPERVISOR_TOKEN")

    asyncio.run(run(
        args.mode, data_dir=data_dir, opts=opts, ha=ha,
        statistic_id=args.statistic_id, statistic_name=args.statistic_name,
        cost_statistic_id=args.cost_statistic_id, cost_statistic_name=args.cost_statistic_name,
        backfill_window_days=args.backfill_window_days,
    ))


if __name__ == "__main__":
    main()
