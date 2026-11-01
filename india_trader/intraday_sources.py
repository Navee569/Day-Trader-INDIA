"""Read-only five-minute candle providers. Credentials never enable order APIs."""
from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from functools import lru_cache

from .core import IST, DataError, parse_bars, symbol
from .data import fetch_bars as yahoo_bars
from .universe import load_nse_equities, load_bse_equities

UPSTOX = "https://api.upstox.com/v3/historical-candle"
MAX_RESPONSE = 8 * 1024 * 1024


def parse_upstox_candles(payload: dict, interval: int = 5) -> list[dict]:
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
    if interval == 5:
        return [bar.record() for bar in parse_bars(rows)]
    if interval != 1:
        raise DataError("Upstox interval must be one or five minutes")
    result, seen = [], set()
    for row in rows:
        try:
            stamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
            values = [float(row[k]) for k in ("open", "high", "low", "close")]
            volume = float(row["volume"])
        except (TypeError, ValueError, KeyError) as exc:
            raise DataError("invalid Upstox one-minute candle") from exc
        o, h, l, c = values
        if stamp.tzinfo is None or stamp.second or stamp.microsecond or stamp in seen or \
                not all(math.isfinite(x) for x in values + [volume]) or \
                l <= 0 or l > min(o, c) or h < max(o, c) or volume < 0:
            raise DataError("invalid Upstox one-minute OHLCV")
        seen.add(stamp)
        result.append({**row, "timestamp": stamp.astimezone(IST).isoformat(),
                       "open": o, "high": h, "low": l, "close": c, "volume": volume})
    return sorted(result, key=lambda x: x["timestamp"])


def _upstox_get(url: str, token: str, interval: int = 5) -> list[dict]:
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise DataError("Upstox response exceeds size limit")
    return parse_upstox_candles(json.loads(raw), interval)


def upstox_bars(ticker: str, isin: str, token: str, range_: str = "5d",
                interval: int = 5, end_day=None) -> list[dict]:
    """Read V3 equity candles in legal one-month chunks, plus current-day bars."""
    if ticker.endswith(".NS"):
        if symbol(ticker, "NSE") != ticker:
            raise ValueError("invalid NSE symbol")
        segment = "NSE_EQ"
    elif ticker.endswith(".BO"):
        if symbol(ticker, "BSE") != ticker:
            raise ValueError("invalid BSE symbol")
        segment = "BSE_EQ"
    else:
        raise ValueError("Upstox connector supports NSE/BSE cash equities only")
    if not isin.startswith("IN") or len(isin) != 12 or not isin.isalnum():
        raise ValueError("NSE ISIN is required for Upstox")
    if not token or any(c.isspace() for c in token):
        raise ValueError("Upstox Analytics Token is missing or malformed")
    if range_ not in ("1d", "5d", "1mo", "60d") or interval not in (1, 5):
        raise ValueError("Unsupported Upstox range")
    key = urllib.parse.quote(segment + "|" + isin, safe="")
    today = datetime.now(IST).date()
    end = end_day or today
    if isinstance(end, str):
        end = datetime.fromisoformat(end).date()
    if end > today:
        raise ValueError("cannot fetch future Upstox candles")
    days = {"1d": 1, "5d": 10, "1mo": 35, "60d": 65}[range_]
    start = end - timedelta(days=days - 1)
    by_time = {}
    errors = []
    historical_end = min(end, today - timedelta(days=1))
    while historical_end >= start:
        chunk_start = max(start, historical_end - timedelta(days=29))
        url = (f"{UPSTOX}/{key}/minutes/{interval}/{historical_end.isoformat()}/"
               f"{chunk_start.isoformat()}")
        try:
            for row in _upstox_get(url, token, interval):
                by_time[row["timestamp"]] = row
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(type(exc).__name__)
        historical_end = chunk_start - timedelta(days=1)
    if end == today:
        url = f"{UPSTOX}/intraday/{key}/minutes/{interval}"
        try:
            for row in _upstox_get(url, token, interval):
                by_time[row["timestamp"]] = row
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(type(exc).__name__)
    if errors and range_ != "1d":
        raise DataError("Upstox historical candle request was incomplete: " + ", ".join(errors))
    if not by_time:
        raise DataError("Upstox five-minute candles unavailable: " + ", ".join(errors))
    return [by_time[k] for k in sorted(by_time)]


@lru_cache(maxsize=2)
def _instruments(exchange):
    rows = load_nse_equities()[0] if exchange == "NSE" else load_bse_equities()[0]
    return {r["ticker"]: r["isin"] for r in rows}


def upstox_instrument(ticker):
    exchange = "NSE" if ticker.endswith(".NS") else "BSE" if ticker.endswith(".BO") else None
    if not exchange:
        raise DataError("Upstox instrument needs NSE/BSE equity ticker")
    isin = _instruments(exchange).get(ticker)
    if not isin:
        raise DataError("Ticker absent from current exchange equity master")
    return isin


def upstox_chart(ticker, token=None, range_="5d", interval=5, end_day=None):
    """Yahoo-shaped container for existing validators; source must stay Upstox."""
    token = token or os.environ.get("UPSTOX_ANALYTICS_TOKEN", "").strip()
    if not token:
        raise DataError("Upstox Analytics Token is not configured")
    bars = upstox_bars(ticker, upstox_instrument(ticker), token, range_, interval, end_day)
    if not bars:
        raise DataError("Upstox returned no candles")
    return _bars_chart(ticker, bars, interval, "Upstox Analytics Token read-only candles")


def _bars_chart(ticker, bars, interval, source):
    stamps = [datetime.fromisoformat(b["timestamp"]) for b in bars]
    data_time = min(datetime.now(IST), stamps[-1].astimezone(IST) + timedelta(minutes=interval))
    obj = {"chart": {"result": [{"meta": {"symbol": ticker, "currency": "INR",
                                           "dataGranularity": f"{interval}m",
                                           "regularMarketTime": int(data_time.timestamp())},
                                 "timestamp": [int(x.timestamp()) for x in stamps],
                                 "indicators": {"quote": [{k: [b[k] for b in bars]
                                                            for k in ("open", "high", "low", "close", "volume")}]}}],
                     "error": None}}
    return json.dumps(obj).encode(), source


def fyers_bars(ticker, app_id, token, range_="5d", interval=5, end_day=None):
    """FYERS historical NSE cash candles; uses only the documented data endpoint."""
    if not ticker.endswith(".NS") or symbol(ticker, "NSE") != ticker:
        raise DataError("FYERS connector supports NSE cash equities only")
    if not re.fullmatch(r"[A-Za-z0-9-]{4,30}", app_id or "") or \
            not token or any(c.isspace() for c in token):
        raise DataError("FYERS app ID and access token are missing or malformed")
    if range_ not in ("1d", "5d", "1mo", "60d") or interval not in (1, 5):
        raise DataError("unsupported FYERS candle range or interval")
    today = datetime.now(IST).date()
    end = datetime.fromisoformat(end_day).date() if isinstance(end_day, str) else end_day or today
    if end > today:
        raise DataError("cannot fetch future FYERS candles")
    start = end - timedelta(days={"1d": 1, "5d": 10, "1mo": 35, "60d": 65}[range_] - 1)
    from_stamp = datetime.combine(start, datetime.min.time(), IST)
    to_stamp = datetime.combine(end + timedelta(days=1), datetime.min.time(), IST) - timedelta(seconds=1)
    # FYERS says the current forming candle may be returned: exclude it in the request.
    to_stamp = min(to_stamp, datetime.now(IST) - timedelta(minutes=interval))
    params = {"symbol": "NSE:" + ticker[:-3] + "-EQ", "resolution": str(interval),
              "date_format": "0", "range_from": str(int(from_stamp.timestamp())),
              "range_to": str(int(to_stamp.timestamp())), "cont_flag": "0"}
    url = "https://api-t1.fyers.in/data/history?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "Authorization": app_id + ":" + token})
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise DataError("FYERS response exceeds size limit")
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get("s") != "ok" or not isinstance(doc.get("candles"), list):
        raise DataError("FYERS did not return successful candle data")
    rows = []
    for candle in doc["candles"]:
        if not isinstance(candle, list) or len(candle) != 6:
            raise DataError("FYERS candle has unexpected shape")
        rows.append([datetime.fromtimestamp(int(candle[0]), IST).isoformat(), *candle[1:]])
    return parse_upstox_candles({"status": "success", "data": {"candles": rows}}, interval)


def fyers_chart(ticker, range_="5d", interval=5, end_day=None):
    app_id = os.environ.get("FYERS_APP_ID", "").strip()
    token = os.environ.get("FYERS_ACCESS_TOKEN", "").strip()
    if not app_id or not token:
        raise DataError("FYERS market-data app and token are not configured")
    bars = fyers_bars(ticker, app_id, token, range_, interval, end_day)
    if not bars:
        raise DataError("FYERS returned no candles")
    return _bars_chart(ticker, bars, interval, "FYERS read-only historical candles")


def upstox_quote(ticker, token=None):
    """Read one dated exchange snapshot; never infer a quote from a candle."""
    token = token or os.environ.get("UPSTOX_ANALYTICS_TOKEN", "").strip()
    if not token:
        raise DataError("Upstox Analytics Token is not configured")
    isin = upstox_instrument(ticker)
    segment = "NSE_EQ" if ticker.endswith(".NS") else "BSE_EQ"
    key = segment + "|" + isin
    url = "https://api.upstox.com/v3/market-quote/quotes?" + urllib.parse.urlencode({"instrument_key": key})
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise DataError("Upstox quote response exceeds size limit")
    try:
        doc = json.loads(raw)
        candidates = doc["data"]
        if doc["status"] != "success" or not isinstance(candidates, dict):
            raise ValueError("bad quote response")
        match = [v for k, v in candidates.items() if k.startswith(segment + ":") and
                 isinstance(v, dict) and v.get("instrument_token") == key]
        if len(match) != 1:
            raise ValueError("instrument identity mismatch")
        item = match[0]
        price = float(item["last_price"])
        previous = float(item["prev_close_price"])
        milliseconds = int(item["last_trade_time"])
        stamp = datetime.fromtimestamp(milliseconds / 1000, IST)
        if not math.isfinite(price) or not math.isfinite(previous) or price <= 0 or previous <= 0:
            raise ValueError("invalid INR quote")
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        raise DataError("unusable Upstox quote for requested equity") from exc
    return {"ticker": ticker, "price": price, "prev_close": previous,
            "time": stamp.isoformat(), "source": "Upstox Analytics Token read-only quote", "currency": "INR"}


def fetch_preferred(ticker: str, range_: str = "5d") -> dict:
    """Try configured read-only Upstox, then unofficial Yahoo. Never mix bars."""
    attempts = []
    token = os.environ.get("UPSTOX_ANALYTICS_TOKEN", "").strip()
    if token and ticker.endswith((".NS", ".BO")):
        try:
            isin = upstox_instrument(ticker)
            bars = upstox_bars(ticker, isin, token, range_)
            return {"source": "Upstox Analytics Token read-only candles", "bars": bars,
                    "attempts": attempts, "exchange_verified": False}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            attempts.append({"source": "Upstox", "status": "FAILED", "reason": type(exc).__name__})
    else:
        attempts.append({"source": "Upstox", "status": "NOT_CONFIGURED"})
    if ticker.endswith(".NS") and os.environ.get("FYERS_APP_ID") and os.environ.get("FYERS_ACCESS_TOKEN"):
        try:
            bars = fyers_bars(ticker, os.environ["FYERS_APP_ID"], os.environ["FYERS_ACCESS_TOKEN"], range_)
            return {"source": "FYERS read-only historical candles", "bars": bars,
                    "attempts": attempts, "exchange_verified": False}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            attempts.append({"source": "FYERS", "status": "FAILED", "reason": type(exc).__name__})
    else:
        attempts.append({"source": "FYERS", "status": "NOT_CONFIGURED"})
    try:
        bars = yahoo_bars(ticker, range_=range_)
        return {"source": "Yahoo Finance unofficial five-minute candles", "bars": bars,
                "attempts": attempts, "exchange_verified": False}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
        attempts.append({"source": "Yahoo", "status": "FAILED", "reason": type(exc).__name__})
        raise DataError("All configured five-minute candle sources failed") from exc
