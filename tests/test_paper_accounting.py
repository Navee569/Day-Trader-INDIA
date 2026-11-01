"""Stage 6 paper cost, ledger reconciliation, order state and reporting checks."""
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from india_trader.auto_paper_broker import LocalPaperBroker
from india_trader.paper_accounting import AccountingError, reconcile, report
from india_trader.paper_costs import fill_charges
from india_trader.paper_lifecycle import PaperLifecycle, _blank, _state, load_config
from tests.test_paper_lifecycle import DAY, FakeManager, decision, signal


class PaperAccountingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = root / "paper.json"
        self.engine = root / "engine.json"
        self.committee = root / "committee.json"
        self.engine.write_text(json.dumps({"day": DAY.date().isoformat(),
                                           "signals": [signal()],
                                           "orders_possible": False}), encoding="utf-8")
        self.committee.write_text(json.dumps({"day": DAY.date().isoformat(),
                                              "decisions": [decision()],
                                              "last_run": {"status": "DECISIONS_RECORDED"},
                                              "orders_possible": False}), encoding="utf-8")

    def cycle(self, manager, now=DAY):
        return PaperLifecycle(self.cfg, self.state, manager).cycle(
            now, self.engine, self.committee)

    def saved(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_indian_intraday_charge_components_are_explicit_and_side_specific(self):
        schedule = self.cfg["cost_schedule"]
        buy = fill_charges(schedule, "BUY", 10000)
        sell = fill_charges(schedule, "SELL", 10000)
        self.assertEqual(buy["brokerage_inr"], 3.0)
        self.assertEqual(buy["exchange_inr"], .31)
        self.assertEqual(buy["sebi_inr"], .01)
        self.assertEqual(buy["gst_inr"], .60)
        self.assertEqual(buy["stamp_inr"], .30)
        self.assertEqual(buy["stt_inr"], 0)
        self.assertEqual(buy["total_inr"], 4.22)
        self.assertEqual(sell["stt_inr"], 2.5)
        self.assertEqual(sell["stamp_inr"], 0)
        self.assertEqual(sell["total_inr"], 6.42)

    def test_brokerage_cap_is_shared_by_partial_fills_of_same_order(self):
        cfg = load_config()
        cfg["cost_schedule"] = {**cfg["cost_schedule"],
                                "brokerage_bps": 100, "brokerage_cap_inr": 20}
        state = _blank(cfg)
        broker = LocalPaperBroker(state, cfg)
        order = broker.submit({"id": "BUY|cap", "ticker": "RELIANCE.NS",
                               "instrument_id": "NSE:RELIANCE-EQ", "signal_id": "cap",
                               "side": "BUY", "quantity": 100,
                               "reserved_inr": 10500.0, "reserve_per_share_inr": 105.0,
                               "max_fill_price_inr": 105.0, "stop_inr": 90.0,
                               "target_inr": 120.0, "risk_cap_inr": 5000.0,
                               "risk_fingerprint": "paper-test", "expires_at":
                               (DAY + timedelta(minutes=3)).isoformat()}, DAY)
        quote = {"price": 100.0, "provider": "UPSTOX", "time": DAY.isoformat()}
        first = broker.fill(order, DAY, quote, 500, "2026-10-28T09:40:00+05:30")
        second = broker.fill(order, DAY + timedelta(minutes=1), quote, 500,
                             "2026-10-28T09:41:00+05:30")
        self.assertEqual((first["quantity"], second["quantity"]), (50, 50))
        self.assertEqual(first["charges"]["brokerage_inr"], 20)
        self.assertEqual(second["charges"]["brokerage_inr"], 0)
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(reconcile(state)["status"], "BALANCED")

    def test_fill_cannot_spend_cash_reserved_for_another_paper_order(self):
        cfg = load_config()
        state = _blank(cfg)
        state["initial_capital_inr"] = state["cash_inr"] = 200.0
        broker = LocalPaperBroker(state, cfg)
        def proposal(ticker):
            return {"id": "BUY|" + ticker, "ticker": ticker,
                    "instrument_id": "NSE:" + ticker[:-3] + "-EQ",
                    "signal_id": ticker, "side": "BUY", "quantity": 1,
                    "reserved_inr": 100.0, "reserve_per_share_inr": 100.0,
                    "max_fill_price_inr": 105.0, "stop_inr": 90.0,
                    "target_inr": 120.0, "risk_cap_inr": 500.0,
                    "risk_fingerprint": ticker, "expires_at":
                    (DAY + timedelta(minutes=3)).isoformat()}
        first = broker.submit(proposal("RELIANCE.NS"), DAY)
        broker.submit(proposal("TCS.NS"), DAY)
        quote = {"price": 100.0, "provider": "UPSTOX", "time": DAY.isoformat()}
        self.assertIsNone(broker.fill(first, DAY, quote, 100,
                                      "2026-10-28T09:40:00+05:30"))
        self.assertEqual(first["status"], "CANCELLED")
        self.assertEqual(state["cash_inr"], 200)
        self.assertEqual(reconcile(state)["available_cash_inr"], 100)

    def test_rejected_and_cancelled_orders_are_persisted(self):
        self.cfg["_risk"]["blocked"].add("RELIANCE.NS")
        self.cycle(FakeManager())
        saved = self.saved()
        self.assertEqual(saved["orders"][0]["status"], "REJECTED")
        self.assertEqual(saved["events"][0]["kind"], "REJECTED")
        self.assertEqual(report(saved)["order_statuses"]["REJECTED"], 1)
        self.state.unlink()
        self.cfg = load_config()
        manager = FakeManager(volume=20)
        self.cycle(manager)
        self.assertEqual(self.saved()["orders"][0]["status"], "PARTIAL")
        self.cycle(manager, DAY + timedelta(minutes=3))
        saved = self.saved()
        self.assertEqual(saved["orders"][0]["status"], "CANCELLED")
        self.assertEqual(saved["orders"][0]["reserved_inr"], 0)
        self.assertEqual(saved["positions"][0]["quantity"], 2)
        self.assertEqual(reconcile(saved)["status"], "BALANCED")

    def test_daily_report_reconstructs_costs_cash_trades_and_events(self):
        manager = FakeManager()
        self.cycle(manager)
        manager.price = 99
        self.committee.unlink()
        self.cycle(manager, DAY + timedelta(minutes=1))
        saved = self.saved()
        view = report(saved, DAY.date().isoformat())
        self.assertEqual(view["balance"]["status"], "BALANCED")
        self.assertEqual(view["order_statuses"]["FILLED"], 2)
        self.assertEqual(view["open_positions"], [])
        self.assertEqual(len(view["trade_history"]), 1)
        self.assertEqual(view["daily"][0]["simulated_fills"], 2)
        self.assertEqual(view["daily"][0]["end_of_day_equity_inr"], saved["cash_inr"])
        self.assertEqual(view["daily"][0]["realized_net_pnl_inr"],
                         saved["realized_pnl_inr"])
        self.assertIn("stt_inr", view["daily"][0]["modeled_charges_inr"])
        self.assertEqual(view["daily"][0]["submitted_orders"], 2)
        self.assertTrue(all(x["paper_only"] for x in view["order_events"]))

    def test_open_unrealized_is_only_current_while_quote_mark_is_fresh(self):
        self.cycle(FakeManager())
        saved = self.saved()
        fresh = report(saved, now=DAY + timedelta(seconds=10))
        stale = report(saved, now=DAY + timedelta(minutes=1))
        self.assertEqual(fresh["valuation_status"], "FRESH_BROKER_MARK")
        self.assertIsNotNone(fresh["unrealized_pnl_inr"])
        self.assertEqual(stale["valuation_status"], "STALE_OR_UNKNOWN")
        self.assertIsNone(stale["unrealized_pnl_inr"])
        self.assertIsNotNone(stale["last_observed_unrealized_pnl_inr"])

    def test_tampered_cash_or_holdings_fail_reconciliation(self):
        self.cycle(FakeManager())
        original = self.saved()
        bad_cash = {**original, "cash_inr": original["cash_inr"] + 1}
        self.state.write_text(json.dumps(bad_cash), encoding="utf-8")
        with self.assertRaises(AccountingError):
            _state(self.state, self.cfg)
        bad_holdings = {**original, "positions": [{**original["positions"][0],
                                                    "quantity": 99}]}
        self.state.write_text(json.dumps(bad_holdings), encoding="utf-8")
        with self.assertRaises(AccountingError):
            _state(self.state, self.cfg)
        bad_charges = json.loads(json.dumps(original))
        fill = next(e for e in bad_charges["events"] if e["kind"] == "SIMULATED_FILL")
        fill["charges"]["brokerage_inr"] += 1
        self.state.write_text(json.dumps(bad_charges), encoding="utf-8")
        with self.assertRaises(AccountingError):
            _state(self.state, self.cfg)

    def test_starting_capital_is_configurable_and_locked_after_creation(self):
        custom = load_config()
        custom["initial_capital_inr"] = 250000
        blank = _blank(custom)
        self.assertEqual(blank["cash_inr"], 250000)
        self.assertEqual(reconcile(blank)["available_cash_inr"], 250000)
        self.cycle(FakeManager())
        with self.assertRaises(ValueError):
            _state(self.state, custom)

    def test_stage5_position_without_cost_basis_is_migrated_in_memory(self):
        self.cycle(FakeManager())
        saved = self.saved()
        saved["positions"][0].pop("cost_basis_inr")
        self.state.write_text(json.dumps(saved), encoding="utf-8")
        migrated = _state(self.state, self.cfg)
        self.assertGreater(migrated["positions"][0]["cost_basis_inr"], 0)
        self.assertEqual(reconcile(migrated)["status"], "BALANCED")


if __name__ == "__main__":
    unittest.main()
