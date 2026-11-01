"""Offline quality and failover tests for the shared read-only data policy."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from types import SimpleNamespace

from india_trader.core import DataError, IST
from india_trader.market_data_manager import (FeedUnavailable, MarketDataManager,
                                              validate_candles)
from india_trader import market_scanner as scanner

NOW = datetime(2026, 10, 28, 9, 40, tzinfo=IST)
TICKER = "RELIANCE.NS"


def candles(last=4):
    start = NOW.replace(hour=9, minute=15)
    return [{"timestamp": (start + timedelta(minutes=5 * i)).isoformat(),
             "open": 100 + i, "high": 101 + i, "low": 99 + i,
             "close": 100.5 + i, "volume": 1000} for i in range(last + 1)]


def daily_rows():
    days = []
    cursor = NOW.date()
    while len(days) < 35:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return [{"date": day.isoformat(), "open": 100, "high": 101, "low": 99,
             "close": 100.5, "volume": 100000} for day in reversed(days)]


def env(**values):
    return patch.dict(os.environ, {"UPSTOX_ANALYTICS_TOKEN": "",
                               "FYERS_APP_ID": "", "FYERS_ACCESS_TOKEN": "", **values})


class MarketDataManagerTests(unittest.TestCase):
    def test_upstox_is_first_and_completed_bar_is_entry_ready(self):
        with tempfile.TemporaryDirectory() as folder, env(UPSTOX_ANALYTICS_TOKEN="test-secret"), \
                patch("india_trader.intraday_sources.upstox_instrument", return_value="INE002A01018"), \
                patch("india_trader.intraday_sources.upstox_bars", return_value=candles()) as upstox, \
                patch("india_trader.intraday_sources.upstox_market_status", return_value={
                    "exchange": "NSE", "state": "NORMAL_OPEN", "open": True,
                    "source": "Upstox exchange status"}), \
                patch("india_trader.intraday_sources.yahoo_bars") as yahoo:
            manager = MarketDataManager(Path(folder) / "events.jsonl", NOW)
            result = manager.fetch_candles(TICKER, "1d", require_entry_ready=True)
            self.assertEqual(result["provider"], "UPSTOX")
            self.assertTrue(result["entry_ready"])
            self.assertEqual(result["quality"]["last_bar_end"], NOW.isoformat())
            self.assertNotIn("test-secret", (Path(folder) / "events.jsonl").read_text())
            upstox.assert_called_once()
            yahoo.assert_not_called()

    def test_fyers_takes_over_after_upstox_outage_and_switch_is_audited(self):
        with tempfile.TemporaryDirectory() as folder, env(UPSTOX_ANALYTICS_TOKEN="test-secret",
               FYERS_APP_ID="APP-100", FYERS_ACCESS_TOKEN="another-secret"), \
                patch("india_trader.intraday_sources.upstox_instrument", return_value="INE002A01018"), \
                patch("india_trader.intraday_sources.upstox_bars", side_effect=[candles(),
                      ConnectionError("offline mock")]), \
                patch("india_trader.intraday_sources.upstox_market_status", return_value={
                    "exchange": "NSE", "state": "NORMAL_OPEN", "open": True,
                    "source": "Upstox exchange status"}), \
                patch("india_trader.intraday_sources.fyers_bars", return_value=candles()) as fyers:
            path = Path(folder) / "events.jsonl"
            manager = MarketDataManager(path, NOW)
            self.assertEqual(manager.fetch_candles(TICKER, "1d")["provider"], "UPSTOX")
            result = manager.fetch_candles(TICKER, "1d", require_entry_ready=True)
            self.assertEqual(result["provider"], "FYERS")
            self.assertEqual(result["attempts"][0]["reason"], "OUTAGE")
            self.assertEqual(manager.health.snapshot()["UPSTOX"]["status"], "FAILED")
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(events[-1]["kind"], "provider_switch")
            self.assertEqual((events[-1]["from"], events[-1]["to"]), ("UPSTOX", "FYERS"))
            self.assertNotIn("secret", path.read_text())
            fyers.assert_called_once()

    def test_provider_switch_reconciles_same_bar_across_restart(self):
        disagree = candles()
        disagree[-1]["open"] = 115
        disagree[-1]["high"] = 117
        disagree[-1]["low"] = 114
        disagree[-1]["close"] = 116
        with tempfile.TemporaryDirectory() as folder, env(UPSTOX_ANALYTICS_TOKEN="test-secret",
             FYERS_APP_ID="APP-100", FYERS_ACCESS_TOKEN="another-secret"), \
                patch("india_trader.intraday_sources.upstox_instrument", return_value="INE002A01018"), \
                patch("india_trader.intraday_sources.upstox_bars", side_effect=[candles(),
                      ConnectionError("offline mock")]), \
                patch("india_trader.intraday_sources.fyers_bars", return_value=disagree):
            path = Path(folder) / "events.jsonl"
            self.assertEqual(MarketDataManager(path, NOW).fetch_candles(TICKER, "1d")["provider"],
                             "UPSTOX")
            restarted = MarketDataManager(path, NOW)
            with self.assertRaises(FeedUnavailable) as raised:
                restarted.fetch_candles(TICKER, "1d", allow_yahoo=False)
            self.assertEqual(raised.exception.attempts[1]["source"], "FYERS")
            self.assertEqual(raised.exception.attempts[1]["reason"], "INVALID_DATA")
            self.assertEqual(restarted.last_source[(TICKER, "candles")], "UPSTOX")

    def test_gaps_missing_volume_and_stale_candles_fail_closed(self):
        rows = candles()
        with self.assertRaises(DataError):
            validate_candles(rows[:2] + rows[3:], TICKER, now=NOW)
        rows[2]["volume"] = None
        with self.assertRaises(DataError):
            validate_candles(rows, TICKER, now=NOW)
        stale = candles(last=2)
        with env(UPSTOX_ANALYTICS_TOKEN="test-secret"), \
                patch("india_trader.intraday_sources.upstox_instrument", return_value="INE002A01018"), \
                patch("india_trader.intraday_sources.upstox_bars", return_value=stale):
            with self.assertRaises(FeedUnavailable) as raised:
                MarketDataManager(audit_path=None, now=NOW).fetch_candles(
                    TICKER, "1d", require_entry_ready=True, allow_yahoo=False)
        self.assertEqual(raised.exception.attempts[0]["status"], "FAILED")

    def test_unofficial_yahoo_is_research_only_even_with_fresh_bars(self):
        with env(), patch("india_trader.intraday_sources.yahoo_bars", return_value=candles()):
            manager = MarketDataManager(audit_path=None, now=NOW)
            result = manager.fetch_candles(TICKER, "1d")
            self.assertEqual(result["provider"], "YAHOO")
            self.assertFalse(result["entry_ready"])
            with self.assertRaises(FeedUnavailable):
                manager.fetch_candles(TICKER, "1d", require_entry_ready=True)

    def test_closed_exchange_status_blocks_entry_even_with_fresh_bars(self):
        with env(UPSTOX_ANALYTICS_TOKEN="test-secret"), \
                patch("india_trader.intraday_sources.upstox_instrument", return_value="INE002A01018"), \
                patch("india_trader.intraday_sources.upstox_bars", return_value=candles()), \
                patch("india_trader.intraday_sources.upstox_market_status", return_value={
                    "exchange": "NSE", "state": "NORMAL_CLOSE", "open": False,
                    "source": "Upstox exchange status"}):
            with self.assertRaises(FeedUnavailable):
                MarketDataManager(audit_path=None, now=NOW).fetch_candles(
                    TICKER, "1d", require_entry_ready=True, allow_yahoo=False)

    def test_quote_falls_back_to_fyers_only_when_status_and_time_are_valid(self):
        with env(UPSTOX_ANALYTICS_TOKEN="test-secret", FYERS_APP_ID="APP-100",
                 FYERS_ACCESS_TOKEN="another-secret"), \
                patch("india_trader.intraday_sources.upstox_quote",
                      side_effect=ConnectionError("offline mock")), \
                patch("india_trader.intraday_sources.fyers_quote", return_value={
                    "ticker": TICKER, "price": 123.4, "currency": "INR",
                    "time": (NOW - timedelta(seconds=5)).isoformat(), "source": "FYERS data quote"}), \
                patch("india_trader.intraday_sources.upstox_market_status", return_value={
                    "exchange": "NSE", "state": "NORMAL_OPEN", "open": True,
                    "source": "Upstox exchange status"}):
            result = MarketDataManager(audit_path=None, now=NOW).fetch_quote(
                TICKER, require_fresh=True)
        self.assertEqual(result["provider"], "FYERS")
        self.assertTrue(result["entry_ready"])
        self.assertEqual(result["attempts"][0]["reason"], "OUTAGE")

    def test_yahoo_quote_fallback_preserves_prior_close_for_research_alerts(self):
        prior = {**candles(last=0)[0], "timestamp": (NOW - timedelta(days=1, minutes=25)).isoformat(),
                 "close": 99.5}
        with env(), patch("india_trader.nse_sources.cm_live_quote",
                          return_value={"status": "UNAVAILABLE", "quote": None}), \
                patch("india_trader.intraday_sources.yahoo_bars",
                      return_value=[prior] + candles()):
            result = MarketDataManager(None, NOW).fetch_quote(TICKER)
        self.assertEqual(result["provider"], "YAHOO")
        self.assertEqual(result["prev_close"], 99.5)
        self.assertFalse(result["entry_ready"])

    def test_scanner_retries_individual_chart_when_yahoo_batch_fails(self):
        series = scanner.parse_chart({"chart": {"result": [{
            "meta": {"symbol": TICKER, "currency": "INR"},
            "timestamp": [int(datetime.fromisoformat(c["timestamp"]).timestamp()) for c in candles()],
            "indicators": {"quote": [{k: [row[k] for row in candles()]
                                      for k in ("open", "high", "low", "close", "volume")}]}}]}}
            , TICKER)
        with env(), patch.object(scanner, "yahoo_get", side_effect=[ValueError("batch failed"),
               json.dumps({"chart": {"result": [{"meta": series["meta"],
               "timestamp": series["t"], "indicators": {"quote": [{
                   "open": series["o"], "high": series["h"], "low": series["l"],
                   "close": series["c"], "volume": series["v"]}]}}]}}).encode()]):
            data, failed = scanner.fetch_spark([TICKER], "1d", "5m", 0)
        self.assertIn(TICKER, data)
        self.assertEqual(failed, {})

    def test_yahoo_batch_needs_provider_echoed_symbol(self):
        payload = {"spark": {"result": [{"symbol": TICKER, "response": [{
            "meta": {"currency": "INR"}, "timestamp": [int(NOW.timestamp())],
            "indicators": {"quote": [{"close": [100]}]}}]}]}}
        self.assertEqual(scanner.parse_spark(payload, {TICKER: TICKER}), {})

    def test_rate_limit_uses_cooldown_and_unofficial_fallback(self):
        limited = urllib.error.HTTPError("https://api.upstox.com", 429, "too many", None, None)
        with env(UPSTOX_ANALYTICS_TOKEN="test-secret"), \
                patch("india_trader.intraday_sources.upstox_instrument", return_value="INE002A01018"), \
                patch("india_trader.intraday_sources.upstox_bars", side_effect=limited) as upstox, \
                patch("india_trader.intraday_sources.yahoo_bars", return_value=candles()):
            manager = MarketDataManager(audit_path=None, now=NOW)
            first = manager.fetch_candles(TICKER, "1d")
            second = manager.fetch_candles(TICKER, "1d")
        self.assertEqual(first["attempts"][0]["reason"], "RATE_LIMIT")
        self.assertEqual(second["attempts"][0]["status"], "COOLDOWN")
        self.assertEqual(upstox.call_count, 1)
        self.assertFalse(second["entry_ready"])

    def test_daily_scanner_uses_upstox_before_nse_and_yahoo(self):
        clock = NOW.replace(hour=16, minute=0)
        with env(UPSTOX_ANALYTICS_TOKEN="test-secret"), \
                patch("india_trader.intraday_sources.upstox_instrument", return_value="INE002A01018"), \
                patch("india_trader.intraday_sources.upstox_daily_bars", return_value=daily_rows()) as upstox, \
                patch.object(MarketDataManager, "_nse_daily") as nse, \
                patch.object(scanner, "yahoo_get") as yahoo:
            data, failed = scanner.fetch_spark([TICKER], "3mo", "1d", 0,
                SimpleNamespace(max_source_fallback=0), manager=MarketDataManager(None, clock))
        self.assertIn(TICKER, data)
        self.assertEqual(failed, {})
        self.assertIn("Upstox", data[TICKER]["meta"]["data_provider"])
        upstox.assert_called_once()
        nse.assert_not_called()
        yahoo.assert_not_called()

    def test_daily_manager_uses_nse_final_reports_before_yahoo(self):
        with env(), patch.object(MarketDataManager, "_nse_daily", return_value=daily_rows()) as nse:
            result = MarketDataManager(None, NOW.replace(hour=16)).fetch_daily(TICKER)
        self.assertEqual(result["provider"], "NSE")
        self.assertFalse(result["entry_ready"])
        nse.assert_called_once()


if __name__ == "__main__":
    unittest.main()
