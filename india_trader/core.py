"""Defensive, paper-only Indian cash equity analytics. No brokerage operations.

Only a conservative 09:15-15:15 continuous-trading research window is
modelled; closing auctions, holidays and security tick sizes are NOT inferred.
"""
from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Iterable

# A fixed offset avoids Windows' optional IANA tzdata package; IST has no DST.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
OPEN = time(9, 15)
CLOSE = time(15, 15)  # Conservative continuous-session cutoff; CAS may start here
BAR_MINUTES = 5
MAX_CSV_ROWS = 100_000
MAX_CSV_SIZE = 20 * 1024 * 1024


class DataError(ValueError):
    """Unsafe, inconsistent or incomplete market-data input."""


def symbol(raw: str, exchange: str = "NSE") -> str:
    """Normalize NSE/BSE cash symbols, rejecting foreign suffixes/URL injections."""
    if not isinstance(raw, str):
        raise ValueError("Symbol must be text")
    s = raw.strip().upper()
    e = exchange.strip().upper()
    if e not in ("NSE", "BSE"):
        raise ValueError("Exchange must be NSE or BSE")
    if s.endswith((".NS", ".BO")):
        suffix = s[-3:]
        if (e == "NSE" and suffix != ".NS") or (e == "BSE" and suffix != ".BO"):
            raise ValueError("Symbol suffix conflicts with --exchange")
        s = s[:-3]
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9&_-]{0,29}", s) or ".." in s:
        raise ValueError("Invalid NSE/BSE cash symbol")
    return s + (".NS" if e == "NSE" else ".BO")


def as_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            raise DataError(f"Invalid ISO 8601 timestamp: {value!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise DataError("Every bar timestamp must have an explicit UTC offset")
    return dt.astimezone(IST)


def number(value: Any, label: str, *, allow_zero: bool = False) -> float:
    try:
        if value is None or isinstance(value, bool):
            raise ValueError()
        v = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise DataError(f"Invalid {label}") from exc
    if not math.isfinite(v) or (v < 0 if allow_zero else v <= 0):
        raise DataError(f"Invalid {label}: expected finite {'nonnegative' if allow_zero else 'positive'} number")
    return v


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "Bar":
        if not isinstance(raw, dict):
            raise DataError("A candle must be an object")
        ts = as_datetime(raw.get("timestamp"))
        op, hi, lo, cl = (number(raw.get(k), k) for k in ("open", "high", "low", "close"))
        vol = None if raw.get("volume") in (None, "") else number(raw["volume"], "volume", allow_zero=True)
        if hi < max(op, cl) or lo > min(op, cl) or hi < lo:
            raise DataError(f"OHLC inconsistency at {ts.isoformat()}")
        # 5-minute candle timestamps denote the BEGINNING of a bar.
        if ts.second or ts.microsecond or ts.minute % BAR_MINUTES:
            raise DataError(f"Candle timestamp is not aligned to 5-minute boundary: {ts.isoformat()}")
        return cls(ts, op, hi, lo, cl, vol)

    def record(self) -> dict[str, Any]:
        return {"timestamp": self.timestamp.isoformat(), "open": self.open,
                "high": self.high, "low": self.low, "close": self.close, "volume": self.volume}


def parse_bars(rows: Iterable[dict[str, Any]]) -> list[Bar]:
    result: list[Bar] = []
    seen = set()
    for i, raw in enumerate(rows):
        if i >= MAX_CSV_ROWS:
            raise DataError(f"Too many bars (maximum {MAX_CSV_ROWS})")
        # Revalidate caller-created Bar instances as rigorously as dictionaries.
        # A frozen dataclass alone does not prohibit NaN, negative values, or
        # naive / misaligned timestamps.
        bar = Bar.parse(raw.record()) if isinstance(raw, Bar) else Bar.parse(raw)
        if bar.timestamp in seen:
            raise DataError(f"Duplicate candle at {bar.timestamp.isoformat()}")
        seen.add(bar.timestamp)
        result.append(bar)
    result.sort(key=lambda b: b.timestamp)
    return result


def read_csv(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if path.stat().st_size > MAX_CSV_SIZE:
        raise DataError("CSV exceeds 20 MiB safety limit")
    with path.open(newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        required = {"timestamp", "open", "high", "low", "close"}
        if not rd.fieldnames or not required.issubset(rd.fieldnames):
            raise DataError("CSV requires timestamp,open,high,low,close (optional volume)")
        if len(set(rd.fieldnames)) != len(rd.fieldnames):
            raise DataError("Duplicate CSV column headers")
        def validated_rows():
            for row in rd:
                if None in row:
                    raise DataError("CSV contains more cells than its column headers")
                yield row
        rows = parse_bars(validated_rows())
        return [b.record() for b in rows]


def read_json_bars(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if path.stat().st_size > MAX_CSV_SIZE:
        raise DataError("JSON exceeds 20 MiB safety limit")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        if "bars" not in data:
            raise DataError("JSON object requires a bars array")
        data = data["bars"]
    if not isinstance(data, list):
        raise DataError("JSON must be array of bars or an object with bars array")
    return [b.record() for b in parse_bars(data)]


def session_bars(rows: Iterable[dict[str, Any]], date: str | None = None) -> list[Bar]:
    bars = parse_bars(rows)
    if not bars:
        return []
    if date is None:
        trading_dates = [b.timestamp.date() for b in bars if OPEN <= b.timestamp.time() < CLOSE]
        if not trading_dates:
            return []
        selected = max(trading_dates)
    else:
        try:
            selected = datetime.strptime(str(date), "%Y-%m-%d").date()
        except ValueError as exc:
            raise DataError("--date must be YYYY-MM-DD") from exc
    return [b for b in bars if b.timestamp.date() == selected and OPEN <= b.timestamp.time() < CLOSE]


def _at(value: str | datetime | None) -> datetime:
    return datetime.now(IST) if value is None else as_datetime(value)


def _consecutive(bars: list[Bar]) -> bool:
    return all(b.timestamp - a.timestamp == timedelta(minutes=BAR_MINUTES)
               for a, b in zip(bars, bars[1:]))


def orb_signal(rows: Iterable[dict[str, Any]], opening_minutes: int = 15,
               as_of: str | datetime | None = None, date: str | None = None) -> dict[str, Any]:
    """Completed 5-minute ORB; fail closed on timestamp gaps or bad bars.

    ``as_of`` is an explicit market-time cutoff, NOT the last bar in input. This
    preserves live safety and makes historic replay deterministic.
    """
    if opening_minutes not in (5, 10, 15, 20, 30, 45, 60):
        raise ValueError("Opening range must be 5,10,15,20,30,45 or 60 minutes")
    cutoff = _at(as_of)
    day = session_bars(rows, date)
    if not day:
        return {"status": "NO_DATA", "as_of": cutoff.isoformat()}
    sdate = day[0].timestamp.date()
    result: dict[str, Any] = {"status": "NO_DATA", "session_date": str(sdate),
                              "as_of": cutoff.isoformat(), "source": "supplied_bars"}
    if sdate > cutoff.date():
        return {**result, "status": "FUTURE_SESSION"}
    # Bars whose close time has not yet occurred must never influence the signal.
    completed = [b for b in day if b.timestamp + timedelta(minutes=BAR_MINUTES) <= cutoff]
    needed = opening_minutes // BAR_MINUTES
    expected_open = datetime.combine(sdate, OPEN, tzinfo=IST)
    if len(completed) < needed:
        return {**result, "status": "INCOMPLETE_OPEN", "detail": "Opening range bars not yet complete"}
    for i in range(needed):
        if completed[i].timestamp != expected_open + timedelta(minutes=i*BAR_MINUTES):
            return {**result, "status": "INCOMPLETE_OPEN", "detail": "Missing or misplaced opening bar"}
    first = completed[:needed]
    hi, lo = max(b.high for b in first), min(b.low for b in first)
    result.update({"range_high": hi, "range_low": lo, "opening_minutes": opening_minutes,
                   "last_complete_bar": completed[-1].timestamp.isoformat(),
                   "volume_missing_in_some_bars": any(b.volume is None for b in completed)})
    after = completed[needed:]
    if not after:
        return {**result, "status": "WAIT"}
    # Fail closed even if there was an apparent breakout before a gap.
    if not _consecutive(completed):
        return {**result, "status": "DATA_GAP", "detail": "Missing 5-minute bar(s)"}
    if any(b.volume == 0 for b in completed):
        return {**result, "status": "ZERO_VOLUME_BAR", "detail": "Zero-volume bars require no-trade review"}
    # A feed can silently stop updating without missing any timestamp *inside*
    # the received series. While a session is active, block stale signals.
    if (sdate == cutoff.date() and OPEN < cutoff.time() < CLOSE and
        (cutoff - (completed[-1].timestamp + timedelta(minutes=5))) > timedelta(minutes=10)):
        return {**result, "status": "STALE_DATA", "detail": "Latest completed candle is >10 minutes old"}
    for b in after:
        if b.close > hi:
            return {**result, "status": "TRIGGERED_LONG", "entry_reference": b.close,
                    "trigger_bar_start": b.timestamp.isoformat(),
                    "trigger_bar_end": (b.timestamp + timedelta(minutes=5)).isoformat(),
                    "note": "Historical completed-bar signal, not an executable fill"}
        if b.close < lo:
            return {**result, "status": "DOWNSIDE_BREAK_NO_LONG",
                    "trigger_bar_start": b.timestamp.isoformat(),
                    "detail": "Downside first; long-only strategy stands aside"}
    return {**result, "status": "WAIT", "last_close": after[-1].close}


def decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"Invalid {label}")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid {label}") from exc
    if not d.is_finite():
        raise ValueError(f"Invalid nonfinite {label}")
    return d


def _tick(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (value/tick).to_integral_value(rounding=rounding)*tick


def plan(entry: Any, stop: Any, capital: Any = 100000, max_risk_pct: Any = 0.5,
         target_r: Any = 2, max_alloc_pct: Any = 20, tick_size: Any = None,
         slippage_bps: Any = 0, fee_buffer: Any = 0) -> dict[str, Any]:
    """Conservative position sizing with user-supplied charges and slippage.

    Defaults are NOT a real brokerage charge calculation. ``fee_buffer`` is a
    fixed INR estimate covering both sides; use broker quote before action.
    """
    if tick_size is None:
        raise ValueError("--tick is mandatory: confirm the instrument-specific tick size")
    e, s, cap, risk, target, alloc, tick, slip, fees = [
        decimal(v, k) for k, v in zip(
            ("entry", "stop", "capital", "max_risk_pct", "target_r", "max_alloc_pct", "tick_size", "slippage_bps", "fee_buffer"),
            (entry, stop, capital, max_risk_pct, target_r, max_alloc_pct, tick_size, slippage_bps, fee_buffer))]
    if not (e > s > 0 and cap > 0 and e <= 1_000_000_000 and cap <= 1_000_000_000_000 and
            0 < risk <= 2 and target > 0 and
            0 < alloc <= 100 and Decimal("0.0001") <= tick <= e and
            s >= Decimal("0.0001") and 0 <= slip <= 500 and 0 <= fees <= cap):
        raise ValueError("Long-only entry>stop>0; validate capital, risk, allocation, tick, slippage and fees")
    # Worst direction rounding for a BUY entry and STOP SELL.
    e_tick = _tick(e, tick, ROUND_CEILING)
    s_tick = _tick(s, tick, ROUND_FLOOR)
    basis = Decimal("1") + slip/Decimal("10000")
    opposing = Decimal("1") - slip/Decimal("10000")
    effective_buy = e_tick*basis
    effective_stop = s_tick*opposing
    per_share = effective_buy - effective_stop
    allowed_loss = cap*risk/100
    available_risk = max(Decimal("0"), allowed_loss-fees)
    risk_qty = int((available_risk/per_share).to_integral_value(rounding=ROUND_FLOOR))
    affordable = max(Decimal("0"), cap*alloc/100-fees)
    cap_qty = int((affordable/effective_buy).to_integral_value(rounding=ROUND_FLOOR))
    qty = max(0, min(risk_qty, cap_qty))
    target_price = _tick(e_tick + target*(e_tick-s_tick), tick, ROUND_CEILING)
    money = lambda x: float(x.quantize(Decimal("0.01")))
    return {"mode": "PAPER_ONLY", "status": "PASS_PAPER" if qty else "BLOCK_ZERO_QUANTITY",
            "side": "LONG_CASH_ONLY", "quantity": qty, "entry": float(e_tick), "stop": float(s_tick),
            "target": float(target_price), "tick_size_assumed": float(tick), "tick_size_source": "EXPLICIT_UNVERIFIED_USER_INPUT",
            "capital_inr": money(cap), "risk_budget_inr": money(allowed_loss),
            "notional_inr_before_slippage": money(qty*e_tick),
            "max_alloc_pct": float(alloc), "slippage_bps_per_side": float(slip),
            "fee_buffer_inr": money(fees),
            "planned_loss_inr_including_assumptions": money(qty*per_share + (fees if qty else 0)),
            "notes": ["Not a broker-ready ticket; not executable or investment advice.",
                      "Slippage is an assumption, not a guaranteed worst case; stops can gap.",
                      "Fee buffer is user-entered, NOT an automatic Indian statutory/broker fee calculation.",
                      "Tick size varies by instrument and exchange; verify before relying on rounding."]}


def save_json(path: str | Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, allow_nan=False, ensure_ascii=False) + "\n", encoding="utf-8")
