"""NSE paper exit planner adapted from upstream MIT trader-exit workflow."""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
import json
from pathlib import Path
import re
import sys

from .core import IST
from .entry_timer import get_chart, ticker
from .market_scanner import trading_day

DEFAULT_FLAT = time(15, 10)
OPEN = time(9, 15)
MAX_FLAT = time(15, 10)  # conservative before NSE CAS transition
MIN_RISK_PCT = Decimal("0.05")
NOTE = "Stop and target are a paper plan, not guaranteed fills; gaps and costs can exceed the estimate."


class ExitError(Exception):
    pass


def number(value, label):
    try:
        x = Decimal(str(value).strip().replace(",", ""))
        if not x.is_finite():
            raise InvalidOperation
        return x
    except (InvalidOperation, TypeError, AttributeError) as exc:
        raise ExitError(f"{label} must be a finite number") from exc


def price(value, label):
    x = number(value, label)
    if x <= 0:
        raise ExitError(f"{label} must be positive")
    return x


def parse_clock(value, label):
    if not value:
        return None
    text = str(value).strip().upper()
    for fmt in ("%H:%M", "%I:%M %p"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    raise ExitError(f"{label} must be HH:MM IST")


def as_ist(value, label):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ExitError(f"{label} must be ISO date/time") from exc
    return (dt.replace(tzinfo=IST) if dt.tzinfo is None else dt).astimezone(IST)


def settings(path):
    cfg = {"flat_by": DEFAULT_FLAT, "min_reward_risk": Decimal("1")}
    p = Path(path)
    if p.is_file():
        doc = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ExitError("config must be a JSON object")
        if doc.get("flat_by") is not None:
            cfg["flat_by"] = parse_clock(doc["flat_by"], "flat_by")
        if doc.get("min_reward_risk") is not None:
            cfg["min_reward_risk"] = number(doc["min_reward_risk"], "min_reward_risk")
    if not OPEN < cfg["flat_by"] <= MAX_FLAT:
        raise ExitError("flat_by must be after 09:15 and no later than 15:10 IST")
    if cfg["min_reward_risk"] <= 0:
        raise ExitError("min_reward_risk must be positive")
    return cfg


def read_entry(path):
    p = Path(path)
    if not p.is_file():
        raise ExitError(f"entry file missing: {p}")
    doc = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ExitError("entry file must contain an object")
    return doc


def gather(a, now):
    """Normalize current timer, legacy entry, and manual plan-ahead inputs."""
    if a.symbol and a.from_path:
        raise ExitError("use --symbol or --from, not both")
    if a.entry_bar is not None and a.entry_bar < 0:
        raise ExitError("entry bar index must be nonnegative")
    if a.side in ("sell", "short"):
        raise ExitError("only long cash-equity paper exits are supported")
    if a.symbol:
        sym = ticker(a.symbol)
        if a.entry is None:
            raise ExitError("--symbol needs --entry PRICE")
        doc = {}
        source = "manual flags"
    else:
        source = a.from_path or "trader/entry.json"
        doc = read_entry(source)
        sym = ticker(doc.get("ticker") or doc.get("symbol"))
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise ExitError("entry file is not a long setup")
    status = str(doc.get("status") or "").upper()
    confirmed = bool(doc) and status == "TRIGGERED" and a.entry is None
    if doc and not confirmed and a.entry is None:
        raise ExitError("entry has not triggered; supply --entry to make a plan ahead")
    e = price(a.entry if a.entry is not None else doc.get("entry_price", doc.get("price")), "entry")
    setup = a.setup or doc.get("setup") or ("orb" if a.or_high is not None else "level")
    if setup not in ("orb", "level"):
        raise ExitError("setup must be orb or level")
    interval = a.interval or doc.get("bar_interval") or doc.get("interval") or "5m"
    if interval not in ("1m", "5m"):
        raise ExitError("interval must be 1m or 5m")
    or_minutes = a.or_minutes or doc.get("opening_minutes") or (
        doc.get("opening_range") or {}).get("minutes") or 15
    step = 5 if interval == "5m" else 1
    if not isinstance(or_minutes, int) or or_minutes <= 0 or or_minutes > 60 or or_minutes % step:
        raise ExitError("opening-range minutes must be 1–60 whole interval bars")
    raw_trigger = doc.get("trigger_at") if confirmed else None
    trigger = as_ist(raw_trigger, "trigger_at") if raw_trigger else None
    if confirmed and trigger is None and doc.get("date") and doc.get("time"):
        legacy_clock = parse_clock(doc["time"], "entry time")
        trigger = datetime.combine(date.fromisoformat(str(doc["date"])), legacy_clock, tzinfo=IST)
    if confirmed and trigger is None:
        raise ExitError("triggered entry needs its completed-bar time")
    source_date = (str(doc.get("trigger_at") or doc.get("as_of") or "")[:10] or doc.get("date"))
    raw_date = a.date or (trigger.date().isoformat() if trigger else source_date) or now.date().isoformat()
    try:
        day = date.fromisoformat(str(raw_date))
    except ValueError as exc:
        raise ExitError("date must be YYYY-MM-DD") from exc
    if trigger and trigger.date() != day:
        raise ExitError("entry date and trigger timestamp disagree")
    clock = parse_clock(a.time, "entry time") if a.time else (trigger.time() if trigger else None)
    if confirmed and a.time and clock != trigger.time():
        raise ExitError("--time cannot change a confirmed trigger time")
    if not clock and doc.get("time") and confirmed:
        clock = parse_clock(doc["time"], "entry time")
    if clock and not OPEN <= clock < MAX_FLAT:
        raise ExitError("entry time must be 09:15–15:10 IST")
    opening = doc.get("opening_range") if isinstance(doc.get("opening_range"), dict) else {}
    high = a.or_high if a.or_high is not None else doc.get("range_high", opening.get("high"))
    low = a.or_low if a.or_low is not None else doc.get("range_low", opening.get("low"))
    if (high is None) != (low is None):
        raise ExitError("provide both opening-range high and low")
    if high is not None:
        high, low = price(high, "opening-range high"), price(low, "opening-range low")
        if high <= low:
            raise ExitError("opening-range high must exceed low")
    valid, why = trading_day(day)
    if not valid:
        raise ExitError("no normal NSE session: " + why)
    if a.entry is not None and doc:
        trigger = None  # override never inherits a historical trigger
    return {"ticker": sym, "entry": e, "setup": setup, "interval": interval, "date": day,
            "entry_time": clock, "trigger_at": trigger, "confirmed": confirmed,
            "or_high": high, "or_low": low, "or_minutes": or_minutes,
            "source": source, "data_origin": doc.get("data_origin") or doc.get("source"),
            "entry_bar": a.entry_bar if a.entry_bar is not None else doc.get("trigger_bar_index", doc.get("bar_index")),
            "volume_quality": doc.get("volume_quality")}


def atr_from_chart(chart, day, interval, through, n=14):
    """Session true range through the entry close; reject gaps and future bars."""
    step = 5 if interval == "5m" else 1
    open_at = datetime.combine(day, OPEN, tzinfo=IST)
    slots = {}
    for bar in chart["bars"]:
        at = bar["time"]
        delta = (at - open_at).total_seconds()
        if at.date() == day and delta >= 0 and delta % (step * 60) == 0 and at + timedelta(minutes=step) <= through:
            slots[int(delta // (step * 60))] = bar
    if len(slots) < 3:
        raise ExitError("ATR needs at least three completed session bars through the entry")
    ordered = sorted(slots)
    if ordered != list(range(ordered[-1] + 1)):
        raise ExitError("ATR source has a missing bar before the entry")
    if through < slots[ordered[-1]]["time"] + timedelta(minutes=step):
        raise ExitError("ATR cutoff is before last completed bar")
    trs, prev = [], None
    for slot in ordered:
        bar = slots[slot]
        tr = bar["high"] - bar["low"]
        if prev is not None:
            tr = max(tr, abs(bar["high"] - prev), abs(bar["low"] - prev))
        trs.append(tr)
        prev = bar["close"]
    return sum(trs[-n:]) / min(n, len(trs)), min(n, len(trs)), len(slots)


def calculate(info, stop_spec, target_spec, tick, flat_by, min_rr, atr=None):
    e = info["entry"]
    tick = price(tick, "tick")
    if tick < Decimal("0.01") or tick.as_tuple().exponent < -2 or tick >= e:
        raise ExitError("tick must be an explicit positive paise increment below the entry")
    stop_rule = (stop_spec or ("or-mid" if info["setup"] == "orb" else "")).lower().strip()
    stop_note = None
    if stop_rule == "or-mid":
        if info["or_high"] is None:
            raise ExitError("or-mid stop needs opening-range high and low")
        raw_stop = (info["or_high"] + info["or_low"]) / 2
    elif stop_rule == "or-low":
        if info["or_low"] is None:
            raise ExitError("or-low stop needs opening-range low")
        raw_stop = info["or_low"]
    elif stop_rule.startswith("price:"):
        raw_stop = price(stop_rule[6:], "stop price")
    elif stop_rule.startswith("pct:"):
        pct = price(stop_rule[4:], "stop percentage")
        if pct >= 100:
            raise ExitError("stop percentage must be below 100")
        raw_stop = e * (1 - pct / 100)
    elif stop_rule.startswith("atr:"):
        if atr is None:
            raise ExitError("atr:K stop needs completed session bars")
        k = price(stop_rule[4:], "ATR multiplier")
        raw_stop = e - k * number(atr[0], "ATR")
        stop_note = f"ATR(14) from {atr[1]} completed bars; {atr[2]} session bars available"
    else:
        raise ExitError("stop must be or-mid, or-low, price:X, pct:X, or atr:K; level setups need an explicit stop")
    if raw_stop >= e or raw_stop <= 0:
        raise ExitError("stop must be below the entry and above zero")
    stop = (raw_stop / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    if stop <= 0 or stop >= e:
        raise ExitError("rounded stop must be below the entry and above zero")
    risk = e - stop
    risk_pct = risk / e * 100
    if risk_pct < MIN_RISK_PCT:
        raise ExitError("stop is under 0.05% from entry; risk distance is too small")
    if target_spec.lower().startswith("price:"):
        raw_target = price(target_spec[6:], "target price")
        target_rule = "price"
        target_r = None
    else:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[Rr]\s*", target_spec)
        if not match:
            raise ExitError("target must be 1R, 1.5R, 2R, or price:X")
        target_r = price(match.group(1), "target R multiple")
        raw_target = e + target_r * risk
        target_rule = "R"
    if raw_target <= e:
        raise ExitError("target must be above the entry")
    target = (raw_target / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    if target <= e:
        raise ExitError("rounded target must be above the entry")
    reward_risk = (target - e) / risk
    warnings = []
    if reward_risk < min_rr:
        warnings.append(f"reward:risk {reward_risk:.2f} is below configured minimum {min_rr}; review before paper sizing")
    if not info["confirmed"]:
        warnings.append("Plan ahead: no completed entry trigger has been recorded")
    if info["date"] != datetime.now(IST).date():
        warnings.append("Historical/future session plan; not a current entry")
    if info["data_origin"] and "Yahoo" in info["data_origin"]:
        warnings.append("Entry price source is unofficial Yahoo; not exchange reconciled")
    if info.get("volume_quality") == "MISSING_OR_ZERO":
        warnings.append("Entry chart contained missing/zero volume; price-only rule was used")
    plan = {"mode": "PAPER_PLAN_ONLY", "status": "PLANNED", "created_by": "india-trader-exit",
            "ticker": info["ticker"], "symbol": info["ticker"], "side": "BUY_LONG_CASH",
            "date": info["date"].isoformat(), "setup": info["setup"], "interval": info["interval"],
            "entry_confirmed": info["confirmed"], "entry_reference": float(e), "entry": float(e),
            "entry_time_ist": info["entry_time"].strftime("%H:%M") if info["entry_time"] else None,
            "entry_bar": info["entry_bar"], "opening_range": {"high": float(info["or_high"]),
            "low": float(info["or_low"]), "minutes": int(info["or_minutes"])} if info["or_high"] else None,
            "stop": float(stop), "stop_rule": stop_rule, "stop_raw": float(raw_stop),
            "target": float(target), "target_rule": target_spec, "target_r": float(target_r) if target_r else float(reward_risk),
            "risk_per_share_inr": float(risk), "risk_per_share": float(risk),
            "risk_pct": float(risk_pct), "reward_risk": float(reward_risk),
            "tick_size_assumed": float(tick), "flat_by_ist": flat_by.strftime("%H:%M"),
            "flat_by": flat_by.strftime("%H:%M"), "trigger_at": info["trigger_at"].isoformat() if info["trigger_at"] else None,
            "data_origin": info["data_origin"], "source_entry": info["source"],
            "volume_quality": info.get("volume_quality"),
            "atr": round(atr[0], 4) if atr else None, "atr_bars": atr[1] if atr else None,
            "stop_note": stop_note, "warnings": warnings, "orders_possible": False, "note": NOTE}
    return plan


def render(plan):
    e = number(plan["entry_reference"], "entry")
    risk = number(plan["risk_per_share_inr"], "risk")
    risk_pct = float(number(plan.get("risk_pct", risk / e * 100), "risk percentage"))
    date_label = plan.get("date") or str(plan.get("trigger_at") or "")[:10] or "date unknown"
    interval = plan.get("interval", "5m")
    entry_time = plan.get("entry_time_ist") or str(plan.get("trigger_at") or "")[11:16]
    lines = [f"Paper exit plan: {plan['ticker']} long, {date_label} ({interval} bars)",
             f"  Entry         Rs.{plan['entry_reference']:.2f}" +
             (f" at {entry_time} IST" if entry_time else " (planned)"),
             f"  Stop          Rs.{plan['stop']:.2f}  {plan['stop_rule']}",
             f"  Target        Rs.{plan['target']:.2f}  {plan['target_rule']}",
             f"  Risk/share    Rs.{plan['risk_per_share_inr']:.2f} ({risk_pct:.2f}%)",
             f"  Reward:risk   {plan['reward_risk']:.2f}",
             f"  Flat by       {plan['flat_by_ist']} IST"]
    if plan.get("stop_note"):
        lines.append("  " + plan["stop_note"])
    lines += ["  note: " + x for x in plan.get("warnings", [])]
    lines.append(NOTE)
    return "\n".join(lines)


def _save(path, doc):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def command_plan(a):
    now = as_ist(a.now, "--now") if a.now else datetime.now(IST)
    info = gather(a, now)
    cfg = settings(a.config)
    flat = parse_clock(a.flat_by, "flat-by") if a.flat_by else cfg["flat_by"]
    if not OPEN < flat <= MAX_FLAT:
        raise ExitError("flat-by must be after 09:15 and no later than 15:10 IST")
    if info["entry_time"] and info["entry_time"] >= flat:
        raise ExitError("entry occurs at or after flat-by; no holding time remains")
    atr = None
    if (a.stop or "").lower().startswith("atr:"):
        if not info["entry_time"]:
            raise ExitError("ATR needs an entry time to exclude future bars")
        chart = get_chart(info["ticker"], info["interval"], info["date"], a.offline)
        through = info["trigger_at"] or datetime.combine(info["date"], info["entry_time"], tzinfo=IST)
        atr = atr_from_chart(chart, info["date"], info["interval"], through)
        info["data_origin"] = chart["source"]
    plan = calculate(info, a.stop, a.target, a.tick, flat, cfg["min_reward_risk"], atr)
    if a.now:
        plan["warnings"].append("As-of override used for replay; not evidence of a live signal")
    if a.write:
        _save(a.write, plan)
    print(json.dumps(plan, indent=2) if a.json else render(plan))
    if a.write and not a.json:
        print("Saved " + a.write)
    return 0


def command_show(a):
    doc = read_entry(a.plan)
    if doc.get("mode") != "PAPER_PLAN_ONLY" or doc.get("status") != "PLANNED":
        raise ExitError("saved file is not a paper exit plan")
    shown = dict(doc)
    shown["warnings"] = list(doc.get("warnings") or [])
    if doc.get("date") != datetime.now(IST).date().isoformat():
        shown["warnings"].append("Saved plan is for a different session; not a current entry")
    if a.now:
        as_ist(a.now, "--now")
        shown["warnings"].append("As-of override used for replay; not evidence of a live signal")
    print(json.dumps(shown, indent=2) if a.json else render(shown))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m india_trader exit-planner",
                                description="NSE paper stop, target, and flat-by plan")
    sub = p.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="make a plan from entry.json or manual price")
    plan.add_argument("--from", dest="from_path", help="entry JSON (default trader/entry.json)")
    plan.add_argument("--symbol")
    plan.add_argument("--entry", help="manual or plan-ahead entry price")
    plan.add_argument("--setup", choices=("orb", "level"))
    plan.add_argument("--or-high")
    plan.add_argument("--or-low")
    plan.add_argument("--or-minutes", type=int)
    plan.add_argument("--date", help="session YYYY-MM-DD")
    plan.add_argument("--time", help="entry HH:MM IST")
    plan.add_argument("--entry-bar", type=int, help="entry bar index from the opening bell")
    plan.add_argument("--interval", choices=("1m", "5m"))
    plan.add_argument("--side", choices=("buy", "long", "sell", "short"), default="buy")
    plan.add_argument("--stop", help="or-mid, or-low, price:X, pct:X, atr:K")
    plan.add_argument("--target", default="2R", help="R multiple or price:X")
    plan.add_argument("--tick", required=True, help="verified instrument-specific INR tick assumption")
    plan.add_argument("--flat-by", help="HH:MM IST, at most 15:10")
    plan.add_argument("--config", default="trader/config.json")
    plan.add_argument("--offline", help="saved Yahoo chart JSON for ATR")
    plan.add_argument("--now", help="IST or offset-aware ISO time for reproducibility")
    plan.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH")
    plan.add_argument("--json", action="store_true")
    show = sub.add_parser("show", help="display a saved paper plan")
    show.add_argument("--plan", default="trader/plan.json")
    show.add_argument("--now", help="as-of replay clock; displayed plan is still labeled by real session age")
    show.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        return command_plan(a) if a.command == "plan" else command_show(a)
    except (ExitError, OSError, ValueError, json.JSONDecodeError) as exc:
        print("exit-planner: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
