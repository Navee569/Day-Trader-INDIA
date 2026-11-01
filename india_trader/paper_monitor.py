"""Foreground scheduled paper research checks; no broker operations."""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
import json
from pathlib import Path
import subprocess
import sys
import time as time_module

from .core import IST
from .market_scanner import trading_day

STATE = Path("trader/state/paper-monitor.json")
EVENTS = Path("trader/state/paper-monitor-events.jsonl")
INTERVALS = {"calendar": 86400, "alerts": 30, "news": 900,
             "insiders": 1800, "earnings": 21600}


class MonitorError(ValueError):
    pass


def symbols_from(path):
    source = Path(path)
    if not source.is_file():
        raise MonitorError("paper monitor needs a watchlist file or --symbol")
    symbols = []
    for line in source.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip().upper().removesuffix(".NS")
        if not value:
            continue
        from .insider_tracker import _symbol
        symbols.append(_symbol(value))
    if not symbols or len(set(symbols)) > 20:
        raise MonitorError("paper monitor needs 1..20 NSE cash symbols")
    return list(dict.fromkeys(symbols))


def jobs(symbols, bse_codes, now, alert_rules):
    rows = [("calendar", ["market-calendar", "refresh"])]
    if trading_day(now.date())[0] and time(9, 15) <= now.time() < time(15, 15) and Path(alert_rules).is_file():
        rows.append(("alerts", ["alerts", "--rules", str(alert_rules), "check", "--json"]))
    rows.append(("news", ["news", "--tickers", *symbols, "--json"]))
    for sym in symbols:
        rows.append(("insiders:" + sym, ["insider-tracker", "new", "--symbol", sym,
                                          "--read", "--json"]))
    for code in bse_codes:
        rows.append(("insiders:bse:" + code, ["insider-tracker", "new", "--exchange", "BSE",
                                                "--symbol", code, "--read", "--json"]))
    rows.append(("earnings", ["earnings-analyst", *symbols, "--no-filings", "--json"]))
    return rows


def command(args):
    try:
        done = subprocess.run([sys.executable, "-m", "india_trader", *args],
                              capture_output=True, text=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MonitorError("scheduled check unavailable: " + type(exc).__name__) from exc
    if done.returncode not in (0, 1) or not done.stdout.strip():
        reason = ("public source unavailable" if "source unavailable" in done.stderr.lower() or
                  "getaddrinfo" in done.stderr.lower() else "CLI returned no usable result")
        raise MonitorError("scheduled check failed: " + args[0] + " (" + reason + ")")
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise MonitorError("scheduled check returned invalid JSON: " + args[0]) from exc


def _read_state(path):
    p = Path(path)
    if not p.is_file():
        return {"last_attempt": {}, "earnings_seen": {}}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("last_attempt"), dict) or \
            not isinstance(data.get("earnings_seen"), dict):
        raise MonitorError("invalid paper monitor state")
    return data


def _save(path, doc):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(target)


def _event(log, event):
    target = Path(log)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, allow_nan=False) + "\n")


def check_once(now, symbols, bse_codes=(), state=STATE, log=EVENTS,
               alert_rules="trader/alerts.json", run=command, force=False):
    """Run due checks; each result is local, dated and informational."""
    now = now.astimezone(IST)
    doc = _read_state(state)
    events = []
    for name, args in jobs(symbols, bse_codes, now, alert_rules):
        interval = INTERVALS[name.split(":", 1)[0]]
        last = doc["last_attempt"].get(name)
        if not force and last:
            try:
                if now - datetime.fromisoformat(last) < timedelta(seconds=interval):
                    continue
            except ValueError:
                pass
        doc["last_attempt"][name] = now.isoformat()
        try:
            result = run(args)
            if name == "calendar":
                pass
            elif name == "alerts":
                events.extend({"kind": "paper_alert", "at": now.isoformat(), "message": ring["message"]}
                              for ring in result.get("rings", []))
            elif name == "news":
                events.extend({"kind": "news", "at": now.isoformat(), "title": item.get("title"),
                               "url": item.get("url"), "ticker": item.get("ticker")}
                              for item in result.get("items", []) if item.get("new"))
            elif name.startswith("insiders:"):
                events.extend({"kind": "insider_filing", "at": now.isoformat(), "id": filing["id"],
                               "symbol": filing["symbol"], "filed_at": filing["filed_at"],
                               "url": filing.get("view_url") or filing.get("xbrl_url")}
                              for filing in result.get("filings", []) if "read_error" not in filing)
            elif name == "earnings":
                for row in result.get("results", []):
                    latest = (row.get("rows") or [{}])[0]
                    key = latest.get("filing_url") or latest.get("filed_at")
                    symbol = row.get("ticker")
                    if key and symbol:
                        previous = doc["earnings_seen"].get(symbol)
                        if previous and previous != key:
                            events.append({"kind": "earnings_filing", "at": now.isoformat(),
                                           "symbol": symbol, "filed_at": latest.get("filed_at"), "url": key})
                        doc["earnings_seen"][symbol] = key
        except (MonitorError, OSError, ValueError, KeyError, TypeError) as exc:
            events.append({"kind": "source_error", "at": now.isoformat(), "job": name,
                           "error": str(exc)[:200]})
    _save(state, doc)
    for event in events:
        _event(log, event)
    return events


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m india_trader paper-monitor",
                                     description="Scheduled foreground paper checks; never sends orders")
    parser.add_argument("command", choices=("once", "watch"))
    parser.add_argument("--symbol", action="append", default=[], help="NSE symbol, repeatable")
    parser.add_argument("--watchlist", default="paper_watchlist.txt")
    parser.add_argument("--bse-code", action="append", default=[])
    parser.add_argument("--state", default=str(STATE))
    parser.add_argument("--log", default=str(EVENTS))
    parser.add_argument("--alert-rules", default="trader/alerts.json")
    parser.add_argument("--force", action="store_true", help="run all due sources for one smoke check")
    parser.add_argument("--max-cycles", type=int, help="bound foreground watch for a smoke check")
    parser.add_argument("--json", action="store_true")
    a = parser.parse_args(argv)
    try:
        from .insider_tracker import _symbol
        from .bse_insiders import bse_code
        symbols = list(dict.fromkeys(_symbol(s) for s in a.symbol)) if a.symbol else symbols_from(a.watchlist)
        codes = list(dict.fromkeys(bse_code(s) for s in a.bse_code))
        if len(symbols) > 20 or len(codes) > 20 or a.max_cycles is not None and a.max_cycles < 1:
            raise MonitorError("watchlists are limited to 20 symbols and max-cycles must be positive")
        if a.command == "watch" and not a.json:
            print("Paper monitor started for " + ", ".join(symbols) +
                  "; press Ctrl+C to stop. Events are saved in " + a.log + ". No orders are placed.", flush=True)
        cycles = 0
        while True:
            now = datetime.now(IST)
            events = check_once(now, symbols, codes, a.state, a.log, a.alert_rules, force=a.force)
            for event in events:
                print(json.dumps(event, allow_nan=False) if a.json else
                      "PAPER " + event["kind"] + ": " + str(event.get("message") or event.get("title") or
                                                         event.get("symbol") or event.get("job")), flush=True)
            cycles += 1
            if a.command == "once" or a.max_cycles is not None and cycles >= a.max_cycles or now.time() >= time(16):
                if a.json:
                    print(json.dumps({"kind": "monitor_end", "cycles": cycles, "events": len(events),
                                      "mode": "PAPER_ONLY"}))
                return 0
            time_module.sleep(30)
    except (MonitorError, OSError, ValueError, TypeError, KeyError) as exc:
        print("paper-monitor: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
