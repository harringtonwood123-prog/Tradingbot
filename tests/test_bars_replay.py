from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from helpers import bar, et
from wickfill.bars import detect_interval_minutes, drop_forming, load_csv, quality_notes, resample
from wickfill.cli import main
from wickfill.config import Config, load_config
from wickfill.replay import run_replay
from wickfill.sample import write_sample

UTC = timezone.utc


def test_load_csv_converts_to_utc_and_sorts(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text(
        "Time,Open,High,Low,Close,Volume\n"
        "2026-11-04 09:35,2,2.1,1.9,2.05,100\n"       # naive -> assumed Eastern
        "2026-11-04T09:30:00-05:00,2,2.1,1.9,2,100\n"  # explicit offset, out of order
        "1793803200,2,2.1,1.9,2,100\n"                 # unix seconds
        "bad,row,here,x,y,z\n"
        "2026-11-04 09:40,2,1.5,1.9,2,100\n"           # high < low
    )
    bars, warnings = load_csv(p)
    assert [b.ts for b in bars][:2] == [
        datetime(2026, 11, 4, 14, 30, tzinfo=UTC), datetime(2026, 11, 4, 14, 35, tzinfo=UTC)]
    assert all(b.ts.utcoffset() == timedelta(0) for b in bars)
    assert len(bars) == 3
    assert any("no timezone" in w for w in warnings) and any("Dropped 2" in w for w in warnings)


def test_load_csv_missing_column(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("time,open,high,low,close\n")
    with pytest.raises(ValueError, match="volume"):
        load_csv(p)


def test_resample_does_not_fill_empty_buckets():
    t = et(2026, 11, 4, 10, 0)
    ones = [bar(t + timedelta(minutes=m), 2 + m / 100, 2.5 + m / 100, 1.9, 2.1 + m / 100, 10)
            for m in (0, 1, 4, 11, 12)]  # nothing traded 10:05-10:09
    out = resample(ones, 5)
    assert [b.ts for b in out] == [t, t + timedelta(minutes=10)]
    assert out[0].open == 2.0 and out[0].close == pytest.approx(2.14) and out[0].volume == 30
    assert out[0].high == pytest.approx(2.54) and out[1].volume == 20
    assert detect_interval_minutes(ones) == 1


def test_drop_forming_bar():
    t = et(2026, 11, 4, 10, 0)
    bars = [bar(t, 2, 2.1, 1.9, 2), bar(t + timedelta(minutes=5), 2, 2.1, 1.9, 2)]
    assert drop_forming(bars, t + timedelta(minutes=9), 5) == bars[:1]
    assert drop_forming(bars, t + timedelta(minutes=10), 5) == bars


def test_quality_notes_flag_gaps_and_possible_splits():
    t = et(2026, 11, 4, 10, 0)
    bars = [bar(t, 2, 2.1, 1.9, 2), bar(t + timedelta(minutes=20), 2, 2.1, 1.9, 2),
            bar(t + timedelta(days=1), 20, 21, 19, 20)]
    notes = quality_notes(bars, 5)
    assert any("3 regular-session" in n for n in notes)
    assert any("split" in n for n in notes)


def test_config_rejects_typos_and_margin(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("strategy:\n  min_rrr: 2\n")
    with pytest.raises(ValueError, match="min_rrr"):
        load_config(p)
    p.write_text("risk:\n  equity: 1000\n  buying_power: 4000\n")
    with pytest.raises(ValueError, match="margin"):
        load_config(p)
    p.write_text("risk:\n  equity: 2500\nstrategy:\n  enable_buy_b: true\n")
    cfg = load_config(p)
    assert cfg.risk.equity == 2500.0 and cfg.strategy.enable_buy_b is True


def test_replay_sample_story(tmp_path):
    p = tmp_path / "s.csv"
    write_sample(p)
    bars, warnings = load_csv(p)
    assert not warnings
    res = run_replay(bars, "DEMO", Config(), catalyst_checked=True)
    kinds = [e.kind for e in res.events]
    assert kinds.count("ZONE_NEW") == 3 and kinds.count("WICK_FILLED") == 3
    signals = [e for e in res.events if e.kind == "SIGNAL"]
    assert signals and all(e.signal.kind == "BUY_A" for e in signals)
    assert all(e.signal.plan.rr >= 1.5 and e.signal.plan.shares >= 1 for e in signals)
    # Without the manual catalyst tick nothing is actionable.
    res = run_replay(bars, "DEMO", Config())
    assert not [e for e in res.events if e.kind == "SIGNAL"]
    assert [e for e in res.events if e.kind == "SETUP_SKIPPED"]


def test_cli_replay_runs(tmp_path, capsys):
    p = tmp_path / "s.csv"
    assert main(["sample", str(p)]) == 0
    assert main(["replay", str(p), "--symbol", "demo", "--catalyst-ok"]) == 0
    out = capsys.readouterr().out
    assert "WICK_FILLED" in out and "Not financial advice" in out and "CST" in out
