"""Regression checks for batch paper risk review and exit exemption."""
import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest

from india_trader import risk_manager as r
from india_trader.budget_manager import DEFAULTS, size_trade
from india_trader.core import IST
from india_trader.paper_orders import _fingerprint


NOW = datetime(2026, 10, 29, 9, 41, tzinfo=IST)
PLAN = {"mode": "PAPER_PLAN_ONLY", "status": "PLANNED", "ticker": "RELIANCE.NS",
        "side": "BUY_LONG_CASH", "date": "2026-10-29", "entry_reference": 100,
        "stop": 95, "target": 110, "flat_by_ist": "15:10",
        "trigger_at": "2026-10-29T09:40:00+05:30"}


def settings(**changes):
    raw = dict(DEFAULTS, budget_inr=10000, max_position_pct=50,
               max_order_inr=10000, min_order_inr=100,
               slippage_bps_per_side=0, fee_buffer_inr=0, blocked=[])
    raw.update(changes)
    return r.limits(raw)


def proposal(ticker="RELIANCE.NS"):
    cfg = settings()
    plan = dict(PLAN, ticker=ticker)
    _, order = size_trade(plan, cfg, NOW)
    return order


class RiskManagerTests(unittest.TestCase):
    def test_exact_entry_and_batch_daily_loss(self):
        first, second = proposal(), proposal("TCS.NS")
        result = r.review([first, second], settings(max_daily_loss_inr=300), NOW)
        self.assertEqual(result["passed"], 1)
        self.assertIn("DAILY_LOSS_LIMIT", result["verdicts"][1]["reasons"])
        self.assertEqual(result["verdicts"][0]["order_fingerprint"], first["fingerprint"])
        self.assertEqual(result["status"], "BLOCK")

    def test_changed_order_fresh_price_and_reward_gate(self):
        order = proposal()
        order["quantity"] += 1
        self.assertIn("ORDER_CHANGED", r.review([order], settings(), NOW)["verdicts"][0]["reasons"])
        fresh = {"as_of": "2026-10-29T09:40:00+05:30", "prices": {"RELIANCE.NS": 109}}
        result = r.review([proposal()], settings(), NOW, prices=fresh)
        self.assertIn("REWARD_RISK_TOO_LOW", result["verdicts"][0]["reasons"])
        self.assertIn("Risk increased", " ".join(result["verdicts"][0]["warnings"]))

    def test_duplicate_window_and_no_stop_allocation_block(self):
        order = proposal()
        duplicate = r.review([order, order], settings(), NOW)
        self.assertIn("DUPLICATE_BATCH_ORDER", duplicate["verdicts"][1]["reasons"])
        weekend = datetime(2026, 10, 31, 10, tzinfo=IST)
        self.assertIn("MARKET_OR_ENTRY_WINDOW_CLOSED",
                      r.review([order], settings(), weekend)["verdicts"][0]["reasons"])
        allocation = {"mode": "PAPER_ALLOCATION_ONLY", "status": "UNREVIEWED_NO_STOP",
                      "orders_possible": False, "ticker": "RELIANCE.NS", "side": "BUY_LONG_CASH",
                      "quantity": 1, "entry_reference": 100}
        allocation["fingerprint"] = _fingerprint(allocation)
        self.assertEqual(r.review([allocation], settings(), NOW)["status"], "BLOCK")

    def test_exit_is_not_blocked_by_entry_limits(self):
        exit_order = {"mode": "PAPER_EXIT_ONLY", "status": "PROPOSED_EXIT", "orders_possible": False,
                      "ticker": "RELIANCE.NS", "side": "SELL_LONG_CASH", "quantity": 5,
                      "order_type": "market_reference"}
        exit_order["fingerprint"] = _fingerprint(exit_order)
        late = datetime(2026, 10, 29, 16, tzinfo=IST)
        state = {"trades_today": 3, "orders_today": 10, "daily_planned_loss_inr": 1500,
                 "traded_symbols_today": ["RELIANCE.NS"]}
        portfolio = {"cash_inr": 1000, "equity_inr": 10000,
                     "positions": [{"ticker": "RELIANCE.NS", "quantity": 10}]}
        result = r.review([exit_order], settings(blocked=["RELIANCE"]), late,
                          portfolio=portfolio, state=state)
        self.assertEqual(result["status"], "PASS")
        too_many = dict(exit_order, quantity=11)
        too_many["fingerprint"] = _fingerprint(too_many)
        self.assertIn("EXIT_EXCEEDS_HOLDING", r.review([too_many], settings(), late,
                      portfolio=portfolio)["verdicts"][0]["reasons"])

    def test_prior_day_paper_counters_do_not_block_new_day(self):
        old = {"date": "2026-10-28", "trades_today": 3, "orders_today": 10,
               "daily_planned_loss_inr": 1500, "traded_symbols_today": ["RELIANCE.NS"]}
        self.assertEqual(r.review([proposal()], settings(), NOW, state=old)["status"], "PASS")

    def test_cli_records_exact_verdict_and_no_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            orders, output = root / "orders.json", root / "risk.json"
            config_file = root / "config.json"
            config_file.write_text(json.dumps({"paper": True, "budget_inr": 10000,
                                              "max_position_pct": 50, "max_order_inr": 10000,
                                              "slippage_bps_per_side": 0, "fee_buffer_inr": 0}))
            orders.write_text(json.dumps({"orders": [proposal()]}))
            args = ["--orders", str(orders), "--config", str(config_file),
                    "--portfolio", str(root / "missing_portfolio.json"),
                    "--prices", str(root / "missing_prices.json"),
                    "--state", str(root / "missing_state.json"),
                    "--now", NOW.isoformat(), "--out", str(output)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(r.main(args), 0)
            self.assertEqual(json.loads(output.read_text())["status"], "PASS")
            output.unlink()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(r.main(args + ["--no-write"]), 0)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
