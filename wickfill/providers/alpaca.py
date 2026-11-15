"""Alpaca Market Data API (official REST API): historical bars and news.

Read-only. This module never places, changes or cancels orders.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..http import Fetch, get_json
from ..models import UTC, Bar


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


class AlpacaData:
    def __init__(self, key_id: str, secret: str, feed: str = "iex",
                 base_url: str = "https://data.alpaca.markets", fetch: Fetch = get_json) -> None:
        if not key_id or not secret:
            raise ValueError("Alpaca API keys are missing. Put them in .env (see .env.example).")
        self.feed = feed
        self._base = base_url.rstrip("/")
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        self._fetch = fetch

    def bars(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Bar]:
        """Completed bars, oldest first, unadjusted for splits. `timeframe` is
        Alpaca's notation: '5Min', '1Day'."""
        out: list[Bar] = []
        token: str | None = None
        while True:
            data = self._fetch(
                f"{self._base}/v2/stocks/{symbol}/bars",
                {"timeframe": timeframe, "start": _iso(start), "end": _iso(end), "limit": 10000,
                 "adjustment": "raw", "feed": self.feed, "sort": "asc", "page_token": token},
                self._headers,
            )
            for b in data.get("bars") or []:
                out.append(Bar(_parse(b["t"]), float(b["o"]), float(b["h"]), float(b["l"]),
                               float(b["c"]), float(b["v"])))
            token = data.get("next_page_token")
            if not token:
                return out

    def news(self, symbol: str, start: datetime, limit: int = 30) -> list[dict[str, Any]]:
        """Newest first: [{headline, summary, source, url, created_at(datetime)}]."""
        data = self._fetch(
            f"{self._base}/v1beta1/news",
            {"symbols": symbol, "start": _iso(start), "limit": min(limit, 50), "sort": "desc"},
            self._headers,
        )
        items = []
        for n in data.get("news") or []:
            items.append({
                "headline": (n.get("headline") or "").strip(),
                "summary": (n.get("summary") or "").strip(),
                "source": n.get("source") or "",
                "url": n.get("url") or "",
                "created_at": _parse(n["created_at"]) if n.get("created_at") else None,
            })
        return items
