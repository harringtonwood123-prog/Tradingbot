from __future__ import annotations

from datetime import datetime, timedelta

from wickfill.engine import ET
from wickfill.models import Bar, Candle


def et(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def bar(ts: datetime, o: float, h: float, l: float, c: float, v: float = 100_000) -> Bar:
    return Bar(ts, o, h, l, c, v)


def flat_bars(start: datetime, n: int, price: float = 2.0, v: float = 50_000) -> list[Bar]:
    return [bar(start + timedelta(minutes=5 * i), price, price + 0.01, price - 0.01, price, v) for i in range(n)]


def candle(o: float, h: float, l: float, c: float, v: float, tf: str = "5m") -> Candle:
    start = et(2026, 11, 3, 10)
    return Candle(tf, start, start + timedelta(minutes=5), o, h, l, c, v)
