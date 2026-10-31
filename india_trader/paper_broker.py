"""Exact-order paper tickets and a local simulated-fill ledger. No broker client."""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys

from .budget_manager import BudgetError, amount, clock, config, money, read_object, ticker
from .paper_orders import _fingerprint
from .risk_manager import RiskError, limits
from .core import IST


class BrokerError(Exception):
    pass


def _write(path, doc):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _orders(path):
    doc = read_object(path, "paper orders")
    rows = doc.get("orders", [doc])
    if not isinstance(rows, list) or not rows or any(not isinstance(x, dict) for x in rows):
        raise BrokerError("orders must contain at least one object")
    return rows


def _state(path):
    doc = read_object(path, "paper broker state", optional=True) or {}
    if not isinstance(doc.get("orders", []), list):
        raise BrokerError("broker order log must be a list")
    doc.setdefault("orders", [])
    return doc


def _portfolio(path, cfg):
    doc = read_object(path, "paper portfolio", optional=True)
    if doc is None:
        if cfg.get("budget_inr") is None:
            raise BrokerError("set budget_inr in config before opening the paper portfolio")
        doc = {"mode": "PAPER_ONLY", "cash_inr": float(cfg["budget_inr"]),
               "positions": [], "realized_pnl_inr": 0.0}
    if doc.get("mode", "PAPER_ONLY") != "PAPER_ONLY":
        raise BrokerError("portfolio is not paper-only")
    if not isinstance(doc.get("positions", []), list):
        raise BrokerError("portfolio positions must be a list")
    return doc


def _today_state(state, now):
    if state.get("date") != now.date().isoformat():
        return {"date": now.date().isoformat(), "orders": state.get("orders", []),
                "daily_planned_loss_inr": 0, "trades_today": 0,
                "orders_today": 0, "traded_symbols_today": []}
    return state


def _latest(state, fp, date):
    return next((x for x in reversed(state.get("orders", []))
                 if x.get("fingerprint") == fp and x.get("date") == date), None)


def _verdict(risk, order, now):
    fp = _fingerprint(order)
    if order.get("fingerprint") != fp:
        raise BrokerError("order changed after sizing; run risk-manager again")
    if risk is None or risk.get("mode") != "PAPER_ONLY" or risk.get("orders_possible") is not False:
        raise BrokerError("paper risk verdict is missing or invalid")
    try:
        checked = datetime.fromisoformat(str(risk["checked_at"]).replace("Z", "+00:00"))
        if checked.tzinfo is None:
            raise ValueError("timezone missing")
        age = (now - checked).total_seconds()
    except (KeyError, ValueError, TypeError) as exc:
        raise BrokerError("risk verdict has no valid timestamp") from exc
    if checked.astimezone(IST).date() != now.date() or not 0 <= age <= 300:
        raise BrokerError("risk verdict is not a fresh same-day check")
    rows = risk.get("verdicts")
    if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
        raise BrokerError("paper risk verdict list is invalid")
    matches = [x for x in rows if x.get("order_fingerprint") == fp]
    if len(matches) != 1 or matches[0].get("status") != "PASS":
        raise BrokerError("this exact order has no unique PASS verdict")
    return matches[0]


def ticket(order, risk, state, now, index):
    side = order.get("side")
    label = "BUY" if side == "BUY_LONG_CASH" else "SELL" if side == "SELL_LONG_CASH" else "?"
    result = {"index": index, "mode": "PAPER_ONLY", "ticker": order.get("ticker"),
              "side": label, "quantity": order.get("quantity"), "type": order.get("order_type"),
              "entry_reference_inr": order.get("entry_reference"), "limit_price_inr": order.get("limit_price"),
              "stop_inr": order.get("stop"), "target_inr": order.get("target"),
              "flat_by_ist": order.get("flat_by_ist"), "fingerprint": order.get("fingerprint"),
              "ready": False, "status": "BLOCKED", "reason": None, "orders_possible": False}
    try:
        row = _verdict(risk, order, now)
        previous = _latest(state, order["fingerprint"], now.date().isoformat())
        if previous:
            result["status"] = previous["status"].upper()
            result["reason"] = "already decided for this exact ticket today"
        else:
            result.update(ready=True, status="READY", max_loss_inr=row.get("max_loss_inr"))
    except (BrokerError, KeyError) as exc:
        result["reason"] = str(exc)
    return result


def decide(order, risk, state, now, index, reply):
    shown = ticket(order, risk, state, now, index)
    if not shown["ready"]:
        raise BrokerError(f"ticket {index} is not ready: {shown['reason']}")
    answer = reply.strip().lower()
    if answer not in ("yes", "no"):
        raise BrokerError("reply must be the user's exact yes or no for this one ticket")
    event = {"at": now.isoformat(), "date": now.date().isoformat(), "index": index,
             "fingerprint": order["fingerprint"], "ticker": order["ticker"],
             "side": order["side"], "quantity": order["quantity"],
             "status": "approved" if answer == "yes" else "declined", "reply": reply,
             "kind": "entry" if order["side"] == "BUY_LONG_CASH" else "exit",
             "max_loss_inr": shown.get("max_loss_inr", 0), "mode": "PAPER_ONLY"}
    state.setdefault("orders", []).append(event)
    return event


def _holding(portfolio, sym):
    return next((p for p in portfolio["positions"] if p.get("ticker") == sym), None)


def simulate_fill(order, risk, state, portfolio, cfg, now, price):
    """Record an explicitly priced local fill after this ticket's exact approval."""
    _verdict(risk, order, now)
    fp, date = order["fingerprint"], now.date().isoformat()
    approved = _latest(state, fp, date)
    if approved is None or approved.get("status") != "approved":
        raise BrokerError("one exact ticket must be approved before simulating its fill")
    px = money(amount(price, "simulated fill price"))
    qty = int(order["quantity"])
    sym = ticker(order["ticker"])
    cash = amount(portfolio["cash_inr"], "paper cash", zero=True)
    holding = _holding(portfolio, sym)
    cost = money(px * qty)
    entry = order["side"] == "BUY_LONG_CASH"
    if entry:
        if state.get("trades_today", 0) >= cfg["max_trades_per_day"] or state.get("orders_today", 0) >= cfg["max_orders_per_day"]:
            raise BrokerError("paper daily trade or order limit reached since risk review")
        if sym in state.get("traded_symbols_today", []):
            raise BrokerError("paper ticker already entered today")
        if order.get("order_type") == "limit" and px > money(amount(order.get("limit_price"), "limit")):
            raise BrokerError("simulated buy exceeds the limit price")
        stop = money(amount(order.get("stop"), "stop"))
        target = money(amount(order.get("target"), "target"))
        if not stop < px < target:
            raise BrokerError("simulated entry is outside the stop/target bracket")
        slip = amount(order.get("slippage_bps_per_side", 0), "slippage", zero=True) / 10000
        fee = amount(order.get("fee_buffer_inr", 0), "fee buffer", zero=True)
        actual_risk = money(qty * (px - money(stop * (1 - slip))) + fee)
        if cost < cfg["min_order_inr"] or cost > cfg["max_order_inr"]:
            raise BrokerError("simulated entry is outside the order-size limits")
        if actual_risk > min(cfg["max_loss_per_trade_inr"], amount(order["max_loss_inr"], "order max loss")):
            raise BrokerError("simulated entry exceeds the reviewed per-trade risk")
        if money(amount(state.get("daily_planned_loss_inr", 0), "daily loss", zero=True) + actual_risk) > cfg["max_daily_loss_inr"]:
            raise BrokerError("simulated entry exceeds the daily risk limit")
        equity = cash + sum(amount(p["average_cost_inr"], "average cost") * int(p["quantity"])
                            for p in portfolio["positions"])
        if cost > cash - money(equity * cfg["cash_buffer_pct"] / 100):
            raise BrokerError("simulated entry exceeds available paper cash buffer")
        existing_qty = int(holding["quantity"]) if holding else 0
        if cost + existing_qty * px > money(equity * cfg["max_position_pct"] / 100):
            raise BrokerError("simulated entry exceeds position cap")
        if holding:
            old = amount(holding["average_cost_inr"], "average cost") * existing_qty
            holding["average_cost_inr"] = float(money((old + cost) / (existing_qty + qty)))
            holding["quantity"] += qty
        else:
            portfolio["positions"].append({"ticker": sym, "quantity": qty,
                "average_cost_inr": float(px), "stop_inr": float(stop),
                "target_inr": float(target), "flat_by_ist": order.get("flat_by_ist")})
        portfolio["cash_inr"] = float(money(cash - cost))
        state["daily_planned_loss_inr"] = float(money(amount(state.get("daily_planned_loss_inr", 0), "daily loss", zero=True) + actual_risk))
        state["trades_today"] = int(state.get("trades_today", 0)) + 1
        state.setdefault("traded_symbols_today", [])
        if sym not in state["traded_symbols_today"]:
            state["traded_symbols_today"].append(sym)
    else:
        if not holding or int(holding["quantity"]) < qty:
            raise BrokerError("simulated exit exceeds paper holding")
        basis = amount(holding["average_cost_inr"], "average cost")
        portfolio["cash_inr"] = float(money(cash + cost))
        try:
            realized = Decimal(str(portfolio.get("realized_pnl_inr", 0)))
            if not realized.is_finite():
                raise ValueError("nonfinite realized P&L")
        except (ValueError, TypeError) as exc:
            raise BrokerError("portfolio has invalid realized P&L") from exc
        portfolio["realized_pnl_inr"] = float(money(realized + (px - basis) * qty))
        holding["quantity"] -= qty
        if holding["quantity"] == 0:
            portfolio["positions"].remove(holding)
    state["orders_today"] = int(state.get("orders_today", 0)) + 1
    event = {"at": now.isoformat(), "date": date, "fingerprint": fp, "ticker": sym,
             "side": order["side"], "quantity": qty, "price_inr": float(px),
             "notional_inr": float(cost), "status": "simulated_fill", "mode": "PAPER_ONLY",
             "kind": "entry" if entry else "exit", "max_loss_inr": float(actual_risk) if entry else 0,
             "note": "Local simulation; no exchange or broker execution"}
    state["orders"].append(event)
    portfolio["equity_inr"] = float(money(Decimal(str(portfolio["cash_inr"])) +
        sum(Decimal(str(p["average_cost_inr"])) * int(p["quantity"]) for p in portfolio["positions"])))
    return event


def flatten_ticket(sym, qty, portfolio, now):
    holding = _holding(portfolio, ticker(sym))
    if not holding:
        raise BrokerError("no paper holding to flatten")
    available = int(holding["quantity"])
    count = available if qty is None else int(qty)
    if count < 1 or count > available:
        raise BrokerError("flatten quantity must be within the paper holding")
    order = {"mode": "PAPER_EXIT_ONLY", "status": "PROPOSED_EXIT", "orders_possible": False,
             "ticker": holding["ticker"], "side": "SELL_LONG_CASH", "quantity": count,
             "order_type": "market_reference", "flat_by_ist": holding.get("flat_by_ist", "15:10"),
             "note": "Position-reducing paper ticket; no order is sent"}
    order["fingerprint"] = _fingerprint(order)
    return order


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m india_trader broker",
                                     description="Paper ticket, simulated fill and portfolio ledger; no broker connection")
    parser.add_argument("--orders", default="trader/orders.json")
    parser.add_argument("--risk", default="trader/state/risk.json")
    parser.add_argument("--state", default="trader/state/broker.json")
    parser.add_argument("--portfolio", default="trader/portfolio.json")
    parser.add_argument("--config", default="trader/config.json")
    parser.add_argument("--now", help="Offset-aware replay clock; omitted uses current IST")
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="show every paper ticket with READY/BLOCKED status")
    show.add_argument("--index", type=int)
    approve = sub.add_parser("approve", help="record the user's exact yes or no for one paper ticket")
    approve.add_argument("--index", type=int, required=True)
    approve.add_argument("--reply", required=True)
    fill = sub.add_parser("fill", help="simulate an exact approved fill at a supplied INR price")
    fill.add_argument("--index", type=int, required=True)
    fill.add_argument("--price", required=True)
    fill.add_argument("--confirm", required=True, choices=["PAPER"])
    flat = sub.add_parser("flatten", help="write a position-reducing paper exit ticket")
    flat.add_argument("--ticker", required=True)
    flat.add_argument("--quantity", type=int)
    flat.add_argument("--out", default="trader/flatten_order.json")
    sub.add_parser("log", help="show the paper decision and fill ledger")
    sub.add_parser("portfolio", help="show paper cash and positions, including flat-by times")
    a = parser.parse_args(argv)
    try:
        now = clock(a.now)
        cfg = limits(config(a.config))
        state = _today_state(_state(a.state), now)
        if a.command in ("portfolio", "flatten", "fill"):
            portfolio = _portfolio(a.portfolio, cfg)
        if a.command in ("show", "approve", "fill"):
            orders = _orders(a.orders)
            risk = read_object(a.risk, "paper risk", optional=True)
        if a.command == "show":
            selected = [(a.index, orders[a.index - 1])] if a.index and 1 <= a.index <= len(orders) else list(enumerate(orders, 1)) if a.index is None else []
            if not selected:
                raise BrokerError("ticket index is outside the order list")
            result = [ticket(order, risk, state, now, index) for index, order in selected]
        elif a.command == "approve":
            if not 1 <= a.index <= len(orders):
                raise BrokerError("ticket index is outside the order list")
            result = decide(orders[a.index - 1], risk, state, now, a.index, a.reply)
            _write(a.state, state)
        elif a.command == "fill":
            if not 1 <= a.index <= len(orders):
                raise BrokerError("ticket index is outside the order list")
            result = simulate_fill(orders[a.index - 1], risk, state, portfolio, cfg, now, a.price)
            _write(a.portfolio, portfolio)
            _write(a.state, state)
        elif a.command == "flatten":
            order = flatten_ticket(a.ticker, a.quantity, portfolio, now)
            doc = {"mode": "PAPER_ONLY", "created_at": now.isoformat(), "orders": [order],
                   "note": "Run risk-manager on this file, then broker show/approve/fill with --orders and --risk"}
            _write(a.out, doc)
            result = {"status": "PROPOSED_EXIT", "path": a.out, "order": order,
                      "next": f"python -m india_trader risk-manager --orders {a.out} --out trader/state/flatten_risk.json"}
        elif a.command == "log":
            result = {"mode": "PAPER_ONLY", "date": now.date().isoformat(),
                      "orders": state["orders"], "orders_possible": False}
        else:
            result = portfolio
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (BrokerError, BudgetError, RiskError, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        print("broker: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
