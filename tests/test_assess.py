from __future__ import annotations

from datetime import datetime, timedelta

from helpers import bar, et
from wickfill import halal
from wickfill.assess import (
    Assessment, assess_symbol, daily_to_candles, decide, filing_flags, format_report, intraday_start, tag_headline,
)
from wickfill.cli import load_env, main
from wickfill.config import Config
from wickfill.models import UTC
from wickfill.providers.alpaca import AlpacaData
from wickfill.providers.edgar import Edgar


# ---------------- halal: business ----------------

def test_business_screen_flags_what_was_asked_for():
    assert halal.screen_business("Smith & Wesson Brands", 3484, "Ordnance & Accessories").verdict == halal.FAIL
    b = halal.screen_business("Acme Defense Systems Inc", 3812, "Search, Detection, Navigation")
    assert b.verdict == halal.FAIL and "defense" in b.categories  # name keyword upgrades the doubtful SIC
    assert "gambling" in halal.screen_business("Lucky Casino Corp", 7990, "Misc Amusement").categories
    assert halal.screen_business("Molson Brewing Co", 2082, "Malt Beverages").verdict == halal.FAIL
    assert halal.screen_business("First Community Bancorp", 6022, "State Commercial Banks").verdict == halal.FAIL


def test_business_screen_pass_doubtful_unknown():
    ok = halal.screen_business("Widget Software Inc", 7372, "Prepackaged Software")
    assert ok.verdict == halal.PASS and ok.categories == []
    assert halal.screen_business("Skyline Aerospace", 3728, "Aircraft Parts").verdict == halal.DOUBTFUL
    assert halal.screen_business("Mystery Co", None, "").verdict == halal.UNKNOWN
    # whole-word matching: "Hamilton" is not pork, "Winston" is not wine
    assert halal.screen_business("Hamilton Winston Devices", 3670, "Electronic Components").verdict == halal.PASS


# ---------------- halal: financial ----------------

def _facts(debt=10e6, cash=5e6, assets=100e6, shares=50e6, revenue=40e6, interest=1e6, extra=None):
    def inst(v):
        return {"units": {"USD": [{"end": "2026-06-30", "val": v}, {"end": "2025-12-31", "val": v * 9}]}}

    def annual(v):
        return {"units": {"USD": [{"start": "2025-01-01", "end": "2025-12-31", "val": v},
                                  {"start": "2025-10-01", "end": "2025-12-31", "val": v / 4}]}}
    gaap = {"Assets": inst(assets), "LongTermDebt": inst(debt), "CashAndCashEquivalentsAtCarryingValue": inst(cash),
            "Revenues": annual(revenue), "InvestmentIncomeInterest": annual(interest)}
    gaap.update(extra or {})
    return {"facts": {"us-gaap": gaap, "dei": {"EntityCommonStockSharesOutstanding": {
        "units": {"shares": [{"end": "2026-08-01", "val": shares}]}}}}}


def test_financial_ratios_pass():
    f = halal.screen_financials(_facts(), price=2.0)  # market cap 100M
    assert f.market_cap == 100e6
    assert f.debt_ratio == 0.10 and f.cash_ratio == 0.05 and f.income_ratio == 0.025
    assert f.debt_to_assets == 0.10 and f.verdict == halal.PASS


def test_financial_ratios_fail_each_way():
    assert halal.screen_financials(_facts(debt=31e6), 2.0).verdict == halal.FAIL
    assert halal.screen_financials(_facts(cash=31e6), 2.0).verdict == halal.FAIL
    assert halal.screen_financials(_facts(interest=2.1e6), 2.0).verdict == halal.FAIL
    # a falling share price shrinks market cap and can flip the result
    assert halal.screen_financials(_facts(), 0.5).verdict == halal.FAIL


def test_financial_unknown_when_data_missing():
    assert halal.screen_financials(None, 2.0).verdict == halal.UNKNOWN
    assert halal.screen_financials(_facts(), None).verdict == halal.UNKNOWN
    pre_revenue = halal.screen_financials(_facts(revenue=0), 2.0)
    assert pre_revenue.verdict == halal.UNKNOWN and pre_revenue.income_ratio is None
    assert halal.screen_financials({"facts": {"ifrs-full": {}}}, 2.0).verdict == halal.UNKNOWN


def test_debt_sums_separate_buckets_and_ignores_stale_tags():
    conv = {"ConvertibleNotesPayable": {"units": {"USD": [{"end": "2026-06-30", "val": 4e6}]}},
            "ShortTermBorrowings": {"units": {"USD": [{"end": "2023-12-31", "val": 99e6}]}}}  # stale: ignored
    f = halal.screen_financials(_facts(extra=conv), 2.0)
    assert f.debt == 14e6


def test_overall_takes_the_worst():
    b_ok = halal.screen_business("Widget Software", 7372, "Software")
    assert halal.overall(b_ok, halal.screen_financials(_facts(), 2.0)) == halal.PASS
    assert halal.overall(b_ok, halal.screen_financials(None, 2.0)) == halal.UNKNOWN
    b_bad = halal.screen_business("Lucky Casino", 7990, "")
    assert halal.overall(b_bad, halal.screen_financials(_facts(), 2.0)) == halal.FAIL


# ---------------- news and filings ----------------

def test_tag_headline():
    assert tag_headline("Acme Announces Pricing of $5 Million Registered Direct Offering") == ["dilution"]
    assert "reverse split" in tag_headline("Acme to effect 1-for-20 reverse stock split")
    assert "listing risk" in tag_headline("Acme receives Nasdaq notice regarding minimum bid price")
    assert tag_headline("Acme wins FDA approval") == ["catalyst"]
    assert tag_headline("Acme CEO to speak at conference") == []


def test_filing_flags():
    sub = {"filings": {"recent": {
        "form": ["424B5", "8-K", "S-3", "NT 10-Q", "10-Q", "S-1"],
        "filingDate": ["2026-11-03", "2026-10-20", "2026-09-01", "2026-08-15", "2026-08-01", "2025-01-01"],
        "items": ["", "3.01,9.01", "", "", "", ""]}}}
    flags = filing_flags(sub, datetime(2026, 11, 15).date(), 180)
    text = " | ".join(flags)
    assert "424B5 on 2026-11-03" in text and "S-3 on 2026-09-01" in text
    assert "S-1 on 2025" not in text  # too old
    assert "item 3.01" in text and "delisting" in text and "NT 10-Q" in text
    assert filing_flags({"filings": {"recent": {"form": ["6-K"], "filingDate": ["2020-01-01"]}}},
                        datetime(2026, 11, 15).date(), 180)[0].startswith("Foreign issuer")
    assert filing_flags({}, datetime(2026, 11, 15).date(), 180) == []


# ---------------- history shaping ----------------

def test_intraday_start_is_monday_of_last_week():
    now = et(2026, 11, 12, 10, 0)  # Thursday
    assert intraday_start(now) == et(2026, 11, 2, 0, 0)


def test_daily_to_candles_builds_weeks_and_respects_cutoff():
    days = [bar(et(2026, 10, d, 0, 0), 2, 2 + d / 10, 1, 2, 100) for d in (19, 20, 21, 22, 23, 26, 27)]
    days.append(bar(et(2026, 11, 2, 0, 0), 2, 9, 1, 2, 100))  # on/after the cutoff: excluded
    d, w = daily_to_candles(days, et(2026, 11, 2, 0, 0).date())
    assert len(d) == 7 and len(w) == 2
    assert d[0].start == et(2026, 10, 19, 9, 30) and d[0].end == et(2026, 10, 19, 16, 0)
    assert w[0].start == et(2026, 10, 19, 9, 30) and w[0].end == et(2026, 10, 23, 16, 0)
    assert w[0].high == 2 + 23 / 10 and w[0].volume == 500 and w[1].volume == 200


# ---------------- end to end with fake HTTP ----------------

NOW = et(2026, 11, 12, 10, 2)  # Thursday morning


def _iso(ts):
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fake_fetch(calls, sic="7372", name="Widget Software Inc"):
    def daily_rows():
        rows, d = [], et(2025, 11, 3, 0, 0)
        while d < et(2026, 11, 12, 0, 0):
            if d.weekday() < 5:
                spike = d.date() == et(2026, 10, 20, 0, 0).date()  # daily wick: close 2.0, high 3.2
                rows.append({"t": _iso(d), "o": 1.6, "h": 3.2 if spike else 1.65, "l": 1.55,
                             "c": 2.0 if spike else 1.6, "v": 9_000_000 if spike else 500_000})
            d += timedelta(days=1)
        return rows

    def five_rows():
        rows, t = [], et(2026, 11, 12, 7, 0)
        closes = [1.9] * 28 + [1.95, 2.04, 2.08, 2.12, 2.16, 2.12, 2.06]  # reclaim 2.00, extend, retest
        for i, c in enumerate(closes):
            lo = 2.01 if i == len(closes) - 1 else c - 0.02
            rows.append({"t": _iso(t + timedelta(minutes=5 * i)), "o": c, "h": c + 0.02, "l": lo, "c": c, "v": 80_000})
        rows.append({"t": _iso(et(2026, 11, 12, 10, 0)), "o": 2.06, "h": 9.0, "l": 2.0, "c": 8.0, "v": 1})  # forming
        return rows

    def fetch(url, params=None, headers=None):
        calls.append((url, params, headers))
        if url.endswith("/bars"):
            return {"bars": daily_rows() if params["timeframe"] == "1Day" else five_rows(), "next_page_token": None}
        if url.endswith("/news"):
            return {"news": [{"headline": "Widget announces pricing of $4M registered direct offering",
                              "summary": "", "source": "benzinga", "url": "https://example.com/n1",
                              "created_at": "2026-11-12T12:05:00Z"}]}
        if "company_tickers" in url:
            return {"0": {"cik_str": 1234, "ticker": "WDGT", "title": name}}
        if "/submissions/" in url:
            return {"name": name, "sic": sic, "sicDescription": "Services-Prepackaged Software",
                    "filings": {"recent": {"form": ["424B5"], "filingDate": ["2026-11-10"], "items": [""]}}}
        if "/companyfacts/" in url:
            return _facts()
        raise AssertionError(url)
    return fetch


def _run(tmp_path, **kw):
    calls = []
    fetch = _fake_fetch(calls, **kw)
    alpaca = AlpacaData("KEYID", "SECRET", fetch=fetch)
    edgar = Edgar("Test User test@example.com", cache_dir=tmp_path, fetch=fetch)
    a = assess_symbol("$wdgt", Config(), alpaca, edgar, NOW, catalyst_checked=True)
    return a, calls


def test_assess_end_to_end(tmp_path):
    a, calls = _run(tmp_path)
    assert a.symbol == "WDGT" and a.name == "Widget Software Inc"
    assert a.price == 2.06  # the forming 10:00 bar (close 8.0) was dropped
    assert a.price_ts == et(2026, 11, 12, 9, 55)
    z = a.snapshot.zone
    assert (z.tf, z.level, z.target) == ("1d", 2.0, 3.2)  # seeded daily wick from Oct 20
    assert a.snapshot.state.value == "AT_RETEST"
    assert a.snapshot.day_gain is not None and round(a.snapshot.day_gain, 3) == 0.287  # vs 1.60 prior close
    assert a.halal_verdict == halal.PASS
    assert a.stance == "BUY SETUP PRESENT, 2 risk flag(s) below"
    assert any("DILUTION RISK" in f and "424B5" in f for f in a.risk_flags)
    assert any(f.startswith("NEWS:") and "offering" in f for f in a.risk_flags)
    report = format_report(a, Config())
    for needle in ("READOUT: BUY SETUP PRESENT", "HALAL SCREEN", "Defense/weapons: not flagged", "Gambling: not flagged",
                   "Alcohol: not flagged", "feed: IEX", "IEX only", "[dilution]", "stop ", "T1 ", "CST",
                   "not financial advice", "Halts are NOT detected"):
        assert needle in report, needle
    # keys go in headers only, never in a URL or the report
    assert all("SECRET" not in url and "SECRET" not in str(params) for url, params, _ in calls)
    assert "SECRET" not in report and "KEYID" not in report
    bars_call = next(p for u, p, _ in calls if u.endswith("/bars"))
    assert bars_call["adjustment"] == "raw" and bars_call["feed"] == "iex"


def test_assess_haram_business_overrides_setup(tmp_path):
    a, _ = _run(tmp_path, sic="7990", name="Lucky Casino Corp")
    assert a.halal_verdict == halal.FAIL and a.stance == "AVOID (fails halal screen)"
    assert "Gambling: FLAGGED" in format_report(a, Config())


def test_assess_reports_failures_instead_of_guessing(tmp_path):
    def broken(url, params=None, headers=None):
        raise RuntimeError("boom")
    a = assess_symbol("WDGT", Config(), AlpacaData("k", "s", fetch=broken),
                      Edgar("T t@example.com", cache_dir=tmp_path, fetch=broken), NOW)
    assert a.stance.startswith("NO DATA") and a.halal_verdict == halal.UNKNOWN
    notes = " ".join(a.data_notes)
    assert "Price data unavailable" in notes and "SEC data unavailable" in notes and "News unavailable" in notes
    assert "no price data" in format_report(a, Config())


def test_stale_data_is_called_out(tmp_path):
    calls = []
    fetch = _fake_fetch(calls)
    a = assess_symbol("WDGT", Config(), AlpacaData("k", "s", fetch=fetch),
                      Edgar("T t@example.com", cache_dir=tmp_path, fetch=fetch), NOW + timedelta(hours=2))
    assert any(n.startswith("STALE") for n in a.data_notes)


def test_decide_wait_states():
    cfg = Config()
    a = Assessment("X", NOW)
    decide(a, cfg)
    assert a.stance == "NO DATA"


def test_cli_assess_needs_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    assert main(["assess", "OMH"]) == 1
    assert "API keys are missing" in capsys.readouterr().err


def test_load_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    p = tmp_path / ".env"
    p.write_text('# comment\nSEC_USER_AGENT="Jane Doe jane@example.com"\nEMPTY=\n')
    load_env(str(p))
    import os
    assert os.environ["SEC_USER_AGENT"] == "Jane Doe jane@example.com"
