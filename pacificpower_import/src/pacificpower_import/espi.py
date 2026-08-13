"""Parse ESPI (NAESB Green Button) XML into interval readings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

ATOM_NS = "http://www.w3.org/2005/Atom"
ESPI_NS = "http://naesb.org/espi"

# NAESB unit-of-measurement codes we recognize.
UOM_WH = 72
UOM_W = 63


@dataclass(frozen=True)
class ReadingType:
    power_of_ten_multiplier: int
    uom: int
    interval_length: int  # seconds, per the ReadingType metadata (not necessarily per-reading)
    flow_direction: int  # 1 = delivered from utility to customer, 19 = received (export)
    commodity: int  # 1 = electricity


@dataclass(frozen=True)
class IntervalReading:
    start: datetime  # tz-aware UTC
    duration: int  # seconds
    wh: float  # normalized to watt-hours; kWh = wh / 1000


@dataclass(frozen=True)
class UsagePoint:
    title: str
    reading_type: ReadingType
    readings: list[IntervalReading]


def _q(tag: str, ns: str = ESPI_NS) -> str:
    return f"{{{ns}}}{tag}"


def parse(xml: str | bytes) -> list[UsagePoint]:
    """Parse a Green Button Atom feed. Returns one UsagePoint per meter reading."""
    root = ET.fromstring(xml)

    usage_point_titles: dict[str, str] = {}
    reading_type: ReadingType | None = None
    interval_blocks: list[ET.Element] = []

    for entry in root.findall(_q("entry", ATOM_NS)):
        title_el = entry.find(_q("title", ATOM_NS))
        content = entry.find(_q("content", ATOM_NS))
        if content is None:
            continue

        up = content.find(_q("UsagePoint"))
        if up is not None:
            self_link = _find_self_link(entry)
            usage_point_titles[self_link] = title_el.text if title_el is not None else ""
            continue

        rt = content.find(_q("ReadingType"))
        if rt is not None:
            reading_type = _parse_reading_type(rt)
            continue

        ib = content.find(_q("IntervalBlock"))
        if ib is not None:
            interval_blocks.append(ib)

    if reading_type is None:
        raise ValueError("No ReadingType found in feed")

    readings: list[IntervalReading] = []
    for block in interval_blocks:
        for ir in block.findall(_q("IntervalReading")):
            readings.append(_parse_interval_reading(ir, reading_type))

    readings.sort(key=lambda r: r.start)

    title = next(iter(usage_point_titles.values()), "Unknown")
    return [UsagePoint(title=title, reading_type=reading_type, readings=readings)]


def _find_self_link(entry: ET.Element) -> str:
    for link in entry.findall(_q("link", ATOM_NS)):
        if link.get("rel") == "self":
            return link.get("href", "")
    return ""


def _parse_reading_type(rt: ET.Element) -> ReadingType:
    def _int(tag: str, default: int = 0) -> int:
        el = rt.find(_q(tag))
        return int(el.text) if el is not None and el.text else default

    return ReadingType(
        power_of_ten_multiplier=_int("powerOfTenMultiplier"),
        uom=_int("uom"),
        interval_length=_int("intervalLength"),
        flow_direction=_int("flowDirection", 1),
        commodity=_int("commodity", 1),
    )


def _parse_interval_reading(ir: ET.Element, rt: ReadingType) -> IntervalReading:
    time_period = ir.find(_q("timePeriod"))
    if time_period is None:
        raise ValueError("IntervalReading missing timePeriod")

    start_el = time_period.find(_q("start"))
    duration_el = time_period.find(_q("duration"))
    value_el = ir.find(_q("value"))

    if start_el is None or duration_el is None or value_el is None:
        raise ValueError("IntervalReading missing start/duration/value")

    start_ts = int(start_el.text or 0)
    duration = int(duration_el.text or 0)
    raw_value = int(value_el.text or 0)

    scaled = raw_value * (10**rt.power_of_ten_multiplier)
    if rt.uom == UOM_WH:
        wh = float(scaled)
    elif rt.uom == UOM_W:
        # Power sample over the interval — convert to energy.
        wh = float(scaled) * duration / 3600.0
    else:
        raise ValueError(f"Unsupported uom: {rt.uom}")

    return IntervalReading(
        start=datetime.fromtimestamp(start_ts, tz=UTC),
        duration=duration,
        wh=wh,
    )


def parse_file(path: str | Path) -> list[UsagePoint]:
    return parse(Path(path).read_bytes())
