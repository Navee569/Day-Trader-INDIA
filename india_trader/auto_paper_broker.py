"""Local, paper-only order ledger used by the autonomous lifecycle.

All cash, orders, fills and positions live in one atomically replaced JSON state.
This module has no network client or live broker adapter.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP


CENT = Decimal("0.01")


def dec(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite money value")
    return result


def money(value):
    return dec(value).quantize(CENT, rounding=ROUND_HALF_UP)


def whole(value):
    return int(dec(value).to_integral_value(rounding=ROUND_FLOOR))


class LocalPaperBroker:
    """Deterministic simulated fills, never an exchange or brokerage connection."""

    def __init__(self, state, settings):
        self.state, self.settings = state, settings

    def _event(self, at, order, kind, **fields):
        row = {"at": at.isoformat(), "order_id": order["id"],
               "ticker": order["ticker"], "kind": kind, "paper_only": True,
               **fields}
        self.state["events"].append(row)
        return row

    def position(self, ticker):
        return next((p for p in self.state["positions"] if p["ticker"] == ticker), None)

    def pending(self, ticker=None, side=None):
        return [o for o in self.state["orders"] if o["status"] in ("PENDING", "PARTIAL")
                and (ticker is None or o["ticker"] == ticker)
                and (side is None or o["side"] == side)]

    def available_cash(self):
        reserved = sum((dec(o["reserved_inr"]) for o in self.pending(side="BUY")), Decimal(0))
        return money(dec(self.state["cash_inr"]) - reserved)

    def submit(self, order, now):
        if order["id"] in {o["id"] for o in self.state["orders"]}:
            raise ValueError("duplicate paper order ID")
        if order["side"] not in ("BUY", "SELL") or order["quantity"] < 1:
            raise ValueError("unsupported paper order")
        if order["side"] == "BUY" and dec(order["reserved_inr"]) > self.available_cash():
            raise ValueError("insufficient available paper cash")
        if order["side"] == "SELL" and (not self.position(order["ticker"]) or
                                        self.position(order["ticker"])["quantity"] < order["quantity"]):
            raise ValueError("paper exit exceeds holding")
        order = {**order, "filled_quantity": 0, "status": "PENDING",
                 "submitted_at": now.isoformat(), "paper_only": True}
        self.state["orders"].append(order)
        self._event(now, order, "SUBMITTED", quantity=order["quantity"])
        return order

    def cancel(self, order, now, reason):
        if order["status"] not in ("PENDING", "PARTIAL"):
            return
        order["status"] = "CANCELLED"
        order["reserved_inr"] = 0.0
        self._event(now, order, "CANCELLED", remaining=order["quantity"] - order["filled_quantity"],
                    reason=reason)

    def reject(self, proposal, now, reasons, signal_id):
        """Persist a risk-rejected paper proposal with no cash reservation."""
        identifier = "REJECT|" + signal_id
        if identifier in {o["id"] for o in self.state["orders"]}:
            return None
        order = {"id": identifier, "ticker": proposal["ticker"],
                 "signal_id": signal_id, "side": "BUY", "quantity": proposal["quantity"],
                 "filled_quantity": 0, "reserved_inr": 0.0, "status": "REJECTED",
                 "submitted_at": now.isoformat(), "risk_fingerprint": proposal["fingerprint"],
                 "reasons": list(reasons), "paper_only": True}
        self.state["orders"].append(order)
        self._event(now, order, "REJECTED", reasons=list(reasons))
        return order

    def fill(self, order, now, quote, bar_volume, bar_start):
        if order["status"] not in ("PENDING", "PARTIAL"):
            raise ValueError("paper order is not pending")
        remaining = order["quantity"] - order["filled_quantity"]
        max_slice = whole(dec(bar_volume) * dec(self.settings["max_volume_participation_pct"]) / 100)
        used = sum(row["quantity"] for row in self.state["events"]
                   if row["kind"] == "SIMULATED_FILL" and row["ticker"] == order["ticker"]
                   and row.get("bar_start") == bar_start)
        qty = min(remaining, max(0, max_slice - used))
        if qty < 1:
            return None
        reference = dec(quote["price"])
        slip = dec(self.settings["slippage_bps_per_side"]) / 10000
        px = money(reference * (1 + slip if order["side"] == "BUY" else 1 - slip))
        if px <= 0:
            raise ValueError("invalid simulated fill price")
        if order["side"] == "BUY" and px > dec(order["max_fill_price_inr"]):
            self.cancel(order, now, "ENTRY_PRICE_CAP_EXCEEDED")
            return None
        gross = money(px * qty)
        if self.settings.get("cost_schedule"):
            from .paper_costs import fill_charges
            prior_brokerage = sum(dec(row.get("charges", {}).get("brokerage_inr", 0))
                                  for row in self.state["events"]
                                  if row["kind"] == "SIMULATED_FILL" and
                                  row["order_id"] == order["id"])
            charges = fill_charges(self.settings["cost_schedule"], order["side"], gross,
                                   prior_brokerage, order["filled_quantity"] == 0)
            fee = dec(charges["total_inr"])
        else:
            fee = money(gross * dec(self.settings["modeled_cost_bps_per_side"]) / 10000 +
                        dec(self.settings["fixed_fee_inr_per_fill"]))
            charges = {"other_inr": float(fee), "total_inr": float(fee)}
        if order["side"] == "BUY":
            position = self.position(order["ticker"])
            if position is not None and position["signal_id"] != order["signal_id"]:
                self.cancel(order, now, "DUPLICATE_POSITION")
                return None
            other_reservations = sum((dec(o["reserved_inr"]) for o in self.pending(side="BUY")
                                      if o["id"] != order["id"]), Decimal(0))
            reserve_per_share = dec(order["reserve_per_share_inr"])
            remaining_reservation = money(reserve_per_share * (remaining - qty))
            if gross + fee + other_reservations + remaining_reservation > \
                    dec(self.state["cash_inr"]):
                self.cancel(order, now, "PAPER_CASH_CHANGED")
                return None
            stop, target = dec(order["stop_inr"]), dec(order["target_inr"])
            if not stop < px < target:
                self.cancel(order, now, "ENTRY_BRACKET_INVALIDATED")
                return None
            risk_increment = money(qty * (px - stop) + fee)
            cumulative_risk = dec(order.get("filled_risk_inr", 0)) + risk_increment
            if cumulative_risk > dec(order["risk_cap_inr"]):
                self.cancel(order, now, "ENTRY_RISK_INCREASED")
                return None
            self.state["cash_inr"] = float(money(dec(self.state["cash_inr"]) - gross - fee))
            if position is None:
                position = {"ticker": order["ticker"], "instrument_id": order["instrument_id"],
                            "signal_id": order["signal_id"], "quantity": 0,
                            "average_cost_inr": 0.0, "cost_basis_inr": 0.0,
                            "entry_fees_inr": 0.0,
                            "stop_inr": float(stop), "target_inr": float(target),
                            "trailing_stop_inr": float(stop), "high_water_inr": float(px),
                            "opened_at": now.isoformat(), "flat_by_ist": self.settings["flat_by_ist"]}
                self.state["positions"].append(position)
            previous = position["quantity"]
            position["cost_basis_inr"] = float(money(
                dec(position.get("cost_basis_inr", dec(position["average_cost_inr"]) * previous)) +
                gross))
            position["average_cost_inr"] = float(money(
                dec(position["cost_basis_inr"]) / (previous + qty)))
            position["entry_fees_inr"] = float(money(dec(position["entry_fees_inr"]) + fee))
            position["quantity"] += qty
            order["filled_risk_inr"] = float(cumulative_risk)
            order["reserved_inr"] = float(remaining_reservation)
        else:
            position = self.position(order["ticker"])
            if position is None or position["quantity"] < qty:
                self.cancel(order, now, "HOLDING_UNAVAILABLE")
                return None
            basis_total = dec(position.get("cost_basis_inr",
                                           dec(position["average_cost_inr"]) *
                                           position["quantity"]))
            allocated_basis = money(basis_total * qty / position["quantity"])
            basis = money(allocated_basis / qty)
            allocated_entry_fee = money(dec(position["entry_fees_inr"]) * qty /
                                        position["quantity"])
            pnl = money(gross - allocated_basis - allocated_entry_fee - fee)
            position["entry_fees_inr"] = float(money(dec(position["entry_fees_inr"]) -
                                                       allocated_entry_fee))
            position["quantity"] -= qty
            position["cost_basis_inr"] = float(money(basis_total - allocated_basis))
            if position["quantity"]:
                position["average_cost_inr"] = float(money(
                    dec(position["cost_basis_inr"]) / position["quantity"]))
            self.state["cash_inr"] = float(money(dec(self.state["cash_inr"]) + gross - fee))
            self.state["realized_pnl_inr"] = float(money(
                dec(self.state["realized_pnl_inr"]) + pnl))
            self.state["trades"].append({"ticker": order["ticker"], "signal_id":
                                         position["signal_id"], "quantity": qty,
                                         "entry_price_inr": float(basis),
                                         "entry_cost_basis_inr": float(allocated_basis),
                                         "exit_price_inr": float(px), "entry_fee_inr":
                                         float(allocated_entry_fee), "exit_fee_inr": float(fee),
                                         "net_pnl_inr": float(pnl), "reason": order["reason"],
                                         "closed_at": now.isoformat(), "paper_only": True})
            if position["quantity"] == 0:
                self.state["positions"].remove(position)
        order["filled_quantity"] += qty
        order["status"] = "FILLED" if order["filled_quantity"] == order["quantity"] else "PARTIAL"
        self._event(now, order, "SIMULATED_FILL", quantity=qty, fill_price_inr=float(px),
                    reference_quote_inr=float(reference), fee_inr=float(fee),
                    notional_inr=float(gross), charges=charges,
                    slippage_inr=float(money(abs(px - reference) * qty)),
                    bar_start=bar_start,
                    max_loss_inr=float(risk_increment) if order["side"] == "BUY" else 0.0,
                    quote_provider=quote["provider"], quote_at=quote["time"],
                    remaining=order["quantity"] - order["filled_quantity"])
        return {"order_id": order["id"], "ticker": order["ticker"], "side": order["side"],
                "quantity": qty, "price_inr": float(px), "fee_inr": float(fee),
                "charges": charges,
                "status": order["status"], "confirmed": True, "paper_only": True}
