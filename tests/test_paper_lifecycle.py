"""Offline Stage 5 integration: synthetic broker bars and quotes, no live account."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from india_trader.core import IST
from india_trader.paper_lifecycle import PaperLifecycle, load_config


DAY = datetime(2026, 10, 28, 9, 41, 50, tzinfo=IST)
SIGNAL_ID = "2026-10-28|RELIANCE.NS|orb|2026-10-28T09:35:00+05:30"


def bars(now, interval, volume=5000):
    first = now.replace(hour=9, minute=15, second=0, microsecond=0)
    last_start = now.replace(second=0, microsecond=0) - timedelta(minutes=interval)
    count = int((last_start - first).total_seconds() // (interval * 60)) + 1
    result = []
    for i in range(count):
        at = first + timedelta(minutes=i * interval)
        px = 103 if i == count - 1 else 100
        result.append({"timestamp": at.isoformat(), "open": px, "high": px + .4,
                       "low": px - .5, "close": px, "volume": volume})
    return result


class FakeManager:
    def __init__(self, price=103, volume=5000, ready=True):
        self.price, self.volume, self.ready = price, volume, ready
        self.now = DAY
        self.calls = []

    def fetch_candles(self, ticker, range_, interval, require_entry_ready, allow_yahoo):
        self.calls.append((ticker, interval))
        return {"provider": "UPSTOX", "entry_ready": self.ready,
                "bars": bars(self.now, interval, self.volume)}

    def fetch_quote(self, ticker, require_fresh, max_age_seconds):
        self.calls.append((ticker, "quote"))
        return {"ticker": ticker, "provider": "UPSTOX", "entry_ready": self.ready,
                "price": self.price, "bid": self.price - .05,
                "ask": self.price + .05,
                "time": (self.now - timedelta(seconds=2)).isoformat()}


def signal():
    return {"id": SIGNAL_ID, "ticker": "RELIANCE.NS", "setup": "orb",
            "trigger_at": "2026-10-28T09:40:00+05:30",
            "recorded_at": "2026-10-28T09:40:30+05:30",
            "one_minute_last_bar": "2026-10-28T09:39:00+05:30",
            "one_minute_last_close": 103.0, "five_minute_last_close": 103.0,
            "opening_range_high": 100.4,
            "quote_at": "2026-10-28T09:40:28+05:30", "quote_price": 103.0,
            "market_status_verified": True, "exchange": "NSE",
            "instrument_id": "NSE:RELIANCE-EQ", "instrument_id_type": "NSE_EQ_SYMBOL",
            "five_minute_bar_complete": True, "data_gaps": 0,
            "volume_quality": "POSITIVE", "source_5m": "UPSTOX",
            "source_1m": "FYERS", "quote_source": "UPSTOX",
            "metrics": {"day_volume": 15000, "turnover_inr": 1500000,
                        "range_pct": 2.0, "vwap": 102.0, "spread_pct": .2,
                        "ma20": None, "rsi14": None, "atr14": None},
            "status": "AWAITING_AI_REVIEW", "orders_possible": False}


def decision():
    return {"signal_id": SIGNAL_ID, "ticker": "RELIANCE.NS",
            "instrument_id": "NSE:RELIANCE-EQ", "rank": 1, "outcome": "SELECT",
            "decided_at": "2026-10-28T09:40:40+05:30",
            "valid_until": "2026-10-28T09:44:00+05:30",
            "evidence_at": "2026-10-28T09:40:00+05:30",
            "final_judge": "AI4", "orders_possible": False}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
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
        self.cfg = load_config()

    def cycle(self, manager, now=DAY):
        return PaperLifecycle(self.cfg, self.state, manager).cycle(
            now, self.engine, self.committee)

    def read(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_selected_signal_risk_checks_and_confirmed_fill_once(self):
        manager = FakeManager()
        first = self.cycle(manager)
        self.assertEqual(first["selected"], 1)
        self.assertEqual(len(first["fills"]), 1)
        self.assertTrue(first["fills"][0]["confirmed"])
        self.assertEqual(first["fills"][0]["side"], "BUY")
        saved = self.read()
        self.assertEqual(len(saved["positions"]), 1)
        self.assertLess(saved["cash_inr"], 100000)
        self.assertFalse(saved["orders_possible"])
        self.assertEqual(saved["orders"][0]["status"], "FILLED")
        second = self.cycle(manager)
        self.assertEqual(second["selected"], 0)
        self.assertEqual(len(self.read()["positions"]), 1)

    def test_exit_runs_without_ai_and_records_net_pnl(self):
        manager = FakeManager()
        self.cycle(manager)
        self.committee.unlink()
        manager.price = 99
        exit_result = self.cycle(manager, DAY + timedelta(minutes=1))
        self.assertTrue(any(x["side"] == "SELL" for x in exit_result["fills"]))
        saved = self.read()
        self.assertEqual(saved["positions"], [])
        self.assertEqual(saved["trades"][0]["reason"], "STOP_OR_TRAIL")
        self.assertLess(saved["trades"][0]["net_pnl_inr"], 0)
        self.assertLess(saved["realized_pnl_inr"], 0)

    def test_target_and_time_cutoff_are_independent_exit_reasons(self):
        for price, when, reason in ((110, DAY + timedelta(minutes=1), "TARGET"),
                                    (103, DAY.replace(hour=15, minute=10), "TIME_FLAT")):
            with self.subTest(reason=reason):
                self.state.unlink(missing_ok=True)
                manager = FakeManager()
                self.cycle(manager)
                manager.price = price
                self.committee.unlink()
                result = self.cycle(manager, when)
                self.assertTrue(any(x["side"] == "SELL" for x in result["fills"]))
                self.assertEqual(self.read()["trades"][0]["reason"], reason)
                self.committee.write_text(json.dumps({"day": DAY.date().isoformat(),
                                                      "decisions": [decision()],
                                                      "last_run": {"status": "DECISIONS_RECORDED"},
                                                      "orders_possible": False}), encoding="utf-8")

    def test_multiple_selected_stocks_continue_through_risk_and_paper_broker(self):
        second_signal = {**signal(), "id": SIGNAL_ID.replace("RELIANCE", "TCS"),
                         "ticker": "TCS.NS", "instrument_id": "NSE:TCS-EQ"}
        second_decision = {**decision(), "signal_id": second_signal["id"],
                           "ticker": "TCS.NS", "instrument_id": "NSE:TCS-EQ", "rank": 2}
        self.engine.write_text(json.dumps({"day": DAY.date().isoformat(),
                                           "signals": [signal(), second_signal],
                                           "orders_possible": False}), encoding="utf-8")
        self.committee.write_text(json.dumps({"day": DAY.date().isoformat(),
                                              "decisions": [decision(), second_decision],
                                              "last_run": {"status": "DECISIONS_RECORDED"},
                                              "orders_possible": False}), encoding="utf-8")
        result = self.cycle(FakeManager())
        self.assertEqual(result["selected"], 2)
        self.assertEqual(len(result["fills"]), 2)
        self.assertEqual({x["ticker"] for x in self.read()["positions"]},
                         {"RELIANCE.NS", "TCS.NS"})

    def test_independent_risk_manager_blocks_configured_ticker(self):
        self.cfg["_risk"]["blocked"].add("RELIANCE.NS")
        result = self.cycle(FakeManager())
        self.assertEqual(result["fills"], [])
        self.assertIn("BLOCKED_TICKER", result["entry_blocked"][0]["reasons"])
        self.assertEqual(self.read()["positions"], [])

    def test_ai_unavailable_blocks_new_entry_and_trailing_stop_works_after_fill(self):
        self.committee.write_text(json.dumps({"day": DAY.date().isoformat(),
                                              "decisions": [decision()],
                                              "last_run": {"status": "AI_UNAVAILABLE_NO_SELECTION"},
                                              "orders_possible": False}), encoding="utf-8")
        blocked = self.cycle(FakeManager())
        self.assertEqual(blocked["fills"], [])
        self.assertEqual(self.read()["positions"], [])
        self.committee.write_text(json.dumps({"day": DAY.date().isoformat(),
                                              "decisions": [decision()],
                                              "last_run": {"status": "DECISIONS_RECORDED"},
                                              "orders_possible": False}), encoding="utf-8")
        manager = FakeManager()
        self.cycle(manager)
        self.committee.unlink()
        manager.price = 102.5
        result = self.cycle(manager, DAY + timedelta(minutes=1))
        self.assertEqual(result["fills"][0]["side"], "SELL")
        self.assertEqual(self.read()["trades"][0]["reason"], "STOP_OR_TRAIL")

    def test_partial_fill_respects_one_bar_volume_cap_and_restart(self):
        manager = FakeManager(volume=20)
        first = self.cycle(manager)
        self.assertEqual(first["fills"][0]["quantity"], 2)
        self.assertEqual(self.read()["orders"][0]["status"], "PARTIAL")
        same_bar = self.cycle(manager)
        self.assertEqual(same_bar["fills"], [])
        next_bar = self.cycle(manager, DAY + timedelta(minutes=1))
        self.assertEqual(next_bar["fills"][0]["quantity"], 2)
        self.assertEqual(len(self.read()["orders"]), 1)

    def test_partial_exit_remains_pending_until_later_completed_bar(self):
        manager = FakeManager()
        self.cycle(manager)
        self.committee.unlink()
        manager.price = 99
        manager.volume = 20
        first = self.cycle(manager, DAY + timedelta(minutes=1))
        self.assertEqual(first["fills"][0]["quantity"], 2)
        self.assertEqual(first["fills"][0]["status"], "PARTIAL")
        same = self.cycle(manager, DAY + timedelta(minutes=1))
        self.assertEqual(same["fills"], [])
        later = self.cycle(manager, DAY + timedelta(minutes=2))
        self.assertEqual(later["fills"][0]["quantity"], 2)
        self.assertEqual(self.read()["trades"][0]["reason"], "STOP_OR_TRAIL")

    def test_unverified_feed_and_expired_decision_never_fill(self):
        manager = FakeManager(ready=False)
        result = self.cycle(manager)
        self.assertEqual(result["fills"], [])
        self.assertEqual(self.read()["positions"], [])
        self.committee.write_text(json.dumps({"day": DAY.date().isoformat(),
                                              "decisions": [{**decision(),
                                                             "valid_until":
                                                             "2026-10-28T09:41:00+05:30"}],
                                              "last_run": {"status": "DECISIONS_RECORDED"},
                                              "orders_possible": False}), encoding="utf-8")
        result = self.cycle(FakeManager())
        self.assertEqual(result["selected"], 0)
        self.assertEqual(result["fills"], [])

    def test_exit_feed_outage_keeps_position_open_and_marks_value_unknown(self):
        manager = FakeManager()
        self.cycle(manager)
        self.committee.unlink()
        manager.ready = False
        result = self.cycle(manager, DAY + timedelta(minutes=1))
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["exit_data_unavailable"][0]["ticker"], "RELIANCE.NS")
        self.assertIsNone(result["unrealized_pnl_inr"])
        self.assertEqual(len(self.read()["positions"]), 1)

    def test_closed_market_does_not_fetch_or_fill(self):
        manager = FakeManager()
        result = self.cycle(manager, datetime(2026, 11, 1, 10, tzinfo=IST))
        self.assertEqual(result["fills"], [])
        self.assertEqual(manager.calls, [])


if __name__ == "__main__":
    unittest.main()
