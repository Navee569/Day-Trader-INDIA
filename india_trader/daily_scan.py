"""NSE-wide daily setup scan from final CM Bhavcopy reports, paper research only."""
from __future__ import annotations

import urllib.error
from collections import defaultdict
from datetime import date, datetime, timedelta

from .core import IST, number
from .nse_sources import bhavcopy_day


def rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder RSI, matching the upstream daily scanner's arithmetic."""
    if len(closes) < n + 1:
        return None
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag, al = (ag * (n - 1) + gains[i]) / n, (al * (n - 1) + losses[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def find_setups(closes: list[float]) -> tuple[list[str], dict]:
    """Use the original repo's five daily setup rules on completed daily closes."""
    if len(closes) < 30:
        raise ValueError("At least 30 completed daily closes are required")
    last = closes[-1]
    hi20 = max(closes[-21:-1])
    ma20 = sum(closes[-20:]) / 20
    ma20_prev = sum(closes[-21:-1]) / 20
    run = closes[-6] / closes[-16] - 1
    range5 = (max(closes[-5:]) - min(closes[-5:])) / last
    current_rsi, prior_rsi = rsi(closes), rsi(closes[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and range5 < 0.05:
        tags.append("bull_flag")
    if closes[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if current_rsi is not None and current_rsi < 30:
        tags.append("oversold")
    if prior_rsi is not None and prior_rsi < 30 and last > closes[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": current_rsi, "rsi_prev": prior_rsi, "high20": hi20,
                  "ma20": ma20, "run_pct": run * 100, "range5_pct": range5 * 100}


def scan_rows(reports: list[dict], min_price: float = 20, min_volume: float = 100000,
              max_hits: int = 50) -> dict:
    """Pure scan over dated final-report objects, oldest or newest first."""
    min_price = number(min_price, "minimum price", allow_zero=True)
    min_volume = number(min_volume, "minimum volume", allow_zero=True)
    if max_hits < 1 or max_hits > 5000:
        raise ValueError("max_hits must be 1 through 5000")
    by_symbol = defaultdict(list)
    seen_dates = set()
    for report in sorted(reports, key=lambda x: x["date"]):
        d = date.fromisoformat(report["date"])
        if d in seen_dates:
            raise ValueError("Duplicate Bhavcopy trading date")
        seen_dates.add(d)
        if report.get("frequency") != "daily":
            raise ValueError("Daily scanner requires final daily reports")
        for row in report["rows"]:
            if row["date"] != report["date"]:
                raise ValueError("Bhavcopy row date mismatch")
            by_symbol[row["symbol"]].append(row)
    if not seen_dates:
        return {"status": "NO_DATA", "reports": 0, "hits": [], "total_hits": 0}
    last_date = max(seen_dates).isoformat()
    hits = []
    counts = {"symbols_seen": len(by_symbol), "short_history": 0, "illiquid": 0,
              "stale_symbol": 0, "scanned": 0}
    for sym, rows in by_symbol.items():
        if rows[-1]["date"] != last_date:
            counts["stale_symbol"] += 1
            continue
        if len(rows) < 30:
            counts["short_history"] += 1
            continue
        last = rows[-1]
        if last["close"] < min_price or last["volume"] < min_volume:
            counts["illiquid"] += 1
            continue
        closes = [r["close"] for r in rows]
        tags, metrics = find_setups(closes)
        counts["scanned"] += 1
        if tags:
            hits.append({"symbol": sym, "date": last_date, "setups": tags,
                         "close_inr": last["close"], "volume": last["volume"],
                         "rsi": round(metrics["rsi"], 1) if metrics["rsi"] is not None else None,
                         "metrics": metrics})
    hits.sort(key=lambda h: (-(h["close_inr"] * h["volume"]), h["symbol"]))
    return {"status": "OK" if len(seen_dates) >= 30 else "PARTIAL_HISTORY",
            "source": "NSE final CM-UDiFF Bhavcopy", "mode": "RESEARCH_ONLY",
            "bar_interval": "1d", "latest_report": last_date, "reports": len(seen_dates),
            "counts": counts, "total_hits": len(hits), "hits": hits[:max_hits],
            "truncated_hits": max(0, len(hits) - max_hits)}


def fetch_recent_reports(as_of: str | None = None, count: int = 35) -> tuple[list[dict], list[dict]]:
    """Fetch a bounded number of daily archives; record missing/failed days."""
    end = date.fromisoformat(as_of) if as_of else datetime.now(IST).date()
    if end > datetime.now(IST).date():
        raise ValueError("Daily scan date cannot be in the future")
    if not 30 <= count <= 60:
        raise ValueError("Report count must be 30 through 60")
    reports, skipped = [], []
    cursor = end
    for _ in range(count * 2 + 12):
        if len(reports) >= count:
            break
        if cursor.weekday() < 5:
            try:
                reports.append(bhavcopy_day(cursor.isoformat()))
            except urllib.error.HTTPError as exc:
                skipped.append({"date": cursor.isoformat(), "reason": f"HTTP_{exc.code}"})
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                skipped.append({"date": cursor.isoformat(), "reason": type(exc).__name__})
        cursor -= timedelta(days=1)
    return reports, skipped


def scan_nse_daily(as_of: str | None = None, count: int = 35,
                   min_price: float = 20, min_volume: float = 100000,
                   max_hits: int = 50) -> dict:
    reports, skipped = fetch_recent_reports(as_of, count)
    result = scan_rows(reports, min_price, min_volume, max_hits)
    result["requested_reports"] = count
    result["skipped_dates"] = skipped
    if len(reports) < count and result["status"] == "OK":
        result["status"] = "PARTIAL_HISTORY"
    return result
