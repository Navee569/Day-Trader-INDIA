"""India entry timer adapted from upstream MIT trader-entry, paper research only."""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
import gzip
import hashlib
import json
import os
import math
from pathlib import Path
import sys
import time as clocktime
import urllib.error
import urllib.parse
import urllib.request

from .core import IST, symbol
from . import calendar_source
from .market_scanner import trading_day

OPEN, END = time(9, 15), time(15, 15)  # conservative CAS-safe continuous-bar end
DEFAULT_BEFORE, DEFAULT_AFTER = time(9, 30), time(15, 10)
MAX_BYTES = 8_000_000
HOSTS = ("query2", "query1")


class EntryError(Exception):
    pass


def ticker(raw):
    s = str(raw).strip().upper()
    normalized = symbol(s, "BSE" if s.endswith(".BO") or s.isdigit() else "NSE")
    return normalized


def hhmm(raw):
    try:
        value = time.fromisoformat(raw)
        if value.second or value.microsecond:
            raise ValueError
        return value
    except (TypeError, ValueError) as exc:
        raise EntryError("time must be HH:MM in IST") from exc


def parse_now(raw):
    if not raw:
        return datetime.now(IST)
    text = raw.strip().replace("Z", "+00:00")
    if len(text) == 5 and text[2] == ":":
        text = datetime.now(IST).date().isoformat() + "T" + text
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise EntryError("--now needs ISO date/time in IST or an offset-aware ISO time") from exc
    return (value.replace(tzinfo=IST) if value.tzinfo is None else value).astimezone(IST)


def config(path):
    settings = {"no_entries_before": DEFAULT_BEFORE, "no_entries_after": DEFAULT_AFTER}
    p = Path(path)
    if p.is_file():
        doc = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise EntryError("config must be a JSON object")
        for key in settings:
            if key in doc:
                settings[key] = hhmm(doc[key])
    if not (OPEN <= settings["no_entries_before"] < settings["no_entries_after"] <= DEFAULT_AFTER):
        raise EntryError("entry window must stay within 09:15–15:10 IST and start before cutoff")
    return settings


def _read_offline(path, sym):
    p = Path(path)
    if p.is_dir():
        matches = [p / (sym + ext) for ext in (".json", ".json.gz")]
        p = next((x for x in matches if x.is_file()), matches[0])
    if not p.is_file():
        raise EntryError(f"offline chart missing: {p}")
    raw = p.read_bytes()
    if len(raw) > MAX_BYTES:
        raise EntryError("offline chart exceeds size limit")
    if p.suffix == ".gz":
        raw = gzip.decompress(raw)
        if len(raw) > MAX_BYTES:
            raise EntryError("decompressed chart exceeds size limit")
    return raw


def _fetch_yahoo(sym, interval, day):
    age = (datetime.now(IST).date() - day).days
    if age < 0:
        raise EntryError("cannot fetch a future session")
    if interval == "1m" and age > 6:
        raise EntryError("Yahoo one-minute history unavailable after about a week; use offline data")
    if interval == "5m" and age > 58:
        raise EntryError("Yahoo five-minute history unavailable after about 60 days; use offline data")
    range_ = "1d" if age == 0 else "5d" if age <= 4 else "1mo" if age <= 28 else "60d"
    if interval == "1m" and range_ == "1mo":
        range_ = "5d"
    suffix = urllib.parse.quote(sym, safe="") + "?" + urllib.parse.urlencode({"range": range_, "interval": interval})
    errors = []
    for host in HOSTS:
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{suffix}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (IndiaTraderPaperResearch/0.2.1)",
                                                      "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise EntryError("Yahoo chart exceeds size limit")
            return raw
        except (urllib.error.URLError, TimeoutError, OSError, EntryError) as exc:
            errors.append(type(exc).__name__)
    raise EntryError("Yahoo chart unavailable at both hosts: " + ", ".join(errors))


def parse_chart(raw, sym, interval, source="Yahoo Finance unofficial"):
    """Validate one Yahoo chart; null OHLC becomes a gap, never a synthetic bar."""
    try:
        doc = json.loads(raw)
        result = doc["chart"]["result"]
        if not isinstance(result, list) or len(result) != 1:
            raise ValueError("missing chart")
        obj = result[0]
        meta = obj["meta"]
        if meta.get("symbol", sym).upper() != sym or meta.get("currency", "INR") != "INR":
            raise ValueError("symbol or currency mismatch")
        if meta.get("dataGranularity") and meta["dataGranularity"] != interval:
            raise ValueError("bar interval mismatch")
        stamps = obj["timestamp"]
        quote = obj["indicators"]["quote"][0]
        if not isinstance(stamps, list) or not stamps or any(
            not isinstance(quote.get(k), list) or len(quote[k]) != len(stamps)
            for k in ("open", "high", "low", "close", "volume")
        ):
            raise ValueError("inconsistent chart arrays")
        data_time = meta.get("regularMarketTime")
        if data_time is not None:
            data_time = datetime.fromtimestamp(int(data_time), IST)
        rows = []
        last_stamp = None
        for i, stamp in enumerate(stamps):
            if any(quote[k][i] is None for k in ("open", "high", "low", "close")):
                continue
            when = datetime.fromtimestamp(int(stamp), IST)
            if last_stamp is not None and when <= last_stamp:
                raise ValueError("duplicate or unordered chart timestamps")
            last_stamp = when
            values = [float(quote[k][i]) for k in ("open", "high", "low", "close")]
            o, h, l, c = values
            v = quote["volume"][i]
            if (not all(math.isfinite(x) for x in values) or l <= 0 or l > min(o, c) or
                h < max(o, c) or (v is not None and (not isinstance(v, (float, int)) or
                not math.isfinite(v) or v < 0))):
                raise ValueError("invalid OHLCV")
            rows.append({"time": when, "open": round(o, 2), "high": round(h, 2),
                         "low": round(l, 2), "close": round(c, 2), "volume": v})
        return {"ticker": sym, "interval": interval, "bars": rows,
                "data_time": data_time, "source": source}
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, OSError) as exc:
        raise EntryError(f"invalid chart for {sym}: {exc}") from exc


def get_chart(sym, interval, day, offline=None):
    if offline:
        return parse_chart(_read_offline(offline, sym), sym, interval)
    attempts = []
    age = (datetime.now(IST).date() - day).days
    rng = "1d" if age == 0 else "5d" if age <= 4 else "1mo" if age <= 28 else "60d"
    if os.environ.get("UPSTOX_ANALYTICS_TOKEN", "").strip():
        try:
            from .intraday_sources import upstox_chart
            raw, source = upstox_chart(sym, range_=rng, interval=int(interval[:-1]), end_day=day)
            return parse_chart(raw, sym, interval, source)
        except (OSError, ValueError, TypeError, KeyError, EntryError) as exc:
            attempts.append({"source": "Upstox", "status": "FAILED", "reason": type(exc).__name__})
    if sym.endswith(".NS") and os.environ.get("FYERS_APP_ID") and os.environ.get("FYERS_ACCESS_TOKEN"):
        try:
            from .intraday_sources import fyers_chart
            raw, source = fyers_chart(sym, range_=rng, interval=int(interval[:-1]), end_day=day)
            result = parse_chart(raw, sym, interval, source)
            result["provider_attempts"] = attempts
            return result
        except (OSError, ValueError, TypeError, KeyError, EntryError) as exc:
            attempts.append({"source": "FYERS", "status": "FAILED", "reason": type(exc).__name__})
    result = parse_chart(_fetch_yahoo(sym, interval, day), sym, interval)
    result["provider_attempts"] = attempts
    return result


def evaluate(chart, now, setup="orb", level=None, interval="5m", or_minutes=15,
             min_volume_x=0, settings=None, live=False):
    """First eligible completed close only; returns an exit-compatible paper entry."""
    settings = settings or config("/nonexistent")
    if interval not in ("5m", "1m"):
        raise EntryError("interval must be 5m or 1m")
    step = 5 if interval == "5m" else 1
    if or_minutes <= 0 or or_minutes % step or or_minutes > 60:
        raise EntryError("opening range must be 1–60 whole interval minutes")
    if setup == "level":
        try:
            level = float(level)
        except (TypeError, ValueError) as exc:
            raise EntryError("level setup needs a finite positive --level") from exc
    if setup not in ("orb", "level") or setup == "level" and (not math.isfinite(level) or level <= 0):
        raise EntryError("level setup needs a finite positive --level")
    if setup == "orb" and level is not None:
        raise EntryError("--level applies only to level setup")
    if not math.isfinite(min_volume_x) or min_volume_x < 0:
        raise EntryError("--min-volume-x must be nonnegative")
    now = now.astimezone(IST)
    day = now.date()
    base = {"mode": "PAPER_RESEARCH_ONLY", "ticker": chart["ticker"], "setup": setup,
            "as_of": now.isoformat(), "bar_interval": interval, "opening_minutes": or_minutes,
            "data_origin": chart["source"], "data_verified_against_exchange": False,
            "provider_attempts": chart.get("provider_attempts", []),
            "calendar_note": ("NSE 2026 normal-session calendar used as a proxy; BSE closures unverified"
                              if chart["ticker"].endswith(".BO") else "NSE 2026 normal-session calendar"),
            "orders_possible": False, "status": "WAIT", "reason": "", "level": level,
            "range_high": None, "range_low": None, "completed_bars": 0,
            "last_close": None, "last_bar_start": None,
            "data_time": chart["data_time"].isoformat() if chart["data_time"] else None,
            "min_volume_x": min_volume_x}
    valid, why = trading_day(day)
    if chart["ticker"].endswith(".BO"):
        bse_session = calendar_source.status(day, "BSE")
        if bse_session["state"] == "NORMAL":
            valid, why = True, ""
            base["calendar_note"] = "BSE current-year public normal-session calendar"
        elif bse_session["state"] in ("CLOSED", "SPECIAL"):
            valid, why = False, "BSE " + bse_session["state"].lower() + ": " + bse_session["reason"]
            base["calendar_note"] = "BSE current-year public calendar; special-session entry timing unsupported"
    if not valid:
        return dict(base, status="MARKET_CLOSED", reason=why)
    if live and chart["data_time"] is None:
        return dict(base, status="DATA_UNVERIFIED", reason="live provider supplied no last-trade timestamp")
    if live and chart["data_time"] > now + timedelta(minutes=10):
        return dict(base, status="DATA_UNVERIFIED", reason="provider timestamp is in the future")
    open_at = datetime.combine(day, OPEN, tzinfo=IST)
    end_at = datetime.combine(day, END, tzinfo=IST)
    range_end = open_at + timedelta(minutes=or_minutes)
    before = datetime.combine(day, settings["no_entries_before"], tzinfo=IST)
    cutoff = datetime.combine(day, settings["no_entries_after"], tzinfo=IST)
    if now < open_at:
        return dict(base, status="NOT_OPEN", reason="NSE continuous session opens at 09:15 IST")
    slots = {}
    for bar in chart["bars"]:
        at = bar["time"]
        delta = (at - open_at).total_seconds()
        if at.date() != day or delta < 0 or at >= end_at or delta % (step * 60):
            continue
        close_at = at + timedelta(minutes=step)
        if close_at > now or live and now < close_at + timedelta(seconds=60):
            continue
        if live and chart["data_time"] and chart["data_time"] < close_at:
            continue
        slots[int(delta // (step * 60))] = bar
    if not slots:
        return dict(base, reason="no completed bar in provider response")
    last_slot = max(slots)
    last = slots[last_slot]
    base.update(completed_bars=len(slots), last_close=last["close"],
                last_bar_start=last["time"].isoformat())
    n_or = or_minutes // step
    opening = [slots.get(i) for i in range(n_or)]
    present = [b for b in opening if b]
    if present:
        base.update(range_high=max(b["high"] for b in present),
                    range_low=min(b["low"] for b in present),
                    opening_bars=len(present), opening_bars_required=n_or)
    base["volume_quality"] = "OK" if all(b["volume"] is not None and b["volume"] > 0 for b in slots.values()) else "MISSING_OR_ZERO"
    if setup == "orb" and now < range_end:
        return dict(base, status="RANGE_FORMING", reason=f"{len(present)} of {n_or} range bars closed")
    if (setup == "orb" or min_volume_x) and any(b is None for b in opening):
        return dict(base, status="DATA_GAP", reason="opening range has a missing completed bar")
    if min_volume_x and any(b["volume"] is None or b["volume"] <= 0 for b in present):
        return dict(base, status="VOLUME_UNVERIFIED", reason="opening range has missing/zero volume")
    threshold = base["range_high"] if setup == "orb" else level
    base["level"] = threshold
    avg_volume = None
    if present and all(b["volume"] is not None and b["volume"] > 0 for b in present):
        avg_volume = sum(b["volume"] for b in present) / len(present)
    first = n_or if setup == "orb" else 0
    for slot in range(first, last_slot + 1):
        bar = slots.get(slot)
        at = open_at + timedelta(minutes=slot * step)
        close_at = at + timedelta(minutes=step)
        if close_at > now:
            break
        if bar is None:
            return dict(base, status="DATA_GAP", reason=f"missing completed bar at {at:%H:%M} IST")
        if min_volume_x and (bar["volume"] is None or bar["volume"] <= 0):
            return dict(base, status="VOLUME_UNVERIFIED", reason=f"missing/zero volume at {at:%H:%M} IST")
        if close_at < before or close_at > cutoff or close_at <= range_end and setup == "orb":
            continue
        if bar["close"] <= threshold:
            continue
        if min_volume_x and (avg_volume is None or bar["volume"] < avg_volume * min_volume_x):
            continue
        return {**base, "status": "TRIGGERED", "reason": "first eligible completed close above level",
                "entry_price": bar["close"], "trigger_at": close_at.isoformat(),
                "trigger_bar_start": at.isoformat(), "trigger_bar_index": slot,
                "signal_age_minutes": round((now - close_at).total_seconds() / 60, 2),
                "volume_multiple": round(bar["volume"] / avg_volume, 3)
                if bar["volume"] is not None and avg_volume else None,
                "pct_over": round(100 * (bar["close"] / threshold - 1), 3),
                "note": "Completed-bar paper signal only; no fill or order"}
    if now >= cutoff:
        return dict(base, status="EXPIRED", reason="no eligible close before 15:10 IST cutoff")
    return dict(base, status="WAIT", reason="awaiting a completed close above level",
                distance_pct=round(100 * (last["close"] / threshold - 1), 3))


def load_watchlist(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    hits = doc.get("hits")
    if not isinstance(hits, list) or not hits:
        raise EntryError("scanner watchlist has no hits")
    out = []
    for hit in hits:
        if not isinstance(hit, dict) or not (hit.get("symbol") or hit.get("ticker")):
            continue
        out.append({"ticker": ticker(hit.get("symbol") or hit.get("ticker")), "level": hit.get("level")})
    if not out:
        raise EntryError("scanner watchlist has no symbols")
    return out


def _chart_for(a, sym, day):
    return get_chart(sym, a.interval, day, a.offline)


def _result(a, sym, when, level=None, chart=None, live=False):
    c = chart or _chart_for(a, sym, when.date())
    return evaluate(c, when, a.setup, a.level if level is None else level, a.interval,
                    a.or_minutes, a.min_volume_x, config(a.config), live)


def _text(res):
    line = f"{res['ticker']} {res['as_of']} IST {res['status']}"
    if res["status"] == "TRIGGERED":
        line += f" at Rs.{res['entry_price']:.2f}, completed {res['trigger_at']}, "
        line += f"{res['pct_over']:+.2f}% over Rs.{res['level']:.2f}"
        if res["signal_age_minutes"] > (5 if res["bar_interval"] == "5m" else 1):
            line += f"; {res['signal_age_minutes']:.0f} minutes old"
    elif res.get("last_close") is not None and res.get("level"):
        line += f" last Rs.{res['last_close']:.2f}, {res.get('distance_pct', 0):+.2f}% vs Rs.{res['level']:.2f}"
    if res.get("reason"):
        line += f"; {res['reason']}"
    if res.get("volume_quality") == "MISSING_OR_ZERO":
        line += "; missing/zero volume in chart (price-only check)"
    return line + f". {res['data_origin']}; paper research only, nothing placed."


def _save(path, doc):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def check(a):
    sym = ticker(a.symbol)
    when = parse_now(a.now)
    if a.offline and not a.now:
        chart = _chart_for(a, sym, when.date())
        when = chart["data_time"] or max((b["time"] for b in chart["bars"]), default=when)
        when += timedelta(minutes=5 if a.interval == "5m" else 1)
    else:
        chart = _chart_for(a, sym, when.date())
    result = _result(a, sym, when, chart=chart, live=not a.offline and not a.now)
    if a.write:
        _save(a.write, result)
    print(json.dumps(result, indent=2) if a.json else _text(result))
    return 0 if result["status"] == "TRIGGERED" else 1


def watchlist(a):
    hits = load_watchlist(a.watchlist)
    when = parse_now(a.now)
    results = []
    for hit in hits:
        try:
            sym = hit["ticker"]
            level = a.level if a.level is not None else hit.get("level")
            chart = _chart_for(a, sym, when.date())
            at = when
            if a.offline and not a.now:
                at = chart["data_time"] or max((b["time"] for b in chart["bars"]), default=when)
                at += timedelta(minutes=5 if a.interval == "5m" else 1)
            result = _result(a, sym, at, level=level, chart=chart,
                             live=not a.offline and not a.now)
            results.append({"ticker": sym, "result": result, "error": None})
        except (EntryError, ValueError, OSError) as exc:
            results.append({"ticker": hit["ticker"], "result": None, "error": str(exc)})
    if a.json:
        print(json.dumps(results, indent=2))
    else:
        for row in results:
            print(_text(row["result"]) if row["result"] else row["ticker"] + ": " + row["error"])
    return 0 if any(r["result"] and r["result"]["status"] == "TRIGGERED" for r in results) else 1


def _key(res):
    payload = {k: res.get(k) for k in ("ticker", "setup", "level", "bar_interval", "opening_minutes", "min_volume_x")}
    payload["date"] = res["as_of"][:10]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]


def watch_loop(provider, now_fn, sleep_fn, emit, a, sym, start, until, state_path, out_path,
               simulated=False, max_errors=5):
    """Bounded poll loop. Simulated clocks advance immediately for offline replay."""
    errors, shown, last_slot = 0, set(), -1
    step = 5 if a.interval == "5m" else 1
    day = start.date()
    stop = min(until, datetime.combine(day, DEFAULT_AFTER, tzinfo=IST))
    preopen_reported = False
    while True:
        now = now_fn()
        if now.date() != day or now > stop + timedelta(seconds=60):
            emit("EXPIRED: watch window ended without a new trigger")
            return 1
        session_ok, session_reason = trading_day(now.date())
        if not session_ok:
            emit(f"MARKET_CLOSED: {session_reason}; nothing to watch")
            return 1
        first_close = datetime.combine(day, OPEN, tzinfo=IST) + timedelta(minutes=step)
        if now < first_close:
            if now >= stop:
                emit("EXPIRED: watch ended before the first completed bar")
                return 1
            if not preopen_reported:
                emit(f"{now:%H:%M} IST: waiting for the first completed {a.interval} bar at {first_close:%H:%M}")
                preopen_reported = True
            sleep_fn(max(1, min((first_close - now).total_seconds(), (stop - now).total_seconds(),
                                a.every if not simulated else 86400)))
            continue
        try:
            chart = provider(now)
            result = _result(a, sym, now, chart=chart, live=not simulated)
            errors = 0
        except (EntryError, OSError, ValueError) as exc:
            errors += 1
            emit(f"feed error {errors}/{max_errors}: {exc}")
            if errors >= max_errors:
                return 1
            sleep_fn(a.every if not simulated else step * 60)
            continue
        if result["status"] in ("MARKET_CLOSED", "DATA_GAP", "VOLUME_UNVERIFIED", "DATA_UNVERIFIED"):
            emit(_text(result))
            return 1
        for bar in chart["bars"]:
            at = bar["time"]
            if at.date() != day or at < datetime.combine(day, OPEN, tzinfo=IST):
                continue
            close_at = at + timedelta(minutes=step)
            if close_at > now or not simulated and now < close_at + timedelta(seconds=60):
                continue
            slot = int((at - datetime.combine(day, OPEN, tzinfo=IST)).total_seconds() // (step * 60))
            if slot > last_slot:
                last_slot = slot
                emit(f"{at:%H:%M} bar closed at Rs.{bar['close']:.2f}; source {chart['source']}")
        if result["status"] == "TRIGGERED":
            key = _key(result)
            state = json.loads(Path(state_path).read_text(encoding="utf-8")) if Path(state_path).is_file() else {}
            if key in state:
                emit("ALREADY_RECORDED: this setup triggered earlier; no duplicate entry file")
                return 1
            state[key] = result["trigger_at"]
            _save(state_path, state)
            _save(out_path, result)
            emit(_text(result))
            emit(f"Saved {out_path} for the paper exit plan. No order placed.")
            return 0
        if result["status"] == "EXPIRED" or now >= stop:
            emit(_text(result))
            return 1
        if result["status"] not in shown:
            shown.add(result["status"])
            emit(_text(result))
        if simulated:
            next_at = min(now + timedelta(minutes=step), stop)
            sleep_fn(max(1, (next_at - now).total_seconds()))
        else:
            open_at = datetime.combine(day, OPEN, tzinfo=IST)
            elapsed = max(0, (now - open_at).total_seconds())
            next_bar = open_at + timedelta(seconds=(int(elapsed // (step * 60)) + 1) * step * 60 + 60)
            sleep_fn(max(1, min(a.every, (next_bar - now).total_seconds(), (stop - now).total_seconds())))


def watch(a):
    if a.every <= 0 or a.every > 300:
        raise EntryError("--every must be between 0 and 300 seconds")
    sym = ticker(a.symbol)
    start = parse_now(a.now)
    if a.offline and not a.now:
        chart = _chart_for(a, sym, start.date())
        if not chart["bars"]:
            raise EntryError("offline chart has no bars")
        replay_day = (chart["data_time"] or chart["bars"][-1]["time"]).date()
        start = datetime.combine(replay_day, OPEN, tzinfo=IST)
    elif a.now:
        chart = _chart_for(a, sym, start.date())
    else:
        chart = None
    until = datetime.combine(start.date(), hhmm(a.until), tzinfo=IST) if a.until else datetime.combine(
        start.date(), DEFAULT_AFTER, tzinfo=IST)
    if until < start:
        raise EntryError("--until must be after the watch start time")
    if a.offline or a.now:
        current = [start]
        now_fn = lambda: current[0]
        sleep_fn = lambda seconds: current.__setitem__(0, current[0] + timedelta(seconds=max(1, seconds)))
    else:
        now_fn = lambda: datetime.now(IST)
        sleep_fn = clocktime.sleep
    provider = (lambda now: chart if chart else _chart_for(a, sym, now.date()))
    return watch_loop(provider, now_fn, sleep_fn, print, a, sym, start, until, a.state, a.out,
                      simulated=bool(a.offline or a.now))


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m india_trader entry-timer",
                                description="Completed-bar NSE paper entry timer")
    sub = p.add_subparsers(dest="command", required=True)

    def common(cmd, json_flag=True):
        cmd.add_argument("--setup", choices=("orb", "level"), default="orb")
        cmd.add_argument("--interval", choices=("5m", "1m"), default="5m")
        cmd.add_argument("--level", type=float)
        cmd.add_argument("--or-minutes", type=int, default=15)
        cmd.add_argument("--min-volume-x", type=float, default=0)
        cmd.add_argument("--side", choices=("buy", "long", "sell", "short"), default="buy")
        cmd.add_argument("--now", help="IST date/time or offset-aware ISO time")
        cmd.add_argument("--offline", help="saved Yahoo chart JSON or folder of TICKER.NS.json files")
        cmd.add_argument("--config", default="trader/config.json")
        if json_flag:
            cmd.add_argument("--json", action="store_true")

    c = sub.add_parser("check", help="one immediate status check")
    c.add_argument("symbol")
    common(c)
    c.add_argument("--write", nargs="?", const="trader/entry.json", metavar="PATH")
    w = sub.add_parser("watch", help="poll completed bars until trigger or cutoff")
    w.add_argument("symbol")
    common(w, json_flag=False)
    w.add_argument("--every", type=float, default=30)
    w.add_argument("--until", default="15:10")
    w.add_argument("--out", default="trader/entry.json")
    w.add_argument("--state", default="trader/state/entry_watch.json")
    wl = sub.add_parser("watchlist", help="check all scanner watchlist hits")
    common(wl)
    wl.add_argument("--watchlist", default="trader/watchlist.json")
    a = p.parse_args(argv)
    try:
        if a.side in ("sell", "short"):
            raise EntryError("only long cash-equity paper entries are supported")
        return {"check": check, "watch": watch, "watchlist": watchlist}[a.command](a)
    except (EntryError, ValueError, OSError, json.JSONDecodeError) as exc:
        print("entry-timer: " + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("entry-timer stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
