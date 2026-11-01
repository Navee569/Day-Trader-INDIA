"""Stage 5 autonomous, local paper-entry and independent exit monitoring.

This foreground controller has no live order path. It consumes Stage 4 decisions,
rechecks Stage 3 signals and broker market data, then uses the independent risk
manager and a local simulated broker. Exits never call AI.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import sys
import time as clock_time

from .ai_committee import _signal, CommitteeError
from .auto_paper_broker import LocalPaperBroker, dec, money, whole
from .paper_costs import estimated_roundtrip, fill_charges, validate_schedule
from .paper_execution import PaperExecution
from .paper_accounting import AccountingError, reconcile, report as ledger_report
from .core import IST
from .market_data_manager import FeedUnavailable, MarketDataManager
from .market_scanner import trading_day
from .paper_broker import flatten_ticket
from .paper_orders import _fingerprint
from .risk_manager import limits, review as risk_review, RiskError


DEFAULT_CONFIG = Path("examples/paper_lifecycle_config.json")
USER_CONFIG = Path("trader/settings/paper_lifecycle.json")
DEFAULT_STATE = Path("trader/state/paper-lifecycle.json")
DEFAULT_ENGINE = Path("trader/state/market-engine.json")
DEFAULT_COMMITTEE = Path("trader/state/ai-committee.json")


class LifecycleError(ValueError):
    pass


def _stamp(value):
    try:
        at = datetime.fromisoformat(value)
        if at.tzinfo is None:
            raise ValueError("timezone required")
        return at.astimezone(IST)
    except (TypeError, ValueError) as exc:
        raise LifecycleError("invalid timestamp") from exc


def _read(path, label, maximum=5_000_000):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > maximum:
        raise LifecycleError(label + " missing or too large")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise LifecycleError(label + " invalid JSON") from exc
    if not isinstance(doc, dict):
        raise LifecycleError(label + " must be an object")
    return doc


def load_config(path=None):
    if path is None:
        path = USER_CONFIG if USER_CONFIG.is_file() else DEFAULT_CONFIG
    cfg = _read(path, "lifecycle config", 64000)
    wanted = {"mode", "initial_capital_inr", "max_open_positions",
              "max_portfolio_exposure_pct", "max_daily_realized_loss_inr",
              "max_spread_pct", "max_quote_age_seconds", "max_entry_move_pct",
              "max_volume_participation_pct", "slippage_bps_per_side",
              "modeled_cost_bps_per_side", "fixed_fee_inr_per_fill", "risk_fee_buffer_inr",
              "cost_schedule", "reward_risk", "trailing_pct", "flat_by_ist", "poll_seconds",
              "risk_limits"}
    if set(cfg) != wanted or cfg["mode"] != "PAPER_ONLY":
        raise LifecycleError("paper-only lifecycle config schema required")
    money_fields = ("initial_capital_inr", "max_daily_realized_loss_inr",
                    "fixed_fee_inr_per_fill", "risk_fee_buffer_inr")
    for key in money_fields:
        value = dec(cfg[key])
        if value < 0 or key == "initial_capital_inr" and value <= 0:
            raise LifecycleError("invalid " + key)
    for key, lo, hi in (("max_portfolio_exposure_pct", 1, 100),
                        ("max_spread_pct", 0.01, 1), ("max_entry_move_pct", 0, 1),
                        ("max_volume_participation_pct", 0.01, 25),
                        ("slippage_bps_per_side", 0, 100),
                        ("modeled_cost_bps_per_side", 0, 100),
                        ("reward_risk", 1, 5), ("trailing_pct", 0, 10)):
        value = dec(cfg[key])
        if not lo <= value <= hi:
            raise LifecycleError("invalid " + key)
    for key, lo, hi in (("max_open_positions", 1, 20),
                        ("max_quote_age_seconds", 1, 30), ("poll_seconds", 5, 300)):
        if type(cfg[key]) is not int or not lo <= cfg[key] <= hi:
            raise LifecycleError("invalid " + key)
    try:
        hour, minute = (int(x) for x in cfg["flat_by_ist"].split(":"))
        flat = time(hour, minute)
    except (TypeError, ValueError) as exc:
        raise LifecycleError("flat_by_ist must be HH:MM") from exc
    if not time(9, 30) <= flat <= time(15, 10):
        raise LifecycleError("flat_by_ist outside conservative NSE session")
    if not isinstance(cfg["risk_limits"], dict):
        raise LifecycleError("risk_limits must be an object")
    risk = limits({**cfg["risk_limits"], "paper": True,
                   "budget_inr": cfg["initial_capital_inr"],
                   "slippage_bps_per_side": cfg["slippage_bps_per_side"],
                   "fee_buffer_inr": cfg["risk_fee_buffer_inr"]})
    validate_schedule(cfg["cost_schedule"])
    if dec(cfg["risk_fee_buffer_inr"]) < estimated_roundtrip(
            cfg["cost_schedule"], risk["max_order_inr"]):
        raise LifecycleError("risk fee buffer must cover an estimated round trip")
    cfg["_risk"] = risk
    return cfg


def _blank(cfg):
    return {"version": 1, "mode": "PAPER_ONLY", "initial_capital_inr":
            float(money(cfg["initial_capital_inr"])), "cash_inr":
            float(money(cfg["initial_capital_inr"])), "realized_pnl_inr": 0.0,
            "positions": [], "orders": [], "events": [], "trades": [], "last_marks": {},
            "seen_signals": [], "last_cycle": None, "orders_possible": False}


def _state(path, cfg):
    if not Path(path).is_file():
        return _blank(cfg)
    doc = _read(path, "paper lifecycle state")
    if doc.get("version") != 1 or doc.get("mode") != "PAPER_ONLY" or \
            doc.get("orders_possible") is not False or \
            dec(doc.get("initial_capital_inr", -1)) != money(cfg["initial_capital_inr"]) or \
            not isinstance(doc.get("last_marks"), dict) or \
            any(not isinstance(doc.get(key), list) for key in
                ("positions", "orders", "events", "trades", "seen_signals")):
        raise LifecycleError("invalid paper state or changed starting capital")
    if dec(doc.get("cash_inr", -1)) < 0:
        raise LifecycleError("invalid paper cash")
    for position in doc["positions"]:
        if "cost_basis_inr" not in position:
            ticker = position["ticker"]
            bought = sum((money(e.get("notional_inr",
                                      dec(e["fill_price_inr"]) * e["quantity"]))
                          for e in doc["events"] if e.get("kind") == "SIMULATED_FILL" and
                          e.get("ticker") == ticker and any(
                              o["id"] == e["order_id"] and o["side"] == "BUY"
                              for o in doc["orders"])), Decimal(0))
            allocated = sum((dec(t.get("entry_cost_basis_inr",
                                       dec(t["entry_price_inr"]) * t["quantity"]))
                             for t in doc["trades"] if t.get("ticker") == ticker), Decimal(0))
            position["cost_basis_inr"] = float(money(bought - allocated))
    reconcile(doc)
    return doc


def _save(path, doc):
    reconcile(doc)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(doc, indent=2, sort_keys=True, allow_nan=False) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


@contextmanager
def _state_lock(path):
    """Serialize independent foreground workers across Windows and Linux."""
    target = Path(str(path) + ".lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LifecycleError("another paper lifecycle cycle holds the state lock") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _prices(quote):
    return {"prices": {quote["ticker"]: quote["price"]},
            "detail": {quote["ticker"]: {"time": quote["time"]}}}


def _risk_state(state, now):
    fills = []
    fingerprints = {order["id"]: order.get("risk_fingerprint") for order in state["orders"]}
    for event in state["events"]:
        if event["kind"] != "SIMULATED_FILL":
            continue
        side = "BUY_LONG_CASH" if next(o for o in state["orders"] if o["id"] == event["order_id"])["side"] == "BUY" else "SELL_LONG_CASH"
        fills.append({"date": event["at"][:10], "status": "simulated_fill",
                      "kind": "entry" if side == "BUY_LONG_CASH" else "exit",
                      "side": side, "ticker": event["ticker"],
                      "fingerprint": fingerprints[event["order_id"]],
                      "max_loss_inr": event.get("max_loss_inr", 0)})
    return {"date": now.date().isoformat(), "orders": fills}


def _verified_snapshot(manager, ticker, now, max_quote_age=30, need_five=False):
    manager.now = now
    one = manager.fetch_candles(ticker, "1d", interval=1,
                                require_entry_ready=True, allow_yahoo=False)
    five = (manager.fetch_candles(ticker, "1d", interval=5,
                                  require_entry_ready=True, allow_yahoo=False)
            if need_five else None)
    quote = manager.fetch_quote(ticker, require_fresh=True,
                                max_age_seconds=max_quote_age)
    if not one.get("entry_ready") or five is not None and not five.get("entry_ready") or \
            not quote.get("entry_ready") or quote.get("ticker") != ticker or \
            quote.get("provider") not in ("UPSTOX", "FYERS") or \
            one.get("provider") not in ("UPSTOX", "FYERS") or \
            five is not None and five.get("provider") not in ("UPSTOX", "FYERS"):
        raise LifecycleError("broker snapshot not entry ready")
    last = one["bars"][-1]
    at = _stamp(last["timestamp"])
    if at.date() != now.date() or not 0 <= (now - at - timedelta(minutes=1)).total_seconds() <= 120 or \
            last["volume"] <= 0:
        raise LifecycleError("one-minute bar stale or empty")
    if five is not None:
        latest_five = five["bars"][-1]
        five_at = _stamp(latest_five["timestamp"])
        if five_at.date() != now.date() or \
                not 0 <= (now - five_at - timedelta(minutes=5)).total_seconds() <= 120 or \
                latest_five["volume"] <= 0:
            raise LifecycleError("five-minute bar stale or empty")
    qt = _stamp(quote["time"])
    if qt.date() != now.date() or not 0 <= (now - qt).total_seconds() <= max_quote_age:
        raise LifecycleError("quote stale")
    bid, ask = quote.get("bid"), quote.get("ask")
    if type(bid) not in (int, float) or type(ask) not in (int, float) or \
            not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask < bid:
        raise LifecycleError("bid/ask depth unavailable")
    spread = 100 * (ask - bid) / ((ask + bid) / 2)
    return {"one": one, "five": five, "quote": quote, "bar": last,
            "bar_start": at.isoformat(), "spread_pct": spread}


def _opening(five, now):
    today = [b for b in five["bars"] if _stamp(b["timestamp"]).date() == now.date()]
    if len(today) < 4 or [_stamp(b["timestamp"]).time() for b in today[:3]] != [
            time(9, 15), time(9, 20), time(9, 25)]:
        raise LifecycleError("opening range not complete")
    return max(b["high"] for b in today[:3]), min(b["low"] for b in today[:3])


class PaperLifecycle:
    def __init__(self, cfg, state_path=DEFAULT_STATE, manager=None, now_fn=None,
                 broker_factory=None):
        self.cfg, self.state_path = cfg, Path(state_path)
        self.manager = manager or MarketDataManager()
        self.now_fn = now_fn or (lambda: datetime.now(IST))
        self.broker_factory = broker_factory or LocalPaperBroker

    def _broker(self, state) -> PaperExecution:
        return self.broker_factory(state, self.cfg)

    def _gate(self, order, quote, now, state):
        broker = self._broker(state)
        portfolio = {"cash_inr": float(broker.available_cash()),
                     "positions": state["positions"]}
        result = risk_review([order], self.cfg["_risk"], now,
                             portfolio=portfolio, prices=_prices(quote),
                             state=_risk_state(state, now))
        return result["verdicts"][0]

    def _exit_one(self, state, position, now, report):
        broker = self._broker(state)
        ticker = position["ticker"]
        try:
            snap = _verified_snapshot(self.manager, ticker, now,
                                      self.cfg["max_quote_age_seconds"])
            quote, bar = snap["quote"], snap["bar"]
            if snap["spread_pct"] > dec(self.cfg["max_spread_pct"]):
                raise LifecycleError("exit spread wider than configured maximum")
        except (FeedUnavailable, LifecycleError, ValueError, TypeError,
                KeyError, IndexError, OSError) as exc:
            report["exit_data_unavailable"].append({"ticker": ticker, "reason": type(exc).__name__})
            return
        high = dec(position["high_water_inr"])
        if _stamp(bar["timestamp"]) >= _stamp(position["opened_at"]):
            high = max(high, dec(bar["high"]))
        state["last_marks"][ticker] = {"price_inr": quote["price"],
                                       "at": quote["time"], "provider": quote["provider"]}
        position["high_water_inr"] = float(money(high))
        trail = money(high * (1 - dec(self.cfg["trailing_pct"]) / 100))
        position["trailing_stop_inr"] = float(max(dec(position["trailing_stop_inr"]),
                                                  dec(position["stop_inr"]), trail))
        pending = broker.pending(ticker, "SELL")
        if pending:
            order = pending[0]
        else:
            price = dec(quote["price"])
            stop = dec(position["trailing_stop_inr"])
            reason = ("STOP_OR_TRAIL" if price <= stop else
                      "TARGET" if price >= dec(position["target_inr"]) else
                      "TIME_FLAT" if now.time() >= time.fromisoformat(self.cfg["flat_by_ist"])
                      else None)
            if reason is None:
                return
            proposal = flatten_ticket(ticker, None,
                                      {"positions": state["positions"]}, now)
            verdict = self._gate(proposal, quote, now, state)
            if verdict["status"] != "PASS":
                report["exit_risk_blocked"].append({"ticker": ticker,
                                                    "reasons": verdict["reasons"]})
                return
            order = broker.submit({"id": f"EXIT|{position['signal_id']}|{now.isoformat()}",
                                   "ticker": ticker, "instrument_id": position["instrument_id"],
                                   "signal_id": position["signal_id"], "side": "SELL",
                                   "quantity": position["quantity"], "reserved_inr": 0.0,
                                   "reason": reason, "risk_fingerprint": proposal["fingerprint"]}, now)
            _save(self.state_path, state)
        filled = broker.fill(order, now, quote, bar["volume"], snap["bar_start"])
        if filled:
            report["fills"].append(filled)
            _save(self.state_path, state)

    def _entry_one(self, state, decision, signal, now, report):
        broker = self._broker(state)
        ticker = decision["ticker"]
        try:
            snap = _verified_snapshot(self.manager, ticker, now,
                                      self.cfg["max_quote_age_seconds"], need_five=True)
            if snap["spread_pct"] > dec(self.cfg["max_spread_pct"]):
                raise LifecycleError("entry spread too wide")
            opening_high, opening_low = _opening(snap["five"], now)
            bar, quote = snap["bar"], snap["quote"]
            bar_end = _stamp(bar["timestamp"]) + timedelta(minutes=1)
            if bar_end <= _stamp(decision["decided_at"]) or \
                    bar_end <= _stamp(signal["trigger_at"]):
                raise LifecycleError("await next completed confirmation bar")
            if bar["close"] <= max(opening_high, signal["opening_range_high"]) or \
                    snap["five"]["bars"][-1]["close"] <= opening_high or \
                    dec(quote["price"]) <= opening_high or \
                    abs(dec(quote["price"]) / dec(bar["close"]) - 1) > Decimal("0.005"):
                raise LifecycleError("breakout not reconfirmed")
            if not trading_day(now.date())[0] or now.time() >= time.fromisoformat(self.cfg["flat_by_ist"]):
                raise LifecycleError("entry session closed")
            active_symbols = {p["ticker"] for p in state["positions"]} | \
                             {o["ticker"] for o in broker.pending(side="BUY")}
            if len(active_symbols) >= self.cfg["max_open_positions"]:
                raise LifecycleError("maximum open paper positions")
            pending_entries = [o for o in state["orders"] if o["side"] == "BUY"
                               and o["submitted_at"][:10] == now.date().isoformat()]
            if len(pending_entries) >= self.cfg["_risk"]["max_trades_per_day"] or \
                    sum(dec(o["risk_cap_inr"]) for o in pending_entries) + \
                    dec(self.cfg["_risk"]["max_loss_per_trade_inr"]) > \
                    dec(self.cfg["_risk"]["max_daily_loss_inr"]):
                raise LifecycleError("paper daily entry risk reserved")
            today_net = sum(dec(t["net_pnl_inr"]) for t in state["trades"]
                            if t["closed_at"][:10] == now.date().isoformat())
            if today_net <= -dec(self.cfg["max_daily_realized_loss_inr"]):
                raise LifecycleError("daily realized loss limit")
            reference = money(quote["price"])
            stop = money((dec(opening_high) + dec(opening_low)) / 2)
            if stop >= reference:
                raise LifecycleError("invalid opening-range stop")
            target = money(reference + dec(self.cfg["reward_risk"]) * (reference - stop))
            cap = money(reference * (1 + dec(self.cfg["max_entry_move_pct"]) / 100 +
                                     dec(self.cfg["slippage_bps_per_side"]) / 10000))
            risk_per_share = cap - stop
            available = broker.available_cash()
            equity = dec(state["cash_inr"]) + sum(
                dec(p["average_cost_inr"]) * p["quantity"] for p in state["positions"])
            exposure = sum(dec(p["average_cost_inr"]) * p["quantity"]
                           for p in state["positions"])
            max_exposure = equity * dec(self.cfg["max_portfolio_exposure_pct"]) / 100 - exposure
            cash_limit = available - equity * dec(self.cfg["_risk"]["cash_buffer_pct"]) / 100
            max_notional = min(dec(self.cfg["_risk"]["max_order_inr"]),
                               equity * dec(self.cfg["_risk"]["max_position_pct"]) / 100,
                               max_exposure, cash_limit)
            fee_guard = dec(self.cfg["risk_fee_buffer_inr"])
            risk_cap = dec(self.cfg["_risk"]["max_loss_per_trade_inr"])
            qty = min(whole((risk_cap - fee_guard) / risk_per_share),
                      whole((max_notional - fee_guard) / cap))
            if qty < 1 or qty * cap < dec(self.cfg["_risk"]["min_order_inr"]):
                raise LifecycleError("paper capital or risk insufficient")
            proposal = {"mode": "PAPER_ONLY", "status": "PROPOSED", "orders_possible": False,
                        "ticker": ticker, "side": "BUY_LONG_CASH", "quantity": qty,
                        "order_type": "market_reference", "entry_reference": float(reference),
                        "stop": float(stop), "target": float(target),
                        "trigger_at": bar_end.isoformat(), "plan_date": now.date().isoformat(),
                        "capital_inr": float(equity), "max_loss_inr": float(risk_cap),
                        "slippage_bps_per_side": self.cfg["slippage_bps_per_side"],
                        "fee_buffer_inr": self.cfg["risk_fee_buffer_inr"]}
            proposal["fingerprint"] = _fingerprint(proposal)
            verdict = self._gate(proposal, quote, now, state)
            if verdict["status"] != "PASS":
                report["entry_blocked"].append({"ticker": ticker,
                                                "reasons": verdict["reasons"]})
                broker.reject(proposal, now, verdict["reasons"], decision["signal_id"])
                state["seen_signals"].append(decision["signal_id"])
                _save(self.state_path, state)
                return
            reserve_per_share = money(cap + fee_guard / qty)
            order = broker.submit({"id": f"BUY|{decision['signal_id']}",
                                   "ticker": ticker, "instrument_id": decision["instrument_id"],
                                   "signal_id": decision["signal_id"], "side": "BUY",
                                   "quantity": qty, "reserved_inr": float(money(reserve_per_share * qty)),
                                   "reserve_per_share_inr": float(reserve_per_share),
                                   "max_fill_price_inr": float(cap), "stop_inr": float(stop),
                                   "target_inr": float(target), "risk_cap_inr": float(risk_cap),
                                   "risk_fingerprint": proposal["fingerprint"],
                                   "expires_at": decision["valid_until"]}, now)
            state["seen_signals"].append(decision["signal_id"])
            state["last_marks"][ticker] = {"price_inr": quote["price"],
                                           "at": quote["time"], "provider": quote["provider"]}
            _save(self.state_path, state)
            filled = broker.fill(order, now, quote, bar["volume"], snap["bar_start"])
            if filled:
                report["fills"].append(filled)
            _save(self.state_path, state)
        except (FeedUnavailable, LifecycleError, RiskError, ValueError, TypeError,
                KeyError, IndexError, OSError) as exc:
            report["entry_wait"].append({"ticker": ticker, "reason": type(exc).__name__})

    def cycle(self, now=None, engine_path=DEFAULT_ENGINE, committee_path=DEFAULT_COMMITTEE):
        with _state_lock(self.state_path):
            return self._cycle_locked(now, engine_path, committee_path)

    def _cycle_locked(self, now=None, engine_path=DEFAULT_ENGINE,
                      committee_path=DEFAULT_COMMITTEE):
        now = (self.now_fn() if now is None else now).astimezone(IST)
        state = _state(self.state_path, self.cfg)
        broker = self._broker(state)
        report = {"at": now.isoformat(), "mode": "PAPER_ONLY", "fills": [],
                  "entry_wait": [], "entry_blocked": [], "exit_data_unavailable": [],
                  "exit_risk_blocked": [], "selected": 0, "orders_possible": False}
        # Exit protection is independent of the committee and runs first.
        if trading_day(now.date())[0] and time(9, 15) <= now.time() < time(15, 15):
            for position in list(state["positions"]):
                self._exit_one(state, position, now, report)
        elif state["positions"]:
            report["exit_data_unavailable"] = [{"ticker": p["ticker"],
                                                 "reason": "MARKET_CLOSED_POSITION_OPEN"}
                                                for p in state["positions"]]
        for order in list(broker.pending(side="BUY")):
            if now >= _stamp(order["expires_at"]) or \
                    now.time() >= time.fromisoformat(self.cfg["flat_by_ist"]):
                broker.cancel(order, now, "DECISION_EXPIRED_OR_ENTRY_CUTOFF")
                _save(self.state_path, state)
                continue
            try:
                snap = _verified_snapshot(self.manager, order["ticker"], now,
                                          self.cfg["max_quote_age_seconds"])
                if snap["spread_pct"] > dec(self.cfg["max_spread_pct"]):
                    raise LifecycleError("pending entry spread too wide")
                filled = broker.fill(order, now, snap["quote"], snap["bar"]["volume"],
                                     snap["bar_start"])
                if filled:
                    report["fills"].append(filled)
                    _save(self.state_path, state)
            except (FeedUnavailable, LifecycleError, ValueError, TypeError,
                    KeyError, IndexError, OSError) as exc:
                report["entry_wait"].append({"ticker": order["ticker"],
                                             "reason": type(exc).__name__})
        if trading_day(now.date())[0] and time(9, 30) <= now.time() < time.fromisoformat(self.cfg["flat_by_ist"]):
            try:
                committee = _read(committee_path, "AI committee state")
                engine = _read(engine_path, "market engine state")
                if committee.get("day") != now.date().isoformat() or \
                        engine.get("day") != now.date().isoformat() or \
                        committee.get("orders_possible") is not False or \
                        engine.get("orders_possible") is not False or \
                        not isinstance(committee.get("last_run"), dict) or \
                        committee["last_run"].get("status") not in (
                            "DECISIONS_RECORDED", "NO_ELIGIBLE_CANDIDATES"):
                    raise LifecycleError("current paper research state unavailable")
                signals = {row.get("id"): row for row in engine.get("signals", [])
                           if isinstance(row, dict)}
                decisions = sorted((d for d in committee.get("decisions", [])
                                    if isinstance(d, dict) and d.get("outcome") == "SELECT"),
                                   key=lambda d: d.get("rank", 999))
                for decision in decisions:
                    sid = decision.get("signal_id")
                    if not isinstance(sid, str) or sid in state["seen_signals"] or \
                            broker.pending(decision.get("ticker"), "BUY") or \
                            broker.position(decision.get("ticker")):
                        continue
                    try:
                        if decision.get("orders_possible") is not False or \
                                decision.get("final_judge") != "AI4" or \
                                decision.get("rank", 0) < 1 or \
                                not _stamp(decision["decided_at"]) <= now < _stamp(decision["valid_until"]):
                            continue
                        clean = _signal(signals.get(sid), now, 300)
                        if decision.get("ticker") != clean["ticker"] or \
                                decision.get("instrument_id") != clean["instrument_id"] or \
                                decision.get("evidence_at") != clean["trigger_at"] or \
                                _stamp(decision["decided_at"]) < _stamp(clean["trigger_at"]) or \
                                _stamp(decision["valid_until"]) > \
                                _stamp(clean["trigger_at"]) + timedelta(seconds=300):
                            continue
                        report["selected"] += 1
                        self._entry_one(state, decision, signals[sid], now, report)
                    except (CommitteeError, LifecycleError, TypeError, KeyError):
                        continue
            except LifecycleError as exc:
                report["entry_wait"].append({"ticker": "*", "reason": str(exc)})
        report["open_positions"] = len(state["positions"])
        report["pending_orders"] = len(broker.pending())
        report["cash_inr"] = state["cash_inr"]
        report["available_cash_inr"] = float(broker.available_cash())
        report["realized_pnl_inr"] = state["realized_pnl_inr"]
        marks = state["last_marks"]
        fresh_marks = all(p["ticker"] in marks and
                          0 <= (now - _stamp(marks[p["ticker"]]["at"])).total_seconds() <=
                          self.cfg["max_quote_age_seconds"]
                          for p in state["positions"])
        if fresh_marks:
            unrealized = sum((money(dec(marks[p["ticker"]]["price_inr"]) *
                                    p["quantity"] -
                                    dec(p.get("cost_basis_inr",
                                              dec(p["average_cost_inr"]) * p["quantity"])) -
                                    dec(p["entry_fees_inr"]) -
                                    dec(fill_charges(self.cfg["cost_schedule"], "SELL",
                                                     dec(marks[p["ticker"]]["price_inr"]) *
                                                     p["quantity"])["total_inr"]))
                              for p in state["positions"]), Decimal(0))
            equity = dec(state["cash_inr"]) + sum(
                dec(marks[p["ticker"]]["price_inr"]) * p["quantity"]
                for p in state["positions"])
            report["unrealized_pnl_inr"] = float(money(unrealized))
            report["marked_equity_inr"] = float(money(equity))
            report["valuation_status"] = ("NO_OPEN_POSITIONS" if not state["positions"]
                                          else "FRESH_BROKER_MARK")
        else:
            report["unrealized_pnl_inr"] = None
            report["marked_equity_inr"] = None
            report["valuation_status"] = "FRESH_BROKER_MARK_UNAVAILABLE"
        state["last_cycle"] = report
        _save(self.state_path, state)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m india_trader paper-lifecycle",
                                     description="Autonomous local paper simulation; no real broker orders")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("once", "run", "status", "report"):
        p = sub.add_parser(name)
        p.add_argument("--state", default=str(DEFAULT_STATE))
        p.add_argument("--config", default=None)
        if name in ("once", "run"):
            p.add_argument("--engine-state", default=str(DEFAULT_ENGINE))
            p.add_argument("--committee-state", default=str(DEFAULT_COMMITTEE))
        if name == "run":
            p.add_argument("--max-cycles", type=int)
        if name == "report":
            p.add_argument("--date", help="IST YYYY-MM-DD daily activity filter")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.command == "status":
            print(json.dumps(_state(args.state, cfg), indent=2, allow_nan=False))
            return 0
        if args.command == "report":
            if args.date:
                try:
                    datetime.strptime(args.date, "%Y-%m-%d")
                except ValueError as exc:
                    raise LifecycleError("--date must be YYYY-MM-DD") from exc
            print(json.dumps(ledger_report(_state(args.state, cfg), args.date,
                                           max_mark_age_seconds=cfg["max_quote_age_seconds"]),
                             indent=2, allow_nan=False))
            return 0
        if args.command == "run" and args.max_cycles is not None and args.max_cycles < 1:
            raise LifecycleError("max-cycles must be positive")
        controller = PaperLifecycle(cfg, args.state)
        cycles = 0
        while True:
            result = controller.cycle(engine_path=args.engine_state,
                                      committee_path=args.committee_state)
            print(json.dumps(result, allow_nan=False), flush=True)
            cycles += 1
            if args.command == "once" or args.max_cycles is not None and cycles >= args.max_cycles:
                return 0
            clock_time.sleep(cfg["poll_seconds"])
    except KeyboardInterrupt:
        return 0
    except (LifecycleError, RiskError, AccountingError, OSError, ValueError,
            TypeError, KeyError) as exc:
        print("paper-lifecycle: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
