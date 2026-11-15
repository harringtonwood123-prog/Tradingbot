from __future__ import annotations

from datetime import timedelta

import pytest

from helpers import bar, candle, et, flat_bars
from wickfill.config import Config
from wickfill.engine import (
    State, SymbolEngine, Zone, bucket_bounds, build_plan, is_wick_candle, update_zone_state,
)

M5 = timedelta(minutes=5)


# ---------------- wick detector ----------------

def test_wick_candle_qualifies():
    s = Config().strategy
    assert is_wick_candle(candle(2.6, 3.6, 2.56, 2.8, 900), [100] * 5, s)


@pytest.mark.parametrize("c, vols, why", [
    (candle(2.6, 3.6, 2.0, 2.9, 900), [100] * 5, "wick under half the range"),
    (candle(2.6, 2.9, 2.75, 2.8, 900), [100] * 5, "wick under 4% of close"),
    (candle(2.6, 3.6, 2.56, 2.8, 199), [100] * 5, "volume under 2x average"),
    (candle(2.6, 3.6, 2.56, 2.8, 900), [100] * 4, "fewer than 5 prior candles"),
    (candle(2.8, 2.8, 2.8, 2.8, 900), [100] * 5, "zero range"),
])
def test_wick_candle_rejected(c, vols, why):
    assert not is_wick_candle(c, vols, Config().strategy), why


def test_wick_thresholds_are_inclusive():
    # wick = 0.5 of range exactly, 4% of close exactly, volume exactly 2x
    c = candle(1.0, 1.04, 0.96, 1.0, 200)
    assert is_wick_candle(c, [100] * 5, Config().strategy)


def test_volume_average_uses_only_last_20():
    s = Config().strategy
    vols = [10_000] * 30 + [100] * 20
    assert is_wick_candle(candle(2.6, 3.6, 2.56, 2.8, 200), vols, s)


# ---------------- state machine ----------------

def _zone(level=2.8, target=3.6) -> Zone:
    t = et(2026, 11, 3, 8)
    return Zone("Z", "T", "5m", level, target, t, t + M5, t + timedelta(days=3))


def _step(z, low, close):
    return update_zone_state(z, bar(et(2026, 11, 3, 9), close, max(close, low) + 0.01, low, close), Config().strategy)


def test_state_sequence_reclaim_extended_retest():
    z = _zone()
    _step(z, 2.70, 2.75); assert z.state == State.BELOW_LEVEL
    _step(z, 2.78, 2.84); assert z.state == State.RECLAIMING and z.closes_above == 1
    _step(z, 2.83, 2.88); assert z.state == State.FRESH_RECLAIM
    _step(z, 2.87, 2.94); assert z.state == State.EXTENDED
    _step(z, 2.81, 2.87); assert z.state == State.AT_RETEST
    assert z.failed_attempts == 0


def test_retest_tolerance_boundary():
    z = _zone(level=2.0)
    _step(z, 2.05, 2.1); _step(z, 2.05, 2.1)
    _step(z, 2.021, 2.1); assert z.state == State.EXTENDED  # low just above level * 1.01
    _step(z, 2.02, 2.1); assert z.state == State.AT_RETEST  # low == level * 1.01


def test_close_at_level_is_not_above():
    z = _zone()
    _step(z, 2.75, 2.80)
    assert z.state == State.BELOW_LEVEL


def test_failure_counts_only_after_confirmed_reclaim():
    z = _zone()
    _step(z, 2.78, 2.84)                 # 1 of 2
    assert _step(z, 2.70, 2.75) is False  # dropped before confirming: not a failure
    assert z.failed_attempts == 0
    _step(z, 2.78, 2.84); _step(z, 2.83, 2.88)  # confirmed
    assert _step(z, 2.70, 2.80) is True   # closes back AT the level: failure
    assert z.failed_attempts == 1 and z.state == State.BELOW_LEVEL and z.closes_above == 0


# ---------------- risk plan ----------------

def test_plan_matches_spec_formulas():
    cfg = Config()
    p = build_plan(2.8, 3.6, 2.87, 0.05, cfg.strategy, cfg.risk, 1000, 1000)
    assert p.stop == pytest.approx(min(2.8 - 2 * 0.05, 2.87 * 0.985))  # 2.70
    assert p.t1 == pytest.approx(3.2)
    assert p.rr == pytest.approx((3.6 - 2.87) / (2.87 - 2.70))
    assert p.shares == 58  # floor(10 / 0.17)
    assert p.capped_by is None


def test_plan_uses_percent_stop_when_atr_is_tiny():
    cfg = Config()
    p = build_plan(2.8, 3.6, 2.81, 0.001, cfg.strategy, cfg.risk, 1000, 1000)
    assert p.stop == pytest.approx(2.81 * 0.985)


def test_plan_size_capped_by_buying_power_and_limits():
    cfg = Config()
    p = build_plan(2.8, 3.6, 2.81, 0.001, cfg.strategy, cfg.risk, 1000, 500)
    assert p.shares == 177 and p.capped_by == "buying_power"  # floor(500 / 2.81)
    cfg.risk.max_shares = 50
    p = build_plan(2.8, 3.6, 2.81, 0.001, cfg.strategy, cfg.risk, 1000, 500)
    assert p.shares == 50 and p.capped_by == "max_shares"


# ---------------- timeframes ----------------

def test_bucket_bounds():
    ts = et(2026, 11, 4, 12, 10)  # a Wednesday
    assert bucket_bounds("15m", ts) == (et(2026, 11, 4, 12, 0), et(2026, 11, 4, 12, 15))
    assert bucket_bounds("60m", ts) == (et(2026, 11, 4, 12, 0), et(2026, 11, 4, 13, 0))
    assert bucket_bounds("1d", ts) == (et(2026, 11, 4, 9, 30), et(2026, 11, 4, 16, 0))
    assert bucket_bounds("1w", ts) == (et(2026, 11, 2, 9, 30), et(2026, 11, 6, 16, 0))
    assert bucket_bounds("1d", et(2026, 11, 4, 17, 0)) is None  # after hours
    assert bucket_bounds("1d", et(2026, 11, 4, 8, 0)) is None   # pre-market


# ---------------- full engine ----------------

def _cfg5() -> Config:
    cfg = Config()
    cfg.strategy.timeframes = ["5m"]  # keep these tests to a single zone
    return cfg


def _story_engine(**kw) -> tuple[SymbolEngine, list]:
    """Prior close 2.00; today flat at 2.60 pre-market, then a wick candle."""
    eng = SymbolEngine("T", _cfg5(), prev_close=2.00)
    eng.catalyst_checked = kw.get("catalyst", True)
    events = []
    bars = flat_bars(et(2026, 11, 3, 7, 0), 21, price=2.60)  # 07:00 .. 08:40
    bars.append(bar(et(2026, 11, 3, 8, 45), 2.60, 3.60, 2.56, 2.80, 900_000))  # wick
    for b in bars:
        events += eng.on_bar(b).events
    return eng, events


def _feed(eng, rows, start):
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        out.append(eng.on_bar(bar(start + i * M5, o, h, l, c)))
    return out


def test_zone_created_from_5m_wick_and_active_after_close():
    eng, events = _story_engine()
    new = [e for e in events if e.kind == "ZONE_NEW" and e.zone.tf == "5m"]
    assert len(new) == 1
    z = new[0].zone
    assert (z.level, z.target) == (2.80, 3.60)
    assert z.created_at == et(2026, 11, 3, 8, 50)
    assert z.status == "ACTIVE"  # its own candle's high does not fill it


def test_buy_a_on_retest_with_full_plan():
    eng, _ = _story_engine()
    res = _feed(eng, [
        (2.80, 2.82, 2.74, 2.76),  # below
        (2.76, 2.86, 2.75, 2.84),  # 1 of 2
        (2.84, 2.90, 2.83, 2.88),  # fresh reclaim (BUY_B disabled by default)
        (2.88, 2.96, 2.87, 2.94),  # extended
        (2.94, 2.95, 2.81, 2.87),  # retest
    ], et(2026, 11, 3, 8, 50))
    states = [r.snapshot.state for r in res]
    assert states == [State.BELOW_LEVEL, State.RECLAIMING, State.FRESH_RECLAIM, State.EXTENDED, State.AT_RETEST]
    assert all(r.snapshot.display == "WAIT" for r in res[:4])
    snap = res[-1].snapshot
    assert snap.display == "BUY A" and snap.signal.actionable
    sig = [e for e in res[-1].events if e.kind == "SIGNAL"]
    assert len(sig) == 1 and sig[0].signal.kind == "BUY_A"
    p = snap.plan
    assert p.stop < 2.87 * 0.985 + 1e-9 and p.t1 == pytest.approx(3.2) and p.target == 3.6
    assert p.shares == int(10 / (p.entry - p.stop))


def test_buy_b_only_when_enabled():
    cfg = _cfg5(); cfg.strategy.enable_buy_b = True
    eng = SymbolEngine("T", cfg, prev_close=2.00); eng.catalyst_checked = True
    for b in flat_bars(et(2026, 11, 3, 7, 0), 21, price=2.60):
        eng.on_bar(b)
    eng.on_bar(bar(et(2026, 11, 3, 8, 45), 2.60, 3.60, 2.56, 2.80, 900_000))
    res = _feed(eng, [(2.80, 2.86, 2.79, 2.84), (2.84, 2.90, 2.83, 2.88)], et(2026, 11, 3, 8, 50))
    assert res[-1].snapshot.display == "BUY B"


def _retest_rows():
    return [(2.80, 2.86, 2.79, 2.84), (2.84, 2.90, 2.83, 2.88), (2.88, 2.90, 2.81, 2.87)]


def test_blocked_without_catalyst_tick():
    eng, _ = _story_engine(catalyst=False)
    res = _feed(eng, _retest_rows(), et(2026, 11, 3, 8, 50))
    snap = res[-1].snapshot
    assert snap.state == State.AT_RETEST and snap.display == "WAIT"
    assert snap.signal.blocks == ("catalyst_checked",)
    assert [e.kind for e in res[-1].events if e.kind in ("SIGNAL", "SETUP_SKIPPED")] == ["SETUP_SKIPPED"]


def test_blocked_when_halted():
    eng, _ = _story_engine()
    eng.halted = True
    res = _feed(eng, _retest_rows(), et(2026, 11, 3, 8, 50))
    assert "not_halted" in res[-1].snapshot.signal.blocks


def test_blocked_when_day_gain_too_small_or_unknown():
    eng, _ = _story_engine()
    eng.prev_close = 2.70  # +6%
    res = _feed(eng, _retest_rows(), et(2026, 11, 3, 8, 50))
    assert "day_gain" in res[-1].snapshot.signal.blocks
    eng2, _ = _story_engine()
    eng2.prev_close = None
    res = _feed(eng2, _retest_rows(), et(2026, 11, 3, 8, 50))
    assert "day_gain" in res[-1].snapshot.signal.blocks


def test_blocked_when_dollar_volume_thin():
    eng, _ = _story_engine()
    eng.cfg.strategy.min_avg_dollar_volume = 10_000_000
    res = _feed(eng, _retest_rows(), et(2026, 11, 3, 8, 50))
    assert "dollar_volume" in res[-1].snapshot.signal.blocks


def test_blocked_when_too_far_toward_target():
    eng, _ = _story_engine()
    # two closes above, then a bar that dips to the level but closes 65% of the way up
    res = _feed(eng, [(2.80, 2.86, 2.79, 2.84), (2.84, 2.90, 2.83, 2.88), (2.88, 3.35, 2.82, 3.32)],
                et(2026, 11, 3, 8, 50))
    assert "not_extended" in res[-1].snapshot.signal.blocks


def test_blocked_when_reward_risk_too_low():
    eng, _ = _story_engine()
    eng.cfg.strategy.min_rr = 50
    res = _feed(eng, _retest_rows(), et(2026, 11, 3, 8, 50))
    assert "reward_risk" in res[-1].snapshot.signal.blocks


def test_blocked_outside_signal_window():
    eng = SymbolEngine("T", _cfg5(), prev_close=2.00); eng.catalyst_checked = True
    for b in flat_bars(et(2026, 11, 3, 15, 0), 21, price=2.60):  # 15:00 .. 16:40
        eng.on_bar(b)
    eng.on_bar(bar(et(2026, 11, 3, 16, 45), 2.60, 3.60, 2.56, 2.80, 900_000))
    res = _feed(eng, _retest_rows(), et(2026, 11, 3, 16, 50))  # after hours
    snap = res[-1].snapshot
    assert snap.state == State.AT_RETEST and "signal_window" in snap.signal.blocks


def test_last_regular_bar_is_outside_window():
    eng = SymbolEngine("T", Config(), prev_close=2.00)
    eng.on_bar(bar(et(2026, 11, 3, 15, 50), 2, 2.1, 1.9, 2))
    z = Zone("Z", "T", "5m", 2.0, 3.0, et(2026, 11, 3, 15), et(2026, 11, 3, 15, 5), et(2026, 11, 6, 15))
    ok = {c.name: c.ok for c in eng._checks(bar(et(2026, 11, 3, 15, 50), 2, 2.1, 1.9, 2.05), z, None, 0.5)}
    assert ok["signal_window"]
    ok = {c.name: c.ok for c in eng._checks(bar(et(2026, 11, 3, 15, 55), 2, 2.1, 1.9, 2.05), z, None, 0.5)}
    assert not ok["signal_window"]  # closes at 16:00: too late to enter


def test_failed_zone_blocks_later_signals():
    eng, _ = _story_engine()
    res = _feed(eng, _retest_rows() + [
        (2.87, 2.88, 2.70, 2.75),  # closes back below: FAIL
        (2.75, 2.86, 2.74, 2.84), (2.84, 2.90, 2.83, 2.88), (2.88, 2.90, 2.81, 2.87),  # reclaim + retest again
    ], et(2026, 11, 3, 8, 50))
    assert any(e.kind == "ZONE_FAILED" for e in res[3].events)
    snap = res[-1].snapshot
    assert snap.state == State.AT_RETEST and snap.zone.failed_attempts == 1
    assert "zone_not_failed" in snap.signal.blocks and snap.display == "WAIT"


def test_wick_filled_by_later_high_and_time_recorded():
    eng, _ = _story_engine()
    res = _feed(eng, [(2.80, 3.59, 2.79, 3.50), (3.50, 3.60, 3.45, 3.55)], et(2026, 11, 3, 8, 50))
    assert not any(e.kind == "WICK_FILLED" for e in res[0].events)  # 3.59 < 3.60
    filled = [e for e in res[1].events if e.kind == "WICK_FILLED"]
    assert filled and filled[0].zone.filled_at == et(2026, 11, 3, 8, 55)
    assert res[1].snapshot.state == State.NO_ZONE


def test_intraday_zone_expires_after_three_days():
    eng, _ = _story_engine()
    r = eng.on_bar(bar(et(2026, 11, 6, 8, 45), 2.7, 2.72, 2.68, 2.7))  # just under 3 days
    assert not any(e.kind == "ZONE_EXPIRED" for e in r.events)
    r = eng.on_bar(bar(et(2026, 11, 6, 8, 50), 2.7, 2.72, 2.68, 2.7))
    assert [e.zone.tf for e in r.events if e.kind == "ZONE_EXPIRED"] == ["5m"]


def test_zone_too_far_above_price_is_ignored():
    eng, _ = _story_engine()
    r = eng.on_bar(bar(et(2026, 11, 3, 8, 50), 1.2, 1.21, 1.15, 1.19))  # 3.60 > 1.19 * 3
    assert r.snapshot.state == State.NO_ZONE and r.snapshot.active_zones >= 1


def test_15m_and_60m_candles_aggregate_into_zones():
    cfg = Config(); cfg.strategy.timeframes = ["15m", "60m"]
    eng = SymbolEngine("T", cfg)
    events = []
    for b in flat_bars(et(2026, 11, 4, 6, 0), 72, price=5.0):  # 06:00 .. 11:55
        events += eng.on_bar(b).events
    rows = [(5.0, 12.58, 4.9, 9.0, 2_000_000), (9.0, 9.5, 7.5, 8.0, 500_000), (8.0, 8.2, 7.0, 7.13, 400_000)]
    for i, (o, h, l, c, v) in enumerate(rows):
        events += eng.on_bar(bar(et(2026, 11, 4, 12, 5 * i), o, h, l, c, v)).events
    z15 = [e.zone for e in events if e.kind == "ZONE_NEW"]
    assert [(z.tf, z.level, z.target) for z in z15] == [("15m", 7.13, 12.58)]
    assert z15[0].candle_start == et(2026, 11, 4, 12, 0) and z15[0].created_at == et(2026, 11, 4, 12, 15)
    for i in range(3, 12):  # rest of the hour drifts down to 5.315
        c = 5.315 if i == 11 else 6.5
        events += eng.on_bar(bar(et(2026, 11, 4, 12, 5 * i), 6.5, 6.6, 5.3, c, 60_000)).events
    z60 = [e.zone for e in events if e.kind == "ZONE_NEW" and e.zone.tf == "60m"]
    assert [(z.level, z.target) for z in z60] == [(5.315, 12.58)]
    # both fill on the same later bar
    events = eng.on_bar(bar(et(2026, 11, 4, 15, 30), 11.0, 12.60, 10.9, 12.0)).events
    assert sorted(e.zone.tf for e in events if e.kind == "WICK_FILLED") == ["15m", "60m"]


def test_daily_candle_uses_regular_session_only_and_closes_at_1600():
    cfg = Config(); cfg.strategy.timeframes = ["1d"]; cfg.strategy.volume_min_prior = 1
    eng = SymbolEngine("T", cfg)
    events = []
    # Mon: quiet full day. Tue: spike high in the regular session, close low.
    for b in flat_bars(et(2026, 11, 2, 9, 30), 78, price=2.0, v=1000):
        events += eng.on_bar(b).events
    events += eng.on_bar(bar(et(2026, 11, 3, 8, 0), 2.0, 9.9, 2.0, 2.0, 5000)).events  # pre-market: ignored by daily
    events += eng.on_bar(bar(et(2026, 11, 3, 9, 30), 2.0, 4.0, 1.9, 2.5, 500_000)).events
    for b in flat_bars(et(2026, 11, 3, 9, 35), 77, price=2.2, v=1000):
        events += eng.on_bar(b).events
    zones = [e.zone for e in events if e.kind == "ZONE_NEW"]
    assert len(zones) == 1
    z = zones[0]
    assert (z.tf, z.level, z.target) == ("1d", 2.2, 4.0)
    assert z.created_at == et(2026, 11, 3, 16, 0)
    assert z.expires_at == z.created_at + timedelta(days=120)


def test_partial_first_candle_is_skipped():
    cfg = Config(); cfg.strategy.timeframes = ["60m"]; cfg.strategy.volume_min_prior = 1
    eng = SymbolEngine("T", cfg)
    eng.on_bar(bar(et(2026, 11, 3, 9, 40), 2, 2.01, 1.99, 2))  # data starts mid-hour
    assert eng._last_candle_start == {}
    for b in flat_bars(et(2026, 11, 3, 9, 45), 15):
        eng.on_bar(b)
    assert eng._last_candle_start["60m"] == et(2026, 11, 3, 10, 0)  # 09:00 candle never recorded


def test_prev_close_rolls_from_regular_session():
    eng = SymbolEngine("T", Config())
    eng.on_bar(bar(et(2026, 11, 2, 15, 55), 2, 2.1, 1.9, 2.0))
    eng.on_bar(bar(et(2026, 11, 2, 17, 0), 2, 2.6, 1.9, 2.5))  # after hours: not the close
    r = eng.on_bar(bar(et(2026, 11, 3, 8, 0), 2.5, 2.7, 2.4, 2.6))
    assert eng.prev_close == 2.0 and r.snapshot.day_gain == pytest.approx(0.30)


def test_out_of_order_bars_rejected():
    eng = SymbolEngine("T", Config())
    eng.on_bar(bar(et(2026, 11, 3, 10, 0), 2, 2.1, 1.9, 2))
    with pytest.raises(ValueError):
        eng.on_bar(bar(et(2026, 11, 3, 10, 0), 2, 2.1, 1.9, 2))
