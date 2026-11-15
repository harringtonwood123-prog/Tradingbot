"""The Wick Fill strategy engine.

Wick detector, zone lifecycle, state machine, signal checks and risk plan all
live in this ONE module. Replay, backtest, paper and live all feed completed
5-minute bars into `SymbolEngine.on_bar()` and read back the same results, so
there is exactly one implementation of the strategy.

The engine does no I/O: no files, no network, no clock. Everything it knows
comes from the bars and flags it is given.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from .config import Config, RiskConfig, StrategyConfig
from .models import UTC, Bar, Candle

ET = ZoneInfo("America/New_York")
BASE_MINUTES = 5
BASE = timedelta(minutes=BASE_MINUTES)
REG_OPEN = time(9, 30)
REG_CLOSE = time(16, 0)
INTRADAY_TFS = {"5m", "15m", "60m"}


class State(str, Enum):
    NO_ZONE = "NO_ZONE"
    BELOW_LEVEL = "BELOW_LEVEL"
    RECLAIMING = "RECLAIMING"
    FRESH_RECLAIM = "FRESH_RECLAIM"
    AT_RETEST = "AT_RETEST"
    EXTENDED = "EXTENDED"


@dataclass
class Zone:
    id: str
    symbol: str
    tf: str
    level: float  # wick candle close
    target: float  # wick candle high
    candle_start: datetime
    created_at: datetime  # when the wick candle closed (zone becomes active)
    expires_at: datetime
    status: str = "ACTIVE"  # ACTIVE | FILLED | EXPIRED
    filled_at: datetime | None = None  # start of the bar that filled it
    failed_attempts: int = 0
    closes_above: int = 0  # consecutive 5m closes above the level
    confirmed: bool = False  # reclaim confirmed and still holding
    state: State = State.BELOW_LEVEL


@dataclass(frozen=True)
class RiskPlan:
    entry: float
    stop: float
    t1: float
    target: float
    risk_per_share: float
    rr: float  # reward:risk to the wick high
    shares: int
    capped_by: str | None  # which limit reduced the size, if any


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Signal:
    kind: str  # BUY_A | BUY_B
    zone_id: str
    plan: RiskPlan | None
    blocks: tuple[str, ...]  # names of failing checks; empty = actionable

    @property
    def actionable(self) -> bool:
        return not self.blocks


@dataclass(frozen=True)
class Event:
    kind: str  # ZONE_NEW | WICK_FILLED | ZONE_EXPIRED | ZONE_FAILED | STATE_CHANGE | SIGNAL | SETUP_SKIPPED
    bar_ts: datetime  # start of the 5m bar on which it happened (known at that bar's close)
    symbol: str
    zone: Zone | None
    detail: str
    signal: Signal | None = None


@dataclass(frozen=True)
class Snapshot:
    """Everything the status box needs, for the focus zone, after one bar."""

    bar_ts: datetime
    symbol: str
    price: float
    state: State
    display: str  # WAIT | BUY A | BUY B
    zone: Zone | None
    plan: RiskPlan | None
    checks: tuple[Check, ...]
    signal: Signal | None
    day_gain: float | None
    atr: float | None
    active_zones: int


@dataclass(frozen=True)
class BarResult:
    snapshot: Snapshot
    events: tuple[Event, ...]


# --------------------------------------------------------------------------
# Pure strategy functions
# --------------------------------------------------------------------------

def is_wick_candle(c: Candle, prior_volumes: list[float], s: StrategyConfig) -> bool:
    """True if a completed candle qualifies as a wick zone."""
    rng = c.high - c.low
    if rng <= 0 or c.close <= 0 or c.volume <= 0:
        return False
    wick = c.high - c.close
    if wick / rng < s.wick_min_ratio:
        return False
    if wick / c.close < s.wick_min_pct:
        return False
    prior = prior_volumes[-s.volume_lookback:]
    if len(prior) < s.volume_min_prior:
        return False
    avg = sum(prior) / len(prior)
    return avg > 0 and c.volume >= s.volume_mult * avg


def update_zone_state(z: Zone, bar: Bar, s: StrategyConfig) -> bool:
    """Advance one zone's state machine with a completed 5m bar.

    Returns True if this bar made the zone FAIL (a confirmed reclaim closed
    back at or below the level).
    """
    if bar.close <= z.level:
        failed = z.confirmed
        if failed:
            z.failed_attempts += 1
        z.closes_above = 0
        z.confirmed = False
        z.state = State.BELOW_LEVEL
        return failed
    z.closes_above += 1
    if z.closes_above < s.reclaim_closes:
        z.state = State.RECLAIMING
    elif z.closes_above == s.reclaim_closes:
        z.confirmed = True
        z.state = State.FRESH_RECLAIM
    elif bar.low <= z.level * (1 + s.retest_tolerance):
        z.state = State.AT_RETEST
    else:
        z.state = State.EXTENDED
    return False


def build_plan(
    level: float,
    target: float,
    price: float,
    atr: float,
    s: StrategyConfig,
    r: RiskConfig,
    equity: float,
    buying_power: float,
) -> RiskPlan:
    """Stop, targets, reward:risk and position size for an entry at `price`."""
    stop = min(level - s.atr_stop_mult * atr, price * (1 - s.pct_stop))
    stop = max(stop, 0.0)
    risk_ps = price - stop
    t1 = level + s.t1_fraction * (target - level)
    rr = (target - price) / risk_ps if risk_ps > 0 else 0.0
    shares = math.floor((equity * r.risk_pct) / risk_ps) if risk_ps > 0 else 0
    capped_by = None
    for name, cap in (
        ("buying_power", math.floor(buying_power / price)),
        ("max_position_value", math.floor(r.max_position_value / price)),
        ("max_shares", r.max_shares),
    ):
        if shares > cap:
            shares, capped_by = cap, name
    return RiskPlan(price, stop, t1, target, risk_ps, rr, max(shares, 0), capped_by)


# --------------------------------------------------------------------------
# Timeframe aggregation (5m -> 15m / 60m / daily / weekly)
# --------------------------------------------------------------------------

def _parse_hhmm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


def is_regular_session(ts: datetime) -> bool:
    et = ts.astimezone(ET)
    return et.weekday() < 5 and REG_OPEN <= et.time() < REG_CLOSE


def bucket_bounds(tf: str, ts: datetime) -> tuple[datetime, datetime] | None:
    """(start, end) in UTC of the `tf` candle containing a bar starting at `ts`.

    15m and 60m candles sit on clock boundaries and include extended hours.
    Daily and weekly candles use the regular session only (09:30-16:00 ET);
    bars outside it belong to no daily/weekly candle (returns None).
    """
    if tf in ("15m", "60m"):
        mins = 15 if tf == "15m" else 60
        start = ts.replace(minute=ts.minute // mins * mins, second=0, microsecond=0)
        return start, start + timedelta(minutes=mins)
    if not is_regular_session(ts):
        return None
    d = ts.astimezone(ET).date()
    if tf == "1d":
        first, last = d, d
    elif tf == "1w":
        first = d - timedelta(days=d.weekday())
        last = first + timedelta(days=4)
    else:
        raise ValueError(f"Unsupported timeframe {tf}")
    start = datetime.combine(first, REG_OPEN, ET).astimezone(UTC)
    end = datetime.combine(last, REG_CLOSE, ET).astimezone(UTC)
    return start, end


class _Aggregator:
    """Builds one higher timeframe from 5m bars. Never invents missing bars."""

    def __init__(self, tf: str) -> None:
        self.tf = tf
        self._start: datetime | None = None
        self._end: datetime | None = None
        self._o = self._h = self._l = self._c = self._v = 0.0

    def _finish(self) -> Candle:
        assert self._start is not None and self._end is not None
        c = Candle(self.tf, self._start, self._end, self._o, self._h, self._l, self._c, self._v)
        self._start = self._end = None
        return c

    def roll(self, ts: datetime) -> Candle | None:
        """Close the pending candle if a bar starting at `ts` is past its end."""
        if self._end is not None and ts >= self._end:
            return self._finish()
        return None

    def add(self, bar: Bar) -> Candle | None:
        """Add a bar; returns the candle if this bar completes it on time."""
        bounds = bucket_bounds(self.tf, bar.ts)
        if bounds is None:
            return None
        if self._start is None:
            self._start, self._end = bounds
            self._o, self._h, self._l, self._v = bar.open, bar.high, bar.low, 0.0
        self._h = max(self._h, bar.high)
        self._l = min(self._l, bar.low)
        self._c = bar.close
        self._v += bar.volume
        if bar.ts + BASE >= self._end:  # type: ignore[operator]
            return self._finish()
        return None


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------

@dataclass
class _Atr:
    period: int
    value: float | None = None
    _prev_close: float | None = None
    _seed: list[float] = field(default_factory=list)

    def update(self, bar: Bar) -> None:
        tr = bar.high - bar.low
        if self._prev_close is not None:
            tr = max(tr, abs(bar.high - self._prev_close), abs(bar.low - self._prev_close))
        self._prev_close = bar.close
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) >= self.period:
                self.value = sum(self._seed) / len(self._seed)
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period


class SymbolEngine:
    """Tracks one symbol. Feed it completed 5m bars, oldest first."""

    def __init__(self, symbol: str, cfg: Config, prev_close: float | None = None) -> None:
        self.symbol = symbol
        self.cfg = cfg
        self.s = cfg.strategy
        self.zones: list[Zone] = []
        self.prev_close = prev_close  # prior regular-session close, for day gain
        self.catalyst_checked = False  # the manual "I checked the news" tick
        self.halted = False
        self.equity = cfg.risk.equity
        self.buying_power = cfg.risk.buying_power if cfg.risk.buying_power is not None else cfg.risk.equity

        self._aggs = {tf: _Aggregator(tf) for tf in self.s.timeframes if tf != "5m"}
        self._volumes: dict[str, deque[float]] = {
            tf: deque(maxlen=self.s.volume_lookback) for tf in self.s.timeframes
        }
        self._last_candle_start: dict[str, datetime] = {}
        self._first_ts: datetime | None = None
        self._last_ts: datetime | None = None
        self._atr = _Atr(self.s.atr_period)
        self._dollar_vol: deque[float] = deque(maxlen=self.s.dollar_volume_lookback)
        self._session_date: date | None = None
        self._last_reg_close: float | None = None
        self._focus_id: str | None = None
        self._focus_state: State = State.NO_ZONE
        self._last_signal_key: tuple | None = None
        self._win_start = _parse_hhmm(self.s.signal_start_et)
        self._win_end = _parse_hhmm(self.s.signal_end_et)

    # ---- inputs other than bars ------------------------------------------

    def set_account(self, equity: float, buying_power: float) -> None:
        self.equity, self.buying_power = equity, buying_power

    def seed_candles(self, tf: str, candles: list[Candle]) -> None:
        """Preload completed higher-timeframe history (e.g. daily candles from
        a provider) so daily/weekly zones exist before the 5m data starts.
        Call before the first on_bar(). Candles must be oldest first and end
        before the 5m data begins."""
        if self._first_ts is not None:
            raise RuntimeError("seed_candles() must be called before on_bar()")
        if tf not in self._volumes:
            return
        for c in candles:
            self._expire_and_fill(c.start, c.high, [])
            self._on_candle(c, [])

    # ---- main entry point ------------------------------------------------

    def on_bar(self, bar: Bar) -> BarResult:
        if self._last_ts is not None and bar.ts <= self._last_ts:
            raise ValueError(f"Bars must be strictly increasing in time; got {bar.ts} after {self._last_ts}")
        if self._first_ts is None:
            self._first_ts = bar.ts
        self._last_ts = bar.ts
        events: list[Event] = []

        # 1. Candles that ended before this bar (late close after a gap in trading).
        for agg in self._aggs.values():
            c = agg.roll(bar.ts)
            if c is not None:
                self._on_candle(c, events, bar.ts)

        # 2. Expire old zones, then check fills against this bar's high.
        self._expire_and_fill(bar.ts, bar.high, events)

        # 3. This bar as a 5m candle, plus any higher candle it completes.
        if "5m" in self.s.timeframes:
            self._on_candle(
                Candle("5m", bar.ts, bar.ts + BASE, bar.open, bar.high, bar.low, bar.close, bar.volume),
                events, bar.ts,
            )
        for agg in self._aggs.values():
            c = agg.add(bar)
            if c is not None:
                self._on_candle(c, events, bar.ts)

        # 4. State machines for every active zone that existed before this bar closed.
        for z in self._active():
            if z.created_at > bar.ts:  # created by this very bar; starts next bar
                continue
            if update_zone_state(z, bar, self.s):
                events.append(Event(
                    "ZONE_FAILED", bar.ts, self.symbol, z,
                    f"closed {bar.close:.4f} back at/below level {z.level:.4f} "
                    f"(failed attempts: {z.failed_attempts})",
                ))

        # 5. Indicators and day-gain bookkeeping.
        self._atr.update(bar)
        self._dollar_vol.append(bar.close * bar.volume)
        self._track_sessions(bar)

        # 6. Focus zone, plan, checks, signal.
        snapshot = self._snapshot(bar, events)
        return BarResult(snapshot, tuple(events))

    # ---- internals -------------------------------------------------------

    def _active(self) -> list[Zone]:
        return [z for z in self.zones if z.status == "ACTIVE"]

    def _expire_and_fill(self, ts: datetime, high: float, events: list[Event]) -> None:
        for z in self._active():
            if ts >= z.expires_at:
                z.status = "EXPIRED"
                events.append(Event("ZONE_EXPIRED", ts, self.symbol, z, "expired unfilled"))
            elif ts >= z.created_at and high >= z.target:
                z.status = "FILLED"
                z.filled_at = ts
                events.append(Event(
                    "WICK_FILLED", ts, self.symbol, z,
                    f"{z.tf} zone (level {z.level:.4f}): high {high:.4f} reached target {z.target:.4f}",
                ))

    def _on_candle(self, c: Candle, events: list[Event], bar_ts: datetime | None = None) -> None:
        last = self._last_candle_start.get(c.tf)
        if last is not None and c.start <= last:
            return  # already seen (seeded history overlapping aggregated data)
        # A candle that started before our first bar is partial: skip it entirely.
        if self._first_ts is not None and c.start < self._first_ts:
            return
        self._last_candle_start[c.tf] = c.start
        vols = self._volumes[c.tf]
        if is_wick_candle(c, list(vols), self.s):
            days = self.s.intraday_expiry_days if c.tf in INTRADAY_TFS else self.s.htf_expiry_days
            z = Zone(
                id=f"{self.symbol}-{c.tf}-{c.start.strftime('%Y%m%dT%H%MZ')}",
                symbol=self.symbol, tf=c.tf, level=c.close, target=c.high,
                candle_start=c.start, created_at=c.end,
                expires_at=c.end + timedelta(days=days),
            )
            self.zones.append(z)
            events.append(Event(
                "ZONE_NEW", bar_ts or c.start, self.symbol, z,
                f"{c.tf} wick: level {z.level:.4f} -> target {z.target:.4f} "
                f"(+{(z.target / z.level - 1) * 100:.1f}%)",
            ))
        vols.append(c.volume)

    def _track_sessions(self, bar: Bar) -> None:
        d = bar.ts.astimezone(ET).date()
        if self._session_date is None:
            self._session_date = d
        elif d != self._session_date:
            if self._last_reg_close is not None:
                self.prev_close = self._last_reg_close
            self._last_reg_close = None
            self._session_date = d
        if is_regular_session(bar.ts):
            self._last_reg_close = bar.close

    def _pick_focus(self, price: float, ts: datetime) -> Zone | None:
        """Nearest active zone above price: target above price (and not absurdly
        far), nearest by distance from price to the level."""
        limit = price * (1 + self.s.max_target_above_price)
        tf_rank = {"1w": 0, "1d": 1, "60m": 2, "15m": 3, "5m": 4}
        cands = [z for z in self._active() if price < z.target <= limit and z.created_at <= ts]
        if not cands:
            return None
        return min(cands, key=lambda z: (abs(z.level - price), tf_rank[z.tf]))

    def _checks(self, bar: Bar, z: Zone, plan: RiskPlan | None, day_gain: float | None) -> list[Check]:
        s = self.s
        et = bar.ts.astimezone(ET)
        close_et = (bar.ts + BASE).astimezone(ET)
        in_window = (
            et.weekday() < 5
            and et.time() >= self._win_start
            and close_et.date() == et.date()
            and close_et.time() < self._win_end
        )
        checks = [Check("signal_window", in_window, f"{s.signal_start_et}-{s.signal_end_et} ET")]
        if day_gain is None:
            checks.append(Check("day_gain", False, "unknown: no prior close in data"))
        else:
            checks.append(Check("day_gain", day_gain >= s.min_day_gain,
                                f"{day_gain * 100:+.1f}% (need >= {s.min_day_gain * 100:.0f}%)"))
        if plan is None:
            checks.append(Check("data", False, f"need {s.atr_period} bars for ATR"))
        else:
            checks.append(Check("data", True, "ATR available"))
            checks.append(Check("reward_risk", plan.rr >= s.min_rr, f"{plan.rr:.2f} (need >= {s.min_rr})"))
            checks.append(Check("position_size", plan.shares >= 1, f"{plan.shares} shares"))
        progress = (bar.close - z.level) / (z.target - z.level)
        checks.append(Check("not_extended", progress < s.max_progress,
                            f"{progress * 100:.0f}% of the way to target (max {s.max_progress * 100:.0f}%)"))
        checks.append(Check("zone_not_failed", z.failed_attempts <= s.max_failed_attempts,
                            f"{z.failed_attempts} failed attempt(s)"))
        n = len(self._dollar_vol)
        adv = sum(self._dollar_vol) / n if n else 0.0
        checks.append(Check("dollar_volume", n >= 5 and adv >= s.min_avg_dollar_volume,
                            f"${adv:,.0f}/bar (need >= ${s.min_avg_dollar_volume:,.0f})"))
        checks.append(Check("catalyst_checked", self.catalyst_checked, "manual news/filings check"))
        checks.append(Check("not_halted", not self.halted, "halted" if self.halted else "trading"))
        return checks

    def _snapshot(self, bar: Bar, events: list[Event]) -> Snapshot:
        price = bar.close
        day_gain = price / self.prev_close - 1 if self.prev_close else None
        z = self._pick_focus(price, bar.ts)
        n_active = len(self._active())
        if z is None:
            self._note_state(bar, None, State.NO_ZONE, events)
            self._last_signal_key = None
            return Snapshot(bar.ts, self.symbol, price, State.NO_ZONE, "WAIT", None, None, (), None,
                            day_gain, self._atr.value, n_active)

        plan = None
        if self._atr.value is not None:
            plan = build_plan(z.level, z.target, price, self._atr.value, self.s, self.cfg.risk,
                              self.equity, self.buying_power)
        checks = self._checks(bar, z, plan, day_gain)
        self._note_state(bar, z, z.state, events)

        kind = None
        if z.state == State.AT_RETEST:
            kind = "BUY_A"
        elif z.state == State.FRESH_RECLAIM and self.s.enable_buy_b:
            kind = "BUY_B"
        signal = None
        display = "WAIT"
        if kind is None:
            self._last_signal_key = None
        else:
            blocks = tuple(c.name for c in checks if not c.ok)
            signal = Signal(kind, z.id, plan, blocks)
            key = (z.id, kind, blocks)
            if key != self._last_signal_key:
                self._last_signal_key = key
                if signal.actionable:
                    assert plan is not None
                    events.append(Event(
                        "SIGNAL", bar.ts, self.symbol, z,
                        f"{kind} entry~{plan.entry:.4f} stop {plan.stop:.4f} T1 {plan.t1:.4f} "
                        f"target {plan.target:.4f} R:R {plan.rr:.2f} shares {plan.shares}",
                        signal,
                    ))
                else:
                    events.append(Event(
                        "SETUP_SKIPPED", bar.ts, self.symbol, z,
                        f"{kind} blocked by: {', '.join(blocks)}", signal,
                    ))
            if signal.actionable:
                display = "BUY A" if kind == "BUY_A" else "BUY B"
        return Snapshot(bar.ts, self.symbol, price, z.state, display, z, plan, tuple(checks),
                        signal, day_gain, self._atr.value, n_active)

    def _note_state(self, bar: Bar, z: Zone | None, state: State, events: list[Event]) -> None:
        zid = z.id if z else None
        if zid != self._focus_id or state != self._focus_state:
            detail = state.value
            if z is not None and state == State.RECLAIMING:
                detail += f" ({z.closes_above} of {self.s.reclaim_closes} closes above {z.level:.4f})"
            elif z is not None:
                detail += f" (level {z.level:.4f}, target {z.target:.4f}, {z.tf})"
            events.append(Event("STATE_CHANGE", bar.ts, self.symbol, z, detail))
            self._focus_id, self._focus_state = zid, state
