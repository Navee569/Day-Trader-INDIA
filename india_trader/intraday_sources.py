"""Read-only five-minute candle providers. Credentials never enable order APIs."""
from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta
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
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429):
                raise
            errors.append(type(exc).__name__)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(type(exc).__name__)
        historical_end = chunk_start - timedelta(days=1)
    if end == today:
        url = f"{UPSTOX}/intraday/{key}/minutes/{interval}"
        try:
            for row in _upstox_get(url, token, interval):
                by_time[row["timestamp"]] = row
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429):
                raise
            errors.append(type(exc).__name__)
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
        depth = item.get("depth") or {}
        buys = depth.get("buy") or [] if isinstance(depth, dict) else []
        sells = depth.get("sell") or [] if isinstance(depth, dict) else []
        bid = float(buys[0]["price"]) if buys else None
        ask = float(sells[0]["price"]) if sells else None
        milliseconds = int(item["last_trade_time"])
        stamp = datetime.fromtimestamp(milliseconds / 1000, IST)
        if not math.isfinite(price) or not math.isfinite(previous) or price <= 0 or previous <= 0:
            raise ValueError("invalid INR quote")
        if (bid is None) != (ask is None) or bid is not None and (
                not all(math.isfinite(x) and x > 0 for x in (bid, ask)) or ask < bid):
            raise ValueError("invalid bid/ask depth")
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        raise DataError("unusable Upstox quote for requested equity") from exc
    return {"ticker": ticker, "price": price, "prev_close": previous,
            "time": stamp.isoformat(), "source": "Upstox Analytics Token read-only quote", "currency": "INR",
            "bid": bid, "ask": ask}


def fyers_quote(ticker, app_id=None, token=None):
    """Read one FYERS v3 data quote; reject missing identity or trade time."""
    if not ticker.endswith(".NS") or symbol(ticker, "NSE") != ticker:
        raise DataError("FYERS quotes support NSE cash equities only")
    app_id = app_id or os.environ.get("FYERS_APP_ID", "").strip()
    token = token or os.environ.get("FYERS_ACCESS_TOKEN", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{4,30}", app_id or "") or \
            not token or any(c.isspace() for c in token):
        raise DataError("FYERS data credentials are unavailable")
    requested = "NSE:" + ticker[:-3] + "-EQ"
    url = "https://api-t1.fyers.in/data/quotes?" + urllib.parse.urlencode({"symbols": requested})
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "Authorization": app_id + ":" + token})
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise DataError("FYERS quote response exceeds size limit")
    try:
        doc = json.loads(raw)
        items = doc["d"]
        if doc.get("s") != "ok" or not isinstance(items, list) or len(items) != 1:
            raise ValueError("unexpected quote count")
        item = items[0]
        if not isinstance(item, dict) or item.get("n") != requested or not isinstance(item.get("v"), dict):
            raise ValueError("FYERS symbol mismatch")
        values = item["v"]
        price = float(values["lp"])
        previous = float(values["prev_close_price"])
        bid = float(values["bid"]) if values.get("bid") is not None else None
        ask = float(values["ask"]) if values.get("ask") is not None else None
        raw_time = float(values["tt"])
        if not all(math.isfinite(x) and x > 0 for x in (price, previous, raw_time)):
            raise ValueError("invalid quote values")
        if (bid is None) != (ask is None) or bid is not None and (
                not all(math.isfinite(x) and x > 0 for x in (bid, ask)) or ask < bid):
            raise ValueError("invalid bid/ask quote")
        stamp = datetime.fromtimestamp(raw_time / (1000 if raw_time > 1e11 else 1), IST)
    except (ValueError, KeyError, TypeError, OverflowError, OSError) as exc:
        raise DataError("unusable FYERS quote for requested equity") from exc
    return {"ticker": ticker, "price": price, "prev_close": previous,
            "time": stamp.isoformat(), "source": "FYERS read-only data quote", "currency": "INR",
            "bid": bid, "ask": ask}


def upstox_market_status(exchange="NSE", token=None):
    """Read the official exchange-status endpoint; only NORMAL_OPEN is open."""
    if exchange not in ("NSE", "BSE"):
        raise DataError("unsupported cash-equity exchange")
    token = token or os.environ.get("UPSTOX_ANALYTICS_TOKEN", "").strip()
    if not token or any(c.isspace() for c in token):
        raise DataError("Upstox data token unavailable")
    req = urllib.request.Request("https://api.upstox.com/v2/market/status/" + exchange,
                                 headers={"Accept": "application/json", "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise DataError("Upstox market-status response exceeds size limit")
    try:
        doc = json.loads(raw)
        data = doc["data"]
        if doc.get("status") != "success" or data["exchange"] != exchange:
            raise ValueError("exchange status identity mismatch")
        state = data["status"]
        if not isinstance(state, str):
            raise ValueError("invalid exchange status")
        cas = data.get("cas_eligible_status") or {}
        cas_state = cas.get("status") if isinstance(cas, dict) else None
        updated = datetime.fromtimestamp(int(data["last_updated"]) / 1000, IST)
    except (ValueError, TypeError, KeyError, OverflowError, OSError) as exc:
        raise DataError("unusable Upstox exchange status") from exc
    return {"exchange": exchange, "state": state, "open": state == "NORMAL_OPEN" and
            cas_state not in ("CTS_CLOSE", "CAS_LM_START", "CAS_M_STOP", "CAS_STOP"),
            "updated_at": updated.isoformat(), "source": "Upstox exchange status"}


def fyers_market_status(exchange="NSE", app_id=None, token=None):
    """Read FYERS normal cash-market state, rejecting ambiguous segments."""
    if exchange != "NSE":
        raise DataError("FYERS market-status adapter supports NSE cash only")
    app_id = app_id or os.environ.get("FYERS_APP_ID", "").strip()
    token = token or os.environ.get("FYERS_ACCESS_TOKEN", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{4,30}", app_id or "") or \
            not token or any(c.isspace() for c in token):
        raise DataError("FYERS data credentials unavailable")
    req = urllib.request.Request("https://api-t1.fyers.in/data/marketStatus",
                                 headers={"Accept": "application/json",
                                          "Authorization": app_id + ":" + token})
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise DataError("FYERS market-status response exceeds size limit")
    try:
        doc = json.loads(raw)
        states = doc["marketStatus"]
        if doc.get("s") != "ok" or not isinstance(states, list):
            raise ValueError("invalid market-status envelope")
        matches = [row for row in states if isinstance(row, dict) and
                   row.get("exchange") == "NSE" and row.get("segment") in ("EQ", "CM", "NSE_EQ") and
                   row.get("market_type") == "NORMAL"]
        if len(matches) != 1 or not isinstance(matches[0].get("status"), str):
            raise ValueError("ambiguous normal cash-market status")
        state = matches[0]["status"]
    except (ValueError, TypeError, KeyError) as exc:
        raise DataError("unusable FYERS cash-market status") from exc
    return {"exchange": exchange, "state": state, "open": state == "OPEN",
            "source": "FYERS normal cash-market status"}


def _daily_rows(rows):
    result = {}
    if not isinstance(rows, list):
        raise DataError("daily candle list is unavailable")
    for candle in rows:
        if not isinstance(candle, list) or len(candle) < 6:
            raise DataError("daily candle has invalid shape")
        try:
            raw = candle[0]
            at = (datetime.fromtimestamp(float(raw), IST) if isinstance(raw, (int, float))
                  else datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
            if at.tzinfo is None:
                if isinstance(raw, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                    at = at.replace(tzinfo=IST)
                else:
                    raise ValueError("daily timestamp lacks timezone")
            day = at.astimezone(IST).date()
            o, h, l, c, v = (float(value) for value in candle[1:6])
            if not all(math.isfinite(x) for x in (o, h, l, c, v)) or \
                    l <= 0 or l > min(o, c) or h < max(o, c) or v < 0:
                raise ValueError("invalid daily OHLCV")
        except (ValueError, TypeError, OverflowError, OSError) as exc:
            raise DataError("invalid daily candle values") from exc
        if day in result:
            raise DataError("duplicate daily candle")
        result[day] = {"date": day.isoformat(), "open": o, "high": h,
                       "low": l, "close": c, "volume": v}
    return [result[day] for day in sorted(result)]


def upstox_daily_bars(ticker, isin, token, end_day=None):
    """Read completed daily V3 bars for one cash equity (roughly three months)."""
    if not ticker.endswith((".NS", ".BO")) or not token or \
            not re.fullmatch(r"IN[A-Z0-9]{10}", isin or ""):
        raise DataError("Upstox daily request needs an equity and read-only token")
    end = end_day or datetime.now(IST).date()
    if isinstance(end, str):
        end = datetime.fromisoformat(end).date()
    if end > datetime.now(IST).date():
        raise DataError("daily date cannot be in the future")
    if end == datetime.now(IST).date() and datetime.now(IST).time() < time(15, 30):
        end -= timedelta(days=1)
    segment = "NSE_EQ" if ticker.endswith(".NS") else "BSE_EQ"
    key = urllib.parse.quote(segment + "|" + isin, safe="")
    start = end - timedelta(days=105)
    url = f"{UPSTOX}/{key}/days/1/{end.isoformat()}/{start.isoformat()}"
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise DataError("Upstox daily response exceeds size limit")
    try:
        doc = json.loads(raw)
        if doc.get("status") != "success":
            raise ValueError("daily API failed")
        return _daily_rows(doc["data"]["candles"])
    except (ValueError, TypeError, KeyError) as exc:
        raise DataError("unusable Upstox daily candles") from exc


def fyers_daily_bars(ticker, app_id, token, end_day=None):
    """Read FYERS completed daily NSE cash candles, without order APIs."""
    if not ticker.endswith(".NS") or symbol(ticker, "NSE") != ticker:
        raise DataError("FYERS daily candles support NSE EQ only")
    if not re.fullmatch(r"[A-Za-z0-9-]{4,30}", app_id or "") or \
            not token or any(c.isspace() for c in token):
        raise DataError("FYERS data credentials unavailable")
    end = end_day or datetime.now(IST).date()
    if isinstance(end, str):
        end = datetime.fromisoformat(end).date()
    if end > datetime.now(IST).date():
        raise DataError("daily date cannot be in the future")
    if end == datetime.now(IST).date() and datetime.now(IST).time() < time(15, 30):
        end -= timedelta(days=1)
    start = end - timedelta(days=105)
    params = {"symbol": "NSE:" + ticker[:-3] + "-EQ", "resolution": "D",
              "date_format": "1", "range_from": start.isoformat(),
              "range_to": end.isoformat(), "cont_flag": "0"}
    url = "https://api-t1.fyers.in/data/history?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "Authorization": app_id + ":" + token})
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise DataError("FYERS daily response exceeds size limit")
    try:
        doc = json.loads(raw)
        if doc.get("s") != "ok":
            raise ValueError("daily API failed")
        return _daily_rows(doc["candles"])
    except (ValueError, TypeError, KeyError) as exc:
        raise DataError("unusable FYERS daily candles") from exc


def fetch_preferred(ticker: str, range_: str = "5d") -> dict:
    """Read-only five-minute research through the shared provider policy."""
    from .market_data_manager import fetch_candles
    return fetch_candles(ticker, range_)
