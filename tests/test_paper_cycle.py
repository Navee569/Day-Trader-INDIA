import unittest
from unittest.mock import patch

from india_trader.paper_cycle import paper_cycle


class PaperCycleTests(unittest.TestCase):
    def signal(self, end="2026-10-28T09:40:00+05:30", missing=False):
        return {"ticker": "RELIANCE.NS", "status": "TRIGGERED_LONG",
                "trigger_bar_end": end, "last_complete_bar": "2026-10-28T09:35:00+05:30",
                "volume_missing_in_some_bars": missing, "entry_reference": 100,
                "range_low": 95}

    def run_cycle(self, row):
        with patch("india_trader.paper_cycle.scan_watchlist", return_value={"results": [row]}):
            return paper_cycle("unused.txt", "2026-10-28T09:40:00+05:30", "0.01")

    def test_fresh_completed_signal_creates_only_paper_plan(self):
        result = self.run_cycle(self.signal())
        self.assertFalse(result["orders_possible"])
        self.assertEqual(result["results"][0]["decision"], "HYPOTHETICAL_PLAN_ONLY")
        self.assertEqual(result["results"][0]["paper_plan"]["mode"], "PAPER_ONLY")

    def test_old_signal_does_not_create_plan(self):
        result = self.run_cycle(self.signal(end="2026-10-28T09:35:00+05:30"))
        self.assertEqual(result["results"][0]["reason"], "OLD_SIGNAL")

    def test_missing_volume_does_not_create_plan(self):
        result = self.run_cycle(self.signal(missing=True))
        self.assertEqual(result["results"][0]["reason"], "VOLUME_NOT_VERIFIED")
