"""Non-executable market-data adapter for public, UNOFFICIAL Yahoo charts."""
from __future__ import annotations
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from .core import IST, Bar, DataError, parse_bars, symbol

HOSTS = (
    "https://query2.finance.yahoo.com/v8/finance/chart/",
    "https://query1.finance.yahoo.com/v8/finance/chart/",
)
MAX_RESPONSE = 8 * 1024 * 1024
INTERVALS = {"5m": {"1d", "5d", "1mo"}}


def fetch_bars(ticker: str, interval: str = "5m", range_: str = "5d") -> list[dict]:
    if interval not in INTERVALS or range_ not in INTERVALS[interval]:
        raise ValueError("Only Yahoo 5m bars with 1d, 5d or 1mo range supported")
    if not ticker.endswith((".NS", ".BO")):
        raise ValueError("Fetch requires explicit .NS or .BO symbol")
    code = symbol(ticker, "NSE" if ticker.endswith(".NS") else "BSE")
    suffix = urllib.parse.quote(code, safe="") + "?" + urllib.parse.urlencode({"interval": interval, "range": range_})
    last_error = None
    for host in HOSTS:
        req = urllib.request.Request(host + suffix, headers={"User-Agent": "Mozilla/5.0 (compatible; IndiaDayTraderPaper/0.2)"})
        try:
            with urllib.request.urlopen(req, timeout=12) as rsp:
                raw = rsp.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise DataError("Provider response too large")
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
    else:
        raise DataError(f"Unofficial data feed unavailable at both Yahoo hosts: {last_error}") from last_error
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DataError("Provider returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise DataError("Provider response must be an object")
    chart = payload.get("chart") or {}
    if not isinstance(chart, dict):
        raise DataError("Provider chart must be an object")
    if chart.get("error"):
        raise DataError(f"Provider rejected request: {chart['error']}")
    results = chart.get("result") or []
    if not isinstance(results, list) or len(results) != 1:
        raise DataError("Provider returned no chart or unexpected chart count")
    r = results[0]
    if not isinstance(r, dict):
        raise DataError("Provider chart data must be an object")
    meta = r.get("meta") or {}
    if not isinstance(meta, dict):
        raise DataError("Provider metadata must be an object")
    if meta.get("currency") not in (None, "INR"):
        raise DataError("Provider response currency is not INR")
    if meta.get("symbol") is not None and (not isinstance(meta["symbol"], str) or meta["symbol"].upper() != code):
        raise DataError("Provider response symbol does not match requested ticker")
    stamps = r.get("timestamp") or []
    indicators = r.get("indicators") or {}
    if not isinstance(stamps, list) or not isinstance(indicators, dict):
        raise DataError("Provider timestamp / indicators must have expected structure")
    quotes = indicators.get("quote") or []
    if not isinstance(quotes, list) or len(quotes) != 1 or not stamps:
        raise DataError("Provider response has no 5-minute OHLC chart")
    q = quotes[0]
    if not isinstance(q, dict):
        raise DataError("Provider quote must be an object")
    if any(not isinstance(q.get(k), list) or len(q[k]) != len(stamps)
           for k in ("open", "high", "low", "close")):
        raise DataError("Provider OHLC lengths are inconsistent")
    rows = []
    for i, ts in enumerate(stamps):
        # Null OHLC placeholders are skipped; the ORB continuity check then
        # blocks signals across the resulting gap.
        if any(q[k][i] is None for k in ("open", "high", "low", "close")):
            continue
        try:
            dt = datetime.fromtimestamp(int(ts), IST)
        except (ValueError, TypeError, OSError, OverflowError) as exc:
            raise DataError("Provider supplied invalid Unix timestamp") from exc
        if (dt.minute % 5 or dt.second or dt.microsecond) and i == len(stamps) - 1 and i > 0:
            # Yahoo sometimes appends a current-price update after the aligned
            # in-progress five-minute candle. It is not another 5m candle.
            try:
                previous = datetime.fromtimestamp(int(stamps[i - 1]), IST)
            except (ValueError, TypeError, OSError, OverflowError) as exc:
                raise DataError("Provider supplied invalid Unix timestamp") from exc
            if (previous.minute % 5 == 0 and previous.second == 0 and
                    previous.microsecond == 0 and
                    0 < (dt - previous).total_seconds() < 300):
                continue
        volume = q.get("volume")
        rows.append({"timestamp": dt.isoformat(), **{k:q[k][i] for k in ("open", "high", "low", "close")},
                     "volume": volume[i] if isinstance(volume, list) and i < len(volume) else None})
    out = parse_bars(rows)
    if not out:
        raise DataError("No valid candles; do not trade")
    return [b.record() for b in out]
