"""Loading and cleaning bar data.

Rules enforced here:
  * timestamps are stored in UTC;
  * the still-forming bar is dropped;
  * minutes with no trades are NEVER forward-filled (a missing bar stays missing);
  * resampling only combines bars that really exist.
"""
from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .engine import is_regular_session
from .models import UTC, Bar

_ALIASES = {
    "ts": ("time", "timestamp", "date", "datetime", "t", "ts"),
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c"),
    "volume": ("volume", "vol", "v"),
}


def _parse_ts(text: str, naive_tz: ZoneInfo) -> tuple[datetime, bool]:
    """Returns (UTC datetime, was_naive)."""
    text = text.strip()
    try:
        num = float(text)
    except ValueError:
        pass
    else:
        if num > 1e11:  # milliseconds
            num /= 1000
        return datetime.fromtimestamp(num, UTC), False
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=naive_tz).astimezone(UTC), True
    return dt.astimezone(UTC), False


def load_csv(path: str | Path, naive_tz: str = "America/New_York") -> tuple[list[Bar], list[str]]:
    """Read OHLCV bars from a CSV. Returns (bars oldest-first, warnings).

    Accepts headers like time/timestamp/date, open, high, low, close, volume
    (case-insensitive). Timestamps may be ISO-8601, or Unix seconds/millis.
    Timestamps without a timezone are assumed to be in `naive_tz`.
    """
    warnings: list[str] = []
    tz = ZoneInfo(naive_tz)
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: empty file")
        lower = {name.strip().lower(): name for name in reader.fieldnames}
        cols: dict[str, str] = {}
        for key, names in _ALIASES.items():
            match = next((lower[n] for n in names if n in lower), None)
            if match is None:
                raise ValueError(f"{path}: missing a '{key}' column (looked for {names})")
            cols[key] = match
        by_ts: dict[datetime, Bar] = {}
        naive = bad = dupes = 0
        for row in reader:
            try:
                ts, was_naive = _parse_ts(row[cols["ts"]], tz)
                o, h, l, c, v = (float(row[cols[k]]) for k in ("open", "high", "low", "close", "volume"))
            except (ValueError, TypeError):
                bad += 1
                continue
            if not (l <= min(o, c) and h >= max(o, c) and l > 0) or v < 0:
                bad += 1
                continue
            naive += was_naive
            dupes += ts in by_ts
            by_ts[ts] = Bar(ts, o, h, l, c, v)
    if naive:
        warnings.append(f"{naive} timestamps had no timezone; assumed {naive_tz}.")
    if bad:
        warnings.append(f"Dropped {bad} unreadable or inconsistent rows (bad numbers or high < low).")
    if dupes:
        warnings.append(f"{dupes} duplicate timestamps; kept the last row for each.")
    return [by_ts[k] for k in sorted(by_ts)], warnings


def detect_interval_minutes(bars: list[Bar]) -> int | None:
    """Most common spacing between bars, in whole minutes."""
    if len(bars) < 2:
        return None
    diffs = Counter(int((b.ts - a.ts).total_seconds() // 60) for a, b in zip(bars, bars[1:]))
    return diffs.most_common(1)[0][0]


def drop_forming(bars: list[Bar], now: datetime, minutes: int) -> list[Bar]:
    """Remove bars that have not finished yet at time `now`."""
    width = timedelta(minutes=minutes)
    return [b for b in bars if b.ts + width <= now]


def resample(bars: list[Bar], minutes: int) -> list[Bar]:
    """Combine bars into `minutes`-wide bars on clock boundaries (minutes must
    divide 60). Buckets with no trades produce no bar: nothing is filled in."""
    if 60 % minutes:
        raise ValueError("minutes must divide 60")
    out: list[Bar] = []
    cur_start: datetime | None = None
    o = h = l = c = v = 0.0
    for b in bars:
        start = b.ts.replace(minute=b.ts.minute // minutes * minutes, second=0, microsecond=0)
        if start != cur_start:
            if cur_start is not None:
                out.append(Bar(cur_start, o, h, l, c, v))
            cur_start, o, h, l, v = start, b.open, b.high, b.low, 0.0
        h, l, c = max(h, b.high), min(l, b.low), b.close
        v += b.volume
    if cur_start is not None:
        out.append(Bar(cur_start, o, h, l, c, v))
    return out


def quality_notes(bars: list[Bar], minutes: int) -> list[str]:
    """Human-readable warnings about gaps and possible split problems."""
    notes: list[str] = []
    width = timedelta(minutes=minutes)
    missing = 0
    for a, b in zip(bars, bars[1:]):
        same_day = a.ts.date() == b.ts.date()
        if same_day and is_regular_session(a.ts) and is_regular_session(b.ts) and b.ts - a.ts > width:
            missing += int((b.ts - a.ts) / width) - 1
        ratio = b.open / a.close
        if ratio >= 2.0 or ratio <= 0.5:
            notes.append(
                f"Price jumped x{ratio:.2f} between {a.ts:%Y-%m-%d %H:%M} and {b.ts:%Y-%m-%d %H:%M} UTC. "
                "If that is a split or reverse split, wick levels before it are not comparable. "
                "Check whether this data is split-adjusted."
            )
    if missing:
        notes.append(
            f"{missing} regular-session {minutes}m slots have no bar (no trades, a halt, or missing data). "
            "They were left empty, not filled in."
        )
    return notes
