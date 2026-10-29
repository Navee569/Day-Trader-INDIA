"""Bounded watchlist scanner; offline CSV-first, network is explicitly opt-in."""
from __future__ import annotations
from pathlib import Path
from .core import DataError, orb_signal, read_csv, symbol
from .intraday_sources import fetch_preferred


def scan_csv_folder(directory: str, as_of=None, date=None, limit=100):
    folder = Path(directory)
    if not folder.is_dir():
        raise DataError("--folder must be an existing directory")
    paths = sorted(folder.glob("*.csv"))
    if not paths:
        raise DataError("No CSV files in folder")
    if len(paths) > limit:
        raise DataError(f"Too many files; maximum {limit}")
    results = []
    for p in paths:
        try:
            result = orb_signal(read_csv(p), as_of=as_of, date=date)
            results.append({"file":p.name, **result})
        except (ValueError, OSError) as exc:
            results.append({"file":p.name,"status":"DATA_ERROR","detail":str(exc)})
    return {"mode":"OFFLINE_CSV_SCAN", "count":len(results),
            "candidates":sum(x['status']=='TRIGGERED_LONG' for x in results),
            "results":results}


def scan_watchlist(file: str, as_of=None, date=None, max_symbols=10):
    path = Path(file)
    if path.stat().st_size > 100_000:
        raise DataError("Watchlist too large")
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    if len(lines) > max_symbols:
        raise DataError(f"Watchlist has {len(lines)} tickers; maximum {max_symbols} per run")
    normalized = []
    for name in lines:
        try:
            normalized.append(symbol(name, "BSE" if name.upper().endswith(".BO") else "NSE"))
        except ValueError:
            # Preserve per-item DATA_ERROR reporting for invalid symbols.
            normalized.append(name.strip().upper())
    if len(set(normalized)) != len(normalized):
        raise DataError("Watchlist has duplicate entries after exchange normalization")
    output = []
    for name in lines:
        try:
            e = "BSE" if name.upper().endswith(".BO") else "NSE"
            ticker = symbol(name, e)
            fetched = fetch_preferred(ticker)
            result = orb_signal(fetched["bars"], as_of=as_of, date=date)
            output.append({"ticker":ticker, **result,
                           "data_origin": fetched["source"], "provider_attempts": fetched["attempts"]})
        except (ValueError, OSError) as exc:
            output.append({"ticker":name, "status":"DATA_ERROR", "detail":str(exc)})
    return {"mode":"MULTISOURCE_WATCHLIST_RESEARCH_ONLY", "count":len(output),
            "candidates":sum(x['status']=='TRIGGERED_LONG' for x in output),
            "results":output}
