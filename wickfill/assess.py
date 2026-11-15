"""Assess a ticker on demand: halal screen, risk flags, news, wick-fill levels.

This produces a rule-based readout for you to weigh. It is not advice and it
never places orders.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import halal
from .bars import drop_forming
from .config import Config
from .engine import BASE, BASE_MINUTES, ET, REG_CLOSE, REG_OPEN, Snapshot, State, SymbolEngine, Zone
from .models import UTC, Bar, Candle
from .providers.alpaca import AlpacaData
from .providers.edgar import Edgar

_NEWS_TAGS: list[tuple[str, str]] = [
    ("dilution", r"offering|registered direct|private placement|at-the-market|atm (program|facility)|warrants?|"
                 r"convertible (notes?|debentures?)|shelf registration|pricing of"),
    ("reverse split", r"reverse (stock )?split|share consolidation"),
    ("listing risk", r"delist\w*|nasdaq (notice|deficiency|notification)|minimum bid|non-?compliance"),
    ("distress", r"bankruptcy|chapter 11|going concern|default(s|ed)?|restructuring"),
    ("catalyst", r"fda|approval|clinical|phase (1|2|3|i|ii|iii)\b|contract|partnership|agreement|award(ed)?|patent|"
                 r"merger|acqui\w+|earnings|results|guidance|launch\w*"),
]
_OFFERING_FORMS = {"S-1", "S-1/A", "S-1MEF", "S-3", "S-3/A", "S-3ASR", "F-1", "F-1/A", "F-3", "F-3/A",
                   "424B1", "424B2", "424B3", "424B4", "424B5"}
_ITEM_FLAGS = {
    "1.03": "bankruptcy or receivership",
    "2.04": "event that accelerates a debt obligation",
    "3.01": "notice of delisting or failure to meet a listing rule",
    "3.02": "unregistered sale of shares (dilution)",
    "4.02": "earlier financial statements should not be relied on",
    "5.03": "charter amendment (often a reverse split or share increase)",
}


@dataclass
class Assessment:
    symbol: str
    as_of: datetime
    name: str = ""
    feed: str = ""
    price: float | None = None
    price_ts: datetime | None = None  # close time of the latest completed 5m bar
    data_notes: list[str] = field(default_factory=list)
    business: halal.BusinessScreen | None = None
    financial: halal.FinancialScreen | None = None
    halal_verdict: str = halal.UNKNOWN
    risk_flags: list[str] = field(default_factory=list)
    news: list[dict[str, Any]] = field(default_factory=list)
    news_checked: bool = False
    snapshot: Snapshot | None = None
    zones: list[Zone] = field(default_factory=list)  # active, above price, nearest first
    stance: str = "NO DATA"
    stance_lines: list[str] = field(default_factory=list)
    holding_lines: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without any network)
# ---------------------------------------------------------------------------

def tag_headline(text: str) -> list[str]:
    low = text.lower()
    return [tag for tag, pattern in _NEWS_TAGS if re.search(rf"\b(?:{pattern})", low)]


def daily_to_candles(daily: list[Bar], before: date) -> tuple[list[Candle], list[Candle]]:
    """Turn provider daily bars into (daily candles, weekly candles) for
    sessions strictly before `before`. Weekly candles are Monday-Friday."""
    days: list[Candle] = []
    for b in daily:
        d = b.ts.astimezone(ET).date()
        if b.ts.astimezone(ET).hour >= 20:  # some feeds stamp daily bars late the evening before
            d += timedelta(days=1)
        if d >= before:
            continue
        days.append(Candle("1d", datetime.combine(d, REG_OPEN, ET).astimezone(UTC),
                           datetime.combine(d, REG_CLOSE, ET).astimezone(UTC),
                           b.open, b.high, b.low, b.close, b.volume))
    weeks: list[Candle] = []
    group: list[Candle] = []

    def flush() -> None:
        if group:
            monday = group[0].start.astimezone(ET).date()
            monday -= timedelta(days=monday.weekday())
            weeks.append(Candle(
                "1w", datetime.combine(monday, REG_OPEN, ET).astimezone(UTC),
                datetime.combine(monday + timedelta(days=4), REG_CLOSE, ET).astimezone(UTC),
                group[0].open, max(c.high for c in group), min(c.low for c in group),
                group[-1].close, sum(c.volume for c in group)))
            group.clear()

    def week_of(c: Candle) -> date:
        d = c.start.astimezone(ET).date()
        return d - timedelta(days=d.weekday())

    for c in days:
        if group and week_of(c) != week_of(group[0]):
            flush()
        group.append(c)
    flush()
    return days, weeks


def intraday_start(now: datetime) -> datetime:
    """Midnight ET on the Monday of last week: 5m history starts on a week
    boundary so daily and weekly candles line up with the seeded history."""
    d = now.astimezone(ET).date()
    monday = d - timedelta(days=d.weekday() + 7)
    return datetime.combine(monday, time(0, 0), ET).astimezone(UTC)


def run_engine(symbol: str, cfg: Config, daily: list[Bar], five: list[Bar], now: datetime,
               catalyst_checked: bool) -> tuple[Snapshot | None, list[Zone]]:
    engine = SymbolEngine(symbol, cfg)
    engine.catalyst_checked = catalyst_checked
    days, weeks = daily_to_candles(daily, intraday_start(now).astimezone(ET).date())
    engine.seed_candles("1w", weeks)
    engine.seed_candles("1d", days)
    if days:
        engine.prev_close = days[-1].close
    snap: Snapshot | None = None
    for bar in drop_forming(five, now, BASE_MINUTES):
        snap = engine.on_bar(bar).snapshot
    if snap is None:
        return None, []
    limit = snap.price * (1 + cfg.strategy.max_target_above_price)
    zones = [z for z in engine.zones if z.status == "ACTIVE" and snap.price < z.target <= limit]
    zones.sort(key=lambda z: abs(z.level - snap.price))
    return snap, zones


def filing_flags(submissions: dict[str, Any], today: date, days: int) -> list[str]:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms, dates = recent.get("form") or [], recent.get("filingDate") or []
    items = recent.get("items") or [""] * len(forms)
    cutoff = today - timedelta(days=days)
    offering: list[str] = []
    late: list[str] = []
    item_hits: dict[str, str] = {}
    foreign = False
    for form, filed, item in zip(forms, dates, items):
        foreign = foreign or form in ("20-F", "6-K", "40-F") or form.startswith("F-")
        if date.fromisoformat(filed) < cutoff:
            continue
        if form in _OFFERING_FORMS:
            offering.append(f"{form} on {filed}")
        elif form.startswith("NT "):
            late.append(f"{form} on {filed}")
        elif form.startswith("8-K"):
            for code in str(item or "").split(","):
                if code.strip() in _ITEM_FLAGS:
                    item_hits.setdefault(code.strip(), filed)
    flags: list[str] = []
    if offering:
        more = f" (+{len(offering) - 4} more)" if len(offering) > 4 else ""
        flags.append("DILUTION RISK: offering/shelf filings in the last "
                     f"{days} days: {', '.join(offering[:4])}{more}. The company can sell shares into a spike.")
    for code, filed in sorted(item_hits.items()):
        flags.append(f"8-K item {code} on {filed}: {_ITEM_FLAGS[code]}.")
    if late:
        flags.append(f"Late filing notice: {', '.join(late[:3])}.")
    if foreign:
        flags.append("Foreign issuer (files 20-F/6-K): lighter disclosure rules; dilution can appear with little notice.")
    return flags


def market_flags(snap: Snapshot | None, five: list[Bar], cfg: Config) -> list[str]:
    flags: list[str] = []
    if snap is None:
        return flags
    if snap.price < 1.0:
        flags.append("Price under $1: exchange minimum-bid rules apply, so reverse splits and delisting notices are common.")
    last_day = five[-1].ts.astimezone(ET).date()
    today = [b for b in five if b.ts.astimezone(ET).date() == last_day]
    hi, lo = max(b.high for b in today), min(b.low for b in today)
    if lo > 0 and hi / lo - 1 >= 0.5:
        flags.append(f"Extreme range on {last_day}: low {lo:.4f} to high {hi:.4f} ({(hi / lo - 1) * 100:.0f}%). "
                     "Moves this large can reverse just as fast, and halts are likely.")
    recent = five[-cfg.strategy.dollar_volume_lookback:]
    adv = sum(b.close * b.volume for b in recent) / len(recent)
    if adv < cfg.strategy.min_avg_dollar_volume:
        flags.append(f"Thin trading: about ${adv:,.0f} per 5-minute bar on this feed. Wide spreads and slippage likely.")
    if snap.day_gain is not None and snap.day_gain >= 1.0:
        flags.append(f"Already up {snap.day_gain * 100:.0f}% on the day: late entries carry the most fade risk.")
    return flags


def decide(a: Assessment, cfg: Config) -> None:
    """Fill in stance, stance_lines and holding_lines from the rest of `a`."""
    snap = a.snapshot
    if snap is None:
        a.stance = "NO DATA"
        a.stance_lines = ["No completed 5-minute bars came back, so nothing can be said about levels."]
        return
    z, plan = snap.zone, snap.plan
    blocks = [c for c in snap.checks if not c.ok]
    hard = [c for c in blocks if c.name != "catalyst_checked"]
    if z is None:
        a.stance = "NO SETUP"
        a.stance_lines = ["No unfilled wick zone above the current price within range. "
                          "This strategy has no level or target here."]
        a.holding_lines = ["If you hold it: there is no wick-fill target left above price, so this strategy "
                           "gives no reason to keep holding. Use your own exit plan."]
    else:
        lv, tg = f"{z.level:.4f}", f"{z.target:.4f}"
        if snap.state == State.BELOW_LEVEL:
            a.stance = "WAIT"
            a.stance_lines = [f"Price is below the level. Wait for {cfg.strategy.reclaim_closes} five-minute "
                              f"closes above {lv}, then a retest that holds. Target would be {tg}."]
        elif snap.state == State.RECLAIMING:
            a.stance = "WAIT"
            a.stance_lines = [f"Reclaim in progress: {z.closes_above} of {cfg.strategy.reclaim_closes} closes "
                              f"above {lv}. Not confirmed yet."]
        elif snap.state == State.FRESH_RECLAIM:
            a.stance = "WAIT (early)"
            a.stance_lines = [f"Just reclaimed {lv}. The preferred entry is a later pullback toward "
                              f"{lv}-{z.level * (1 + cfg.strategy.retest_tolerance):.4f} that closes back above it."]
        elif snap.state == State.EXTENDED:
            a.stance = "WAIT"
            a.stance_lines = [f"Above the level and extended. Wait for a pullback whose low reaches "
                              f"{z.level * (1 + cfg.strategy.retest_tolerance):.4f} or lower and which closes "
                              f"above {lv}. Do not chase."]
        elif snap.state == State.AT_RETEST:
            if not hard:
                a.stance = "BUY SETUP PRESENT" if not blocks else "BUY SETUP PRESENT (pending your news check)"
                a.stance_lines = [f"Retest of {lv} is holding and every rule check passes."]
            else:
                a.stance = "WAIT (setup blocked)"
                a.stance_lines = [f"Retest of {lv} is holding, but these rule checks fail:"]
        if hard and snap.state != State.AT_RETEST:
            a.stance_lines.append("Even on a valid retest, these rule checks currently fail:")
        a.stance_lines += [f"  - {c.name}: {c.detail}" for c in hard]
        if plan is not None:
            a.holding_lines = [
                f"If you hold it: the strategy's stop is {plan.stop:.4f}. A 5-minute close at or below "
                f"{lv} means the zone failed.",
                f"Take-profit levels: T1 {plan.t1:.4f} (halfway), final {tg} (the wick high).",
            ]
    if a.risk_flags and a.stance.startswith("BUY SETUP"):
        a.stance += f", {len(a.risk_flags)} risk flag(s) below"
    if a.halal_verdict == halal.FAIL:
        a.stance = "AVOID (fails halal screen)"
    elif a.halal_verdict in (halal.DOUBTFUL, halal.UNKNOWN):
        a.stance += f" | halal: {a.halal_verdict}, verify first"


# ---------------------------------------------------------------------------
# Orchestration (network)
# ---------------------------------------------------------------------------

def _is_market_hours(now: datetime) -> bool:
    et = now.astimezone(ET)
    return et.weekday() < 5 and time(4, 0) <= et.time() < time(20, 0)


def assess_symbol(symbol: str, cfg: Config, alpaca: AlpacaData, edgar: Edgar | None,
                  now: datetime, catalyst_checked: bool = False) -> Assessment:
    symbol = symbol.upper().strip().lstrip("$")
    a = Assessment(symbol, now, feed=alpaca.feed)
    if alpaca.feed == "iex":
        a.data_notes.append("Feed is IEX only (one exchange). Prices can miss the true high/low and volume is "
                            "a small fraction of the real total, so volume-based rules are unreliable.")

    # --- prices and levels ---
    five: list[Bar] = []
    try:
        daily = alpaca.bars(symbol, "1Day", now - timedelta(days=cfg.data.daily_history_days), now)
        five = alpaca.bars(symbol, "5Min", intraday_start(now), now)
        a.snapshot, a.zones = run_engine(symbol, cfg, daily, five, now, catalyst_checked)
        five = drop_forming(five, now, BASE_MINUTES)
    except Exception as exc:  # noqa: BLE001 - report, never guess
        a.data_notes.append(f"Price data unavailable: {exc}")
    if a.snapshot is not None:
        a.price = a.snapshot.price
        a.price_ts = a.snapshot.bar_ts + BASE
        age = (now - a.price_ts).total_seconds() / 60
        if _is_market_hours(now) and age > cfg.data.stale_after_minutes:
            a.data_notes.append(f"STALE: the latest bar is {age:.0f} minutes old (no trades on this feed, a halt, "
                                "or delayed data). Do not act on these levels without a live quote.")
        a.risk_flags += market_flags(a.snapshot, five, cfg)

    # --- company, filings, halal ---
    sic: int | None = None
    sic_desc = ""
    facts = None
    if edgar is None:
        a.data_notes.append("SEC lookup skipped (SEC_USER_AGENT not set): no halal screen or filing flags.")
    else:
        try:
            hit = edgar.lookup(symbol)
            if hit is None:
                a.data_notes.append("Ticker not found at the SEC: no industry code, filings or financials.")
            else:
                cik, a.name = hit
                sub = edgar.submissions(cik)
                a.name = sub.get("name") or a.name
                sic = int(sub["sic"]) if str(sub.get("sic") or "").isdigit() else None
                sic_desc = sub.get("sicDescription") or ""
                a.risk_flags += filing_flags(sub, now.astimezone(ET).date(), cfg.data.filings_days)
                try:
                    facts = edgar.company_facts(cik)
                except Exception as exc:  # noqa: BLE001
                    a.data_notes.append(f"SEC financial data unavailable: {exc}")
        except Exception as exc:  # noqa: BLE001
            a.data_notes.append(f"SEC data unavailable: {exc}")
    a.business = halal.screen_business(a.name, sic, sic_desc)
    a.financial = halal.screen_financials(facts, a.price, cfg.halal.max_debt_ratio, cfg.halal.max_cash_ratio,
                                          cfg.halal.max_impermissible_income)
    a.halal_verdict = halal.overall(a.business, a.financial)

    # --- news ---
    try:
        a.news = alpaca.news(symbol, now - timedelta(days=cfg.data.news_days))
        a.news_checked = True
        for n in a.news:
            n["tags"] = tag_headline(f"{n['headline']} {n['summary']}")
        seen = {t for n in a.news for t in n["tags"]}
        for tag, text in (("dilution", "Recent headlines mention an offering, warrants or convertible notes."),
                          ("reverse split", "Recent headlines mention a reverse split."),
                          ("listing risk", "Recent headlines mention a delisting or exchange compliance notice."),
                          ("distress", "Recent headlines mention bankruptcy, default or going-concern doubt.")):
            if tag in seen:
                a.risk_flags.append(f"NEWS: {text}")
        if not a.news:
            a.risk_flags.append(f"No headlines in the last {cfg.data.news_days} days from this source. "
                                "A big move with no news is a risk in itself.")
    except Exception as exc:  # noqa: BLE001
        a.data_notes.append(f"News unavailable: {exc}")

    decide(a, cfg)
    return a


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _money(x: float | None) -> str:
    if x is None:
        return "n/a"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"${x / div:,.1f}{suffix}"
    return f"${x:,.0f}"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _when(ts: datetime, local_tz: str) -> str:
    return f"{ts.astimezone(ET):%b %d %H:%M} ET / {ts.astimezone(ZoneInfo(local_tz)):%H:%M %Z}"


def format_report(a: Assessment, cfg: Config) -> str:
    tz = cfg.display.local_tz
    L: list[str] = []
    title = f"{a.symbol}  {a.name}".rstrip()
    L += ["=" * 78, title, "=" * 78]
    if a.price is not None and a.price_ts is not None:
        gain = f"   day {a.snapshot.day_gain * 100:+.1f}%" if a.snapshot and a.snapshot.day_gain is not None else ""
        L.append(f"Price {a.price:.4f} as of {_when(a.price_ts, tz)}   feed: {a.feed.upper()}{gain}")
    L.append(f"READOUT: {a.stance}")
    L += [f"  {line}" for line in a.stance_lines]
    for note in a.data_notes:
        L.append(f"  ! {note}")

    L += ["", "HALAL SCREEN (automated first pass, not a ruling): " + a.halal_verdict]
    b, f = a.business, a.financial
    if b is not None:
        ind = f"SIC {b.sic} {b.sic_description}" if b.sic is not None else "industry code unknown"
        L.append(f"  Business: {b.verdict}  ({ind})")
        L += [f"    - {x}" for x in b.findings]
        asked = [("defense", "Defense/weapons"), ("gambling", "Gambling"), ("alcohol", "Alcohol")]
        L.append("    " + "   ".join(
            f"{label}: {'FLAGGED' if key in b.categories else ('not flagged' if b.sic is not None else 'unknown')}"
            for key, label in asked))
    if f is not None:
        L.append(f"  Financial ratios: {f.verdict}" + (f"  (balance sheet {f.as_of})" if f.as_of else ""))
        if f.as_of:
            L.append(f"    Debt {_money(f.debt)} / market cap {_money(f.market_cap)} = {_pct(f.debt_ratio)} "
                     f"(limit {_pct(cfg.halal.max_debt_ratio)}); debt/total assets {_pct(f.debt_to_assets)}")
            L.append(f"    Cash+securities {_money(f.cash_and_securities)} / market cap = {_pct(f.cash_ratio)} "
                     f"(limit {_pct(cfg.halal.max_cash_ratio)})")
            L.append(f"    Interest income {_money(f.interest_income)} / revenue {_money(f.revenue)} = "
                     f"{_pct(f.income_ratio)} (limit {_pct(cfg.halal.max_impermissible_income)})")
        L += [f"    - {n}" for n in f.notes]

    L += ["", f"RISK FLAGS ({len(a.risk_flags)})"]
    L += [f"  - {x}" for x in a.risk_flags] or ["  none found by these checks (that is not the same as no risk)"]

    L += ["", "LEVELS (wick-fill rules)"]
    snap = a.snapshot
    if snap is None:
        L.append("  no price data")
    elif not a.zones:
        L.append("  no unfilled wick zones above price")
    else:
        L.append(f"  {'TF':<4} {'Level':>9} {'Target':>9} {'Upside':>7}  {'State':<14} {'Failed':>6}  Wick candle")
        for z in a.zones[:6]:
            mark = "*" if snap.zone is not None and z.id == snap.zone.id else " "
            L.append(f" {mark}{z.tf:<4} {z.level:>9.4f} {z.target:>9.4f} {(z.target / snap.price - 1) * 100:>6.0f}%  "
                     f"{z.state.value:<14} {z.failed_attempts:>6}  {_when(z.candle_start, tz)}")
        L.append("  * = nearest zone, used for the plan below. Upside is from the current price to the target.")
        if snap.plan is not None and snap.zone is not None:
            p = snap.plan
            cap = f", capped by {p.capped_by}" if p.capped_by else ""
            L.append(f"  Plan if entered at {p.entry:.4f}: stop {p.stop:.4f}, T1 {p.t1:.4f}, target {p.target:.4f}, "
                     f"R:R {p.rr:.2f}, size {p.shares} shares (risking {_money(p.shares * p.risk_per_share)}{cap})")
        L.append("  Rule checks: " + "  ".join(
            f"[{'x' if c.ok else ' '}] {c.name}" for c in snap.checks if c.name != "not_halted"))
        L.append("  Halts are NOT detected by this tool. Check your broker for a halt before acting.")
    L += [f"  {line}" for line in a.holding_lines]

    L += ["", f"NEWS (last {cfg.data.news_days} days)"]
    if not a.news_checked:
        L.append("  not available")
    elif not a.news:
        L.append("  no headlines found")
    for n in a.news[:8]:
        when = _when(n["created_at"], tz) if n.get("created_at") else "?"
        tags = f" [{', '.join(n['tags'])}]" if n.get("tags") else ""
        L.append(f"  {when}{tags}  {n['headline']}")
        if n.get("url"):
            L.append(f"      {n['url']}")
    L += ["", "Read the news and the filings yourself before acting. Educational tool, not financial advice."]
    return "\n".join(L)
