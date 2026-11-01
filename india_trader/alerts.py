"""IST paper alerts for Indian cash equities. Informational; no order operations."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import json
import io
from pathlib import Path
import re
import sys
import time as time_module

from .budget_manager import BudgetError, clock, ticker
from .core import IST
from .market_scanner import OPEN, ScanError, trading_day
from . import calendar_source


class AlertError(Exception):
    pass


TYPES = {"above", "below", "stop", "target", "pct_move", "time", "opening_bell"}
PRICE_TYPES = {"above", "below", "stop", "target", "pct_move"}
SESSION_END = time(15, 15)  # conservative research cutoff; special sessions unknown
FLAT_BY = time(15, 10)
QUOTE_MAX_AGE = 900


def _number(value, name):
    try:
        out = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AlertError(f"{name} must be a finite positive number") from exc
    if not out.is_finite() or out <= 0 or out > Decimal("1000000000000"):
        raise AlertError(f"{name} must be a finite positive number")
    return out


def _hhmm(value):
    if not re.fullmatch(r"\d{1,2}:\d{2}", str(value or "")):
        raise AlertError("time must be IST HH:MM")
    try:
        return time.fromisoformat(str(value))
    except ValueError as exc:
        raise AlertError("time must be IST HH:MM") from exc


def _date(value):
    try:
        d = datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError as exc:
        raise AlertError("date must be YYYY-MM-DD") from exc
    if d.isoformat() != value:
        raise AlertError("date must be YYYY-MM-DD")
    return d


def _read(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise AlertError(f"invalid JSON at {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise AlertError(f"{p} must contain an object")
    return data


def _write(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def market_clock(now):
    ok, reason = trading_day(now.date())
    calendar = calendar_source.status(now.date())
    opened = ok and OPEN <= now.time() < SESSION_END
    if ok:
        reason = "regular research session" if opened else "before open" if now.time() < OPEN else "after research close"
    next_open = None
    candidate = now.date() if ok and now.time() < OPEN else now.date() + timedelta(days=1)
    for i in range(15):
        d = candidate + timedelta(days=i)
        if trading_day(d)[0]:
            next_open = f"{d.isoformat()}T09:15:00+05:30"
            break
    return {"time_ist": now.isoformat(), "date": now.date().isoformat(),
            "exchange": "NSE", "session_open": opened, "trading_day": ok,
            "opens_ist": "09:15" if ok else None, "research_closes_ist": "15:15" if ok else None,
            "next_open_ist": next_open, "reason": reason,
            "calendar_verified": now.year == 2026 or calendar["verified"],
            "calendar_note": ("Public NSE calendar refreshed; special sessions fail closed"
                              if calendar["verified"] else "Known 2026 NSE normal sessions only; refresh public market calendar for later years"),
            "mode": "PAPER_ONLY"}


def validate(rule):
    if not isinstance(rule, dict):
        raise AlertError("every rule must be an object")
    if not isinstance(rule.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,90}", rule["id"]):
        raise AlertError("rule id must be 1–90 letters, digits, dots, hyphens or underscores")
    typ = rule.get("type")
    if typ not in TYPES:
        raise AlertError(f"unknown rule type {typ!r}")
    if typ in PRICE_TYPES or rule.get("ticker"):
        sym = ticker(rule.get("ticker"))
        if sym != rule.get("ticker"):
            raise AlertError("rule ticker must be canonical .NS or .BO")
    if typ in ("above", "below", "stop", "target"):
        _number(rule.get("price"), "price")
    if typ == "pct_move":
        if _number(rule.get("pct"), "pct") > 1000:
            raise AlertError("pct threshold must be at most 1000")
        if rule.get("direction") not in (None, "up", "down"):
            raise AlertError("direction must be up or down")
        if rule.get("ref") is not None:
            _number(rule["ref"], "reference price")
    if typ == "time":
        at = _hhmm(rule.get("at"))
        if rule.get("before_close") and at > FLAT_BY:
            raise AlertError("before-close alert must be no later than 15:10 IST")
    if rule.get("date") is not None:
        _date(rule["date"])
    if rule.get("rearm") not in (None, True, False):
        raise AlertError("rearm must be true or false")
    return rule


def from_plan(plan, flat_override=None):
    if not isinstance(plan, dict) or plan.get("mode") != "PAPER_PLAN_ONLY" or plan.get("status") != "PLANNED":
        raise AlertError("a saved paper exit plan is required")
    sym = ticker(plan.get("ticker") or plan.get("symbol"))
    date = _date(plan.get("date")).isoformat()
    stop = _number(plan.get("stop"), "plan stop")
    target = _number(plan.get("target"), "plan target")
    if stop >= target:
        raise AlertError("paper plan stop must be below target")
    flat = str(plan.get("flat_by_ist") or plan.get("flat_by") or flat_override or "15:10")
    if _hhmm(flat) > FLAT_BY:
        raise AlertError("plan flat-by exceeds 15:10 IST")
    prefix = sym.lower().replace(".", "-")
    out = [
        {"id": prefix + "-plan-stop", "type": "stop", "ticker": sym,
         "price": float(stop), "date": date, "note": "paper plan stop"},
        {"id": prefix + "-plan-target", "type": "target", "ticker": sym,
         "price": float(target), "date": date, "note": "paper plan target"},
        {"id": prefix + "-flat-by", "type": "time", "ticker": sym,
         "at": flat, "label": "Flat by", "before_close": True, "date": date},
    ]
    return [validate(x) for x in out]


def parse_yahoo(payload, sym):
    try:
        doc = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
        chart = doc["chart"]
        results = chart["result"]
        if not isinstance(results, list) or len(results) != 1:
            raise ValueError("missing chart result")
        meta = results[0]["meta"]
        if meta.get("symbol", "").upper() != sym or meta.get("currency") != "INR":
            raise ValueError("symbol or INR currency mismatch")
        price = _number(meta["regularMarketPrice"], "quote price")
        prev = _number(meta["chartPreviousClose"], "previous close") if meta.get("chartPreviousClose") is not None else _number(meta["previousClose"], "previous close")
        stamp = datetime.fromtimestamp(int(meta["regularMarketTime"]), IST)
    except (ValueError, KeyError, TypeError, IndexError, OverflowError, AlertError) as exc:
        raise AlertError(f"unusable Yahoo INR quote for {sym}") from exc
    return {"ticker": sym, "price": float(price), "prev_close": float(prev),
            "time": stamp.isoformat(), "source": "Yahoo Finance unofficial", "currency": "INR"}


def quotes_for(symbols, offline=None):
    found, errors = {}, []
    if not offline:
        from .market_data_manager import MarketDataManager
        manager = MarketDataManager()
    for sym in dict.fromkeys(symbols):
        if not offline:
            try:
                quote = manager.fetch_quote(sym)
                found[sym] = {**quote, "provider_attempts": quote["attempts"]}
                continue
            except (OSError, ValueError, TypeError, KeyError) as exc:
                errors.append({"ticker": sym, "error": type(exc).__name__})
                continue
        try:
            payload = (Path(offline) / f"yahoo-{sym}.json").read_text(encoding="utf-8")
            found[sym] = parse_yahoo(payload, sym)
            found[sym]["provider_attempts"] = []
        except (OSError, ScanError, AlertError) as exc:
            errors.append({"ticker": sym, "error": str(exc)})
    return found, errors


def _timestamp(raw):
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("timezone missing")
        return stamp.astimezone(IST)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AlertError("quote needs a valid offset-aware time") from exc


def _fresh(quote, now):
    age = (now - _timestamp(quote.get("time"))).total_seconds()
    return 0 <= age <= QUOTE_MAX_AGE


def prices_file(quotes, existing=None, now=None):
    old = existing or {}
    if not isinstance(old.get("prices", {}), dict) or not isinstance(old.get("detail", {}), dict):
        raise AlertError("existing price file has invalid shape")
    prices, detail = dict(old.get("prices", {})), dict(old.get("detail", {}))
    for sym, q in quotes.items():
        prices[sym] = q["price"]
        detail[sym] = {"time": q["time"], "prev_close": q.get("prev_close"),
                       "source": q["source"]}
    return {"mode": "PAPER_ONLY", "as_of": (now or datetime.now(IST)).isoformat(),
            "prices": prices, "detail": detail,
            "note": "Dated unofficial quotes for paper alerts/sizing; check detail.time for freshness"}


def read_quotes(path):
    doc = _read(path)
    if doc is None or not isinstance(doc.get("prices"), dict):
        raise AlertError("saved quote file needs prices and dated detail")
    detail = doc.get("detail")
    if not isinstance(detail, dict):
        raise AlertError("saved quote file has no dated detail")
    out = {}
    for sym, value in doc["prices"].items():
        if sym not in detail or not isinstance(detail[sym], dict):
            continue
        try:
            s = ticker(sym)
            if s != sym:
                continue
            row = detail[sym]
            out[sym] = {"ticker": sym, "price": float(_number(value, "quote price")),
                        "prev_close": float(_number(row["prev_close"], "previous close")) if row.get("prev_close") is not None else None,
                        "time": _timestamp(row["time"]).isoformat(),
                        "source": str(row.get("source") or "saved user quote"), "currency": "INR"}
        except (AlertError, KeyError, BudgetError):
            continue
    return out


def evaluate(rules, quotes, state, now):
    """Return due information-only alerts and a new once-only state."""
    status = market_clock(now)
    old = state.get("rung", {})
    if not isinstance(old, dict):
        raise AlertError("alert state rung must be an object")
    rung = dict(old)
    rings, notes = [], []
    today = now.date().isoformat()
    if not status["trading_day"]:
        return rings, dict(state, rung=rung, last_check=now.isoformat()), [status["reason"]]
    for rule in rules:
        validate(rule)
        if rule.get("date") and rule["date"] != today:
            continue
        rid, typ = rule["id"], rule["type"]
        prior = rung.get(rid)
        rung_today = isinstance(prior, str) and prior[:10] == today
        if typ == "opening_bell":
            hit = status["session_open"]
            repeating = rung_today
            detail = "NSE normal research session opened at 09:15 IST"
        elif typ == "time":
            hit = now.time() >= _hhmm(rule["at"])
            repeating = rung_today
            detail = f"{rule.get('label') or 'Time alert'}: {rule['at']} IST reached"
            if rule.get("ticker"):
                detail += f"; check paper holding, then use broker flatten for {rule['ticker']} if needed"
        else:
            sym = rule["ticker"]
            quote = quotes.get(sym)
            if quote is None:
                notes.append(f"{sym}: no quote; {rid} not evaluated")
                continue
            if not _fresh(quote, now):
                notes.append(f"{sym}: quote stale/future; {rid} not evaluated")
                continue
            price = _number(quote["price"], "quote price")
            if typ == "pct_move":
                ref = _number(rule.get("ref") or quote.get("prev_close"), "previous close/reference") if (rule.get("ref") or quote.get("prev_close")) is not None else None
                if ref is None:
                    notes.append(f"{sym}: no previous close/reference; {rid} not evaluated")
                    continue
                pct = (price / ref - 1) * 100
                direction = rule.get("direction")
                hit = abs(pct) >= _number(rule["pct"], "pct") and (direction is None or
                      (direction == "up" and pct > 0) or (direction == "down" and pct < 0))
                repeating = rung_today
                detail = f"{sym} moved {pct:+.2f}% from INR {ref:.2f} to INR {price:.2f}"
            else:
                level = _number(rule["price"], "price")
                hit = price >= level if typ in ("above", "target") else price <= level
                repeating = prior is not None
                if not hit and rule.get("rearm") and prior is not None:
                    rung.pop(rid, None)
                detail = f"{sym} INR {price:.2f} {'at or above' if typ in ('above', 'target') else 'at or below'} {typ} INR {level:.2f}"
            detail += f" at {quote['time']} ({quote['source']}; delayed/unofficial)"
        if hit and not repeating:
            rung[rid] = now.isoformat()
            rings.append({"id": rid, "type": typ, "ticker": rule.get("ticker"),
                          "time_ist": now.isoformat(), "message": detail,
                          "mode": "PAPER_ONLY", "orders_possible": False})
    return rings, dict(state, rung=rung, last_check=now.isoformat()), notes


def _rules(path):
    doc = _read(path, {"rules": []})
    rows = doc.get("rules")
    if not isinstance(rows, list):
        raise AlertError("alert rules must be a list")
    ids = set()
    for row in rows:
        validate(row)
        if row["id"] in ids:
            raise AlertError("duplicate alert rule id")
        ids.add(row["id"])
    return doc


def _state(path):
    return _read(path, {})


def _holdings(path, plan_path):
    pf = _read(path, {})
    items = pf.get("positions", [])
    if not isinstance(items, list):
        raise AlertError("portfolio positions must be a list")
    syms = [ticker(p.get("ticker") or p.get("symbol")) for p in items if isinstance(p, dict)]
    plan = _read(plan_path, {})
    if plan.get("ticker") or plan.get("symbol"):
        syms.append(ticker(plan.get("ticker") or plan.get("symbol")))
    return list(dict.fromkeys(syms))


def watch_loop(check_fn, now_fn, sleep_fn, emit, every=30, until=time(15, 30), max_checks=None):
    """Foreground paper alert poller; check_fn persists once-only state."""
    if not 5 <= every <= 900 or max_checks is not None and max_checks < 1:
        raise AlertError("watch needs --every 5..900 seconds and positive --max-checks")
    checks = 0
    while True:
        now = now_fn().astimezone(IST)
        market = market_clock(now)
        if not market["trading_day"] or now.time() >= until:
            return checks
        result = check_fn()
        checks += 1
        for ring in result.get("rings", []):
            emit({"kind": "ring", "at": now.isoformat(), **ring})
        for error in result.get("quote_errors", []):
            emit({"kind": "quote_error", "at": now.isoformat(), **error})
        if max_checks is not None and checks >= max_checks:
            return checks
        sleep_fn(every)


def _watch_main(a):
    if a.now:
        raise AlertError("watch uses the live IST clock; --now is for a single replay check")
    until = _hhmm(a.until)
    if until <= OPEN or until > time(15, 30):
        raise AlertError("watch --until must be after 09:15 and no later than 15:30 IST")
    args = ["--rules", a.rules, "--state", a.state, "--prices", a.prices,
            "--plan", a.plan, "--portfolio", a.portfolio, "check", "--json",
            "--source", a.source]
    if a.offline:
        args += ["--offline", a.offline]
    def check_once():
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            rc = main(args)
        try:
            result = json.loads(captured.getvalue())
        except json.JSONDecodeError as exc:
            raise AlertError("paper alert check failed; inspect the error above") from exc
        if rc not in (0, 1) or not isinstance(result, dict) or "rings" not in result:
            raise AlertError("paper alert check returned an invalid result")
        return result
    def emit(event):
        path = Path(a.log)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, allow_nan=False) + "\n")
        if a.json:
            print(json.dumps(event, allow_nan=False), flush=True)
        elif event["kind"] == "ring":
            print("\aALERT " + event["message"], flush=True)
        else:
            print("Quote error: " + event.get("ticker", "?") + ": " + event.get("error", ""), flush=True)
    if not a.json:
        print("Paper alert watch started; keep this window open. No orders will be placed.", flush=True)
    count = watch_loop(check_once, lambda: datetime.now(IST), time_module.sleep, emit,
                       a.every, until, a.max_checks)
    if a.json:
        print(json.dumps({"kind": "watch_end", "checks": count, "mode": "PAPER_ONLY"}), flush=True)
    else:
        print(f"Paper alert watch ended after {count} check(s).", flush=True)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m india_trader alerts", description="IST paper price/time alerts; never an order")
    p.add_argument("--now", help="IST replay clock; offset-aware ISO preferred")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--json", action="store_true", help="structured output")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("clock", help="NSE normal-session status and next opening")
    add = sub.add_parser("add", help="add a paper alert rule, or three rules from the exit plan")
    group = add.add_mutually_exclusive_group(required=True)
    for name in ("above", "below", "stop", "target", "pct"):
        group.add_argument("--" + name)
    group.add_argument("--at", help="IST HH:MM")
    group.add_argument("--opening-bell", action="store_true")
    group.add_argument("--from-plan", action="store_true")
    add.add_argument("--ticker")
    add.add_argument("--symbol", dest="ticker", help="alias for --ticker")
    add.add_argument("--config", default="trader/config.json", help="fallback flat_by for --from-plan")
    add.add_argument("--id")
    add.add_argument("--label")
    add.add_argument("--date")
    add.add_argument("--direction", choices=("up", "down"))
    add.add_argument("--ref")
    add.add_argument("--rearm", action="store_true")
    add.add_argument("--before-close", action="store_true")
    add.add_argument("--note")
    check = sub.add_parser("check", help="check rules once using dated quote data")
    check.add_argument("--offline", help="directory of yahoo-SYMBOL.json saved chart responses")
    check.add_argument("--source", choices=("yahoo", "saved"), default="yahoo")
    check.add_argument("--prices-out", help="write fetched dated quotes here")
    check.add_argument("--saved", action="store_true", help="read --prices only, no network")
    check.add_argument("--no-write", action="store_true")
    watch = sub.add_parser("watch", help="continuously check paper alerts in this foreground window")
    watch.add_argument("--source", choices=("yahoo", "saved"), default="yahoo")
    watch.add_argument("--offline", help="saved Yahoo chart-response folder")
    watch.add_argument("--every", type=float, default=30, help="poll interval in seconds, 5..900")
    watch.add_argument("--until", default="15:30", help="stop by IST HH:MM, no later than 15:30")
    watch.add_argument("--max-checks", type=int, help="optional bound for a local smoke run")
    watch.add_argument("--log", default="trader/state/alert-events.jsonl")
    quote = sub.add_parser("quote", help="get unofficial Yahoo INR quotes")
    quote.add_argument("tickers", nargs="*")
    quote.add_argument("--holdings", action="store_true")
    quote.add_argument("--offline", help="directory of yahoo-SYMBOL.json saved chart responses")
    quote.add_argument("--source", choices=("yahoo", "saved"), default="yahoo")
    quote.add_argument("--write", nargs="?", const="", metavar="PATH", help="merge dated quotes into --prices or PATH")
    sub.add_parser("list", help="show alert rules and once-only state")
    reset = sub.add_parser("reset", help="rearm one rule")
    reset.add_argument("id")
    for command_parser in (sub.choices.values()):
        command_parser.add_argument("--json", dest="command_json", action="store_true",
                                    help="structured output")
        command_parser.add_argument("--now", dest="command_now", help="IST replay clock")
    a = p.parse_args(argv)
    try:
        if a.command_now is not None:
            a.now = a.command_now
        a.json = a.json or a.command_json
        if a.command == "watch":
            return _watch_main(a)
        now = clock(a.now)
        rc = 0
        if a.command == "clock":
            result = market_clock(now)
            rc = 0 if result["session_open"] else 1
        elif a.command == "add":
            doc = _rules(a.rules)
            if a.from_plan:
                cfg = _read(a.config, {})
                new = from_plan(_read(a.plan), cfg.get("flat_by"))
                ids = {r["id"] for r in new}
                doc["rules"] = [r for r in doc["rules"] if r["id"] not in ids] + new
                state = _state(a.state)
                for rid in ids:
                    state.get("rung", {}).pop(rid, None)
                _write(a.state, state)
            else:
                typ = "opening_bell" if a.opening_bell else "time" if a.at else "pct_move" if a.pct is not None else next(name for name in ("above", "below", "stop", "target") if getattr(a, name) is not None)
                sym = ticker(a.ticker) if a.ticker else None
                val = a.pct if typ == "pct_move" else getattr(a, typ) if typ in ("above", "below", "stop", "target") else None
                rid = a.id or (f"{sym.lower().replace('.', '-')}-{typ}-{val}" if sym and val else f"{typ}-{a.at.replace(':', '') if a.at else 'nse'}")
                rule = {"id": rid, "type": typ}
                if sym:
                    rule["ticker"] = sym
                if typ in ("above", "below", "stop", "target"):
                    rule["price"] = float(_number(val, "price"))
                if typ == "pct_move":
                    rule["pct"] = float(_number(val, "pct"))
                    if a.direction:
                        rule["direction"] = a.direction
                    if a.ref:
                        rule["ref"] = float(_number(a.ref, "reference price"))
                if typ == "time":
                    rule.update(at=a.at, label=a.label or "Time alert", before_close=a.before_close)
                if a.rearm:
                    rule["rearm"] = True
                if a.date:
                    rule["date"] = a.date
                if a.note:
                    rule["note"] = a.note
                validate(rule)
                if any(r["id"] == rule["id"] for r in doc["rules"]):
                    raise AlertError("rule id already exists")
                doc["rules"].append(rule)
                new = [rule]
            _write(a.rules, doc)
            result = {"added": new, "mode": "PAPER_ONLY", "orders_possible": False}
        elif a.command == "list":
            result = {"rules": _rules(a.rules)["rules"], "state": _state(a.state), "mode": "PAPER_ONLY"}
        elif a.command == "reset":
            state = _state(a.state)
            if a.id not in {r["id"] for r in _rules(a.rules)["rules"]}:
                raise AlertError("unknown rule id")
            state.get("rung", {}).pop(a.id, None)
            _write(a.state, state)
            result = {"reset": a.id, "mode": "PAPER_ONLY"}
        elif a.command == "quote":
            syms = [ticker(s) for s in a.tickers]
            if a.holdings:
                syms += _holdings(a.portfolio, a.plan)
            syms = list(dict.fromkeys(syms))
            if not syms:
                raise AlertError("quote needs tickers or --holdings")
            if a.source == "saved":
                stored = read_quotes(a.prices)
                quotes, errors = {s: stored[s] for s in syms if s in stored}, [
                    {"ticker": s, "error": "no saved dated quote"} for s in syms if s not in stored]
            else:
                quotes, errors = quotes_for(syms, a.offline)
            for q in quotes.values():
                age = (now - _timestamp(q["time"])).total_seconds() / 60
                q["age_minutes"] = round(age, 1)
                q["fresh_for_alert"] = 0 <= age <= QUOTE_MAX_AGE / 60
            if a.write is not None and quotes:
                destination = a.write or a.prices
                _write(destination, prices_file(quotes, _read(destination), now))
            result = {"quotes": quotes, "errors": errors, "mode": "PAPER_ONLY",
                      "note": "Unofficial delayed INR quotes; no execution or guaranteed alert delivery"}
            rc = 0 if quotes else 1
        else:
            rules = _rules(a.rules)["rules"]
            if not rules:
                raise AlertError("no alert rules; use alerts add")
            syms = list(dict.fromkeys(r["ticker"] for r in rules if r["type"] in PRICE_TYPES and
                     (not r.get("date") or r["date"] == now.date().isoformat())))
            if a.saved or a.source == "saved":
                quotes, errors = read_quotes(a.prices), []
            elif not market_clock(now)["trading_day"]:
                quotes, errors = {}, []
            else:
                quotes, errors = quotes_for(syms, a.offline) if syms else ({}, [])
            rings, state, notes = evaluate(rules, quotes, _state(a.state), now)
            if not a.no_write:
                _write(a.state, state)
                if quotes and not (a.saved or a.source == "saved"):
                    destination = a.prices_out or a.prices
                    _write(destination, prices_file(quotes, _read(destination), now))
            result = {"market": market_clock(now), "rings": rings, "notes": notes,
                      "quote_errors": errors, "quotes": quotes, "mode": "PAPER_ONLY",
                      "orders_possible": False, "notification_delivery": "CLI_ONLY"}
            rc = 0 if rings else 1
        if a.json:
            print(json.dumps(result, indent=2, allow_nan=False))
        elif a.command == "clock":
            print(f"NSE {'OPEN' if result['session_open'] else 'closed'}: {result['time_ist']} ({result['reason']}).")
            if result.get("next_open_ist"):
                print(f"Next normal open: {result['next_open_ist']}.")
            print(result["calendar_note"])
        elif a.command == "quote":
            for sym, q in result["quotes"].items():
                print(f"{sym} INR {q['price']:.2f} at {q['time']} [{q['source']}]; fresh_for_alert={q['fresh_for_alert']}")
            for error in result["errors"]:
                print(f"error: {error['ticker']}: {error['error']}")
            print("Quotes are informational and may be delayed; no order is placed.")
        elif a.command == "check":
            market = result["market"]
            print(f"NSE {'OPEN' if market['session_open'] else 'closed'}: {market['time_ist']} ({market['reason']}).")
            for ring in result["rings"]:
                print("ALERT " + ring["message"])
            if not result["rings"]:
                print("No alert rang.")
            for note in result["notes"]:
                print("note: " + note)
            for error in result["quote_errors"]:
                print(f"error: {error['ticker']}: {error['error']}")
            print("Alerts are information only; paper exits require a separate broker record.")
        elif a.command == "list":
            for rule in result["rules"]:
                print(f"{rule['id']}: {json.dumps(rule, allow_nan=False)}")
            if not result["rules"]:
                print("No alert rules.")
        elif a.command == "add":
            for rule in result["added"]:
                print(f"Added {rule['id']}: {json.dumps(rule, allow_nan=False)}")
        else:
            print(f"Re-armed {result['reset']}.")
        return rc
    except (AlertError, BudgetError, ScanError, OSError, ValueError, TypeError, KeyError, StopIteration) as exc:
        print("alerts: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
