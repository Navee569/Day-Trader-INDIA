"""India paper exit-plan behavior and downstream handoff."""
import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest

from india_trader import exit_planner as x
from india_trader import entry_timer as e
from india_trader.core import IST
from india_trader.paper_orders import risk_review, size_paper_order
from tests.test_entry_timer import chart


ENTRY = {"status": "TRIGGERED", "ticker": "RELIANCE.NS", "setup": "orb",
         "bar_interval": "5m", "entry_price": 103, "trigger_at": "2026-10-28T09:40:00+05:30",
         "trigger_bar_index": 4, "range_high": 102, "range_low": 99,
         "opening_minutes": 15, "data_origin": "offline_test", "orders_possible": False}


def run(*args):
    with contextlib.redirect_stdout(io.StringIO()) as output:
        code = x.main(list(args))
    return code, output.getvalue()


class ExitPlannerTests(unittest.TestCase):
    def test_default_midpoint_and_budget_contract(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "entry.json"
            path.write_text(json.dumps(ENTRY))
            code, output = run("plan", "--from", str(path), "--tick", "0.05",
                               "--now", "2026-10-28T09:40:00+05:30", "--json")
            self.assertEqual(code, 0)
            p = json.loads(output)
            self.assertEqual(p["stop"], 100.5)
            self.assertEqual(p["target"], 108)
            self.assertEqual(p["reward_risk"], 2)
            self.assertTrue(p["entry_confirmed"])
            self.assertEqual(size_paper_order(p, fee_buffer="0")["status"], "PROPOSED")

    def test_stop_choices_and_target_rounding(self):
        info = {"ticker": "RELIANCE.NS", "entry": x.Decimal("103"), "setup": "orb",
                "interval": "5m", "date": datetime(2026, 10, 28).date(), "entry_time": None,
                "trigger_at": None, "confirmed": False, "or_high": x.Decimal("102"),
                "or_low": x.Decimal("99"), "or_minutes": 15, "source": "test",
                "data_origin": None, "entry_bar": None}
        flat = x.time(15, 10)
        self.assertEqual(x.calculate(info, "or-low", "1R", "0.05", flat, x.Decimal(1))["stop"], 99)
        self.assertEqual(x.calculate(info, "pct:1", "2R", "0.05", flat, x.Decimal(1))["stop"], 101.95)
        self.assertEqual(x.calculate(info, "price:100", "price:110.03", "0.05", flat, x.Decimal(1))["target"], 110.05)

    def test_bad_stops_target_tick_and_level_default_rejected(self):
        base = ["plan", "--symbol", "RELIANCE", "--entry", "103", "--date", "2026-10-28",
                "--tick", "0.05", "--now", "2026-10-28T09:30:00+05:30"]
        self.assertEqual(run(*base)[0], 1)  # level setup needs explicit stop
        self.assertEqual(run(*base, "--stop", "price:103")[0], 1)
        self.assertEqual(run(*base, "--stop", "price:102", "--target", "price:103")[0], 1)
        self.assertEqual(run(*base, "--stop", "price:102.99")[0], 1)
        self.assertEqual(run(*base, "--stop", "price:100", "--flat-by", "15:20")[0], 1)
        self.assertEqual(run(*base, "--stop", "price:100", "--side", "short")[0], 1)

    def test_plan_ahead_does_not_pass_paper_risk(self):
        code, output = run("plan", "--symbol", "RELIANCE", "--entry", "103",
                           "--stop", "price:100", "--tick", "0.05", "--date", "2026-10-28",
                           "--time", "09:40", "--entry-bar", "5",
                           "--now", "2026-10-28T09:30:00+05:30", "--json")
        self.assertEqual(code, 0)
        p = json.loads(output)
        self.assertFalse(p["entry_confirmed"])
        self.assertIsNone(p["trigger_at"])
        self.assertEqual(p["entry_bar"], 5)
        order = size_paper_order(p, fee_buffer="0")
        review = risk_review(order, "2026-10-28T09:40:00+05:30")
        self.assertEqual(review["status"], "BLOCK")
        self.assertIn("UNTRIGGERED_PLAN", review["reasons"])

    def test_entry_after_flat_by_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "entry.json"
            path.write_text(json.dumps(ENTRY))
            self.assertEqual(run("plan", "--from", str(path), "--tick", "0.05",
                                 "--flat-by", "09:35")[0], 1)

    def test_atr_uses_only_completed_bars_and_rejects_gaps(self):
        c = e.parse_chart(chart("1m"), "RELIANCE.NS", "1m")
        day = datetime(2026, 10, 28).date()
        through = datetime(2026, 10, 28, 9, 32, tzinfo=IST)
        atr, count, available = x.atr_from_chart(c, day, "1m", through)
        self.assertEqual(count, 14)
        self.assertEqual(available, 17)
        self.assertGreater(atr, 0)
        with self.assertRaises(x.ExitError):
            x.atr_from_chart(e.parse_chart(chart("1m", gap=2), "RELIANCE.NS", "1m"),
                             day, "1m", through)

    def test_atr_plan_from_saved_chart_and_show(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            entry = root / "entry.json"
            entry.write_text(json.dumps(ENTRY))
            saved = root / "RELIANCE.NS.json"
            saved.write_bytes(chart())
            plan = root / "plan.json"
            code, _ = run("plan", "--from", str(entry), "--stop", "atr:1.5",
                          "--offline", str(saved), "--tick", "0.05", "--write", str(plan),
                          "--now", "2026-10-28T09:40:00+05:30")
            self.assertEqual(code, 0)
            doc = json.loads(plan.read_text())
            self.assertEqual(doc["atr_bars"], 5)
            self.assertIn("ATR(14)", doc["stop_note"])
            code, shown = run("show", "--plan", str(plan), "--now", "2026-10-28T09:40:00+05:30")
            self.assertEqual(code, 0)
            self.assertIn("different session", shown)

    def test_config_minimum_reward_warning(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text('{"flat_by":"14:45","min_reward_risk":3}')
            code, output = run("plan", "--symbol", "RELIANCE", "--entry", "103",
                               "--stop", "price:100", "--tick", "0.05", "--date", "2026-10-28",
                               "--config", str(cfg), "--now", "2026-10-28T09:30:00+05:30", "--json")
            self.assertEqual(code, 0)
            doc = json.loads(output)
            self.assertEqual(doc["flat_by_ist"], "14:45")
            self.assertTrue(any("below configured minimum" in w for w in doc["warnings"]))

    def test_price_override_is_plan_ahead_on_original_session(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "entry.json"
            path.write_text(json.dumps(ENTRY))
            code, output = run("plan", "--from", str(path), "--entry", "104",
                               "--tick", "0.05", "--now", "2026-10-28T09:40:00+05:30", "--json")
            self.assertEqual(code, 0)
            doc = json.loads(output)
            self.assertEqual(doc["date"], "2026-10-28")
            self.assertFalse(doc["entry_confirmed"])
            self.assertIsNone(doc["trigger_at"])
            self.assertTrue(any("As-of override" in w for w in doc["warnings"]))


if __name__ == "__main__":
    unittest.main()
