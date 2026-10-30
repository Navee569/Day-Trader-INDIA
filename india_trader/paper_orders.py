"""Paper budget, risk, and approval tickets. Contains no brokerage connector."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from decimal import Decimal, ROUND_FLOOR

from .core import IST, as_datetime, decimal, symbol
from .market_calendar import regular_nse_entry_day


def _fingerprint(order: dict) -> str:
    data = {key: value for key, value in order.items() if key != "fingerprint"}
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _order_terms(order: dict) -> list[str]:
    """Independently verify a proposal's arithmetic, not just its fingerprint."""
    if not isinstance(order, dict) or order.get("side") != "BUY_LONG_CASH" or order.get("orders_possible") is not False:
        return ["INVALID_ORDER_TERMS"]
    ticker = order.get("ticker")
    if not isinstance(ticker, str):
        return ["INVALID_ORDER_TERMS"]
    try:
        exchange = "BSE" if ticker.endswith(".BO") else "NSE"
        if symbol(ticker, exchange) != ticker:
            return ["INVALID_ORDER_TERMS"]
        qty = order["quantity"]
        if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 100_000_000:
            return ["INVALID_ORDER_TERMS"]
        entry = decimal(order["entry_reference"], "entry")
        stop = decimal(order["stop"], "stop")
        target = decimal(order["target"], "target")
        capital = decimal(order["capital_inr"], "capital")
        max_loss = decimal(order["max_loss_inr"], "max loss")
        position_pct = decimal(order["max_position_pct"], "position percent")
        buffer_pct = decimal(order["cash_buffer_pct"], "cash buffer percent")
        slip = decimal(order["slippage_bps_per_side"], "slippage")
        fees = decimal(order["fee_buffer_inr"], "fee buffer")
        notional = decimal(order["planned_notional_inr"], "notional")
        loss = decimal(order["planned_loss_inr"], "planned loss")
        flat = time.fromisoformat(order["flat_by_ist"])
        as_datetime(order["trigger_at"])
    except (ValueError, KeyError, TypeError, OverflowError):
        return ["INVALID_ORDER_TERMS"]
    if not (0 < stop < entry < target and entry <= 1_000_000_000 and
            0 < capital <= 1_000_000_000_000 and 0 < max_loss <= capital and
            0 < position_pct <= 100 and 0 <= buffer_pct < 100 and
            0 <= slip <= 1000 and 0 <= fees < max_loss and
            time(9, 45) <= flat <= time(15, 10)):
        return ["INVALID_ORDER_TERMS"]
    buy = entry * (1 + slip / 10000)
    stop_fill = stop * (1 - slip / 10000)
    expected_notional = buy * qty
    expected_loss = (buy - stop_fill) * qty + fees
    tolerance = Decimal("0.01")  # float serialization of the sized JSON
    reasons = []
    if (notional <= 0 or loss <= 0 or
            abs(notional - expected_notional) > tolerance or
            abs(loss - expected_loss) > tolerance):
        reasons.append("ORDER_MATH_MISMATCH")
    if (expected_notional > capital * position_pct / 100 or
            expected_notional > capital * (1 - buffer_pct / 100)):
        reasons.append("POSITION_CAP")
    if expected_loss > max_loss:
        reasons.append("PER_TRADE_LOSS_LIMIT")
    return reasons


def size_paper_order(exit_plan: dict, capital: str = "100000", max_loss: str = "500",
                     max_position_pct: str = "20", cash_buffer_pct: str = "10",
                     slippage_bps: str = "10", fee_buffer: str = "50") -> dict:
    if exit_plan.get("status") != "PLANNED" or exit_plan.get("mode") != "PAPER_PLAN_ONLY":
        raise ValueError("Budget requires a completed paper exit plan")
    ticker = exit_plan.get("ticker", "")
    exchange = "BSE" if ticker.endswith(".BO") else "NSE"
    if symbol(ticker, exchange) != ticker:
        raise ValueError("Paper order requires a verified NSE/BSE ticker")
    entry = decimal(exit_plan["entry_reference"], "entry")
    stop = decimal(exit_plan["stop"], "stop")
    target = decimal(exit_plan["target"], "target")
    funds = decimal(capital, "capital")
    loss_limit = decimal(max_loss, "max loss")
    position_pct = decimal(max_position_pct, "max position percent")
    buffer_pct = decimal(cash_buffer_pct, "cash buffer percent")
    slip = decimal(slippage_bps, "slippage basis points")
    fees = decimal(fee_buffer, "fee buffer")
    if not (0 < stop < entry < target and entry <= 1_000_000_000 and
            0 < funds <= 1_000_000_000_000 and 0 < loss_limit <= funds and
            0 < position_pct <= 100 and 0 <= buffer_pct < 100 and
            0 <= slip <= 1000 and 0 <= fees < loss_limit):
        raise ValueError("Invalid paper budget, price, slippage, or fee assumptions")
    buy_price = entry * (1 + slip / 10000)
    stop_fill = stop * (1 - slip / 10000)
    risk_per_share = buy_price - stop_fill
    risk_qty = ((loss_limit - fees) / risk_per_share).to_integral_value(rounding=ROUND_FLOOR)
    available = funds * (1 - buffer_pct / 100)
    position_cap = funds * position_pct / 100
    cash_qty = (available / buy_price).to_integral_value(rounding=ROUND_FLOOR)
    cap_qty = (position_cap / buy_price).to_integral_value(rounding=ROUND_FLOOR)
    qty = int(min(risk_qty, cash_qty, cap_qty))
    if qty > 100_000_000:
        raise ValueError("Paper quantity exceeds the supported whole-share limit")
    if qty < 1:
        return {"status": "BLOCK", "reason": "Budget permits zero whole shares",
                "mode": "PAPER_ONLY", "ticker": ticker, "orders_possible": False}
    order = {"mode": "PAPER_ONLY", "status": "PROPOSED", "ticker": ticker,
             "side": "BUY_LONG_CASH", "quantity": qty, "entry_reference": float(entry),
             "stop": float(stop), "target": float(target),
             "trigger_at": exit_plan["trigger_at"], "flat_by_ist": exit_plan["flat_by_ist"],
             "capital_inr": float(funds), "max_loss_inr": float(loss_limit),
             "max_position_pct": float(position_pct), "cash_buffer_pct": float(buffer_pct),
             "slippage_bps_per_side": float(slip), "fee_buffer_inr": float(fees),
             "planned_notional_inr": float(buy_price * qty),
             "planned_loss_inr": float(risk_per_share * qty + fees),
             "data_origin": exit_plan.get("data_origin"), "orders_possible": False}
    order["fingerprint"] = _fingerprint(order)
    return order


def risk_review(order: dict, as_of: str | None = None, state: dict | None = None,
                max_daily_loss: str = "1500", max_trades_per_day: int = 3) -> dict:
    """Check the exact paper order against cash, daily limits, and IST window."""
    now = datetime.now(IST) if as_of is None else as_datetime(as_of)
    state = {} if state is None else state
    reasons = []
    if not isinstance(state, dict):
        reasons.append("INVALID_RISK_STATE")
        state = {}
    try:
        max_day = decimal(max_daily_loss, "daily loss limit")
        daily_loss = decimal(state.get("daily_planned_loss_inr", 0), "daily planned loss")
        cash = decimal(state.get("cash_inr", order.get("capital_inr", 0)), "cash")
        traded = state.get("traded_symbols_today", [])
        count = state.get("trades_today", 0)
        if (max_day <= 0 or daily_loss < 0 or cash < 0 or
                isinstance(max_trades_per_day, bool) or not isinstance(max_trades_per_day, int) or
                not 1 <= max_trades_per_day <= 100 or isinstance(count, bool) or
                not isinstance(count, int) or count < 0 or not isinstance(traded, list) or
                any(not isinstance(s, str) for s in traded)):
            raise ValueError("Invalid risk state")
    except (ValueError, KeyError, TypeError, OverflowError):
        reasons.append("INVALID_RISK_STATE")
        max_day, daily_loss, cash, traded, count = Decimal(0), Decimal(0), Decimal(0), [], 0
        max_trades_per_day = 0
    if order.get("status") != "PROPOSED" or order.get("mode") != "PAPER_ONLY":
        reasons.append("NOT_A_PAPER_PROPOSAL")
    if order.get("fingerprint") != _fingerprint(order):
        reasons.append("ORDER_CHANGED")
    reasons.extend(_order_terms(order))
    if now.weekday() >= 5 or not time(9, 30) <= now.time() <= time(15, 10):
        reasons.append("OUTSIDE_NORMAL_ENTRY_WINDOW")
    if isinstance(order.get("ticker"), str) and order["ticker"].endswith(".NS") and not regular_nse_entry_day(now.date()):
        reasons.append("NSE_REGULAR_SESSION_UNVERIFIED_OR_CLOSED")
    if isinstance(order.get("ticker"), str) and order["ticker"].endswith(".BO") and not regular_nse_entry_day(now.date()):
        reasons.append("BSE_ENTRY_BLOCKED_BY_NSE_CLOSURE_OR_UNKNOWN_YEAR")
    try:
        trigger = as_datetime(order.get("trigger_at"))
        if trigger.date() != now.date() or (now - trigger).total_seconds() > 300 or now < trigger:
            reasons.append("STALE_OR_FUTURE_ENTRY")
    except (ValueError, TypeError):
        reasons.append("INVALID_ENTRY_TIME")
    try:
        planned_loss = decimal(order.get("planned_loss_inr"), "planned loss")
        notional = decimal(order.get("planned_notional_inr"), "notional")
    except (ValueError, TypeError):
        planned_loss, notional = Decimal(0), Decimal(0)
    if daily_loss + planned_loss > max_day:
        reasons.append("DAILY_LOSS_LIMIT")
    if count >= max_trades_per_day:
        reasons.append("DAILY_TRADE_LIMIT")
    if notional > cash:
        reasons.append("INSUFFICIENT_PAPER_CASH")
    if order.get("ticker") in traded:
        reasons.append("DUPLICATE_TICKER_TODAY")
    return {"mode": "PAPER_ONLY", "status": "PASS" if not reasons else "BLOCK",
            "ticker": order.get("ticker"), "order_fingerprint": order.get("fingerprint"),
            "checked_at": now.isoformat(), "reasons": reasons,
            "calendar_verified": False,
            "calendar_note": "NSE 2026 published EQ holidays blocked; special sessions, ad hoc closures, BSE and other years are not verified",
            "orders_possible": False}


def paper_ticket(order: dict, review: dict, approval: bool = False,
                 now: datetime | None = None) -> dict:
    """Record a human decision for a reviewed paper proposal; never invent a fill."""
    if order.get("fingerprint") != _fingerprint(order):
        raise ValueError("Order changed after sizing")
    if _order_terms(order):
        raise ValueError("Paper order terms or sizing arithmetic are invalid")
    if review.get("status") != "PASS" or review.get("order_fingerprint") != order["fingerprint"]:
        raise ValueError("Exact paper order has not passed risk review")
    stamp = now or datetime.now(IST)
    if approval:
        checked = as_datetime(review.get("checked_at"))
        if checked.date() != stamp.astimezone(IST).date() or not 0 <= (stamp - checked).total_seconds() <= 300:
            raise ValueError("Paper approval requires a fresh same-day risk review")
    return {"mode": "PAPER_ONLY", "status": "APPROVED_UNFILLED" if approval else "AWAITING_APPROVAL",
            "ticker": order["ticker"], "side": order["side"], "quantity": order["quantity"],
            "entry_reference": order["entry_reference"], "stop": order["stop"],
            "target": order["target"], "fingerprint": order["fingerprint"],
            "approval_recorded_at": stamp.astimezone(IST).isoformat() if approval else None,
            "fill": None, "orders_possible": False,
            "note": "Approval does not imply a simulated fill or send any broker order"}
