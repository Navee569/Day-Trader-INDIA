"""Read-only current-year NSE/BSE cash-session calendar from public market data."""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import sys
import urllib.request

from .core import IST

URL = "https://api.upstox.com/v2/market/holidays"
DEFAULT_CACHE = Path("trader/state/market-calendar.json")
MAX_BYTES = 512_000
MAX_AGE = timedelta(days=7)


class CalendarError(ValueError):
    pass


def parse_holidays(raw, year):
    try:
        doc = json.loads(raw)
        if doc["status"] != "success" or not isinstance(doc["data"], list):
            raise ValueError("invalid holiday response")
        events = {}
        for item in doc["data"]:
            day = date.fromisoformat(item["date"])
            if day.year != year or day.isoformat() in events:
                raise ValueError("duplicate or wrong-year holiday date")
            closed = item.get("closed_exchanges") or []
            opened = item.get("open_exchanges") or []
            if not isinstance(closed, list) or not isinstance(opened, list):
                raise ValueError("invalid exchange list")
            times = {}
            for exchange in opened:
                name = exchange["exchange"]
                if name not in ("NSE", "BSE"):
                    continue
                start = datetime.fromtimestamp(int(exchange["start_time"]) / 1000, IST)
                end = datetime.fromtimestamp(int(exchange["end_time"]) / 1000, IST)
                if start.date() != day or end.date() != day or end <= start:
                    raise ValueError("invalid market timing")
                times[name] = {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
            events[day.isoformat()] = {"description": str(item.get("description", ""))[:160],
                                       "type": str(item.get("holiday_type", "")),
                                       "closed": [x for x in closed if x in ("NSE", "BSE")],
                                       "open": times}
        if not events:
            raise ValueError("empty holiday list")
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise CalendarError("invalid public market holiday response") from exc
    return events


def refresh(cache=DEFAULT_CACHE, offline=None, now=None):
    now = now or datetime.now(IST)
    if offline:
        source = Path(offline)
        if not source.is_file() or source.stat().st_size > MAX_BYTES:
            raise CalendarError("offline holiday response missing or too large")
        raw = source.read_bytes()
        origin = "offline public holiday response"
    else:
        req = urllib.request.Request(URL, headers={"Accept": "application/json",
                                                  "User-Agent": "IndiaTraderPaperResearch/0.2.1"})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                raw = response.read(MAX_BYTES + 1)
        except OSError as exc:
            raise CalendarError("public holiday source unavailable") from exc
        origin = URL
    if len(raw) > MAX_BYTES:
        raise CalendarError("holiday response exceeds size limit")
    doc = {"year": now.year, "retrieved_at": now.isoformat(), "source": origin,
           "events": parse_holidays(raw, now.year)}
    path = Path(cache)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    tmp.replace(path)
    return doc


def load(cache=DEFAULT_CACHE, now=None):
    now = now or datetime.now(IST)
    path = Path(cache)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(doc["retrieved_at"])
        if stamp.tzinfo is None or stamp > now or now - stamp > MAX_AGE or \
                doc["year"] != now.year or not isinstance(doc["events"], dict):
            return None
        return doc
    except (ValueError, TypeError, KeyError):
        return None


def status(day, exchange="NSE", cache=DEFAULT_CACHE, now=None):
    if exchange not in ("NSE", "BSE"):
        raise CalendarError("calendar exchange must be NSE or BSE")
    now = now or datetime.now(IST)
    doc = load(cache, now)
    if not doc or doc["year"] != day.year:
        return {"state": "UNKNOWN", "reason": "current-year public market calendar not refreshed",
                "verified": False}
    item = doc["events"].get(day.isoformat())
    if item and exchange in item["closed"]:
        return {"state": "CLOSED", "reason": item["description"], "verified": True}
    if item:
        hours = item["open"].get(exchange)
        if not hours:
            return {"state": "UNKNOWN", "reason": "holiday exchange status missing", "verified": False}
        if hours != {"start": "09:15", "end": "15:30"} or item["type"] == "SPECIAL_TIMING":
            return {"state": "SPECIAL", "reason": item["description"], "verified": True,
                    "hours": hours}
    if day.weekday() >= 5:
        return {"state": "CLOSED", "reason": "weekend", "verified": True}
    return {"state": "NORMAL", "reason": "normal cash session", "verified": True,
            "hours": {"start": "09:15", "end": "15:30"}}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m india_trader market-calendar")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("refresh", help="save current-year public NSE/BSE holiday data")
    r.add_argument("--offline", help="saved official response for replay")
    r.add_argument("--cache", default=str(DEFAULT_CACHE))
    s = sub.add_parser("status", help="show cached normal/closed/special status")
    s.add_argument("--date", default=datetime.now(IST).date().isoformat())
    s.add_argument("--exchange", choices=("NSE", "BSE"), default="NSE")
    s.add_argument("--cache", default=str(DEFAULT_CACHE))
    args = parser.parse_args(argv)
    try:
        if args.command == "refresh":
            result = refresh(args.cache, args.offline)
            print(json.dumps({"status": "OK", "year": result["year"],
                              "events": len(result["events"]), "source": result["source"]}))
            return 0
        result = status(date.fromisoformat(args.date), args.exchange, args.cache)
        print(json.dumps({"date": args.date, "exchange": args.exchange, **result}))
        return 0 if result["state"] == "NORMAL" else 1
    except (CalendarError, OSError, ValueError) as exc:
        print("market-calendar: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
