"""Seeded NSE cash-equity scenario replay from recent five-minute bars.

India adaptation of the MIT-licensed upstream trader-timemachine workflow.
This is descriptive paper research, never a return forecast or an order.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import gzip
import io
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request

from .core import IST
from .entry_timer import EntryError, parse_chart, ticker
from .exit_planner import parse_clock
from .market_scanner import trading_day

OPEN = time(9, 15)
END = time(15, 15)  # conservative continuous-bar end for CAS-eligible shares
SESSION_BARS = 72
MIN_SESSIONS = 20
BAND_PATHS = 2000
SAMPLE_PATHS = 160
HIST_BINS = 24
MAX_BYTES = 8_000_000


class MachineError(Exception):
    pass


def _number(value, what):
    try:
        x = float(value)
    except (TypeError, ValueError) as exc:
        raise MachineError(f"{what} must be a finite number") from exc
    if not math.isfinite(x) or x <= 0:
        raise MachineError(f"{what} must be a finite positive number")
    return x


def _time(raw, what):
    try:
        value = parse_clock(raw, what)
    except ValueError as exc:
        raise MachineError(str(exc)) from exc
    if value is None:
        raise MachineError(f"{what} is required")
    return value


def _now(raw):
    if not raw:
        return datetime.now(IST)
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise MachineError("--now needs an ISO date/time") from exc
    return (dt.replace(tzinfo=IST) if dt.tzinfo is None else dt).astimezone(IST)


def load_plan(a, now):
    if a.symbol:
        needed = ("entry", "stop", "target", "entry_time")
        missing = [x for x in needed if getattr(a, x) is None]
        if missing:
            raise MachineError("manual plan needs " + ", ".join("--" + x.replace("_", "-") for x in missing))
        raw = {"ticker": a.symbol, "date": a.date or now.date().isoformat(),
               "entry_reference": a.entry, "stop": a.stop, "target": a.target,
               "entry_time_ist": a.entry_time, "setup": a.setup, "interval": "5m",
               "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "side": "buy" if a.side == "buy" else "sell", "flat_by_ist": a.until or "15:15",
               "entry_confirmed": False, "source": "manual flags"}
    else:
        p = Path(a.plan)
        if not p.is_file():
            raise MachineError(f"plan missing: {p}; run exit-planner plan --write first")
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise MachineError("saved plan must be a JSON object")
        raw["source"] = str(p)
    return normalize_plan(raw)


def normalize_plan(raw):
    sym = ticker(raw.get("ticker") or raw.get("symbol"))
    side = str(raw.get("side") or "BUY_LONG_CASH").lower()
    if side not in ("buy", "long", "buy_long_cash", "sell", "short", "sell_short_cash_paper"):
        raise MachineError("side must be a paper buy or sell")
    side = "sell" if side in ("sell", "short", "sell_short_cash_paper") else "buy"
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise MachineError("Time Machine needs five-minute bars; one-minute history is too short for its pool")
    try:
        day = date.fromisoformat(str(raw.get("date") or ""))
    except ValueError as exc:
        raise MachineError("plan date must be YYYY-MM-DD") from exc
    valid, why = trading_day(day)
    if not valid:
        raise MachineError("plan has no verified normal NSE session: " + why)
    entry = _number(raw.get("entry_reference", raw.get("entry")), "entry")
    stop = _number(raw.get("stop"), "stop")
    target = _number(raw.get("target"), "target")
    if side == "buy" and not stop < entry < target:
        raise MachineError("buy plan needs stop < entry < target")
    if side == "sell" and not target < entry < stop:
        raise MachineError("sell plan needs target < entry < stop")
    trigger = raw.get("trigger_at")
    entry_time = raw.get("entry_time_ist") or raw.get("entry_time")
    if not entry_time and trigger:
        try:
            dt = datetime.fromisoformat(str(trigger).replace("Z", "+00:00"))
        except ValueError as exc:
            raise MachineError("invalid trigger_at") from exc
        if dt.tzinfo is None:
            raise MachineError("trigger_at must include a timezone")
        if dt.astimezone(IST).date() != day:
            raise MachineError("trigger date disagrees with plan date")
        entry_time = dt.astimezone(IST).strftime("%H:%M")
    t = _time(entry_time, "entry time")
    open_at = datetime.combine(day, OPEN, tzinfo=IST)
    close_at = datetime.combine(day, t, tzinfo=IST)
    minutes = int((close_at - open_at).total_seconds() // 60)
    if minutes % 5 or not 5 <= minutes < SESSION_BARS * 5:
        raise MachineError("entry close must be on the five-minute 09:20–15:10 IST grid")
    k = minutes // 5 - 1
    given_bar = raw.get("entry_bar")
    if given_bar is not None and int(given_bar) != k:
        raise MachineError("plan entry bar index disagrees with its close time")
    opening = raw.get("opening_range") or {}
    if not isinstance(opening, dict):
        raise MachineError("opening_range must be an object")
    or_minutes = opening.get("minutes", raw.get("opening_minutes", 15))
    if not isinstance(or_minutes, int) or or_minutes < 5 or or_minutes > 60 or or_minutes % 5:
        raise MachineError("opening range must be 5–60 whole five-minute bars")
    flat = raw.get("flat_by_ist") or raw.get("flat_by") or "15:15"
    flat_time = _time(flat, "flat-by")
    if not t < flat_time <= END or (flat_time.hour * 60 + flat_time.minute - 9 * 60 - 15) % 5:
        raise MachineError("flat-by must be a five-minute grid time after entry and no later than 15:15 IST")
    rule = str(raw.get("stop_rule") or "auto").replace("_", "-").lower()
    tick = raw.get("tick_size_assumed")
    if tick is not None:
        tick = _number(tick, "assumed tick")
        if tick < 0.01 or tick >= entry or Decimal(str(tick)).as_tuple().exponent < -2:
            raise MachineError("assumed tick must be a positive paise increment below entry")
    risk = entry - stop if side == "buy" else stop - entry
    return {"ticker": sym, "side": side, "date": day.isoformat(), "setup": str(raw.get("setup") or "orb"),
            "interval": interval, "entry": entry, "stop": stop, "target": target,
            "risk_per_share": risk, "target_r": abs(target - entry) / risk,
            "entry_time_ist": t.strftime("%H:%M"), "entry_bar": k,
            "or_minutes": or_minutes, "stop_rule": rule,
            "flat_by_ist": flat_time.strftime("%H:%M"), "entry_confirmed": bool(raw.get("entry_confirmed")),
            "trigger_at": trigger, "source": raw.get("source"), "data_origin": raw.get("data_origin"),
            "tick_size_assumed": tick}


def fetch_chart(sym):
    fallbacks = []
    if os.environ.get("UPSTOX_ANALYTICS_TOKEN", "").strip():
        try:
            from .intraday_sources import upstox_chart
            return upstox_chart(sym, range_="60d", interval=5)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            fallbacks.append("Upstox " + type(exc).__name__)
    if sym.endswith(".NS") and os.environ.get("FYERS_APP_ID") and os.environ.get("FYERS_ACCESS_TOKEN"):
        try:
            from .intraday_sources import fyers_chart
            return fyers_chart(sym, range_="60d", interval=5)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            fallbacks.append("FYERS " + type(exc).__name__)
    fallback = " (fallback after " + ", ".join(fallbacks) + ")" if fallbacks else ""
    suffix = urllib.parse.quote(sym, safe="") + "?range=60d&interval=5m"
    failures = []
    for host in ("query2", "query1"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{suffix}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (IndiaTraderPaperResearch/0.2.1)",
                                                  "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise MachineError("chart exceeds size limit")
            return raw, "Yahoo Finance unofficial 60d five-minute chart" + fallback
        except (urllib.error.URLError, TimeoutError, OSError, MachineError) as exc:
            failures.append(type(exc).__name__)
    raise MachineError("Yahoo chart unavailable at both hosts: " + ", ".join(failures))


def read_offline(path, sym):
    p = Path(path)
    if p.is_dir():
        candidates = [p / f"{sym}.json", p / f"{sym}.json.gz", p / f"{sym}_60d.json"]
        p = next((q for q in candidates if q.is_file()), candidates[0])
    if not p.is_file():
        raise MachineError(f"offline chart missing: {p}")
    raw = p.read_bytes()
    if len(raw) > MAX_BYTES:
        raise MachineError("offline chart exceeds size limit")
    if p.suffix == ".gz":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as packed:
            raw = packed.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise MachineError("decompressed chart exceeds size limit")
    return raw, str(p)


def parse_sessions(raw, sym, now):
    chart = parse_chart(raw, sym, "5m")
    meta_time = chart["data_time"]
    as_of = min(now, meta_time) if meta_time else now
    if meta_time is None:
        raise MachineError("chart has no last-trade timestamp to confirm completed bars")
    count_raw = len(json.loads(raw)["chart"]["result"][0].get("timestamp") or [])
    dropped = {"no_price": count_raw - len(chart["bars"]), "off_grid": 0,
               "not_complete": 0, "outside_hours": 0}
    sessions = {}
    for bar in chart["bars"]:
        at = bar["time"]
        start = datetime.combine(at.date(), OPEN, tzinfo=IST)
        delta = (at - start).total_seconds()
        if delta < 0 or delta >= SESSION_BARS * 300:
            dropped["outside_hours"] += 1
            continue
        if delta % 300:
            dropped["off_grid"] += 1
            continue
        if at + timedelta(minutes=5) > as_of:
            dropped["not_complete"] += 1
            continue
        slot = int(delta // 300)
        day = at.date().isoformat()
        sessions.setdefault(day, {})[slot] = {"o": bar["open"], "h": bar["high"],
                                               "l": bar["low"], "c": bar["close"],
                                               "v": bar["volume"]}
    return sessions, dropped, as_of


def full_sessions(days, before):
    full, short = [], []
    for day in sorted(days):
        if day >= before:
            continue
        slots = days[day]
        ok, _ = trading_day(date.fromisoformat(day))
        if ok and set(slots) == set(range(SESSION_BARS)):
            full.append((day, [slots[i] for i in range(SESSION_BARS)]))
        else:
            short.append({"date": day, "bars": len(slots)})
    return full, short


def build_pool(sessions):
    pool = [[] for _ in range(SESSION_BARS)]
    for _, bars in sessions:
        for i in range(1, SESSION_BARS):
            prev = bars[i - 1]["c"]
            pool[i].append(tuple(bars[i][k] / prev for k in ("o", "h", "l", "c")))
    return pool


def quantile(sorted_values, q):
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy"):
    if not 1 <= replays <= 1_000_000:
        raise MachineError("replays must be 1–1,000,000")
    if not 0 <= window <= 10:
        raise MachineError("window must be 0–10 bars")
    if last <= k:
        raise MachineError("replay cutoff leaves no bars after entry")
    rng = random.Random(seed)
    outcomes, closes, paths = [], [], []
    long = side == "buy"
    risk = entry - stop if long else stop - entry
    for n in range(replays):
        value, outcome = entry, None
        path = [round(entry, 2)] if n < SAMPLE_PATHS else None
        for i in range(k + 1, last + 1):
            chosen = i if window == 0 else max(1, min(SESSION_BARS - 1, i + rng.randint(-window, window)))
            o, h, l, c = rng.choice(pool[chosen])
            hi, lo, close = value * h, value * l, value * c
            if outcome is None:
                if (lo <= stop if long else hi >= stop):
                    outcome = "stop"  # same-bar stop/target ambiguity: stop first
                elif (hi >= target if long else lo <= target):
                    outcome = "target"
            value = close
            if path is not None:
                path.append(round(value, 2))
        outcomes.append(outcome or "neither")
        closes.append(value)
        if path is not None:
            paths.append(path)
    counts = Counter(outcomes)
    green = sum(1 for outcome, close in zip(outcomes, closes)
                if outcome == "target" or outcome == "neither" and (close > entry if long else close < entry))
    multiples = [abs(target - entry) / risk if outcome == "target" else -1.0 if outcome == "stop"
                 else ((close - entry) if long else (entry - close)) / risk
                 for outcome, close in zip(outcomes, closes)]
    sorted_closes = sorted(closes)
    # Re-seed so the first paths match the primary replay, as in the upstream fan chart.
    rng = random.Random(seed)
    all_paths = []
    for _ in range(BAND_PATHS):
        value, path = entry, [entry]
        for i in range(k + 1, last + 1):
            chosen = i if window == 0 else max(1, min(SESSION_BARS - 1, i + rng.randint(-window, window)))
            _, _, _, c = rng.choice(pool[chosen])
            value *= c
            path.append(value)
        all_paths.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(path[j] for path in all_paths)
        bands.append([round(quantile(col, q), 2) for q in (0.05, 0.25, 0.50, 0.75, 0.95)])
    lo, hi = quantile(sorted_closes, 0.01), quantile(sorted_closes, 0.99)
    span = (hi - lo) / HIST_BINS
    bins = [0] * HIST_BINS
    for close in sorted_closes:
        ix = int((close - lo) / span) if span else 0
        if 0 <= ix < HIST_BINS:
            bins[ix] += 1
    return {"replays": replays, "seed": seed, "window": window, "bars_left": last - k,
            "p_target": round(counts["target"] / replays, 4),
            "p_stop": round(counts["stop"] / replays, 4),
            "p_neither": round(counts["neither"] / replays, 4),
            "p_green": round(green / replays, 4),
            "median_close": round(quantile(sorted_closes, 0.5), 2),
            "p5_close": round(quantile(sorted_closes, 0.05), 2),
            "p95_close": round(quantile(sorted_closes, 0.95), 2),
            "avg_R": round(statistics.mean(multiples), 3),
            "r_at_target": round(abs(target - entry) / risk, 4),
            "bands": bands, "band_paths": BAND_PATHS, "sample_paths": paths,
            "hist": {"lo": round(lo, 2), "hi": round(hi, 2), "bins": bins}}


def _tick_down(value, tick):
    d = Decimal(str(value))
    t = Decimal(str(tick))
    return float((d / t).to_integral_value(rounding=ROUND_FLOOR) * t)


def _tick_up(value, tick):
    d = Decimal(str(value))
    t = Decimal(str(tick))
    return float((d / t).to_integral_value(rounding=ROUND_CEILING) * t)


def _tick_near(value, tick):
    d = Decimal(str(value))
    t = Decimal(str(tick))
    return float((d / t).to_integral_value(rounding=ROUND_HALF_UP) * t)


def history_rule(plan, entry_day=None):
    if plan["setup"] != "orb":
        return None
    long = plan["side"] == "buy"
    rule = plan["stop_rule"]
    tick = plan.get("tick_size_assumed") or 0.01
    if rule == "auto":
        rule = "pct"
        n = plan["or_minutes"] // 5
        if entry_day and all(i in entry_day for i in range(n)):
            high = max(entry_day[i]["h"] for i in range(n))
            low = min(entry_day[i]["l"] for i in range(n))
            rounded = _tick_down if long else _tick_up
            if abs(rounded((high + low) / 2, tick) - plan["stop"]) < tick / 10:
                rule = "or-mid"
            elif abs(rounded(low if long else high, tick) - plan["stop"]) < tick / 10:
                rule = "or-low" if long else "or-high"
    if rule not in (("or-mid", "or-low") if long else ("or-mid", "or-high")):
        rule = "pct"
    return {"setup": "orb", "or_minutes": plan["or_minutes"], "stop_rule": rule,
            "stop_pct": 100 * plan["risk_per_share"] / plan["entry"],
            "target_r": plan["target_r"], "tick_assumed": tick, "side": plan["side"]}


def replay_history(sessions, rule, last):
    if rule is None:
        return {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                "note": "Historical rule replay supports ORB only; this plan uses a different setup"}
    n = rule["or_minutes"] // 5
    long = rule.get("side", "buy") == "buy"
    trades = []
    tick = rule["tick_assumed"]
    for day, bars in sessions:
        high = max(b["h"] for b in bars[:n])
        low = min(b["l"] for b in bars[:n])
        hit = next((i for i in range(n, last + 1)
                    if (bars[i]["c"] > high if long else bars[i]["c"] < low)), None)
        if hit is None:
            continue
        entry = bars[hit]["c"]
        rounded = _tick_down if long else _tick_up
        if rule["stop_rule"] == "or-mid":
            stop = rounded((high + low) / 2, tick)
        elif rule["stop_rule"] in ("or-low", "or-high"):
            stop = rounded(low if long else high, tick)
        else:
            stop = rounded(entry * (1 - rule["stop_pct"] / 100 if long else
                                    1 + rule["stop_pct"] / 100), tick)
        if stop <= 0 or (stop >= entry if long else stop <= entry):
            continue
        risk = entry - stop if long else stop - entry
        target = _tick_near(entry + (1 if long else -1) * rule["target_r"] * risk, tick)
        outcome, close_price = "neither", bars[last]["c"]
        for bar in bars[hit + 1:last + 1]:
            if (bar["o"] <= stop if long else bar["o"] >= stop):
                outcome, close_price = "stop_gap", bar["o"]
                break
            if (bar["l"] <= stop if long else bar["h"] >= stop):
                outcome, close_price = "stop", stop
                break
            if (bar["h"] >= target if long else bar["l"] <= target):
                outcome, close_price = "target", target
                break
        r = ((close_price - entry) if long else (entry - close_price)) / risk
        trades.append({"date": day, "entry_time_ist": (datetime.combine(date.fromisoformat(day), OPEN) +
                       timedelta(minutes=5 * (hit + 1))).strftime("%H:%M"),
                       "entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2),
                       "outcome": outcome, "R": round(r, 3)})
    counts = Counter(x["outcome"] for x in trades)
    return {"sessions": len(sessions), "triggered": len(trades),
            "target": counts["target"], "stop": counts["stop"] + counts["stop_gap"],
            "stop_gap": counts["stop_gap"], "neither": counts["neither"],
            "green": sum(x["R"] > 0 for x in trades),
            "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
            "rule": rule, "trades": trades}


def verdict(mc):
    hit = mc["p_target"] + mc["p_stop"]
    if hit == 0:
        return "No replay hit either boundary; the exit-time price decides every simulated outcome."
    share = mc["p_target"] / hit
    even = 1 / (1 + mc["r_at_target"])
    delta = share - even
    if abs(delta) < 0.05:
        label = "Close to break-even among boundary hits"
    elif abs(delta) < 0.15:
        label = "Simulated boundary hits lean toward " + ("target" if delta > 0 else "stop")
    else:
        label = "Simulated boundary hits strongly favor " + ("target" if delta > 0 else "stop")
    return f"{label}: target won {share * 100:.1f}% of boundary hits; break-even at {even * 100:.1f}%."


def verdict_notes(mc):
    notes = []
    if mc["p_neither"] > 0.40:
        notes.append(f"{mc['p_neither'] * 100:.1f}% hit neither by flat-by; their final price affects average R.")
    hit = mc["p_target"] + mc["p_stop"]
    if hit:
        lean = mc["p_target"] / hit - 1 / (1 + mc["r_at_target"])
        if abs(lean) >= 0.05 and mc["avg_R"] and (mc["avg_R"] > 0) != (lean > 0):
            notes.append("Average R points the other way because of replays that hit neither.")
    return notes


def run_machine(raw, plan, now, replays=10000, seed=19, window=2, until=None, source="offline"):
    days, dropped, as_of = parse_sessions(raw, plan["ticker"], now)
    k = plan["entry_bar"]
    plan_day = days.get(plan["date"], {})
    if k not in plan_day:
        raise MachineError("plan entry close is absent or incomplete in the chart")
    until_time = _time(until or plan["flat_by_ist"], "until")
    end = datetime.combine(date.fromisoformat(plan["date"]), until_time)
    open_at = datetime.combine(date.fromisoformat(plan["date"]), OPEN)
    minutes = int((end - open_at).total_seconds() // 60)
    if minutes % 5 or not 10 <= minutes <= SESSION_BARS * 5:
        raise MachineError("until must be on the five-minute grid, after entry and no later than 15:15 IST")
    if until_time > _time(plan["flat_by_ist"], "plan flat-by"):
        raise MachineError("until cannot extend beyond the saved exit plan's flat-by")
    last = minutes // 5 - 1
    if last <= k:
        raise MachineError("until leaves no completed bars after entry")
    sessions, short = full_sessions(days, plan["date"])
    if len(sessions) < MIN_SESSIONS:
        raise MachineError(f"only {len(sessions)} full prior sessions; at least {MIN_SESSIONS} are required")
    pool = build_pool(sessions)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window,
                  plan["side"])
    mc.update({"until": until_time.strftime("%H:%M"), "pool_sessions": len(sessions),
               "pool_from": sessions[0][0], "pool_to": sessions[-1][0],
               "band_times": [(open_at + timedelta(minutes=5 * (i + 1))).strftime("%H:%M")
                              for i in range(k, last + 1)]})
    rule = history_rule(plan, plan_day)
    history = replay_history(sessions, rule, last)
    warnings = []
    entry_bar = plan_day[k]
    if not entry_bar["l"] - 0.01 <= plan["entry"] <= entry_bar["h"] + 0.01:
        warnings.append("Plan entry is outside the saved entry bar's high-low range")
    if not plan["entry_confirmed"]:
        warnings.append("Plan ahead: entry was not confirmed; scenario replay is hypothetical")
    if plan["date"] != now.date().isoformat():
        warnings.append("Historical session; this is not a current signal")
    if short:
        warnings.append(f"Excluded {len(short)} incomplete/gapped prior sessions")
    zero_volume = sum(1 for day, bars in sessions for bar in bars if bar["v"] in (None, 0))
    if zero_volume:
        warnings.append(f"{zero_volume} prior-session bars have missing/zero volume; price-only simulation")
    warnings += ["Yahoo chart is unofficial and not reconciled to NSE",
                 "Synthetic paths resample recent bars; no out-of-sample validation or probability calibration",
                 "MC stop is modeled at the stop even on gaps; actual loss can be worse than -1R",
                 "No fees, STT, taxes, spread, slippage, impact, corporate actions, or survivor correction"]
    return {"created_by": "india-trader-time-machine", "mode": "PAPER_RESEARCH_ONLY",
            "created_at": now.isoformat(), "source": source, "orders_possible": False,
            "data": {"ticker": plan["ticker"], "interval": "5m", "range": "60d",
                     "as_of": as_of.isoformat(), "dropped": dropped,
                     "short_sessions_left_out": short, "zero_volume_bars": zero_volume},
            "plan": plan, "mc": mc, "history": history,
            "verdict": verdict(mc), "verdict_notes": verdict_notes(mc),
            "warnings": warnings,
            "note": "Recent-bar scenario simulation and historical rule replay, not a forecast or guaranteed fill."}


def report(doc):
    mc, hist = doc["mc"], doc["history"]
    rule = hist.get("rule") or {}
    rule_label = ("ORB " + ("percentage-risk proxy" if rule.get("stop_rule") == "pct" else
                             str(rule.get("stop_rule")) + " stop")) if rule else "rule unavailable"
    lines = [f"{mc['replays']:,} seeded replays of {doc['plan']['ticker']} from {doc['plan']['entry_time_ist']} "
             f"to {mc['until']} IST; seed {mc['seed']}; {mc['pool_sessions']} full prior NSE sessions.",
             f"  target first {mc['p_target'] * 100:.1f}% | stop first {mc['p_stop'] * 100:.1f}% | "
             f"neither {mc['p_neither'] * 100:.1f}% | green {mc['p_green'] * 100:.1f}%",
             f"  Exit-time price: median Rs.{mc['median_close']:.2f}, 5–95% Rs.{mc['p5_close']:.2f}–Rs.{mc['p95_close']:.2f}",
             f"  Average R {mc['avg_R']:+.3f}",
             f"History ({rule_label}): {hist['triggered']} of {hist['sessions']} sessions triggered; "
             f"{hist.get('target', 0)} target, {hist.get('stop', 0)} stop, "
             f"{hist.get('neither', 0)} neither; average R {hist['avg_R'] if hist.get('avg_R') is not None else 'n/a'}.",
             doc["verdict"]]
    lines += doc["verdict_notes"]
    lines += ["note: " + x for x in doc["warnings"]]
    lines.append(doc["note"])
    return "\n".join(lines)


def render_chart(doc, height=17):
    mc = doc["mc"]
    bands = mc["bands"]
    if not bands:
        raise MachineError("saved result has no fan-chart bands")
    width = min(72, len(bands))
    selected = [bands[round(i * (len(bands) - 1) / max(1, width - 1))] for i in range(width)]
    low = min(x[0] for x in selected)
    high = max(x[4] for x in selected)
    if high <= low:
        high = low + 1
    grid = [[" " for _ in selected] for _ in range(height)]
    def row(value):
        return max(0, min(height - 1, round((high - value) / (high - low) * (height - 1))))
    for x, q in enumerate(selected):
        for y in range(row(q[4]), row(q[0]) + 1):
            grid[y][x] = "."
        for y in range(row(q[3]), row(q[1]) + 1):
            grid[y][x] = ":"
        grid[row(q[2])][x] = "*"
    lines = ["ASCII fan chart: * median, : 25–75%, . 5–95% (hypothetical prices)"]
    for y, chars in enumerate(grid):
        level = high - y * (high - low) / max(1, height - 1)
        lines.append(f"{level:9.2f} |" + "".join(chars))
    lines.append("          +" + "-" * width)
    lines.append(f"           {doc['plan']['entry_time_ist']} IST to {mc['until']} IST")
    bins = mc.get("hist", {}).get("bins", [])
    lines.append("Exit-time price histogram (24 bins; counts are simulated paths)")
    if bins:
        longest = max(bins) or 1
        for i, count in enumerate(bins):
            lines.append(f"{i + 1:02d} {'#' * round(count / longest * 30)} {count}")
    return "\n".join(lines)


def _save(path, doc):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m india_trader time-machine",
                                description="Seeded 60-day NSE paper scenario simulation")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="10,000 synthetic paths and past ORB rule replay")
    run.add_argument("--plan", default="trader/plan.json")
    run.add_argument("--symbol")
    run.add_argument("--side", choices=("buy", "sell"), default="buy")
    run.add_argument("--date")
    run.add_argument("--entry", type=float)
    run.add_argument("--stop", type=float)
    run.add_argument("--target", type=float)
    run.add_argument("--entry-time")
    run.add_argument("--entry-bar", type=int)
    run.add_argument("--setup", choices=("orb", "level"), default="orb")
    run.add_argument("--or-minutes", type=int, default=15)
    run.add_argument("--stop-rule", default="auto", choices=("auto", "or-mid", "or-low", "or-high", "pct"))
    run.add_argument("--replays", type=int, default=10000)
    run.add_argument("--seed", type=int, default=19)
    run.add_argument("--window", type=int, default=2)
    run.add_argument("--until", help="IST five-minute close, no later than the plan's flat-by")
    run.add_argument("--offline", metavar="FILE", help="saved Yahoo 60-day JSON or .json.gz")
    run.add_argument("--now", help="IST or offset-aware ISO cutoff, for reproducible runs")
    run.add_argument("--json", action="store_true")
    run.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH")
    chart = sub.add_parser("chart", help="ASCII fan chart and exit-price histogram from a saved run")
    chart.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json",
                       default="trader/timemachine.json")
    a = p.parse_args(argv)
    try:
        if a.command == "chart":
            doc = json.loads(Path(a.from_file).read_text(encoding="utf-8"))
            if doc.get("created_by") != "india-trader-time-machine":
                raise MachineError("saved file is not an India Time Machine result")
            print(render_chart(doc))
            return 0
        now = _now(a.now)
        plan = load_plan(a, now)
        raw, source = read_offline(a.offline, plan["ticker"]) if a.offline else fetch_chart(plan["ticker"])
        doc = run_machine(raw, plan, now, a.replays, a.seed, a.window, a.until, source)
        if a.write:
            _save(a.write, doc)
        print(json.dumps(doc, indent=2) if a.json else report(doc))
        if a.write and not a.json:
            print("Saved " + a.write)
        return 0
    except (MachineError, EntryError, ValueError, OSError, EOFError, KeyError, TypeError) as exc:
        print("time-machine: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
