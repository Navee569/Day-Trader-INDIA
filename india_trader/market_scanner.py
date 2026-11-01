#!/usr/bin/env python3
"""Indian NSE cash equity scanner, adapted from Jake Schincariol's MIT scanner.

Three workflows: official NSE EQ universe; daily five-setup scan; two-stage
five-minute opening-range scan. Yahoo spark/chart data is unofficial and
paper-research only. No broker operations.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.parse

from .core import IST, symbol as normalize_symbol
from .universe import load_bse_equities, load_nse_equities
from . import calendar_source

UA = "Mozilla/5.0 (compatible; IndiaDayTraderPaper/0.2)"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
SPARK_MAX = 20
PAUSE = 0.3
BACKOFF = 5.0
SLEEP = time.sleep
BAR_MIN = 5
MAX_CONFIRM = 200
MIN_DAILY_BARS = 30
DEFAULT_UNIVERSE = "trader/universe.txt"
NOTE = "A scan lists charts matching a rule; it is not a recommendation or a verified trade signal."

# NSE equities holiday list published at nseindia.com/resources/exchange-communication-holidays.
# Only 2026 is built in. Unknown future calendars fail closed for ORB.
NSE_HOLIDAYS = {
    "2026-01-15": "Maharashtra municipal election",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Ram Navami",
    "2026-03-31": "Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Muhurat special session; regular schedule unavailable",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas Day",
}
CALENDAR_YEARS = (2026,)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 15)  # conservative continuous-bar cutoff; CAS may begin
DAILY_CLOSE = datetime.time(15, 30)

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- time

def to_ist(utc):
    """Naive UTC datetime to naive IST, independent of Windows tzdata."""
    return utc.replace(tzinfo=datetime.timezone.utc).astimezone(IST).replace(tzinfo=None)


def ist_to_utc(local):
    return local.replace(tzinfo=IST).astimezone(datetime.timezone.utc).replace(tzinfo=None)


def ts_to_ist(ts):
    return datetime.datetime.fromtimestamp(int(ts), IST).replace(tzinfo=None)


def parse_now(text):
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    try:
        dt = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ScanError('--now wants "2026-10-29 10:15" (IST) or offset-aware ISO time')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def parse_hhmm(text, flag="--at"):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScanError("%s wants IST HH:MM, for example 10:15" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


def trading_day(d):
    if d.year in CALENDAR_YEARS:
        if d.weekday() >= 5:
            return False, "weekend"
        name = NSE_HOLIDAYS.get(d.isoformat())
        if name:
            return False, "NSE holiday: " + name
    dynamic = calendar_source.status(d)
    if dynamic["state"] == "NORMAL":
        return True, ""
    if dynamic["state"] in ("CLOSED", "SPECIAL"):
        return False, "NSE " + dynamic["state"].lower() + ": " + dynamic["reason"]
    if d.year in CALENDAR_YEARS:
        return True, ""
    return False, "NSE holiday calendar not verified for %s" % d.year


def session(d):
    ok, _ = trading_day(d)
    return (OPEN, CLOSE) if ok else None


def clock_label(t):
    """datetime or time -> '10:15 AM'."""
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


# ---------------------------------------------------------------- network

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ScanError("market data response exceeds 8 MiB")
    return raw.decode("utf-8")


def yahoo_get(path):
    """GET a Yahoo Finance path. query2 first, query1 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def to_yahoo(sym):
    return sym.upper()  # NSE ticker already ends in .NS; BSE ticker in .BO


def spark_path(symbols, rng, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(urllib.parse.quote(to_yahoo(s), safe=".") for s in symbols), rng, interval)


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (urllib.parse.quote(to_yahoo(sym), safe="."), rng, interval)


# ---------------------------------------------------------------- parsing

def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    if not isinstance(res, dict):
        raise ScanError("Yahoo chart result is not an object")
    meta = res.get("meta") or {}
    if not isinstance(meta, dict) or meta.get("currency") != "INR":
        raise ScanError("Yahoo chart is not denominated in INR")
    indicators = res.get("indicators") or {}
    if not isinstance(indicators, dict):
        raise ScanError("Yahoo indicators have unexpected shape")
    quotes = indicators.get("quote") or [{}]
    if not isinstance(quotes, list) or len(quotes) != 1:
        raise ScanError("Yahoo quote count is unexpected")
    q = quotes[0] or {}
    t = res.get("timestamp") or []
    if not isinstance(t, list) or len(t) > 10000 or not isinstance(q, dict):
        raise ScanError("Yahoo chart columns have unexpected shape")
    n = len(t)

    def col(k):
        v = q.get(k)
        if v is not None and (not isinstance(v, list) or len(v) != n):
            raise ScanError("Yahoo %s column length mismatch" % k)
        return v
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": meta}


def parse_spark(text_or_doc, wanted=None):
    """Spark JSON -> {SYM: series}. Symbols Yahoo does not know are simply missing
    from the answer. `wanted` restricts results to requested Indian symbols."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        if not isinstance(item, dict):
            continue
        resp = item.get("response") or []
        if not isinstance(resp, list) or not resp or not isinstance(resp[0], dict):
            continue
        metadata = resp[0].get("meta") or {}
        if not isinstance(metadata, dict):
            continue
        ysym = item.get("symbol") or metadata.get("symbol") or ""
        ysym = str(ysym).upper()
        actual = str(metadata.get("symbol") or "").upper()
        if not actual or actual != ysym:
            continue
        if wanted is not None and ysym not in wanted:
            continue
        sym = (wanted or {}).get(ysym, ysym)
        if not sym.endswith((".NS", ".BO")):
            continue
        out[sym] = series_from_result(resp[0])
    if err and not out:
        reason = (err.get("description") or err.get("code") or err) if isinstance(err, dict) else err
        raise ScanError("Yahoo spark: %s" % reason)
    return out


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, (str, bytes, bytearray)) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not isinstance(res, list) or not res:
        err = chart.get("error") or {}
        reason = err.get("description", "unknown ticker") if isinstance(err, dict) else str(err)
        raise ScanError("no data for %s: %s" % (sym, reason))
    series = series_from_result(res[0])
    actual = str(series["meta"].get("symbol") or "").upper()
    if not actual or (sym and actual != sym.upper()):
        raise ScanError("Yahoo returned %s for requested %s" % (actual, sym))
    return series


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _absorb(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors."""
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc:
        sym = name_hint
        if not sym:
            try:
                sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
            except (AttributeError, KeyError, TypeError, IndexError):
                raise ScanError("an offline v8 chart without a symbol: name the file <TICKER>.json")
        sym = sym.upper()
        try:
            data[sym] = parse_chart(doc, sym)
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % name_hint)


def load_offline(path):
    """A folder of <T>.json(.gz) files or one file -> ({SYM: series}, {SYM: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            if isinstance(doc, dict) and ("chart" in doc or "spark" in doc):
                _absorb(doc, "" if "spark" in doc else _stem(f), data, errors)
            else:
                _absorb(doc, "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

# NSE EQ classification is delegated to the official exchange CSV parser.


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes (the same math as the reel's refs/scan.py)."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now_utc):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the session closes, anything dated after the clock dropped."""
    now = to_ist(now_utc)
    today = now.date()
    s = session(today)
    today_done = s is None or now.time() >= DAILY_CLOSE
    out = {}
    for ts, c in zip(series["t"], series["c"]):
        if c is None:
            continue
        try:
            close = float(c)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(close) or close <= 0:
            continue
        try:
            d = ts_to_ist(ts).date()
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        if d > today or (d == today and not today_done):
            continue
        out[d] = close
    return sorted(out.items())


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, symbols, now_utc, min_price, min_volume):
    """Pure. data = {SYM: series}. Returns (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    local_now = to_ist(now_utc)
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_closes(s, now_utc)
        price = bars[-1][1] if bars else None
        vol = None
        if bars and s.get("v"):
            final_date = bars[-1][0]
            for ts, volume in zip(s["t"], s["v"]):
                try:
                    same_day = ts_to_ist(ts).date() == final_date
                except (TypeError, ValueError, OverflowError, OSError):
                    same_day = False
                if same_day and volume is not None:
                    try:
                        v = float(volume)
                        if math.isfinite(v) and v >= 0:
                            vol = v
                    except (TypeError, ValueError, OverflowError):
                        pass
        # Spark daily quotes contain closes only. Its meta volume is usable
        # outside the trading session when the quote timestamp is dated the
        # same as the final completed daily close; otherwise it may be today's
        # partial volume attached to yesterday's close.
        if vol is None and bars and not (OPEN <= local_now.time() < DAILY_CLOSE):
            meta = s.get("meta") or {}
            try:
                quote_time = ts_to_ist(meta.get("regularMarketTime")).date()
                candidate = float(meta.get("regularMarketVolume"))
                if quote_time == bars[-1][0] and math.isfinite(candidate) and candidate >= 0:
                    vol = candidate
            except (TypeError, ValueError, OverflowError, OSError):
                pass
        if price is None or price < min_price or (min_volume > 0 and (vol is None or vol < min_volume)):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c in bars]
        tags, nums = find_setups(closes)
        if tags:
            hits.append({"symbol": sym, "setups": tags, "price": round(closes[-1], 2),
                         "close_date": d_last.isoformat(), "rsi": round(nums["rsi"], 1),
                         "volume": int(vol) if vol is not None else None})
    hits.sort(key=lambda h: (-(h["price"] * (h["volume"] or 0)), h["symbol"]))  # most rupees traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    if last_date:
        counts["stale_scanned"] = sum(n for d, n in dates.items() if d != last_date)
        counts["stale_hit"] = sum(h["close_date"] != last_date.isoformat() for h in hits)
        hits = [h for h in hits if h["close_date"] == last_date.isoformat()]
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k):
    return datetime.datetime.combine(day, OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff):
    """5-minute bars of `day` that are complete at `cutoff` (naive India time),
    on the grid, with a close. Sorted, one per slot."""
    s = session(day)
    close_t = s[1] if s else CLOSE
    out = {}
    hs, ls = series.get("h"), series.get("l")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        try:
            close = float(c)
            high = None if not hs or hs[i] is None else float(hs[i])
            low = None if not ls or ls[i] is None else float(ls[i])
            vols = series.get("v")
            volume = None if not vols or vols[i] is None else float(vols[i])
        except (TypeError, ValueError, OverflowError, IndexError):
            continue
        if (not math.isfinite(close) or close <= 0 or
            any(x is not None and (not math.isfinite(x) or x < 0) for x in (high, low, volume))):
            continue
        try:
            et = ts_to_ist(ts)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        if et.date() != day or et.second or et.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if et.time() < OPEN or et.time() >= close_t:
            continue  # pre-market, after hours, the 4:00 PM closing print
        if et + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        v = series.get("v")
        out[et] = {"start": et, "c": close, "h": high, "l": low, "v": volume}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_range):
    """Every slot from 9:15 to the cutoff is there, and at least the range."""
    want = int((cutoff - slot(day, 0)).total_seconds() // (BAR_MIN * 60))
    s = session(day)
    if s:
        want = min(want, int((datetime.datetime.combine(day, s[1]) - slot(day, 0)).total_seconds()
                             // (BAR_MIN * 60)))
    if want < n_range or len(bars) != want:
        return False
    return all(b["start"] == slot(day, k) for k, b in enumerate(bars))


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(b["c"] for b in bars[:n_range])
    return any(b["c"] >= top for b in bars[n_range:])


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None or b["h"] < max(b["c"], b["l"])
           for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": clock_label(closes_at),
                    "bar_start": b["start"].strftime("%H:%M"), "bar_index": k,
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None):
    """-> (day, cutoff naive IST) or raise ScanError with the plain message."""
    now = to_ist(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = trading_day(day)
    if not ok:
        raise ScanError("No NSE session on %s (%s): the market is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    s = session(day)
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, s[1])
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (at.strftime("%H:%M"), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s IST on %s. Replay the session with --at 10:15."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < first_close:
        raise ScanError("It is %s IST: the %d-minute opening range runs 9:15 to %s and the first bar after it "
                        "closes at %s. Run orb again after %s."
                        % (clock_label(cutoff), or_minutes, clock_label(range_end), clock_label(first_close),
                           clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:15." % (clock_label(cutoff), until.strftime("%H:%M"),
                                                      clock_label(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_volume=0, confirm=None,
             max_confirm=None):
    """stage1 = {SYM: series} with closes (spark or v8). confirm(sym) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (price x volume so far) are confirmed; the rest are counted as unconfirmed.
    Returns (hits, counts, failed {sym: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0,
              "zero_or_missing_volume": 0, "scanned": 0, "candidates": 0,
              "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff)
        if not usable(bars, day, cutoff, n_range):
            counts["unusable"] += 1
            continue
        vol = sum((b["v"] or 0) for b in bars)
        if not any(b["v"] is not None for b in bars):
            try:
                vol = float((s.get("meta") or {}).get("regularMarketVolume") or 0)
            except (TypeError, ValueError, OverflowError):
                vol = 0
        if bars[-1]["c"] < min_price or vol < min_volume:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range):
            cands.append((sym, bars, bars[-1]["c"] * vol))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, bars, _ in cands:
        full = bars
        if confirm is not None:
            try:
                s2 = confirm(sym)
            except ScanError as e:
                failed[sym] = str(e)
                continue
            full = session_bars(s2, day, cutoff)
            if not usable(full, day, cutoff, n_range):
                failed[sym] = "the v8 chart has a missing bar"
                continue
        if any(b["v"] is None or b["v"] <= 0 for b in full):
            counts["zero_or_missing_volume"] += 1
            failed[sym] = "zero or missing five-minute volume"
            continue
        if any(b["h"] is None or b["l"] is None or b["h"] < max(b["c"], b["l"])
               or b["l"] > b["c"] for b in full[:n_range]):
            failed[sym] = "invalid opening-range OHLC"
            continue
        counts["confirmed"] += 1
        h = orb_hit(full, n_range)
        if h:
            hits.append(dict({"symbol": sym, "setups": ["orb"]}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None, day=None, manager=None):
    """Fetch chart series in provider order; batch only the final Yahoo fallback."""
    data, failed = {}, {}
    unresolved = list(symbols)
    if interval == "1d":
        from .market_data_manager import MarketDataManager, FeedUnavailable
        manager = manager or MarketDataManager()
        unresolved = []
        for i, sym in enumerate(symbols):
            if i and pause:
                SLEEP(pause)
            try:
                fetched = manager.fetch_daily(sym, end_day=day)
                rows = fetched["bars"]
                stamps = [int(datetime.datetime.combine(datetime.date.fromisoformat(row["date"]),
                          datetime.time(15, 30), IST).timestamp()) for row in rows]
                data[sym] = {"t": stamps, "o": [row["open"] for row in rows],
                             "h": [row["high"] for row in rows],
                             "l": [row["low"] for row in rows],
                             "c": [row["close"] for row in rows],
                             "v": [row["volume"] for row in rows],
                             "meta": {"symbol": sym, "currency": "INR",
                                      "data_provider": fetched["source"],
                                      "regularMarketTime": stamps[-1]}}
            except (FeedUnavailable, OSError, ValueError, TypeError, KeyError, ScanError):
                unresolved.append(sym)
    if interval == "5m" and (os.environ.get("UPSTOX_ANALYTICS_TOKEN") or
                             os.environ.get("FYERS_APP_ID") and os.environ.get("FYERS_ACCESS_TOKEN")):
        from .market_data_manager import MarketDataManager, FeedUnavailable
        from .intraday_sources import _bars_chart
        manager = manager or MarketDataManager()
        unresolved = []
        for i, sym in enumerate(symbols):
            if i and pause:
                SLEEP(pause)
            try:
                fetched = manager.fetch_candles(sym, rng, interval=5, end_day=day,
                                                allow_yahoo=False)
                raw, _ = _bars_chart(sym, fetched["bars"], 5, fetched["source"])
                series = parse_chart(raw, sym)
                series["meta"]["data_provider"] = fetched["source"]
                series["meta"]["entry_ready"] = fetched["entry_ready"]
                data[sym] = series
            except (FeedUnavailable, OSError, ValueError, TypeError, KeyError, ScanError):
                unresolved.append(sym)
    batches = [unresolved[i:i + SPARK_MAX] for i in range(0, len(unresolved), SPARK_MAX)]
    retried = 0
    retry_cap = getattr(a, "max_source_fallback", 0) if a is not None else 0
    for k, batch in enumerate(batches):
        if k and pause:
            SLEEP(pause)
        wanted = {to_yahoo(s): s for s in batch}
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)), wanted)
        except (ScanError, ValueError, OSError):
            got = {}
        for sym in batch:
            if sym in got:
                series = got[sym]
                series.setdefault("meta", {})["data_provider"] = "Yahoo Finance unofficial"
                series["meta"]["entry_ready"] = False
                data[sym] = series
                if manager is not None:
                    manager._selected(sym, "YAHOO", manager.now or datetime.datetime.now(IST),
                                      capability="daily" if interval == "1d" else "candles")
                continue
            # A failed batch must not bypass the final per-symbol Yahoo attempt.
            if retry_cap and retried >= retry_cap:
                failed[sym] = "Yahoo chart retry limit reached after batch gap"
                continue
            retried += 1
            try:
                series = parse_chart(yahoo_get(chart_path(sym, rng, interval)), sym)
                series["meta"]["data_provider"] = "Yahoo Finance unofficial"
                series["meta"]["entry_ready"] = False
                data[sym] = series
                if manager is not None:
                    manager._selected(sym, "YAHOO", manager.now or datetime.datetime.now(IST),
                                      capability="daily" if interval == "1d" else "candles")
            except (ScanError, ValueError, OSError) as exc:
                failed[sym] = "no validated chart from any configured provider: " + type(exc).__name__
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  Yahoo fallback batch %d of %d" % (k + 1, len(batches)))
    return data, failed


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)  # offline: every chart in the file, unless a list was given
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `python -m india_trader scanner universe` first, or pass --symbols" % path)
        syms = symbols_from_file(path)
    if a.limit is not None and a.limit < 1:
        raise ScanError("--limit must be positive")
    out, seen = [], set()
    for s in syms:
        try:
            normalized = normalize_symbol(s, "BSE" if s.upper().endswith(".BO") else "NSE")
        except ValueError as exc:
            raise ScanError("invalid Indian cash-equity symbol %r: %s" % (s, exc)) from exc
        if normalized not in seen:
            out.append(normalized)
            seen.add(normalized)
    if a.limit is not None:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def exchange_counts(symbols):
    return {"NSE": sum(s.endswith(".NS") for s in symbols),
            "BSE": sum(s.endswith(".BO") for s in symbols)}


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln.split("|")[0].strip().upper())
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def money(x):
    return "INR {:,.2f}".format(x)


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    t0 = time.time()
    if a.exchange == "NSE" and a.bse_zip:
        raise ScanError("--bse-zip requires --exchange BSE or BOTH")
    if a.exchange == "BSE" and a.offline:
        raise ScanError("Use --bse-zip for a saved BSE archive")
    rows, origins, dates, counts = [], {}, {}, {}
    if a.exchange in ("NSE", "BOTH"):
        nse, origins["NSE"] = load_nse_equities(a.offline)
        rows.extend(nse)
        counts["NSE_EQ"] = len(nse)
    if a.exchange in ("BSE", "BOTH"):
        bse, origins["BSE"], dates["BSE"] = load_bse_equities(a.bse_zip)
        rows.extend(bse)
        counts["BSE_CASH_EQ"] = len(bse)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write("%s|%s|%s\n" % (row["ticker"], row["name"], row["isin"]))
    doc = {"mode": "universe", "exchange": a.exchange, "count": len(rows),
           "counts": counts, "source": origins, "source_dates": dates,
           "coverage": "NSE EQ; BSE active one-share-lot EQ groups A/B/X/T/XT when selected",
           "retrieved_at": datetime.datetime.now(IST).isoformat(),
           "out": None if a.no_write else a.out,
           "seconds": round(time.time() - t0, 1)}
    print(json.dumps(doc, indent=2) if a.json else
          "%s cash-equity universe: %s symbols from %s%s" %
          (a.exchange, len(rows), ", ".join(origins.values()),
           " (no file written)" if a.no_write else "; wrote " + a.out))
    return 0 if rows else 1


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(a, offline_syms)
    if not a.offline:
        progress(a, "Fetching completed daily data for %s symbols; Upstox, FYERS, NSE, then Yahoo..."
                 % "{:,}".format(len(symbols)))
        data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a)
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, a.min_volume)
    nfailed = len(symbols) - counts["fetched"]
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "india-trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "status": "NO_USABLE_CHARTS" if not counts["scanned"] else
                     ("PARTIAL" if nfailed or counts.get("stale_scanned") else "OK"),
           "source": "offline Yahoo fixture" if a.offline else
                     "Upstox, FYERS, NSE final Bhavcopy, Yahoo unofficial fallback",
           "provider_counts": {provider: sum(1 for series in data.values()
                                              if provider in series.get("meta", {}).get("data_provider", ""))
                               for provider in ("Upstox", "FYERS", "NSE", "Yahoo")},
           "exchange_counts": exchange_counts(symbols),
           "filters": {"min_price": a.min_price, "min_volume": a.min_volume},
           "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    local = to_ist(now)
    print("Daily scan, %s IST, closes through %s (completed daily research data)." % (
        local.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing"))
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (INR %s+ and %s+ shares)%s; "
          "%s scanned." % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
                           (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
                           "{:,}".format(counts["fetched"] - counts["illiquid"]), _n(a.min_price),
                           "{:,}".format(a.min_volume),
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           "{:,}".format(counts["scanned"])))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (price x volume):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-6s %10s  RSI %5.1f  %s" % (h["symbol"], money(h["price"]), h["rsi"],
                                              ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE)
    return 0 if hits else 1


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or "15:10", "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [ts_to_ist(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day)
    symbols = read_symbols(a, offline_syms)
    confirm = None
    confirmation_source = {}
    if not a.offline:
        from .market_data_manager import MarketDataManager, FeedUnavailable
        from .intraday_sources import _bars_chart
        manager = MarketDataManager()
        today = to_ist(parse_now(None)).date()
        if (today - day).days > 6:
            raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
        rng = "1d" if day == today else "5d"
        progress(a, "Stage 1: five-minute charts for %s symbols; Upstox, FYERS, then Yahoo fallback..."
                 % "{:,}".format(len(symbols)))
        data, failed = fetch_spark(symbols, rng, "%dm" % BAR_MIN, a.pause, a, day, manager)
        done = []

        def confirm(sym):
            if a.pause:
                SLEEP(a.pause)
            done.append(sym)
            if len(done) % 50 == 0:
                progress(a, "  stage 2: %d v8 charts so far" % len(done))
            stage_one = data.get(sym)
            if stage_one and stage_one.get("meta", {}).get("data_provider", "").startswith(("Upstox", "FYERS")):
                confirmation_source[sym] = stage_one["meta"]["data_provider"]
                return stage_one
            try:
                fetched = manager.fetch_candles(sym, rng, interval=BAR_MIN, end_day=day,
                                                allow_yahoo=False)
                raw, _ = _bars_chart(sym, fetched["bars"], BAR_MIN, fetched["source"])
                result = parse_chart(raw, sym)
                confirmation_source[sym] = fetched["source"]
                return result
            except (FeedUnavailable, OSError, ValueError, TypeError, KeyError, ScanError):
                confirmation_source[sym] = "Yahoo Finance unofficial fallback"
                return parse_chart(yahoo_get(chart_path(sym, rng, "%dm" % BAR_MIN)), sym)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_volume, confirm,
                                     a.max_confirm)
    if confirmation_source:
        for hit in hits:
            hit["confirmation_source"] = confirmation_source.get(hit["symbol"])
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed])
    as_of = "%s %s IST" % (day.isoformat(), cutoff.strftime("%H:%M"))
    wall_clock = to_ist(parse_now(None))
    replay = a.offline or cutoff < wall_clock - datetime.timedelta(minutes=5)
    doc = {"created_by": "india-trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "orb",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "unconfirmed": counts["unconfirmed"],
           "clock_mode": "HISTORICAL_REPLAY" if replay else "CURRENT_SESSION_RESEARCH",
           "data_timeliness_unverified": True,
           "status": "NO_USABLE_CHARTS" if not counts["scanned"] else
                     ("PARTIAL" if nfailed or counts["unconfirmed"] or counts["unusable"] else "OK"),
           "source": "offline Yahoo fixture" if a.offline else
                     "Upstox primary, FYERS secondary, NSE five-minute unavailable, Yahoo unofficial fallback",
           "exchange_counts": exchange_counts(symbols),
           "calendar_source": "NSE built-in 2026 holidays plus fresh public NSE calendar; "
                              "special sessions fail closed; BSE sessions not verified for ORB",
           "or_minutes": a.or_minutes,
           "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": a.min_price, "min_volume": a.min_volume}, "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s IST: %d-minute range (9:15 to %s), 5-minute bars, %s."
          % (day_label(day), clock_label(cutoff), a.or_minutes,
             clock_label(slot(day, a.or_minutes // BAR_MIN)), doc["source"]))
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]),
             (" (%s under the price / volume floor)" % "{:,}".format(counts["illiquid"])) if counts["illiquid"] else "",
             "{:,}".format(counts["candidates"]), "{:,}".format(counts["confirmed"])))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % ("{:,}".format(counts["unconfirmed"]), "{:,}".format(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), ("the %s checked" % "{:,}".format(counts["confirmed"])) if counts["unconfirmed"]
                 else "{:,}".format(counts["scanned"]), clock_label(cutoff)))
        for h in hits[:a.show]:
            print("  %8s  %-6s %10s  range %s to %s  %+.2f%% over" % (
                h["at"], h["symbol"], money(h["price"]), _p(h["or_low"]), _p(h["or_high"]), h["pct_over"]))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    if replay:
        print("Historical replay of completed bars; not a live signal.")
    print(NOTE)
    return 0 if hits else 1


def _p(x):
    return "{:,.2f}".format(x)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m india_trader scanner",
                                 description="Indian cash-equity scanner: universe, daily setups, and opening-range breakouts.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="download official NSE EQ and optional BSE cash-equity lists")
    p.add_argument("--exchange", choices=("NSE", "BSE", "BOTH"), default="NSE")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--offline", metavar="CSV", help="saved NSE EQUITY_L.csv")
    p.add_argument("--bse-zip", metavar="ZIP", help="saved official BSE SCRIP.ZIP")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")

    def common(p, min_price, min_volume):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated tickers instead of the universe")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of tickers")
        p.add_argument("--universe", help="symbol list, one SYMBOL|Name a line (default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price)
        p.add_argument("--min-volume", type=int, default=min_volume)
        p.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-19 10:15" India, or ...Z)')
        p.add_argument("--out", default="trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="pre-market setups from daily closes")
    common(p, 20.0, 100000)
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, 20.0, 0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--max-source-fallback", type=int, default=0,
                   help="limit per-symbol Yahoo chart retries after batch gaps (0 = all missing charts)")
    p.add_argument("--at", help="cut the clock at this India time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, 15:10)")
    p.add_argument("--config", default="trader/config.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        if a.cmd != "universe":
            if a.pause < 0 or a.min_price < 0 or a.min_volume < 0 or a.show < 0:
                raise ScanError("pause, price, volume, and show must be nonnegative")
            if a.cmd == "orb" and a.max_confirm < 0:
                raise ScanError("--max-confirm must be zero or positive")
            if a.cmd == "orb" and a.max_source_fallback < 0:
                raise ScanError("--max-source-fallback must be zero or positive")
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except (ScanError, ValueError, OSError) as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
