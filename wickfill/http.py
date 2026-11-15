"""Minimal JSON-over-HTTPS GET using only the standard library.

Secrets travel in headers and are never put in URLs, error messages or logs.
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

Fetch = Callable[[str, dict[str, Any] | None, dict[str, str] | None], Any]


class HttpError(Exception):
    def __init__(self, status: int | None, url: str, message: str) -> None:
        super().__init__(f"{message} (HTTP {status}) for {url}" if status else f"{message} for {url}")
        self.status = status


def get_json(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
             timeout: float = 20.0, retries: int = 2) -> Any:
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{url}?{urllib.parse.urlencode(clean)}"
    shown = url.split("?")[0]
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", **(headers or {})})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            hint = {
                401: "not authorised: check your API keys in .env",
                403: "forbidden: your keys or data plan do not allow this request",
                404: "not found",
                422: "request rejected",
                429: "rate limited: wait a minute and try again",
            }.get(exc.code, "request failed")
            raise HttpError(exc.code, shown, hint) from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt < retries:
                time.sleep(1 + attempt)
                continue
            raise HttpError(None, shown, f"network problem ({type(exc).__name__})") from None
    raise HttpError(None, shown, "request failed")
