"""NSE cash-equity universe from the exchange's public securities CSV."""
from __future__ import annotations

import csv
import io
import urllib.request
from datetime import datetime
from pathlib import Path

from .core import DataError, IST, symbol

NSE_EQUITY_CSV = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
MAX_BYTES = 2_000_000


def parse_nse_equities(raw: bytes) -> list[dict]:
    if len(raw) > MAX_BYTES:
        raise DataError("NSE equity list exceeds size limit")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    needed = {"SYMBOL", "NAME OF COMPANY", "SERIES", "ISIN NUMBER"}
    if not reader.fieldnames or not needed.issubset({h.strip() for h in reader.fieldnames}):
        raise DataError("Unexpected NSE equity list headers")
    result, seen = [], set()
    for row in reader:
        if None in row:
            raise DataError("Malformed NSE equity list row")
        item = {k.strip(): (v or "").strip() for k, v in row.items()}
        if item["SERIES"] != "EQ":
            continue
        try:
            ticker = symbol(item["SYMBOL"], "NSE")
        except ValueError as exc:
            raise DataError(f"Invalid NSE symbol in equity list: {item['SYMBOL']}") from exc
        isin = item["ISIN NUMBER"]
        if len(isin) != 12 or not isin.isalnum() or not isin.startswith("IN"):
            raise DataError(f"Invalid ISIN for {ticker}")
        if ticker in seen:
            raise DataError(f"Duplicate NSE symbol: {ticker}")
        seen.add(ticker)
        result.append({"ticker": ticker, "name": item["NAME OF COMPANY"], "isin": isin})
    if not result:
        raise DataError("NSE equity list has no EQ securities")
    return result


def load_nse_equities(csv_path: str | None = None) -> tuple[list[dict], str]:
    if csv_path:
        raw = Path(csv_path).read_bytes()
        return parse_nse_equities(raw), str(Path(csv_path))
    req = urllib.request.Request(NSE_EQUITY_CSV, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv"})
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read(MAX_BYTES + 1)
    return parse_nse_equities(raw), NSE_EQUITY_CSV


def write_universe(path: str, rows: list[dict], source: str) -> dict:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(f"{x['ticker']}|{x['name']}|{x['isin']}\n" for x in rows), encoding="utf-8")
    return {"mode": "NSE_EQ_UNIVERSE", "source": source, "retrieved_at": datetime.now(IST).isoformat(),
            "count": len(rows), "path": str(dest), "coverage": "NSE EQ series only; BSE and other series excluded"}
