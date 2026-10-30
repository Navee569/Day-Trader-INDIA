"""Read-only entry and exit handoff for Indian cash-equity paper research."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR

from .core import IST, DataError, as_datetime, decimal, number, orb_signal, session_bars
from .market_calendar import regular_nse_entry_day


def entry_check(rows: list[dict], ticker: str, setup: str = "orb", level: float | None = None,
                as_of: str | None = None, opening_minutes: int = 15,
                min_volume_x: float = 0) -> dict:
    """Use completed five-minute closes only; never place or imply an order."""
    if setup not in ("orb", "level"):
        raise ValueError("Entry setup must be orb or level")
    cutoff = datetime.now(IST) if as_of is None else as_datetime(as_of)
    threshold = number(level, "entry level") if setup == "level" else None
    minimum = number(min_volume_x, "minimum volume multiple", allow_zero=True)
    day = session_bars(rows, cutoff.date().isoformat())
    base = {"mode": "PAPER_RESEARCH_ONLY", "ticker": ticker, "setup": setup,
            "as_of": cutoff.isoformat(), "bar_interval": "5m", "orders_possible": False}
    if ticker.endswith(".NS") and not regular_nse_entry_day(cutoff.date()):
        return {**base, "status": "NSE_REGULAR_SESSION_UNVERIFIED_OR_CLOSED"}
    if not day:
        return {**base, "status": "NO_DATA"}
    if day[0].timestamp.date() != cutoff.date():
        return {**base, "status": "NO_SESSION_FOR_CUTOFF"}
    completed = [b for b in day if b.timestamp + timedelta(minutes=5) <= cutoff]
    if not completed:
        return {**base, "status": "WAIT", "reason": "No completed bar"}
    if any(b.volume == 0 for b in completed):
        return {**base, "status": "ZERO_VOLUME_BAR"}
    if any(b.timestamp - a.timestamp != timedelta(minutes=5) for a, b in zip(completed, completed[1:])):
        return {**base, "status": "DATA_GAP"}
    last_end = completed[-1].timestamp + timedelta(minutes=5)
    if (cutoff.date() == last_end.date() and time(9, 15) < cutoff.time() < time(15, 15)
            and cutoff - last_end > timedelta(minutes=10)):
        return {**base, "status": "STALE_DATA", "last_complete_bar_end": last_end.isoformat()}
    range_high = range_low = None
    if setup == "orb":
        signal = orb_signal(rows, opening_minutes, cutoff, cutoff.date().isoformat())
        if signal["status"] not in ("WAIT", "TRIGGERED_LONG", "DOWNSIDE_BREAK_NO_LONG"):
            return {**base, "status": signal["status"], "reason": signal.get("detail")}
        if signal["status"] == "DOWNSIDE_BREAK_NO_LONG":
            return {**base, "status": "DOWNSIDE_BREAK_NO_LONG",
                    "reason": "Opening range broke down before an eligible long breakout"}
        range_high, range_low = signal["range_high"], signal["range_low"]
        threshold = range_high
        candidates = [b for b in completed if b.timestamp.time() >=
                      (datetime.combine(b.timestamp.date(), time(9, 15), tzinfo=IST)
                       + timedelta(minutes=opening_minutes)).time()]
    else:
        candidates = [b for b in completed if b.timestamp.time() >= time(9, 30)]
    if not candidates:
        return {**base, "status": "WAIT", "level": threshold,
                "range_high": range_high, "range_low": range_low}
    opening = completed[:max(1, opening_minutes // 5)]
    opening_volume = None if any(b.volume is None for b in opening) else sum(b.volume for b in opening) / len(opening)
    for bar in candidates:
        if bar.close <= threshold:
            continue
        if minimum:
            if bar.volume is None or opening_volume in (None, 0):
                return {**base, "status": "VOLUME_UNVERIFIED", "level": threshold}
            if bar.volume < minimum * opening_volume:
                continue
        end = bar.timestamp + timedelta(minutes=5)
        if end.time() < time(9, 30) or end.time() > time(15, 10):
            return {**base, "status": "OUTSIDE_ENTRY_WINDOW", "trigger_at": end.isoformat()}
        if cutoff - end > timedelta(minutes=5):
            return {**base, "status": "STALE_SIGNAL", "trigger_at": end.isoformat(),
                    "signal_age_minutes": round((cutoff - end).total_seconds() / 60, 2),
                    "note": "Historical trigger; it is too old for a new paper entry"}
        return {**base, "status": "TRIGGERED", "entry_price": bar.close,
                "trigger_at": end.isoformat(), "trigger_bar_start": bar.timestamp.isoformat(),
                "level": threshold, "range_high": range_high, "range_low": range_low,
                "signal_age_minutes": round((cutoff - end).total_seconds() / 60, 2),
                "volume_multiple": round(bar.volume / opening_volume, 3)
                if bar.volume is not None and opening_volume else None,
                "note": "Completed-bar paper signal only; no fill or order"}
    return {**base, "status": "EXPIRED" if cutoff.time() >= time(15, 10) else "WAIT",
            "level": threshold, "last_close": completed[-1].close,
            "range_high": range_high, "range_low": range_low}


def _atr(bars: list[dict], trigger_at: str, n: int = 14) -> tuple[float, int]:
    from .core import parse_bars
    cutoff = as_datetime(trigger_at)
    completed = [b for b in parse_bars(bars) if b.timestamp + timedelta(minutes=5) <= cutoff]
    if not completed:
        raise DataError("ATR stop needs completed bars through entry")
    true_ranges = []
    for i, bar in enumerate(completed):
        prev = completed[i - 1].close if i else bar.open
        true_ranges.append(max(bar.high - bar.low, abs(bar.high - prev), abs(bar.low - prev)))
    sample = true_ranges[-n:]
    return sum(sample) / len(sample), len(sample)


def exit_plan(entry: dict, stop_spec: str = "or-mid", target_spec: str = "2R",
              tick_size: str = "0.05", bars: list[dict] | None = None,
              flat_by: str = "15:10") -> dict:
    """Set stop and target before a hypothetical paper fill."""
    if entry.get("status") != "TRIGGERED":
        raise ValueError("Exit plan requires a triggered entry")
    price = decimal(entry["entry_price"], "entry price")
    tick = decimal(tick_size, "tick size")
    if not (0 < price <= 1_000_000_000 and decimal("0.0001", "minimum tick") <= tick <= price):
        raise ValueError("Entry must be positive and tick must be 0.0001 through the entry price")
    stop_note = None
    if stop_spec == "or-mid":
        if entry.get("range_high") is None or entry.get("range_low") is None:
            raise ValueError("Opening-range midpoint requires an ORB entry")
        raw_stop = (decimal(entry["range_high"], "range high") + decimal(entry["range_low"], "range low")) / 2
    elif stop_spec == "or-low":
        if entry.get("range_low") is None:
            raise ValueError("Opening-range low requires an ORB entry")
        raw_stop = decimal(entry["range_low"], "range low")
    elif stop_spec.startswith("price:"):
        raw_stop = decimal(stop_spec[6:], "stop price")
    elif stop_spec.startswith("pct:"):
        pct = decimal(stop_spec[4:], "stop percentage")
        if not 0 < pct < 100:
            raise ValueError("Stop percentage must be between 0 and 100")
        raw_stop = price * (1 - pct / 100)
    elif stop_spec.startswith("atr:"):
        if bars is None:
            raise ValueError("ATR stop requires saved five-minute bars")
        k = decimal(stop_spec[4:], "ATR multiplier")
        if k <= 0:
            raise ValueError("ATR multiplier must be positive")
        atr, count = _atr(bars, entry["trigger_at"])
        raw_stop = price - k * decimal(atr, "ATR")
        stop_note = f"ATR uses {count} completed bars (14 preferred)"
    else:
        raise ValueError("Stop must be or-mid, or-low, price:X, pct:X, or atr:K")
    stop = (raw_stop / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    if stop <= 0 or stop >= price or (price - stop) / price < decimal("0.0005", "minimum risk"):
        raise ValueError("Stop must be below entry with at least 0.05% price risk")
    risk = price - stop
    if target_spec.lower().startswith("price:"):
        raw_target = decimal(target_spec[6:], "target price")
    else:
        text = target_spec.upper().removesuffix("R")
        multiple = decimal(text, "target R multiple")
        if multiple <= 0:
            raise ValueError("Target multiple must be positive")
        raw_target = price + multiple * risk
    target = (raw_target / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    if target <= price:
        raise ValueError("Target must be above entry")
    try:
        closing = time.fromisoformat(flat_by)
    except ValueError as exc:
        raise ValueError("flat_by must be HH:MM IST") from exc
    if closing > time(15, 10) or closing < time(9, 45):
        raise ValueError("flat_by must be 09:45–15:10 IST for the conservative session")
    return {"mode": "PAPER_PLAN_ONLY", "status": "PLANNED", "ticker": entry["ticker"],
            "setup": entry["setup"], "entry_reference": float(price), "stop": float(stop),
            "target": float(target), "risk_per_share_inr": float(risk),
            "reward_risk": float((target - price) / risk), "tick_size_assumed": float(tick),
            "flat_by_ist": flat_by, "trigger_at": entry["trigger_at"],
            "data_origin": entry.get("data_origin"),
            "stop_rule": stop_spec, "target_rule": target_spec, "stop_note": stop_note,
            "orders_possible": False, "note": "A stop is a plan, not a guaranteed fill"}
