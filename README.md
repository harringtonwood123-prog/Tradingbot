# Wick Fill assistant

A long-only small-cap **scanner + Discord alert + paper-trading** tool for the "Wick Fill"
strategy. It runs on your laptop.

> **Education only. Not financial advice.** Nothing here predicts or promises profit.
> Small-cap day trading can lose money quickly. Check the news and filings before acting.

## What it does today

| Command | What | State |
|---|---|---|
| `assess` | You give it tickers from your own screener. It returns a halal screen, risk flags, recent news and the wick-fill levels to wait for. | built, **not yet run against the live APIs** (see below) |
| `replay` | Runs a CSV of bars through the strategy engine | built and tested |
| `scan`, `watch`, `paper`, `report`, `backtest` | Alerts, paper trading, backtesting | not built |

There is **no order placement of any kind** in the code. It does not send alerts or signals;
it answers when you ask.

## Setup (macOS)

You need Python 3.11 or newer. Check with `python3 --version`.

```bash
cd Tradingbot
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Each time you open a new Terminal window, run `source .venv/bin/activate` again first.

## Assess tickers

**1. Add your keys (once)**

```bash
cp .env.example .env
```

Open `.env` in a text editor and fill in:

- `ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY`: from your Alpaca dashboard (paper keys are fine).
- `SEC_USER_AGENT`: your name and email. The SEC requires this from automated tools.

`.env` is ignored by git. Never paste it into a chat.

**2. Run it**

The simplest way: start it, then type tickers whenever you want one assessed.

```bash
python -m wickfill
```

At the `tickers>` prompt type one or more symbols (for example `OMH MERC`) and press
Enter. Type `q` to quit. It uses `config.yaml` automatically if that file exists.

Or pass the tickers on the command line:

```bash
python -m wickfill assess OMH MERC PLCE
```

Or keep tickers in a file (one per line) and save the report:

```bash
python -m wickfill assess --file inbox.txt --out report.txt
```

Add `--catalyst-ok` only after you have read the news and filings yourself.

**3. What you get for each ticker**

- **READOUT**: one line. `BUY SETUP PRESENT`, `WAIT` (with the exact level and what must
  happen there), `NO SETUP`, `AVOID (fails halal screen)` or `NO DATA`.
- **HALAL SCREEN**: PASS / FAIL / DOUBTFUL / UNKNOWN, with defense, gambling and alcohol
  called out by name, plus the debt, cash and interest-income ratios.
- **RISK FLAGS**: offering and shelf filings, delisting notices, reverse-split signs,
  sub-$1 price, extreme range, thin trading, no-news moves.
- **LEVELS**: every unfilled wick zone above price (timeframe, level, target, state), the
  stop, T1, target, R:R and share size, and exit levels if you already hold it.
- **NEWS**: recent headlines with links, tagged dilution / reverse split / catalyst.

**Not yet proven:** the environment this was built in could not reach Alpaca or the SEC,
so `assess` has only been tested against simulated responses. The first real run is on
your laptop. If a request fails, the report says which part is unavailable rather than
guessing; send me the message and I will fix it.

### How the halal screen works, and its limits

It is an automated first pass, **not a religious ruling**.

- **Business activity**: the company's SEC industry code (SIC) and its name are checked
  for alcohol, tobacco, gambling, weapons and defense, conventional banking and insurance,
  pork, cannabis and adult content. Clear cases FAIL. Mixed industries (aerospace, hotels,
  restaurants, grocery, media, generic "gaming") are DOUBTFUL.
- **Financial ratios** (AAOIFI-style, from the company's own SEC filings): interest-bearing
  debt / market cap <= 30%, cash and interest-bearing securities / market cap <= 30%,
  interest income / revenue <= 5%. Thresholds are in `config.yaml` under `halal`.
- **What it cannot see**: revenue by business line. A company with a small haram segment
  can pass, and a defense contractor filed under a neutral code can be missed.
- **Often UNKNOWN for small caps**: foreign companies that report in another currency,
  and companies with no revenue, cannot be fully checked. UNKNOWN means not verified.
- Market cap uses the last reported share count, which is often stale for diluting
  small caps. Debt is the sum of tagged debt items and excludes leases.
- Screening providers and scholars use different thresholds and can disagree. Confirm
  with a certified screener or a scholar you trust.

### Other limits

- **Halts are not detected.** Check your broker.
- **Float and short interest** are not available from these sources.
- **IEX feed** (Alpaca free plan) sees one exchange only. Highs, lows and volume are
  incomplete, so wick levels and volume rules can be wrong. Set `data.feed: "sip"` if you
  have the paid plan.
- Wick history: daily and weekly zones come from about 400 days of daily bars; intraday
  zones from the last one to two weeks of 5-minute bars.

### Where the rules come from

The strategy is the "wick fill" idea Mini_Tradez posts about: a candle that closes far
below its high leaves a wick; if price breaks and reclaims that candle's close, the wick
high becomes the target. His X, YouTube and Whop pages could not be opened from the build
environment, so the exact rules here come from your written spec and your log of his
calls, not from his videos. In that log, 3 of the 7 calls with a checkable target reached
it, and several names fell 40-80% the same day.

## TradingView indicator

`tradingview/wick_fill.pine` draws the same wick zones on your chart, with a status box
(WAIT / BUY A / BUY B, level, target, stop, T1, R:R, shares, failed attempts, checklist).

1. Open the file on GitHub, click **Raw**, select all and copy.
2. In TradingView open a **5-minute** chart with **extended hours on**.
3. Open the **Pine Editor** tab at the bottom, delete what is there, paste, click
   **Add to chart**.
4. Click the gear icon on the indicator to set your equity and risk, and tick
   "I checked the news and filings" per ticker once you have.

Limits: the script was written without access to TradingView's compiler, so it may need a
fix on first paste (send me the red error line). It only finds zones inside the chart
history TradingView has loaded, so on a 5-minute chart old daily and weekly wicks will be
missing; check the 1-hour or daily chart for those. It has no halal screen, news or filing
checks; use `assess` for those. Its numbers can differ slightly from `assess` because the
data source differs.

## Replay and tests

**1. Run the tests**

```bash
pytest -q
```

Expect `63 passed`. That means the wick detector, state machine, signal checks, risk maths
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
  assess.py    per-ticker assessment and report
  halal.py     business-activity and financial-ratio screen
  providers/   alpaca.py (bars, news), edgar.py (SEC filings and financials)
  http.py      small HTTPS helper
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
   zones and 5 prior weeks for weekly zones. `assess` loads daily/weekly history from the
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

## Data sources: what you must check

Only official APIs are used. Nothing is scraped. I cannot verify these for your account:

- **Alpaca**: which market-data plan you have (free = IEX only), that your keys allow the
  news endpoint, and Alpaca's terms for data use and rate limits.
- **SEC EDGAR**: free and public; requires the identifying User-Agent and at most 10
  requests per second (the tool makes 3 per ticker).

## Safety

Read-only. The Alpaca module only downloads bars and news; no broker connection exists. When one is added it will require a
config flag, an environment variable and a typed confirmation for every single order.
