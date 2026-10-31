"""Batch paper risk gate for Indian cash-equity proposals. No execution."""
from __future__ import annotations

import argparse
from datetime import datetime, time
from decimal import Decimal
import json
from pathlib import Path
import sys

from .budget_manager import BudgetError, _price_record, amount, clock, config, money, read_object, ticker
from .market_scanner import trading_day
from .paper_orders import _fingerprint
from .core import IST


class RiskError(Exception):
    pass


def _count(value, name):
    if isinstance(value, bool):
        raise RiskError(f"{name} must be a nonnegative whole number")
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise RiskError(f"{name} must be a nonnegative whole number") from exc
    if n < 0 or str(value).strip() not in (str(n), str(float(n))):
        raise RiskError(f"{name} must be a nonnegative whole number")
    return n


def _time(value, name):
    try:
        h, m = map(int, str(value).split(":"))
        return time(h, m)
    except (TypeError, ValueError) as exc:
        raise RiskError(f"{name} must be HH:MM IST") from exc


def limits(raw):
    c = config("/nonexistent")
    c.update(raw)
    if c.get("paper") is not True:
        raise RiskError("risk manager is paper-only")
    for key, default in (("max_daily_loss_inr", 1500), ("max_trades_per_day", 3),
                         ("max_orders_per_day", 10)):
        c[key] = _count(c.get(key, default), key) if key != "max_daily_loss_inr" else amount(c.get(key, default), key)
    for key in ("budget_inr", "max_loss_per_trade_inr", "max_position_pct", "max_order_inr",
                "min_order_inr", "cash_buffer_pct", "min_reward_risk"):
        if c.get(key) is not None:
            c[key] = amount(c[key], key, zero=key in ("min_order_inr", "cash_buffer_pct", "min_reward_risk"))
    c["no_entries_before"] = _time(c.get("no_entries_before", "09:30"), "no_entries_before")
    c["no_entries_after"] = _time(c.get("no_entries_after", "15:10"), "no_entries_after")
    if c["no_entries_before"] >= c["no_entries_after"] or c["no_entries_after"] > time(15, 15):
        raise RiskError("entry window must fit the normal 09:15–15:15 IST research session")
    c["blocked"] = {ticker(x) for x in c.get("blocked", [])}
    return c


def _portfolio(doc, budget, prices=None, now=None):
    if doc is None:
        return budget, {}, budget, "assumed paper budget"
    cash = amount(doc.get("cash_inr", doc.get("cash")), "portfolio cash", zero=True)
    holdings = {}
    valued = []
    complete_basis = True
    for item in doc.get("positions", []):
        if not isinstance(item, dict):
            raise RiskError("portfolio positions must be objects")
        sym = ticker(item.get("ticker") or item.get("symbol"))
        qty = _count(item.get("quantity", item.get("shares")), "holding quantity")
        holdings[sym] = holdings.get(sym, 0) + qty
        quote = _price_record(prices, sym, now)[0] if prices is not None and now is not None else None
        basis = item.get("average_cost_inr")
        if quote is not None:
            valued.append(qty * quote)
        elif basis is not None:
            valued.append(qty * amount(basis, "holding average cost"))
        else:
            complete_basis = False
    if doc.get("positions") and complete_basis:
        equity = cash + sum(valued, Decimal(0))
        source = "paper portfolio valued at fresh quotes where available, otherwise average cost"
    else:
        equity = amount(doc.get("equity_inr", doc.get("equity", budget)), "paper account equity")
        source = "paper portfolio stored equity (quotes/cost basis incomplete)"
    return cash, holdings, equity, source


def _day_state(state, now):
    """Reconcile same-day counters with the append-only simulated-fill log."""
    today = now.date().isoformat()
    events = state.get("orders", [])
    if not isinstance(events, list):
        raise RiskError("broker order log must be a list")
    fills = [x for x in events if isinstance(x, dict) and x.get("date") == today
             and x.get("status") == "simulated_fill"]
    keys = {(x.get("fingerprint"), x.get("side")) for x in fills}
    entries = {(x.get("fingerprint"), x.get("ticker")) for x in fills if x.get("kind") == "entry"}
    loss = sum((amount(x.get("max_loss_inr", 0), "logged max loss", zero=True)
                for x in fills if x.get("kind") == "entry"), Decimal(0))
    current = state if state.get("date") in (None, today) else {}
    return {
        "daily_planned_loss_inr": max(amount(current.get("daily_planned_loss_inr", 0), "daily planned loss", zero=True), loss),
        "trades_today": max(_count(current.get("trades_today", 0), "trades_today"), len(entries)),
        "orders_today": max(_count(current.get("orders_today", 0), "orders_today"), len(keys)),
        "traded_symbols_today": sorted({ticker(x) for x in current.get("traded_symbols_today", [])}
                                        | {ticker(x["ticker"]) for x in fills if x.get("kind") == "entry"}),
    }


def review(orders, cfg, now, portfolio=None, prices=None, state=None):
    if not isinstance(orders, list):
        raise RiskError("orders must be a list")
    state = state or {}
    if not isinstance(state, dict):
        raise RiskError("broker state must be an object")
    state = _day_state(state, now)
    budget = cfg.get("budget_inr")
    if budget is None:
        budget = next((amount(o.get("capital_inr"), "proposal capital") for o in orders
                       if isinstance(o, dict) and o.get("capital_inr")), None)
    if budget is None:
        raise RiskError("risk check needs budget_inr or a paper proposal with capital_inr")
    cash, holdings, equity, account_source = _portfolio(portfolio, budget, prices, now)
    daily_loss = amount(state.get("daily_planned_loss_inr", 0), "daily planned loss", zero=True)
    trades = _count(state.get("trades_today", 0), "trades_today")
    orders_today = _count(state.get("orders_today", trades), "orders_today")
    previously = {ticker(x) for x in state.get("traded_symbols_today", [])}
    seen = set()
    rows = []
    session, session_reason = trading_day(now.date())
    for index, order in enumerate(orders, 1):
        reasons, warnings = [], []
        if not isinstance(order, dict):
            rows.append({"index": index, "status": "BLOCK", "reasons": ["INVALID_ORDER"],
                         "warnings": [], "fingerprint": None})
            continue
        fingerprint = _fingerprint(order)
        if order.get("fingerprint") != fingerprint:
            reasons.append("ORDER_CHANGED")
        sym = None
        try:
            sym = ticker(order.get("ticker"))
            if sym != order.get("ticker"):
                reasons.append("INVALID_TICKER")
        except BudgetError:
            reasons.append("INVALID_TICKER")
        side = order.get("side")
        entry_side = side == "BUY_LONG_CASH"
        exit_side = side == "SELL_LONG_CASH"
        if not entry_side and not exit_side:
            reasons.append("UNSUPPORTED_SIDE_OR_ASSET")
        try:
            qty = _count(order.get("quantity"), "quantity")
            if qty < 1:
                reasons.append("INVALID_QUANTITY")
        except RiskError:
            qty = 0
            reasons.append("INVALID_QUANTITY")
        if order.get("orders_possible") is not False or order.get("mode") not in ("PAPER_ONLY", "PAPER_EXIT_ONLY"):
            reasons.append("NOT_A_PAPER_PROPOSAL")
        if entry_side and order.get("status") != "PROPOSED":
            reasons.append("NOT_A_PAPER_PROPOSAL")
        if exit_side and order.get("status") != "PROPOSED_EXIT":
            reasons.append("INVALID_EXIT")
        if sym and (sym, side) in seen:
            reasons.append("DUPLICATE_BATCH_ORDER")
        if sym:
            seen.add((sym, side))
        if exit_side:
            if sym and qty > holdings.get(sym, 0):
                reasons.append("EXIT_EXCEEDS_HOLDING")
            for key in ("order_type", "type"):
                if key in order and order[key] not in ("market_reference", "market", "limit"):
                    reasons.append("INVALID_EXIT_FORMAT")
            if not reasons:
                holdings[sym] -= qty
                orders_today += 1
            rows.append({"index": index, "ticker": sym, "side": side,
                         "status": "PASS" if not reasons else "BLOCK", "kind": "exit",
                         "reasons": reasons, "warnings": warnings,
                         "order_fingerprint": fingerprint, "fingerprint": fingerprint,
                         "max_loss_inr": 0, "reward_risk": None})
            continue
        # An unsupported sell is never converted to a short entry.
        if not entry_side:
            rows.append({"index": index, "ticker": sym, "side": side, "status": "BLOCK",
                         "reasons": reasons, "warnings": warnings,
                         "order_fingerprint": fingerprint, "fingerprint": fingerprint})
            continue
        if sym in cfg["blocked"]:
            reasons.append("BLOCKED_TICKER")
        if sym in previously:
            reasons.append("DUPLICATE_TICKER_TODAY")
        if not session or not cfg["no_entries_before"] <= now.time() <= cfg["no_entries_after"]:
            reasons.append("MARKET_OR_ENTRY_WINDOW_CLOSED")
        if not session:
            warnings.append(session_reason)
        if trades >= cfg["max_trades_per_day"]:
            reasons.append("DAILY_TRADE_LIMIT")
        if orders_today >= cfg["max_orders_per_day"]:
            reasons.append("DAILY_ORDER_LIMIT")
        if order.get("plan_date") and order["plan_date"] != now.date().isoformat():
            reasons.append("PLAN_DATE_MISMATCH")
        trigger = order.get("trigger_at")
        if not trigger:
            reasons.append("UNTRIGGERED_PLAN")
        else:
            try:
                at = datetime.fromisoformat(str(trigger).replace("Z", "+00:00"))
                if at.tzinfo is None or not 0 <= (now - at.astimezone(IST)).total_seconds() <= 300:
                    reasons.append("STALE_OR_FUTURE_ENTRY")
            except ValueError:
                reasons.append("INVALID_TRIGGER_TIME")
        try:
            entry = money(amount(order.get("entry_reference"), "entry"))
            stop = money(amount(order.get("stop"), "stop"))
            target = money(amount(order.get("target"), "target"))
            if not stop < entry < target:
                reasons.append("INVALID_BRACKET")
            if order.get("order_type") == "limit":
                limit_price = money(amount(order.get("limit_price"), "limit_price"))
                if limit_price != entry:
                    reasons.append("LIMIT_PRICE_MISMATCH")
            elif order.get("order_type") not in (None, "market_reference"):
                reasons.append("INVALID_ORDER_TYPE")
            quote, note = _price_record(prices, sym, now) if sym else (None, "no valid ticker")
            if note != "no quote file":
                warnings.append(note)
            ref = entry if order.get("order_type") == "limit" else max(entry, money(quote) if quote else entry)
            slip = amount(order.get("slippage_bps_per_side", 0), "slippage bps", zero=True) / 10000
            fees = amount(order.get("fee_buffer_inr", 0), "fee buffer", zero=True)
            buy = money(ref * (1 + slip))
            stop_fill = money(stop * (1 - slip))
            risk = buy - stop_fill
            if risk <= 0 or target <= buy:
                reasons.append("INVALID_BRACKET")
                rr = Decimal(0)
            else:
                rr = (target - buy) / risk
            loss = money(qty * max(Decimal(0), risk) + fees)
            notional = money(qty * buy)
            if loss > cfg["max_loss_per_trade_inr"] or loss > amount(order.get("max_loss_inr", 0), "order max loss", zero=True):
                reasons.append("PER_TRADE_LOSS_LIMIT")
            if rr < cfg["min_reward_risk"]:
                reasons.append("REWARD_RISK_TOO_LOW")
            if daily_loss + loss > cfg["max_daily_loss_inr"]:
                reasons.append("DAILY_LOSS_LIMIT")
            if notional < cfg["min_order_inr"] or notional > cfg["max_order_inr"]:
                reasons.append("ORDER_SIZE_LIMIT")
            if notional + holdings.get(sym, 0) * ref > money(equity * cfg["max_position_pct"] / 100):
                reasons.append("POSITION_CAP")
            if notional > cash - money(equity * cfg["cash_buffer_pct"] / 100):
                reasons.append("CASH_BUFFER_OR_MARGIN")
            if order.get("planned_loss_inr") is not None and loss > money(amount(order["planned_loss_inr"], "planned loss", zero=True)):
                warnings.append("Risk increased from the saved budget estimate")
        except (BudgetError, ValueError, TypeError, ArithmeticError):
            reasons.append("INVALID_PRICE_OR_RISK")
            loss = notional = rr = Decimal(0)
        if not reasons:
            cash -= notional
            daily_loss += loss
            trades += 1
            orders_today += 1
            previously.add(sym)
            holdings[sym] = holdings.get(sym, 0) + qty
        rows.append({"index": index, "ticker": sym, "side": side, "kind": "entry",
                     "status": "PASS" if not reasons else "BLOCK", "reasons": sorted(set(reasons)),
                     "warnings": warnings, "order_fingerprint": fingerprint,
                     "fingerprint": fingerprint, "max_loss_inr": float(loss),
                     "reward_risk": round(float(rr), 3), "notional_inr": float(notional)})
    passed = sum(row["status"] == "PASS" for row in rows)
    return {"created_by": "india-trader-risk", "mode": "PAPER_ONLY",
            "status": "PASS" if passed == len(rows) and rows else "BLOCK",
            "checked_at": now.isoformat(), "calendar_verified": session,
            "calendar_note": "Normal-session 2026 list only; special sessions not verified",
            "orders_possible": False, "account_source": account_source,
            "passed": passed, "blocked": len(rows) - passed, "verdicts": rows,
            "cash_after_assumed_fills_inr": float(money(cash)),
            "daily_loss_after_assumed_fills_inr": float(money(daily_loss)),
            "note": "Sequential assumptions for risk checks only; no fill or order is recorded"}


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m india_trader risk-manager")
    p.add_argument("--orders", default="trader/orders.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--now")
    p.add_argument("--today", help="YYYY-MM-DD replay day; must match the IST clock")
    p.add_argument("--out", default="trader/state/risk.json")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        now = clock(a.now)
        if a.today and a.today != now.date().isoformat():
            raise RiskError("--today must match the IST replay/current date")
        source = read_object(a.orders, "paper orders")
        orders = source.get("orders") if "orders" in source else [source]
        cfg = limits(config(a.config))
        result = review(orders, cfg, now,
                        read_object(a.portfolio, "paper portfolio", optional=True),
                        read_object(a.prices, "prices", optional=True),
                        read_object(a.state, "paper broker state", optional=True))
        if not a.no_write:
            target = Path(a.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        if a.json:
            print(json.dumps(result, indent=2))
        else:
            for row in result["verdicts"]:
                print(f"{row['status']} #{row['index']} {row.get('ticker') or '?'}: " +
                      (", ".join(row["reasons"]) if row["reasons"] else
                       f"max loss Rs.{row['max_loss_inr']:.2f}, reward:risk {row.get('reward_risk')}"))
            print(f"{result['passed']} passed, {result['blocked']} blocked; paper only")
        return 0 if result["status"] == "PASS" else 1
    except (BudgetError, RiskError, ValueError, OSError, KeyError, TypeError) as exc:
        print("risk-manager: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
