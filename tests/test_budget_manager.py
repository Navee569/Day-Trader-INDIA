"""Paper INR budget sizing, weighted allocation, and downstream handoff."""
import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest

from india_trader import budget_manager as b
from india_trader.core import IST
from india_trader.paper_orders import risk_review


NOW = datetime(2026, 10, 29, 9, 41, tzinfo=IST)
PLAN = {"mode": "PAPER_PLAN_ONLY", "status": "PLANNED", "ticker": "RELIANCE.NS",
        "side": "BUY_LONG_CASH", "date": "2026-10-29", "entry_reference": 100,
        "stop": 95, "target": 110, "flat_by_ist": "15:10",
        "trigger_at": "2026-10-29T09:40:00+05:30", "entry_confirmed": True,
        "data_origin": "synthetic-test"}


def cfg(**changes):
    base = dict(b.DEFAULTS, budget_inr=10000, max_loss_per_trade_inr=200,
                max_position_pct=50, max_order_inr=3000, min_order_inr=100,
                cash_buffer_pct=10, slippage_bps_per_side=0, fee_buffer_inr=0,
                blocked=set())
    base.update(changes)
    return base


class BudgetManagerTests(unittest.TestCase):
    def test_four_limits_and_risk_handoff(self):
        sizing, order = b.size_trade(PLAN, cfg(), NOW)
        self.assertEqual([(x["name"], x["shares"]) for x in sizing["limits"]],
                         [("max loss", 40), ("position cap", 50), ("max order", 30), ("cash", 90)])
        self.assertEqual(sizing["bound_by"], "max order")
        self.assertEqual(order["quantity"], 30)
        self.assertEqual(order["planned_loss_inr"], 150)
        self.assertEqual(order["market_lot_assumed"], 1)
        self.assertEqual(risk_review(order, NOW.isoformat())["status"], "PASS")
        self.assertFalse(order["orders_possible"])
        self.assertEqual(b.size_trade(PLAN, cfg(max_order_inr=100000), NOW)[0]["bound_by"], "max loss")
        self.assertEqual(b.size_trade(PLAN, cfg(max_order_inr=100000, max_position_pct=20), NOW)[0]["bound_by"],
                         "position cap")
        cash = b.size_trade(PLAN, cfg(max_order_inr=100000, max_loss_per_trade_inr=1000,
                                       max_position_pct=100), NOW, portfolio={"cash_inr": 2000})[0]
        self.assertEqual(cash["bound_by"], "cash")
        self.assertEqual(cash["quantity"], 10)

    def test_fresh_quote_raises_risk_and_stale_quote_is_ignored(self):
        fresh = {"prices": {"RELIANCE.NS": 104}, "as_of": "2026-10-29T09:40:00+05:30"}
        stale = {"prices": {"RELIANCE.NS": 104}, "as_of": "2026-10-29T09:20:00+05:30"}
        a, _ = b.size_trade(PLAN, cfg(max_order_inr=100000, max_position_pct=100), NOW,
                            price_doc=fresh)
        c, _ = b.size_trade(PLAN, cfg(max_order_inr=100000, max_position_pct=100), NOW,
                            price_doc=stale)
        self.assertEqual(a["entry_reference"], 104)
        self.assertEqual(a["quantity"], 22)
        self.assertEqual(c["entry_reference"], 100)
        self.assertEqual(c["quantity"], 40)
        self.assertIn("older", c["quote_note"])

    def test_limit_price_and_refusal_gates(self):
        sizing, order = b.size_trade(PLAN, cfg(), NOW, limit="plan")
        self.assertEqual(sizing["order_type"], "limit")
        self.assertEqual(order["limit_price"], 100)
        with self.assertRaisesRegex(b.BudgetError, "above stop and below target"):
            b.size_trade(PLAN, cfg(), NOW, limit=110)
        too_wide, proposal = b.size_trade(PLAN, cfg(max_loss_per_trade_inr=1), NOW)
        self.assertIsNone(proposal)
        self.assertIn("one share risks", too_wide["refusal"])
        rr, proposal = b.size_trade(PLAN, cfg(min_reward_risk=3), NOW)
        self.assertIsNone(proposal)
        self.assertIn("reward:risk", rr["refusal"])
        small, proposal = b.size_trade(PLAN, cfg(min_order_inr=4000), NOW)
        self.assertIsNone(proposal)
        self.assertIn("minimum order", small["refusal"])

    def test_plan_and_config_rejections(self):
        with self.assertRaisesRegex(b.BudgetError, "IST"):
            b.size_trade(dict(PLAN, date="2026-10-28"), cfg(), NOW)
        with self.assertRaisesRegex(b.BudgetError, "long"):
            b.size_trade(dict(PLAN, side="sell"), cfg(), NOW)
        with self.assertRaisesRegex(b.BudgetError, "blocked"):
            b.size_trade(PLAN, cfg(blocked={"RELIANCE.NS"}), NOW)
        with self.assertRaisesRegex(b.BudgetError, "budget"):
            b.size_trade(PLAN, dict(b.DEFAULTS, blocked=set()), NOW)
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "config.json"
            p.write_text('{"paper":false,"budget_inr":10000}')
            with self.assertRaisesRegex(b.BudgetError, "paper-only"):
                b.config(p)

    def test_weighted_cap_redistribution_and_idle(self):
        rows = b.inline_weights("RELIANCE=80,TCS=20")
        result, orders = b.size_weights(rows, cfg(max_order_inr=100000), NOW)
        allocations = {x["ticker"]: x["allocation_inr"] for x in result["positions"]}
        self.assertEqual(allocations, {"RELIANCE.NS": 5000, "TCS.NS": 4000})
        self.assertEqual(result["idle_inr"], 0)
        self.assertEqual(orders, [])
        capped, _ = b.size_weights(rows, cfg(max_order_inr=2000), NOW)
        self.assertEqual(capped["invested_inr"], 4000)
        self.assertEqual(capped["idle_inr"], 5000)

    def test_weighted_whole_shares_drop_and_no_stop(self):
        rows = b.inline_weights("RELIANCE=90,TCS=10")
        prices = {"prices": {"RELIANCE.NS": 100, "TCS.NS": 8000},
                  "as_of": "2026-10-29T09:40:00+05:30"}
        result, orders = b.size_weights(rows, cfg(max_order_inr=100000), NOW, prices_doc=prices)
        self.assertEqual(len(result["positions"]), 1)
        self.assertEqual(result["positions"][0]["ticker"], "RELIANCE.NS")
        self.assertEqual(result["positions"][0]["quantity"], 50)
        self.assertEqual(orders[0]["status"], "UNREVIEWED_NO_STOP")
        self.assertEqual(risk_review(orders[0], NOW.isoformat())["status"], "BLOCK")
        self.assertEqual(result["dropped"][0]["ticker"], "TCS.NS")

    def test_duplicate_stale_weight_price_and_missing_whole_shares(self):
        with self.assertRaisesRegex(b.BudgetError, "duplicate"):
            b.inline_weights("RELIANCE=50,RELIANCE.NS=50")
        rows = b.inline_weights("RELIANCE=100")
        with self.assertRaisesRegex(b.BudgetError, "needs --prices"):
            b.size_weights(rows, cfg(), NOW, whole_shares=True)
        old = {"prices": {"RELIANCE.NS": 100}, "as_of": "2026-10-29T09:00:00+05:30"}
        with self.assertRaisesRegex(b.BudgetError, "dated price"):
            b.size_weights(rows, cfg(), NOW, prices_doc=old)

    def test_cli_saved_collection_and_refusal_replaces_old_proposal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = root / "plan.json"
            saved = root / "orders.json"
            plan.write_text(json.dumps(PLAN))
            args = ["--plan", str(plan), "--budget", "10000", "--now", NOW.isoformat(),
                    "--write", str(saved)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(b.main(args), 0)
            doc = json.loads(saved.read_text())
            self.assertEqual(len(doc["orders"]), 1)
            self.assertEqual(risk_review(doc["orders"][0], NOW.isoformat())["status"], "PASS")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(b.main(args + ["--max-loss", "1"]), 1)
            self.assertEqual(json.loads(saved.read_text())["orders"], [])

    def test_cli_targets_file_with_dated_prices(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            targets = root / "targets.json"
            prices = root / "prices.json"
            saved = root / "allocations.json"
            targets.write_text(json.dumps({"source": "synthetic-test", "weights": [
                {"ticker": "RELIANCE.NS", "weight": 70},
                {"ticker": "TCS.NS", "weight": 30}]}))
            prices.write_text(json.dumps({"as_of": "2026-10-29T09:40:00+05:30",
                                          "prices": {"RELIANCE.NS": 100, "TCS.NS": 200}}))
            args = ["--targets", str(targets), "--prices", str(prices), "--budget", "10000",
                    "--now", NOW.isoformat(), "--write", str(saved)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(b.main(args), 0)
            doc = json.loads(saved.read_text())
            self.assertEqual(doc["status"], "ALLOCATION_ONLY")
            self.assertEqual(len(doc["orders"]), 2)
            self.assertTrue(all(x["status"] == "UNREVIEWED_NO_STOP" for x in doc["orders"]))


if __name__ == "__main__":
    unittest.main()
