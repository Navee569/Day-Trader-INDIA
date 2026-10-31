"""Offline India paper alert rule, quote, clock and CLI handoff checks."""
import contextlib
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest

from india_trader import alerts as a
from india_trader.budget_manager import _price_record
from india_trader.core import IST


NOW = datetime(2026, 10, 29, 9, 41, tzinfo=IST)
PLAN = {"mode": "PAPER_PLAN_ONLY", "status": "PLANNED", "ticker": "RELIANCE.NS",
        "date": "2026-10-29", "stop": 95, "target": 110, "flat_by_ist": "15:10"}


def chart(ticker="RELIANCE.NS", price=94, prev=100, when=None, currency="INR"):
    when = when or NOW - timedelta(minutes=1)
    return {"chart": {"result": [{"meta": {"symbol": ticker, "currency": currency,
        "regularMarketPrice": price, "chartPreviousClose": prev,
        "regularMarketTime": int(when.timestamp())}}], "error": None}}


class AlertTests(unittest.TestCase):
    def test_ist_calendar_clock_and_closed_day_fail_closed(self):
        self.assertTrue(a.market_clock(NOW)["session_open"])
        self.assertFalse(a.market_clock(datetime(2026, 10, 20, 10, tzinfo=IST))["trading_day"])
        self.assertFalse(a.market_clock(datetime(2027, 10, 29, 10, tzinfo=IST))["calendar_verified"])
        self.assertFalse(a.market_clock(datetime(2027, 10, 29, 10, tzinfo=IST))["session_open"])
        bell = {"id": "bell", "type": "opening_bell"}
        rings, _, notes = a.evaluate([bell], {}, {}, datetime(2026, 10, 20, 10, tzinfo=IST))
        self.assertEqual(rings, [])
        self.assertTrue(notes)

    def test_plan_stop_target_flat_once_and_date(self):
        rules = a.from_plan(PLAN)
        self.assertEqual([r["type"] for r in rules], ["stop", "target", "time"])
        quotes = {"RELIANCE.NS": a.parse_yahoo(chart(), "RELIANCE.NS")}
        rings, state, _ = a.evaluate(rules, quotes, {}, NOW)
        self.assertEqual([r["type"] for r in rings], ["stop"])
        self.assertEqual(a.evaluate(rules, quotes, state, NOW)[0], [])
        later = datetime(2026, 10, 29, 15, 10, tzinfo=IST)
        flat, state, _ = a.evaluate(rules, {}, state, later)
        self.assertEqual([r["type"] for r in flat], ["time"])
        self.assertIn("broker flatten", flat[0]["message"])
        self.assertEqual(a.evaluate(rules, {}, state, later)[0], [])
        tomorrow = datetime(2026, 10, 30, 9, 41, tzinfo=IST)
        self.assertEqual(a.evaluate(rules, quotes, state, tomorrow)[0], [])

    def test_rearm_percent_direction_time_and_opening_bell(self):
        price_rule = {"id": "level", "type": "above", "ticker": "TCS.NS",
                      "price": 102, "rearm": True}
        pct_rule = {"id": "pct", "type": "pct_move", "ticker": "TCS.NS",
                    "pct": 3, "direction": "up"}
        bell = {"id": "bell", "type": "opening_bell"}
        timed = {"id": "timed", "type": "time", "at": "09:40"}
        rules = [price_rule, pct_rule, bell, timed]
        quote = {"TCS.NS": a.parse_yahoo(chart("TCS.NS", 104, 100), "TCS.NS")}
        rings, state, _ = a.evaluate(rules, quote, {}, NOW)
        self.assertEqual({r["id"] for r in rings}, {"level", "pct", "bell", "timed"})
        low = {"TCS.NS": a.parse_yahoo(chart("TCS.NS", 101, 100), "TCS.NS")}
        quiet, state, _ = a.evaluate(rules, low, state, NOW)
        self.assertEqual(quiet, [])
        high, _, _ = a.evaluate(rules, quote, state, NOW)
        self.assertEqual([r["id"] for r in high], ["level"])

    def test_quote_validation_and_stale_price_suppression(self):
        with self.assertRaises(a.AlertError):
            a.parse_yahoo(chart(currency="USD"), "RELIANCE.NS")
        with self.assertRaises(a.AlertError):
            a.parse_yahoo(chart(price=float("nan")), "RELIANCE.NS")
        rule = {"id": "stop", "type": "stop", "ticker": "RELIANCE.NS", "price": 95}
        stale = a.parse_yahoo(chart(when=NOW - timedelta(minutes=16)), "RELIANCE.NS")
        rings, _, notes = a.evaluate([rule], {"RELIANCE.NS": stale}, {}, NOW)
        self.assertEqual(rings, [])
        self.assertIn("stale", " ".join(notes))
        fresh = a.parse_yahoo(chart(), "RELIANCE.NS")
        doc = a.prices_file({"RELIANCE.NS": fresh}, now=NOW)
        self.assertEqual(_price_record(doc, "RELIANCE.NS", NOW)[0], 94)

    def test_cli_offline_quote_rules_check_reset_and_no_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            offline = root / "quotes"
            offline.mkdir()
            (offline / "yahoo-RELIANCE.NS.json").write_text(json.dumps(chart()))
            plan = root / "plan.json"
            plan.write_text(json.dumps(PLAN))
            rules, state, prices = root / "alerts.json", root / "state.json", root / "prices.json"
            base = ["--now", NOW.isoformat(), "--rules", str(rules), "--state", str(state),
                    "--prices", str(prices), "--plan", str(plan)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(a.main(base + ["add", "--from-plan"]), 0)
                self.assertEqual(a.main(base + ["quote", "RELIANCE", "--offline", str(offline), "--write"]), 0)
                self.assertEqual(a.main(base + ["check", "--saved", "--no-write"]), 0)
            self.assertFalse(state.exists() and json.loads(state.read_text()).get("rung"))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(a.main(base + ["check", "--offline", str(offline)]), 0)
                self.assertEqual(a.main(base + ["check", "--saved"]), 1)
                self.assertEqual(a.main(base + ["list"]), 0)
            self.assertEqual(len(json.loads(state.read_text())["rung"]), 1)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(a.main(base + ["reset", "reliance-ns-plan-stop"]), 0)
            self.assertFalse(json.loads(state.read_text())["rung"])
            self.assertIn("RELIANCE.NS", json.loads(prices.read_text())["prices"])

    def test_cli_custom_rules_and_holdings_quote_handoff(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            offline = root / "quotes"
            offline.mkdir()
            (offline / "yahoo-TCS.NS.json").write_text(json.dumps(chart("TCS.NS", 104, 100)))
            (offline / "yahoo-RELIANCE.NS.json").write_text(json.dumps(chart()))
            (root / "portfolio.json").write_text(json.dumps({"positions": [{"ticker": "TCS.NS", "quantity": 2}]}))
            (root / "plan.json").write_text(json.dumps(PLAN))
            base = ["--now", NOW.isoformat(), "--rules", str(root / "alerts.json"),
                    "--state", str(root / "state.json"), "--prices", str(root / "prices.json"),
                    "--plan", str(root / "plan.json"), "--portfolio", str(root / "portfolio.json")]
            captured = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(a.main(base + ["add", "--ticker", "TCS", "--pct", "3", "--direction", "up"]), 0)
                self.assertEqual(a.main(base + ["add", "--opening-bell"]), 0)
                self.assertEqual(a.main(base + ["add", "--at", "15:10", "--before-close"]), 0)
            with contextlib.redirect_stdout(captured):
                self.assertEqual(a.main(base + ["quote", "--holdings", "--offline", str(offline), "--write"]), 0)
            self.assertTrue(json.loads(captured.getvalue())["quotes"]["TCS.NS"]["fresh_for_alert"])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(a.main(base + ["check", "--saved"]), 0)
            saved = json.loads((root / "prices.json").read_text())
            self.assertEqual(set(saved["prices"]), {"TCS.NS", "RELIANCE.NS"})
            self.assertEqual(len(json.loads((root / "state.json").read_text())["rung"]), 2)
            with self.assertRaises(a.AlertError):
                a.validate({"id": "late", "type": "time", "at": "15:25", "before_close": True})


if __name__ == "__main__":
    unittest.main()
