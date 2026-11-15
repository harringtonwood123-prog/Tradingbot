"""SEC EDGAR (official, free data.sec.gov APIs): company identity, industry
code, recent filings and reported financials.

The SEC asks every automated client to send a User-Agent that identifies you
(name and email). Set SEC_USER_AGENT in .env. Limit: 10 requests per second.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..http import Fetch, get_json


class Edgar:
    def __init__(self, user_agent: str, cache_dir: str | Path = "data/cache", fetch: Fetch = get_json) -> None:
        if not user_agent or "@" not in user_agent:
            raise ValueError(
                "SEC_USER_AGENT is missing. The SEC requires your name and email, "
                'e.g. SEC_USER_AGENT="Jane Doe jane@example.com" in .env')
        self._headers = {"User-Agent": user_agent}
        self._cache = Path(cache_dir)
        self._fetch = fetch
        self._tickers: dict[str, tuple[int, str]] | None = None

    def _ticker_map(self) -> dict[str, tuple[int, str]]:
        if self._tickers is None:
            path = self._cache / "company_tickers.json"
            if path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
                data = json.loads(path.read_text())
            else:
                data = self._fetch("https://www.sec.gov/files/company_tickers.json", None, self._headers)
                self._cache.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data))
            self._tickers = {
                str(row["ticker"]).upper(): (int(row["cik_str"]), str(row["title"]))
                for row in data.values()
            }
        return self._tickers

    def lookup(self, ticker: str) -> tuple[int, str] | None:
        """(CIK, company name) or None if the SEC has no such ticker."""
        return self._ticker_map().get(ticker.upper().replace(".", "-"))

    def submissions(self, cik: int) -> dict[str, Any]:
        return self._fetch(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", None, self._headers)

    def company_facts(self, cik: int) -> dict[str, Any]:
        return self._fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", None, self._headers)
