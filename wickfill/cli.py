"""Command line: python -m wickfill <command>"""
from __future__ import annotations

import argparse
import sys
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import __version__
from .bars import detect_interval_minutes, drop_forming, load_csv, quality_notes, resample
from .config import load_config
from .engine import BASE_MINUTES
from .models import UTC
from .replay import format_event, format_status_box, run_replay
from .sample import write_sample

DISCLAIMER = (
    "Educational tool only. Not financial advice. Nothing here predicts or promises profit. "
    "Check the news and filings before acting."
)
_LATER = {"scan": 2, "watch": 2, "paper": 3, "report": 3, "backtest": 4}


def load_env(path: str = ".env") -> None:
    """Read KEY=VALUE lines from .env into the environment (existing values win)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _read_tickers(args: argparse.Namespace) -> list[str]:
    raw = list(args.tickers)
    if args.file:
        for line in Path(args.file).read_text().splitlines():
            raw += line.split("#")[0].replace(",", " ").split()
    seen: list[str] = []
    for t in raw:
        t = t.upper().strip().lstrip("$")
        if t and t not in seen:
            seen.append(t)
    return seen


def _cmd_assess(args: argparse.Namespace) -> int:
    from .assess import assess_symbol, format_report
    from .providers.alpaca import AlpacaData
    from .providers.edgar import Edgar

    cfg = load_config(args.config)
    load_env()
    tickers = _read_tickers(args)
    if not tickers:
        print("Give at least one ticker, e.g.  python -m wickfill assess OMH MERC", file=sys.stderr)
        return 1
    try:
        alpaca = AlpacaData(os.environ.get("ALPACA_API_KEY_ID", ""), os.environ.get("ALPACA_API_SECRET_KEY", ""),
                            feed=cfg.data.feed, base_url=cfg.data.alpaca_data_url)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    edgar = None
    try:
        edgar = Edgar(os.environ.get("SEC_USER_AGENT", ""))
    except ValueError as exc:
        print(f"WARNING: {exc}", file=sys.stderr)
    reports = []
    for t in tickers:
        a = assess_symbol(t, cfg, alpaca, edgar, datetime.now(UTC), catalyst_checked=args.catalyst_ok)
        reports.append(format_report(a, cfg))
        print(reports[-1])
        print()
    if args.out:
        Path(args.out).write_text("\n\n".join(reports) + "\n")
        print(f"Saved to {args.out}")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    tz = cfg.display.local_tz
    bars, warnings = load_csv(args.csv, naive_tz=args.tz)
    if not bars:
        print("No usable bars in that file.", file=sys.stderr)
        return 1
    interval = detect_interval_minutes(bars)
    if interval is None or interval > BASE_MINUTES or BASE_MINUTES % interval:
        print(f"Bars look like {interval}-minute data. Replay needs 1m or 5m bars.", file=sys.stderr)
        return 1
    if interval < BASE_MINUTES:
        warnings.append(f"Input is {interval}m data; combined into 5m bars.")
        bars = resample(bars, BASE_MINUTES)
    kept = drop_forming(bars, datetime.now(UTC), BASE_MINUTES)
    if len(kept) < len(bars):
        warnings.append(f"Dropped {len(bars) - len(kept)} still-forming bar(s) at the end.")
    bars = kept
    warnings += quality_notes(bars, BASE_MINUTES)

    result = run_replay(bars, args.symbol.upper(), cfg, args.prev_close, args.catalyst_ok)

    print(f"Wick Fill replay: {args.symbol.upper()}  {len(bars)} five-minute bars")
    print("Times are the START of the 5m bar; an event is known when that bar closes.")
    print("Prices are used as given. Make sure your export is NOT split-adjusted across a split.")
    for w in warnings:
        print(f"WARNING: {w}")
    if not args.catalyst_ok:
        print("NOTE: catalyst check not ticked (--catalyst-ok), so every BUY will show as blocked.")
    print()
    for e in result.events:
        if e.kind == "STATE_CHANGE" and not args.verbose:
            continue
        print(format_event(e, tz))
    counts = Counter(e.kind for e in result.events)
    print()
    print("Summary: " + ", ".join(f"{k}={counts[k]}" for k in (
        "ZONE_NEW", "WICK_FILLED", "ZONE_EXPIRED", "ZONE_FAILED", "SIGNAL", "SETUP_SKIPPED")))
    if result.final is not None:
        print()
        print(format_status_box(result.final, tz))
    print()
    print(DISCLAIMER)
    return 0


def _cmd_sample(args: argparse.Namespace) -> int:
    n = write_sample(args.out)
    print(f"Wrote {n} SYNTHETIC bars to {args.out} (made-up data for testing, not real prices).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wickfill", description="Wick Fill assistant. " + DISCLAIMER)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("replay", help="run a CSV of 1m/5m bars through the strategy engine")
    p.add_argument("csv")
    p.add_argument("--symbol", required=True)
    p.add_argument("--config", help="path to config.yaml (defaults are used if omitted)")
    p.add_argument("--prev-close", type=float, help="prior day's regular close, if the CSV starts mid-story")
    p.add_argument("--catalyst-ok", action="store_true", help="pretend the manual catalyst check was ticked")
    p.add_argument("--tz", default="America/New_York", help="timezone of timestamps that have none")
    p.add_argument("--verbose", action="store_true", help="also print every state change")
    p.set_defaults(func=_cmd_replay)

    p = sub.add_parser("assess", help="assess tickers: halal screen, risk flags, news, wick-fill levels")
    p.add_argument("tickers", nargs="*")
    p.add_argument("--file", help="text file of tickers (one per line or space/comma separated)")
    p.add_argument("--config", help="path to config.yaml (defaults are used if omitted)")
    p.add_argument("--catalyst-ok", action="store_true", help="you have already read the news and filings")
    p.add_argument("--out", help="also save the report to this text file")
    p.set_defaults(func=_cmd_assess)

    p = sub.add_parser("sample", help="write a synthetic sample CSV to try replay with")
    p.add_argument("out", nargs="?", default="data/sample_5m.csv")
    p.set_defaults(func=_cmd_sample)

    for name, phase in _LATER.items():
        p = sub.add_parser(name, help=f"(arrives in Phase {phase})")
        p.set_defaults(func=lambda a, n=name, ph=phase: (print(f"'{n}' is not built yet; it arrives in Phase {ph}."), 2)[1])

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
