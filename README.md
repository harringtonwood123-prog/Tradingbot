# Wick Fill assistant

A long-only small-cap **scanner + Discord alert + paper-trading** tool for the "Wick Fill"
strategy. It runs on your laptop.

> **Education only. Not financial advice.** Nothing here predicts or promises profit.
> Small-cap day trading can lose money quickly. Check the news and filings before acting.

## Status

| Phase | What | State |
|---|---|---|
| 1 | Core engine + CSV replay + unit tests | **done** |
| 2 | Alpaca data provider, scanner, Discord alerts, inbox, status view | not built |
| 3 | Paper broker, SQLite journal, daily report | not built |
| 4 | Backtester, Monte Carlo, parameter sweep | not built |
| 5 | Optional Alpaca paper-account adapter (off by default, every order confirmed) | not built |

There is **no order placement of any kind** in the code yet.

## Setup (macOS)

You need Python 3.11 or newer. Check with `python3 --version`.

```bash
cd Tradingbot
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Each time you open a new Terminal window, run `source .venv/bin/activate` again first.

## Phase 1: how to run it

**1. Run the tests**

```bash
pytest -q
```

Expect `44 passed`. That means the wick detector, state machine, signal checks, risk maths
and data handling all behave as specified.

**2. Make a synthetic sample file and replay it**

```bash
python -m wickfill sample
```

```bash
python -m wickfill replay data/sample_5m.csv --symbol DEMO --catalyst-ok --verbose
```

The sample is **made-up data** shaped to show the whole story. You should see three
`ZONE_NEW` lines (5m, 15m, 60m wicks, all with target 3.60), state changes through
`RECLAIMING -> FRESH_RECLAIM -> EXTENDED -> AT_RETEST`, two `SIGNAL  BUY_A` lines with
entry, stop, T1, target, R:R and shares, then three `WICK_FILLED` lines at 10:25 ET,
and a status box at the end. Run it again without `--catalyst-ok` and every BUY turns
into `SETUP_SKIPPED ... blocked by: catalyst_checked`. That is the manual news check
doing its job.

**3. Replay your own data**

Export 1-minute or 5-minute bars (with extended hours) to CSV with columns
`time, open, high, low, close, volume`, then:

```bash
python -m wickfill replay path/to/OMH_5m.csv --symbol OMH --prev-close 3.10 --catalyst-ok
```

- `--prev-close` is the previous day's regular-session close. Needed only if the file
  does not include the previous day; without it the day-gain check says "unknown" and
  blocks signals rather than guessing.
- `--tz` sets the timezone for timestamps that have none (default US/Eastern).
- `--config config.yaml` uses your own settings (`cp config.example.yaml config.yaml`).

Other commands (`scan`, `watch`, `paper`, `report`, `backtest`) exist but only print
which phase they arrive in.

## Layout

```
wickfill/
  engine.py    THE strategy: wick detector, zones, state machine, signals, risk plan
  bars.py      CSV loading, UTC conversion, resampling, data-quality warnings
  replay.py    feeds bars through the engine; status box
  config.py    typed settings + YAML loader
  sample.py    synthetic sample data
  cli.py       command line
tests/         pytest suite
config.example.yaml
```

`engine.py` does no I/O and reads no clock. Replay uses it now; the scanner, paper
broker and backtester will call the same `SymbolEngine.on_bar()`.

## How the engine reads your spec (assumptions)

Please read these. Each is a place where the spec could be read more than one way.

1. **Times.** Everything is stored in UTC. Output shows US/Eastern and your local zone.
   Every printed time is the **start** of a 5-minute bar; the event is only known when
   that bar closes, five minutes later.
2. **Candle boundaries.** 15m and 60m candles sit on clock boundaries (12:00, 12:15, ...;
   12:00, 13:00, ...) and include pre-market and after-hours. Some charting platforms
   start 60m candles at 09:30 instead; if yours does, 60m levels will differ.
3. **Daily and weekly candles** use the regular session only (09:30-16:00 ET), Monday to
   Friday. On a half day the daily candle closes when the next bar arrives.
4. **History needed.** A candle only qualifies with at least 5 earlier candles of the same
   timeframe for the volume average. From 5m data alone that means 5 prior days for daily
   zones and 5 prior weeks for weekly zones. Phase 2 loads daily/weekly history from the
   data provider (`SymbolEngine.seed_candles`).
5. **Partial candles are skipped.** If the data starts mid-hour, that first 60m candle is
   ignored rather than treated as a real candle.
6. **Fill.** A zone is filled by the first later 5m bar whose high >= target. The wick
   candle itself never fills its own zone.
7. **Expiry** is in calendar days from the candle close.
8. **Reclaim** counts consecutive **5-minute closes** strictly above the level, for zones
   of every timeframe. A close exactly at the level counts as not above.
9. **AT_RETEST right after FRESH_RECLAIM.** The spec says a retest is "already confirmed,
   low <= level x 1.01, close > level". The bar right after a fresh reclaim often still has
   its low within 1% of the level, so it counts as a retest (BUY_A) without ever being
   EXTENDED first. This follows the spec literally. Tell me if you want a retest to
   require an EXTENDED bar first.
10. **Zone failure** = a confirmed reclaim (2 closes above) followed by a 5m close at or
    below the level, whether or not you took a trade. One failure blocks that zone.
11. **"Nearest active zone above price"** = among unfilled zones whose target is above
    price and within 200%, the one whose level is closest to the price. Ties go to the
    higher timeframe. Signals come only from that zone.
12. **Day gain** = price vs the previous regular-session close.
13. **Signal window.** The bar must start at or after 04:00 ET and close before 16:00 ET,
    so the 15:55 bar gives no signal (there would be no time to trade it).
14. **ATR14** is Wilder's ATR on 5m bars, including overnight gaps in true range.
15. **Dollar volume** = average of close x volume over the last 20 five-minute bars.
    The default minimum ($20,000 per bar) is my placeholder; set your own.
16. **BUY_B is off by default** (`strategy.enable_buy_b`).
17. **Engine vs broker limits.** The engine applies the strategy blocks and sizing caps.
    Max trades per day, the -3% daily stop, one position per symbol, the 60-bar time exit
    and flat-by-16:00 belong to the paper broker and backtester (Phases 3-4).

## Data rules

- UTC storage; the still-forming bar is dropped; minutes with no trades are never
  filled in; resampling only combines bars that exist.
- **Split-adjusted vs unadjusted prices.** Wick levels are raw prices. If a split or
  reverse split happens between the wick candle and now, the level and target are wrong.
  Use unadjusted intraday data and treat any zone that spans a split as invalid. Replay
  warns when price jumps 2x or more between consecutive bars.

## Data provider: what you must check (Phase 2)

You chose **Alpaca**. I cannot verify these for your account, so please confirm them:

- You have API keys for your **paper** account (`https://paper-api.alpaca.markets`).
  Keys will live in a `.env` file that is never committed or logged.
- **Which market-data plan you have.** The free plan gives real-time data from the IEX
  exchange only, which is a small share of small-cap volume and thin in pre-market, and
  it limits recent consolidated (SIP) data. Volume-based rules (2x volume, relative
  volume, dollar volume) will be distorted on IEX-only data. The paid plan gives full
  consolidated data. The bot will label the feed in every alert.
- Alpaca's terms for market-data use and display, and its API rate limits.
- Alpaca has no float data and its screener is limited, so float filters need another
  permitted source or stay manual.

## Safety

Paper mode only. No broker connection exists yet. When one is added it will require a
config flag, an environment variable and a typed confirmation for every single order.
