"""Exact-order paper tickets and a local simulated-fill ledger. No broker client."""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
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
    day = now.date().isoformat()
    current = state if state.get("date") == day else {"date": day, "orders": state.get("orders", [])}
    fills = [x for x in current.get("orders", []) if x.get("date") == day and
             x.get("status") == "simulated_fill"]
    entries = [x for x in fills if x.get("kind") == "entry"]
    entry_ids = {x.get("fingerprint") for x in entries}
    order_ids = {x.get("fingerprint") for x in fills}
    loss = sum((amount(x.get("max_loss_inr", 0), "logged fill risk", zero=True)
                for x in entries), Decimal(0))
    current["trades_today"] = max(int(current.get("trades_today", 0)), len(entry_ids))
    current["orders_today"] = max(int(current.get("orders_today", 0)), len(order_ids))
    current["daily_planned_loss_inr"] = float(max(
        amount(current.get("daily_planned_loss_inr", 0), "daily loss", zero=True), loss))
    symbols = current.get("traded_symbols_today", [])
    if not isinstance(symbols, list):
        raise BrokerError("traded symbols must be a list")
    current["traded_symbols_today"] = list(dict.fromkeys(symbols +
        [x["ticker"] for x in entries if isinstance(x.get("ticker"), str)]))
    return current


def _latest(state, fp, date):
    return next((x for x in reversed(state.get("orders", []))
                 if x.get("fingerprint") == fp and x.get("date") == date), None)


def _verdict(risk, order, now, fresh=True):
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
    if checked.astimezone(IST).date() != now.date() or age < 0 or (fresh and age > 300):
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
        previous = _latest(state, order["fingerprint"], now.date().isoformat())
        row = _verdict(risk, order, now, fresh=previous is None)
        if previous:
            filled = sum(int(x.get("quantity", 0)) for x in state.get("orders", [])
                         if x.get("fingerprint") == order["fingerprint"] and
                         x.get("date") == now.date().isoformat() and x.get("status") == "simulated_fill")
            result["status"] = ("PARTIAL_FILL" if previous["status"] == "simulated_fill" and
                                filled < int(order["quantity"]) else previous["status"].upper())
            result["reason"] = (f"{int(order['quantity']) - filled} paper shares remain" if
                                result["status"] == "PARTIAL_FILL" else
                                "already decided for this exact ticket today")
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
    if answer != "no":
        words = re.findall(r"[a-z]+", answer)
        if not words or words[0] != "yes" or any(w in words for w in
                ("all", "both", "every", "maybe", "if", "unless", "wait", "except", "not", "no")) or "?" in reply:
            raise BrokerError("reply must explicitly approve only this one ticket")
        named = {s.upper() for s in re.findall(r"[A-Za-z][A-Za-z0-9.]{1,25}", reply)
                 if s.upper().endswith((".NS", ".BO"))}
        if named and named != {order["ticker"]}:
            raise BrokerError("reply names a different ticker")
    event = {"at": now.isoformat(), "date": now.date().isoformat(), "index": index,
             "fingerprint": order["fingerprint"], "ticker": order["ticker"],
             "side": order["side"], "quantity": order["quantity"],
             "status": "approved" if answer != "no" else "declined", "reply": reply,
             "kind": "entry" if order["side"] == "BUY_LONG_CASH" else "exit",
             "max_loss_inr": shown.get("max_loss_inr", 0), "mode": "PAPER_ONLY"}
    state.setdefault("orders", []).append(event)
    return event


def record_result(order, state, now, status, note=None):
    """Record a refused/failed paper attempt after approval, without any fill."""
    if status not in ("denied", "failed"):
        raise BrokerError("paper result can only be denied or failed; use fill for a simulation")
    fp, date = order["fingerprint"], now.date().isoformat()
    events = [x for x in state.get("orders", []) if x.get("fingerprint") == fp and x.get("date") == date]
    filled = sum(int(x.get("quantity", 0)) for x in events if x.get("status") == "simulated_fill")
    if not events or events[-1].get("status") not in ("approved", "simulated_fill") or filled >= int(order["quantity"]):
        raise BrokerError("paper result needs an approved ticket with shares remaining today")
    event = {"at": now.isoformat(), "date": date, "fingerprint": fp,
             "ticker": order["ticker"], "side": order["side"], "quantity": order["quantity"],
             "status": status, "note": note or "No paper fill recorded", "mode": "PAPER_ONLY"}
    state["orders"].append(event)
    return event


def _holding(portfolio, sym):
    return next((p for p in portfolio["positions"] if p.get("ticker") == sym), None)


def simulate_fill(order, risk, state, portfolio, cfg, now, price, quantity=None):
    """Record an explicitly priced local fill after this ticket's exact approval."""
    _verdict(risk, order, now, fresh=False)
    fp, date = order["fingerprint"], now.date().isoformat()
    events = [x for x in state.get("orders", []) if x.get("fingerprint") == fp and x.get("date") == date]
    approved = next((x for x in events if x.get("status") == "approved"), None)
    if approved is None or (events and events[-1].get("status") in ("declined", "denied", "failed")):
        raise BrokerError("one exact ticket must be approved before simulating its fill")
    px = money(amount(price, "simulated fill price"))
    original_qty = int(order["quantity"])
    filled = sum(int(x.get("quantity", 0)) for x in events if x.get("status") == "simulated_fill")
    qty = original_qty - filled if quantity is None else int(quantity)
    if qty < 1 or filled + qty > original_qty:
        raise BrokerError("simulated fill quantity exceeds this ticket's remaining whole shares")
    first_fill = filled == 0
    sym = ticker(order["ticker"])
    cash = amount(portfolio["cash_inr"], "paper cash", zero=True)
    holding = _holding(portfolio, sym)
    cost = money(px * qty)
    entry = order["side"] == "BUY_LONG_CASH"
    if entry:
        if first_fill and (state.get("trades_today", 0) >= cfg["max_trades_per_day"] or state.get("orders_today", 0) >= cfg["max_orders_per_day"]):
            raise BrokerError("paper daily trade or order limit reached since risk review")
        if first_fill and sym in state.get("traded_symbols_today", []):
            raise BrokerError("paper ticker already entered today")
        if order.get("order_type") == "limit" and px > money(amount(order.get("limit_price"), "limit")):
            raise BrokerError("simulated buy exceeds the limit price")
        stop = money(amount(order.get("stop"), "stop"))
        target = money(amount(order.get("target"), "target"))
        if not stop < px < target:
            raise BrokerError("simulated entry is outside the stop/target bracket")
        slip = amount(order.get("slippage_bps_per_side", 0), "slippage", zero=True) / 10000
        fee = amount(order.get("fee_buffer_inr", 0), "fee buffer", zero=True) if first_fill else Decimal(0)
        actual_risk = money(qty * (px - money(stop * (1 - slip))) + fee)
        if (first_fill and money(px * original_qty) < cfg["min_order_inr"]) or cost > cfg["max_order_inr"]:
            raise BrokerError("simulated entry is outside the order-size limits")
        prior_risk = sum((amount(x.get("max_loss_inr", 0), "prior fill risk", zero=True)
                          for x in events if x.get("status") == "simulated_fill"), Decimal(0))
        if prior_risk + actual_risk > min(cfg["max_loss_per_trade_inr"], amount(order["max_loss_inr"], "order max loss")):
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
        if first_fill:
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
    if first_fill:
        state["orders_today"] = int(state.get("orders_today", 0)) + 1
    event = {"at": now.isoformat(), "date": date, "fingerprint": fp, "ticker": sym,
             "side": order["side"], "quantity": qty, "price_inr": float(px),
             "notional_inr": float(cost), "status": "simulated_fill", "mode": "PAPER_ONLY",
             "kind": "entry" if entry else "exit", "max_loss_inr": float(actual_risk) if entry else 0,
             "remaining_quantity": original_qty - filled - qty,
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
    parser.add_argument("--today", help="YYYY-MM-DD replay day; must match the IST clock")
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="show every paper ticket with READY/BLOCKED status")
    show.add_argument("--index", type=int)
    show.add_argument("--json", action="store_true", help="print structured tickets")
    approve = sub.add_parser("approve", help="record the user's exact yes or no for one paper ticket")
    approve.add_argument("--index", type=int, required=True)
    approve.add_argument("--reply", required=True)
    decline = sub.add_parser("decline", help="record a no for one paper ticket")
    decline.add_argument("--index", type=int, required=True)
    decline.add_argument("--reply", default="no")
    result = sub.add_parser("result", help="record a denied or failed paper attempt")
    result.add_argument("--index", type=int, required=True)
    result.add_argument("--status", choices=("denied", "failed"), required=True)
    result.add_argument("--note")
    fill = sub.add_parser("fill", help="simulate an exact approved fill at a supplied INR price")
    fill.add_argument("--index", type=int, required=True)
    fill.add_argument("--price", required=True)
    fill.add_argument("--quantity", "--qty", type=int, help="whole shares; default all remaining")
    fill.add_argument("--confirm", required=True, choices=["PAPER"])
    flat = sub.add_parser("flatten", help="write a position-reducing paper exit ticket")
    flat.add_argument("--ticker", "--symbol", required=True)
    flat.add_argument("--quantity", "--qty", type=int)
    flat.add_argument("--out", "--orders", default="trader/flatten_order.json")
    log = sub.add_parser("log", help="show the paper decision and fill ledger")
    log.add_argument("--all", action="store_true", help="include earlier days")
    sub.add_parser("portfolio", help="show paper cash and positions, including flat-by times")
    a = parser.parse_args(argv)
    try:
        now = clock(a.now)
        if a.today and a.today != now.date().isoformat():
            raise BrokerError("--today must match the IST replay/current date")
        cfg = limits(config(a.config))
        state = _today_state(_state(a.state), now)
        if a.command in ("portfolio", "flatten", "fill"):
            portfolio = _portfolio(a.portfolio, cfg)
        if a.command in ("show", "approve", "decline", "result", "fill"):
            orders = _orders(a.orders)
            risk = read_object(a.risk, "paper risk", optional=True)
        if a.command == "show":
            selected = [(a.index, orders[a.index - 1])] if a.index and 1 <= a.index <= len(orders) else list(enumerate(orders, 1)) if a.index is None else []
            if not selected:
                raise BrokerError("ticket index is outside the order list")
            result = [ticket(order, risk, state, now, index) for index, order in selected]
            if not a.json:
                for item in result:
                    print(f"PAPER TICKET #{item['index']}: {item['side']} {item['quantity']} {item['ticker']}")
                    print(f"  Type: {item['type']}; reference INR {item['entry_reference_inr']}; "
                          f"stop INR {item['stop_inr']}; target INR {item['target_inr']}")
                    print(f"  Flat by: {item['flat_by_ist']} IST; max planned loss INR {item.get('max_loss_inr')}")
                    print(f"  {item['status']}: {item['reason'] or 'exact risk PASS; one explicit yes required'}")
                print("Paper simulation only; no broker or exchange order is sent.")
                return 0 if any(x["ready"] for x in result) else 1
            if a.index is not None:
                result = result[0]
        elif a.command in ("approve", "decline"):
            if not 1 <= a.index <= len(orders):
                raise BrokerError("ticket index is outside the order list")
            result = decide(orders[a.index - 1], risk, state, now, a.index,
                            a.reply if a.command == "approve" else "no")
            if a.command == "decline":
                result["reply"] = a.reply
            _write(a.state, state)
        elif a.command == "result":
            if not 1 <= a.index <= len(orders):
                raise BrokerError("ticket index is outside the order list")
            _verdict(risk, orders[a.index - 1], now, fresh=False)
            result = record_result(orders[a.index - 1], state, now, a.status, a.note)
            _write(a.state, state)
        elif a.command == "fill":
            if not 1 <= a.index <= len(orders):
                raise BrokerError("ticket index is outside the order list")
            result = simulate_fill(orders[a.index - 1], risk, state, portfolio, cfg, now, a.price,
                                   a.quantity)
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
                      "orders": state["orders"] if a.all else [x for x in state["orders"]
                          if x.get("date") == now.date().isoformat()], "orders_possible": False}
        else:
            result = portfolio
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (BrokerError, BudgetError, RiskError, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        print("broker: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
