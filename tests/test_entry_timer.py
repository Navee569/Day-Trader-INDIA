"""Completed-bar and bounded-watch checks for Indian entry timer."""
import contextlib
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest

from india_trader import entry_timer as e
from india_trader.core import IST
from india_trader.trade_workflow import exit_plan


def chart(interval="5m", gap=None, volume=1000):
    step = 5 if interval == "5m" else 1
    n_open = 15 // step
    start = datetime(2026, 10, 28, 9, 15, tzinfo=IST)
    stamps, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for i in range(n_open + 3):
        if i == gap:
            continue
        close = 100 if i < n_open else 101 if i == n_open else 103
        stamps.append(int((start + timedelta(minutes=i * step)).timestamp()))
        opens.append(100)
        highs.append(102 if i < n_open else 104)
        lows.append(99)
        closes.append(close)
        volumes.append(volume)
    doc = {"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS", "currency": "INR",
                "dataGranularity": interval, "regularMarketTime": stamps[-1] + step * 60},
            "timestamp": stamps, "indicators": {"quote": [{"open": opens, "high": highs,
                 "low": lows, "close": closes, "volume": volumes}]}}]}}
    return json.dumps(doc).encode()


def at(hm):
    return datetime.fromisoformat("2026-10-28T" + hm + ("" if hm.count(":") == 2 else ":00") + "+05:30")


class EntryTimerTests(unittest.TestCase):
    def test_first_completed_orb_close_and_exit_handoff(self):
        c = e.parse_chart(chart(), "RELIANCE.NS", "5m")
        before = e.evaluate(c, at("09:39:59"))
        self.assertEqual(before["status"], "WAIT")
        result = e.evaluate(c, at("09:40"))
        self.assertEqual(result["status"], "TRIGGERED")
        self.assertEqual(result["trigger_at"], "2026-10-28T09:40:00+05:30")
        self.assertEqual(result["entry_price"], 103)
        self.assertEqual(exit_plan(result, tick_size="0.05")["status"], "PLANNED")

    def test_one_minute_and_volume_rule(self):
        c = e.parse_chart(chart("1m"), "RELIANCE.NS", "1m")
        self.assertEqual(e.evaluate(c, at("09:32"), interval="1m")["status"], "TRIGGERED")
        self.assertEqual(e.evaluate(c, at("09:32"), interval="1m", min_volume_x=1.5)["status"], "WAIT")

    def test_missing_bars_block_and_optional_volume_filter(self):
        self.assertEqual(e.evaluate(e.parse_chart(chart(gap=1), "RELIANCE.NS", "5m"), at("09:40"))["status"],
                         "DATA_GAP")
        zero = e.parse_chart(chart(volume=0), "RELIANCE.NS", "5m")
        self.assertEqual(e.evaluate(zero, at("09:40"))["status"], "TRIGGERED")
        self.assertEqual(e.evaluate(zero, at("09:40"))["volume_quality"], "MISSING_OR_ZERO")
        self.assertEqual(e.evaluate(zero, at("09:40"), min_volume_x=1)["status"], "VOLUME_UNVERIFIED")

    def test_wrong_symbol_currency_and_interval_rejected(self):
        with self.assertRaises(e.EntryError):
            e.parse_chart(chart(), "TCS.NS", "5m")
        with self.assertRaises(e.EntryError):
            e.parse_chart(chart(), "RELIANCE.NS", "1m")
        with self.assertRaises(e.EntryError):
            e.ticker("500325.BO")

    def test_live_requires_provider_last_trade_time(self):
        c = e.parse_chart(chart(), "RELIANCE.NS", "5m")
        c["data_time"] = None
        self.assertEqual(e.evaluate(c, at("09:40"), live=True)["status"], "DATA_UNVERIFIED")

    def test_market_closed_and_expiry(self):
        c = e.parse_chart(chart(), "RELIANCE.NS", "5m")
        self.assertEqual(e.evaluate(c, datetime(2026, 10, 20, 10, tzinfo=IST))["status"], "MARKET_CLOSED")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "entry.json"
            p.write_bytes(chart())
            with contextlib.redirect_stdout(io.StringIO()):
                code = e.main(["check", "RELIANCE", "--offline", str(p), "--now", "2026-10-28T09:31:00+05:30"])
            self.assertEqual(code, 1)

    def test_offline_watch_writes_once_and_replays_bars(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "RELIANCE.NS.json").write_bytes(chart())
            state, out = root / "state.json", root / "entry.json"
            args = ["watch", "RELIANCE", "--offline", str(root), "--now", "2026-10-28T09:15:00+05:30",
                    "--until", "09:45", "--state", str(state), "--out", str(out)]
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(e.main(args), 0)
            self.assertIn("09:35 bar closed", output.getvalue())
            self.assertEqual(json.loads(out.read_text())["status"], "TRIGGERED")
            first = out.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(e.main(args), 1)
            self.assertIn("ALREADY_RECORDED", output.getvalue())
            self.assertEqual(first, out.read_bytes())

    def test_watchlist_checks_each_symbol(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "RELIANCE.NS.json").write_bytes(chart())
            (root / "watchlist.json").write_text('{"hits":[{"symbol":"RELIANCE.NS"},{"symbol":"TCS.NS"}]}')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = e.main(["watchlist", "--watchlist", str(root / "watchlist.json"),
                               "--offline", str(root), "--now", "2026-10-28T09:40:00+05:30", "--json"])
            self.assertEqual(code, 0)
            rows = json.loads(output.getvalue())
            self.assertEqual(rows[0]["result"]["status"], "TRIGGERED")
            self.assertIsNotNone(rows[1]["error"])


if __name__ == "__main__":
    unittest.main()
