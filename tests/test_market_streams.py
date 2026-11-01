"""Offline read-only WebSocket normalization and failover tests."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from india_trader.core import DataError, IST
from india_trader.market_streams import (ReadOnlyStreamSupervisor,
                                         parse_fyers_message, parse_upstox_message)

NOW = datetime(2026, 10, 28, 9, 40, tzinfo=IST)
KEY = "NSE_EQ|INE002A01018"


class MarketStreamTests(unittest.TestCase):
    def test_upstox_v3_tick_requires_subscribed_key_and_fresh_trade_time(self):
        payload = {"feeds": {KEY: {"ltpc": {"ltp": 123.4,
                   "ltt": str(int((NOW - timedelta(seconds=5)).timestamp() * 1000))}}}}
        rows = parse_upstox_message(payload, {KEY: "RELIANCE.NS"}, NOW)
        self.assertEqual(rows[0]["provider"], "UPSTOX")
        self.assertEqual(rows[0]["ticker"], "RELIANCE.NS")
        self.assertFalse(rows[0]["orders_possible"])
        with self.assertRaises(DataError):
            parse_upstox_message(payload, {"NSE_EQ|WRONG": "RELIANCE.NS"}, NOW)
        payload["feeds"][KEY]["ltpc"]["ltt"] = str(int((NOW - timedelta(minutes=2)).timestamp() * 1000))
        with self.assertRaises(DataError):
            parse_upstox_message(payload, {KEY: "RELIANCE.NS"}, NOW)

    def test_fyers_tick_rejects_wrong_symbol_and_timestamp(self):
        payload = {"symbol": "NSE:RELIANCE-EQ", "ltp": 123.4,
                   "last_traded_time": int((NOW - timedelta(seconds=5)).timestamp())}
        self.assertEqual(parse_fyers_message(payload, {"NSE:RELIANCE-EQ": "RELIANCE.NS"}, NOW)[0]
                         ["provider"], "FYERS")
        with self.assertRaises(DataError):
            parse_fyers_message(payload, {"NSE:TCS-EQ": "TCS.NS"}, NOW)
        payload["last_traded_time"] = int((NOW + timedelta(minutes=1)).timestamp())
        with self.assertRaises(DataError):
            parse_fyers_message(payload, {"NSE:RELIANCE-EQ": "RELIANCE.NS"}, NOW)

    def test_supervisor_switches_to_fyers_when_upstox_socket_unavailable(self):
        quotes, statuses = [], []
        with tempfile.TemporaryDirectory() as folder:
            supervisor = ReadOnlyStreamSupervisor(["RELIANCE.NS"], quotes.append,
                                                  statuses.append, now=NOW,
                                                  audit_path=Path(folder) / "events.jsonl",
                                                  retry_seconds=1)
            class FakeSocket:
                def connect(self):
                    supervisor._message("FYERS", {"symbol": "NSE:RELIANCE-EQ", "ltp": 123.4,
                        "last_traded_time": int(NOW.timestamp())},
                        {"NSE:RELIANCE-EQ": "RELIANCE.NS"})
                    supervisor.stop()
                def disconnect(self):
                    pass
            with patch.object(supervisor, "_upstox_socket", side_effect=ImportError("missing SDK")), \
                    patch.object(supervisor, "_fyers_socket", return_value=FakeSocket()):
                supervisor.run()
            self.assertEqual(len(quotes), 1)
            self.assertEqual(quotes[0]["provider"], "FYERS")
            self.assertTrue(any(row["provider"] == "UPSTOX" and row["status"] == "UNAVAILABLE"
                                for row in statuses))
            self.assertIn("provider_selected", (Path(folder) / "events.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
