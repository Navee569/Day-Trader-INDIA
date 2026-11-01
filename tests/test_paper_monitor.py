"""Offline schedule, once-only state, and paper-event checks."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess

from india_trader import paper_monitor as m
from india_trader.core import IST


class PaperMonitorTests(unittest.TestCase):
    def test_due_intervals_and_new_earnings_only(self):
        now = datetime(2026, 10, 28, 10, tzinfo=IST)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rules = root / "alerts.json"
            rules.write_text('{"rules":[]}')
            calls = []
            filing = ["old"]
            def run(args):
                calls.append(args[0])
                return {
                    "market-calendar": {"status": "OK"},
                    "alerts": {"rings": [{"message": "paper target touched"}]},
                    "news": {"items": [{"new": True, "title": "Test filing", "url": "https://example.com"}]},
                    "insider-tracker": {"filings": [{"id": "current:1", "symbol": "RELIANCE",
                                                   "filed_at": now.isoformat(), "xbrl_url": "https://example.com"}]},
                    "earnings-analyst": {"results": [{"ticker": "RELIANCE.NS", "rows": [
                        {"filing_url": filing[0], "filed_at": now.isoformat()}]}]},
                }[args[0]]
            state, log = root / "state.json", root / "events.jsonl"
            events = m.check_once(now, ["RELIANCE"], state=state, log=log, alert_rules=rules, run=run)
            self.assertEqual({x["kind"] for x in events}, {"paper_alert", "news", "insider_filing"})
            self.assertEqual(len(calls), 5)
            self.assertEqual(m.check_once(now + timedelta(seconds=10), ["RELIANCE"],
                                          state=state, log=log, alert_rules=rules, run=run), [])
            self.assertEqual(len(calls), 5)
            filing[0] = "new"
            later = m.check_once(now + timedelta(hours=6), ["RELIANCE"], state=state,
                                 log=log, alert_rules=rules, run=run)
            self.assertIn("earnings_filing", {x["kind"] for x in later})
            self.assertGreater(len(log.read_text().splitlines()), 3)

    def test_watchlist_validation_and_no_alert_without_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            watch = root / "watch.txt"
            watch.write_text("# example\nRELIANCE\nRELIANCE.NS\n")
            self.assertEqual(m.symbols_from(watch), ["RELIANCE"])
            self.assertNotIn("alerts", [name for name, _ in m.jobs(["RELIANCE"], [],
                              datetime(2026, 10, 28, 10, tzinfo=IST), root / "none.json")])

    def test_public_source_failure_is_logged_without_raw_stderr(self):
        failure = subprocess.CompletedProcess([], 2, "", "public NSE source unavailable: getaddrinfo failed")
        with patch.object(m.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(m.MonitorError, "public source unavailable") as caught:
                m.command(["insider-tracker", "new", "--symbol", "RELIANCE"])
        self.assertNotIn("getaddrinfo", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
