"""Local paper broker ledger. Every fill is an explicitly entered simulation."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time as clock
from contextlib import contextmanager
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile

from .core import IST, as_datetime, decimal, symbol
from .paper_orders import paper_ticket, risk_review
from .market_calendar import regular_nse_entry_day


def new_ledger(cash: str = "100000") -> dict:
    amount = decimal(cash, "starting paper cash")
    if amount <= 0:
        raise ValueError("Starting paper cash must be positive")
    return {"mode": "PAPER_ONLY", "cash_inr": str(amount), "positions": {},
            "orders": [], "events": [], "next_id": 1, "orders_possible": False}


def validate_ledger(ledger: dict) -> None:
    """Reject corrupt local paper state before a CLI transaction can use it."""
    if (not isinstance(ledger, dict) or ledger.get("mode") != "PAPER_ONLY" or
            ledger.get("orders_possible") is not False or
            not isinstance(ledger.get("orders"), list) or
            not isinstance(ledger.get("positions"), dict) or
            not isinstance(ledger.get("events"), list)):
        raise ValueError("Invalid paper-only ledger structure")
    if decimal(ledger.get("cash_inr"), "paper cash") < 0:
        raise ValueError("Invalid paper cash")
    next_id = ledger.get("next_id")
    if isinstance(next_id, bool) or not isinstance(next_id, int) or next_id < 1:
        raise ValueError("Invalid next paper order ID")
    seen = set()
    for row in ledger["orders"]:
        if not isinstance(row, dict):
            raise ValueError("Invalid paper order row")
        ident, qty = row.get("id"), row.get("quantity")
        ticker = row.get("ticker")
        if (isinstance(ident, bool) or not isinstance(ident, int) or ident < 1 or
                ident in seen or ident >= next_id or isinstance(qty, bool) or
                not isinstance(qty, int) or qty < 0 or not isinstance(ticker, str) or
                row.get("mode") != "PAPER_ONLY" or row.get("orders_possible") is not False or
                row.get("side") not in ("BUY_LONG_CASH", "SELL_TO_CLOSE") or
                not isinstance(row.get("fingerprint"), str) or not row["fingerprint"]):
            raise ValueError("Invalid or duplicate paper order ID or fields")
        seen.add(ident)
        exchange = "BSE" if ticker.endswith(".BO") else "NSE"
        if symbol(ticker, exchange) != ticker:
            raise ValueError("Invalid paper order symbol")
        if row["side"] == "BUY_LONG_CASH":
            filled = row.get("filled_quantity")
            fills = row.get("fills")
            if (isinstance(filled, bool) or not isinstance(filled, int) or
                    not 0 <= filled <= qty or not isinstance(fills, list) or
                    (qty == 0 and row.get("status") != "CANCELED")):
                raise ValueError("Invalid paper entry fill totals")
            total = 0
            for fill in fills:
                if (not isinstance(fill, dict) or isinstance(fill.get("quantity"), bool) or
                        not isinstance(fill.get("quantity"), int) or fill["quantity"] < 1 or
                        decimal(fill.get("price_inr"), "paper fill price") <= 0):
                    raise ValueError("Invalid paper fill row")
                total += fill["quantity"]
            if total != filled:
                raise ValueError("Paper filled quantity disagrees with recorded fills")
        elif qty < 1:
            raise ValueError("Invalid sell-to-close quantity")
    for ticker, position in ledger["positions"].items():
        if not isinstance(ticker, str) or not isinstance(position, dict):
            raise ValueError("Invalid paper position")
        exchange = "BSE" if ticker.endswith(".BO") else "NSE"
        if symbol(ticker, exchange) != ticker:
            raise ValueError("Invalid paper position symbol")
        qty = position.get("quantity")
        if (isinstance(qty, bool) or not isinstance(qty, int) or qty < 1 or
                decimal(position.get("cost_inr"), "paper position cost") <= 0):
            raise ValueError("Invalid paper position shares or cost")


def load_ledger(path: str | Path, starting_cash: str | None = None) -> dict:
    file = Path(path)
    if not file.exists():
        if starting_cash is None:
            raise ValueError("Paper ledger is missing; initialize it with paper-broker init")
        return new_ledger(starting_cash)
    with file.open(encoding="utf-8") as stream:
        ledger = json.load(stream)
    validate_ledger(ledger)
    return ledger


def save_ledger(path: str | Path, ledger: dict) -> None:
    """Replace the one ledger file atomically after validating the JSON."""
    validate_ledger(ledger)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(ledger, indent=2, allow_nan=False, ensure_ascii=False) + "\n"
    temporary = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=p.parent, prefix=".paper-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, p)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def ledger_lock(path: str | Path, timeout: float = 30):
    """Serialize each CLI read/modify/write transaction across processes."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lockfile = target.with_name(target.name + ".lock")
    with lockfile.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if lockfile.stat().st_size == 0:
                stream.write(b"\0")
                stream.flush()
            acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(stream, fcntl.LOCK_UN)
        deadline = clock.monotonic() + timeout
        while True:
            try:
                stream.seek(0)
                acquire()
                break
            except (BlockingIOError, PermissionError, OSError) as exc:
                if clock.monotonic() >= deadline:
                    raise ValueError("Paper ledger is busy; try again after the other command finishes") from exc
                clock.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            release()


def _now(value: str | datetime | None) -> datetime:
    return datetime.now(IST) if value is None else as_datetime(value)


def _order(ledger: dict, order_id: int) -> dict:
    matches = [row for row in ledger["orders"] if row.get("id") == order_id]
    if len(matches) != 1:
        raise ValueError("Unknown paper order ID")
    return matches[0]


def _event(ledger: dict, action: str, at: datetime, **details) -> None:
    ledger["events"].append({"at": at.isoformat(), "action": action, **details})


def _approval_digest(order: dict) -> str:
    keys = ("fingerprint", "ticker", "side", "quantity", "entry_reference", "trigger_at",
            "stop", "target", "flat_by_ist", "planned_loss_inr", "planned_notional_inr",
            "max_loss_inr", "fee_buffer_inr", "slippage_bps_per_side", "approved_at",
            "approval_reply")
    terms = {key: order[key] for key in keys if key in order}
    return hashlib.sha256(json.dumps(terms, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _check_approval_digest(order: dict) -> None:
    if order.get("approval_digest") != _approval_digest(order):
        raise ValueError("Approved paper ticket changed; prepare a new reviewed ticket")


def _state(ledger: dict, at: datetime) -> dict:
    today = at.date()
    entered = [o for o in ledger["orders"] if o.get("side") == "BUY_LONG_CASH" and
               o.get("approved_at") and as_datetime(o["approved_at"]).date() == today]
    reserved = sum((decimal(o["planned_notional_inr"], "reserved notional") *
                    Decimal(o["quantity"] - o["filled_quantity"]) / Decimal(o["quantity"])
                    for o in ledger["orders"] if o["status"] in ("APPROVED_UNFILLED", "PARTIALLY_FILLED", "SANDBOX_PARTIALLY_FILLED", "SANDBOX_SEND_ATTEMPTED", "SANDBOX_ACCEPTED", "SANDBOX_UNKNOWN", "SANDBOX_CANCEL_ATTEMPTED", "SANDBOX_CANCEL_ACK", "SANDBOX_CANCEL_UNKNOWN") and o["side"] == "BUY_LONG_CASH"), Decimal(0))
    return {"cash_inr": str(decimal(ledger["cash_inr"], "paper cash") - reserved),
            "trades_today": len(entered),
            "daily_planned_loss_inr": str(sum((decimal(o["planned_loss_inr"], "loss") for o in entered), Decimal(0))),
            "traded_symbols_today": [o["ticker"] for o in entered]}


def submit(ledger: dict, order: dict, review: dict, at: str | datetime | None = None,
           reply: str = "YES") -> dict:
    """Record a single approved paper proposal, after a fresh independent review."""
    stamp = _now(at)
    paper_ticket(order, review, approval=True, now=stamp)
    if order.get("side") != "BUY_LONG_CASH" or order.get("orders_possible") is not False:
        raise ValueError("Only whole-share long cash paper proposals are supported")
    if isinstance(order.get("quantity"), bool) or not isinstance(order.get("quantity"), int) or order["quantity"] < 1:
        raise ValueError("Invalid paper quantity")
    if any(o["fingerprint"] == order["fingerprint"] for o in ledger["orders"]):
        raise ValueError("This paper proposal was already submitted")
    if reply != "YES":
        raise ValueError("Approval must be an exact YES for one ticket")
    if any(o["ticker"] == order["ticker"] and o["status"] in ("APPROVED_UNFILLED", "PARTIALLY_FILLED", "SANDBOX_PARTIALLY_FILLED", "OPEN", "SANDBOX_SEND_ATTEMPTED", "SANDBOX_ACCEPTED", "SANDBOX_UNKNOWN", "SANDBOX_CANCEL_ATTEMPTED", "SANDBOX_CANCEL_ACK", "SANDBOX_CANCEL_UNKNOWN")
           for o in ledger["orders"]):
        raise ValueError("There is already an active paper order for this ticker")
    if order["ticker"] in ledger["positions"]:
        raise ValueError("There is already an open paper position for this ticker")
    fresh = risk_review(order, stamp.isoformat(), _state(ledger, stamp))
    if fresh["status"] != "PASS":
        raise ValueError("Fresh paper risk check blocked: " + ", ".join(fresh["reasons"]))
    result = copy.deepcopy(ledger)
    row = {"id": result["next_id"], "status": "APPROVED_UNFILLED", "mode": "PAPER_ONLY",
           "fingerprint": order["fingerprint"], "ticker": order["ticker"],
           "side": "BUY_LONG_CASH", "quantity": order["quantity"],
           "filled_quantity": 0, "entry_reference": order["entry_reference"],
           "trigger_at": order["trigger_at"],
           "stop": order["stop"], "target": order["target"],
           "flat_by_ist": order["flat_by_ist"],
           "planned_loss_inr": order["planned_loss_inr"],
           "planned_notional_inr": order["planned_notional_inr"],
           "max_loss_inr": order["max_loss_inr"],
           "fee_buffer_inr": order["fee_buffer_inr"],
           "slippage_bps_per_side": order["slippage_bps_per_side"],
           "approved_at": stamp.isoformat(), "approval_reply": reply,
           "fills": [], "orders_possible": False}
    result["orders"].append(row)
    row["approval_digest"] = _approval_digest(row)
    result["next_id"] += 1
    _event(result, "APPROVED", stamp, order_id=row["id"], fingerprint=row["fingerprint"])
    return result


def decline(ledger: dict, order: dict, reply: str, at: str | datetime | None = None) -> dict:
    if not isinstance(reply, str) or reply.strip().lower() not in ("no", "deny", "decline"):
        raise ValueError("Decline requires an explicit NO")
    if order.get("mode") != "PAPER_ONLY" or order.get("status") != "PROPOSED" or not order.get("fingerprint"):
        raise ValueError("Decline requires a paper proposal")
    result = copy.deepcopy(ledger)
    _event(result, "DECLINED", _now(at), ticker=order.get("ticker"),
           fingerprint=order["fingerprint"], reply=reply)
    return result


def propose_flatten(ledger: dict, ticker: str, at: str | datetime | None = None,
                    quantity: int | None = None) -> dict:
    """Create an exact sell-to-close ticket from shares already held locally."""
    stamp = _now(at)
    exchange = "BSE" if str(ticker).upper().endswith(".BO") else "NSE"
    ticker = symbol(ticker, exchange)
    position = ledger["positions"].get(ticker)
    if not position:
        raise ValueError("No paper shares to flatten; reconcile external holdings separately")
    qty = position["quantity"] if quantity is None else quantity
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1 or qty > position["quantity"]:
        raise ValueError("Flatten quantity exceeds the open paper position")
    if any(o["ticker"] == ticker and o["side"] == "SELL_TO_CLOSE" and o["status"] in
           ("EXIT_PROPOSED", "EXIT_APPROVED", "SANDBOX_SEND_ATTEMPTED", "SANDBOX_ACCEPTED",
            "SANDBOX_UNKNOWN", "SANDBOX_CANCEL_ATTEMPTED", "SANDBOX_CANCEL_ACK",
            "SANDBOX_CANCEL_UNKNOWN")
           for o in ledger["orders"]):
        raise ValueError("A sell-to-close ticket is already pending for this ticker")
    result = copy.deepcopy(ledger)
    ticket = {"id": result["next_id"], "mode": "PAPER_ONLY", "status": "EXIT_PROPOSED",
              "ticker": ticker, "side": "SELL_TO_CLOSE", "quantity": qty,
              "proposed_at": stamp.isoformat(), "orders_possible": False}
    ticket["fingerprint"] = hashlib.sha256(json.dumps(ticket, sort_keys=True).encode()).hexdigest()
    result["orders"].append(ticket)
    result["next_id"] += 1
    _event(result, "EXIT_PROPOSED", stamp, order_id=ticket["id"], ticker=ticker,
           quantity=qty, fingerprint=ticket["fingerprint"])
    return result


def approve_flatten(ledger: dict, order_id: int, at: str | datetime | None = None,
                    reply: str = "YES") -> dict:
    stamp = _now(at)
    result = copy.deepcopy(ledger)
    order = _order(result, order_id)
    if reply != "YES" or order.get("status") != "EXIT_PROPOSED":
        raise ValueError("This exact exit ticket needs a YES")
    if not 0 <= (stamp - as_datetime(order["proposed_at"])).total_seconds() <= 300:
        raise ValueError("Sell-to-close ticket is stale; propose again")
    held = result["positions"].get(order["ticker"], {}).get("quantity", 0)
    if held < order["quantity"]:
        raise ValueError("Exit quantity exceeds current paper shares")
    original = {k: v for k, v in order.items() if k != "fingerprint"}
    if hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest() != order["fingerprint"]:
        raise ValueError("Sell-to-close ticket changed")
    order["status"] = "EXIT_APPROVED"
    order["approved_at"] = stamp.isoformat()
    order["approval_reply"] = reply
    order["approval_digest"] = _approval_digest(order)
    _event(result, "EXIT_APPROVED", stamp, order_id=order_id,
           fingerprint=order["fingerprint"], reply=reply)
    return result


def begin_sandbox_send(ledger: dict, order_id: int, instrument_key: str,
                       at: str | datetime | None = None) -> dict:
    """Persist an attempt marker BEFORE network I/O; never retry an unknown outcome."""
    from .broker_sandbox import order_payload
    stamp = _now(at)
    result = copy.deepcopy(ledger)
    order = _order(result, order_id)
    order_payload(order, instrument_key)
    _check_approval_digest(order)
    if not 0 <= (stamp - as_datetime(order["approved_at"])).total_seconds() <= 300:
        raise ValueError("Sandbox handoff needs a fresh same-day approval")
    if order["side"] == "BUY_LONG_CASH":
        if order["ticker"] in result["positions"]:
            raise ValueError("Paper position already open")
        if not 0 <= (stamp - as_datetime(order["trigger_at"])).total_seconds() <= 300:
            raise ValueError("Entry signal stale for sandbox handoff")
        if stamp.weekday() >= 5 or not time(9, 30) <= stamp.time() <= time.fromisoformat(order["flat_by_ist"]):
            raise ValueError("Outside approved entry window")
        if order["ticker"].endswith(".NS") and not regular_nse_entry_day(stamp.date()):
            raise ValueError("NSE regular session is closed or the calendar year is unverified")
        if order["ticker"].endswith(".BO") and not regular_nse_entry_day(stamp.date()):
            raise ValueError("BSE sandbox entry blocked by NSE closure or an unverified calendar year")
    elif result["positions"].get(order["ticker"], {}).get("quantity", 0) < order["quantity"]:
        raise ValueError("Sell-to-close exceeds current paper position")
    order["status"] = "SANDBOX_SEND_ATTEMPTED"
    order["sandbox_attempted_at"] = stamp.isoformat()
    order["sandbox_instrument_key"] = instrument_key
    _event(result, "SANDBOX_SEND_ATTEMPTED", stamp, order_id=order_id,
           fingerprint=order["fingerprint"])
    return result


def record_sandbox_result(ledger: dict, order_id: int, broker_id: str | None,
                          at: str | datetime | None = None) -> dict:
    result = copy.deepcopy(ledger)
    order = _order(result, order_id)
    if order["status"] != "SANDBOX_SEND_ATTEMPTED":
        raise ValueError("No pending sandbox attempt for this order")
    if broker_id is not None and (not isinstance(broker_id, str) or
                                  not re.fullmatch(r"[A-Za-z0-9-]{1,80}", broker_id)):
        raise ValueError("Invalid sandbox order ID")
    order["status"] = "SANDBOX_ACCEPTED" if broker_id else "SANDBOX_UNKNOWN"
    if broker_id:
        order["sandbox_order_id"] = broker_id
    _event(result, order["status"], _now(at), order_id=order_id, sandbox_order_id=broker_id)
    return result


def begin_sandbox_cancel(ledger: dict, order_id: int, at: str | datetime | None = None) -> dict:
    result = copy.deepcopy(ledger)
    order = _order(result, order_id)
    if order["status"] not in ("SANDBOX_ACCEPTED", "SANDBOX_PARTIALLY_FILLED") or not order.get("sandbox_order_id"):
        raise ValueError("Only a confirmed sandbox order can be canceled remotely")
    order["status"] = "SANDBOX_CANCEL_ATTEMPTED"
    _event(result, "SANDBOX_CANCEL_ATTEMPTED", _now(at), order_id=order_id)
    return result


def record_sandbox_cancel(ledger: dict, order_id: int, acknowledged: bool,
                          at: str | datetime | None = None) -> dict:
    result = copy.deepcopy(ledger)
    order = _order(result, order_id)
    if order["status"] != "SANDBOX_CANCEL_ATTEMPTED":
        raise ValueError("No sandbox cancellation in progress")
    order["status"] = "SANDBOX_CANCEL_ACK" if acknowledged else "SANDBOX_CANCEL_UNKNOWN"
    _event(result, order["status"], _now(at), order_id=order_id)
    return result


def deny_sandbox_permission(ledger: dict, order_id: int,
                            at: str | datetime | None = None) -> dict:
    result = copy.deepcopy(ledger)
    order = _order(result, order_id)
    if order["status"] not in ("APPROVED_UNFILLED", "EXIT_APPROVED"):
        raise ValueError("No approved paper ticket awaiting sandbox permission")
    order["status"] = "SANDBOX_DENIED"
    _event(result, "SANDBOX_DENIED", _now(at), order_id=order_id)
    return result


def reconcile_sandbox(ledger: dict, order_id: int, outcome: str,
                      broker_id: str | None = None,
                      at: str | datetime | None = None) -> dict:
    """Record a user's independently checked sandbox orderbook outcome."""
    if outcome not in ("accepted", "canceled", "rejected"):
        raise ValueError("Reconcile outcome must be accepted, canceled or rejected")
    result = copy.deepcopy(ledger)
    order = _order(result, order_id)
    if order["status"] not in ("SANDBOX_SEND_ATTEMPTED", "SANDBOX_UNKNOWN",
                               "SANDBOX_CANCEL_ATTEMPTED", "SANDBOX_CANCEL_UNKNOWN",
                               "SANDBOX_CANCEL_ACK"):
        raise ValueError("Only an attempted, unknown or acknowledged sandbox outcome can be reconciled")
    if outcome == "accepted":
        ident = broker_id or order.get("sandbox_order_id")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", ident):
            raise ValueError("Accepted sandbox order requires its verified order ID")
        order["sandbox_order_id"] = ident
        order["status"] = "SANDBOX_ACCEPTED"
    else:
        if order["status"] == "SANDBOX_CANCEL_ACK" and outcome == "rejected":
            raise ValueError("Cancellation acknowledgement requires orderbook verification")
        if order.get("filled_quantity", 0):
            if outcome != "canceled" or order["side"] != "BUY_LONG_CASH":
                raise ValueError("Cannot reject an order with recorded fills")
            order["status"] = "OPEN"
            order["quantity"] = order["filled_quantity"]
        else:
            order["status"] = "SANDBOX_CANCELED" if outcome == "canceled" else "SANDBOX_REJECTED"
    _event(result, "SANDBOX_RECONCILED", _now(at), order_id=order_id,
           outcome=outcome, source="USER_VERIFIED_SANDBOX_ORDERBOOK")
    return result


def ticket_view(ledger: dict) -> dict:
    """Plain-language review of each paper ticket and its current state."""
    tickets = []
    for row in ledger["orders"]:
        if row["side"] == "BUY_LONG_CASH":
            plain = (f"BUY {row['quantity']} {row['ticker']} paper shares; "
                     f"reference INR {row['entry_reference']}, stop INR {row['stop']}, "
                     f"target INR {row['target']}; planned loss INR {row['planned_loss_inr']}")
        else:
            plain = f"SELL {row['quantity']} held {row['ticker']} paper shares to close"
        tickets.append({"id": row["id"], "status": row["status"],
                        "plain": plain, "fingerprint": row["fingerprint"],
                        "sandbox_order_id": row.get("sandbox_order_id")})
    return {"mode": "PAPER_ONLY", "cash_inr": ledger["cash_inr"],
            "positions": ledger["positions"], "tickets": tickets,
            "orders_possible": False,
            "note": "Sandbox order ID is not a fill; manually entered prices are simulations"}


def cancel(ledger: dict, order_id: int, at: str | datetime | None = None) -> dict:
    result = copy.deepcopy(ledger)
    order = _order(result, order_id)
    if order["status"] not in ("APPROVED_UNFILLED", "PARTIALLY_FILLED"):
        raise ValueError("Only an unfilled paper remainder can be canceled")
    order["status"] = "CANCELED" if not order["filled_quantity"] else "OPEN"
    order["quantity"] = order["filled_quantity"]
    _event(result, "CANCELED_REMAINDER", _now(at), order_id=order_id)
    return result


def fill_entry(ledger: dict, order_id: int, quantity: int, price: str, at: str | datetime | None = None) -> dict:
    """Record manually supplied simulated fill, never infer market execution."""
    stamp = _now(at)
    amount = decimal(price, "simulated fill price")
    if amount <= 0 or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ValueError("Simulated fill needs a positive price and whole-share quantity")
    result = copy.deepcopy(ledger)
    order = _order(result, order_id)
    if order["status"] not in ("APPROVED_UNFILLED", "PARTIALLY_FILLED", "SANDBOX_ACCEPTED", "SANDBOX_PARTIALLY_FILLED") or quantity > order["quantity"] - order["filled_quantity"]:
        raise ValueError("Paper fill exceeds the approved unfilled quantity")
    _check_approval_digest(order)
    if as_datetime(order["approved_at"]).date() != stamp.date() or stamp < as_datetime(order["approved_at"]):
        raise ValueError("Paper entry fill must follow same-day approval")
    if stamp.time() > time.fromisoformat(order["flat_by_ist"]):
        raise ValueError("Paper entry fill is after the approved flat-by time")
    cost = amount * quantity
    cash = decimal(result["cash_inr"], "paper cash")
    other_reserved = sum((decimal(o["planned_notional_inr"], "reserved notional") *
                          Decimal(o["quantity"] - o["filled_quantity"]) / Decimal(o["quantity"])
                         for o in result["orders"] if o["id"] != order_id and
                         o["status"] in ("APPROVED_UNFILLED", "PARTIALLY_FILLED", "SANDBOX_PARTIALLY_FILLED", "SANDBOX_SEND_ATTEMPTED", "SANDBOX_ACCEPTED", "SANDBOX_UNKNOWN", "SANDBOX_CANCEL_ATTEMPTED", "SANDBOX_CANCEL_ACK", "SANDBOX_CANCEL_UNKNOWN") and o["side"] == "BUY_LONG_CASH"), Decimal(0))
    if cost > cash - other_reserved:
        raise ValueError("Insufficient paper cash for simulated fill")
    prior_cost = sum(decimal(fill["price_inr"], "prior fill") * fill["quantity"] for fill in order["fills"])
    filled_qty = order["filled_quantity"] + quantity
    total_cost = prior_cost + cost
    stop = decimal(order["stop"], "stop")
    stop_fill = stop * (1 - decimal(order["slippage_bps_per_side"], "slippage") / 10000)
    projected_loss = total_cost - stop_fill * filled_qty + decimal(order["fee_buffer_inr"], "fees")
    allowed_unit = decimal(order["planned_notional_inr"], "notional") / order["quantity"]
    if amount <= stop or amount > allowed_unit or projected_loss > decimal(order["max_loss_inr"], "max loss"):
        raise ValueError("Simulated fill exceeds the approved notional or loss limits")
    ticker = order["ticker"]
    position = result["positions"].get(ticker, {"quantity": 0, "cost_inr": "0"})
    position["quantity"] += quantity
    position["cost_inr"] = str(decimal(position["cost_inr"], "position cost") + cost)
    result["positions"][ticker] = position
    result["cash_inr"] = str(cash - cost)
    order["filled_quantity"] += quantity
    order["status"] = ("OPEN" if order["filled_quantity"] == order["quantity"] else
                       "SANDBOX_PARTIALLY_FILLED" if order.get("sandbox_order_id") else "PARTIALLY_FILLED")
    fill = {"at": stamp.isoformat(), "quantity": quantity, "price_inr": str(amount),
            "kind": "SIMULATED_USER_INPUT"}
    order["fills"].append(fill)
    _event(result, "ENTRY_FILL", stamp, order_id=order_id, ticker=ticker,
           quantity=quantity, price_inr=str(amount), kind="SIMULATED_USER_INPUT")
    return result


def close_position(ledger: dict, ticker: str, quantity: int, price: str,
                   at: str | datetime | None = None, reason: str = "MANUAL",
                   exit_id: int | None = None) -> dict:
    """Record a manual simulated sell; the price is supplied by the user."""
    stamp = _now(at)
    exchange = "BSE" if str(ticker).upper().endswith(".BO") else "NSE"
    ticker = symbol(ticker, exchange)
    amount = decimal(price, "simulated exit price")
    if amount <= 0 or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ValueError("Simulated exit needs a positive price and whole-share quantity")
    if reason not in ("MANUAL", "STOP", "TARGET", "FLAT_BY"):
        raise ValueError("Invalid paper exit reason")
    result = copy.deepcopy(ledger)
    position = result["positions"].get(ticker)
    if not position or quantity > position["quantity"]:
        raise ValueError("Cannot sell more than the open paper shares")
    if any(o["ticker"] == ticker and o["status"] in
           ("SANDBOX_PARTIALLY_FILLED", "SANDBOX_CANCEL_ATTEMPTED", "SANDBOX_CANCEL_UNKNOWN", "SANDBOX_CANCEL_ACK")
           for o in result["orders"]):
        raise ValueError("Reconcile or cancel the outstanding sandbox entry before closing")
    if any(o["ticker"] == ticker and o["side"] == "BUY_LONG_CASH" and
           o["filled_quantity"] < o["quantity"] and
           o["status"] in ("PARTIALLY_FILLED", "SANDBOX_ACCEPTED", "SANDBOX_UNKNOWN")
           for o in result["orders"]):
        raise ValueError("Cancel or reconcile the outstanding entry remainder before closing")
    if any(o["ticker"] == ticker and o["side"] == "SELL_TO_CLOSE" and
           o["status"] in ("EXIT_APPROVED", "SANDBOX_SEND_ATTEMPTED", "SANDBOX_ACCEPTED",
                            "SANDBOX_UNKNOWN", "SANDBOX_CANCEL_ATTEMPTED",
                            "SANDBOX_CANCEL_ACK", "SANDBOX_CANCEL_UNKNOWN") and
           o["id"] != exit_id for o in result["orders"]):
        raise ValueError("An approved or outstanding exit ticket requires its exact exit ID or reconciliation")
    if exit_id is not None:
        exit_order = _order(result, exit_id)
        if (exit_order["status"] not in ("EXIT_APPROVED", "SANDBOX_ACCEPTED") or
            exit_order["ticker"] != ticker or exit_order["quantity"] != quantity or
            stamp < as_datetime(exit_order["approved_at"])):
            raise ValueError("Exact sell-to-close ticket has not been approved")
        _check_approval_digest(exit_order)
        exit_order["status"] = "EXIT_CLOSED"
    old_qty = position["quantity"]
    cost = decimal(position["cost_inr"], "position cost")
    allocated = cost if quantity == old_qty else cost * Decimal(quantity) / Decimal(old_qty)
    proceeds = amount * quantity
    position["quantity"] -= quantity
    position["cost_inr"] = str(cost - allocated)
    if not position["quantity"]:
        del result["positions"][ticker]
        for order in result["orders"]:
            if order["ticker"] == ticker and order["status"] in ("OPEN", "PARTIALLY_FILLED", "SANDBOX_PARTIALLY_FILLED"):
                order["status"] = "CLOSED"
                order["quantity"] = order["filled_quantity"]
    result["cash_inr"] = str(decimal(result["cash_inr"], "paper cash") + proceeds)
    _event(result, "EXIT_FILL", stamp, ticker=ticker, quantity=quantity,
           price_inr=str(amount), realized_pnl_inr=str(proceeds - allocated),
           reason=reason, exit_id=exit_id, kind="SIMULATED_USER_INPUT")
    return result
