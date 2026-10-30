"""Transparent 5-minute ORB historical replay, NOT a probability forecast."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, time, timedelta
from statistics import mean
from .core import Bar, IST, DataError, OPEN, CLOSE, parse_bars, _consecutive
from .market_calendar import NSE_2026_CLOSED


def backtest_orb(rows, opening_minutes=15, reward_r=2.0, max_entry="11:30",
                 require_full_session=True):
    """Long-only, next-bar-open synthetic fills; stop-first on same-bar ambiguity.

    Default excludes incomplete days: exactly 72 5-minute continuous bars through 15:10.
    A candle cannot be used to enter at its own closing price; next bar open
    is used, with zero hypothetical costs/slippage. Results are descriptive.
    """
    if opening_minutes not in (5, 10, 15, 20, 30, 45, 60):
        raise ValueError("Invalid opening_minutes")
    try:
        hour, minute = map(int, max_entry.split(":"))
        entry_limit = time(hour, minute)
    except (ValueError, AttributeError) as exc:
        raise ValueError("max_entry must be HH:MM") from exc
    if not (time(9, 20) <= entry_limit <= time(15, 0)):
        raise ValueError("max_entry outside supported intraday window")
    if not (not isinstance(reward_r, bool) and isinstance(reward_r, (int, float)) and 0 < reward_r <= 20):
        raise ValueError("reward_r must be positive and <=20")
    data = parse_bars(rows)
    sessions: dict[str, list[Bar]] = defaultdict(list)
    for b in data:
        if OPEN <= b.timestamp.time() < CLOSE:
            sessions[str(b.timestamp.date())].append(b)
    all_rows = []
    for day, bars in sorted(sessions.items()):
        expected = datetime.combine(bars[0].timestamp.date(), OPEN, tzinfo=IST)
        entry = {"date": day, "status": "SKIP", "reason": ""}
        if bars[0].timestamp.date() in NSE_2026_CLOSED:
            entry["reason"] = "NSE_2026_PUBLISHED_HOLIDAY"
            all_rows.append(entry)
            continue
        if (bars[0].timestamp != expected or not _consecutive(bars) or
            (require_full_session and (len(bars) != 72 or bars[-1].timestamp.time() != time(15,10)))):
            entry["reason"] = "INCOMPLETE_OR_GAPPED_SESSION"
            all_rows.append(entry)
            continue
        if any(b.volume == 0 for b in bars):
            entry["reason"] = "ZERO_VOLUME_BAR"
            all_rows.append(entry)
            continue
        n = opening_minutes // 5
        if len(bars) < n+2:
            entry["reason"] = "INSUFFICIENT_BARS"
            all_rows.append(entry)
            continue
        hi = max(b.high for b in bars[:n])
        lo = min(b.low for b in bars[:n])
        hit = None
        for i in range(n, len(bars)-1):
            b = bars[i]
            # Entry occurs at NEXT bar open, not on the trigger bar's start.
            # Check the actual hypothesized entry timestamp against max_entry.
            if bars[i+1].timestamp.time() > entry_limit:
                break
            if b.close < lo:
                entry["reason"] = "DOWNSIDE_FIRST"
                break
            if b.close > hi:
                hit = i
                break
        if hit is None:
            if not entry["reason"]:
                entry["reason"] = "NO_LONG_TRIGGER"
            all_rows.append(entry)
            continue
        # At 5m close of signal, next candle's OPEN is first hypothesized fill.
        trigger = bars[hit]
        following = bars[hit+1:]
        if not following or following[0].timestamp != trigger.timestamp+timedelta(minutes=5):
            entry["reason"] = "MISSING_NEXT_BAR"
            all_rows.append(entry)
            continue
        buy = following[0].open
        if buy <= lo:
            entry["reason"] = "GAP_BELOW_STOP"
            all_rows.append(entry)
            continue
        risk = buy - lo
        target = buy + float(reward_r)*risk
        exit_at = None
        outcome = None
        exit_ts = None
        for b in following:
            # OHLC cannot reveal high/low ordering: choose stop FIRST.
            if b.open <= lo:
                exit_at, outcome = b.open, "STOP_GAP"
            elif b.low <= lo:
                exit_at, outcome = lo, "STOP"
            elif b.high >= target:
                exit_at, outcome = target, "TARGET"
            if exit_at is not None:
                exit_ts = b.timestamp.isoformat()
                break
        if exit_at is None:
            exit_at, outcome = bars[-1].close, "SESSION_CLOSE"
            exit_ts = bars[-1].timestamp.isoformat()
        pnl = exit_at - buy
        all_rows.append({"date":day, "status":"SIMULATED", "reason":outcome,
                         "signal_bar_start":trigger.timestamp.isoformat(),
                         "entry_time":following[0].timestamp.isoformat(),
                         "entry":round(buy,4), "stop":round(lo,4),
                         "target":round(target,4), "exit_time":exit_ts,
                         "exit":round(exit_at,4), "gross_pnl_per_share_inr":round(pnl,4),
                         "gross_r_multiple":round(pnl/risk,5)})
    wins = [e for e in all_rows if e["status"] == "SIMULATED" and e["gross_pnl_per_share_inr"] > 0]
    trades = [e for e in all_rows if e["status"] == "SIMULATED"]
    return {"mode":"HISTORICAL_HYPOTHETICAL_PAPER_ONLY",
            "method":"5m completed-bar ORB; enter NEXT bar open; stop-first on intra-bar ambiguity",
            "data_sessions":len(sessions), "simulated_trades":len(trades),
            "skipped_sessions":len(all_rows)-len(trades),
            "insufficient_sample":len(trades) < 30,
            "win_rate_pct":round(100*len(wins)/len(trades),2) if trades else None,
            "mean_gross_r":round(mean(e["gross_r_multiple"] for e in trades),4) if trades else None,
            "zero_fees_slippage_assumed":True,
            "warnings":["Fewer than 30 simulations: descriptive results are highly unstable." if len(trades)<30 else "These historical results do not establish a reliable live-trading edge.",
                        "Historical bars cannot prove a live fill or performance.",
                        "No corporate-action or survivorship-bias adjustment.",
                        "No commissions, STT, spread, taxes or slippage; these reduce returns.",
                        "Calendar, instrument eligibility and liquidity not validated."],
            "sessions":all_rows}
