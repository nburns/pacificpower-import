"""CLI entrypoint: orchestrate scraper → parser → HA statistics import."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from .espi import IntervalReading, UsagePoint, parse_file
from .ha_client import HAClient, StatisticEntry
from .scraper import PacificPowerScraper, Period, ScraperOptions
from .state import State

log = logging.getLogger("pacificpower_import")


def _yesterday() -> date:
    # Portal's max ending date is yesterday — today's intervals aren't published yet.
    return date.today() - timedelta(days=1)


def _prune_downloads(dest_dir: Path, keep: int = 30) -> None:
    if not dest_dir.exists():
        return
    xmls = sorted(dest_dir.glob("*.xml"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in xmls[keep:]:
        stale.unlink(missing_ok=True)

# One request per period gives us the maximum available range in a single file.
# For the backfill, "Two Year" is the widest option Pacific Power exposes.
BACKFILL_PERIOD: Period = "Two Year"

# Daily incremental: pull "One Day" ending yesterday and rely on late-arriving
# intervals landing on subsequent runs (the import is idempotent).
DAILY_PERIOD: Period = "One Day"


async def run(mode: str, *, data_dir: Path, opts: ScraperOptions, ha: HAClient,
              statistic_id: str, statistic_name: str) -> None:
    state = State.load(data_dir / "state.json")
    dest_dir = data_dir / "downloads"

    async with PacificPowerScraper(opts) as scraper:
        if mode == "backfill":
            xml_paths = [
                await scraper.download_greenbutton(
                    period=BACKFILL_PERIOD,
                    ending_on=_yesterday(),
                    dest_dir=dest_dir,
                )
            ]
        elif mode == "daily":
            # Pull last 3 days so late-arriving intervals get re-imported.
            # "Last 3 days" ends at yesterday (today isn't published yet).
            xml_paths = []
            for offset in (2, 1, 0):
                xml_paths.append(
                    await scraper.download_greenbutton(
                        period=DAILY_PERIOD,
                        ending_on=_yesterday() - timedelta(days=offset),
                        dest_dir=dest_dir,
                    )
                )
        else:
            raise ValueError(f"Unknown mode: {mode}")

    readings: list[IntervalReading] = []
    usage_point: UsagePoint | None = None
    for path in xml_paths:
        (up,) = parse_file(path)
        usage_point = up
        readings.extend(up.readings)

    if not readings or usage_point is None:
        log.warning("No readings parsed — nothing to import")
        return

    readings.sort(key=lambda r: r.start)
    log.info("Parsed %d intervals (%s → %s)",
             len(readings), readings[0].start, readings[-1].start)

    # Build cumulative-sum series. On the first run cumulative_wh is 0 and we
    # start summing from the first interval. On later runs we skip intervals
    # we've already accounted for (start <= latest_interval_start) — but still
    # re-emit them, using the cumulative_wh we already recorded, so HA gets
    # the same sum values (safe idempotent overwrite).
    baseline_wh = state.cumulative_wh
    baseline_start = state.latest_interval_start
    entries: list[StatisticEntry] = []
    running_wh = 0.0
    max_start = baseline_start
    for r in readings:
        running_wh += r.wh
        if baseline_start is not None and r.start <= baseline_start:
            # Re-emit historical rows verbatim to preserve idempotency.
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
    log.info("Imported %d statistics into HA (%s)", len(entries), statistic_id)

    # Persist state.
    now = datetime.now().astimezone()
    if mode == "backfill":
        state.last_backfill = now
    state.last_incremental = now
    if max_start is not None:
        state.latest_interval_start = max_start
        # Persist total by re-computing from what we just imported.
        state.cumulative_wh = baseline_wh + sum(
            r.wh for r in readings
            if baseline_start is None or r.start > baseline_start
        )
    state.save(data_dir / "state.json")
    _prune_downloads(dest_dir)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["backfill", "daily"], required=True)
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "/data"))
    parser.add_argument("--statistic-id",
                        default=os.environ.get("STATISTIC_ID", "pacificpower:electric_consumption"))
    parser.add_argument("--statistic-name",
                        default=os.environ.get("STATISTIC_NAME", "Pacific Power electric consumption"))
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
    ))


if __name__ == "__main__":
    main()
