"""Offline checks of the exact-ticket paper broker handoff and ledger."""
import contextlib
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest

from india_trader import paper_broker as b
from india_trader import risk_manager as r
from india_trader.budget_manager import DEFAULTS, size_trade
from india_trader.core import IST


NOW = datetime(2026, 10, 29, 9, 41, tzinfo=IST)


def setup():
    raw = dict(DEFAULTS, budget_inr=10000, max_position_pct=50,
               max_order_inr=10000, slippage_bps_per_side=0,
               fee_buffer_inr=0, blocked=[])
    return r.limits(raw)


def entry():
    plan = {"mode": "PAPER_PLAN_ONLY", "status": "PLANNED", "ticker": "RELIANCE.NS",
            "side": "BUY_LONG_CASH", "date": NOW.date().isoformat(),
            "entry_reference": 100, "stop": 95, "target": 110,
            "flat_by_ist": "15:10", "trigger_at": "2026-10-29T09:40:00+05:30"}
    return size_trade(plan, setup(), NOW)[1]


def verdict(order, at=NOW, portfolio=None, state=None):
    return r.review([order], setup(), at, portfolio=portfolio, state=state)


class PaperBrokerTests(unittest.TestCase):
    def test_broker_rebuilds_daily_limits_from_fill_log(self):
        order = entry()
        state = {"orders": [{"date": NOW.date().isoformat(), "status": "simulated_fill",
                              "fingerprint": order["fingerprint"], "ticker": "RELIANCE.NS",
                              "kind": "entry", "quantity": 20, "max_loss_inr": 100}]}
        rebuilt = b._today_state(state, NOW)
        self.assertEqual(rebuilt["trades_today"], 1)
        self.assertEqual(rebuilt["orders_today"], 1)
        self.assertEqual(rebuilt["daily_planned_loss_inr"], 100)
        self.assertEqual(rebuilt["traded_symbols_today"], ["RELIANCE.NS"])

    def test_partial_paper_fills_count_one_trade_and_one_order(self):
        order, state = entry(), b._today_state({}, NOW)
        risk = verdict(order)
        portfolio = b._portfolio("nonexistent-paper-portfolio.json", setup())
        b.decide(order, risk, state, NOW, 1, "yes RELIANCE.NS")
        first = b.simulate_fill(order, risk, state, portfolio, setup(), NOW, 100, quantity=20)
        self.assertEqual(first["remaining_quantity"], 30)
        self.assertEqual(b.ticket(order, risk, state, NOW, 1)["status"], "PARTIAL_FILL")
        later = NOW + timedelta(minutes=10)
        second = b.simulate_fill(order, risk, state, portfolio, setup(), later, 100, quantity=30)
        self.assertEqual(second["remaining_quantity"], 0)
        self.assertEqual(portfolio["positions"][0]["quantity"], 50)
        self.assertEqual(state["trades_today"], 1)
        self.assertEqual(state["orders_today"], 1)
        self.assertEqual(state["daily_planned_loss_inr"], 250)
        with self.assertRaises(b.BrokerError):
            b.simulate_fill(order, risk, state, portfolio, setup(), later, 100, quantity=1)

    def test_denied_or_failed_paper_result_keeps_portfolio_unchanged(self):
        order, state = entry(), b._today_state({}, NOW)
        risk = verdict(order)
        portfolio = b._portfolio("nonexistent-paper-portfolio.json", setup())
        b.decide(order, risk, state, NOW, 1, "yes")
        b.record_result(order, state, NOW, "failed", "paper quote unavailable")
        with self.assertRaises(b.BrokerError):
            b.simulate_fill(order, risk, state, portfolio, setup(), NOW, 100)
        self.assertEqual(portfolio["cash_inr"], 10000)
        self.assertEqual(state["trades_today"], 0)

    def test_exact_yes_ticket_fill_and_duplicate_guard(self):
        order = entry()
        risk = verdict(order)
        state = b._today_state({}, NOW)
        portfolio = b._portfolio("nonexistent-paper-portfolio.json", setup())
        shown = b.ticket(order, risk, state, NOW, 1)
        self.assertEqual(shown["status"], "READY")
        self.assertEqual(shown["stop_inr"], 95)
        with self.assertRaises(b.BrokerError):
            b.decide(order, risk, state, NOW, 1, "yes to all")
        b.decide(order, risk, state, NOW, 1, "yes")
        fill = b.simulate_fill(order, risk, state, portfolio, setup(), NOW, 100)
        self.assertEqual(fill["status"], "simulated_fill")
        self.assertEqual(portfolio["cash_inr"], 5000)
        self.assertEqual(portfolio["positions"][0]["quantity"], 50)
        self.assertEqual(state["trades_today"], 1)
        self.assertEqual(state["daily_planned_loss_inr"], 250)
        self.assertEqual(b.ticket(order, risk, state, NOW, 1)["status"], "SIMULATED_FILL")
        with self.assertRaises(b.BrokerError):
            b.simulate_fill(order, risk, state, portfolio, setup(), NOW, 100)

    def test_tamper_stale_risk_decline_and_bad_price(self):
        order, state = entry(), b._today_state({}, NOW)
        risk = verdict(order)
        changed = dict(order, quantity=1)
        self.assertEqual(b.ticket(changed, risk, state, NOW, 1)["status"], "BLOCKED")
        self.assertEqual(b.ticket(order, risk, state, NOW + timedelta(minutes=6), 1)["status"], "BLOCKED")
        portfolio = b._portfolio("nonexistent-paper-portfolio.json", setup())
        b.decide(order, risk, state, NOW, 1, "no")
        with self.assertRaises(b.BrokerError):
            b.simulate_fill(order, risk, state, portfolio, setup(), NOW, 100)
        self.assertEqual(portfolio["cash_inr"], 10000)
        state = b._today_state({}, NOW)
        b.decide(order, risk, state, NOW, 1, "yes")
        with self.assertRaises(b.BrokerError):
            b.simulate_fill(order, risk, state, portfolio, setup(), NOW, 109)
        self.assertEqual(state["orders_today"], 0)
        self.assertEqual(portfolio["cash_inr"], 10000)

    def test_flatten_exit_even_after_entry_window_and_realized_loss(self):
        order, state = entry(), b._today_state({}, NOW)
        portfolio = b._portfolio("nonexistent-paper-portfolio.json", setup())
        risk = verdict(order)
        b.decide(order, risk, state, NOW, 1, "yes")
        b.simulate_fill(order, risk, state, portfolio, setup(), NOW, 100)
        late = datetime(2026, 10, 29, 16, tzinfo=IST)
        exit_order = b.flatten_ticket("RELIANCE", None, portfolio, late)
        exit_risk = verdict(exit_order, late, portfolio, state)
        self.assertEqual(exit_risk["status"], "PASS")
        b.decide(exit_order, exit_risk, state, late, 1, "yes")
        result = b.simulate_fill(exit_order, exit_risk, state, portfolio, setup(), late, 98)
        self.assertEqual(result["kind"], "exit")
        self.assertEqual(portfolio["positions"], [])
        self.assertEqual(portfolio["cash_inr"], 9900)
        self.assertEqual(portfolio["realized_pnl_inr"], -100)
        self.assertEqual(state["orders_today"], 2)
        with self.assertRaises(b.BrokerError):
            b.flatten_ticket("RELIANCE", None, portfolio, late)

    def test_entry_rechecks_daily_limits_since_verdict(self):
        order, risk = entry(), verdict(entry())
        state = b._today_state({}, NOW)
        portfolio = b._portfolio("nonexistent-paper-portfolio.json", setup())
        b.decide(order, risk, state, NOW, 1, "yes")
        state["trades_today"] = setup()["max_trades_per_day"]
        with self.assertRaises(b.BrokerError):
            b.simulate_fill(order, risk, state, portfolio, setup(), NOW, 100)
        self.assertEqual(portfolio["cash_inr"], 10000)

    def test_cli_file_handoff_show_approve_fill_flatten_and_log(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            order = entry()
            paths = {key: root / f"{key}.json" for key in
                     ("orders", "risk", "state", "portfolio", "config", "exit", "exit_risk")}
            paths["orders"].write_text(json.dumps({"orders": [order]}))
            paths["risk"].write_text(json.dumps(verdict(order)))
            paths["config"].write_text(json.dumps({"paper": True, "budget_inr": 10000,
                "max_position_pct": 50, "max_order_inr": 10000,
                "slippage_bps_per_side": 0, "fee_buffer_inr": 0}))
            base = ["--orders", str(paths["orders"]), "--risk", str(paths["risk"]),
                    "--state", str(paths["state"]), "--portfolio", str(paths["portfolio"]),
                    "--config", str(paths["config"]), "--now", NOW.isoformat()]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(b.main(base + ["show"]), 0)
                self.assertEqual(b.main(base + ["approve", "--index", "1", "--reply", "yes"]), 0)
                self.assertEqual(b.main(base + ["fill", "--index", "1", "--price", "100",
                                                 "--confirm", "PAPER"]), 0)
                self.assertEqual(b.main(base + ["flatten", "--ticker", "RELIANCE",
                                                 "--out", str(paths["exit"])]), 0)
                self.assertEqual(b.main(base + ["log"]), 0)
            self.assertEqual(len(json.loads(paths["state"].read_text())["orders"]), 2)
            self.assertEqual(json.loads(paths["exit"].read_text())["orders"][0]["side"], "SELL_LONG_CASH")
            self.assertEqual(json.loads(paths["portfolio"].read_text())["cash_inr"], 5000)
            late = datetime(2026, 10, 29, 16, tzinfo=IST)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(r.main(["--orders", str(paths["exit"]),
                    "--config", str(paths["config"]), "--portfolio", str(paths["portfolio"]),
                    "--state", str(paths["state"]), "--now", late.isoformat(),
                    "--out", str(paths["exit_risk"])]), 0)
            exit_base = ["--orders", str(paths["exit"]), "--risk", str(paths["exit_risk"]),
                         "--state", str(paths["state"]), "--portfolio", str(paths["portfolio"]),
                         "--config", str(paths["config"]), "--now", late.isoformat()]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(b.main(exit_base + ["show"]), 0)
                self.assertEqual(b.main(exit_base + ["approve", "--index", "1", "--reply", "yes"]), 0)
                self.assertEqual(b.main(exit_base + ["fill", "--index", "1", "--price", "98",
                                                      "--confirm", "PAPER"]), 0)
            final = json.loads(paths["portfolio"].read_text())
            self.assertEqual(final["positions"], [])
            self.assertEqual(final["realized_pnl_inr"], -100)

    def test_cli_partial_fill_and_failed_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            order = entry()
            paths = {key: root / f"{key}.json" for key in ("orders", "risk", "state", "portfolio", "config")}
            paths["orders"].write_text(json.dumps({"orders": [order]}))
            paths["risk"].write_text(json.dumps(verdict(order)))
            paths["config"].write_text(json.dumps({"paper": True, "budget_inr": 10000,
                "max_position_pct": 50, "max_order_inr": 10000,
                "slippage_bps_per_side": 0, "fee_buffer_inr": 0}))
            base = ["--orders", str(paths["orders"]), "--risk", str(paths["risk"]),
                    "--state", str(paths["state"]), "--portfolio", str(paths["portfolio"]),
                    "--config", str(paths["config"]), "--now", NOW.isoformat(),
                    "--today", NOW.date().isoformat()]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(b.main(base + ["show"]), 0)
                self.assertEqual(b.main(base + ["approve", "--index", "1", "--reply", "yes RELIANCE.NS"]), 0)
                self.assertEqual(b.main(base + ["fill", "--index", "1", "--price", "100",
                                                 "--qty", "20", "--confirm", "PAPER"]), 0)
                self.assertEqual(b.main(base + ["result", "--index", "1", "--status", "failed",
                                                 "--note", "paper remainder cancelled"]), 0)
                self.assertEqual(b.main(base + ["log", "--all"]), 0)
            self.assertIn("PAPER TICKET", output.getvalue())
            self.assertEqual(json.loads(paths["portfolio"].read_text())["positions"][0]["quantity"], 20)
            events = json.loads(paths["state"].read_text())["orders"]
            self.assertEqual([x["status"] for x in events], ["approved", "simulated_fill", "failed"])


if __name__ == "__main__":
    unittest.main()
