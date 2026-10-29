"""Read-only five-minute candle providers. Credentials never enable order APIs."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .core import IST, DataError, parse_bars, symbol
from .data import fetch_bars as yahoo_bars
from .universe import load_nse_equities

UPSTOX = "https://api.upstox.com/v3/historical-candle"
MAX_RESPONSE = 8 * 1024 * 1024


def parse_upstox_candles(payload: dict) -> list[dict]:
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise DataError("Upstox did not return successful candle data")
    candles = (payload.get("data") or {}).get("candles")
    if not isinstance(candles, list):
        raise DataError("Upstox candle list is missing")
    rows = []
    for candle in candles:
        if not isinstance(candle, list) or len(candle) < 6:
            raise DataError("Upstox candle has unexpected shape")
        rows.append({"timestamp": candle[0], "open": candle[1], "high": candle[2],
                     "low": candle[3], "close": candle[4], "volume": candle[5]})
    return [bar.record() for bar in parse_bars(rows)]


def _upstox_get(url: str, token: str) -> list[dict]:
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise DataError("Upstox response exceeds size limit")
    return parse_upstox_candles(json.loads(raw))


def upstox_bars(ticker: str, isin: str, token: str, range_: str = "5d") -> list[dict]:
    """Combine official historical and current-day V3 candles for one NSE EQ share."""
    if symbol(ticker, "NSE") != ticker or not ticker.endswith(".NS"):
        raise ValueError("Upstox connector currently supports NSE EQ only")
    if not isin.startswith("IN") or len(isin) != 12 or not isin.isalnum():
        raise ValueError("NSE ISIN is required for Upstox")
    if not token or any(c.isspace() for c in token):
        raise ValueError("Upstox Analytics Token is missing or malformed")
    if range_ not in ("1d", "5d", "1mo"):
        raise ValueError("Unsupported Upstox range")
    key = urllib.parse.quote("NSE_EQ|" + isin, safe="")
    today = datetime.now(IST).date()
    days = {"1d": 1, "5d": 10, "1mo": 35}[range_]
    by_time = {}
    errors = []
    if range_ != "1d":
        url = f"{UPSTOX}/{key}/minutes/5/{today.isoformat()}/{(today - timedelta(days=days)).isoformat()}"
        try:
            for row in _upstox_get(url, token):
                by_time[row["timestamp"]] = row
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(type(exc).__name__)
    url = f"{UPSTOX}/intraday/{key}/minutes/5"
    try:
        for row in _upstox_get(url, token):
            by_time[row["timestamp"]] = row
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(type(exc).__name__)
    if not by_time:
        raise DataError("Upstox five-minute candles unavailable: " + ", ".join(errors))
    return [by_time[k] for k in sorted(by_time)]


def fetch_preferred(ticker: str, range_: str = "5d") -> dict:
    """Try configured read-only Upstox, then unofficial Yahoo. Never mix bars."""
    attempts = []
    token = os.environ.get("UPSTOX_ANALYTICS_TOKEN", "").strip()
    if token and ticker.endswith(".NS"):
        try:
            rows, _ = load_nse_equities()
            isin = next((r["isin"] for r in rows if r["ticker"] == ticker), None)
            if not isin:
                raise DataError("Ticker absent from current NSE EQ list")
            bars = upstox_bars(ticker, isin, token, range_)
            return {"source": "Upstox Analytics Token read-only candles", "bars": bars,
                    "attempts": attempts, "exchange_verified": False}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            attempts.append({"source": "Upstox", "status": "FAILED", "reason": type(exc).__name__})
    else:
        attempts.append({"source": "Upstox", "status": "NOT_CONFIGURED"})
    try:
        bars = yahoo_bars(ticker, range_=range_)
        return {"source": "Yahoo Finance unofficial five-minute candles", "bars": bars,
                "attempts": attempts, "exchange_verified": False}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
        attempts.append({"source": "Yahoo", "status": "FAILED", "reason": type(exc).__name__})
        raise DataError("All configured five-minute candle sources failed") from exc
