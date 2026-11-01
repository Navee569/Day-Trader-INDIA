"""Deterministic offline checks for the Stage 3 paper-market controller."""
from datetime import datetime, timedelta
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from india_trader.core import IST
from india_trader.market_data_manager import FeedUnavailable
from india_trader.market_engine import ContinuousMarketEngine, EngineError, _metrics


NOW = datetime(2026, 10, 28, 9, 41, tzinfo=IST)


def _bars(interval):
    start = NOW.replace(hour=9, minute=15, second=0, microsecond=0)
    count = 25 if interval == 1 else 5
    result = []
    for i in range(count):
        at = start + timedelta(minutes=i * interval)
        px = 100 + (3 if i == count - 1 else i * 0.2)
        result.append({"timestamp": at.isoformat(), "open": px, "high": px + 0.2,
                       "low": px - 0.2, "close": px, "volume": 3000.0})
    return result


def _series():
    bars = _bars(5)
    return {"t": [int(datetime.fromisoformat(b["timestamp"]).timestamp()) for b in bars],
            "o": [b["open"] for b in bars], "h": [b["high"] for b in bars],
            "l": [b["low"] for b in bars], "c": [b["close"] for b in bars],
            "v": [b["volume"] for b in bars],
            "meta": {"data_provider": "Yahoo Finance unofficial"}}


class FakeManager:
    def __init__(self, fail=False, ask=103.1, one=None):
        self.fail, self.ask, self.one = fail, ask, one
        self.calls = []

    def fetch_candles(self, ticker, range_, interval, require_entry_ready, allow_yahoo):
        self.calls.append((ticker, interval, require_entry_ready, allow_yahoo))
        if self.fail:
            raise FeedUnavailable([{"source": "UPSTOX", "status": "FAILED"}])
        return {"source": "Upstox read-only", "provider": "UPSTOX", "entry_ready": True,
                "bars": _bars(interval) if interval == 5 else self.one or _bars(1)}

    def fetch_quote(self, ticker, require_fresh):
        self.calls.append((ticker, "quote", require_fresh))
        return {"ticker": ticker, "provider": "UPSTOX", "entry_ready": True,
                "price": 103.0, "bid": 102.9, "ask": self.ask,
                "time": (NOW - timedelta(seconds=2)).isoformat()}


class MarketEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "market-engine.json"

    def _engine(self, manager=None, symbols=None, fetch=None, **settings):
        def broad(batch, now):
            return ({ticker: _series() for ticker in batch}, {})
        return ContinuousMarketEngine(symbols or ["RELIANCE.NS"], self.state,
                                      manager or FakeManager(), fetch or broad, **settings)

    def test_closed_session_does_not_fetch_market_data(self):
        calls = []
        engine = self._engine(fetch=lambda batch, now: calls.append(batch))
        result = engine.cycle(datetime(2026, 11, 1, 10, 0, tzinfo=IST))
        self.assertEqual(result["status"], "MARKET_CLOSED")
        self.assertEqual(calls, [])
        self.assertEqual(result["coverage"]["not_proven_reliable_live"], 1)

    def test_broad_rotation_and_explicit_coverage(self):
        batches = []
        def fetch(batch, now):
            batches.append(batch)
            return {}, {ticker: "NO_FEED" for ticker in batch}
        engine = self._engine(symbols=["RELIANCE.NS", "TCS.NS", "INFY.NS"],
                              fetch=fetch, broad_batch_size=1)
        first = engine.cycle(NOW)
        second = engine.cycle(NOW + timedelta(minutes=5))
        self.assertEqual(batches, [["RELIANCE.NS"], ["TCS.NS"]])
        self.assertEqual(first["coverage"]["universe"], 3)
        self.assertEqual(second["broad"]["not_attempted_this_cycle"], 2)
        self.assertEqual(second["broad"]["failed"], 1)

    def test_two_symbols_are_monitored_without_stopping_after_first_signal(self):
        engine = self._engine(symbols=["RELIANCE.NS", "TCS.NS"], detailed_batch_size=2)
        result = engine.cycle(NOW)
        self.assertEqual(result["broad"]["shortlisted"], 2)
        self.assertEqual(result["detailed"]["attempted"], 2)
        self.assertEqual({s["ticker"] for s in result["detailed"]["new_signals"]},
                         {"RELIANCE.NS", "TCS.NS"})

    def test_special_session_is_not_scanned_as_normal_market(self):
        calls = []
        engine = self._engine(fetch=lambda batch, now: calls.append(batch))
        with patch("india_trader.market_engine.scanner.trading_day",
                   return_value=(False, "NSE special session")):
            result = engine.cycle(NOW)
        self.assertEqual(result["status"], "MARKET_CLOSED")
        self.assertEqual(result["reason"], "NSE special session")
        self.assertEqual(calls, [])

    def test_candidate_signal_is_once_only_across_restart(self):
        manager = FakeManager()
        engine = self._engine(manager)
        first = engine.cycle(NOW)
        self.assertEqual(first["broad"]["shortlisted"], 1)
        self.assertEqual(first["detailed"]["verified_live"], 1)
        self.assertEqual(len(first["detailed"]["new_signals"]), 1)
        self.assertEqual(first["detailed"]["new_signals"][0]["status"], "AWAITING_AI_REVIEW")
        self.assertEqual(first["detailed"]["new_signals"][0]["metrics"]["spread_pct"], 0.1942)
        self.assertTrue(all(call[2] and call[3] is False for call in manager.calls[:2]))
        between_slots = engine.cycle(NOW + timedelta(seconds=30))
        self.assertIsNone(between_slots["broad"])
        self.assertEqual(between_slots["coverage"]["latest_broad_scanned"], 1)
        self.assertEqual(between_slots["coverage"]["reliable_live_now"], 1)
        reopened = self._engine(FakeManager())
        second = reopened.cycle(NOW + timedelta(minutes=1))
        self.assertEqual(second["detailed"]["new_signals"], [])
        self.assertEqual(len(reopened.state["signals"]), 1)
        self.assertFalse(reopened.state["orders_possible"])

    def test_failed_broker_feed_blocks_signal(self):
        engine = self._engine(FakeManager(fail=True))
        result = engine.cycle(NOW)
        self.assertEqual(result["detailed"]["verified_live"], 0)
        self.assertEqual(result["detailed"]["new_signals"], [])
        self.assertEqual(engine.state["candidates"]["RELIANCE.NS"]["status"], "DATA_UNAVAILABLE")

    def test_wide_or_missing_spread_blocks_signal(self):
        for ask in (None, 104.0):
            with self.subTest(ask=ask):
                engine = self._engine(FakeManager(ask=ask))
                result = engine.cycle(NOW)
                self.assertEqual(result["detailed"]["new_signals"], [])
                self.assertEqual(engine.state["candidates"]["RELIANCE.NS"]["status"],
                                 "QUALITY_BLOCKED")
                self.state.unlink()

    def test_candidate_expires_and_no_old_trigger_is_emitted(self):
        engine = self._engine(FakeManager(), candidate_ttl_minutes=1)
        engine.cycle(NOW)
        result = engine.cycle(NOW + timedelta(minutes=2))
        self.assertEqual(result["expired"], ["RELIANCE.NS"])
        self.assertEqual(result["detailed"]["attempted"], 0)

    def test_invalid_limits_and_corrupt_state_fail_closed(self):
        with self.assertRaises(EngineError):
            self._engine(max_spread_pct=0)
        with patch.dict("os.environ", {"FYERS_APP_ID": "APP-100", "FYERS_ACCESS_TOKEN": "token"}):
            with self.assertRaises(EngineError):
                self._engine(detailed_batch_size=10)
        self.state.write_text("{broken", encoding="utf-8")
        with self.assertRaises(EngineError):
            self._engine()

    def test_metrics_reject_missing_spread_without_inventing_one(self):
        metrics = _metrics(_bars(5), {"price": 103})
        self.assertIsNone(metrics["spread_pct"])
        self.assertGreater(metrics["turnover_inr"], 1_000_000)


if __name__ == "__main__":
    unittest.main()
