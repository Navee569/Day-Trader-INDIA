"""Offline checks for the Indian news workflow."""
import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest

from india_trader import news_monitor as n


NSE = b'''<rss><channel>
<item><title>Financial Results</title><description>Reliance Industries Limited has informed the Exchange regarding financial results. |SUBJECT: Financial Results</description><pubDate>30-Oct-2026 10:00:00</pubDate><link></link></item>
<item><title>Reliance Mutual Fund NAV</title><description>Nippon Life India Asset Management Limited has informed the Exchange. |SUBJECT: Declaration of NAV</description><pubDate>30-Oct-2026 10:00:00</pubDate><link></link></item>
</channel></rss>'''
YAHOO_EMPTY = b'<rss><channel></channel></rss>'
GOOGLE = b'''<rss><channel><item><title>Reliance Industries raises dividend</title><description>Reliance Industries announced a dividend.</description><link>https://news.google.com/a</link><guid>abc</guid><pubDate>Fri, 30 Oct 2026 04:45:00 GMT</pubDate></item></channel></rss>'''
CHART = json.dumps({"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS", "currency": "INR",
           "regularMarketPrice": 103, "chartPreviousClose": 100, "regularMarketTime": 1793336400}}]}}).encode()


class NewsMonitorTest(unittest.TestCase):
    def test_nse_time_and_company_filter(self):
        items = n.tag_and_match(n.parse_rss(NSE, "NSE announcements"),
                                {"RELIANCE.NS": "Reliance Industries Limited"})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["time"].astimezone(n.IST).hour, 10)
        self.assertTrue(items[0]["important"])
        self.assertEqual(items[0]["link_type"], "company_search_not_individual_filing")

    def test_dedupe_merges_touches_without_collapsing_company_search(self):
        a = n.tag_and_match(n.parse_rss(NSE, "NSE announcements"),
                            {"RELIANCE.NS": "Reliance Industries Limited"})[0]
        b = dict(a, id="other", title="Another board announcement", touches=["TCS.NS"])
        self.assertEqual(len(n.dedupe([a, b])), 2)
        b["title"] = a["title"]
        self.assertEqual(n.dedupe([a, b])[0]["touches"], ["RELIANCE.NS", "TCS.NS"])

    def test_chart_validation(self):
        self.assertEqual(n.parse_chart(CHART, "RELIANCE.NS")["pct"], 3)
        with self.assertRaises(n.NewsError):
            n.parse_chart(CHART, "TCS.NS")

    def test_watch_sources(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "watch.json").write_text('{"hits":[{"symbol":"RELIANCE.NS"}]}')
            (root / "plan.json").write_text('{"symbol":"TCS.NS"}')
            (root / "portfolio.json").write_text('{"positions":[{"ticker":"INFY.NS"}]}')
            (root / "universe.txt").write_text("RELIANCE.NS|Reliance Industries Limited|X\n")
            out = n.watch_from_files(root / "portfolio.json", root / "watch.json",
                                     root / "plan.json", root / "universe.txt")
            self.assertEqual(set(out), {"RELIANCE.NS", "TCS.NS", "INFY.NS"})
            self.assertEqual(out["RELIANCE.NS"], "Reliance Industries Limited")

    def test_offline_fallback_state_and_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "nse-announcements.xml").write_bytes(NSE)
            (root / "yahoo-RELIANCE.NS.xml").write_bytes(YAHOO_EMPTY)
            (root / "google-RELIANCE.NS.xml").write_bytes(GOOGLE)
            (root / "chart-RELIANCE.NS.json").write_bytes(CHART)
            (root / "universe.txt").write_text("RELIANCE.NS|Reliance Industries Limited|X\n")
            state = root / "state.json"
            args = ["--tickers", "RELIANCE", "--universe", str(root / "universe.txt"),
                    "--offline", str(root), "--state", str(state), "--now", "2026-10-30T11:00:00+05:30",
                    "--json"]
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(n.main(args + ["--dry-run"]), 0)
            doc = json.loads(output.getvalue())
            self.assertEqual(len(doc["items"]), 2)
            self.assertEqual(doc["sources"]["headlines:RELIANCE.NS"]["provider"], "Google News")
            self.assertTrue(doc["moves"][0]["moving"])
            self.assertFalse(state.exists())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(n.main(args), 0)
            self.assertTrue(state.exists())
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(n.main(args), 1)
            self.assertEqual(json.loads(output.getvalue())["items"], [])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(n.main(args + ["--all"]), 1)
            self.assertEqual(len(json.loads(output.getvalue())["items"]), 2)

    def test_partial_feed_is_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "nse-announcements.xml").write_bytes(NSE)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = n.main(["--tickers", "TCS", "--offline", str(root), "--json", "--dry-run",
                               "--now", "2026-10-30T11:00:00+05:30"])
            doc = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertTrue(doc["partial"])
            self.assertTrue(any("Google headlines" in note for note in doc["notes"]))

    def test_select_only_requested_official_feed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "nse-announcements.xml").write_bytes(NSE)
            (root / "universe.txt").write_text("RELIANCE.NS|Reliance Industries Limited|X\n")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(n.main(["--tickers", "RELIANCE", "--universe", str(root / "universe.txt"),
                                         "--offline", str(root), "--feeds", "announcements",
                                         "--no-headlines", "--no-moves", "--dry-run", "--json",
                                         "--now", "2026-10-30T11:00:00+05:30"]), 0)
            doc = json.loads(output.getvalue())
            self.assertEqual(set(doc["sources"]), {"nse_announcements"})
            self.assertEqual(len(doc["items"]), 1)


if __name__ == "__main__":
    unittest.main()
