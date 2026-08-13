from datetime import UTC, datetime
from pathlib import Path

from pacificpower_import.espi import parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_pacific_power_month_sample():
    (usage_point,) = parse_file(FIXTURES / "greenbutton_sample.xml")

    assert usage_point.title == "1234 EXAMPLE ST ANYTOWN OR"
    assert usage_point.reading_type.uom == 72  # Wh
    assert usage_point.reading_type.power_of_ten_multiplier == 3
    assert usage_point.reading_type.commodity == 1  # electricity
    assert usage_point.reading_type.flow_direction == 1  # delivered

    readings = usage_point.readings
    assert len(readings) == 30  # one month of daily readings

    first = readings[0]
    assert first.duration == 86400
    # Value scaling: raw × 10^powerOfTen = Wh. Fixture values are scrubbed,
    # so just assert the shape (10^3 Wh = 1 kWh per unit → daily values land
    # in the tens-of-kWh range).
    assert 10_000 <= first.wh <= 100_000
    assert first.start == datetime.fromtimestamp(1783839600, tz=UTC)

    # Monotonically increasing timestamps.
    for prev, curr in zip(readings, readings[1:]):
        assert curr.start > prev.start

    # Total consumption across the sample: sum of daily values × 1000 Wh.
    total_kwh = sum(r.wh for r in readings) / 1000
    assert 1000 < total_kwh < 2000  # ~1400 kWh across 30 days


def test_parses_hourly_sample():
    (up,) = parse_file(FIXTURES / "greenbutton_hourly_sample.xml")

    assert up.reading_type.uom == 72
    assert up.reading_type.power_of_ten_multiplier == 3
    readings = up.readings
    assert len(readings) == 24
    assert all(r.duration == 3600 for r in readings)
    # Realistic residential day: total between 5 and 200 kWh.
    total_kwh = sum(r.wh for r in readings) / 1000
    assert 5 < total_kwh < 200
