"""Reconciliation and daily performance views for the local paper ledger."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from .auto_paper_broker import dec, money
from .core import IST


class AccountingError(ValueError):
    pass


def _fill_notional(event):
    return money(event.get("notional_inr",
                           dec(event["fill_price_inr"]) * event["quantity"]))


def reconcile(state):
    """Fail closed when saved cash, holdings, orders or realized P&L disagree."""
    if state.get("mode") != "PAPER_ONLY" or state.get("orders_possible") is not False:
        raise AccountingError("ledger is not paper-only")
    orders = state.get("orders")
    events = state.get("events")
    positions = state.get("positions")
    trades = state.get("trades")
    if any(not isinstance(x, list) for x in (orders, events, positions, trades)):
        raise AccountingError("ledger collections invalid")
    by_id = {o.get("id"): o for o in orders if isinstance(o, dict)}
    if len(by_id) != len(orders) or None in by_id:
        raise AccountingError("duplicate or invalid order ID")
    cash = money(state["initial_capital_inr"])
    quantities = defaultdict(int)
    bought_notional = defaultdict(lambda: Decimal(0))
    bought_fees = defaultdict(lambda: Decimal(0))
    fill_counts = defaultdict(int)
    reserved = Decimal(0)
    for event in events:
        if not isinstance(event, dict) or event.get("order_id") not in by_id:
            raise AccountingError("event without matching order")
        if event.get("kind") != "SIMULATED_FILL":
            continue
        order = by_id[event["order_id"]]
        ticker = order["ticker"]
        if ticker != event.get("ticker") or type(event.get("quantity")) is not int or \
                event["quantity"] < 1:
            raise AccountingError("invalid fill quantity or ticker")
        fee, gross = money(event["fee_inr"]), _fill_notional(event)
        if fee < 0 or gross <= 0:
            raise AccountingError("invalid fill cash flow")
        if gross != money(dec(event["fill_price_inr"]) * event["quantity"]):
            raise AccountingError("fill notional differs from price and quantity")
        if "charges" in event:
            parts = event["charges"]
            if not isinstance(parts, dict) or \
                    any(dec(v) < 0 for v in parts.values()) or \
                    money(parts.get("total_inr", -1)) != fee or \
                    money(sum((dec(v) for k, v in parts.items() if k != "total_inr"),
                              Decimal(0))) != fee:
                raise AccountingError("modeled charge components do not reconcile")
        if "slippage_inr" in event and money(event["slippage_inr"]) != money(
                abs(dec(event["fill_price_inr"]) -
                    dec(event["reference_quote_inr"])) * event["quantity"]):
            raise AccountingError("slippage impact does not reconcile")
        fill_counts[order["id"]] += event["quantity"]
        if order["side"] == "BUY":
            cash -= gross + fee
            quantities[ticker] += event["quantity"]
            bought_notional[ticker] += gross
            bought_fees[ticker] += fee
        elif order["side"] == "SELL":
            cash += gross - fee
            quantities[ticker] -= event["quantity"]
            if quantities[ticker] < 0:
                raise AccountingError("paper short position detected")
        else:
            raise AccountingError("invalid paper fill side")
    for order in orders:
        qty = order.get("quantity")
        done = fill_counts[order["id"]]
        if type(qty) is not int or qty < 1 or done != order.get("filled_quantity") or \
                done > qty:
            raise AccountingError("order fill count mismatch")
        status = order.get("status")
        if (status == "PENDING" and done != 0 or
                status == "PARTIAL" and not 0 < done < qty or
                status == "FILLED" and done != qty or
                status == "REJECTED" and done != 0 or
                status == "CANCELLED" and done >= qty or
                status not in ("PENDING", "PARTIAL", "FILLED", "REJECTED", "CANCELLED")):
            raise AccountingError("order state mismatch")
        reserve = money(order.get("reserved_inr", 0))
        if reserve < 0 or status in ("FILLED", "REJECTED", "CANCELLED") and reserve != 0:
            raise AccountingError("invalid order reservation")
        if status in ("PENDING", "PARTIAL") and order["side"] == "BUY":
            reserved += reserve
    if money(cash) != money(state["cash_inr"]):
        raise AccountingError("paper cash does not reconcile with fill history")
    current = {p.get("ticker"): p for p in positions if isinstance(p, dict)}
    if len(current) != len(positions) or None in current:
        raise AccountingError("duplicate or invalid position")
    if {t: q for t, q in quantities.items() if q} != \
            {t: p.get("quantity") for t, p in current.items()}:
        raise AccountingError("holding quantities do not reconcile")
    used_basis = defaultdict(lambda: Decimal(0))
    used_fees = defaultdict(lambda: Decimal(0))
    realized = Decimal(0)
    for trade in trades:
        if not isinstance(trade, dict):
            raise AccountingError("invalid trade record")
        ticker = trade["ticker"]
        realized += dec(trade["net_pnl_inr"])
        used_basis[ticker] += dec(trade.get("entry_cost_basis_inr",
                                           dec(trade["entry_price_inr"]) *
                                           trade["quantity"]))
        used_fees[ticker] += dec(trade["entry_fee_inr"])
    if money(realized) != money(state["realized_pnl_inr"]):
        raise AccountingError("realized P&L does not reconcile")
    for ticker, position in current.items():
        basis = money(bought_notional[ticker] - used_basis[ticker])
        fees = money(bought_fees[ticker] - used_fees[ticker])
        if basis < 0 or fees < 0 or \
                abs(basis - money(position.get("cost_basis_inr",
                                             dec(position["average_cost_inr"]) *
                                             position["quantity"]))) > Decimal("0.01") or \
                abs(fees - money(position["entry_fees_inr"])) > Decimal("0.01"):
            raise AccountingError("position cost basis or fees do not reconcile")
    book_equity = money(cash + sum((
        dec(p.get("cost_basis_inr", dec(p["average_cost_inr"]) * p["quantity"])) +
        dec(p["entry_fees_inr"]) for p in positions), Decimal(0)))
    if book_equity != money(dec(state["initial_capital_inr"]) + realized):
        raise AccountingError("paper book equity does not reconcile")
    available = money(cash - reserved)
    if available < 0:
        raise AccountingError("paper reservations exceed cash")
    return {"status": "BALANCED", "cash_inr": float(money(cash)),
            "reserved_inr": float(money(reserved)),
            "available_cash_inr": float(available),
            "open_positions": len(current), "orders": len(orders),
            "filled_shares": sum(fill_counts.values()),
            "fill_events": sum(e.get("kind") == "SIMULATED_FILL" for e in events),
            "paper_only": True}


def daily_summaries(state):
    """Reconstruct each day's cash and activity; unknown EOD marks stay unknown."""
    cycle_at = (state.get("last_cycle") or {}).get("at")
    days = sorted({x["at"][:10] for x in state["events"]} |
                  {x["closed_at"][:10] for x in state["trades"]} |
                  ({cycle_at[:10]} if isinstance(cycle_at, str) else set()))
    cash = money(state["initial_capital_inr"])
    quantities = defaultdict(int)
    result = []
    for day in days:
        start_cash = cash
        rows = [x for x in state["events"] if x["at"][:10] == day]
        charges = defaultdict(lambda: Decimal(0))
        buys = sells = Decimal(0)
        slippage = Decimal(0)
        fills = 0
        submitted = cancelled = rejected = 0
        by_id = {o["id"]: o for o in state["orders"]}
        for event in rows:
            kind = event["kind"]
            submitted += kind == "SUBMITTED"
            cancelled += kind == "CANCELLED"
            rejected += kind == "REJECTED"
            if kind != "SIMULATED_FILL":
                continue
            fills += 1
            gross = _fill_notional(event)
            fee = money(event["fee_inr"])
            slippage += dec(event.get("slippage_inr", 0))
            for key, value in event.get("charges", {"other_inr": fee}).items():
                if key != "total_inr":
                    charges[key] += dec(value)
            side = by_id[event["order_id"]]["side"]
            if side == "BUY":
                buys += gross
                cash -= gross + fee
                quantities[event["ticker"]] += event["quantity"]
            else:
                sells += gross
                cash += gross - fee
                quantities[event["ticker"]] -= event["quantity"]
        trades = [t for t in state["trades"] if t["closed_at"][:10] == day]
        open_count = sum(q > 0 for q in quantities.values())
        result.append({"date": day, "starting_cash_inr": float(money(start_cash)),
                       "ending_cash_inr": float(money(cash)),
                       "buy_turnover_inr": float(money(buys)),
                       "sell_turnover_inr": float(money(sells)),
                       "modeled_charges_inr": {k: float(money(v)) for k, v in sorted(charges.items())},
                       "total_modeled_charges_inr": float(money(sum(charges.values(), Decimal(0)))),
                       "slippage_impact_inr": float(money(slippage)),
                       "realized_net_pnl_inr": float(money(sum(
                           (dec(t["net_pnl_inr"]) for t in trades), Decimal(0)))),
                       "closed_slices": len(trades), "simulated_fills": fills,
                       "submitted_orders": submitted, "cancelled_orders": cancelled,
                       "rejected_orders": rejected,
                       "open_positions_at_day_end": open_count,
                       "end_of_day_equity_inr": float(money(cash)) if open_count == 0 else None,
                       "equity_note": ("All paper positions closed; equity equals cash"
                                       if open_count == 0 else
                                       "No verified end-of-day price mark stored")})
    return result


def report(state, date=None, now=None, max_mark_age_seconds=30):
    balance = reconcile(state)
    summaries = daily_summaries(state)
    if date is not None:
        summaries = [row for row in summaries if row["date"] == date]
    latest = state.get("last_cycle") or {}
    clock = datetime.now(IST) if now is None else now.astimezone(IST)
    try:
        last_at = datetime.fromisoformat(latest["at"])
        mark_fresh = last_at.tzinfo is not None and \
            0 <= (clock - last_at.astimezone(IST)).total_seconds() <= max_mark_age_seconds and \
            latest.get("unrealized_pnl_inr") is not None
    except (KeyError, TypeError, ValueError):
        mark_fresh = False
    return {"mode": "PAPER_ONLY", "balance": balance,
            "initial_capital_inr": state["initial_capital_inr"],
            "realized_pnl_inr": state["realized_pnl_inr"],
            "unrealized_pnl_inr": (0.0 if not state["positions"] else
                                   latest.get("unrealized_pnl_inr") if mark_fresh else None),
            "unrealized_as_of": latest.get("at"),
            "last_observed_unrealized_pnl_inr": latest.get("unrealized_pnl_inr"),
            "valuation_status": ("NO_OPEN_POSITIONS" if not state["positions"] else
                                 "FRESH_BROKER_MARK" if mark_fresh else "STALE_OR_UNKNOWN"),
            "open_positions": state["positions"],
            "order_statuses": {status: sum(o["status"] == status for o in state["orders"])
                               for status in ("PENDING", "PARTIAL", "FILLED",
                                              "REJECTED", "CANCELLED")},
            "daily": summaries,
            "trade_history": [t for t in state["trades"]
                              if date is None or t["closed_at"][:10] == date],
            "order_events": [e for e in state["events"]
                             if date is None or e["at"][:10] == date],
            "orders_possible": False}
