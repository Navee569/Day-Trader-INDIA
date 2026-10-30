"""INR paper budget manager adapted from the MIT-licensed trader-budget workflow.

Calculations only. No broker client, credentials, orders, or simulated fills.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
import json
from pathlib import Path
import sys

from .core import IST, symbol
from .paper_orders import _fingerprint


class BudgetError(Exception):
    pass


PAISE = Decimal("0.01")
DEFAULTS = {
    "budget_inr": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 20,
    "max_order_inr": 25000,
    "min_order_inr": 100,
    "max_loss_per_trade_inr": 500,
    "min_reward_risk": 1,
    "slippage_bps_per_side": 10,
    "fee_buffer_inr": 50,
    "blocked": [],
    "paper": True,
}


def amount(value, what, zero=False):
    try:
        x = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BudgetError(f"{what} must be a finite number") from exc
    if not x.is_finite() or (x < 0 if zero else x <= 0) or x > Decimal("1000000000000"):
        raise BudgetError(f"{what} must be {'nonnegative' if zero else 'positive'}")
    return x


def money(value):
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


def shares(funds, per_share):
    return max(0, int((funds / per_share).to_integral_value(rounding=ROUND_FLOOR)))


def clock(raw):
    if raw is None:
        return datetime.now(IST)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BudgetError("--now needs an ISO date/time") from exc
    return (dt.replace(tzinfo=IST) if dt.tzinfo is None else dt).astimezone(IST)


def read_object(path, what, optional=False):
    p = Path(path)
    if optional and not p.exists():
        return None
    if not p.is_file():
        raise BudgetError(f"{what} missing: {p}")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise BudgetError(f"invalid {what}: {exc}") from exc
    if not isinstance(doc, dict):
        raise BudgetError(f"{what} must be a JSON object")
    return doc


def ticker(raw):
    s = str(raw or "").strip().upper()
    if not s:
        raise BudgetError("ticker is required")
    if not s.endswith((".NS", ".BO")):
        s += ".NS"
    exchange = "BSE" if s.endswith(".BO") else "NSE"
    try:
        if symbol(s, exchange) != s:
            raise BudgetError("invalid NSE/BSE ticker")
    except (TypeError, ValueError) as exc:
        raise BudgetError(f"invalid NSE/BSE ticker: {s}") from exc
    return s


def config(path):
    raw = read_object(path, "budget config", optional=True) or {}
    if "paper" in raw and raw["paper"] is not True:
        raise BudgetError("budget manager is paper-only; config cannot enable live mode")
    cfg = dict(DEFAULTS, **raw)
    for key in ("cash_buffer_pct", "max_position_pct", "max_order_inr", "min_order_inr",
                "max_loss_per_trade_inr", "min_reward_risk", "slippage_bps_per_side", "fee_buffer_inr"):
        cfg[key] = amount(cfg[key], key, zero=key in ("cash_buffer_pct", "min_order_inr",
                                                     "min_reward_risk", "slippage_bps_per_side", "fee_buffer_inr"))
    if cfg["budget_inr"] is not None:
        cfg["budget_inr"] = amount(cfg["budget_inr"], "budget_inr")
    if not (cfg["cash_buffer_pct"] < 100 and cfg["max_position_pct"] <= 100 and
            cfg["slippage_bps_per_side"] <= 1000 and
            cfg["fee_buffer_inr"] < cfg["max_loss_per_trade_inr"]):
        raise BudgetError("invalid buffer, position cap, slippage, or fee/loss combination")
    blocked = cfg.get("blocked") or []
    if not isinstance(blocked, list):
        raise BudgetError("blocked must be a list")
    cfg["blocked"] = {ticker(x) for x in blocked}
    return cfg


def budget_value(cfg, override):
    value = override if override is not None else cfg.get("budget_inr")
    if value is None:
        raise BudgetError("set budget_inr in trader/config.json or pass --budget INR")
    return amount(value, "budget")


def _price_record(doc, sym, now):
    """A dated local quote is usable for risk sizing for at most 15 minutes."""
    if doc is None:
        return None, "no quote file"
    prices = doc.get("prices", doc)
    if not isinstance(prices, dict) or sym not in prices:
        return None, "no quote for ticker"
    value = amount(prices[sym], f"quote for {sym}")
    if money(value) < PAISE:
        raise BudgetError(f"quote for {sym} is below one paise")
    detail = doc.get("detail") or {}
    detail = detail.get(sym, {}) if isinstance(detail, dict) else {}
    if not isinstance(detail, dict):
        detail = {}
    stamp = detail.get("time") or detail.get("fetched") or doc.get("as_of")
    if not stamp:
        return None, "undated quote ignored"
    try:
        at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None, "invalid quote time ignored"
    if at.tzinfo is None:
        return None, "quote without timezone ignored"
    age = (now - at.astimezone(IST)).total_seconds()
    if not -60 <= age <= 900:
        return None, "quote older than 15 minutes or from the future ignored"
    return value, f"dated quote at {at.astimezone(IST).isoformat()}"


def size_trade(plan, cfg, now, budget=None, limit=None, price_doc=None, portfolio=None,
               max_loss=None):
    if plan.get("mode") != "PAPER_PLAN_ONLY" or plan.get("status") != "PLANNED":
        raise BudgetError("use a paper exit plan from exit-planner")
    if str(plan.get("side") or "BUY_LONG_CASH").lower() not in ("buy", "long", "buy_long_cash"):
        raise BudgetError("budget manager sizes long cash-equity paper plans only")
    sym = ticker(plan.get("ticker") or plan.get("symbol"))
    if sym in cfg["blocked"]:
        raise BudgetError(f"{sym} is on the blocked list")
    if str(plan.get("date")) != now.date().isoformat():
        raise BudgetError("plan date differs from the IST replay/current date; create a new plan")
    entry = money(amount(plan.get("entry_reference", plan.get("entry")), "plan entry"))
    stop = money(amount(plan.get("stop"), "stop"))
    target = money(amount(plan.get("target"), "target"))
    if not stop < entry < target:
        raise BudgetError("long plan needs stop < entry < target")
    funds = budget_value(cfg, budget)
    loss_limit = amount(max_loss, "max loss") if max_loss is not None else cfg["max_loss_per_trade_inr"]
    if cfg["fee_buffer_inr"] >= loss_limit:
        raise BudgetError("fee buffer must be smaller than the max loss")
    quote, quote_note = _price_record(price_doc, sym, now)
    if limit is not None:
        ref = entry if limit == "plan" else money(amount(limit, "limit price"))
        ref_from = "limit"
    elif quote is not None and money(quote) > entry:
        ref, ref_from = money(quote), "fresh quote"
    else:
        ref, ref_from = entry, "plan"
    if not stop < ref < target:
        raise BudgetError("sizing price must be above stop and below target; make a new plan")
    slip = cfg["slippage_bps_per_side"] / Decimal(10000)
    buy = money(ref * (1 + slip))
    stop_fill = money(stop * (1 - slip))
    if buy >= target:
        raise BudgetError("target does not exceed the slippage-adjusted entry")
    risk = buy - stop_fill
    rr = (target - buy) / risk
    buffer = money(funds * cfg["cash_buffer_pct"] / 100)
    available = funds - buffer
    cash_note = "budget less cash buffer"
    if portfolio is not None:
        cash = amount(portfolio.get("cash_inr", portfolio.get("cash")), "portfolio cash", zero=True)
        if cash - buffer < available:
            available = max(Decimal(0), cash - buffer)
            cash_note = "portfolio cash less cash buffer"
    caps = [
        ("max loss", loss_limit - cfg["fee_buffer_inr"], risk),
        ("position cap", money(funds * cfg["max_position_pct"] / 100), buy),
        ("max order", cfg["max_order_inr"], buy),
        ("cash", max(Decimal(0), available), buy),
    ]
    limits = [{"name": name, "limit_inr": float(value), "per_share_inr": float(per),
               "shares": shares(value, per)} for name, value, per in caps]
    qty = min(row["shares"] for row in limits)
    bound = next(row["name"] for row in limits if row["shares"] == qty)
    for row in limits:
        row["binds"] = row["name"] == bound
    notional = money(buy * qty)
    estimated_loss = money(risk * qty + cfg["fee_buffer_inr"])
    refusal = None
    if qty == 0:
        refusal = (f"one share risks Rs.{risk:.2f}, above the available per-trade loss"
                   if bound == "max loss" else f"one share exceeds the {bound} limit")
    elif rr < cfg["min_reward_risk"]:
        refusal = f"reward:risk {rr:.2f} is below minimum {cfg['min_reward_risk']}"
    elif notional < cfg["min_order_inr"]:
        refusal = f"Rs.{notional:.2f} is below the minimum order Rs.{cfg['min_order_inr']:.2f}"
    sizing = {"mode": "PAPER_ONLY", "ticker": sym, "date": now.date().isoformat(),
              "entry_plan": float(entry), "entry_reference": float(ref), "entry_from": ref_from,
              "quote_note": quote_note, "order_type": "limit" if limit is not None else "market_reference",
              "stop": float(stop), "target": float(target), "risk_per_share_inr": float(risk),
              "reward_risk_after_slippage": round(float(rr), 4), "budget_inr": float(funds),
              "cash_buffer_inr": float(buffer), "cash_available_inr": float(max(0, available)),
              "cash_note": cash_note, "limits": limits, "quantity": qty, "bound_by": bound,
              "planned_notional_inr": float(notional), "planned_loss_inr": float(estimated_loss),
              "max_loss_inr": float(loss_limit), "refusal": refusal,
              "plan_entry_confirmed": bool(plan.get("entry_confirmed")),
              "market_lot_assumed": 1,
              "note": "Gross paper sizing; one-share lot, stop fill, charges and slippage are assumptions"}
    if refusal:
        return sizing, None
    order = {"mode": "PAPER_ONLY", "status": "PROPOSED", "orders_possible": False,
             "ticker": sym, "side": "BUY_LONG_CASH", "quantity": qty,
             "entry_reference": float(ref), "stop": float(stop), "target": float(target),
             "order_type": sizing["order_type"],
             "limit_price": float(ref) if limit is not None else None,
             "trigger_at": plan.get("trigger_at"), "flat_by_ist": plan.get("flat_by_ist"),
             "plan_date": plan.get("date"), "capital_inr": float(funds),
             "max_loss_inr": float(loss_limit), "max_position_pct": float(cfg["max_position_pct"]),
             "max_order_inr": float(cfg["max_order_inr"]),
             "cash_buffer_pct": float(cfg["cash_buffer_pct"]),
             "slippage_bps_per_side": float(cfg["slippage_bps_per_side"]),
             "fee_buffer_inr": float(cfg["fee_buffer_inr"]),
             "planned_notional_inr": float(notional), "planned_loss_inr": float(estimated_loss),
             "market_lot_assumed": 1,
             "bound_by": bound, "sizing_limits": limits, "data_origin": plan.get("data_origin"),
             "paper_bracket": {"entry_reference": float(ref), "stop_loss": float(stop),
                               "take_profit": float(target)},
             "note": "Paper proposal only; no broker order or guaranteed stop fill"}
    order["fingerprint"] = _fingerprint(order)
    return sizing, order


def parse_weights(data):
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    if isinstance(data, dict):
        data = [{"ticker": key, "weight": value} for key, value in data.items()]
    if not isinstance(data, list):
        raise BudgetError("weights must be a list or ticker-to-weight object")
    rows, seen = [], set()
    for item in data:
        if not isinstance(item, dict):
            raise BudgetError("every weight row must be an object")
        sym = ticker(item.get("ticker") or item.get("symbol"))
        if sym in seen:
            raise BudgetError(f"duplicate ticker in weights: {sym}")
        seen.add(sym)
        value = amount(item.get("weight"), f"weight for {sym}", zero=True)
        rows.append({"ticker": sym, "name": str(item.get("name") or "")[:80],
                     "weight": value})
    return rows


def inline_weights(raw):
    out = []
    for pair in raw.split(","):
        parts = pair.strip().split("=", 1)
        if len(parts) != 2:
            raise BudgetError("--weights needs TICKER=WEIGHT pairs")
        out.append({"ticker": parts[0], "weight": parts[1]})
    return parse_weights(out)


def _allocate(rows, investable, cap):
    remaining = list(rows)
    allocations, capped = {}, set()
    left = investable
    while remaining:
        total_weight = sum((r["weight"] for r in remaining), Decimal(0))
        trial = {r["ticker"]: left * r["weight"] / total_weight for r in remaining}
        over = [r for r in remaining if trial[r["ticker"]] > cap]
        if not over:
            allocations.update(trial)
            break
        for r in over:
            allocations[r["ticker"]] = cap
            capped.add(r["ticker"])
            left -= cap
        remaining = [r for r in remaining if r["ticker"] not in capped]
        if left <= 0:
            allocations.update({r["ticker"]: Decimal(0) for r in remaining})
            break
    return allocations, capped


def _round_allocations(exact, cap, unit):
    rounded = {sym: (value / unit).to_integral_value(rounding=ROUND_FLOOR) * unit
               for sym, value in exact.items()}
    residue = sum(exact.values(), Decimal(0)) - sum(rounded.values(), Decimal(0))
    units = int((residue / unit).to_integral_value(rounding=ROUND_FLOOR))
    order = sorted(exact, key=lambda s: (-(exact[s] - rounded[s]), s))
    for sym in order:
        if units == 0:
            break
        if rounded[sym] + unit <= cap:
            rounded[sym] += unit
            units -= 1
    return rounded


def size_weights(rows, cfg, now, budget=None, prices_doc=None, portfolio=None,
                 top=None, cents=False, whole_shares=False):
    funds = budget_value(cfg, budget)
    if top is not None and top < 1:
        raise BudgetError("--top must be a positive count")
    keep, dropped = [], []
    for row in rows:
        if row["ticker"] in cfg["blocked"]:
            dropped.append({"ticker": row["ticker"], "reason": "blocked ticker"})
        elif row["weight"] == 0:
            dropped.append({"ticker": row["ticker"], "reason": "zero weight"})
        else:
            keep.append(row)
    keep.sort(key=lambda r: (-r["weight"], r["ticker"]))
    if top is not None:
        for row in keep[top:]:
            dropped.append({"ticker": row["ticker"], "reason": f"outside top {top}"})
        keep = keep[:top]
    if not keep:
        raise BudgetError("no positive unblocked weights remain")
    buffer = money(funds * cfg["cash_buffer_pct"] / 100)
    investable = funds - buffer
    if portfolio is not None:
        cash = amount(portfolio.get("cash_inr", portfolio.get("cash")), "portfolio cash", zero=True)
        investable = max(Decimal(0), min(investable, cash - buffer))
    cap = min(money(funds * cfg["max_position_pct"] / 100), cfg["max_order_inr"])
    unit = PAISE if cents else Decimal(1)
    price_map = {}
    if prices_doc is not None:
        for row in keep:
            price, note = _price_record(prices_doc, row["ticker"], now)
            if price is None:
                raise BudgetError(f"{row['ticker']} needs a dated price for whole-share sizing: {note}")
            price_map[row["ticker"]] = money(price)
    elif whole_shares:
        raise BudgetError("--whole-shares needs --prices with dated INR prices")
    while True:
        total = sum((r["weight"] for r in keep), Decimal(0))
        normalized = [{**r, "normalized": r["weight"] * 100 / total} for r in keep]
        exact, capped = _allocate(keep, investable, cap)
        rupees = _round_allocations(exact, cap, unit)
        quantities = None
        if price_map:
            quantities = {sym: shares(rupees[sym], price_map[sym]) for sym in rupees}
            spent = sum((quantities[s] * price_map[s] for s in quantities), Decimal(0))
            left = investable - spent
            while True:
                eligible = [s for s in quantities if price_map[s] <= left and
                            (quantities[s] + 1) * price_map[s] <= cap]
                if not eligible:
                    break
                best = max(eligible, key=lambda s: (rupees[s] - quantities[s] * price_map[s], s))
                unit_price = price_map[best]
                capacity = shares(cap - quantities[best] * unit_price, unit_price)
                extra = min(capacity, shares(left, unit_price),
                            max(1, shares(left / len(eligible), unit_price)))
                quantities[best] += extra
                left -= extra * unit_price
            rupees = {sym: money(quantities[sym] * price_map[sym]) for sym in quantities}
        small = [r for r in keep if rupees[r["ticker"]] < cfg["min_order_inr"]]
        if not small:
            break
        worst = min(small, key=lambda r: (r["weight"], r["ticker"]))
        dropped.append({"ticker": worst["ticker"], "reason": "below minimum order after whole-share rounding"
                        if price_map else "below minimum allocation"})
        keep = [r for r in keep if r["ticker"] != worst["ticker"]]
        if not keep:
            raise BudgetError("every allocation is below the minimum order")
    positions = []
    for row in normalized:
        sym = row["ticker"]
        position = {"ticker": sym, "name": row["name"], "source_weight": float(row["weight"]),
                    "weight_pct": round(float(row["normalized"]), 2),
                    "allocation_inr": float(rupees[sym]), "capped": sym in capped}
        if price_map:
            position.update({"price_reference_inr": float(price_map[sym]),
                             "quantity": quantities[sym]})
        positions.append(position)
    invested = sum((rupees[p["ticker"]] for p in positions), Decimal(0))
    result = {"mode": "PAPER_ALLOCATION_ONLY", "date": now.date().isoformat(),
              "budget_inr": float(funds), "cash_buffer_inr": float(buffer),
              "investable_inr": float(investable), "max_position_inr": float(cap),
              "priced_whole_shares": bool(price_map), "positions": positions,
              "invested_inr": float(money(invested)),
              "idle_inr": float(money(investable - invested)), "dropped": dropped,
              "no_stop": True, "orders_possible": False,
              "market_lot_assumed": 1,
              "note": "Allocation research only; one-share lot is assumed and every ticker needs its own stop and risk review"}
    proposals = []
    if price_map:
        for p in positions:
            proposals.append({"mode": "PAPER_ALLOCATION_ONLY", "status": "UNREVIEWED_NO_STOP",
                              "orders_possible": False, "ticker": p["ticker"], "side": "BUY_LONG_CASH",
                              "quantity": p["quantity"], "price_reference_inr": p["price_reference_inr"],
                              "planned_notional_inr": p["allocation_inr"], "stop": None,
                              "note": "No stop or target; not eligible for paper risk approval"})
    return result, proposals


def _save(path, doc):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _report_trade(sizing, order):
    s = sizing
    lines = [f"Paper budget: {s['ticker']} at Rs.{s['entry_reference']:.2f} ({s['entry_from']}); "
             f"stop Rs.{s['stop']:.2f}, target Rs.{s['target']:.2f}.",
             f"  Budget Rs.{s['budget_inr']:.2f}; cash buffer Rs.{s['cash_buffer_inr']:.2f}; "
             f"risk/share including assumed slippage Rs.{s['risk_per_share_inr']:.2f}."]
    for row in s["limits"]:
        lines.append(f"  {row['name']}: {row['shares']} shares" + (" (binds)" if row["binds"] else ""))
    lines.append(f"  {s['quote_note']}; {s['cash_note']}.")
    if order is None:
        lines.append("NO PAPER PROPOSAL: " + s["refusal"])
    else:
        lines.append(f"  Proposed {s['quantity']} whole shares, Rs.{s['planned_notional_inr']:.2f} "
                     f"notional, Rs.{s['planned_loss_inr']:.2f} planned loss at the assumed stop fill.")
        lines.append("Paper proposal only; pass the saved file to risk review. A stop fill can be worse.")
    return "\n".join(lines)


def _report_weights(result):
    lines = [f"Indicative India paper split: budget Rs.{result['budget_inr']:.2f}; "
             f"investable Rs.{result['investable_inr']:.2f}; cap/stock Rs.{result['max_position_inr']:.2f}."]
    for row in result["positions"]:
        extra = f", {row['quantity']} whole shares at Rs.{row['price_reference_inr']:.2f}" if "quantity" in row else ""
        lines.append(f"  {row['ticker']}: {row['weight_pct']:.2f}% -> Rs.{row['allocation_inr']:.2f}" +
                     extra + (" (capped)" if row["capped"] else ""))
    lines.append(f"  Invested Rs.{result['invested_inr']:.2f}; idle Rs.{result['idle_inr']:.2f}.")
    for row in result["dropped"]:
        lines.append(f"  Dropped {row['ticker']}: {row['reason']}.")
    lines.append("Allocation only: no stops or targets. Each ticker needs its own exit plan and risk review.")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m india_trader budget-manager",
                                description="INR paper-only trade sizing and weighted allocation")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--budget", help="hypothetical capital in INR; overrides config")
    p.add_argument("--max-loss", help="hypothetical max loss per trade in INR")
    p.add_argument("--limit", nargs="?", const="plan", metavar="PRICE")
    p.add_argument("--prices", help="dated local INR quote JSON, default trader/prices.json if present")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--now", help="ISO IST/offset-aware clock for reproducible paper replay")
    p.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH")
    p.add_argument("--json", action="store_true")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--weights", help="inline TICKER=WEIGHT pairs")
    mode.add_argument("--targets", help="JSON weights list or ticker-to-weight object")
    p.add_argument("--top", type=int)
    p.add_argument("--whole-shares", action="store_true")
    p.add_argument("--cents", action="store_true", help="round indicative INR slices to paise")
    a = p.parse_args(argv)
    try:
        cfg, now = config(a.config), clock(a.now)
        prices_path = a.prices or ("trader/prices.json" if Path("trader/prices.json").is_file() else None)
        prices_doc = read_object(prices_path, "prices") if prices_path else None
        portfolio = read_object(a.portfolio, "paper portfolio", optional=True) if a.portfolio else None
        if a.weights or a.targets:
            if a.limit is not None or a.max_loss is not None:
                raise BudgetError("--limit and --max-loss apply to a single exit plan")
            doc = read_object(a.targets, "targets") if a.targets else None
            rows = parse_weights(doc) if doc is not None else inline_weights(a.weights)
            result, orders = size_weights(rows, cfg, now, a.budget, prices_doc, portfolio,
                                          a.top, a.cents, a.whole_shares)
            display = {"result": result, "orders": orders, "source": doc.get("source") if doc else "inline weights"}
            status = "ALLOCATION_ONLY"
            output = _report_weights(result)
            code = 0
        else:
            if a.top is not None or a.whole_shares or a.cents:
                raise BudgetError("--top, --whole-shares and --cents need weights mode")
            plan = read_object(a.plan, "paper exit plan")
            sizing, order = size_trade(plan, cfg, now, a.budget, a.limit, prices_doc, portfolio, a.max_loss)
            orders = [order] if order else []
            display = {"sizing": sizing, "orders": orders}
            status = "PROPOSED" if order else "REFUSED"
            output = _report_trade(sizing, order)
            code = 0 if order else 1
        if a.write:
            saved = {"created_by": "india-trader-budget", "created_at": now.isoformat(),
                     "mode": "PAPER_ONLY", "status": status, "orders_possible": False,
                     "orders": orders, "note": "Paper proposals only; no broker connection or fill"}
            _save(a.write, saved)
            if not a.json:
                output += "\nSaved " + a.write
        print(json.dumps(display, indent=2, allow_nan=False) if a.json else output)
        return code
    except (BudgetError, ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        if a.write:
            try:
                _save(a.write, {"created_by": "india-trader-budget", "mode": "PAPER_ONLY",
                                "status": "REFUSED", "orders_possible": False, "orders": [],
                                "reason": str(exc)})
            except OSError:
                pass
        print("budget-manager: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
