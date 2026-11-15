"""Replay a CSV of bars through the engine, bar by bar, as if it were live."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Config
from .engine import BASE_MINUTES, ET, Event, Snapshot, SymbolEngine
from .models import Bar


@dataclass
class ReplayResult:
    events: list[Event]
    final: Snapshot | None
    engine: SymbolEngine


def run_replay(
    bars: list[Bar],
    symbol: str,
    cfg: Config,
    prev_close: float | None = None,
    catalyst_checked: bool = False,
) -> ReplayResult:
    """`bars` must be completed 5-minute bars, oldest first."""
    engine = SymbolEngine(symbol, cfg, prev_close=prev_close)
    engine.catalyst_checked = catalyst_checked
    events: list[Event] = []
    final: Snapshot | None = None
    for bar in bars:
        result = engine.on_bar(bar)
        events.extend(result.events)
        final = result.snapshot
    return ReplayResult(events, final, engine)


def fmt_time(ts: datetime, local_tz: str) -> str:
    et = ts.astimezone(ET)
    local = ts.astimezone(ZoneInfo(local_tz))
    return f"{et:%Y-%m-%d %H:%M} ET ({local:%H:%M} {local:%Z})"


def format_event(e: Event, local_tz: str) -> str:
    return f"{fmt_time(e.bar_ts, local_tz)}  {e.kind:<13} {e.detail}"


def format_status_box(snap: Snapshot, local_tz: str) -> str:
    """The terminal version of the TradingView status box."""
    rows: list[tuple[str, str]] = [
        ("Symbol", snap.symbol),
        ("As of", fmt_time(snap.bar_ts, local_tz) + f" ({BASE_MINUTES}m bar start)"),
        ("Price", f"{snap.price:.4f}"),
        ("Action", snap.display),
        ("State", snap.state.value),
    ]
    z = snap.zone
    if z is None:
        rows.append(("Zone", "no active wick zone above price"))
    else:
        rows += [
            ("Zone", f"{z.tf} candle of {fmt_time(z.candle_start, local_tz)}"),
            ("Level", f"{z.level:.4f}"),
            ("Target", f"{z.target:.4f}"),
        ]
        if snap.plan is None:
            rows.append(("Plan", "not enough bars yet for ATR"))
        else:
            p = snap.plan
            cap = f" (capped by {p.capped_by})" if p.capped_by else ""
            rows += [
                ("Stop", f"{p.stop:.4f}"),
                ("T1", f"{p.t1:.4f}"),
                ("R:R", f"{p.rr:.2f}"),
                ("Shares", f"{p.shares}{cap}"),
            ]
        rows.append(("Failed", str(z.failed_attempts)))
    if snap.day_gain is not None:
        rows.append(("Day gain", f"{snap.day_gain * 100:+.1f}%"))
    lines = [f"{k:<9} {v}" for k, v in rows]
    if snap.checks:
        lines.append("Checklist")
        lines += [f"  [{'x' if c.ok else ' '}] {c.name:<17} {c.detail}" for c in snap.checks]
    width = max(len(line) for line in lines)
    bar = "+" + "-" * (width + 2) + "+"
    return "\n".join([bar] + [f"| {line:<{width}} |" for line in lines] + [bar])
