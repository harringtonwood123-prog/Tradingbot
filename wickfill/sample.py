"""Generates a small SYNTHETIC two-day 5m dataset for trying out `replay`.

This is made-up data shaped to show a full wick-fill story (quiet day, then a
pre-market spike that leaves a wick, a dip, a reclaim, a retest and the fill).
It is not real market data and says nothing about how the strategy performs.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

from .engine import ET

# (price path) each entry: (HH:MM, open, high, low, close, volume)
_DAY2_STORY: list[tuple[str, float, float, float, float, int]] = [
    ("08:00", 2.60, 3.60, 2.56, 2.80, 900_000),  # the wick candle
    ("08:05", 2.80, 2.84, 2.74, 2.78, 150_000),
    ("08:10", 2.78, 2.80, 2.72, 2.74, 120_000),
    ("08:15", 2.74, 2.76, 2.68, 2.70, 90_000),
    ("08:20", 2.70, 2.72, 2.66, 2.68, 80_000),
    ("08:25", 2.68, 2.70, 2.64, 2.66, 70_000),
    ("08:30", 2.66, 2.69, 2.63, 2.67, 60_000),
    ("08:35", 2.67, 2.70, 2.65, 2.68, 60_000),
    ("08:40", 2.68, 2.71, 2.66, 2.69, 60_000),
    ("08:45", 2.69, 2.72, 2.67, 2.70, 60_000),
    ("08:50", 2.70, 2.73, 2.68, 2.71, 60_000),
    ("08:55", 2.71, 2.74, 2.69, 2.72, 60_000),
    ("09:00", 2.72, 2.75, 2.70, 2.73, 60_000),
    ("09:05", 2.73, 2.76, 2.71, 2.74, 60_000),
    ("09:10", 2.74, 2.77, 2.72, 2.75, 60_000),
    ("09:15", 2.75, 2.78, 2.73, 2.76, 60_000),
    ("09:20", 2.76, 2.79, 2.74, 2.77, 60_000),
    ("09:25", 2.77, 2.80, 2.75, 2.78, 60_000),
    ("09:30", 2.78, 2.86, 2.77, 2.84, 110_000),  # reclaim close 1
    ("09:35", 2.84, 2.90, 2.83, 2.88, 110_000),  # reclaim close 2 -> FRESH_RECLAIM
    ("09:40", 2.88, 2.96, 2.87, 2.94, 100_000),  # EXTENDED
    ("09:45", 2.94, 3.00, 2.92, 2.98, 100_000),
    ("09:50", 2.98, 3.02, 2.93, 2.95, 90_000),
    ("09:55", 2.95, 2.96, 2.81, 2.87, 100_000),  # dips to the level, closes above -> AT_RETEST
    ("10:00", 2.87, 2.99, 2.86, 2.97, 100_000),
    ("10:05", 2.97, 3.12, 2.96, 3.10, 110_000),
    ("10:10", 3.10, 3.26, 3.09, 3.24, 110_000),
    ("10:15", 3.24, 3.40, 3.22, 3.38, 110_000),
    ("10:20", 3.38, 3.52, 3.36, 3.50, 110_000),
    ("10:25", 3.50, 3.66, 3.48, 3.62, 120_000),  # high >= 3.60 -> WICK_FILLED
    ("10:30", 3.62, 3.64, 3.52, 3.55, 90_000),
]


def _flat(start: datetime, end: datetime, base: float, volume: int) -> list[tuple]:
    rows, t, i = [], start, 0
    while t < end:
        wobble = 0.01 * ((i % 4) - 1.5) / 1.5  # small deterministic zig-zag
        o = round(base + wobble, 4)
        c = round(base - wobble, 4)
        rows.append((t, o, round(max(o, c) + 0.01, 4), round(min(o, c) - 0.01, 4), c, volume))
        t += timedelta(minutes=5)
        i += 1
    return rows


def sample_rows() -> list[tuple]:
    day1 = datetime(2026, 11, 2, tzinfo=ET)  # a Monday
    day2 = day1 + timedelta(days=1)
    rows = _flat(day1.replace(hour=9, minute=30), day1.replace(hour=16), 2.00, 40_000)
    rows += _flat(day2.replace(hour=4), day2.replace(hour=8), 2.60, 60_000)  # gapped up pre-market
    for hhmm, o, h, l, c, v in _DAY2_STORY:
        hh, mm = hhmm.split(":")
        rows.append((day2.replace(hour=int(hh), minute=int(mm)), o, h, l, c, v))
    return rows


def write_sample(path: str | Path) -> int:
    rows = sample_rows()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for t, o, h, l, c, v in rows:
            w.writerow([t.isoformat(), o, h, l, c, v])
    return len(rows)
