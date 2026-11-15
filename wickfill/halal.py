"""Automated Shariah pre-screen: business activity + financial ratios.

This is a first-pass filter, NOT a religious ruling. It uses the company's SEC
industry code, its name, and figures from its own SEC filings. It cannot see
revenue by business line, so a company with a small haram side business can
pass, and a mixed company can be marked doubtful. Scholars and screening
providers also differ on thresholds. Confirm with a certified screener or a
scholar you trust before relying on it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

PASS, DOUBTFUL, FAIL, UNKNOWN = "PASS", "DOUBTFUL", "FAIL", "UNKNOWN"
_ORDER = {PASS: 0, UNKNOWN: 1, DOUBTFUL: 2, FAIL: 3}

# SEC SIC code -> (verdict, category, reason)
_SIC: dict[int, tuple[str, str, str]] = {}


def _add(codes: list[int], verdict: str, category: str, reason: str) -> None:
    for c in codes:
        _SIC[c] = (verdict, category, reason)


_add([2082, 2084, 2085, 5180, 5181, 5182, 5921], FAIL, "alcohol", "alcoholic beverages")
_add([2080], DOUBTFUL, "alcohol", "beverages (may include alcohol)")
_add([2100, 2111, 2121, 2131, 2141, 5194], FAIL, "tobacco", "tobacco")
_add([7993, 7999], DOUBTFUL, "gambling", "amusement/recreation services (often gaming)")
_add([7990], DOUBTFUL, "gambling", "misc. amusement and recreation (includes casinos and betting)")
_add([7011], DOUBTFUL, "gambling", "hotels and motels (casino resorts file here; hotels also sell alcohol)")
_add([3480, 3482, 3483, 3484, 3489, 3760, 3761, 3764, 3769, 3795], FAIL, "defense",
     "weapons, ordnance, missiles or military vehicles")
_add([3812, 3720, 3721, 3724, 3728, 3730], DOUBTFUL, "defense",
     "aerospace / guidance systems / shipbuilding (often defense contractors)")
_add([6020, 6021, 6022, 6029, 6035, 6036, 6099, 6111, 6141, 6153, 6159, 6162, 6163, 6172, 6199],
     FAIL, "conventional finance", "interest-based banking or lending")
_add([6211, 6221, 6282], DOUBTFUL, "conventional finance", "brokerage / investment services")
_add([6311, 6321, 6324, 6331, 6351, 6361, 6399, 6411], FAIL, "conventional finance", "conventional insurance")
_add([6770], DOUBTFUL, "conventional finance", "blank-check company (cash usually held in interest-bearing trust)")
_add([2011, 2013], DOUBTFUL, "pork", "meat packing (may include pork)")
_add([5812, 5813], DOUBTFUL, "food service", "restaurants/bars (alcohol and non-halal food sales)")
_add([5411, 5912], DOUBTFUL, "retail", "grocery/drug retail (sells alcohol and tobacco)")
_add([7812, 7822, 7829, 7841, 4833, 4841, 7900], DOUBTFUL, "entertainment", "film / TV / entertainment content")

# Whole-word patterns checked against the company name and SIC description.
_KEYWORDS: list[tuple[str, str, str]] = [
    (r"casino|gambling|sportsbook|betting|wager|lottery|lotto|igaming|slot machines?", FAIL, "gambling"),
    (r"gaming", DOUBTFUL, "gambling"),
    (r"brewer(y|ies|ing)|distiller(y|ies|s)?|winer(y|ies)|wines?|spirits|liquor|beer|vodka|whiske?y|tequila|alcohol",
     FAIL, "alcohol"),
    (r"tobacco|cigarettes?|cigars?|vape|vaping|nicotine", FAIL, "tobacco"),
    (r"cannabis|marijuana|weed", FAIL, "cannabis"),
    (r"defen[cs]e|weapons?|munitions?|firearms?|ammunition|missiles?|ordnance|armaments?", FAIL, "defense"),
    (r"aerospace|drones?|tactical|military", DOUBTFUL, "defense"),
    (r"bank|bancorp|bancshares|lending|loans?|mortgage|credit|insurance|assurance", FAIL, "conventional finance"),
    (r"financial|acquisition corp", DOUBTFUL, "conventional finance"),
    (r"pork", FAIL, "pork"),
    (r"adult entertainment", FAIL, "adult"),
]


@dataclass
class BusinessScreen:
    verdict: str
    sic: int | None
    sic_description: str
    findings: list[str] = field(default_factory=list)  # human-readable reasons
    categories: list[str] = field(default_factory=list)


def screen_business(name: str, sic: int | None, sic_description: str) -> BusinessScreen:
    verdict = PASS if sic is not None else UNKNOWN
    findings: list[str] = []
    cats: list[str] = []
    if sic is None:
        findings.append("no SEC industry code found: business activity could not be checked")
    elif sic in _SIC:
        v, cat, reason = _SIC[sic]
        verdict = v
        cats.append(cat)
        findings.append(f"SIC {sic} ({sic_description or 'n/a'}): {reason}")
    text = f"{name} {sic_description}".lower()
    for pattern, v, cat in _KEYWORDS:
        m = re.search(rf"\b(?:{pattern})\b", text)
        if m:
            if _ORDER[v] > _ORDER[verdict]:
                verdict = v
            if cat not in cats:
                cats.append(cat)
            findings.append(f"name/industry mentions '{m.group(0)}' ({cat})")
    return BusinessScreen(verdict, sic, sic_description, findings, cats)


# ---------------------------------------------------------------------------
# Financial ratios from SEC XBRL "company facts"
# ---------------------------------------------------------------------------

def _series(facts: dict[str, Any], name: str, unit: str = "USD") -> list[dict[str, Any]]:
    for taxonomy in ("us-gaap", "ifrs-full", "dei"):
        node = (facts.get("facts") or {}).get(taxonomy, {}).get(name)
        if node:
            units = node.get("units") or {}
            if unit in units:
                return units[unit]
            if unit == "USD" and units and taxonomy != "dei":
                return []  # reported in another currency: do not mix currencies
    return []


def _latest_instant(facts: dict[str, Any], names: list[str], unit: str = "USD") -> tuple[float, date, str] | None:
    best: tuple[float, date, str] | None = None
    for name in names:
        for row in _series(facts, name, unit):
            if "start" in row or "end" not in row:
                continue
            end = date.fromisoformat(row["end"])
            if best is None or end > best[1]:
                best = (float(row["val"]), end, name)
    return best


def _latest_annual(facts: dict[str, Any], names: list[str]) -> tuple[float, date, str] | None:
    best: tuple[float, date, str] | None = None
    for name in names:
        for row in _series(facts, name):
            if "start" not in row or "end" not in row:
                continue
            start, end = date.fromisoformat(row["start"]), date.fromisoformat(row["end"])
            if not 340 <= (end - start).days <= 380:
                continue
            if best is None or end > best[1]:
                best = (float(row["val"]), end, name)
    return best


@dataclass
class FinancialScreen:
    verdict: str
    as_of: date | None = None
    debt: float | None = None
    cash_and_securities: float | None = None
    total_assets: float | None = None
    market_cap: float | None = None
    shares: float | None = None
    shares_as_of: date | None = None
    interest_income: float | None = None
    revenue: float | None = None
    debt_ratio: float | None = None  # vs market cap
    cash_ratio: float | None = None  # vs market cap
    debt_to_assets: float | None = None  # shown for reference
    income_ratio: float | None = None
    notes: list[str] = field(default_factory=list)


def screen_financials(facts: dict[str, Any] | None, price: float | None,
                      max_debt: float = 0.30, max_cash: float = 0.30, max_income: float = 0.05) -> FinancialScreen:
    """AAOIFI-style ratios: interest-bearing debt / market cap, cash and
    interest-bearing securities / market cap, impermissible income / revenue."""
    fs = FinancialScreen(UNKNOWN)
    if not facts:
        fs.notes.append("no financial data from SEC filings: ratios could not be checked")
        return fs
    assets = _latest_instant(facts, ["Assets"])
    if assets is None:
        fs.notes.append("no balance sheet in US dollars found in SEC data (foreign filers often report "
                        "in another currency or format): ratios could not be checked")
        return fs
    fs.total_assets, fs.as_of = assets[0], assets[1]

    def at_balance_date(names: list[str]) -> float | None:
        hit = _latest_instant(facts, names)
        if hit is None or abs((hit[1] - assets[1]).days) > 100:
            return None
        return hit[0]

    def first(*names: str) -> float | None:
        for n in names:
            v = at_balance_date([n])
            if v is not None:
                return v
        return None

    # Approximate: sums the debt items the company tagged. Leases are not counted.
    combined = first("DebtLongtermAndShorttermCombinedAmount")
    if combined is not None:
        buckets = [combined]
    else:
        lt_non, lt_all = first("LongTermDebtNoncurrent"), first("LongTermDebt")
        debt_cur, lt_cur, stb = first("DebtCurrent"), first("LongTermDebtCurrent"), first("ShortTermBorrowings")
        if lt_non is not None:
            buckets = [lt_non, debt_cur if debt_cur is not None else (lt_cur or 0.0) + (stb or 0.0)]
        elif lt_all is not None:
            buckets = [lt_all, stb]  # LongTermDebt already includes its current part
        else:
            buckets = [debt_cur if debt_cur is not None else stb]
    conv = first("ConvertibleNotesPayable")
    if conv is None:
        parts = [first("ConvertibleNotesPayableCurrent"), first("ConvertibleLongTermNotesPayable")]
        conv = sum(x or 0.0 for x in parts) if any(x is not None for x in parts) else None
    buckets += [conv, first("NotesPayable", "NotesPayableCurrent")]
    if all(x is None for x in buckets):
        fs.debt = 0.0
        fs.notes.append("no debt items tagged in the latest filing: debt treated as zero (verify)")
    else:
        fs.debt = sum(x or 0.0 for x in buckets)
    cash = at_balance_date(["CashAndCashEquivalentsAtCarryingValue",
                            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"])
    securities = at_balance_date(["ShortTermInvestments", "MarketableSecuritiesCurrent",
                                  "AvailableForSaleSecuritiesDebtSecuritiesCurrent"])
    if cash is not None or securities is not None:
        fs.cash_and_securities = (cash or 0.0) + (securities or 0.0)

    shares = _latest_instant(facts, ["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"], "shares")
    if shares and price:
        fs.shares, fs.shares_as_of = shares[0], shares[1]
        fs.market_cap = shares[0] * price
        fs.notes.append(f"market cap uses the share count reported for {shares[1]}; "
                        "small caps often issue shares, so it may be out of date")
    fs.debt_to_assets = fs.debt / fs.total_assets if fs.total_assets else None

    revenue = _latest_annual(facts, ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                                     "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"])
    interest = _latest_annual(facts, ["InvestmentIncomeInterest", "InvestmentIncomeInterestAndDividend",
                                      "InterestIncomeOther", "InterestAndOtherIncome"])
    if revenue:
        fs.revenue = revenue[0]
    if interest:
        fs.interest_income = interest[0]
    if fs.revenue and fs.revenue > 0:
        fs.income_ratio = (fs.interest_income or 0.0) / fs.revenue
        if interest is None:
            fs.notes.append("no interest income tagged: treated as zero")
        fs.notes.append("income test covers interest income only; haram revenue by business line is not in this data")
    else:
        fs.notes.append("no annual revenue reported (pre-revenue company?): income test not possible")

    if fs.market_cap:
        fs.debt_ratio = fs.debt / fs.market_cap
        if fs.cash_and_securities is not None:
            fs.cash_ratio = fs.cash_and_securities / fs.market_cap
    else:
        fs.notes.append("share count or price missing: market-cap ratios not possible")

    tests = [(fs.debt_ratio, max_debt), (fs.cash_ratio, max_cash), (fs.income_ratio, max_income)]
    if any(r is not None and r > limit for r, limit in tests):
        fs.verdict = FAIL
    elif all(r is not None for r, _ in tests):
        fs.verdict = PASS
    else:
        fs.verdict = UNKNOWN
    return fs


def overall(business: BusinessScreen, financial: FinancialScreen) -> str:
    return max(business.verdict, financial.verdict, key=lambda v: _ORDER[v])
