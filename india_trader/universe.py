"""NSE cash-equity universe from the exchange's public securities CSV."""
from __future__ import annotations

import csv
import io
import re
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from .core import DataError, IST, symbol

NSE_EQUITY_CSV = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
BSE_SCRIP_ZIP = "https://www.bseindia.com/downloads/Help/file/scrip.zip"
MAX_BYTES = 2_000_000
MAX_BSE_ZIP_BYTES = 5_000_000
MAX_BSE_CSV_BYTES = 8_000_000
BSE_CASH_GROUPS = {"A", "B", "X", "T", "XT"}


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


def parse_bse_scrip_zip(raw: bytes) -> tuple[list[dict], str]:
    """Read BSE's dated security master in memory; never extract ZIP members."""
    if len(raw) > MAX_BSE_ZIP_BYTES:
        raise DataError("BSE security archive exceeds size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            matches = [info for info in archive.infolist()
                       if re.fullmatch(r"SCRIP/BSE_EQ_SCRIP_(\d{8})\.csv", info.filename,
                                       flags=re.IGNORECASE)]
            if len(matches) != 1:
                raise DataError("BSE archive must contain exactly one dated equity master")
            info = matches[0]
            if info.file_size > MAX_BSE_CSV_BYTES:
                raise DataError("BSE equity master exceeds size limit")
            dated = re.search(r"(\d{2})(\d{2})(\d{4})\.csv$", info.filename, re.IGNORECASE)
            assert dated is not None
            source_date = datetime.strptime("".join(dated.groups()), "%d%m%Y").date().isoformat()
            payload = archive.read(info)
    except (zipfile.BadZipFile, RuntimeError, UnicodeError) as exc:
        raise DataError("Invalid BSE security archive") from exc
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    needed = {"FinInstrmId", "TckrSymb", "SctySrs", "FinInstrmNm", "ISIN",
              "NewBrdLotQty", "SctyTpFlg", "Sts"}
    if not reader.fieldnames or not needed.issubset(reader.fieldnames):
        raise DataError("Unexpected BSE equity master headers")
    result, seen = [], set()
    for row in reader:
        if None in row:
            raise DataError("Malformed BSE equity master row")
        if (row["SctyTpFlg"].strip() != "EQ" or row["Sts"].strip() != "A" or
            row["SctySrs"].strip() not in BSE_CASH_GROUPS or
            row["NewBrdLotQty"].strip() != "1" or "#" in row["TckrSymb"]):
            continue
        code = row["FinInstrmId"].strip()
        isin = row["ISIN"].strip()
        if not re.fullmatch(r"\d{6}", code) or not re.fullmatch(r"IN[A-Z0-9]{10}", isin):
            raise DataError("Invalid BSE equity code or ISIN")
        ticker = symbol(code, "BSE")
        if ticker in seen:
            raise DataError("Duplicate BSE equity code")
        seen.add(ticker)
        result.append({"ticker": ticker, "name": row["FinInstrmNm"].strip(),
                       "isin": isin, "series": row["SctySrs"].strip(),
                       "source_date": source_date})
    if not result:
        raise DataError("BSE equity master has no eligible cash equities")
    return result, source_date


def load_bse_equities(zip_path: str | None = None) -> tuple[list[dict], str, str]:
    if zip_path:
        raw = Path(zip_path).read_bytes()
        rows, date = parse_bse_scrip_zip(raw)
        return rows, str(Path(zip_path)), date
    req = urllib.request.Request(BSE_SCRIP_ZIP, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read(MAX_BSE_ZIP_BYTES + 1)
    rows, date = parse_bse_scrip_zip(raw)
    return rows, BSE_SCRIP_ZIP, date


def write_universe(path: str, rows: list[dict], source: str) -> dict:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(f"{x['ticker']}|{x['name']}|{x['isin']}\n" for x in rows), encoding="utf-8")
    return {"mode": "NSE_EQ_UNIVERSE", "source": source, "retrieved_at": datetime.now(IST).isoformat(),
            "count": len(rows), "path": str(dest), "coverage": "NSE EQ series only; BSE and other series excluded"}
