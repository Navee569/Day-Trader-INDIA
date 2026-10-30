"""Official NSE cash-market research sources. Read-only and paper-only."""
from __future__ import annotations

import csv
import io
import json
import math
import re
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, time

from .core import IST
from .market_calendar import NSE_2026_CLOSED

BHAVCOPY = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{day}_F_0000.csv.zip"
CM_LIVE = "https://mcp.nseindia.in/cmmkt/mcp"
MAX_ZIP = 8 * 1024 * 1024
MAX_CSV = 32 * 1024 * 1024


def bhavcopy_day(day: str, ticker: str | None = None) -> dict:
    """Read one final daily CM report. It is never an intraday candle feed."""
    requested = date.fromisoformat(day)
    if requested > datetime.now(IST).date():
        raise ValueError("Bhavcopy date cannot be in the future")
    if ticker is not None and not re.fullmatch(r"[A-Z0-9&._-]{1,30}", ticker.upper()):
        raise ValueError("Invalid NSE ticker")
    url = BHAVCOPY.format(day=requested.strftime("%Y%m%d"))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (IndiaDayTraderPaper/0.2)"})
    with urllib.request.urlopen(req, timeout=20) as response:
        payload = response.read(MAX_ZIP + 1)
    if len(payload) > MAX_ZIP:
        raise ValueError("NSE Bhavcopy ZIP exceeds size limit")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = archive.infolist()
        if len(members) != 1 or not members[0].filename.lower().endswith(".csv"):
            raise ValueError("Unexpected Bhavcopy archive contents")
        if members[0].file_size > MAX_CSV:
            raise ValueError("NSE Bhavcopy CSV exceeds size limit")
        with archive.open(members[0]) as stream:
            reader = csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig", newline=""))
            required = {"TradDt", "Sgmt", "Src", "FinInstrmTp", "TckrSymb", "SctySrs", "OpnPric", "HghPric", "LwPric", "ClsPric", "TtlTradgVol"}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError("NSE Bhavcopy columns have changed")
            rows = []
            for row in reader:
                if row["TradDt"] != day:
                    raise ValueError("Bhavcopy trade date does not match requested date")
                if row["Sgmt"] != "CM" or row["Src"] != "NSE" or row["FinInstrmTp"] != "STK" or row["SctySrs"] != "EQ":
                    continue
                if ticker and row["TckrSymb"].upper() != ticker.upper():
                    continue
                prices = {key: float(row[column]) for key, column in
                          (("open", "OpnPric"), ("high", "HghPric"),
                           ("low", "LwPric"), ("close", "ClsPric"))}
                volume = int(row["TtlTradgVol"])
                if (not all(math.isfinite(value) and value > 0 for value in prices.values())
                        or prices["low"] > min(prices["open"], prices["close"])
                        or prices["high"] < max(prices["open"], prices["close"])
                        or prices["low"] > prices["high"] or volume < 0):
                    raise ValueError("Invalid NSE Bhavcopy OHLCV values")
                rows.append({"symbol": row["TckrSymb"], "date": day, **prices, "volume": volume})
    return {"source": "NSE official CM-UDiFF final Bhavcopy", "source_url": url,
            "frequency": "daily", "mode": "RESEARCH_ONLY", "date": day,
            "count": len(rows), "rows": rows}


def _mcp_post(method: str, params: dict, session: str | None = None) -> tuple[dict, str | None]:
    body = {"jsonrpc": "2.0", "method": method}
    if not method.startswith("notifications/"):
        body["id"] = 1
        body["params"] = params
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-03-26"}
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(CM_LIVE, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(2_000_000).decode("utf-8", "replace")
        if not raw.strip():
            return {}, response.headers.get("Mcp-Session-Id")
        if "text/event-stream" in response.headers.get("Content-Type", ""):
            events = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
            if not events:
                raise ValueError("NSE MCP returned no result event")
            raw = events[-1]
        return json.loads(raw), response.headers.get("Mcp-Session-Id")


def cm_live_quote(ticker: str, now: datetime | None = None) -> dict:
    """Get NSE CM Market Live quote if its MCP session is operational."""
    if not re.fullmatch(r"[A-Z0-9&._-]{1,30}", ticker.upper()):
        raise ValueError("Invalid NSE ticker")
    clock = now or datetime.now(IST)
    if clock.tzinfo is None:
        raise ValueError("Market clock must include a timezone")
    local = clock.astimezone(IST)
    if (local.weekday() >= 5 or local.date() in NSE_2026_CLOSED or
            not (time(9, 15) <= local.time() < time(15, 30))):
        return {"status": "MARKET_CLOSED", "source": "NSE CM Market Live MCP",
                "mode": "RESEARCH_ONLY", "symbol": ticker.upper(),
                "checked_at": local.isoformat(), "quote": None,
                "note": "2026 published NSE EQ holidays checked; special sessions and ad hoc closures are not checked"}
    if local.year != 2026:
        return {"status": "UNAVAILABLE", "source": "NSE CM Market Live MCP",
                "mode": "RESEARCH_ONLY", "symbol": ticker.upper(),
                "reason": "Exchange calendar year unverified", "quote": None}
    try:
        init, session = _mcp_post("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                "clientInfo": {"name": "india-day-trader-paper", "version": "0.2.1"}})
        if "error" in init or not session:
            raise ValueError("NSE CM Market Live MCP initialization failed")
        _mcp_post("notifications/initialized", {}, session)
        listed, _ = _mcp_post("tools/list", {}, session)
        tools = listed.get("result", {}).get("tools", [])
        candidates = [t for t in tools if "quote" in t.get("name", "").lower() and
                      isinstance(t.get("inputSchema", {}).get("properties"), dict)]
        for tool in candidates:
            props = tool["inputSchema"]["properties"]
            key = next((k for k in ("symbol", "ticker", "stockSymbol", "stock_symbol") if k in props), None)
            if key and set(tool["inputSchema"].get("required", [])) <= {key}:
                result, _ = _mcp_post("tools/call", {"name": tool["name"], "arguments": {key: ticker.upper()}}, session)
                if "error" in result or result.get("result", {}).get("isError"):
                    raise ValueError("NSE CM Market Live quote tool returned an error")
                if not result.get("result", {}).get("content"):
                    raise ValueError("NSE CM Market Live quote tool returned no content")
                return {"status": "OK", "source": "NSE CM Market Live MCP", "mode": "RESEARCH_ONLY",
                        "delay_note": "NSE says prices are about 1-3 minutes behind real time",
                        "symbol": ticker.upper(), "fetched_at": datetime.now(IST).isoformat(),
                        "quote": result.get("result")}
        raise ValueError("NSE CM Market Live quote tool could not be identified")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "UNAVAILABLE", "source": "NSE CM Market Live MCP", "mode": "RESEARCH_ONLY",
                "symbol": ticker.upper(), "reason": str(exc), "quote": None}
