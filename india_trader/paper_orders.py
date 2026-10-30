"""Paper budget, risk, and approval tickets. Contains no brokerage connector."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from decimal import ROUND_FLOOR

from .core import IST, as_datetime, decimal, symbol


def _fingerprint(order: dict) -> str:
    data = {key: value for key, value in order.items() if key != "fingerprint"}
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


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
    if not (0 < stop < entry < target and funds > 0 and loss_limit > 0 and
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
    state = state or {}
    reasons = []
    if order.get("status") != "PROPOSED" or order.get("mode") != "PAPER_ONLY":
        reasons.append("NOT_A_PAPER_PROPOSAL")
    if order.get("fingerprint") != _fingerprint(order):
        reasons.append("ORDER_CHANGED")
    if not order.get("ticker", "").endswith((".NS", ".BO")):
        reasons.append("INVALID_TICKER")
    if now.weekday() >= 5 or not time(9, 30) <= now.time() <= time(15, 10):
        reasons.append("OUTSIDE_NORMAL_ENTRY_WINDOW")
    if not order.get("trigger_at"):
        reasons.append("UNTRIGGERED_PLAN")
    else:
        try:
            trigger = as_datetime(order["trigger_at"])
            if trigger.date() != now.date() or (now - trigger).total_seconds() > 300 or now < trigger:
                reasons.append("STALE_OR_FUTURE_ENTRY")
        except (ValueError, TypeError, KeyError):
            reasons.append("INVALID_TRIGGER_TIME")
    if decimal(order.get("planned_loss_inr", 0), "planned loss") > decimal(order.get("max_loss_inr", 0), "max loss"):
        reasons.append("PER_TRADE_LOSS_LIMIT")
    daily_loss = decimal(state.get("daily_planned_loss_inr", 0), "daily planned loss")
    if daily_loss + decimal(order.get("planned_loss_inr", 0), "planned loss") > decimal(max_daily_loss, "daily loss limit"):
        reasons.append("DAILY_LOSS_LIMIT")
    if int(state.get("trades_today", 0)) >= max_trades_per_day:
        reasons.append("DAILY_TRADE_LIMIT")
    cash = decimal(state.get("cash_inr", order.get("capital_inr", 0)), "cash")
    if decimal(order.get("planned_notional_inr", 0), "notional") > cash:
        reasons.append("INSUFFICIENT_PAPER_CASH")
    if order.get("ticker") in state.get("traded_symbols_today", []):
        reasons.append("DUPLICATE_TICKER_TODAY")
    return {"mode": "PAPER_ONLY", "status": "PASS" if not reasons else "BLOCK",
            "ticker": order.get("ticker"), "order_fingerprint": order.get("fingerprint"),
            "checked_at": now.isoformat(), "reasons": reasons,
            "calendar_verified": False,
            "calendar_note": "NSE holiday and special-session calendar is not integrated",
            "orders_possible": False}


def paper_ticket(order: dict, review: dict, approval: bool = False,
                 now: datetime | None = None) -> dict:
    """Record a human decision for a reviewed paper proposal; never invent a fill."""
    if order.get("fingerprint") != _fingerprint(order):
        raise ValueError("Order changed after sizing")
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
