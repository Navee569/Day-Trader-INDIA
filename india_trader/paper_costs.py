"""Explicit, configurable Indian cash-equity paper charge assumptions.

Rates are examples, not a broker contract note. Every component rounds to paise
per simulated fill so the ledger is deterministic and auditable.
"""
from __future__ import annotations

from decimal import Decimal

from .auto_paper_broker import dec, money


FIELDS = {"market", "brokerage_bps", "brokerage_cap_inr", "stt_buy_bps",
          "stt_sell_bps", "exchange_txn_bps", "sebi_turnover_bps", "gst_pct",
          "stamp_buy_bps", "dp_sell_inr", "other_buy_inr", "other_sell_inr"}


def validate_schedule(raw):
    if not isinstance(raw, dict) or set(raw) != FIELDS or \
            raw["market"] != "NSE_CASH_INTRADAY_ILLUSTRATIVE":
        raise ValueError("invalid paper cost schedule")
    for key in FIELDS - {"market"}:
        value = dec(raw[key])
        ceiling = 100 if key.endswith("_bps") or key == "gst_pct" else 1000
        if not 0 <= value <= ceiling:
            raise ValueError("invalid paper charge: " + key)
    return raw


def fill_charges(schedule, side, notional, prior_brokerage=0, first_fill=True):
    """Return paise-rounded component totals for a BUY or SELL paper fill."""
    if side not in ("BUY", "SELL"):
        raise ValueError("unsupported paper side")
    gross = money(notional)
    if gross <= 0:
        raise ValueError("paper notional must be positive")
    brokerage = min(money(gross * dec(schedule["brokerage_bps"]) / 10000),
                    max(Decimal(0), dec(schedule["brokerage_cap_inr"]) -
                        dec(prior_brokerage)))
    exchange = money(gross * dec(schedule["exchange_txn_bps"]) / 10000)
    sebi = money(gross * dec(schedule["sebi_turnover_bps"]) / 10000)
    stt_key = "stt_buy_bps" if side == "BUY" else "stt_sell_bps"
    stt = money(gross * dec(schedule[stt_key]) / 10000)
    stamp = money(gross * dec(schedule["stamp_buy_bps"]) / 10000) \
        if side == "BUY" else Decimal(0)
    dp = dec(schedule["dp_sell_inr"]) if side == "SELL" and first_fill else Decimal(0)
    other = dec(schedule["other_buy_inr"] if side == "BUY" else
                schedule["other_sell_inr"])
    gst = money((brokerage + exchange + sebi) * dec(schedule["gst_pct"]) / 100)
    parts = {"brokerage_inr": brokerage, "stt_inr": stt,
             "exchange_inr": exchange, "sebi_inr": sebi, "gst_inr": gst,
             "stamp_inr": stamp, "dp_inr": money(dp), "other_inr": money(other)}
    parts["total_inr"] = money(sum(parts.values(), Decimal(0)))
    return {key: float(value) for key, value in parts.items()}


def estimated_roundtrip(schedule, maximum_notional):
    return money(dec(fill_charges(schedule, "BUY", maximum_notional)["total_inr"]) +
                 dec(fill_charges(schedule, "SELL", maximum_notional)["total_inr"]))
